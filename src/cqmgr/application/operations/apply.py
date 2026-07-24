"""Surface-neutral Apply and deterministic child reconciliation operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cqmgr.application.ports.apply import (
    ApplyRevalidation,
    ApplyRevalidator,
)
from cqmgr.application.ports.apply_records import ApplyRecordRepositoryStatus
from cqmgr.application.ports.plans import PlanRepositoryStatus
from cqmgr.application.ports.provider_writes import (
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
from cqmgr.domain.plans import PlanLedgerState
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
    from cqmgr.application.ports.plans import PlanCodec, PlanLease, PlanRepository
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
    provider_outcome: StableSymbol | None = None
    unknown_resolution: UnknownDispatchResolution | None = None
    audit_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplyData:
    """Durable Apply evidence returned through the shared result boundary."""

    plan_digest: str
    intent_id: str | None = None
    children: tuple[ApplyChildData, ...] = ()
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
        self, request: ApplyRequest
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
            return await self._resume(existing.record, request)
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
            )
        created = self._apply_records.create(record, request.authentication_key)
        if created.status is not ApplyRecordRepositoryStatus.STORED:
            return _result(
                request,
                reached=False,
                outcome="apply-record-intent-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=plan.resource_scope,
                audit_record_ids=tuple(audit_record_ids),
            )
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
                intent_id=record.intent_id,
                audit_record_ids=tuple(audit_record_ids),
            )
        lease = lease_outcome.lease
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
    ) -> OperationResult[ApplyData]:
        """Resume at the first child without durable dispatch intent."""
        planned_by_id = {child.child_id: child for child in plan.children}
        for child in record.children:
            if child.disposition is ApplyChildDisposition.ACCEPTED:
                continue
            if child.disposition in {
                ApplyChildDisposition.FAILED,
                ApplyChildDisposition.UNKNOWN,
                ApplyChildDisposition.UNATTEMPTED,
            }:
                return _result_for_record(
                    request,
                    plan.resource_scope,
                    record,
                    reached=False,
                    outcome=record.state.value,
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                    audit_record_ids=tuple(audit_record_ids),
                )
            planned_child = planned_by_id[child.child_id]
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
                        record,
                        audit_record_ids,
                    )
            except BaseException:  # noqa: BLE001
                return self._pre_dispatch_failure(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                )

            try:
                provider_result = await self._writer.dispatch(write)
            except BaseException:  # noqa: BLE001
                return await self._unknown_dispatch(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    write,
                    audit_record_ids,
                )

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
            )
            if not self._save(record, request):
                return self._critical_unknown(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
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
                    )
                return self._finish(
                    plan.resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
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
            )
        return self._finish(
            plan.resource_scope,
            lease,
            request,
            record,
            audit_record_ids,
            outcome="applied",
            exit_class=ExitClass.SUCCESS,
        )

    async def _resume(  # noqa: C901, PLR0911
        self,
        record: ApplyRecord,
        request: ApplyRequest,
    ) -> OperationResult[ApplyData]:
        """Recover an already-consumed Apply without re-dispatching an intent."""
        resumed = self._repository.resume_dispatched(
            request.digest,
            request.authentication_key,
            request.now,
        )
        if (
            resumed.status
            not in {
                PlanRepositoryStatus.LEASED,
                PlanRepositoryStatus.DISPATCHED,
            }
            or resumed.plan_bytes is None
            or resumed.lease is None
            or resumed.authenticated is not True
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
        plan: QuotaPlan | None = None
        try:
            decoded = self._codec.decode(resumed.plan_bytes)
            plan = decoded.plan
            invalid = (
                decoded.digest != request.digest
                or not decoded.authenticate(request.authentication_key.reveal())
                or _request_drift(plan, request)
            )
        except (TypeError, ValueError):
            invalid = True
        if invalid or plan is None:
            return self._critical_unknown(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                [],
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
                        [],
                    )
                return self._invalidate(
                    plan,
                    resumed.lease,
                    request,
                    reason=StableSymbol("revalidation-incomplete"),
                )
            return self._critical_unknown(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                [],
            )
        if _revalidation_drift(plan, request, refreshed):
            if resumed.status is PlanRepositoryStatus.LEASED:
                stopped = record.stop_unattempted(request.now)
                if not self._save(stopped, request):
                    return self._critical_unknown(
                        record.resource_scope,
                        resumed.lease,
                        request,
                        record,
                        [],
                    )
                return self._invalidate(plan, resumed.lease, request)
            return self._critical_unknown(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                [],
            )
        if resumed.status is PlanRepositoryStatus.LEASED:
            consumed = self._repository.mark_dispatched(
                resumed.lease,
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
                    audit_record_ids=(),
                )
        unresolved = next(
            (
                child
                for child in record.children
                if child.dispatch_intent_at is not None and child.disposition is None
            ),
            None,
        )
        if unresolved is not None:
            planned = next(
                child
                for child in plan.children
                if child.child_id == unresolved.child_id
            )
            write = QuotaPreferenceWrite(
                child_id=unresolved.child_id,
                slice_identity=unresolved.slice_identity,
                target=unresolved.target,
                preference_identity=unresolved.preference_identity,
                action=(
                    QuotaPreferenceWriteAction.AMEND
                    if unresolved.preference_existed
                    else QuotaPreferenceWriteAction.CREATE
                ),
                current_etag=unresolved.etag,
                contact_value=refreshed.contact_value,
                acknowledgements=planned.acknowledgements,
            )
            return await self._unknown_dispatch(
                record.resource_scope,
                resumed.lease,
                request,
                record,
                write,
                [],
            )
        return await self._dispatch_children(
            plan,
            resumed.lease,
            request,
            record,
            [],
            refreshed.contact_value,
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
            )
        return _result(
            request,
            reached=False,
            outcome="plan-invalidated",
            exit_class=ExitClass.STALE_OR_CONFLICTING,
            resource_scope=plan.resource_scope,
            audit_record_ids=(audit_id,),
        )

    async def _unknown_dispatch(  # noqa: PLR0911, PLR0913
        self,
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
            )
        try:
            audit_record_ids.append(
                self._append_audit(
                    request,
                    resource_scope,
                    AuditRecordKind.CRITICAL_UNKNOWN,
                    "unknown",
                    record.intent_id,
                    facts=_child_facts(
                        next(
                            child
                            for child in record.children
                            if child.child_id == write.child_id
                        )
                    ),
                )
            )
        except BaseException:  # noqa: BLE001
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
            )
        record = record.finalize(request.now)
        if not self._save(record, request):
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
            )
        if not self._quarantine(lease, request, "unknown-dispatch"):
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
            )
        try:
            resolution = await self._unknown_resolver.resolve_unknown(write)
        except BaseException:  # noqa: BLE001
            resolution = UnknownWriteResolution.UNRESOLVED
        if resolution is not UnknownWriteResolution.UNRESOLVED:
            resolution_value = UnknownDispatchResolution(resolution.value)
            appended = self._apply_records.append_unknown_resolution(
                record.intent_id,
                write.child_id,
                resolution_value,
                request.now,
                request.authentication_key,
            )
            if appended.status is not ApplyRecordRepositoryStatus.STORED:
                return self._critical_unknown(
                    resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                )
            record = record.resolve_unknown(
                write.child_id,
                resolution_value,
                request.now,
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
                    )
                )
            except BaseException:  # noqa: BLE001
                return self._critical_unknown(
                    resource_scope,
                    lease,
                    request,
                    record,
                    audit_record_ids,
                )
        return _result_for_record(
            request,
            resource_scope,
            record,
            reached=False,
            outcome="unknown-dispatch",
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            audit_record_ids=tuple(audit_record_ids),
            quarantine_identity=write.preference_identity,
        )

    def _pre_dispatch_failure(
        self,
        resource_scope: ResourceScope,
        lease: PlanLease,
        request: ApplyRequest,
        record: ApplyRecord,
        audit_record_ids: list[str],
    ) -> OperationResult[ApplyData]:
        if not self._quarantine(
            lease,
            request,
            "dispatch-intent-persistence-failed",
        ):
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
            )
        if not any(child.dispatch_intent_at is not None for child in record.children):
            record = record.stop_unattempted(request.now)
            self._save(record, request)
        return _result_for_record(
            request,
            resource_scope,
            record,
            reached=False,
            outcome="dispatch-intent-persistence-failed",
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            audit_record_ids=tuple(audit_record_ids),
        )

    def _critical_unknown(
        self,
        resource_scope: ResourceScope,
        lease: PlanLease,
        request: ApplyRequest,
        record: ApplyRecord,
        audit_record_ids: list[str],
    ) -> OperationResult[ApplyData]:
        quarantine_identity = next(
            (
                child.preference_identity
                for child in reversed(record.children)
                if child.dispatch_intent_at is not None
            ),
            record.intent_id,
        )
        quarantined = self._quarantine(lease, request, "critical-unknown")
        critical_record = self._record_critical_evidence(
            resource_scope,
            request,
            record,
            quarantine_identity,
        )
        return _result_for_record(
            request,
            resource_scope,
            critical_record,
            reached=False,
            outcome=(
                "critical-unknown" if quarantined else "critical-unknown-uncontained"
            ),
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            audit_record_ids=tuple(audit_record_ids),
            quarantine_identity=quarantine_identity if quarantined else None,
        )

    def _record_critical_evidence(
        self,
        resource_scope: ResourceScope,
        request: ApplyRequest,
        record: ApplyRecord,
        quarantine_identity: str,
    ) -> ApplyRecord:
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
            self._append_audit(
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
            return durable
        return durable

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
        outcome: str,
        exit_class: ExitClass,
    ) -> OperationResult[ApplyData]:
        try:
            audit_record_ids.append(
                self._append_audit(
                    request,
                    resource_scope,
                    AuditRecordKind.APPLY_RESULT,
                    outcome,
                    record.intent_id,
                    facts=_aggregate_facts(record),
                )
            )
        except BaseException:  # noqa: BLE001
            return self._critical_unknown(
                resource_scope,
                lease,
                request,
                record,
                audit_record_ids,
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
            )
        return _result_for_record(
            request,
            resource_scope,
            record,
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

    def _append_audit(  # noqa: PLR0913
        self,
        request: ApplyRequest,
        resource_scope: ResourceScope,
        kind: AuditRecordKind,
        outcome: str,
        correlation_id: str,
        *,
        facts: tuple[AuditFact, ...],
    ) -> str:
        retained = self._audit.append(
            AuditRecordDraft(
                kind=kind,
                operation=OperationName("plan.apply"),
                resource_scope=resource_scope,
                occurred_at=request.now,
                outcome=StableSymbol(outcome),
                correlation_id=RedactedText(correlation_id),
                facts=facts,
            ),
            sensitive_values=(request.contact_value,),
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
            or current.effective != expected.effective
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


def _result_for_record(  # noqa: PLR0913
    request: ApplyRequest,
    resource_scope: ResourceScope,
    record: ApplyRecord,
    *,
    reached: bool,
    outcome: str,
    exit_class: ExitClass,
    audit_record_ids: tuple[str, ...],
    quarantine_identity: str | None = None,
) -> OperationResult[ApplyData]:
    children = tuple(
        ApplyChildData(
            child.child_id,
            child.disposition,
            child.slice_identity,
            child.target,
            child.preference_identity,
            child.etag,
            child.provider_outcome,
            child.unknown_resolution,
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
        intent_id=record.intent_id,
        children=children,
        audit_record_ids=audit_record_ids,
        quarantine_identity=quarantine_identity,
    )


def _result(  # noqa: PLR0913
    request: ApplyRequest,
    *,
    reached: bool,
    outcome: str,
    exit_class: ExitClass,
    resource_scope: ResourceScope | None = None,
    intent_id: str | None = None,
    children: tuple[ApplyChildData, ...] = (),
    audit_record_ids: tuple[str, ...] = (),
    quarantine_identity: str | None = None,
) -> OperationResult[ApplyData]:
    return OperationResult(
        operation=OperationName("plan.apply"),
        resource_scope=resource_scope,
        boundary=OperationBoundary(StableSymbol("plan-applied"), reached),
        outcome=Outcome(StableSymbol(outcome), exit_class),
        completeness=Completeness.complete(),
        started_at=request.now,
        finished_at=request.now,
        data=ApplyData(
            request.digest,
            intent_id,
            children,
            audit_record_ids,
            quarantine_identity,
        ),
    )
