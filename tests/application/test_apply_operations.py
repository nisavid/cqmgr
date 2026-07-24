"""Apply and deterministic reconciliation acceptance tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast, override

import pytest

from cqmgr.adapters.persistence import apply_records as apply_record_persistence
from cqmgr.adapters.persistence.apply_records import LocalApplyRecordRepository
from cqmgr.adapters.serialization.plans import PlanCodec
from cqmgr.application.operations.apply import (
    ApplyData,
    ApplyPlanOperations,
    ApplyRequest,
    ComposedApplyRevalidator,
)
from cqmgr.application.ports.apply import (
    ApplyContactRefresh,
    ApplyEvidenceRefresh,
    ApplyRevalidation,
    RefreshedApplyChild,
)
from cqmgr.application.ports.apply_records import (
    ApplyRecordRepositoryOutcome,
    ApplyRecordRepositoryStatus,
)
from cqmgr.application.ports.plans import (
    PlanLease,
    PlanRepositoryOutcome,
    PlanRepositoryStatus,
)
from cqmgr.application.ports.provider_writes import (
    QuotaPreferenceWrite,
    QuotaPreferenceWriteAction,
    QuotaPreferenceWriteResult,
    UnknownWriteResolution,
)
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    ApplyChildRecord,
    ApplyRecord,
    ApplyRecordState,
    UnknownDispatchResolution,
    UnknownResolutionEvidence,
)
from cqmgr.domain.audit import AuditRecordKind
from cqmgr.domain.plans import (
    ContactBinding,
    EvidenceBinding,
    PlanKind,
    PlanLedgerState,
    PlanPrincipal,
    QuotaRequestBundlePlan,
    QuotaRequestPlan,
    QuotaRequestPlanChild,
    TargetStrategy,
)
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import ExitClass, OperationResult, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from pathlib import Path

    from cqmgr.application.ports.apply import ApplyRevalidator
    from cqmgr.application.ports.apply_records import ApplyRecordRepository
    from cqmgr.application.ports.audit import AuditJournal
    from cqmgr.application.ports.plans import (
        PlanCodec as PlanCodecPort,
    )
    from cqmgr.application.ports.plans import (
        PlanRepository,
    )
    from cqmgr.application.ports.provider_writes import (
        QuotaPreferenceUnknownResolver,
        QuotaPreferenceWriter,
    )
    from cqmgr.domain.audit import AuditRecordDraft
    from cqmgr.domain.plans import QuotaPlan

NOW = datetime(2026, 7, 24, 1, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
KEY = SecretValue(b"k" * 32)
PRINCIPAL = PlanPrincipal("principal://accounts/123")
CONTACT = ContactBinding(
    StableSymbol("direct-user"),
    "principal://accounts/123",
    "hmac-sha256:" + ("c" * 64),
)
UNIT = QuotaUnit("1")


def _slice(quota_id: str, quota_scope: QuotaScope) -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        resource_scope=SCOPE,
        service="compute.googleapis.com",
        quota_id=quota_id,
        dimensions=NormalizedDimensions((("region", "us-central1"),)),
        quota_scope=quota_scope,
    )


def _child(
    child_id: str,
    quota_id: str,
    *,
    direct_rank: int,
    scope_rank: int,
    target: int,
) -> QuotaRequestPlanChild:
    return QuotaRequestPlanChild(
        child_id=child_id,
        slice_identity=_slice(quota_id, QuotaScope.REGIONAL),
        target=QuotaQuantity(target, UNIT),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(2, UNIT),
        workload=QuotaQuantity(4, UNIT),
        prior_desired=None,
        granted=None,
        preference_name=None,
        preference_etag=None,
        target_strategy=TargetStrategy.MINIMUM,
        target_derivation=StableSymbol("usage-plus-workload"),
        direct_accelerator_rank=direct_rank,
        scope_breadth_rank=scope_rank,
        warnings=(),
        required_acknowledgements=(),
        acknowledgements=(),
        evidence=(),
    )


def _plan() -> QuotaRequestBundlePlan:
    children = (
        _child("direct", "GPU-DIRECT", direct_rank=0, scope_rank=1, target=6),
        _child("companion", "GPU-ALL", direct_rank=1, scope_rank=2, target=8),
    )
    return QuotaRequestBundlePlan(
        resource_scope=SCOPE,
        kind=PlanKind.BUNDLE,
        selected_location="us-central1",
        target_strategy=TargetStrategy.MINIMUM,
        normalized_workload="compute-instance:a4-highgpu-8g:1",
        children=children,
        constraints=tuple(
            ConstraintReference(child.slice_identity) for child in children
        ),
        principal=PRINCIPAL,
        contact_binding=CONTACT,
        installation_id="installation-123",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


class _MemoryPlanRepository:
    def __init__(self, plan: QuotaPlan) -> None:
        self.encoded = PlanCodec.encode(plan, KEY.reveal())
        self.state = PlanLedgerState.AVAILABLE
        self.invalidated_reason: StableSymbol | None = None
        self.fail_mark_dispatched = False
        self.fail_complete = False
        self.fail_quarantine = False
        self.fail_acquire = False
        self.fail_invalidate = False
        self.lease: PlanLease | None = None
        self.load_outcome: PlanRepositoryOutcome | None = None
        self.resume_outcome: PlanRepositoryOutcome | None = None

    def load(
        self, digest: str, _key: SecretValue, _now: datetime
    ) -> PlanRepositoryOutcome:
        assert digest == self.encoded.digest
        if self.load_outcome is not None:
            return self.load_outcome
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.AVAILABLE,
            plan_bytes=self.encoded.bytes,
            state=self.state,
            authenticated=True,
        )

    def acquire_lease(
        self, digest: str, _key: SecretValue, now: datetime
    ) -> PlanRepositoryOutcome:
        if self.fail_acquire:
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        self.state = PlanLedgerState.LEASED
        self.lease = PlanLease(digest, "lease-1", now + timedelta(minutes=1))
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.LEASED,
            state=self.state,
            lease=self.lease,
            authenticated=True,
        )

    def resume_dispatched(
        self, _digest: str, _key: SecretValue, _now: datetime
    ) -> PlanRepositoryOutcome:
        if self.resume_outcome is not None:
            return self.resume_outcome
        if (
            self.state
            not in {
                PlanLedgerState.LEASED,
                PlanLedgerState.DISPATCHED,
            }
            or self.lease is None
        ):
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        return PlanRepositoryOutcome(
            (
                PlanRepositoryStatus.LEASED
                if self.state is PlanLedgerState.LEASED
                else PlanRepositoryStatus.DISPATCHED
            ),
            plan_bytes=self.encoded.bytes,
            state=self.state,
            lease=self.lease,
            authenticated=True,
        )

    def invalidate(
        self,
        _lease: PlanLease,
        reason: StableSymbol,
        _key: SecretValue,
        _now: datetime,
    ) -> PlanRepositoryOutcome:
        if self.fail_invalidate:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        self.state = PlanLedgerState.INVALIDATED
        self.invalidated_reason = reason
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.INVALIDATED,
            state=self.state,
            reason=reason,
            authenticated=True,
        )

    def mark_dispatched(
        self, _lease: PlanLease, _key: SecretValue, _now: datetime
    ) -> PlanRepositoryOutcome:
        if self.fail_mark_dispatched:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        self.state = PlanLedgerState.DISPATCHED
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.DISPATCHED,
            state=self.state,
            authenticated=True,
        )

    def complete(
        self, _lease: PlanLease, _key: SecretValue, _now: datetime
    ) -> PlanRepositoryOutcome:
        if self.fail_complete:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        self.state = PlanLedgerState.CONSUMED
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.CONSUMED,
            state=self.state,
            authenticated=True,
        )

    def quarantine(
        self,
        _lease: PlanLease,
        reason: StableSymbol,
        _key: SecretValue,
        _now: datetime,
    ) -> PlanRepositoryOutcome:
        if self.fail_quarantine:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        self.state = PlanLedgerState.QUARANTINED
        self.invalidated_reason = reason
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.QUARANTINED,
            state=self.state,
            reason=reason,
            authenticated=True,
        )


class _MemoryApplyRecords:
    def __init__(self) -> None:
        self.record: ApplyRecord | None = None
        self.fail_on_revision: int | None = None
        self.fail_create = False
        self.fail_append_resolution = False
        self.resolutions: list[UnknownResolutionEvidence] = []

    def create(
        self, record: ApplyRecord, _key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        if self.fail_create:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        if self.record is not None and self.record != record:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        self.record = record
        return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.STORED, record)

    def load(self, _intent_id: str, _key: SecretValue) -> ApplyRecordRepositoryOutcome:
        if self.record is None:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE, self.record
        )

    def save(
        self, record: ApplyRecord, _key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        if record.revision == self.fail_on_revision:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        self.record = record
        return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.STORED, record)

    def append_unknown_resolution(
        self,
        intent_id: str,
        child_id: str,
        resolution: UnknownDispatchResolution,
        recorded_at: datetime,
        _authentication_key: SecretValue,
    ) -> ApplyRecordRepositoryOutcome:
        if self.fail_append_resolution:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        evidence = UnknownResolutionEvidence(
            intent_id,
            child_id,
            resolution,
            recorded_at,
        )
        if self.resolutions and self.resolutions != [evidence]:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        self.resolutions = [evidence]
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.STORED,
            resolutions=(evidence,),
        )

    def load_unknown_resolutions(
        self,
        _intent_id: str,
        _authentication_key: SecretValue,
    ) -> ApplyRecordRepositoryOutcome:
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE,
            resolutions=tuple(self.resolutions),
        )


@dataclass(frozen=True)
class _AuditRecord:
    record_id: str


class _MemoryAuditJournal:
    def __init__(self) -> None:
        self.drafts: list[AuditRecordDraft] = []
        self.fail_on_append: int | None = None

    def append(self, draft: AuditRecordDraft, **_kwargs: object) -> _AuditRecord:
        if len(self.drafts) + 1 == self.fail_on_append:
            raise OSError
        self.drafts.append(draft)
        return _AuditRecord(f"audit-{len(self.drafts):020d}")


class _ScriptedRevalidator:
    def __init__(self, result: ApplyRevalidation) -> None:
        self.result = result

    async def refresh(self, _plan: QuotaPlan, _now: datetime) -> ApplyRevalidation:
        return self.result


class _PrincipalRefresher:
    async def refresh_principal(self, plan: QuotaPlan, now: datetime) -> PlanPrincipal:
        del plan, now
        return PRINCIPAL


class _ContactRefresher:
    async def refresh_contact(
        self, binding: ContactBinding, now: datetime
    ) -> ApplyContactRefresh:
        del binding, now
        return ApplyContactRefresh(CONTACT, "resolved@example.com")


class _EvidenceRefresher:
    def __init__(self, plan: QuotaPlan) -> None:
        self._plan = plan

    async def refresh_evidence(
        self, plan: QuotaPlan, now: datetime
    ) -> ApplyEvidenceRefresh:
        del plan, now
        return ApplyEvidenceRefresh(
            SCOPE,
            self._plan.constraints,
            tuple(_refreshed(child) for child in self._plan.children),
        )


class _RaisingRevalidator:
    async def refresh(self, _plan: QuotaPlan, _now: datetime) -> ApplyRevalidation:
        raise OSError


class _CancellingRevalidator:
    async def refresh(self, _plan: QuotaPlan, _now: datetime) -> ApplyRevalidation:
        raise asyncio.CancelledError


class _FailingCodec:
    @staticmethod
    def decode(_data: bytes) -> object:
        raise ValueError


class _RaisingResolver:
    async def resolve_unknown(
        self, _request: QuotaPreferenceWrite
    ) -> UnknownWriteResolution:
        raise OSError


class _FailFastWriter:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, _request: object) -> None:
        self.calls += 1
        msg = "revalidation failure reached provider dispatch"
        raise AssertionError(msg)

    async def resolve_unknown(
        self, _request: QuotaPreferenceWrite
    ) -> UnknownWriteResolution:
        msg = "revalidation failure reached unknown resolution"
        raise AssertionError(msg)


class _ScriptedWriter:
    def __init__(
        self,
        *results: QuotaPreferenceWriteResult | BaseException,
    ) -> None:
        self.results = list(results)
        self.requests: list[QuotaPreferenceWrite] = []

    async def dispatch(
        self, request: QuotaPreferenceWrite
    ) -> QuotaPreferenceWriteResult:
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _ScriptedResolver:
    def __init__(
        self,
        resolution: UnknownWriteResolution = UnknownWriteResolution.UNRESOLVED,
    ) -> None:
        self.resolution = resolution
        self.requests: list[QuotaPreferenceWrite] = []

    async def resolve_unknown(
        self, request: QuotaPreferenceWrite
    ) -> UnknownWriteResolution:
        self.requests.append(request)
        return self.resolution


def _refreshed(child: QuotaRequestPlanChild) -> RefreshedApplyChild:
    return RefreshedApplyChild(
        child_id=child.child_id,
        slice_identity=child.slice_identity,
        effective=child.effective,
        usage=child.usage,
        preference_name=child.preference_name,
        preference_etag=child.preference_etag,
        evidence=child.evidence,
    )


def test_all_child_revalidation_drift_invalidates_without_provider_writes() -> None:
    """One later-child drift invalidates the whole plan before consumption."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    audit = _MemoryAuditJournal()
    writer = _FailFastWriter()
    refreshed = tuple(_refreshed(child) for child in plan.children)
    revalidation = ApplyRevalidation(
        resource_scope=SCOPE,
        principal=PRINCIPAL,
        contact_binding=CONTACT,
        contact_value="resolved@example.com",
        constraints=plan.constraints,
        children=(
            refreshed[0],
            RefreshedApplyChild(
                child_id=refreshed[1].child_id,
                slice_identity=refreshed[1].slice_identity,
                effective=QuotaQuantity(5, UNIT),
                usage=refreshed[1].usage,
                preference_name=refreshed[1].preference_name,
                preference_etag=refreshed[1].preference_etag,
                evidence=refreshed[1].evidence,
            ),
        ),
    )
    result = asyncio.run(
        ApplyPlanOperations(
            repository=cast("PlanRepository", repository),
            apply_records=cast("ApplyRecordRepository", _MemoryApplyRecords()),
            audit=cast("AuditJournal", audit),
            codec=cast("PlanCodecPort", PlanCodec()),
            revalidator=cast("ApplyRevalidator", _ScriptedRevalidator(revalidation)),
            writer=cast("QuotaPreferenceWriter", writer),
            unknown_resolver=cast(
                "QuotaPreferenceUnknownResolver",
                _ScriptedResolver(),
            ),
        ).apply(
            ApplyRequest(
                digest=repository.encoded.digest,
                authentication_key=KEY,
                local_installation_id="installation-123",
                resource_scope_acknowledgement=SCOPE,
                principal=PRINCIPAL,
                contact_binding=CONTACT,
                contact_value="operator@example.com",
                now=NOW + timedelta(minutes=1),
            )
        )
    )

    assert not result.boundary.reached
    assert result.outcome.code == StableSymbol("plan-invalidated")
    assert result.outcome.exit_class is ExitClass.STALE_OR_CONFLICTING
    assert writer.calls == 0
    assert repository.state is PlanLedgerState.INVALIDATED
    assert repository.invalidated_reason == StableSymbol("child-evidence-drift")
    assert [draft.kind for draft in audit.drafts] == [AuditRecordKind.APPLY_RESULT]


