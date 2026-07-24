"""Surface-neutral Apply and deterministic child reconciliation operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from cqmgr.application.ports.apply import (
    ApplyRevalidation,
    ApplyRevalidator,
)
from cqmgr.application.ports.apply_records import ApplyRecordRepositoryStatus
from cqmgr.application.ports.plans import PlanRepositoryStatus
from cqmgr.application.ports.provider_writes import (
    QuotaPreferenceUnknownResolutionResult,
    QuotaPreferenceWrite,
    QuotaPreferenceWriteAction,
    UnknownWriteResolution,
)
from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    ApplyChildRecord,
    ApplyRecord,
    ApplyRecordState,
    UnknownDispatchResolution,
)
from cqmgr.domain.audit import (
    AuditFact,
    AuditFactName,
    AuditRecordDraft,
    AuditRecordKind,
)
from cqmgr.domain.plans import (
    PlanKind,
    PlanLedgerState,
    QuotaRequestBundlePlan,
    QuotaRequestPlanChild,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.application.ports.apply import (
        ApplyContactRefresher,
        ApplyEvidenceRefresher,
        ApplyPrincipalRefresher,
    )
    from cqmgr.application.ports.apply_records import ApplyRecordRepository
    from cqmgr.application.ports.audit import AuditJournal
    from cqmgr.application.ports.plans import (
        PlanCodec,
        PlanLease,
        PlanRepository,
        PlanRepositoryOutcome,
    )
    from cqmgr.application.ports.provider_writes import (
        QuotaPreferenceUnknownResolver,
        QuotaPreferenceWriter,
    )
    from cqmgr.application.ports.secrets import SecretValue
    from cqmgr.domain.plans import (
        ContactBinding,
        EvidenceBinding,
        PlanPrincipal,
        QuotaPlan,
    )
    from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity, QuotaQuantity
    from cqmgr.domain.scopes import ResourceScope


@dataclass(frozen=True, slots=True)
class ApplyRequest:
    """Complete operator inputs required to consume one local plan."""

    digest: str
    authentication_key: SecretValue = field(repr=False)
    local_installation_id: str
    resource_scope_acknowledgement: ResourceScope
    principal: PlanPrincipal
    contact_binding: ContactBinding
    contact_value: str = field(repr=False)
    now: datetime


class ApplyProgressState(StrEnum):
    """Surface-neutral progress state for one ordered Apply child."""

    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    UNATTEMPTED = "unattempted"


@dataclass(frozen=True, slots=True)
class ApplyProgressEvent:
    """One observable transition in deterministic Apply child order."""

    order: int
    total: int
    child_id: str
    state: ApplyProgressState

    def __post_init__(self) -> None:
        """Reject progress facts that cannot identify one ordered child."""
        if not isinstance(self.order, int) or isinstance(self.order, bool):
            msg = "Apply progress order must be an integer"
            raise TypeError(msg)
        if not isinstance(self.total, int) or isinstance(self.total, bool):
            msg = "Apply progress total must be an integer"
            raise TypeError(msg)
        if self.total < 1 or self.order < 1 or self.order > self.total:
            msg = "Apply progress order must be within the non-empty child set"
            raise ValueError(msg)
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "Apply progress child_id must be a non-empty string"
            raise ValueError(msg)
        if not isinstance(self.state, ApplyProgressState):
            msg = "Apply progress state must be an ApplyProgressState"
            raise TypeError(msg)


type ApplyProgressObserver = Callable[[ApplyProgressEvent], None]
"""Best-effort presentation observer that cannot own Apply safety."""


class ComposedApplyRevalidator:
    """Production composition for independent read-only Apply refreshers."""

    def __init__(
        self,
        *,
        principal: ApplyPrincipalRefresher,
        contact: ApplyContactRefresher,
        evidence: ApplyEvidenceRefresher,
    ) -> None:
        """Bind current identity, contact, and provider-evidence refreshers."""
        self._principal = principal
        self._contact = contact
        self._evidence = evidence

    async def refresh(
        self,
        plan: QuotaPlan,
        now: datetime,
    ) -> ApplyRevalidation:
        """Refresh all mutation-gating facts without exposing a write seam."""
        principal, contact, evidence = await asyncio.gather(
            self._principal.refresh_principal(plan, now),
            self._contact.refresh_contact(plan.contact_binding, now),
            self._evidence.refresh_evidence(plan, now),
        )
        return ApplyRevalidation(
            resource_scope=evidence.resource_scope,
            principal=principal,
            contact_binding=contact.binding,
            contact_value=contact.value,
            constraints=evidence.constraints,
            children=evidence.children,
        )


@dataclass(frozen=True, slots=True)
class ApplyChildData:
    """One returned durable child outcome and provider identity."""

    child_id: str
    disposition: ApplyChildDisposition
    slice_identity: EffectiveQuotaSliceIdentity
    target: QuotaQuantity
    preference_identity: str
    etag: str | None
    trace_id: str | None = None
    provider_outcome: StableSymbol | None = None
    unknown_resolution: UnknownDispatchResolution | None = None
    audit_record_ids: tuple[str, ...] = ()
    submitted_at: datetime | None = None
    warnings: tuple[StableSymbol, ...] = ()
    required_acknowledgements: tuple[StableSymbol, ...] = ()
    acknowledgements: tuple[StableSymbol, ...] = ()

    @property
    def unresolved_acknowledgements(self) -> tuple[StableSymbol, ...]:
        """Return required Plan acknowledgements absent from the applied intent."""
        acknowledged = frozenset(self.acknowledgements)
        return tuple(
            item for item in self.required_acknowledgements if item not in acknowledged
        )


@dataclass(frozen=True, slots=True)
class ApplyData:
    """Durable Apply evidence returned through the shared result boundary."""

    plan_digest: str
    kind: PlanKind | None
    intent_id: str | None = None
    children: tuple[ApplyChildData, ...] = ()
    verified_no_ops: tuple[QuotaRequestPlanChild, ...] = ()
    audit_record_ids: tuple[str, ...] = ()
    quarantine_identity: str | None = None


class ApplyPlanOperations:
    """Consume a plan once and durably preserve every ordered child outcome."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        repository: PlanRepository,
        apply_records: ApplyRecordRepository,
        audit: AuditJournal,
        codec: PlanCodec,
        revalidator: ApplyRevalidator,
        writer: QuotaPreferenceWriter,
        unknown_resolver: QuotaPreferenceUnknownResolver,
    ) -> None:
        """Bind durable local state and narrow external provider boundaries."""
        self._repository = repository
        self._apply_records = apply_records
        self._audit = audit
        self._codec = codec
        self._revalidator = revalidator
        self._writer = writer
        self._unknown_resolver = unknown_resolver

    async def apply(  # noqa: C901, PLR0911
        self,
        request: ApplyRequest,
        *,
        on_progress: ApplyProgressObserver | None = None,
    ) -> OperationResult[ApplyData]:
        """Apply every child in plan-bound order without blind retry."""
        existing = self._apply_records.load(
            request.digest,
            request.authentication_key,
        )
        if (
            existing.status is ApplyRecordRepositoryStatus.AVAILABLE
            and existing.record is not None
        ):
            return await self._resume(
                existing.record,
                request,
                on_progress=on_progress,
            )
        loaded = self._repository.load(
            request.digest,
            request.authentication_key,
            request.now,
        )
        if (
            loaded.status is not PlanRepositoryStatus.AVAILABLE
            or loaded.plan_bytes is None
            or loaded.state is not PlanLedgerState.AVAILABLE
            or loaded.authenticated is not True
        ):
            return _result(
                request,
                reached=False,
                outcome="plan-unavailable",
                exit_class=ExitClass.STALE_OR_CONFLICTING,
            )
        decoded = None
        try:
            decoded = self._codec.decode(loaded.plan_bytes)
            authenticated = decoded.authenticate(request.authentication_key.reveal())
        except (TypeError, ValueError):
            authenticated = False
        if not authenticated or decoded is None or decoded.digest != request.digest:
            return _result(
                request,
                reached=False,
                outcome="plan-unauthenticated",
                exit_class=ExitClass.AUTHORIZATION,
            )
        plan = decoded.plan
        if _request_drift(plan, request):
            return _result(
                request,
                reached=False,
                outcome="plan-precondition-failed",
                exit_class=ExitClass.REJECTED_PRECONDITION,
                resource_scope=plan.resource_scope,
                plan=plan,
            )
        try:
            refreshed = await self._revalidator.refresh(plan, request.now)
        except Exception:  # noqa: BLE001
            return self._invalidate_after_preflight(
                plan,
                request,
                reason=StableSymbol("revalidation-incomplete"),
            )
        if _revalidation_drift(plan, request, refreshed):
            return self._invalidate_after_preflight(plan, request)

        lease_outcome = self._repository.acquire_lease(
            request.digest,
            request.authentication_key,
            request.now,
        )
        if (
            lease_outcome.status is not PlanRepositoryStatus.LEASED
            or lease_outcome.lease is None
        ):
            return _result(
                request,
                reached=False,
                outcome="plan-lease-failed",
                exit_class=ExitClass.STALE_OR_CONFLICTING,
                resource_scope=plan.resource_scope,
                plan=plan,
            )
        lease = lease_outcome.lease
        record = _apply_record(plan, request)
        audit_record_ids: list[str] = []
        try:
            audit_record_ids.append(
                self._append_audit(
                    request,
                    plan.resource_scope,
                    AuditRecordKind.APPLY_INTENT,
                    "pre-apply-intent",
                    record.intent_id,
                    facts=_aggregate_facts(record),
                )
            )
        except BaseException:  # noqa: BLE001
            return _result(
                request,
                reached=False,
                outcome="apply-intent-audit-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=plan.resource_scope,
                plan=plan,
            )
        created = self._apply_records.create(record, request.authentication_key)
        if (
            created.status is not ApplyRecordRepositoryStatus.STORED
            or created.record is None
        ):
            return _result(
                request,
                reached=False,
                outcome="apply-record-intent-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=plan.resource_scope,
                plan=plan,
                audit_record_ids=tuple(audit_record_ids),
            )
        record = created.record
        consumed = self._repository.mark_dispatched(
            lease,
            request.authentication_key,
            request.now,
        )
        if consumed.status is not PlanRepositoryStatus.DISPATCHED:
            return _result(
                request,
                reached=False,
                outcome="plan-consumption-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=plan.resource_scope,
                plan=plan,
                intent_id=record.intent_id,
                audit_record_ids=tuple(audit_record_ids),
            )

        return await self._dispatch_children(
            plan,
            lease,
            request,
            record,
            audit_record_ids,
            refreshed.contact_value,
            on_progress=on_progress,
        )

    def _invalidate_after_preflight(
        self,
        plan: QuotaPlan,
        request: ApplyRequest,
        *,
        reason: StableSymbol | None = None,
    ) -> OperationResult[ApplyData]:
        """Acquire only the authority required to persist a no-write invalidation."""
        lease_outcome = self._repository.acquire_lease(
            request.digest,
            request.authentication_key,
            request.now,
        )
        if (
            lease_outcome.status is not PlanRepositoryStatus.LEASED
            or lease_outcome.lease is None
        ):
            return _result(
                request,
                reached=False,
                outcome="plan-lease-failed",
                exit_class=ExitClass.STALE_OR_CONFLICTING,
                resource_scope=plan.resource_scope,
                plan=plan,
            )
        return self._invalidate(
            plan,
            lease_outcome.lease,
            request,
            reason=reason,
        )

    async def _dispatch_children(  # noqa: C901, PLR0911, PLR0913
        self,
        plan: QuotaPlan,
        lease: PlanLease,
        request: ApplyRequest,
        record: ApplyRecord,
        audit_record_ids: list[str],
        contact_value: str,
        *,
        on_progress: ApplyProgressObserver | None,
    ) -> OperationResult[ApplyData]:
        """Resume at the first child without durable dispatch intent."""
        planned_by_id = {child.child_id: child for child in plan.children}
        total = len(record.children)
        for order, child in enumerate(record.children, start=1):
            if child.disposition is ApplyChildDisposition.ACCEPTED:
                _notify_progress(
                    on_progress,
                    ApplyProgressEvent(
                        order,
                        total,
                        child.child_id,
                        ApplyProgressState.ACCEPTED,
                    ),
                )
                continue
            if child.disposition in {
                ApplyChildDisposition.FAILED,
                ApplyChildDisposition.UNKNOWN,
                ApplyChildDisposition.UNATTEMPTED,
            }:
                return self._critical_unknown(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                    plan=plan,
                    reconciliation_identity=child.preference_identity,
                )
            planned_child = planned_by_id[child.child_id]
            durable_before_intent = record
            write = QuotaPreferenceWrite(
                child_id=child.child_id,
                slice_identity=child.slice_identity,
                target=child.target,
                preference_identity=child.preference_identity,
                action=(
                    QuotaPreferenceWriteAction.AMEND
                    if child.preference_existed
                    else QuotaPreferenceWriteAction.CREATE
                ),
                current_etag=child.etag,
                contact_value=contact_value,
                acknowledgements=planned_child.acknowledgements,
            )
            try:
                audit_record_ids.append(
                    self._append_audit(
                        request,
                        plan.resource_scope,
                        AuditRecordKind.APPLY_INTENT,
                        "child-dispatch-intent",
                        record.intent_id,
                        facts=_child_facts(child),
                    )
                )
                record = record.record_dispatch_intent(child.child_id, request.now)
                if not self._save(record, request):
                    return self._pre_dispatch_failure(
                        plan.resource_scope,
                        lease,
                        request,
                        durable_before_intent,
                        write.preference_identity,
                        audit_record_ids,
                        plan=plan,
                    )
            except BaseException:  # noqa: BLE001
                return self._pre_dispatch_failure(
                    plan.resource_scope,
                    lease,
                    request,
                    durable_before_intent,
                    write.preference_identity,
                    audit_record_ids,
                    plan=plan,
                )

            _notify_progress(
                on_progress,
                ApplyProgressEvent(
                    order,
                    total,
                    child.child_id,
                    ApplyProgressState.DISPATCHING,
                ),
            )
            try:
                provider_result = await self._writer.dispatch(write)
            except BaseException:  # noqa: BLE001
                result = await self._unknown_dispatch(
                    plan,
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    write,
                    audit_record_ids,
                )
                _notify_progress(
                    on_progress,
                    ApplyProgressEvent(
                        order,
                        total,
                        child.child_id,
                        ApplyProgressState.UNKNOWN,
                    ),
                )
                return result

            disposition = (
                ApplyChildDisposition.ACCEPTED
                if provider_result.accepted
                else ApplyChildDisposition.FAILED
            )
            record = record.record_outcome(
                child.child_id,
                disposition,
                provider_result.outcome,
                request.now,
                accepted_etag=provider_result.etag,
                accepted_trace_id=provider_result.trace_id,
            )
            if not self._save(record, request):
                return self._critical_unknown(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                    plan=plan,
                )
            try:
                audit_record_ids.append(
                    self._append_audit(
                        request,
                        plan.resource_scope,
                        AuditRecordKind.APPLY_RESULT,
                        disposition.value,
                        record.intent_id,
                        facts=_child_facts(
                            next(
                                item
                                for item in record.children
                                if item.child_id == child.child_id
                            )
                        ),
                    )
                )
            except BaseException:  # noqa: BLE001
                return self._critical_unknown(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                    plan=plan,
                )
            _notify_progress(
                on_progress,
                ApplyProgressEvent(
                    order,
                    total,
                    child.child_id,
                    ApplyProgressState(disposition.value),
                ),
            )
            if disposition is ApplyChildDisposition.FAILED:
                record = record.finalize(request.now)
                if not self._save(record, request):
                    return self._critical_unknown(
                        plan.resource_scope,
                        lease,
                        request,
                        record,
                        audit_record_ids,
                        plan=plan,
                    )
                return self._finish(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                    plan=plan,
                    outcome=provider_result.outcome.value,
                    exit_class=_failure_exit(provider_result.outcome),
                )

        record = record.finalize(request.now)
        if not self._save(record, request):
            return self._critical_unknown(
                plan.resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        return self._finish(
            plan.resource_scope,
            lease,
            request,
            record,
            audit_record_ids,
            plan=plan,
            outcome="applied",
            exit_class=ExitClass.SUCCESS,
        )

    async def _resume(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        record: ApplyRecord,
        request: ApplyRequest,
        *,
        on_progress: ApplyProgressObserver | None,
    ) -> OperationResult[ApplyData]:
        """Recover an already-consumed Apply without re-dispatching an intent."""
        resumed = self._repository.resume_dispatched(
            request.digest,
            request.authentication_key,
            request.now,
        )
        plan = self._authenticated_resumed_plan(resumed, request)
        consumed_terminal = (
            resumed.status is PlanRepositoryStatus.CONSUMED
            and plan is not None
            and record.state
            in {
                ApplyRecordState.ACCEPTED,
                ApplyRecordState.FAILED,
            }
            and record.intent_id == request.digest
            and record.plan_digest == request.digest
            and record.resource_scope == request.resource_scope_acknowledgement
        )
        if consumed_terminal:
            return self._project_consumed_terminal(record, request, plan)
        quarantined_unknown = (
            resumed.status is PlanRepositoryStatus.QUARANTINED
            and record.state is ApplyRecordState.UNKNOWN
        )
        quarantined_prewrite_failure = (
            resumed.status is PlanRepositoryStatus.QUARANTINED
            and resumed.reason == StableSymbol("dispatch-intent-persistence-failed")
            and record.state is ApplyRecordState.FAILED
            and not any(
                child.disposition is ApplyChildDisposition.FAILED
                for child in record.children
            )
        )
        recoverable_quarantine = quarantined_unknown or quarantined_prewrite_failure
        if (
            (
                resumed.status
                not in {
                    PlanRepositoryStatus.LEASED,
                    PlanRepositoryStatus.DISPATCHED,
                }
                and not recoverable_quarantine
            )
            or resumed.plan_bytes is None
            or resumed.authenticated is not True
            or (resumed.lease is None and not recoverable_quarantine)
        ):
            return _result_for_record(
                request,
                record.resource_scope,
                record,
                reached=False,
                outcome="apply-recovery-unavailable",
                exit_class=ExitClass.STALE_OR_CONFLICTING,
                audit_record_ids=(),
            )
        if plan is None:
            return self._critical_unknown(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                [],
            )
        if (
            record.state
            in {
                ApplyRecordState.ACCEPTED,
                ApplyRecordState.FAILED,
            }
            and not quarantined_prewrite_failure
        ):
            if (
                resumed.status is not PlanRepositoryStatus.DISPATCHED
                or resumed.lease is None
            ):
                return self._critical_unknown(
                    record.resource_scope,
                    resumed.lease,
                    request,
                    record,
                    [],
                    plan=plan,
                )
            if record.state is ApplyRecordState.ACCEPTED:
                return self._finish(
                    record.resource_scope,
                    resumed.lease,
                    request,
                    record,
                    [],
                    plan=plan,
                    outcome="applied",
                    exit_class=ExitClass.SUCCESS,
                )
            failed = next(
                (
                    child
                    for child in record.children
                    if child.disposition is ApplyChildDisposition.FAILED
                ),
                None,
            )
            if failed is None:
                return self._finish(
                    record.resource_scope,
                    resumed.lease,
                    request,
                    record,
                    [],
                    plan=plan,
                    outcome="dispatch-intent-persistence-failed",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                )
            provider_outcome = cast("StableSymbol", failed.provider_outcome)
            return self._finish(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                [],
                plan=plan,
                outcome=provider_outcome.value,
                exit_class=_failure_exit(provider_outcome),
            )
        audit_record_ids: list[str] = []
        if quarantined_prewrite_failure:
            try:
                audit_record_ids.append(
                    self._append_audit(
                        request,
                        record.resource_scope,
                        AuditRecordKind.APPLY_RESULT,
                        "dispatch-intent-persistence-failed",
                        record.intent_id,
                        facts=_aggregate_facts(record),
                        occurred_at=cast("datetime", record.finished_at),
                        deduplicate=True,
                    )
                )
            except Exception:  # noqa: BLE001
                return self._critical_unknown(
                    record.resource_scope,
                    None,
                    request,
                    record,
                    audit_record_ids,
                    plan=plan,
                )
            return _result_for_record(
                request,
                record.resource_scope,
                record,
                reached=False,
                outcome="dispatch-intent-persistence-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                plan=plan,
                audit_record_ids=tuple(audit_record_ids),
            )
        try:
            audit_record_ids.extend(
                self._append_child_outcome_audit(
                    request,
                    record.resource_scope,
                    record,
                    child,
                )
                for child in record.children
                if child.disposition is ApplyChildDisposition.ACCEPTED
            )
        except Exception:  # noqa: BLE001
            return self._critical_unknown(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        stopping = next(
            (
                child
                for child in record.children
                if child.disposition
                in {
                    ApplyChildDisposition.FAILED,
                    ApplyChildDisposition.UNKNOWN,
                }
            ),
            None,
        )
        unresolved = next(
            (
                child
                for child in record.children
                if child.dispatch_intent_at is not None and child.disposition is None
            ),
            None,
        )
        if stopping is not None or unresolved is not None:
            if (
                resumed.status
                not in {
                    PlanRepositoryStatus.DISPATCHED,
                    PlanRepositoryStatus.QUARANTINED,
                }
                or (
                    resumed.status is PlanRepositoryStatus.QUARANTINED
                    and stopping is None
                )
                or (
                    resumed.lease is None
                    and resumed.status is not PlanRepositoryStatus.QUARANTINED
                )
            ):
                return self._critical_unknown(
                    record.resource_scope,
                    resumed.lease,
                    request,
                    record,
                    [],
                    plan=plan,
                )
            if stopping is not None:
                if stopping.disposition is ApplyChildDisposition.FAILED:
                    record = record.finalize(request.now)
                    if not self._save(record, request):
                        return self._critical_unknown(
                            record.resource_scope,
                            resumed.lease,
                            request,
                            record,
                            audit_record_ids,
                            plan=plan,
                        )
                    provider_outcome = cast("StableSymbol", stopping.provider_outcome)
                    return self._finish(
                        record.resource_scope,
                        cast("PlanLease", resumed.lease),
                        request,
                        record,
                        audit_record_ids,
                        plan=plan,
                        outcome=provider_outcome.value,
                        exit_class=_failure_exit(provider_outcome),
                    )
                recovery_child = stopping
            else:
                recovery_child = cast("ApplyChildRecord", unresolved)
            planned = next(
                child
                for child in plan.children
                if child.child_id == recovery_child.child_id
            )
            write = QuotaPreferenceWrite(
                child_id=recovery_child.child_id,
                slice_identity=recovery_child.slice_identity,
                target=recovery_child.target,
                preference_identity=recovery_child.preference_identity,
                action=(
                    QuotaPreferenceWriteAction.AMEND
                    if recovery_child.preference_existed
                    else QuotaPreferenceWriteAction.CREATE
                ),
                current_etag=recovery_child.etag,
                contact_value=request.contact_value,
                acknowledgements=planned.acknowledgements,
            )
            if unresolved is not None:
                return await self._unknown_dispatch(
                    plan,
                    record.resource_scope,
                    cast("PlanLease", resumed.lease),
                    request,
                    record,
                    write,
                    audit_record_ids,
                )
            return await self._finish_unknown(
                plan,
                record.resource_scope,
                resumed.lease,
                request,
                record,
                write,
                audit_record_ids,
                already_quarantined=quarantined_unknown,
            )
        try:
            refreshed = await self._revalidator.refresh(plan, request.now)
        except BaseException:  # noqa: BLE001
            if resumed.status is PlanRepositoryStatus.LEASED:
                stopped = record.stop_unattempted(request.now)
                if not self._save(stopped, request):
                    return self._critical_unknown(
                        record.resource_scope,
                        resumed.lease,
                        request,
                        record,
                        audit_record_ids,
                        plan=plan,
                    )
                return self._invalidate(
                    plan,
                    cast("PlanLease", resumed.lease),
                    request,
                    reason=StableSymbol("revalidation-incomplete"),
                )
            return self._critical_unknown(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        accepted_child_ids = frozenset(
            child.child_id
            for child in record.children
            if child.disposition is ApplyChildDisposition.ACCEPTED
        )
        if _revalidation_drift(
            plan,
            request,
            refreshed,
            accepted_child_ids=accepted_child_ids,
        ):
            if resumed.status is PlanRepositoryStatus.LEASED:
                stopped = record.stop_unattempted(request.now)
                if not self._save(stopped, request):
                    return self._critical_unknown(
                        record.resource_scope,
                        resumed.lease,
                        request,
                        record,
                        audit_record_ids,
                        plan=plan,
                    )
                return self._invalidate(
                    plan,
                    cast("PlanLease", resumed.lease),
                    request,
                )
            return self._critical_unknown(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        if resumed.status is PlanRepositoryStatus.LEASED:
            consumed = self._repository.mark_dispatched(
                cast("PlanLease", resumed.lease),
                request.authentication_key,
                request.now,
            )
            if consumed.status is not PlanRepositoryStatus.DISPATCHED:
                return _result_for_record(
                    request,
                    record.resource_scope,
                    record,
                    reached=False,
                    outcome="plan-consumption-failed",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                    plan=plan,
                    audit_record_ids=tuple(audit_record_ids),
                )
        return await self._dispatch_children(
            plan,
            cast("PlanLease", resumed.lease),
            request,
            record,
            audit_record_ids,
            refreshed.contact_value,
            on_progress=on_progress,
        )

    def _authenticated_resumed_plan(
        self,
        resumed: PlanRepositoryOutcome,
        request: ApplyRequest,
    ) -> QuotaPlan | None:
        """Authenticate the immutable reviewed authority for terminal replay."""
        if resumed.authenticated is not True or resumed.plan_bytes is None:
            return None
        try:
            decoded = self._codec.decode(resumed.plan_bytes)
            authenticated = decoded.authenticate(request.authentication_key.reveal())
        except (TypeError, ValueError):
            return None
        if not (
            authenticated
            and decoded.digest == request.digest
            and not _request_drift(decoded.plan, request)
        ):
            return None
        return decoded.plan

    def _project_consumed_terminal(
        self,
        record: ApplyRecord,
        request: ApplyRequest,
        plan: QuotaPlan,
    ) -> OperationResult[ApplyData]:
        """Return one authenticated terminal Apply after ledger completion."""
        audit_record_ids: list[str] = []
        failed = next(
            (
                child
                for child in record.children
                if child.disposition is ApplyChildDisposition.FAILED
            ),
            None,
        )
        if record.state is ApplyRecordState.ACCEPTED:
            outcome = "applied"
            exit_class = ExitClass.SUCCESS
        elif failed is None:
            outcome = "dispatch-intent-persistence-failed"
            exit_class = ExitClass.OPERATIONAL_FAILURE
        else:
            provider_outcome = cast("StableSymbol", failed.provider_outcome)
            outcome = provider_outcome.value
            exit_class = _failure_exit(provider_outcome)
        try:
            audit_record_ids.extend(
                self._append_child_outcome_audit(
                    request,
                    record.resource_scope,
                    record,
                    child,
                )
                for child in record.children
                if child.disposition
                in {
                    ApplyChildDisposition.ACCEPTED,
                    ApplyChildDisposition.FAILED,
                }
            )
            audit_record_ids.append(
                self._append_audit(
                    request,
                    record.resource_scope,
                    AuditRecordKind.APPLY_RESULT,
                    outcome,
                    record.intent_id,
                    facts=_aggregate_facts(record),
                    occurred_at=cast("datetime", record.finished_at),
                    deduplicate=True,
                )
            )
        except Exception:  # noqa: BLE001
            return self._critical_unknown(
                record.resource_scope,
                None,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        return _result_for_record(
            request,
            record.resource_scope,
            record,
            reached=record.state is ApplyRecordState.ACCEPTED,
            outcome=outcome,
            exit_class=exit_class,
            plan=plan,
            audit_record_ids=tuple(audit_record_ids),
        )

    def _invalidate(
        self,
        plan: QuotaPlan,
        lease: PlanLease,
        request: ApplyRequest,
        *,
        reason: StableSymbol | None = None,
    ) -> OperationResult[ApplyData]:
        reason = reason or StableSymbol("child-evidence-drift")
        invalidated = self._repository.invalidate(
            lease,
            reason,
            request.authentication_key,
            request.now,
        )
        if invalidated.status is not PlanRepositoryStatus.INVALIDATED:
            return _result(
                request,
                reached=False,
                outcome="plan-invalidation-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=plan.resource_scope,
                plan=plan,
            )
        try:
            audit_id = self._append_audit(
                request,
                plan.resource_scope,
                AuditRecordKind.APPLY_RESULT,
                "plan-invalidated",
                request.digest,
                facts=(
                    AuditFact(
                        AuditFactName.PLAN_DIGEST,
                        RedactedText(request.digest),
                    ),
                ),
            )
        except BaseException:  # noqa: BLE001
            return _result(
                request,
                reached=False,
                outcome="plan-invalidation-audit-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=plan.resource_scope,
                plan=plan,
            )
        return _result(
            request,
            reached=False,
            outcome="plan-invalidated",
            exit_class=ExitClass.STALE_OR_CONFLICTING,
            resource_scope=plan.resource_scope,
            plan=plan,
            audit_record_ids=(audit_id,),
        )

    async def _unknown_dispatch(  # noqa: PLR0913
        self,
        plan: QuotaPlan,
        resource_scope: ResourceScope,
        lease: PlanLease,
        request: ApplyRequest,
        record: ApplyRecord,
        write: QuotaPreferenceWrite,
        audit_record_ids: list[str],
    ) -> OperationResult[ApplyData]:
        record = record.record_outcome(
            write.child_id,
            ApplyChildDisposition.UNKNOWN,
            StableSymbol("transport-unknown"),
            request.now,
        )
        if not self._save(record, request):
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        return await self._finish_unknown(
            plan,
            resource_scope,
            lease,
            request,
            record,
            write,
            audit_record_ids,
        )

    async def _finish_unknown(  # noqa: C901, PLR0911, PLR0913
        self,
        plan: QuotaPlan,
        resource_scope: ResourceScope,
        lease: PlanLease | None,
        request: ApplyRequest,
        record: ApplyRecord,
        write: QuotaPreferenceWrite,
        audit_record_ids: list[str],
        *,
        already_quarantined: bool = False,
    ) -> OperationResult[ApplyData]:
        """Finalize, contain, and reconcile one durable unknown write."""
        if record.state is ApplyRecordState.IN_PROGRESS:
            record = record.finalize(request.now)
            if not self._save(record, request):
                return self._critical_unknown(
                    resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                    plan=plan,
                )
        unknown_child = next(
            child for child in record.children if child.child_id == write.child_id
        )
        try:
            audit_record_ids.append(
                self._append_child_outcome_audit(
                    request,
                    resource_scope,
                    record,
                    unknown_child,
                )
            )
            audit_record_ids.append(
                self._append_audit(
                    request,
                    resource_scope,
                    AuditRecordKind.APPLY_RESULT,
                    "unknown-dispatch",
                    record.intent_id,
                    facts=_aggregate_facts(record),
                    occurred_at=cast("datetime", record.finished_at),
                    deduplicate=True,
                )
            )
        except BaseException:  # noqa: BLE001
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        if not already_quarantined and (
            lease is None
            or not self._quarantine(
                lease,
                request,
                "unknown-dispatch",
            )
        ):
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        loaded_resolutions = self._apply_records.load_unknown_resolutions(
            record.intent_id,
            request.authentication_key,
        )
        if loaded_resolutions.status is not ApplyRecordRepositoryStatus.AVAILABLE:
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        retained = next(
            (
                evidence
                for evidence in loaded_resolutions.resolutions
                if evidence.child_id == write.child_id
            ),
            None,
        )
        resolution_recorded_at = request.now
        if retained is not None:
            resolution_value = retained.resolution
            resolution_recorded_at = retained.recorded_at
            resolution = UnknownWriteResolution(resolution_value.value)
            resolution_etag = retained.lineage_etag
            resolution_trace_id = retained.lineage_trace_id
        else:
            try:
                resolution_result = await self._unknown_resolver.resolve_unknown(write)
            except Exception:  # noqa: BLE001
                resolution_result = QuotaPreferenceUnknownResolutionResult(
                    UnknownWriteResolution.UNRESOLVED
                )
            resolution = resolution_result.resolution
            resolution_etag = resolution_result.etag
            resolution_trace_id = resolution_result.trace_id
        if resolution is not UnknownWriteResolution.UNRESOLVED:
            resolution_value = UnknownDispatchResolution(resolution.value)
            if retained is None:
                appended = self._apply_records.append_unknown_resolution(
                    record.intent_id,
                    write.child_id,
                    resolution_value,
                    resolution_recorded_at,
                    request.authentication_key,
                    lineage_etag=resolution_etag,
                    lineage_trace_id=resolution_trace_id,
                )
                if appended.status is not ApplyRecordRepositoryStatus.STORED:
                    return self._critical_unknown(
                        resource_scope,
                        lease,
                        request,
                        record,
                        audit_record_ids,
                        plan=plan,
                    )
            record = record.resolve_unknown(
                write.child_id,
                resolution_value,
                resolution_recorded_at,
                accepted_etag=resolution_etag,
                accepted_trace_id=resolution_trace_id,
            )
            try:
                audit_record_ids.append(
                    self._append_audit(
                        request,
                        resource_scope,
                        AuditRecordKind.APPLY_RESULT,
                        f"unknown-resolved-{resolution.value}",
                        record.intent_id,
                        facts=_child_facts(
                            next(
                                child
                                for child in record.children
                                if child.child_id == write.child_id
                            )
                        ),
                        occurred_at=resolution_recorded_at,
                        deduplicate=True,
                    )
                )
            except BaseException:  # noqa: BLE001
                return self._critical_unknown(
                    resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                    plan=plan,
                )
        return _result_for_record(
            request,
            resource_scope,
            record,
            plan=plan,
            reached=False,
            outcome="unknown-dispatch",
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            audit_record_ids=tuple(audit_record_ids),
            quarantine_identity=write.preference_identity,
        )

    def _pre_dispatch_failure(  # noqa: PLR0913
        self,
        resource_scope: ResourceScope,
        lease: PlanLease,
        request: ApplyRequest,
        record: ApplyRecord,
        reconciliation_identity: str,
        audit_record_ids: list[str],
        *,
        plan: QuotaPlan,
    ) -> OperationResult[ApplyData]:
        terminal = record.stop_remaining_unattempted(request.now)
        if not self._save(terminal, request):
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
                reconciliation_identity=reconciliation_identity,
            )
        if not self._quarantine(
            lease,
            request,
            "dispatch-intent-persistence-failed",
        ):
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                terminal,
                audit_record_ids,
                plan=plan,
                reconciliation_identity=reconciliation_identity,
            )
        try:
            audit_record_ids.append(
                self._append_audit(
                    request,
                    resource_scope,
                    AuditRecordKind.APPLY_RESULT,
                    "dispatch-intent-persistence-failed",
                    terminal.intent_id,
                    facts=_aggregate_facts(terminal),
                    occurred_at=cast("datetime", terminal.finished_at),
                    deduplicate=True,
                )
            )
        except Exception:  # noqa: BLE001
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                terminal,
                audit_record_ids,
                plan=plan,
                reconciliation_identity=reconciliation_identity,
            )
        return _result_for_record(
            request,
            resource_scope,
            terminal,
            plan=plan,
            reached=False,
            outcome="dispatch-intent-persistence-failed",
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            audit_record_ids=tuple(audit_record_ids),
        )

    def _critical_unknown(  # noqa: PLR0913
        self,
        resource_scope: ResourceScope,
        lease: PlanLease | None,
        request: ApplyRequest,
        record: ApplyRecord,
        audit_record_ids: list[str],
        *,
        plan: QuotaPlan | None = None,
        reconciliation_identity: str | None = None,
    ) -> OperationResult[ApplyData]:
        quarantine_identity = reconciliation_identity or next(
            (
                child.preference_identity
                for child in reversed(record.children)
                if child.dispatch_intent_at is not None
            ),
            record.intent_id,
        )
        quarantined = lease is None or self._quarantine(
            lease,
            request,
            "critical-unknown",
        )
        critical_record, critical_audit_id = self._record_critical_evidence(
            resource_scope,
            request,
            record,
            quarantine_identity,
        )
        if critical_audit_id is not None and critical_audit_id not in audit_record_ids:
            audit_record_ids.append(critical_audit_id)
        return _result_for_record(
            request,
            resource_scope,
            critical_record,
            plan=plan,
            reached=False,
            outcome=(
                "critical-unknown" if quarantined else "critical-unknown-uncontained"
            ),
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            audit_record_ids=tuple(audit_record_ids),
            quarantine_identity=quarantine_identity,
        )

    def _record_critical_evidence(
        self,
        resource_scope: ResourceScope,
        request: ApplyRequest,
        record: ApplyRecord,
        quarantine_identity: str,
    ) -> tuple[ApplyRecord, str | None]:
        """Best-effort evidence after the primary persistence path failed."""
        loaded = self._apply_records.load(
            record.intent_id,
            request.authentication_key,
        )
        durable = (
            loaded.record
            if loaded.status is ApplyRecordRepositoryStatus.AVAILABLE
            and loaded.record is not None
            else record
        )
        try:
            unresolved = next(
                (
                    child
                    for child in durable.children
                    if child.dispatch_intent_at is not None
                    and child.disposition is None
                ),
                None,
            )
            if unresolved is not None:
                durable = durable.record_outcome(
                    unresolved.child_id,
                    ApplyChildDisposition.UNKNOWN,
                    StableSymbol("terminal-persistence-unknown"),
                    request.now,
                )
                self._save(durable, request)
                durable = durable.finalize(request.now)
                self._save(durable, request)
            elif durable.state is ApplyRecordState.IN_PROGRESS and not any(
                child.dispatch_intent_at is not None for child in durable.children
            ):
                durable = durable.stop_unattempted(request.now)
                self._save(durable, request)
            critical = durable.mark_critical_unknown(request.now)
            if self._save(critical, request):
                durable = critical
            critical_audit_id = self._append_audit(
                request,
                resource_scope,
                AuditRecordKind.CRITICAL_UNKNOWN,
                "critical-unknown",
                record.intent_id,
                facts=(
                    AuditFact(
                        AuditFactName.PREFERENCE_IDENTITY,
                        RedactedText(quarantine_identity),
                    ),
                ),
            )
        except BaseException:  # noqa: BLE001
            return durable, None
        return durable, critical_audit_id

    def _quarantine(
        self,
        lease: PlanLease,
        request: ApplyRequest,
        reason: str,
    ) -> bool:
        return (
            self._repository.quarantine(
                lease,
                StableSymbol(reason),
                request.authentication_key,
                request.now,
            ).status
            is PlanRepositoryStatus.QUARANTINED
        )

    def _finish(  # noqa: PLR0913
        self,
        resource_scope: ResourceScope,
        lease: PlanLease,
        request: ApplyRequest,
        record: ApplyRecord,
        audit_record_ids: list[str],
        *,
        plan: QuotaPlan | None = None,
        outcome: str,
        exit_class: ExitClass,
    ) -> OperationResult[ApplyData]:
        try:
            failed = next(
                (
                    child
                    for child in record.children
                    if child.disposition is ApplyChildDisposition.FAILED
                ),
                None,
            )
            if failed is not None:
                child_audit_id = self._append_child_outcome_audit(
                    request,
                    resource_scope,
                    record,
                    failed,
                )
                if child_audit_id not in audit_record_ids:
                    audit_record_ids.append(child_audit_id)
            aggregate_audit_id = self._append_audit(
                request,
                resource_scope,
                AuditRecordKind.APPLY_RESULT,
                outcome,
                record.intent_id,
                facts=_aggregate_facts(record),
                occurred_at=cast("datetime", record.finished_at),
                deduplicate=True,
            )
            audit_record_ids.append(aggregate_audit_id)
        except BaseException:  # noqa: BLE001
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        completed = self._repository.complete(
            lease,
            request.authentication_key,
            request.now,
        )
        if completed.status is not PlanRepositoryStatus.CONSUMED:
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
                plan=plan,
            )
        return _result_for_record(
            request,
            resource_scope,
            record,
            plan=plan,
            reached=record.state is ApplyRecordState.ACCEPTED,
            outcome=outcome,
            exit_class=exit_class,
            audit_record_ids=tuple(audit_record_ids),
        )

    def _save(self, record: ApplyRecord, request: ApplyRequest) -> bool:
        return (
            self._apply_records.save(record, request.authentication_key).status
            is ApplyRecordRepositoryStatus.STORED
        )

    def _append_child_outcome_audit(
        self,
        request: ApplyRequest,
        resource_scope: ResourceScope,
        record: ApplyRecord,
        child: ApplyChildRecord,
    ) -> str:
        """Append one stable child outcome fact or return its retained identity."""
        if child.disposition is ApplyChildDisposition.UNKNOWN:
            kind = AuditRecordKind.CRITICAL_UNKNOWN
            outcome = "unknown"
        else:
            kind = AuditRecordKind.APPLY_RESULT
            outcome = cast("ApplyChildDisposition", child.disposition).value
        return self._append_audit(
            request,
            resource_scope,
            kind,
            outcome,
            record.intent_id,
            facts=_child_facts(child),
            occurred_at=cast("datetime", child.outcome_recorded_at),
            deduplicate=True,
        )

    def _append_audit(  # noqa: PLR0913
        self,
        request: ApplyRequest,
        resource_scope: ResourceScope,
        kind: AuditRecordKind,
        outcome: str,
        correlation_id: str,
        *,
        facts: tuple[AuditFact, ...],
        occurred_at: datetime | None = None,
        deduplicate: bool = False,
    ) -> str:
        retained = self._audit.append(
            AuditRecordDraft(
                kind=kind,
                operation=OperationName("plan.apply"),
                resource_scope=resource_scope,
                occurred_at=occurred_at or request.now,
                outcome=StableSymbol(outcome),
                correlation_id=RedactedText(correlation_id),
                facts=facts,
            ),
            sensitive_values=(request.contact_value,),
            deduplicate=deduplicate,
        )
        return retained.record_id


def _request_drift(plan: QuotaPlan, request: ApplyRequest) -> bool:
    return (
        request.local_installation_id != plan.installation_id
        or request.resource_scope_acknowledgement != plan.resource_scope
        or request.principal != plan.principal
        or request.contact_binding != plan.contact_binding
        or bool(plan.unresolved_acknowledgements)
    )


def _revalidation_drift(
    plan: QuotaPlan,
    request: ApplyRequest,
    refreshed: ApplyRevalidation,
    *,
    accepted_child_ids: frozenset[str] = frozenset(),
) -> bool:
    if (
        refreshed.resource_scope != plan.resource_scope
        or request.resource_scope_acknowledgement != plan.resource_scope
        or refreshed.principal != plan.principal
        or request.principal != plan.principal
        or refreshed.contact_binding != plan.contact_binding
        or request.contact_binding != plan.contact_binding
        or refreshed.constraints != plan.constraints
        or len(refreshed.children) != len(plan.children)
    ):
        return True
    for expected, current in zip(plan.children, refreshed.children, strict=True):
        if (
            current.child_id != expected.child_id
            or current.slice_identity != expected.slice_identity
        ):
            return True
        if expected.child_id in accepted_child_ids:
            continue
        if (
            current.effective != expected.effective
            or current.usage != expected.usage
            or current.preference_name != expected.preference_name
            or current.preference_etag != expected.preference_etag
            or _evidence_drift(
                expected.evidence,
                current.evidence,
                request.now,
            )
            or not current.fresh
            or not current.complete
            or current.ambiguous
            or not current.mutable
            or current.ongoing_rollout
        ):
            return True
    return False


def _evidence_drift(
    expected: tuple[EvidenceBinding, ...],
    current: tuple[EvidenceBinding, ...],
    now: datetime,
) -> bool:
    if tuple((item.name, item.value_digest) for item in current) != tuple(
        (item.name, item.value_digest) for item in expected
    ):
        return True
    return any(
        refreshed.observed_at < planned.observed_at or refreshed.observed_at > now
        for planned, refreshed in zip(expected, current, strict=True)
    )


def _apply_record(plan: QuotaPlan, request: ApplyRequest) -> ApplyRecord:
    return ApplyRecord(
        intent_id=request.digest,
        plan_digest=request.digest,
        kind=plan.kind,
        resource_scope=plan.resource_scope,
        created_at=request.now,
        children=tuple(
            ApplyChildRecord(
                child_id=child.child_id,
                slice_identity=child.slice_identity,
                target=child.target,
                preference_identity=(
                    child.preference_name
                    or _deterministic_preference_identity(child.slice_identity)
                ),
                etag=child.preference_etag,
                preference_existed=child.preference_name is not None,
                baseline=child.effective,
            )
            for child in plan.children
        ),
    )


def _deterministic_preference_identity(
    identity: EffectiveQuotaSliceIdentity,
) -> str:
    canonical = json.dumps(
        {
            "resource_scope": identity.resource_scope.canonical_name,
            "service": identity.service,
            "quota_id": identity.quota_id,
            "dimensions": identity.dimensions.items,
            "quota_scope": identity.quota_scope.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    suffix = hashlib.sha256(canonical).hexdigest()[:32]
    return (
        f"{identity.resource_scope.canonical_name}/locations/global/"
        f"quotaPreferences/cqmgr-{suffix}"
    )


def _aggregate_facts(record: ApplyRecord) -> tuple[AuditFact, ...]:
    return (
        AuditFact(AuditFactName.PLAN_DIGEST, RedactedText(record.plan_digest)),
        AuditFact(
            AuditFactName.PLAN_SUBJECT,
            RedactedText(
                f"{record.kind.value}:"
                f"{','.join(child.child_id for child in record.children)}"
            ),
        ),
        *(fact for child in record.children for fact in _child_facts(child)),
    )


def _child_facts(child: ApplyChildRecord) -> tuple[AuditFact, ...]:
    identity = child.slice_identity
    exact_slice = json.dumps(
        {
            "scope": identity.resource_scope.canonical_name,
            "service": identity.service,
            "quota_id": identity.quota_id,
            "dimensions": identity.dimensions.items,
            "quota_scope": identity.quota_scope.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        AuditFact(AuditFactName.PLAN_CHILD, RedactedText(child.child_id)),
        AuditFact(
            AuditFactName.PREFERENCE_IDENTITY,
            RedactedText(child.preference_identity),
        ),
        AuditFact(AuditFactName.EXACT_SLICE, RedactedText(exact_slice)),
        AuditFact(
            AuditFactName.TARGET,
            RedactedText(f"{child.target.value}:{child.target.unit.symbol}"),
        ),
        AuditFact(
            AuditFactName.ETAG,
            RedactedText(child.etag or "none"),
        ),
        AuditFact(
            AuditFactName.ACTION,
            RedactedText("amend" if child.preference_existed else "create"),
        ),
        AuditFact(
            AuditFactName.DISPOSITION,
            RedactedText(
                child.disposition.value if child.disposition is not None else "pending"
            ),
        ),
    )


def _failure_exit(outcome: StableSymbol) -> ExitClass:
    if outcome.value in {"conflicting", "unchanged", "etag-conflict"}:
        return ExitClass.STALE_OR_CONFLICTING
    return ExitClass.OPERATIONAL_FAILURE


def _notify_progress(
    observer: ApplyProgressObserver | None,
    event: ApplyProgressEvent,
) -> None:
    """Publish presentation progress without granting it Apply authority."""
    if observer is None:
        return
    try:
        observer(event)
    except Exception:  # noqa: BLE001 - presentation cannot interrupt Apply
        return


def _result_for_record(  # noqa: PLR0913
    request: ApplyRequest,
    resource_scope: ResourceScope,
    record: ApplyRecord,
    *,
    plan: QuotaPlan | None = None,
    reached: bool,
    outcome: str,
    exit_class: ExitClass,
    audit_record_ids: tuple[str, ...],
    quarantine_identity: str | None = None,
) -> OperationResult[ApplyData]:
    plan_children = (
        {child.child_id: child for child in plan.children} if plan is not None else {}
    )
    children = tuple(
        _apply_child_data(
            child,
            plan_children.get(child.child_id),
            audit_record_ids,
        )
        for child in record.children
        if child.disposition is not None
    )
    return _result(
        request,
        reached=reached,
        outcome=outcome,
        exit_class=exit_class,
        resource_scope=resource_scope,
        plan=plan,
        kind=record.kind,
        intent_id=record.intent_id,
        children=children,
        verified_no_ops=(
            plan.no_op_children if isinstance(plan, QuotaRequestBundlePlan) else ()
        ),
        audit_record_ids=audit_record_ids,
        quarantine_identity=quarantine_identity,
    )


def _apply_child_data(
    child: ApplyChildRecord,
    planned: QuotaRequestPlanChild | None,
    audit_record_ids: tuple[str, ...],
) -> ApplyChildData:
    """Project one durable outcome with its authenticated Plan safety context."""
    return ApplyChildData(
        child_id=child.child_id,
        disposition=cast("ApplyChildDisposition", child.disposition),
        slice_identity=child.slice_identity,
        target=child.target,
        preference_identity=child.preference_identity,
        etag=child.accepted_etag or child.etag,
        trace_id=child.accepted_trace_id,
        provider_outcome=child.provider_outcome,
        unknown_resolution=child.unknown_resolution,
        audit_record_ids=audit_record_ids,
        submitted_at=child.dispatch_intent_at,
        warnings=planned.warnings if planned is not None else (),
        required_acknowledgements=(
            planned.required_acknowledgements if planned is not None else ()
        ),
        acknowledgements=planned.acknowledgements if planned is not None else (),
    )


def _result(  # noqa: PLR0913
    request: ApplyRequest,
    *,
    reached: bool,
    outcome: str,
    exit_class: ExitClass,
    resource_scope: ResourceScope | None = None,
    plan: QuotaPlan | None = None,
    kind: PlanKind | None = None,
    intent_id: str | None = None,
    children: tuple[ApplyChildData, ...] = (),
    verified_no_ops: tuple[QuotaRequestPlanChild, ...] = (),
    audit_record_ids: tuple[str, ...] = (),
    quarantine_identity: str | None = None,
) -> OperationResult[ApplyData]:
    if plan is not None:
        kind = plan.kind
        if isinstance(plan, QuotaRequestBundlePlan):
            verified_no_ops = plan.no_op_children
    return OperationResult(
        operation=OperationName("plan.apply"),
        resource_scope=resource_scope,
        boundary=OperationBoundary(StableSymbol("plan-applied"), reached),
        outcome=Outcome(StableSymbol(outcome), exit_class),
        completeness=Completeness.complete(),
        started_at=request.now,
        finished_at=request.now,
        data=ApplyData(
            plan_digest=request.digest,
            kind=kind,
            intent_id=intent_id,
            children=children,
            verified_no_ops=verified_no_ops,
            audit_record_ids=audit_record_ids,
            quarantine_identity=quarantine_identity,
        ),
    )
