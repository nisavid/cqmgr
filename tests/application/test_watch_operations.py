"""Watch lifecycle operation acceptance tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.adapters.serialization.watch import HmacWatchResumeCodec
from cqmgr.application.operations.watch import (
    WatchOperations,
    WatchRequest,
    WatchStartError,
)
from cqmgr.application.ports.apply_records import (
    ApplyRecordRepositoryOutcome,
    ApplyRecordRepositoryStatus,
)
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    CancellationToken,
    CoordinationDeadlineExceededError,
)
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.application.ports.watch import (
    WatchCheckpointRepositoryOutcome,
    WatchCheckpointRepositoryStatus,
    WatchObservation,
    WatchObservationTransientError,
)
from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    ApplyChildRecord,
    ApplyRecord,
    ApplyRecordState,
    UnknownDispatchResolution,
    UnknownResolutionEvidence,
)
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import ExitClass, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
)
from cqmgr.domain.watch import WatchCheckpoint, WatchEventKind, WatchStreamEvent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from cqmgr.application.ports.apply_records import ApplyRecordRepository
    from cqmgr.application.ports.coordination import BudgetRequest
    from cqmgr.application.ports.watch import (
        WatchCheckpointRepository,
        WatchObservationRequest,
    )
NOW = datetime(2026, 7, 24, 7, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
UNIT = QuotaUnit("1")
KEY = SecretValue(b"w" * 32)
LONG_WATCH_POLL_COUNT = 1024


def _child(
    child_id: str,
    disposition: ApplyChildDisposition,
) -> ApplyChildRecord:
    identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        f"quota-{child_id}",
        NormalizedDimensions((("region", "us-central1"),)),
        QuotaScope.REGIONAL,
    )
    dispatched = disposition is not ApplyChildDisposition.UNATTEMPTED
    return ApplyChildRecord(
        child_id=child_id,
        slice_identity=identity,
        target=QuotaQuantity(8, UNIT),
        preference_identity=(
            f"{SCOPE.canonical_name}/locations/global/quotaPreferences/{child_id}"
        ),
        etag=f"etag-{child_id}",
        preference_existed=True,
        dispatch_intent_at=NOW if dispatched else None,
        disposition=disposition,
        provider_outcome=StableSymbol(
            "submitted" if disposition is ApplyChildDisposition.ACCEPTED else "failed"
        )
        if dispatched
        else None,
        outcome_recorded_at=NOW if dispatched else None,
        accepted_etag=(
            f"etag-{child_id}"
            if disposition is ApplyChildDisposition.ACCEPTED
            else None
        ),
        baseline=QuotaQuantity(4, UNIT),
    )


def _record() -> ApplyRecord:
    return ApplyRecord(
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        kind=PlanKind.BUNDLE,
        resource_scope=SCOPE,
        created_at=NOW,
        children=(
            _child("direct", ApplyChildDisposition.ACCEPTED),
            _child("companion", ApplyChildDisposition.ACCEPTED),
            _child("failed", ApplyChildDisposition.FAILED),
            _child("later", ApplyChildDisposition.UNATTEMPTED),
        ),
        state=ApplyRecordState.FAILED,
        finished_at=NOW,
    )


def _status(
    reconciliation: Reconciliation,
    *,
    granted: int | None = None,
    effective: int | None = None,
    minute: int = 0,
) -> QuotaRequestStatus:
    observed = NOW + timedelta(minutes=minute)
    return QuotaRequestStatus.derive(
        reconciliation=reconciliation,
        baseline=QuotaQuantity(4, UNIT),
        desired=QuotaQuantity(8, UNIT),
        granted=None if granted is None else QuotaQuantity(granted, UNIT),
        effective=None if effective is None else QuotaQuantity(effective, UNIT),
        status_observed_at=observed,
        effective_observed_at=None if effective is None else observed,
    )


class _Records:
    def __init__(self, record: ApplyRecord) -> None:
        self.record = record
        self.resolutions: tuple[UnknownResolutionEvidence, ...] = ()
        self.superseding: ApplyRecord | None = None

    def load(self, intent_id: str, _key: SecretValue) -> ApplyRecordRepositoryOutcome:
        if intent_id != self.record.intent_id:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE, self.record
        )

    def load_unknown_resolutions(
        self, intent_id: str, _key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        if intent_id != self.record.intent_id:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE,
            self.record,
            self.resolutions,
        )

    def find_superseding_record(
        self,
        selected_intent_id: str,
        preference_identities: frozenset[str],
        _key: SecretValue,
    ) -> ApplyRecordRepositoryOutcome:
        assert selected_intent_id == self.record.intent_id
        assert preference_identities
        if self.superseding is None:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE,
            self.superseding,
        )


class _Checkpoints:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.items: dict[str, WatchCheckpoint] = {}
        self.saves = 0
        self.fail_after = fail_after

    def save(
        self, checkpoint: WatchCheckpoint, authentication_key: SecretValue
    ) -> WatchCheckpointRepositoryOutcome:
        del authentication_key
        self.saves += 1
        if self.fail_after is not None and self.saves > self.fail_after:
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.FAILED
            )
        self.items[checkpoint.checkpoint_id] = checkpoint
        return WatchCheckpointRepositoryOutcome(
            WatchCheckpointRepositoryStatus.STORED,
            checkpoint,
        )

    def load(
        self, checkpoint_id: str, authentication_key: SecretValue
    ) -> WatchCheckpointRepositoryOutcome:
        del authentication_key
        checkpoint = self.items.get(checkpoint_id)
        if checkpoint is None:
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.MISSING
            )
        return WatchCheckpointRepositoryOutcome(
            WatchCheckpointRepositoryStatus.AVAILABLE,
            checkpoint,
        )


class _Clock:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic_value = 100.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    async def sleep(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class _Jitter:
    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        assert attempt >= 0
        assert identity
        return delay * 0.75


class _Budgets:
    def __init__(self) -> None:
        self.requests: list[BudgetRequest] = []

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        assert deadline > 0
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return BudgetGrant(100.0, request)


class _Reader:
    def __init__(
        self,
        scripts: dict[str, list[QuotaRequestStatus]],
        *,
        retry_after: dict[str, float] | None = None,
    ) -> None:
        self.scripts = scripts
        self.retry_after = retry_after or {}
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def observe(self, request: WatchObservationRequest) -> WatchObservation:
        child_id = request.child.child_id
        self.calls.append(child_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        values = self.scripts[child_id]
        status = values.pop(0) if len(values) > 1 else values[0]
        return WatchObservation(
            status=status,
            preference_target=request.child.target,
            etag=request.child.lineage_etag,
            trace_id=request.child.lineage_trace_id,
            observed_at=status.status_observed_at,
            retry_after_seconds=self.retry_after.get(child_id),
        )


def _operations(
    reader: _Reader,
    *,
    record: ApplyRecord | None = None,
    checkpoints: _Checkpoints | None = None,
) -> tuple[WatchOperations, _Clock, _Budgets, _Checkpoints]:
    clock = _Clock()
    budgets = _Budgets()
    checkpoints = checkpoints or _Checkpoints()
    operations = WatchOperations(
        apply_records=cast(
            "ApplyRecordRepository",
            _Records(record or _record()),
        ),
        checkpoints=cast("WatchCheckpointRepository", checkpoints),
        resume_codec=HmacWatchResumeCodec(),
        reader=reader,
        budgets=budgets,
        clock=clock,
        stream_ids=lambda: "stream-1",
        jitter=_Jitter(),
        poll_interval_seconds=1.0,
    )
    return operations, clock, budgets, checkpoints


async def _collect(
    operations: WatchOperations,
    request: WatchRequest,
) -> list[WatchStreamEvent]:
    return [event async for event in operations.watch(request)]


def test_watch_polls_accepted_children_independently_and_emits_only_changes() -> None:
    """A partial Apply keeps every child visible and polls only accepted children."""
    asyncio.run(_watch_polls_accepted_children_independently())


async def _watch_polls_accepted_children_independently() -> None:
    submitted = _status(Reconciliation.RECONCILING)
    direct_granted = _status(Reconciliation.SETTLED, granted=8, minute=1)
    companion_granted = _status(Reconciliation.SETTLED, granted=8, minute=2)
    reader = _Reader(
        {
            "direct": [submitted, direct_granted, direct_granted],
            "companion": [submitted, submitted, companion_granted],
        }
    )
    operations, _clock, budgets, _checkpoints = _operations(reader)

    events = [
        event
        async for event in operations.watch(
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=110.0,
                cancellation=CancellationToken(),
            )
        )
    ]

    assert [event.event for event in events] == [
        WatchEventKind.INITIAL,
        WatchEventKind.CHILD_STATUS_CHANGED,
        WatchEventKind.CHILD_STATUS_CHANGED,
        WatchEventKind.TERMINAL,
    ]
    assert [event.child_id for event in events] == [
        None,
        "direct",
        "companion",
        None,
    ]
    assert events[-1].result is not None
    assert events[-1].result.outcome.exit_class is ExitClass.SUCCESS
    child_ids = tuple(
        summary.child.child_id for summary in events[-1].aggregate.children
    )
    assert child_ids == (
        "direct",
        "companion",
        "failed",
        "later",
    )
    assert reader.max_active == len(_record().children) - 2
    assert "failed" not in reader.calls
    assert "later" not in reader.calls
    assert len(budgets.requests) == len(reader.calls)
    assert len({event.resume for event in events}) == len(events)


def test_watch_timeout_and_interruption_preserve_provider_state() -> None:
    """Observation boundaries do not relabel a reconciling child as failed."""
    asyncio.run(_watch_timeout_and_interruption_preserve_provider_state())


async def _watch_timeout_and_interruption_preserve_provider_state() -> None:
    submitted = _status(Reconciliation.RECONCILING)
    for interrupted in (False, True):
        task: asyncio.Task[None] | None = None
        token = CancellationToken()
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        operations, clock, _budgets, _checkpoints = _operations(reader)
        if interrupted:

            async def cancel_after_first_tick(
                cancellation: CancellationToken = token,
            ) -> None:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                cancellation.cancel()

            task = asyncio.create_task(cancel_after_first_tick())
        events = [
            event
            async for event in operations.watch(
                WatchRequest(
                    intent_id=_record().intent_id,
                    condition=WatchCondition.FULFILLED,
                    resume=None,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 1.0,
                    cancellation=token,
                )
            )
        ]
        if task is not None:
            await task
        terminal = events[-1]
        assert terminal.event is WatchEventKind.TERMINAL
        assert terminal.result is not None
        assert terminal.result.outcome.exit_class is (
            ExitClass.INTERRUPTED if interrupted else ExitClass.TIMEOUT
        )
        assert all(
            summary.status is None
            or summary.status.reconciliation is Reconciliation.RECONCILING
            for summary in terminal.aggregate.children
        )


def test_resume_is_installation_subject_checkpoint_and_lineage_bound() -> None:
    """Tampering, a foreign installation, and provider lineage drift fail closed."""
    asyncio.run(_resume_is_installation_subject_checkpoint_and_lineage_bound())


def test_resume_rejects_a_later_local_apply_for_a_watched_preference() -> None:
    """Authenticated local supersession is rejected before provider polling."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        operations, clock, _budgets, _checkpoints = _operations(
            _Reader({"direct": [submitted], "companion": [submitted]})
        )
        first = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )
        records = cast("_Records", operations.apply_records)
        records.superseding = replace(
            records.record,
            intent_id="sha256:" + ("c" * 64),
            plan_digest="sha256:" + ("d" * 64),
            created_at=clock.now() + timedelta(seconds=1),
        )
        resumed_reader = _Reader({"direct": [submitted], "companion": [submitted]})
        resumed = WatchOperations(
            apply_records=operations.apply_records,
            checkpoints=operations.checkpoints,
            resume_codec=HmacWatchResumeCodec(),
            reader=resumed_reader,
            budgets=operations.budgets,
            clock=clock,
            stream_ids=lambda: "stream-2",
            jitter=_Jitter(),
            poll_interval_seconds=1,
        )

        with pytest.raises(WatchStartError) as raised:
            await _collect(
                resumed,
                WatchRequest(
                    intent_id=None,
                    condition=None,
                    resume=first[-1].resume,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 1,
                    cancellation=CancellationToken(),
                ),
            )
        assert raised.value.exit_class is ExitClass.REJECTED_PRECONDITION
        assert resumed_reader.calls == []

    asyncio.run(run())