def test_composed_revalidator_assembles_all_current_mutation_gating_facts() -> None:
    """Production composition refreshes identity, contact, and all children."""
    plan = _plan()

    refreshed = asyncio.run(
        ComposedApplyRevalidator(
            principal=_PrincipalRefresher(),
            contact=_ContactRefresher(),
            evidence=_EvidenceRefresher(plan),
        ).refresh(plan, NOW + timedelta(minutes=1))
    )

    assert refreshed == _valid_revalidation(plan)
    assert "resolved@example.com" not in repr(refreshed)
    with pytest.raises(ValueError, match="contact value"):
        replace(refreshed, contact_value="")
    with pytest.raises(TypeError, match="must be tuples"):
        replace(
            refreshed,
            constraints=cast("tuple[ConstraintReference, ...]", []),
        )
    with pytest.raises(TypeError, match="must be tuples"):
        replace(
            refreshed,
            children=cast("tuple[RefreshedApplyChild, ...]", []),
        )
    with pytest.raises(ValueError, match="contact value"):
        ApplyContactRefresh(CONTACT, "")


def test_newer_equivalent_evidence_is_fresh_not_drift() -> None:
    """Revalidation may advance observation time when evidence values agree."""
    plan = _plan()
    evidence = EvidenceBinding(
        StableSymbol("effective-quota"),
        "sha256:" + ("e" * 64),
        NOW,
    )
    children = tuple(replace(child, evidence=(evidence,)) for child in plan.children)
    plan = replace(plan, children=children)
    current = _valid_revalidation(plan)
    current = replace(
        current,
        children=tuple(
            replace(
                child,
                evidence=(
                    replace(
                        child.evidence[0],
                        observed_at=NOW + timedelta(seconds=30),
                    ),
                ),
            )
            for child in current.children
        ),
    )
    writer = _ScriptedWriter(
        *(
            QuotaPreferenceWriteResult(
                accepted=True,
                outcome=StableSymbol("submitted"),
            )
            for _child_value in plan.children
        )
    )

    result, repository, _records, _audit = _apply(
        plan,
        writer,
        revalidator=_ScriptedRevalidator(current),
    )

    assert result.boundary.reached
    assert repository.state is PlanLedgerState.CONSUMED


