"""Provider-neutral Watch lifecycle observation and authenticated resume."""

# ruff: noqa: EM101

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from cqmgr.application.ports.apply_records import ApplyRecordRepositoryStatus
from cqmgr.application.ports.coordination import (
    BudgetRequest,
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
)
from cqmgr.application.ports.watch import (
    WatchCheckpointRepositoryStatus,
    WatchObservationRequest,
    WatchObservationTransientError,
)
from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    ApplyRecordState,
    UnknownDispatchResolution,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import (
    Completeness,
    EvidenceGap,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.status import QuotaRequestStatus, WatchCondition, WatchDisposition
from cqmgr.domain.watch import (
    WatchAggregate,
    WatchCheckpoint,
    WatchChildIdentity,
    WatchChildLineage,
    WatchChildSummary,
    WatchEventKind,
    WatchResultData,
    WatchResumeClaims,
    WatchStreamEvent,
    WatchSubject,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from cqmgr.application.ports.apply_records import ApplyRecordRepository
    from cqmgr.application.ports.coordination import (
        BudgetCoordinator,
        CancellationToken,
        JitterSource,
    )
    from cqmgr.application.ports.secrets import SecretValue
    from cqmgr.application.ports.watch import (
        WatchCheckpointRepository,
        WatchClock,
        WatchObservation,
        WatchObservationReader,
        WatchResumeCodec,
        WatchStreamIdSource,
    )
    from cqmgr.domain.apply_records import (
        ApplyRecord,
        UnknownResolutionEvidence,
    )


class WatchStartError(Exception):
    """Watch could not authenticate a complete subject before streaming."""

    def __init__(self, code: str, exit_class: ExitClass) -> None:
        """Retain one stable surface-mappable failure."""
        super().__init__(code)
        self.code = StableSymbol(code)
        self.exit_class = exit_class


def _error(code: str, exit_class: ExitClass) -> WatchStartError:
    """Build one stable Watch start failure without leaking provider detail."""
    return WatchStartError(code, exit_class)


@dataclass(frozen=True, slots=True)
class WatchRequest:
    """Initial or resumed Watch controls under one caller deadline."""

    intent_id: str | None
    condition: WatchCondition | None
    resume: str | None
    authentication_key: SecretValue = field(repr=False)
    installation_id: str
    deadline: float
    cancellation: CancellationToken
    adc_quota_project: str | None = None

    def __post_init__(self) -> None:
        """Require exactly one subject selector and an explicit bounded deadline."""
        initial = self.intent_id is not None
        resumed = self.resume is not None
        if initial == resumed:
            msg = "Watch requires exactly one intent_id or resume token"
            raise ValueError(msg)
        if initial and (
            not isinstance(self.intent_id, str)
            or not self.intent_id
            or not isinstance(self.condition, WatchCondition)
        ):
            msg = "initial Watch requires intent_id and condition"
            raise ValueError(msg)
        if resumed and self.condition is not None:
            msg = "resumed Watch recovers its condition from the token"
            raise ValueError(msg)
        if not isinstance(self.installation_id, str) or not self.installation_id:
            msg = "Watch installation_id must be non-empty"
            raise ValueError(msg)
        if (
            isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not math.isfinite(self.deadline)
        ):
            msg = "Watch deadline must be finite monotonic seconds"
            raise ValueError(msg)
        if self.adc_quota_project is not None and (
            not isinstance(self.adc_quota_project, str) or not self.adc_quota_project
        ):
            msg = "ADC quota project must be None or non-empty"
            raise ValueError(msg)


@dataclass(slots=True)
class _Run:
    """Mutable state owned by one Watch invocation only."""

    subject: WatchSubject
    aggregate: WatchAggregate
    lineages: dict[str, WatchChildLineage]
    next_due: dict[str, float]
    started_at: datetime
    started_monotonic: float
    stream_id: str
    replayed_resolutions: tuple[tuple[str, bool], ...] = ()
    pending_subject: WatchSubject | None = None
    resume: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    unchanged_attempts: dict[str, int] = field(default_factory=dict)
    last_material_observed_at: datetime | None = None
    terminated: bool = False
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class _ObservationAttempt:
    """One successful observation or retryable per-child miss."""

    child: WatchChildIdentity
    observation: WatchObservation | None = None
    retry_after_seconds: float | None = None


class WatchOperations:
    """Observe one durable Apply subject without exposing provider mutation."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        apply_records: ApplyRecordRepository,
        checkpoints: WatchCheckpointRepository,
        resume_codec: WatchResumeCodec,
        reader: WatchObservationReader,
        budgets: BudgetCoordinator,
        clock: WatchClock,
        stream_ids: WatchStreamIdSource,
        jitter: JitterSource,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        """Bind authenticated local state and narrow read-only runtime seams."""
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            msg = "Watch poll interval must be positive finite seconds"
            raise ValueError(msg)
        self.apply_records = apply_records
        self.checkpoints = checkpoints
        self.resume_codec = resume_codec
        self.reader = reader
        self.budgets = budgets
        self.clock = clock
        self.stream_ids = stream_ids
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.jitter = jitter

    async def watch(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        request: WatchRequest,
    ) -> AsyncIterator[WatchStreamEvent]:
        """Emit one initial observation, material changes, and one terminal result."""
        if request.deadline <= self.clock.monotonic():
            raise _error("watch-deadline-expired", ExitClass.TIMEOUT)
        run = await self._start(request)
        replayed_accepted = {
            child_id for child_id, accepted in run.replayed_resolutions if accepted
        }
        initial_children = tuple(
            child
            for child in run.subject.accepted_children
            if child.child_id not in replayed_accepted
        )
        try:
            observations = await self._observe(
                request,
                initial_children,
            )
        except (asyncio.CancelledError, CoordinationCancelledError) as error:
            raise _error("watch-interrupted", ExitClass.INTERRUPTED) from error
        self._require_not_superseded(request, run)
        self._integrate_observations(run, observations)
        initial = self._event(request, run, WatchEventKind.INITIAL)
        yield initial
        if initial.event is WatchEventKind.TERMINAL:
            return
        if run.pending_subject is not None:
            try:
                advancements = _advance_run_subject(
                    run,
                    run.pending_subject,
                    self.clock.monotonic(),
                )
                if advancements != run.replayed_resolutions:
                    raise _error(
                        "watch-resolution-replay-changed",
                        ExitClass.STALE_OR_CONFLICTING,
                    )
                run.pending_subject = None
                self._require_not_superseded(request, run)
            except WatchStartError as error:
                yield self._terminal_from_error(request, run, error)
                return
        for child_id, accepted in run.replayed_resolutions:
            event = self._event(
                request,
                run,
                (
                    WatchEventKind.ACCEPTED_WATCH_SET_CHANGED
                    if accepted
                    else WatchEventKind.UNKNOWN_RESOLUTION_RECORDED
                ),
                child_id=child_id,
            )
            yield event
            if event.event is WatchEventKind.TERMINAL:
                return
        if replayed_accepted:
            try:
                previous = {
                    child_id: _summary(run.aggregate, child_id).status
                    for child_id in replayed_accepted
                }
                observations = await self._observe(
                    request,
                    tuple(
                        child
                        for child in run.subject.accepted_children
                        if child.child_id in replayed_accepted
                    ),
                )
            except (asyncio.CancelledError, CoordinationCancelledError):
                yield self._terminal(
                    request,
                    run,
                    code="watch-interrupted",
                    exit_class=ExitClass.INTERRUPTED,
                )
                return
            except WatchStartError as error:
                yield self._terminal_from_error(request, run, error)
                return
            try:
                self._require_not_superseded(request, run)
                self._integrate_observations(run, observations)
            except WatchStartError as error:
                yield self._terminal_from_error(request, run, error)
                return
            for child_id in replayed_accepted:
                if _material_status(previous[child_id]) == _material_status(
                    _summary(run.aggregate, child_id).status
                ):
                    continue
                event = self._event(
                    request,
                    run,
                    WatchEventKind.CHILD_STATUS_CHANGED,
                    child_id=child_id,
                )
                yield event
                if event.event is WatchEventKind.TERMINAL:
                    return
        if run.aggregate.disposition is not WatchDisposition.PENDING:
            yield self._terminal_for_disposition(request, run)
            return

        while True:
            try:
                advancements = self._refresh_subject(request, run)
            except WatchStartError as error:
                yield self._terminal_from_error(request, run, error)
                return
            for child_id, accepted in advancements:
                event = self._event(
                    request,
                    run,
                    (
                        WatchEventKind.ACCEPTED_WATCH_SET_CHANGED
                        if accepted
                        else WatchEventKind.UNKNOWN_RESOLUTION_RECORDED
                    ),
                    child_id=child_id,
                )
                yield event
                if event.event is WatchEventKind.TERMINAL:
                    return
            now = self.clock.monotonic()
            if request.cancellation.cancelled:
                yield self._terminal(
                    request,
                    run,
                    code="watch-interrupted",
                    exit_class=ExitClass.INTERRUPTED,
                )
                return
            if now >= request.deadline:
                yield self._terminal(
                    request,
                    run,
                    code="watch-timeout",
                    exit_class=ExitClass.TIMEOUT,
                )
                return
            due_at = min(run.next_due.values())
            try:
                await self.clock.sleep(min(due_at, request.deadline) - now)
            except asyncio.CancelledError:
                yield self._terminal(
                    request,
                    run,
                    code="watch-interrupted",
                    exit_class=ExitClass.INTERRUPTED,
                )
                return
            now = self.clock.monotonic()
            if request.cancellation.cancelled:
                continue
            if now >= request.deadline:
                continue
            try:
                advancements = self._refresh_subject(request, run)
            except WatchStartError as error:
                yield self._terminal_from_error(request, run, error)
                return
            for child_id, accepted in advancements:
                event = self._event(
                    request,
                    run,
                    (
                        WatchEventKind.ACCEPTED_WATCH_SET_CHANGED
                        if accepted
                        else WatchEventKind.UNKNOWN_RESOLUTION_RECORDED
                    ),
                    child_id=child_id,
                )
                yield event
                if event.event is WatchEventKind.TERMINAL:
                    return
            now = self.clock.monotonic()
            due = tuple(
                child
                for child in run.subject.accepted_children
                if run.next_due[child.child_id] <= now
            )
            try:
                observations = await self._observe(request, due)
            except (asyncio.CancelledError, CoordinationCancelledError):
                yield self._terminal(
                    request,
                    run,
                    code="watch-interrupted",
                    exit_class=ExitClass.INTERRUPTED,
                )
                return
            except WatchStartError as error:
                yield self._terminal_from_error(request, run, error)
                return
            try:
                self._require_not_superseded(request, run)
            except WatchStartError as error:
                yield self._terminal_from_error(request, run, error)
                return
            old_by_id = {
                summary.child.child_id: summary.status
                for summary in run.aggregate.children
            }
            try:
                self._integrate_observations(run, observations)
            except WatchStartError as error:
                yield self._terminal(
                    request,
                    run,
                    code=error.code.value,
                    exit_class=error.exit_class,
                )
                return
            changed = tuple(
                child.child_id
                for child in due
                if _material_status(old_by_id[child.child_id])
                != _material_status(_summary(run.aggregate, child.child_id).status)
            )
            for child_id in changed:
                event = self._event(
                    request,
                    run,
                    WatchEventKind.CHILD_STATUS_CHANGED,
                    child_id=child_id,
                )
                yield event
                if event.event is WatchEventKind.TERMINAL:
                    return
            if run.aggregate.disposition is not WatchDisposition.PENDING:
                yield self._terminal_for_disposition(request, run)
                return

    async def _start(self, request: WatchRequest) -> _Run:
        started_at = self.clock.now()
        started_monotonic = self.clock.monotonic()
        stream_id = self.stream_ids()
        if not isinstance(stream_id, str) or not stream_id:
            raise _error("watch-stream-identity-failed", ExitClass.OPERATIONAL_FAILURE)
        if request.resume is None:
            subject = self._load_subject(
                request.intent_id or "",
                request.condition,
                request.authentication_key,
            )
            return _Run(
                subject=subject,
                aggregate=_unobserved_aggregate(subject),
                lineages=_durable_lineages(subject),
                next_due={
                    child.child_id: started_monotonic
                    for child in subject.accepted_children
                },
                started_at=started_at,
                started_monotonic=started_monotonic,
                stream_id=stream_id,
            )

        try:
            claims = self.resume_codec.decode(
                request.resume, request.authentication_key
            )
        except (TypeError, ValueError) as error:
            raise _error(
                "watch-resume-unauthenticated", ExitClass.AUTHORIZATION
            ) from error
        if claims.installation_id != request.installation_id:
            raise _error(
                "watch-resume-foreign-installation",
                ExitClass.STALE_OR_CONFLICTING,
            )
        loaded = self.checkpoints.load(claims.checkpoint_id, request.authentication_key)
        if (
            loaded.status is not WatchCheckpointRepositoryStatus.AVAILABLE
            or loaded.checkpoint is None
        ):
            raise _error("watch-checkpoint-unavailable", ExitClass.STALE_OR_CONFLICTING)
        checkpoint = loaded.checkpoint
        if (
            checkpoint.installation_id != request.installation_id
            or checkpoint.subject.intent_id != claims.intent_id
            or checkpoint.subject.condition is not claims.condition
            or checkpoint.subject.resolution_checkpoint != claims.resolution_checkpoint
            or checkpoint.sequence != claims.sequence
            or _subject_digest(checkpoint.subject) != claims.subject_digest
        ):
            raise _error(
                "watch-resume-checkpoint-mismatch",
                ExitClass.STALE_OR_CONFLICTING,
            )
        current = self._load_subject(
            claims.intent_id,
            claims.condition,
            request.authentication_key,
        )
        run = _Run(
            subject=checkpoint.subject,
            aggregate=checkpoint.aggregate,
            lineages={lineage.child_id: lineage for lineage in checkpoint.lineages},
            next_due={
                child.child_id: started_monotonic
                for child in checkpoint.subject.accepted_children
            },
            started_at=started_at,
            started_monotonic=started_monotonic,
            stream_id=stream_id,
            resume=request.resume,
        )
        run.replayed_resolutions = _resolution_advancements(
            checkpoint.subject,
            current,
        )
        run.pending_subject = current
        return run

    def _refresh_subject(
        self,
        request: WatchRequest,
        run: _Run,
    ) -> tuple[tuple[str, bool], ...]:
        """Advance through authenticated append-only resolution evidence."""
        current = self._load_subject(
            run.subject.intent_id,
            run.subject.condition,
            request.authentication_key,
        )
        return _advance_run_subject(run, current, self.clock.monotonic())

    def _require_not_superseded(
        self,
        request: WatchRequest,
        run: _Run,
    ) -> None:
        outcome = self.apply_records.find_superseding_record(
            run.subject.intent_id,
            frozenset(
                child.preference_identity for child in run.subject.accepted_children
            ),
            request.authentication_key,
        )
        if outcome.status is ApplyRecordRepositoryStatus.AVAILABLE:
            raise _error(
                "watch-locally-superseded",
                ExitClass.REJECTED_PRECONDITION,
            )
        if outcome.status is not ApplyRecordRepositoryStatus.MISSING:
            raise _error(
                "watch-supersession-state-unavailable",
                ExitClass.STALE_OR_CONFLICTING,
            )

    def _load_subject(
        self,
        intent_id: str,
        condition: WatchCondition | None,
        key: SecretValue,
    ) -> WatchSubject:
        if condition is None:
            raise _error("watch-condition-missing", ExitClass.USAGE)
        loaded = self.apply_records.load(intent_id, key)
        resolutions = self.apply_records.load_unknown_resolutions(intent_id, key)
        if (
            loaded.status is not ApplyRecordRepositoryStatus.AVAILABLE
            or loaded.record is None
            or resolutions.status is not ApplyRecordRepositoryStatus.AVAILABLE
        ):
            raise _error(
                "watch-apply-record-unavailable",
                ExitClass.STALE_OR_CONFLICTING,
            )
        record = loaded.record
        if record.state is ApplyRecordState.IN_PROGRESS:
            raise _error(
                "watch-apply-record-in-progress",
                ExitClass.REJECTED_PRECONDITION,
            )
        try:
            subject = _subject(record, resolutions.resolutions, condition)
        except WatchStartError:
            raise
        except (TypeError, ValueError) as error:
            raise _error(
                "watch-subject-invalid",
                ExitClass.STALE_OR_CONFLICTING,
            ) from error
        superseding = self.apply_records.find_superseding_record(
            intent_id,
            frozenset(child.preference_identity for child in subject.accepted_children),
            key,
        )
        if superseding.status is ApplyRecordRepositoryStatus.AVAILABLE:
            raise _error(
                "watch-locally-superseded",
                ExitClass.REJECTED_PRECONDITION,
            )
        if superseding.status is not ApplyRecordRepositoryStatus.MISSING:
            raise _error(
                "watch-supersession-state-unavailable",
                ExitClass.STALE_OR_CONFLICTING,
            )
        return subject

    async def _observe(
        self,
        request: WatchRequest,
        children: tuple[WatchChildIdentity, ...],
    ) -> tuple[_ObservationAttempt, ...]:
        async def one(
            child: WatchChildIdentity,
        ) -> _ObservationAttempt:
            request.cancellation.raise_if_cancelled()
            await self.budgets.acquire(
                BudgetRequest(
                    provider=child.slice_identity.service,
                    project=child.slice_identity.resource_scope.canonical_name,
                    adc_quota_project=request.adc_quota_project,
                    units=2,
                ),
                deadline=request.deadline,
                cancellation=request.cancellation,
            )
            try:
                observation = await self.reader.observe(
                    WatchObservationRequest(
                        child=child,
                        deadline=request.deadline,
                        cancellation=request.cancellation,
                    )
                )
            except WatchObservationTransientError as error:
                return _ObservationAttempt(
                    child,
                    retry_after_seconds=error.retry_after_seconds,
                )
            return _ObservationAttempt(child, observation)

        tasks = tuple(asyncio.create_task(one(child)) for child in children)
        try:
            return tuple(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            raise
        except CoordinationCancelledError:
            raise
        except CoordinationDeadlineExceededError as error:
            raise _error("watch-timeout", ExitClass.TIMEOUT) from error
        except BaseException as error:
            raise _error(
                "watch-observation-failed", ExitClass.OPERATIONAL_FAILURE
            ) from error
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _integrate_observations(
        self,
        run: _Run,
        observations: tuple[_ObservationAttempt, ...],
    ) -> None:
        summaries = {
            summary.child.child_id: summary for summary in run.aggregate.children
        }
        now = self.clock.monotonic()
        diagnostics: list[Diagnostic] = []
        for attempt_result in observations:
            child = attempt_result.child
            observation = attempt_result.observation
            if observation is None:
                attempt = run.unchanged_attempts.get(child.child_id, 0) + 1
                run.unchanged_attempts[child.child_id] = attempt
                backoff = _bounded_backoff(self.poll_interval_seconds, attempt)
                jittered = self.jitter.apply(
                    backoff,
                    attempt=attempt,
                    identity=child.child_id,
                )
                delay = max(
                    jittered,
                    attempt_result.retry_after_seconds or 0,
                )
                run.next_due[child.child_id] = now + delay
                diagnostics.append(_transient_observation_diagnostic())
                continue
            previous = summaries.get(child.child_id)
            if (
                observation.preference_target != child.target
                or observation.status.desired != child.target
            ):
                raise _error(
                    "watch-preference-target-mismatch",
                    ExitClass.REJECTED_PRECONDITION,
                )
            expected = run.lineages.get(child.child_id)
            if expected is None:
                expected = WatchChildLineage(
                    child.child_id,
                    child.lineage_etag,
                    child.lineage_trace_id,
                )
            _require_same_lineage(expected, observation)
            run.lineages[child.child_id] = WatchChildLineage(
                child.child_id,
                observation.etag,
                observation.trace_id,
            )
            summaries[child.child_id] = WatchChildSummary(child, observation.status)
            unchanged = previous is not None and (
                _material_status(previous.status)
                == _material_status(observation.status)
            )
            attempt = (
                run.unchanged_attempts.get(child.child_id, 0) + 1 if unchanged else 0
            )
            run.unchanged_attempts[child.child_id] = attempt
            backoff = _bounded_backoff(self.poll_interval_seconds, attempt)
            jittered = self.jitter.apply(
                backoff,
                attempt=attempt,
                identity=child.child_id,
            )
            delay = max(jittered, observation.retry_after_seconds or 0)
            run.next_due[child.child_id] = now + delay
            diagnostics.extend(observation.diagnostics)
        run.diagnostics = tuple(diagnostics)
        ordered = tuple(
            summaries[child.child_id]
            if child.child_id in summaries
            else WatchChildSummary(child, None)
            for child in run.subject.children
        )
        run.aggregate = WatchAggregate.derive(run.subject, ordered)

    def _event(
        self,
        request: WatchRequest,
        run: _Run,
        kind: WatchEventKind,
        *,
        child_id: str | None = None,
    ) -> WatchStreamEvent:
        try:
            token = self._checkpoint(request, run)
        except WatchStartError:
            if run.resume is None:
                raise
            return self._terminal(
                request,
                run,
                code="watch-checkpoint-persistence-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
            )
        run.resume = token
        observed_at = self.clock.now()
        run.last_material_observed_at = observed_at
        event = WatchStreamEvent(
            stream_id=run.stream_id,
            sequence=run.sequence,
            event=kind,
            resume=token,
            observed_at=observed_at,
            subject=run.subject,
            aggregate=run.aggregate,
            child_id=child_id,
            diagnostics=run.diagnostics,
        )
        run.sequence += 1
        return event

    def _terminal_for_disposition(
        self, request: WatchRequest, run: _Run
    ) -> WatchStreamEvent:
        if run.aggregate.disposition is WatchDisposition.REACHED:
            return self._terminal(
                request,
                run,
                code=run.subject.condition.value,
                exit_class=ExitClass.SUCCESS,
            )
        return self._terminal(
            request,
            run,
            code="requested-outcome-unmet",
            exit_class=ExitClass.REQUESTED_OUTCOME_UNMET,
        )

    def _terminal_from_error(
        self,
        request: WatchRequest,
        run: _Run,
        error: WatchStartError,
    ) -> WatchStreamEvent:
        diagnostics: tuple[Diagnostic, ...] = ()
        completeness = Completeness.complete()
        if error.code.value == "watch-observation-failed":
            diagnostics = (_observation_failure_diagnostic(),)
            completeness = Completeness.incomplete(
                EvidenceGap(
                    StableSymbol("provider-watch-observation"),
                    StableSymbol("required-refresh-failed"),
                )
            )
            error = _error(
                "watch-observation-incomplete",
                ExitClass.INCOMPLETE_EVIDENCE,
            )
        return self._terminal(
            request,
            run,
            code=error.code.value,
            exit_class=error.exit_class,
            completeness=completeness,
            diagnostics=diagnostics,
        )

    def _terminal(  # noqa: PLR0913
        self,
        request: WatchRequest,
        run: _Run,
        *,
        code: str,
        exit_class: ExitClass,
        completeness: Completeness | None = None,
        diagnostics: tuple[Diagnostic, ...] = (),
    ) -> WatchStreamEvent:
        if not diagnostics:
            diagnostics = run.diagnostics
        try:
            token = self._checkpoint(request, run)
            run.resume = token
        except WatchStartError:
            if run.resume is None:
                raise
            token = run.resume
            code = "watch-checkpoint-persistence-failed"
            if completeness is not None and not completeness.is_complete:
                exit_class = ExitClass.INCOMPLETE_EVIDENCE
            else:
                exit_class = ExitClass.OPERATIONAL_FAILURE
                completeness = Completeness.complete()
            diagnostics = (*diagnostics, _checkpoint_failure_diagnostic())
        finished_at = self.clock.now()
        data = WatchResultData(
            subject=run.subject,
            aggregate=run.aggregate,
            resume=token,
            deadline=run.started_at
            + timedelta(seconds=max(0.0, request.deadline - run.started_monotonic)),
            elapsed_seconds=max(0.0, self.clock.monotonic() - run.started_monotonic),
            last_material_observed_at=(run.last_material_observed_at or run.started_at),
        )
        result = OperationResult(
            operation=OperationName("request.watch"),
            resource_scope=run.subject.resource_scope,
            boundary=OperationBoundary(
                StableSymbol(run.subject.condition.value),
                reached=exit_class is ExitClass.SUCCESS,
            ),
            outcome=Outcome(StableSymbol(code), exit_class),
            completeness=completeness or Completeness.complete(),
            started_at=run.started_at,
            finished_at=finished_at,
            data=data,
            diagnostics=diagnostics,
        )
        event = WatchStreamEvent(
            stream_id=run.stream_id,
            sequence=run.sequence,
            event=WatchEventKind.TERMINAL,
            resume=token,
            observed_at=finished_at,
            subject=run.subject,
            aggregate=run.aggregate,
            result=result,
            diagnostics=diagnostics,
        )
        run.sequence += 1
        return event

    def _checkpoint(self, request: WatchRequest, run: _Run) -> str:
        checkpoint_id = _checkpoint_id(run, self.clock.now())
        checkpoint = WatchCheckpoint(
            checkpoint_id=checkpoint_id,
            installation_id=request.installation_id,
            subject=run.subject,
            aggregate=run.aggregate,
            lineages=tuple(
                run.lineages[child.child_id] for child in run.subject.accepted_children
            ),
            sequence=run.sequence,
            saved_at=self.clock.now(),
        )
        stored = self.checkpoints.save(checkpoint, request.authentication_key)
        if stored.status is not WatchCheckpointRepositoryStatus.STORED:
            raise _error(
                "watch-checkpoint-persistence-failed",
                ExitClass.OPERATIONAL_FAILURE,
            )
        return self.resume_codec.encode(
            WatchResumeClaims(
                installation_id=request.installation_id,
                checkpoint_id=checkpoint_id,
                intent_id=run.subject.intent_id,
                subject_digest=_subject_digest(run.subject),
                condition=run.subject.condition,
                resolution_checkpoint=run.subject.resolution_checkpoint,
                sequence=run.sequence,
            ),
            request.authentication_key,
        )


def _subject(
    record: ApplyRecord,
    resolutions: tuple[UnknownResolutionEvidence, ...],
    condition: WatchCondition,
) -> WatchSubject:
    resolution_by_child: dict[str, UnknownResolutionEvidence] = {}
    for resolution in resolutions:
        if resolution.intent_id != record.intent_id:
            raise _error(
                "watch-resolution-intent-mismatch",
                ExitClass.STALE_OR_CONFLICTING,
            )
        existing = resolution_by_child.get(resolution.child_id)
        if existing is not None and existing != resolution:
            raise _error("watch-resolution-conflict", ExitClass.STALE_OR_CONFLICTING)
        resolution_by_child[resolution.child_id] = resolution
    child_ids = {child.child_id for child in record.children}
    if not set(resolution_by_child).issubset(child_ids):
        raise _error("watch-resolution-child-mismatch", ExitClass.STALE_OR_CONFLICTING)
    for child in record.children:
        retained = resolution_by_child.get(child.child_id)
        if child.unknown_resolution is not None and (
            retained is None
            or retained.resolution is not child.unknown_resolution
            or retained.recorded_at != child.resolution_recorded_at
            or retained.lineage_etag != child.accepted_etag
            or retained.lineage_trace_id != child.accepted_trace_id
        ):
            raise _error(
                "watch-resolution-history-mismatch",
                ExitClass.STALE_OR_CONFLICTING,
            )
    children = tuple(
        WatchChildIdentity(
            child_id=child.child_id,
            order=order,
            slice_identity=child.slice_identity,
            target=child.target,
            disposition=child.disposition or ApplyChildDisposition.UNATTEMPTED,
            preference_identity=child.preference_identity,
            lineage_etag=(
                resolution_by_child[child.child_id].lineage_etag
                if child.child_id in resolution_by_child
                else child.accepted_etag
            ),
            lineage_trace_id=(
                resolution_by_child[child.child_id].lineage_trace_id
                if child.child_id in resolution_by_child
                else child.accepted_trace_id
            ),
            unknown_resolution=(
                resolution_by_child[child.child_id].resolution
                if child.child_id in resolution_by_child
                else None
            ),
            resolution_checkpoint=(
                resolution_by_child[child.child_id].checkpoint
                if child.child_id in resolution_by_child
                else 0
            ),
            baseline=child.baseline,
        )
        for order, child in enumerate(record.children)
    )
    return WatchSubject(
        kind=record.kind,
        resource_scope=record.resource_scope,
        condition=condition,
        intent_id=record.intent_id,
        plan_digest=record.plan_digest,
        children=children,
        resolution_checkpoint=len(resolutions),
    )


def _unobserved_aggregate(subject: WatchSubject) -> WatchAggregate:
    """Retain every child without inventing provider lifecycle evidence."""
    return WatchAggregate.derive(
        subject,
        tuple(WatchChildSummary(child, None) for child in subject.children),
    )


def _durable_lineages(
    subject: WatchSubject,
) -> dict[str, WatchChildLineage]:
    """Seed verification from authenticated Apply or resolution evidence."""
    return {
        child.child_id: WatchChildLineage(
            child.child_id,
            child.lineage_etag,
            child.lineage_trace_id,
        )
        for child in subject.accepted_children
    }


def _advance_run_subject(
    run: _Run,
    current: WatchSubject,
    now: float,
) -> tuple[tuple[str, bool], ...]:
    """Apply only valid monotonic resolution-journal extensions."""
    advancements = _resolution_advancements(run.subject, current)
    if not advancements:
        return ()
    previous_summaries = {
        item.child.child_id: item.status for item in run.aggregate.children
    }
    run.subject = current
    run.aggregate = WatchAggregate.derive(
        current,
        tuple(
            WatchChildSummary(child, previous_summaries.get(child.child_id))
            for child in current.children
        ),
    )
    durable = _durable_lineages(current)
    for child_id, accepted in advancements:
        if accepted:
            run.lineages[child_id] = durable[child_id]
            run.next_due[child_id] = now
    return advancements


def _resolution_advancements(
    previous: WatchSubject,
    current: WatchSubject,
) -> tuple[tuple[str, bool], ...]:
    """Validate a complete subject as the same subject plus journal appends."""
    if (
        previous.kind is not current.kind
        or previous.resource_scope != current.resource_scope
        or previous.condition is not current.condition
        or previous.intent_id != current.intent_id
        or previous.plan_digest != current.plan_digest
        or len(previous.children) != len(current.children)
        or current.resolution_checkpoint < previous.resolution_checkpoint
    ):
        raise _error("watch-subject-changed", ExitClass.STALE_OR_CONFLICTING)
    advancements: list[tuple[str, bool]] = []
    for old, new in zip(previous.children, current.children, strict=True):
        if (
            old.child_id != new.child_id
            or old.order != new.order
            or old.slice_identity != new.slice_identity
            or old.target != new.target
            or old.disposition is not new.disposition
            or old.preference_identity != new.preference_identity
            or old.baseline != new.baseline
        ):
            raise _error("watch-subject-changed", ExitClass.STALE_OR_CONFLICTING)
        if old.unknown_resolution is not None:
            if new != old:
                raise _error(
                    "watch-resolution-history-changed",
                    ExitClass.STALE_OR_CONFLICTING,
                )
            continue
        if new.unknown_resolution is None:
            if (
                old.lineage_etag != new.lineage_etag
                or old.lineage_trace_id != new.lineage_trace_id
                or old.resolution_checkpoint != new.resolution_checkpoint
            ):
                raise _error(
                    "watch-provider-lineage-changed",
                    ExitClass.STALE_OR_CONFLICTING,
                )
            continue
        if (
            old.disposition is not ApplyChildDisposition.UNKNOWN
            or old.resolution_checkpoint != 0
            or new.resolution_checkpoint <= 0
        ):
            raise _error(
                "watch-resolution-history-changed",
                ExitClass.STALE_OR_CONFLICTING,
            )
        advancements.append(
            (
                new.child_id,
                new.unknown_resolution is UnknownDispatchResolution.ACCEPTED,
            )
        )
    if current.resolution_checkpoint != (
        previous.resolution_checkpoint + len(advancements)
    ):
        raise _error(
            "watch-resolution-checkpoint-mismatch",
            ExitClass.STALE_OR_CONFLICTING,
        )
    return tuple(advancements)


def _require_same_lineage(
    expected: WatchChildLineage,
    observation: WatchObservation,
) -> None:
    if expected.trace_id is not None:
        matches = observation.trace_id == expected.trace_id
    else:
        matches = expected.etag is not None and observation.etag == expected.etag
    if not matches:
        raise _error(
            "watch-provider-lineage-mismatch",
            ExitClass.REJECTED_PRECONDITION,
        )


def _summary(aggregate: WatchAggregate, child_id: str) -> WatchChildSummary:
    return next(item for item in aggregate.children if item.child.child_id == child_id)


def _material_status(status: QuotaRequestStatus | None) -> tuple[object, ...] | None:
    """Exclude refresh timestamps while retaining every lifecycle/value fact."""
    if status is None:
        return None
    return (
        status.reconciliation,
        status.provider_reconciliation,
        status.grant_satisfaction,
        status.effective_confirmation,
        status.baseline,
        status.desired,
        status.granted,
        status.effective,
    )


def _bounded_backoff(interval: float, attempt: int) -> float:
    """Exponentially back off without unbounded integer exponentiation."""
    try:
        return min(60.0, math.ldexp(interval, attempt))
    except OverflowError:
        return 60.0


def _subject_digest(subject: WatchSubject) -> str:
    payload = {
        "kind": subject.kind.value,
        "resource_scope": subject.resource_scope.canonical_name,
        "condition": subject.condition.value,
        "intent_id": subject.intent_id,
        "plan_digest": subject.plan_digest,
        "resolution_checkpoint": subject.resolution_checkpoint,
        "children": [
            {
                "child_id": child.child_id,
                "order": child.order,
                "slice": {
                    "service": child.slice_identity.service,
                    "quota_id": child.slice_identity.quota_id,
                    "dimensions": child.slice_identity.dimensions.items,
                    "scope": child.slice_identity.quota_scope.value,
                },
                "target": child.target.value,
                "unit": child.target.unit.symbol,
                "baseline": (None if child.baseline is None else child.baseline.value),
                "disposition": child.disposition.value,
                "preference_identity": child.preference_identity,
                "lineage_etag": child.lineage_etag,
                "lineage_trace_id": child.lineage_trace_id,
                "unknown_resolution": (
                    None
                    if child.unknown_resolution is None
                    else child.unknown_resolution.value
                ),
                "resolution_checkpoint": child.resolution_checkpoint,
            }
            for child in subject.children
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _checkpoint_id(run: _Run, now: datetime) -> str:
    payload = (
        f"{run.stream_id}:{run.sequence}:{now.isoformat()}:"
        f"{_subject_digest(run.subject)}"
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _observation_failure_diagnostic() -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode("watch-observation-failed"),
        severity=Severity.ERROR,
        phase=DiagnosticPhase("watch-observation"),
        source=DiagnosticSource("provider"),
        retry=RetryDisposition.AFTER_BACKOFF,
        message=RedactedText("A required Watch provider refresh failed."),
    )


def _checkpoint_failure_diagnostic() -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode("watch-checkpoint-persistence-failed"),
        severity=Severity.ERROR,
        phase=DiagnosticPhase("watch-checkpoint"),
        source=DiagnosticSource("local-state"),
        retry=RetryDisposition.UNKNOWN,
        message=RedactedText("The latest Watch checkpoint could not be persisted."),
    )


def _transient_observation_diagnostic() -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode("watch-observation-transient"),
        severity=Severity.WARNING,
        phase=DiagnosticPhase("watch-observation"),
        source=DiagnosticSource("provider"),
        retry=RetryDisposition.AFTER_BACKOFF,
        message=RedactedText(
            "A transient Watch observation failed and will be retried."
        ),
    )