async def _resume_is_installation_subject_checkpoint_and_lineage_bound() -> None:
    submitted = _status(Reconciliation.RECONCILING)
    reader = _Reader({"direct": [submitted], "companion": [submitted]})
    operations, clock, _budgets, _checkpoints = _operations(reader)
    first = [
        event
        async for event in operations.watch(
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.FULFILLED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
    ]
    token = first[-1].resume
    tamper_index = len(token) // 2
    tampered = (
        token[:tamper_index]
        + ("A" if token[tamper_index] != "A" else "B")
        + token[tamper_index + 1 :]
    )

    for resume, installation_id in (
        (tampered, "installation-123"),
        (token, "foreign-installation"),
    ):
        with pytest.raises(WatchStartError) as raised:
            async for _event in operations.watch(
                WatchRequest(
                    intent_id=None,
                    condition=None,
                    resume=resume,
                    authentication_key=KEY,
                    installation_id=installation_id,
                    deadline=clock.monotonic() + 1,
                    cancellation=CancellationToken(),
                )
            ):
                pass
        assert raised.value.exit_class in {
            ExitClass.AUTHORIZATION,
            ExitClass.STALE_OR_CONFLICTING,
        }

    drifted_reader = _Reader({"direct": [submitted], "companion": [submitted]})
    original_observe = drifted_reader.observe

    async def observe_with_drift(
        request: WatchObservationRequest,
    ) -> WatchObservation:
        result = await original_observe(request)
        if request.child.child_id == "direct":
            return replace(result, etag="foreign-etag")
        return result

    drifted_reader.observe = observe_with_drift  # type: ignore[method-assign]
    drifted = WatchOperations(
        apply_records=operations.apply_records,
        checkpoints=operations.checkpoints,
        resume_codec=HmacWatchResumeCodec(),
        reader=drifted_reader,
        budgets=operations.budgets,
        clock=clock,
        stream_ids=lambda: "stream-2",
        jitter=_Jitter(),
        poll_interval_seconds=1,
    )
    with pytest.raises(WatchStartError) as raised:
        async for _event in drifted.watch(
            WatchRequest(
                intent_id=None,
                condition=None,
                resume=token,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        ):
            pass
    assert raised.value.exit_class is ExitClass.REJECTED_PRECONDITION


def test_resume_replays_monotonic_accepted_resolution_before_first_poll() -> None:
    """A later accepted proof expands and checkpoints the Watch set before I/O."""
    asyncio.run(_resume_replays_monotonic_accepted_resolution_before_first_poll())


async def _resume_replays_monotonic_accepted_resolution_before_first_poll() -> None:
    record = replace(
        _record(),
        children=(
            _child("direct", ApplyChildDisposition.ACCEPTED),
            _child("later", ApplyChildDisposition.UNKNOWN),
        ),
        state=ApplyRecordState.UNKNOWN,
    )
    submitted = _status(Reconciliation.RECONCILING)
    first_reader = _Reader({"direct": [submitted]})
    operations, clock, _budgets, _checkpoints = _operations(
        first_reader,
        record=record,
    )
    first = [
        event
        async for event in operations.watch(
            WatchRequest(
                intent_id=record.intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
    ]
    records = cast("_Records", operations.apply_records)
    records.resolutions = (
        UnknownResolutionEvidence(
            record.intent_id,
            "later",
            UnknownDispatchResolution.ACCEPTED,
            clock.now(),
            lineage_etag="resolved-etag",
        ),
    )
    resumed_reader = _Reader(
        {
            "direct": [submitted],
            "later": [submitted],
        }
    )
    resumed_original = resumed_reader.observe
    later_transient = False

    async def transient_later_once(
        request: WatchObservationRequest,
    ) -> WatchObservation:
        nonlocal later_transient
        if request.child.child_id == "later" and not later_transient:
            later_transient = True
            raise WatchObservationTransientError(0)
        return await resumed_original(request)

    resumed_reader.observe = transient_later_once  # type: ignore[method-assign]
    resumed = WatchOperations(
        apply_records=operations.apply_records,
        checkpoints=operations.checkpoints,
        resume_codec=HmacWatchResumeCodec(),
        reader=resumed_reader,
        budgets=operations.budgets,
        clock=clock,
        stream_ids=lambda: "stream-2",
        jitter=_Jitter(),
        poll_interval_seconds=1,
    )
    stream = resumed.watch(
        WatchRequest(
            intent_id=None,
            condition=None,
            resume=first[-1].resume,
            authentication_key=KEY,
            installation_id="installation-123",
            deadline=clock.monotonic() + 1,
            cancellation=CancellationToken(),
        )
    )

    initial = await anext(stream)
    replay = await anext(stream)

    assert initial.event is WatchEventKind.INITIAL
    assert tuple(child.child_id for child in initial.subject.accepted_children) == (
        "direct",
    )
    assert initial.aggregate.children[0].status == submitted
    assert replay.event is WatchEventKind.ACCEPTED_WATCH_SET_CHANGED
    assert replay.child_id == "later"
    assert resumed_reader.calls == ["direct"]
    assert tuple(child.child_id for child in replay.subject.accepted_children) == (
        "direct",
        "later",
    )
    assert replay.aggregate.children[1].status is None
    remaining = [event async for event in stream]
    assert all(
        event.event is not WatchEventKind.CHILD_STATUS_CHANGED for event in remaining
    )
    assert remaining[-1].event is WatchEventKind.TERMINAL
    assert resumed_reader.calls[0] == "direct"
    assert later_transient


@pytest.mark.parametrize(
    ("reconciliation", "granted", "expected"),
    [
        (Reconciliation.SETTLED, 6, ExitClass.REQUESTED_OUTCOME_UNMET),
        (Reconciliation.SETTLED, 4, ExitClass.REQUESTED_OUTCOME_UNMET),
        (Reconciliation.FAILED, None, ExitClass.REQUESTED_OUTCOME_UNMET),
        (Reconciliation.SUPERSEDED, None, ExitClass.REQUESTED_OUTCOME_UNMET),
    ],
)
def test_conclusive_adverse_lifecycle_is_requested_outcome_unmet(
    reconciliation: Reconciliation,
    granted: int | None,
    expected: ExitClass,
) -> None:
    """Partial, zero, failed, and superseded states terminate without flattening."""

    async def run() -> None:
        status = _status(reconciliation, granted=granted)
        reader = _Reader({"direct": [status], "companion": [status]})
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = [
            event
            async for event in operations.watch(
                WatchRequest(
                    intent_id=_record().intent_id,
                    condition=WatchCondition.GRANTED,
                    resume=None,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 10,
                    cancellation=CancellationToken(),
                )
            )
        ]

        assert [event.event for event in events] == [
            WatchEventKind.INITIAL,
            WatchEventKind.TERMINAL,
        ]
        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is expected
        assert all(
            summary.status is None or summary.status.reconciliation is reconciliation
            for summary in events[-1].aggregate.children
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("effective", "effective_minute"),
    [(8, -1), (6, 0)],
)
def test_fulfilled_stale_or_mismatched_effective_evidence_times_out(
    effective: int,
    effective_minute: int,
) -> None:
    """A full grant remains pending until effective evidence is fresh and equal."""

    async def run() -> None:
        status_observed = NOW
        effective_observed = NOW + timedelta(minutes=effective_minute)
        status = QuotaRequestStatus.derive(
            reconciliation=Reconciliation.SETTLED,
            baseline=QuotaQuantity(4, UNIT),
            desired=QuotaQuantity(8, UNIT),
            granted=QuotaQuantity(8, UNIT),
            effective=QuotaQuantity(effective, UNIT),
            status_observed_at=status_observed,
            effective_observed_at=effective_observed,
        )
        reader = _Reader({"direct": [status], "companion": [status]})
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = [
            event
            async for event in operations.watch(
                WatchRequest(
                    intent_id=_record().intent_id,
                    condition=WatchCondition.FULFILLED,
                    resume=None,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 1,
                    cancellation=CancellationToken(),
                )
            )
        ]

        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is ExitClass.TIMEOUT

    asyncio.run(run())


def test_running_watch_replays_later_resolution_before_polling_new_child() -> None:
    """A live stream advances through the same append-only resolution frontier."""
    asyncio.run(_running_watch_replays_later_resolution_before_polling_new_child())


async def _running_watch_replays_later_resolution_before_polling_new_child() -> None:
    record = replace(
        _record(),
        children=(
            _child("direct", ApplyChildDisposition.ACCEPTED),
            _child("later", ApplyChildDisposition.UNKNOWN),
        ),
        state=ApplyRecordState.UNKNOWN,
    )
    submitted = _status(Reconciliation.RECONCILING)
    reader = _Reader({"direct": [submitted], "later": [submitted]})
    operations, clock, _budgets, _checkpoints = _operations(reader, record=record)
    stream = operations.watch(
        WatchRequest(
            intent_id=record.intent_id,
            condition=WatchCondition.GRANTED,
            resume=None,
            authentication_key=KEY,
            installation_id="installation-123",
            deadline=clock.monotonic() + 2,
            cancellation=CancellationToken(),
        )
    )
    initial = await anext(stream)
    records = cast("_Records", operations.apply_records)
    records.resolutions = (
        UnknownResolutionEvidence(
            record.intent_id,
            "later",
            UnknownDispatchResolution.ACCEPTED,
            clock.now(),
            lineage_etag="resolved-etag",
        ),
    )

    replay = await anext(stream)

    assert initial.event is WatchEventKind.INITIAL
    assert replay.event is WatchEventKind.ACCEPTED_WATCH_SET_CHANGED
    assert replay.child_id == "later"
    assert reader.calls == ["direct"]
    assert replay.aggregate.children[1].status is None
    await cast("AsyncGenerator[WatchStreamEvent, None]", stream).aclose()


def test_failed_unknown_resolution_is_checkpointed_but_never_polled() -> None:
    """Rejected read-after-unknown evidence remains visible and non-watchable."""
    asyncio.run(_failed_unknown_resolution_is_checkpointed_but_never_polled())


async def _failed_unknown_resolution_is_checkpointed_but_never_polled() -> None:
    record = replace(
        _record(),
        children=(
            _child("direct", ApplyChildDisposition.ACCEPTED),
            _child("later", ApplyChildDisposition.UNKNOWN),
        ),
        state=ApplyRecordState.UNKNOWN,
    )
    submitted = _status(Reconciliation.RECONCILING)
    reader = _Reader({"direct": [submitted]})
    operations, clock, _budgets, _checkpoints = _operations(reader, record=record)
    stream = operations.watch(
        WatchRequest(
            intent_id=record.intent_id,
            condition=WatchCondition.GRANTED,
            resume=None,
            authentication_key=KEY,
            installation_id="installation-123",
            deadline=clock.monotonic() + 2,
            cancellation=CancellationToken(),
        )
    )
    await anext(stream)
    records = cast("_Records", operations.apply_records)
    records.resolutions = (
        UnknownResolutionEvidence(
            record.intent_id,
            "later",
            UnknownDispatchResolution.FAILED,
            clock.now(),
        ),
    )

    replay = await anext(stream)

    assert replay.event is WatchEventKind.UNKNOWN_RESOLUTION_RECORDED
    assert replay.child_id == "later"
    assert reader.calls == ["direct"]
    assert tuple(child.child_id for child in replay.subject.accepted_children) == (
        "direct",
    )
    await cast("AsyncGenerator[WatchStreamEvent, None]", stream).aclose()


def test_retry_after_throttles_each_child_independently() -> None:
    """One child's provider delay does not suppress another due observation."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        granted = _status(Reconciliation.SETTLED, granted=8, minute=1)
        reader = _Reader(
            {
                "direct": [submitted, granted],
                "companion": [submitted, submitted, granted],
            },
            retry_after={"direct": 5.0, "companion": 1.0},
        )
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = [
            event
            async for event in operations.watch(
                WatchRequest(
                    intent_id=_record().intent_id,
                    condition=WatchCondition.GRANTED,
                    resume=None,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 3,
                    cancellation=CancellationToken(),
                )
            )
        ]

        assert reader.calls.count("direct") == 1
        expected_companion_polls = 3
        assert reader.calls.count("companion") == expected_companion_polls
        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is ExitClass.TIMEOUT

    asyncio.run(run())


def test_midstream_observation_failure_emits_one_incomplete_terminal() -> None:
    """Once streaming begins, observation failure closes with an evidence gap."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        original = reader.observe

        async def fail_after_initial(
            request: WatchObservationRequest,
        ) -> WatchObservation:
            if reader.calls.count(request.child.child_id) >= 1:
                msg = "read failed"
                raise RuntimeError(msg)
            return await original(request)

        reader.observe = fail_after_initial  # type: ignore[method-assign]
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = [
            event
            async for event in operations.watch(
                WatchRequest(
                    intent_id=_record().intent_id,
                    condition=WatchCondition.GRANTED,
                    resume=None,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 3,
                    cancellation=CancellationToken(),
                )
            )
        ]

        assert [event.event for event in events] == [
            WatchEventKind.INITIAL,
            WatchEventKind.TERMINAL,
        ]
        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
        assert not events[-1].result.completeness.is_complete
        assert events[-1].diagnostics[0].code.value == "watch-observation-failed"

    asyncio.run(run())


def test_terminal_checkpoint_failure_reuses_last_durable_resume() -> None:
    """A material-event checkpoint failure emits one resumable terminal."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        granted = _status(Reconciliation.SETTLED, granted=8, minute=1)
        checkpoints = _Checkpoints(fail_after=1)
        operations, clock, _budgets, _checkpoints = _operations(
            _Reader(
                {
                    "direct": [submitted, granted],
                    "companion": [submitted],
                }
            ),
            checkpoints=checkpoints,
        )
        events = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 3,
                cancellation=CancellationToken(),
            ),
        )

        assert [event.event for event in events] == [
            WatchEventKind.INITIAL,
            WatchEventKind.TERMINAL,
        ]
        initial, terminal = events
        assert terminal.resume == initial.resume
        assert terminal.result is not None
        assert terminal.result.outcome.code.value == (
            "watch-checkpoint-persistence-failed"
        )
        assert terminal.result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
        assert terminal.result.completeness.is_complete
        assert terminal.diagnostics[-1].code.value == (
            "watch-checkpoint-persistence-failed"
        )

    asyncio.run(run())


def test_resumed_first_checkpoint_failure_uses_input_resume() -> None:
    """The authenticated input token remains a terminal fallback on resume."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        checkpoints = _Checkpoints()
        operations, clock, _budgets, _checkpoints = _operations(
            _Reader({"direct": [submitted], "companion": [submitted]}),
            checkpoints=checkpoints,
        )
        first = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )
        checkpoints.fail_after = checkpoints.saves
        resumed = WatchOperations(
            apply_records=operations.apply_records,
            checkpoints=operations.checkpoints,
            resume_codec=HmacWatchResumeCodec(),
            reader=_Reader({"direct": [submitted], "companion": [submitted]}),
            budgets=operations.budgets,
            clock=clock,
            stream_ids=lambda: "stream-2",
            jitter=_Jitter(),
            poll_interval_seconds=1,
        )
        events = await _collect(
            resumed,
            WatchRequest(
                intent_id=None,
                condition=None,
                resume=first[-1].resume,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )

        assert [event.event for event in events] == [WatchEventKind.TERMINAL]
        assert events[0].resume == first[-1].resume
        assert events[0].result is not None
        assert events[0].result.outcome.code.value == (
            "watch-checkpoint-persistence-failed"
        )

    asyncio.run(run())


def test_supersession_persisted_during_observation_blocks_initial_event() -> None:
    """A concurrent local Apply is rechecked after reads and before emission."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        operations, clock, _budgets, _checkpoints = _operations(reader)
        records = cast("_Records", operations.apply_records)
        original = reader.observe

        async def supersede_during_read(
            request: WatchObservationRequest,
        ) -> WatchObservation:
            observation = await original(request)
            if request.child.child_id == "direct":
                records.superseding = replace(
                    records.record,
                    intent_id="sha256:" + ("c" * 64),
                    plan_digest="sha256:" + ("d" * 64),
                    created_at=clock.now(),
                )
            return observation

        reader.observe = supersede_during_read  # type: ignore[method-assign]
        with pytest.raises(WatchStartError) as raised:
            await _collect(
                operations,
                WatchRequest(
                    intent_id=_record().intent_id,
                    condition=WatchCondition.GRANTED,
                    resume=None,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 2,
                    cancellation=CancellationToken(),
                ),
            )
        assert raised.value.code.value == "watch-locally-superseded"

    asyncio.run(run())


def test_material_comparison_ignores_refresh_timestamps() -> None:
    """Fresh timestamps alone neither emit changes nor reset lifecycle facts."""

    async def run() -> None:
        first = _status(Reconciliation.RECONCILING)
        refreshed = _status(Reconciliation.RECONCILING, minute=1)
        operations, clock, _budgets, _checkpoints = _operations(
            _Reader(
                {
                    "direct": [first, refreshed],
                    "companion": [first, refreshed],
                }
            )
        )
        events = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 3,
                cancellation=CancellationToken(),
            ),
        )

        assert [event.event for event in events] == [
            WatchEventKind.INITIAL,
            WatchEventKind.TERMINAL,
        ]
        assert events[-1].result is not None
        assert events[-1].result.data.last_material_observed_at == (
            events[0].observed_at
        )
        assert events[-1].result.data.deadline.tzinfo is UTC

    asyncio.run(run())


def test_transient_child_read_retries_without_stopping_siblings() -> None:
    """A documented transient miss is isolated and retried under the deadline."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        original = reader.observe
        failed_once = False

        async def transient_once(
            request: WatchObservationRequest,
        ) -> WatchObservation:
            nonlocal failed_once
            if request.child.child_id == "direct" and not failed_once:
                failed_once = True
                raise WatchObservationTransientError(1)
            return await original(request)

        reader.observe = transient_once  # type: ignore[method-assign]
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 4,
                cancellation=CancellationToken(),
            ),
        )

        assert events[0].aggregate.children[0].status is None
        assert any(
            event.event is WatchEventKind.CHILD_STATUS_CHANGED
            and event.child_id == "direct"
            for event in events
        )
        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is ExitClass.TIMEOUT

    asyncio.run(run())


def test_adaptive_backoff_remains_bounded_after_many_unchanged_polls() -> None:
    """A long-lived unchanged Watch cannot overflow exponential scheduling."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 61_500,
                cancellation=CancellationToken(),
            ),
        )

        assert reader.calls.count("direct") > LONG_WATCH_POLL_COUNT
        assert [event.event for event in events] == [
            WatchEventKind.INITIAL,
            WatchEventKind.TERMINAL,
        ]

    asyncio.run(run())


def test_midstream_provider_deadline_maps_to_timeout() -> None:
    """A shared-deadline exception retains the Watch timeout contract."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        original = reader.observe

        async def expire_after_initial(
            request: WatchObservationRequest,
        ) -> WatchObservation:
            if reader.calls.count(request.child.child_id) >= 1:
                raise CoordinationDeadlineExceededError
            return await original(request)

        reader.observe = expire_after_initial  # type: ignore[method-assign]
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 3,
                cancellation=CancellationToken(),
            ),
        )

        assert [event.event for event in events] == [
            WatchEventKind.INITIAL,
            WatchEventKind.TERMINAL,
        ]
        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is ExitClass.TIMEOUT

    asyncio.run(run())