def test_provider_write_port_excludes_retry_and_validate_only_controls() -> None:
    """The one-call mutation seam binds only reviewed provider intent."""
    plan = _single_plan()
    child = plan.children[0]
    write = QuotaPreferenceWrite(
        child_id=child.child_id,
        slice_identity=child.slice_identity,
        target=child.target,
        preference_identity=cast("str", plan.preference_name),
        action=QuotaPreferenceWriteAction.AMEND,
        current_etag=plan.preference_etag,
        contact_value="operator@example.com",
    )

    assert {item.name for item in fields(write)} == {
        "child_id",
        "slice_identity",
        "target",
        "preference_identity",
        "action",
        "current_etag",
        "contact_value",
        "acknowledgements",
    }
    with pytest.raises(TypeError, match="accepted"):
        QuotaPreferenceWriteResult(
            accepted=cast("bool", "yes"),
            outcome=StableSymbol("submitted"),
        )
    with pytest.raises(ValueError, match="etag"):
        replace(write, current_etag="")


@pytest.mark.parametrize(
    ("field_name", "value", "error", "message"),
    [
        ("child_id", "", ValueError, "child_id"),
        ("slice_identity", "slice", TypeError, "slice_identity"),
        ("target", "target", TypeError, "target"),
        ("preference_identity", "", ValueError, "preference_identity"),
        ("action", "create", TypeError, "action"),
        ("contact_value", "", ValueError, "contact value"),
        ("acknowledgements", [], TypeError, "acknowledgements"),
        ("acknowledgements", ("unsafe",), TypeError, "acknowledgements"),
    ],
)
def test_provider_write_rejects_cross_wired_mutation_intent(
    field_name: str,
    value: object,
    error: type[Exception],
    message: str,
) -> None:
    """Malformed mutation intent cannot cross the narrow provider seam."""
    plan = _single_plan()
    write = QuotaPreferenceWrite(
        child_id="single",
        slice_identity=plan.slice_identity,
        target=plan.target,
        preference_identity=cast("str", plan.preference_name),
        action=QuotaPreferenceWriteAction.AMEND,
        current_etag=plan.preference_etag,
        contact_value="resolved@example.com",
    )

    with pytest.raises(error, match=message):
        replace(
            write,
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_provider_write_result_requires_stable_outcome() -> None:
    """Provider classifications remain typed and stable."""
    with pytest.raises(TypeError, match="outcome"):
        QuotaPreferenceWriteResult(
            accepted=True,
            outcome=cast("StableSymbol", "submitted"),
        )


def _valid_revalidation(plan: QuotaPlan) -> ApplyRevalidation:
    return ApplyRevalidation(
        resource_scope=plan.resource_scope,
        principal=plan.principal,
        contact_binding=plan.contact_binding,
        contact_value="resolved@example.com",
        constraints=plan.constraints,
        children=tuple(_refreshed(child) for child in plan.children),
    )


def _single_plan() -> QuotaRequestPlan:
    child = _child(
        "single",
        "GPU-DIRECT",
        direct_rank=0,
        scope_rank=1,
        target=6,
    )
    return QuotaRequestPlan(
        resource_scope=SCOPE,
        slice_identity=child.slice_identity,
        target=child.target,
        effective=child.effective,
        effective_observed_at=NOW,
        preference_name=(
            f"{SCOPE.canonical_name}/locations/global/quotaPreferences/existing"
        ),
        preference_etag="current-etag",
        principal=PRINCIPAL,
        contact_binding=CONTACT,
        warnings=(),
        required_acknowledgements=(),
        acknowledgements=(),
        constraints=(ConstraintReference(child.slice_identity),),
        evidence=(),
        installation_id="installation-123",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
    )


def _apply(  # noqa: PLR0913
    plan: QuotaPlan,
    writer: _ScriptedWriter,
    *,
    records: _MemoryApplyRecords | None = None,
    repository: _MemoryPlanRepository | None = None,
    audit: _MemoryAuditJournal | None = None,
    revalidator: object | None = None,
    unknown_resolver: _ScriptedResolver | None = None,
    codec: object | None = None,
    request: ApplyRequest | None = None,
) -> tuple[
    OperationResult[ApplyData],
    _MemoryPlanRepository,
    _MemoryApplyRecords,
    _MemoryAuditJournal,
]:
    repository = repository or _MemoryPlanRepository(plan)
    apply_records = records or _MemoryApplyRecords()
    audit = audit or _MemoryAuditJournal()
    result = asyncio.run(
        ApplyPlanOperations(
            repository=cast("PlanRepository", repository),
            apply_records=cast("ApplyRecordRepository", apply_records),
            audit=cast("AuditJournal", audit),
            codec=cast("PlanCodecPort", codec or PlanCodec()),
            revalidator=cast(
                "ApplyRevalidator",
                revalidator or _ScriptedRevalidator(_valid_revalidation(plan)),
            ),
            writer=writer,
            unknown_resolver=unknown_resolver or _ScriptedResolver(),
        ).apply(
            request
            or ApplyRequest(
                digest=repository.encoded.digest,
                authentication_key=KEY,
                local_installation_id="installation-123",
                resource_scope_acknowledgement=SCOPE,
                principal=PRINCIPAL,
                contact_binding=CONTACT,
                contact_value="operator@example.com",
                now=NOW + timedelta(minutes=1),
            )
        )
    )
    return result, repository, apply_records, audit


def test_apply_dispatches_bound_order_once_and_durably_accepts_every_child() -> None:
    """The complete pre-intent and barrier precede exact ordered writes."""
    plan = _plan()
    writer = _ScriptedWriter(
        QuotaPreferenceWriteResult(accepted=True, outcome=StableSymbol("submitted")),
        QuotaPreferenceWriteResult(accepted=True, outcome=StableSymbol("submitted")),
    )

    result, repository, records, audit = _apply(plan, writer)

    assert result.boundary.reached
    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert repository.state is PlanLedgerState.CONSUMED
    assert records.record is not None
    assert records.record.state is ApplyRecordState.ACCEPTED
    assert tuple(request.child_id for request in writer.requests) == (
        "direct",
        "companion",
    )
    assert writer.requests[0].preference_identity.startswith(
        f"{SCOPE.canonical_name}/locations/global/quotaPreferences/cqmgr-"
    )
    assert all(request.current_etag is None for request in writer.requests)
    assert all(
        request.contact_value == "resolved@example.com" for request in writer.requests
    )
    assert [draft.kind for draft in audit.drafts] == [
        AuditRecordKind.APPLY_INTENT,
        AuditRecordKind.APPLY_INTENT,
        AuditRecordKind.APPLY_RESULT,
        AuditRecordKind.APPLY_INTENT,
        AuditRecordKind.APPLY_RESULT,
        AuditRecordKind.APPLY_RESULT,
    ]


def test_apply_revalidates_then_persists_preintent_before_consumption_barrier() -> None:
    """The complete bundle is fresh and durable before acquiring dispatch authority."""
    plan = _plan()
    events: list[str] = []

    class OrderedRevalidator(_ScriptedRevalidator):
        @override
        async def refresh(self, _plan: QuotaPlan, _now: datetime) -> ApplyRevalidation:
            events.append("revalidated")
            return await super().refresh(_plan, _now)

    class OrderedAudit(_MemoryAuditJournal):
        @override
        def append(self, draft: AuditRecordDraft, **kwargs: object) -> _AuditRecord:
            if draft.outcome == StableSymbol("pre-apply-intent"):
                events.append("preintent")
            return super().append(draft, **kwargs)

    class OrderedRepository(_MemoryPlanRepository):
        @override
        def acquire_lease(
            self, digest: str, _key: SecretValue, now: datetime
        ) -> PlanRepositoryOutcome:
            events.append("lease")
            return super().acquire_lease(digest, _key, now)

        @override
        def mark_dispatched(
            self, _lease: PlanLease, _key: SecretValue, _now: datetime
        ) -> PlanRepositoryOutcome:
            events.append("dispatched")
            return super().mark_dispatched(_lease, _key, _now)

    class OrderedWriter(_ScriptedWriter):
        @override
        async def dispatch(
            self, request: QuotaPreferenceWrite
        ) -> QuotaPreferenceWriteResult:
            events.append("provider")
            return await super().dispatch(request)

    repository = OrderedRepository(plan)
    writer = OrderedWriter(
        QuotaPreferenceWriteResult(
            accepted=True,
            outcome=StableSymbol("submitted"),
        ),
        QuotaPreferenceWriteResult(
            accepted=True,
            outcome=StableSymbol("submitted"),
        ),
    )
    _apply(
        plan,
        writer,
        repository=repository,
        audit=OrderedAudit(),
        revalidator=OrderedRevalidator(_valid_revalidation(plan)),
    )

    assert events[:5] == [
        "revalidated",
        "preintent",
        "lease",
        "dispatched",
        "provider",
    ]


def test_preintent_audit_failure_leaves_plan_available_without_dispatch() -> None:
    """Failure before the barrier returns typed evidence without leasing the plan."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    audit = _MemoryAuditJournal()
    audit.fail_on_append = 1
    writer = _ScriptedWriter()

    result, repository, records, _audit = _apply(
        plan,
        writer,
        repository=repository,
        audit=audit,
    )

    assert result.outcome.code == StableSymbol("apply-intent-audit-failed")
    assert repository.state is PlanLedgerState.AVAILABLE
    assert records.record is None
    assert writer.requests == []


def test_cancellation_during_preflight_propagates_without_leasing_or_dispatch() -> None:
    """Caller cancellation before durable intent remains an ordinary interruption."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    writer = _ScriptedWriter()

    with pytest.raises(asyncio.CancelledError):
        _apply(
            plan,
            writer,
            repository=repository,
            revalidator=_CancellingRevalidator(),
        )

    assert repository.state is PlanLedgerState.AVAILABLE
    assert writer.requests == []


def test_lease_conflict_after_preintent_never_crosses_provider_boundary() -> None:
    """An optimistic loser retains durable intent but cannot dispatch."""
    plan = _plan()

    class ConflictingRepository(_MemoryPlanRepository):
        @override
        def acquire_lease(
            self, digest: str, _key: SecretValue, now: datetime
        ) -> PlanRepositoryOutcome:
            del digest, _key, now
            return PlanRepositoryOutcome(
                PlanRepositoryStatus.CONFLICT,
                state=PlanLedgerState.AVAILABLE,
                authenticated=True,
            )

    repository = ConflictingRepository(plan)
    writer = _ScriptedWriter()

    result, repository, records, audit = _apply(
        plan,
        writer,
        repository=repository,
    )

    assert result.outcome.code == StableSymbol("plan-lease-failed")
    assert repository.state is PlanLedgerState.AVAILABLE
    assert records.record is not None
    assert [draft.outcome for draft in audit.drafts] == [
        StableSymbol("pre-apply-intent")
    ]
    assert writer.requests == []


def test_apply_rejects_unavailable_undecodable_and_request_drift_before_preintent() -> (
    None
):
    """Every preflight identity failure remains a terminal zero-write result."""
    plan = _plan()

    unavailable = _MemoryPlanRepository(plan)
    unavailable.load_outcome = PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
    missing, *_ = _apply(plan, _ScriptedWriter(), repository=unavailable)
    undecodable, *_ = _apply(plan, _ScriptedWriter(), codec=_FailingCodec())

    drift_repository = _MemoryPlanRepository(plan)
    drift_request = ApplyRequest(
        digest=drift_repository.encoded.digest,
        authentication_key=KEY,
        local_installation_id="foreign-installation",
        resource_scope_acknowledgement=SCOPE,
        principal=PRINCIPAL,
        contact_binding=CONTACT,
        contact_value="operator@example.com",
        now=NOW + timedelta(minutes=1),
    )
    drifted, *_ = _apply(
        plan,
        _ScriptedWriter(),
        repository=drift_repository,
        request=drift_request,
    )

    assert missing.outcome.code == StableSymbol("plan-unavailable")
    assert undecodable.outcome.code == StableSymbol("plan-unauthenticated")
    assert drifted.outcome.code == StableSymbol("plan-precondition-failed")


def test_failed_revalidation_classifies_lease_invalidation_and_audit_failures() -> None:
    """No-write invalidation reports each failed local durability boundary."""
    plan = _plan()

    lease_failure = _MemoryPlanRepository(plan)
    lease_failure.fail_acquire = True
    no_lease, *_ = _apply(
        plan,
        _ScriptedWriter(),
        repository=lease_failure,
        revalidator=_RaisingRevalidator(),
    )

    invalidation_failure = _MemoryPlanRepository(plan)
    invalidation_failure.fail_invalidate = True
    not_invalidated, *_ = _apply(
        plan,
        _ScriptedWriter(),
        repository=invalidation_failure,
        revalidator=_RaisingRevalidator(),
    )

    audit_failure = _MemoryAuditJournal()
    audit_failure.fail_on_append = 1
    no_audit, *_ = _apply(
        plan,
        _ScriptedWriter(),
        audit=audit_failure,
        revalidator=_RaisingRevalidator(),
    )

    assert no_lease.outcome.code == StableSymbol("plan-lease-failed")
    assert not_invalidated.outcome.code == StableSymbol("plan-invalidation-failed")
    assert no_audit.outcome.code == StableSymbol("plan-invalidation-audit-failed")


@pytest.mark.parametrize(
    ("writer_result", "failed_revision", "failed_append", "expected"),
    [
        (
            QuotaPreferenceWriteResult(
                accepted=False,
                outcome=StableSymbol("provider-rejected"),
            ),
            None,
            3,
            "critical-unknown",
        ),
        (
            QuotaPreferenceWriteResult(
                accepted=False,
                outcome=StableSymbol("provider-rejected"),
            ),
            3,
            None,
            "critical-unknown",
        ),
        (
            QuotaPreferenceWriteResult(
                accepted=True,
                outcome=StableSymbol("submitted"),
            ),
            3,
            None,
            "critical-unknown",
        ),
    ],
)
def test_terminal_child_evidence_failure_is_always_quarantined(
    writer_result: QuotaPreferenceWriteResult,
    failed_revision: int | None,
    failed_append: int | None,
    expected: str,
) -> None:
    """No accepted or failed dispatch escapes without durable terminal evidence."""
    plan = _single_plan()
    records = _MemoryApplyRecords()
    records.fail_on_revision = failed_revision
    audit = _MemoryAuditJournal()
    audit.fail_on_append = failed_append

    result, repository, _records, _audit = _apply(
        plan,
        _ScriptedWriter(writer_result),
        records=records,
        audit=audit,
    )

    assert result.outcome.code == StableSymbol(expected)
    assert repository.state is PlanLedgerState.QUARANTINED


@pytest.mark.parametrize(
    ("failed_revision", "failed_append"),
    [
        (2, None),
        (None, 3),
        (3, None),
    ],
)
def test_unknown_dispatch_persistence_failures_are_critical(
    failed_revision: int | None,
    failed_append: int | None,
) -> None:
    """Every unknown checkpoint is durable or escalates to critical unknown."""
    plan = _single_plan()
    records = _MemoryApplyRecords()
    records.fail_on_revision = failed_revision
    audit = _MemoryAuditJournal()
    audit.fail_on_append = failed_append

    result, repository, _records, _audit = _apply(
        plan,
        _ScriptedWriter(TimeoutError("transport lost")),
        records=records,
        audit=audit,
    )

    assert result.outcome.code == StableSymbol("critical-unknown")
    assert repository.state is PlanLedgerState.QUARANTINED


def test_unknown_reconciliation_read_and_append_failures_remain_quarantined() -> None:
    """Read loss stays unresolved and rejected append evidence becomes critical."""
    plan = _single_plan()
    unresolved, repository, *_ = _apply(
        plan,
        _ScriptedWriter(TimeoutError("transport lost")),
        unknown_resolver=cast("_ScriptedResolver", _RaisingResolver()),
    )

    records = _MemoryApplyRecords()
    records.fail_append_resolution = True
    rejected, rejected_repository, *_ = _apply(
        plan,
        _ScriptedWriter(TimeoutError("transport lost")),
        records=records,
        unknown_resolver=_ScriptedResolver(UnknownWriteResolution.ACCEPTED),
    )

    assert unresolved.outcome.code == StableSymbol("unknown-dispatch")
    assert repository.state is PlanLedgerState.QUARANTINED
    assert rejected.outcome.code == StableSymbol("critical-unknown")
    assert rejected_repository.state is PlanLedgerState.QUARANTINED


def test_child_preintent_quarantine_failure_is_critical_unknown() -> None:
    """A failed child preintent plus failed containment becomes critical unknown."""
    plan = _single_plan()
    repository = _MemoryPlanRepository(plan)
    repository.fail_quarantine = True
    audit = _MemoryAuditJournal()
    audit.fail_on_append = 2

    result, *_ = _apply(
        plan,
        _ScriptedWriter(),
        repository=repository,
        audit=audit,
    )

    assert result.outcome.code == StableSymbol("critical-unknown-uncontained")


def test_nonconflict_provider_failure_uses_operational_exit_class() -> None:
    """A provider rejection is distinct from an etag or identity conflict."""
    result, *_ = _apply(
        _single_plan(),
        _ScriptedWriter(
            QuotaPreferenceWriteResult(
                accepted=False,
                outcome=StableSymbol("provider-rejected"),
            )
        ),
    )

    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE


def test_unknown_first_child_is_never_retried_and_later_child_is_unattempted() -> None:
    """Transport ambiguity stops the bundle and reconciles the exact identity."""
    plan = _plan()
    writer = _ScriptedWriter(TimeoutError("scripted transport loss"))
    resolver = _ScriptedResolver(
        resolution=UnknownWriteResolution.ACCEPTED,
    )

    result, repository, records, _audit = _apply(
        plan,
        writer,
        unknown_resolver=resolver,
    )

    assert not result.boundary.reached
    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert repository.state is PlanLedgerState.QUARANTINED
    assert len(writer.requests) == 1
    assert records.record is not None
    assert tuple(child.disposition for child in records.record.children) == (
        ApplyChildDisposition.UNKNOWN,
        ApplyChildDisposition.UNATTEMPTED,
    )
    assert result.data.quarantine_identity == writer.requests[0].preference_identity
    assert resolver.requests == writer.requests
    assert records.record.children[0].unknown_resolution is None
    assert records.resolutions[0].resolution is UnknownDispatchResolution.ACCEPTED


def test_unknown_resolution_audit_failure_returns_contained_critical_unknown() -> None:
    """Resolution proof without its audit result cannot escape as an exception."""
    plan = _plan()
    audit = _MemoryAuditJournal()
    audit.fail_on_append = 4
    writer = _ScriptedWriter(TimeoutError("scripted transport loss"))
    resolver = _ScriptedResolver(
        resolution=UnknownWriteResolution.ACCEPTED,
    )

    result, repository, records, _audit = _apply(
        plan,
        writer,
        audit=audit,
        unknown_resolver=resolver,
    )

    assert result.outcome.code == StableSymbol("critical-unknown")
    assert repository.state is PlanLedgerState.QUARANTINED
    assert records.resolutions[0].resolution is UnknownDispatchResolution.ACCEPTED


def test_single_slice_amends_existing_identity_with_current_etag() -> None:
    """Single Apply preserves a revalidated semantic identity and etag."""
    plan = _single_plan()
    writer = _ScriptedWriter(
        QuotaPreferenceWriteResult(accepted=True, outcome=StableSymbol("submitted"))
    )

    result, _repository, records, _audit = _apply(plan, writer)

    assert result.boundary.reached
    assert records.record is not None
    assert records.record.kind is PlanKind.SINGLE
    assert len(writer.requests) == 1
    assert writer.requests[0].preference_identity == plan.preference_name
    assert writer.requests[0].current_etag == "current-etag"
    assert writer.requests[0].action.value == "amend"


def test_conclusive_failure_stops_without_rollback() -> None:
    """The first provider failure retains exact failure and prior outcomes."""
    plan = _plan()
    writer = _ScriptedWriter(
        QuotaPreferenceWriteResult(accepted=False, outcome=StableSymbol("conflicting"))
    )

    result, repository, records, _audit = _apply(plan, writer)

    assert result.outcome.exit_class is ExitClass.STALE_OR_CONFLICTING
    assert repository.state is PlanLedgerState.CONSUMED
    assert records.record is not None
    assert records.record.state is ApplyRecordState.FAILED
    assert tuple(child.disposition for child in records.record.children) == (
        ApplyChildDisposition.FAILED,
        ApplyChildDisposition.UNATTEMPTED,
    )
    assert len(writer.requests) == 1


def test_revalidation_exception_durably_invalidates_with_no_write_result() -> None:
    """Incomplete revalidation is terminal no-write evidence, never a loose lease."""
    plan = _plan()
    writer = _ScriptedWriter()

    result, repository, _records, audit = _apply(
        plan,
        writer,
        revalidator=_RaisingRevalidator(),
    )

    assert result.outcome.code == StableSymbol("plan-invalidated")
    assert repository.state is PlanLedgerState.INVALIDATED
    assert repository.invalidated_reason == StableSymbol("revalidation-incomplete")
    assert writer.requests == []
    assert [draft.kind for draft in audit.drafts] == [AuditRecordKind.APPLY_RESULT]


def _recovery_record(plan: QuotaPlan) -> ApplyRecord:
    return ApplyRecord(
        intent_id=PlanCodec.encode(plan, KEY.reveal()).digest,
        plan_digest=PlanCodec.encode(plan, KEY.reveal()).digest,
        kind=plan.kind,
        resource_scope=plan.resource_scope,
        created_at=NOW + timedelta(minutes=1),
        children=tuple(
            ApplyChildRecord(
                child_id=child.child_id,
                slice_identity=child.slice_identity,
                target=child.target,
                preference_identity=(
                    child.preference_name
                    or (
                        f"{SCOPE.canonical_name}/locations/global/"
                        f"quotaPreferences/recovery-{child.child_id}"
                    )
                ),
                etag=child.preference_etag,
            )
            for child in plan.children
        ),
    )


def test_recovery_rejects_missing_authority_and_invalid_authenticated_plan() -> None:
    """Prepared intent cannot resume without exact ledger and plan authority."""
    plan = _plan()
    unavailable_repository = _MemoryPlanRepository(plan)
    unavailable_records = _MemoryApplyRecords()
    unavailable_records.record = _recovery_record(plan)

    unavailable, *_ = _apply(
        plan,
        _ScriptedWriter(),
        records=unavailable_records,
        repository=unavailable_repository,
    )

    invalid_repository = _MemoryPlanRepository(plan)
    invalid_repository.acquire_lease(
        invalid_repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    invalid_repository.state = PlanLedgerState.DISPATCHED
    invalid_records = _MemoryApplyRecords()
    invalid_records.record = _recovery_record(plan)
    invalid, invalid_repository, *_ = _apply(
        plan,
        _ScriptedWriter(),
        records=invalid_records,
        repository=invalid_repository,
        codec=_FailingCodec(),
    )

    assert unavailable.outcome.code == StableSymbol("apply-recovery-unavailable")
    assert invalid.outcome.code == StableSymbol("critical-unknown")
    assert invalid_repository.state is PlanLedgerState.QUARANTINED


@pytest.mark.parametrize(
    ("ledger_state", "fail_save", "expected"),
    [
        (PlanLedgerState.LEASED, True, "critical-unknown"),
        (PlanLedgerState.DISPATCHED, False, "critical-unknown"),
    ],
)
def test_recovery_refresh_failure_never_reopens_dispatch(
    ledger_state: PlanLedgerState,
    fail_save: bool,  # noqa: FBT001
    expected: str,
) -> None:
    """Incomplete resumed preflight is invalidated or quarantined, never dispatched."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.acquire_lease(
        repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    repository.state = ledger_state
    records = _MemoryApplyRecords()
    records.record = _recovery_record(plan)
    records.fail_on_revision = 1 if fail_save else None

    result, repository, *_ = _apply(
        plan,
        _ScriptedWriter(),
        records=records,
        repository=repository,
        revalidator=_RaisingRevalidator(),
    )

    assert result.outcome.code == StableSymbol(expected)
    assert repository.state is PlanLedgerState.QUARANTINED


@pytest.mark.parametrize("fail_save", [False, True])
def test_prebarrier_recovery_drift_invalidates_or_escalates(
    fail_save: bool,  # noqa: FBT001
) -> None:
    """A drifted prepared intent cannot cross the consumption barrier."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.acquire_lease(
        repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    records = _MemoryApplyRecords()
    records.record = _recovery_record(plan)
    records.fail_on_revision = 1 if fail_save else None
    drifted = _valid_revalidation(plan)
    drifted = replace(
        drifted,
        children=(
            replace(drifted.children[0], effective=QuotaQuantity(5, UNIT)),
            drifted.children[1],
        ),
    )

    result, repository, *_ = _apply(
        plan,
        _ScriptedWriter(),
        records=records,
        repository=repository,
        revalidator=_ScriptedRevalidator(drifted),
    )

    assert result.outcome.code == StableSymbol(
        "critical-unknown" if fail_save else "plan-invalidated"
    )
    assert repository.state is (
        PlanLedgerState.QUARANTINED if fail_save else PlanLedgerState.INVALIDATED
    )


def test_prebarrier_recovery_consumption_conflict_stops_before_provider() -> None:
    """A failed recovered barrier cannot dispatch any prepared child."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.acquire_lease(
        repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    repository.fail_mark_dispatched = True
    records = _MemoryApplyRecords()
    records.record = _recovery_record(plan)
    writer = _ScriptedWriter()

    result, *_ = _apply(
        plan,
        writer,
        records=records,
        repository=repository,
    )

    assert result.outcome.code == StableSymbol("plan-consumption-failed")
    assert writer.requests == []


def test_recovery_returns_existing_terminal_child_without_redispatch() -> None:
    """A durable stopping disposition remains terminal on every recovery."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.acquire_lease(
        repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    repository.state = PlanLedgerState.DISPATCHED
    records = _MemoryApplyRecords()
    records.record = (
        _recovery_record(plan)
        .record_dispatch_intent("direct", NOW + timedelta(minutes=1))
        .record_outcome(
            "direct",
            ApplyChildDisposition.FAILED,
            StableSymbol("provider-rejected"),
            NOW + timedelta(minutes=1),
        )
        .finalize(NOW + timedelta(minutes=1))
    )
    writer = _ScriptedWriter()

    result, *_ = _apply(
        plan,
        writer,
        records=records,
        repository=repository,
    )

    assert result.outcome.code == StableSymbol("failed")
    assert writer.requests == []


def test_top_level_and_evidence_identity_drift_are_whole_bundle_failures() -> None:
    """Scope and evidence identity changes invalidate before durable preintent."""
    plan = _plan()
    other_scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/987654321")
    top_level = replace(_valid_revalidation(plan), resource_scope=other_scope)
    top_result, *_ = _apply(
        plan,
        _ScriptedWriter(),
        revalidator=_ScriptedRevalidator(top_level),
    )

    evidence = EvidenceBinding(
        StableSymbol("effective-quota"),
        "sha256:" + ("a" * 64),
        NOW,
    )
    evidence_plan = replace(
        plan,
        children=(
            replace(plan.children[0], evidence=(evidence,)),
            plan.children[1],
        ),
    )
    refreshed = _valid_revalidation(evidence_plan)
    refreshed = replace(
        refreshed,
        children=(
            replace(
                refreshed.children[0],
                evidence=(replace(evidence, value_digest="sha256:" + ("b" * 64)),),
            ),
            refreshed.children[1],
        ),
    )
    evidence_result, *_ = _apply(
        evidence_plan,
        _ScriptedWriter(),
        revalidator=_ScriptedRevalidator(refreshed),
    )

    assert top_result.outcome.code == StableSymbol("plan-invalidated")
    assert evidence_result.outcome.code == StableSymbol("plan-invalidated")


def test_recovery_marks_intent_without_outcome_unknown_without_redispatch() -> None:
    """A crashed child intent is reconciled and never sent a second time."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.acquire_lease(
        repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    repository.state = PlanLedgerState.DISPATCHED
    records = _MemoryApplyRecords()
    records.record = _recovery_record(plan).record_dispatch_intent(
        "direct", NOW + timedelta(minutes=1)
    )
    writer = _ScriptedWriter()
    resolver = _ScriptedResolver(UnknownWriteResolution.ACCEPTED)

    result, repository, records, _audit = _apply(
        plan,
        writer,
        records=records,
        repository=repository,
        unknown_resolver=resolver,
    )

    assert result.outcome.code == StableSymbol("unknown-dispatch")
    assert writer.requests == []
    assert len(resolver.requests) == 1
    assert repository.state is PlanLedgerState.QUARANTINED
    assert records.record is not None
    assert tuple(child.disposition for child in records.record.children) == (
        ApplyChildDisposition.UNKNOWN,
        ApplyChildDisposition.UNATTEMPTED,
    )


def test_recovery_resumes_only_next_child_after_durable_prior_acceptance() -> None:
    """A crash between children resumes the next child in the bound order."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.acquire_lease(
        repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    repository.state = PlanLedgerState.DISPATCHED
    records = _MemoryApplyRecords()
    records.record = (
        _recovery_record(plan)
        .record_dispatch_intent("direct", NOW + timedelta(minutes=1))
        .record_outcome(
            "direct",
            ApplyChildDisposition.ACCEPTED,
            StableSymbol("submitted"),
            NOW + timedelta(minutes=1),
        )
    )
    writer = _ScriptedWriter(
        QuotaPreferenceWriteResult(accepted=True, outcome=StableSymbol("submitted"))
    )

    result, repository, records, _audit = _apply(
        plan,
        writer,
        records=records,
        repository=repository,
    )

    assert result.boundary.reached
    assert tuple(request.child_id for request in writer.requests) == ("companion",)
    assert repository.state is PlanLedgerState.CONSUMED
    assert records.record is not None
    assert records.record.state is ApplyRecordState.ACCEPTED


def test_recovery_quarantines_whole_bundle_drift_before_next_child() -> None:
    """A restart never dispatches the next child from stale revalidation facts."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.acquire_lease(
        repository.encoded.digest,
        KEY,
        NOW + timedelta(minutes=1),
    )
    repository.state = PlanLedgerState.DISPATCHED
    records = _MemoryApplyRecords()
    records.record = (
        _recovery_record(plan)
        .record_dispatch_intent("direct", NOW + timedelta(minutes=1))
        .record_outcome(
            "direct",
            ApplyChildDisposition.ACCEPTED,
            StableSymbol("submitted"),
            NOW + timedelta(minutes=1),
        )
    )
    drifted = _valid_revalidation(plan)
    drifted = replace(
        drifted,
        children=(
            drifted.children[0],
            replace(
                drifted.children[1],
                effective=QuotaQuantity(5, UNIT),
            ),
        ),
    )
    writer = _FailFastWriter()

    result, repository, _records, _audit = _apply(
        plan,
        cast("_ScriptedWriter", writer),
        records=records,
        repository=repository,
        revalidator=_ScriptedRevalidator(drifted),
    )

    assert result.outcome.code == StableSymbol("critical-unknown")
    assert repository.state is PlanLedgerState.QUARANTINED
    assert writer.calls == 0


def test_every_pre_dispatch_persistence_failure_performs_zero_provider_writes() -> None:
    """Aggregate and child intent failures cannot cross the provider boundary."""
    plan = _plan()
    scenarios: list[
        tuple[
            _MemoryPlanRepository,
            _MemoryApplyRecords,
            _MemoryAuditJournal,
        ]
    ] = []

    aggregate_audit = _MemoryAuditJournal()
    aggregate_audit.fail_on_append = 1
    scenarios.append(
        (_MemoryPlanRepository(plan), _MemoryApplyRecords(), aggregate_audit)
    )

    create_records = _MemoryApplyRecords()
    create_records.fail_create = True
    scenarios.append(
        (_MemoryPlanRepository(plan), create_records, _MemoryAuditJournal())
    )

    barrier_repository = _MemoryPlanRepository(plan)
    barrier_repository.fail_mark_dispatched = True
    scenarios.append((barrier_repository, _MemoryApplyRecords(), _MemoryAuditJournal()))

    child_audit = _MemoryAuditJournal()
    child_audit.fail_on_append = 2
    scenarios.append((_MemoryPlanRepository(plan), _MemoryApplyRecords(), child_audit))

    child_records = _MemoryApplyRecords()
    child_records.fail_on_revision = 1
    scenarios.append(
        (_MemoryPlanRepository(plan), child_records, _MemoryAuditJournal())
    )

    for repository, records, audit in scenarios:
        writer = _ScriptedWriter()
        result, _repository, _records, _audit = _apply(
            plan,
            writer,
            records=records,
            repository=repository,
            audit=audit,
        )
        assert not result.boundary.reached
        assert writer.requests == []


def test_durable_preintent_recovers_after_consumption_barrier_failure() -> None:
    """A stored preintent can safely retry only the failed local barrier."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.fail_mark_dispatched = True
    records = _MemoryApplyRecords()
    first_writer = _ScriptedWriter()

    first, _repository, _records, _audit = _apply(
        plan,
        first_writer,
        records=records,
        repository=repository,
    )
    repository.fail_mark_dispatched = False
    retry_writer = _ScriptedWriter(
        *(
            QuotaPreferenceWriteResult(
                accepted=True,
                outcome=StableSymbol("submitted"),
            )
            for _child_value in plan.children
        )
    )
    retry, repository, records, _audit = _apply(
        plan,
        retry_writer,
        records=records,
        repository=repository,
    )

    assert first.outcome.code == StableSymbol("plan-consumption-failed")
    assert first_writer.requests == []
    assert retry.boundary.reached
    assert repository.state is PlanLedgerState.CONSUMED
    assert records.record is not None
    assert records.record.state is ApplyRecordState.ACCEPTED


def test_prebarrier_recovery_refresh_failure_is_terminal_no_write() -> None:
    """A prepared Apply invalidates when recovery cannot complete revalidation."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.fail_mark_dispatched = True
    records = _MemoryApplyRecords()
    first_writer = _ScriptedWriter()
    _apply(
        plan,
        first_writer,
        records=records,
        repository=repository,
    )
    repository.fail_mark_dispatched = False
    retry_writer = _FailFastWriter()

    result, repository, records, _audit = _apply(
        plan,
        cast("_ScriptedWriter", retry_writer),
        records=records,
        repository=repository,
        revalidator=_RaisingRevalidator(),
    )

    assert result.outcome.code == StableSymbol("plan-invalidated")
    assert repository.state is PlanLedgerState.INVALIDATED
    assert repository.invalidated_reason == StableSymbol("revalidation-incomplete")
    assert retry_writer.calls == 0
    assert records.record is not None
    assert records.record.state is ApplyRecordState.FAILED
    assert all(
        child.disposition is ApplyChildDisposition.UNATTEMPTED
        for child in records.record.children
    )


def test_aggregate_audit_or_completion_failure_after_writes_is_critical_unknown() -> (
    None
):
    """A missing aggregate terminal durability quarantines accepted writes."""
    plan = _single_plan()
    for fail_audit, fail_complete in ((True, False), (False, True)):
        repository = _MemoryPlanRepository(plan)
        repository.fail_complete = fail_complete
        audit = _MemoryAuditJournal()
        audit.fail_on_append = 4 if fail_audit else None
        writer = _ScriptedWriter(
            QuotaPreferenceWriteResult(accepted=True, outcome=StableSymbol("submitted"))
        )

        result, repository, _records, _audit = _apply(
            plan,
            writer,
            repository=repository,
            audit=audit,
        )

        assert result.outcome.code == StableSymbol("critical-unknown")
        assert repository.state is PlanLedgerState.QUARANTINED
        assert len(writer.requests) == 1


def test_quarantine_failure_is_reported_as_uncontained_critical_unknown() -> None:
    """A failed containment write cannot be reported as durable quarantine."""
    plan = _plan()
    repository = _MemoryPlanRepository(plan)
    repository.fail_quarantine = True
    writer = _ScriptedWriter(TimeoutError("scripted transport loss"))

    result, repository, _records, _audit = _apply(
        plan,
        writer,
        repository=repository,
    )

    assert result.outcome.code == StableSymbol("critical-unknown-uncontained")
    assert result.data.quarantine_identity is None
    assert repository.state is PlanLedgerState.DISPATCHED


def test_terminal_persistence_failure_returns_critical_unknown_and_quarantines() -> (
    None
):
    """A possible write without durable outcome is exit-nine critical unknown."""
    plan = _plan()
    records = _MemoryApplyRecords()
    records.fail_on_revision = 2
    writer = _ScriptedWriter(
        QuotaPreferenceWriteResult(accepted=True, outcome=StableSymbol("submitted"))
    )

    result, repository, records, _audit = _apply(plan, writer, records=records)

    assert not result.boundary.reached
    assert result.outcome.code == StableSymbol("critical-unknown")
    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert repository.state is PlanLedgerState.QUARANTINED
    assert len(writer.requests) == 1
    assert records.record is not None
    assert tuple(child.disposition for child in records.record.children) == (
        ApplyChildDisposition.UNKNOWN,
        ApplyChildDisposition.UNATTEMPTED,
    )
    assert result.data.quarantine_identity == writer.requests[0].preference_identity


def test_real_repository_recovers_exact_revision_after_terminal_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical fallback advances from the actual last durable revision."""
    plan = _plan()
    plan_repository = _MemoryPlanRepository(plan)
    records = LocalApplyRecordRepository(tmp_path)
    audit = _MemoryAuditJournal()
    writer = _ScriptedWriter(
        QuotaPreferenceWriteResult(
            accepted=True,
            outcome=StableSymbol("submitted"),
        )
    )
    original_publish = apply_record_persistence._publish  # noqa: SLF001
    replacements = 0
    terminal_replace_number = 2

    def fail_second_replace(path: Path, data: bytes, *, replace: bool) -> None:
        nonlocal replacements
        if replace:
            replacements += 1
            if replacements == terminal_replace_number:
                raise OSError
        original_publish(path, data, replace=replace)

    monkeypatch.setattr(
        apply_record_persistence,
        "_publish",
        fail_second_replace,
    )
    result = asyncio.run(
        ApplyPlanOperations(
            repository=cast("PlanRepository", plan_repository),
            apply_records=records,
            audit=cast("AuditJournal", audit),
            codec=cast("PlanCodecPort", PlanCodec()),
            revalidator=cast(
                "ApplyRevalidator",
                _ScriptedRevalidator(_valid_revalidation(plan)),
            ),
            writer=writer,
            unknown_resolver=_ScriptedResolver(),
        ).apply(
            ApplyRequest(
                digest=plan_repository.encoded.digest,
                authentication_key=KEY,
                local_installation_id="installation-123",
                resource_scope_acknowledgement=SCOPE,
                principal=PRINCIPAL,
                contact_binding=CONTACT,
                contact_value="untrusted@example.com",
                now=NOW + timedelta(minutes=1),
            )
        )
    )
    durable = records.load(plan_repository.encoded.digest, KEY)

    assert result.outcome.code == StableSymbol("critical-unknown")
    assert durable.record is not None
    assert durable.record.state is ApplyRecordState.CRITICAL_UNKNOWN
    assert tuple(child.disposition for child in durable.record.children) == (
        ApplyChildDisposition.UNKNOWN,
        ApplyChildDisposition.UNATTEMPTED,
    )