def test_fatal_observation_cancels_and_joins_sibling_reads() -> None:
    """One fatal child read cannot leave another provider task running."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        companion_started = asyncio.Event()
        companion_cancelled = asyncio.Event()

        async def fail_with_blocking_sibling(
            request: WatchObservationRequest,
        ) -> WatchObservation:
            if request.child.child_id == "direct":
                await companion_started.wait()
                msg = "fatal read"
                raise RuntimeError(msg)
            companion_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                companion_cancelled.set()
            raise AssertionError

        reader.observe = fail_with_blocking_sibling  # type: ignore[method-assign]
        operations, clock, _budgets, _checkpoints = _operations(reader)
        with pytest.raises(WatchStartError) as raised:
            await _collect(
                operations,
                WatchRequest(
                    intent_id=_record().intent_id,
                    condition=WatchCondition.GRANTED,
                    resume=None,
                    authentication_key=KEY,
                    installation_id="installation-123",
                    deadline=clock.monotonic() + 3,
                    cancellation=CancellationToken(),
                ),
            )
        assert raised.value.exit_class is ExitClass.OPERATIONAL_FAILURE
        assert companion_cancelled.is_set()

    asyncio.run(run())


def test_initial_target_mismatch_fails_before_any_public_event() -> None:
    """A same-name preference with a different target is never adopted."""

    async def run() -> None:
        submitted = _status(Reconciliation.RECONCILING)
        reader = _Reader({"direct": [submitted], "companion": [submitted]})
        original = reader.observe

        async def mismatch(
            request: WatchObservationRequest,
        ) -> WatchObservation:
            observed = await original(request)
            if request.child.child_id == "direct":
                return replace(
                    observed,
                    preference_target=QuotaQuantity(9, UNIT),
                )
            return observed

        reader.observe = mismatch  # type: ignore[method-assign]
        operations, clock, _budgets, _checkpoints = _operations(reader)
        request = WatchRequest(
            intent_id=_record().intent_id,
            condition=WatchCondition.GRANTED,
            resume=None,
            authentication_key=KEY,
            installation_id="installation-123",
            deadline=clock.monotonic() + 3,
            cancellation=CancellationToken(),
        )

        with pytest.raises(WatchStartError) as raised:
            await _collect(operations, request)
        assert raised.value.exit_class is ExitClass.REJECTED_PRECONDITION

    asyncio.run(run())


@pytest.mark.parametrize(
    ("condition", "status", "exit_class"),
    [
        (
            WatchCondition.FULFILLED,
            _status(Reconciliation.SETTLED, granted=8, effective=8),
            ExitClass.SUCCESS,
        ),
        (
            WatchCondition.GRANTED,
            _status(Reconciliation.UNKNOWN),
            ExitClass.TIMEOUT,
        ),
    ],
)
def test_full_fulfillment_and_recoverable_unknown_keep_distinct_boundaries(
    condition: WatchCondition,
    status: QuotaRequestStatus,
    exit_class: ExitClass,
) -> None:
    """Fresh full evidence succeeds; provider unknown remains pending."""

    async def run() -> None:
        reader = _Reader({"direct": [status], "companion": [status]})
        operations, clock, _budgets, _checkpoints = _operations(reader)
        events = await _collect(
            operations,
            WatchRequest(
                intent_id=_record().intent_id,
                condition=condition,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )

        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is exit_class
        assert all(
            summary.status is None
            or summary.status.reconciliation is status.reconciliation
            for summary in events[-1].aggregate.children
        )

    asyncio.run(run())


def test_stable_trace_allows_etag_rotation_across_resume() -> None:
    """A stable provider trace preserves intent lineage across etag updates."""

    async def run() -> None:
        record = replace(
            _record(),
            children=tuple(
                replace(
                    child,
                    accepted_etag=None,
                    accepted_trace_id=f"trace-{child.child_id}",
                )
                if child.disposition is ApplyChildDisposition.ACCEPTED
                else child
                for child in _record().children
            ),
        )
        submitted = _status(Reconciliation.RECONCILING)
        first_reader = _Reader({"direct": [submitted], "companion": [submitted]})
        operations, clock, _budgets, _checkpoints = _operations(
            first_reader,
            record=record,
        )
        first = await _collect(
            operations,
            WatchRequest(
                intent_id=record.intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )
        resumed_reader = _Reader({"direct": [submitted], "companion": [submitted]})
        original = resumed_reader.observe

        async def rotated(
            request: WatchObservationRequest,
        ) -> WatchObservation:
            observation = await original(request)
            return replace(observation, etag=f"rotated-{request.child.child_id}")

        resumed_reader.observe = rotated  # type: ignore[method-assign]
        resumed = WatchOperations(
            apply_records=operations.apply_records,
            checkpoints=operations.checkpoints,
            resume_codec=HmacWatchResumeCodec(),
            reader=resumed_reader,
            budgets=operations.budgets,
            clock=clock,
            stream_ids=lambda: "stream-2",
            jitter=_Jitter(),
            poll_interval_seconds=1,
        )

        events = await _collect(
            resumed,
            WatchRequest(
                intent_id=None,
                condition=None,
                resume=first[-1].resume,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )

        assert events[0].event is WatchEventKind.INITIAL
        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is ExitClass.TIMEOUT

    asyncio.run(run())


def test_replayed_resolution_closes_with_terminal_if_initial_read_fails() -> None:
    """A replay event makes later setup failure a streamed terminal outcome."""

    async def run() -> None:
        record = replace(
            _record(),
            children=(
                _child("direct", ApplyChildDisposition.ACCEPTED),
                _child("later", ApplyChildDisposition.UNKNOWN),
            ),
            state=ApplyRecordState.UNKNOWN,
        )
        submitted = _status(Reconciliation.RECONCILING)
        operations, clock, _budgets, _checkpoints = _operations(
            _Reader({"direct": [submitted]}),
            record=record,
        )
        first = await _collect(
            operations,
            WatchRequest(
                intent_id=record.intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )
        records = cast("_Records", operations.apply_records)
        records.resolutions = (
            UnknownResolutionEvidence(
                record.intent_id,
                "later",
                UnknownDispatchResolution.ACCEPTED,
                clock.now(),
                lineage_etag="resolved-etag",
            ),
        )
        reader = _Reader({"direct": [submitted], "later": [submitted]})

        original = reader.observe

        async def fail(request: WatchObservationRequest) -> WatchObservation:
            if request.child.child_id == "later":
                msg = "provider read failed"
                raise RuntimeError(msg)
            return await original(request)

        reader.observe = fail  # type: ignore[method-assign]
        resumed = WatchOperations(
            apply_records=operations.apply_records,
            checkpoints=operations.checkpoints,
            resume_codec=HmacWatchResumeCodec(),
            reader=reader,
            budgets=operations.budgets,
            clock=clock,
            stream_ids=lambda: "stream-2",
            jitter=_Jitter(),
            poll_interval_seconds=1,
        )
        events = await _collect(
            resumed,
            WatchRequest(
                intent_id=None,
                condition=None,
                resume=first[-1].resume,
                authentication_key=KEY,
                installation_id="installation-123",
                deadline=clock.monotonic() + 1,
                cancellation=CancellationToken(),
            ),
        )

        assert [event.event for event in events] == [
            WatchEventKind.INITIAL,
            WatchEventKind.ACCEPTED_WATCH_SET_CHANGED,
            WatchEventKind.TERMINAL,
        ]
        assert events[-1].result is not None
        assert events[-1].result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
        assert not events[-1].result.completeness.is_complete
        assert events[-1].diagnostics[0].code.value == "watch-observation-failed"

    asyncio.run(run())
