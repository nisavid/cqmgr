"""Compose, Preview, and plan-review acceptance tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast, override

import pytest

from cqmgr.adapters.serialization.plans import PlanCodec
from cqmgr.application.operations.plans import (
    ComposeChild,
    ComposeRequest,
    PlanReviewRequest,
    PreviewRequest,
    RequestPlanOperations,
)
from cqmgr.application.ports.plans import (
    EncodedPlan,
    PlanRepositoryOutcome,
    PlanRepositoryStatus,
)
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.audit import AuditFactName
from cqmgr.domain.plans import (
    ContactBinding,
    EvidenceBinding,
    PlanIncapability,
    PlanKind,
    PlanLedgerState,
    PlanPrincipal,
    QuotaRequestBundlePlan,
    QuotaRequestPlan,
    TargetStrategy,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import ExitClass, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from pathlib import Path

    from cqmgr.application.ports.audit import AuditJournal
    from cqmgr.application.ports.plans import PlanCodec as PlanCodecPort
    from cqmgr.application.ports.plans import PlanRepository
    from cqmgr.domain.audit import AuditRecord, AuditRecordDraft

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
UNIT = QuotaUnit("1")


def _slice(
    quota_id: str,
    scope: QuotaScope = QuotaScope.REGIONAL,
) -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        resource_scope=SCOPE,
        service="compute.googleapis.com",
        quota_id=quota_id,
        dimensions=NormalizedDimensions((("region", "us-central1"),)),
        quota_scope=scope,
    )


@dataclass
class FailFastMutationPort:
    """A mutation-shaped sentinel that makes any write, including validation, fail."""

    calls: int = 0

    async def create_or_amend(self, *_args: object, **_kwargs: object) -> None:
        """Fail if Preview reaches a provider write operation."""
        self.calls += 1
        msg = "Preview reached a provider write"
        raise AssertionError(msg)

    async def validate_only(self, *_args: object, **_kwargs: object) -> None:
        """Fail if Preview reaches provider mutation-shaped validation."""
        self.calls += 1
        msg = "Preview reached provider validateOnly"
        raise AssertionError(msg)


class MemoryPlanRepository:
    """Hermetic plan repository double at the public storage port."""

    def __init__(self) -> None:
        """Initialize empty local storage state."""
        self.stored: EncodedPlan | None = None
        self.exported: tuple[EncodedPlan, Path] | None = None
        self.state = PlanLedgerState.AVAILABLE
        self.store_status = PlanRepositoryStatus.STORED
        self.export_status = PlanRepositoryStatus.EXPORTED
        self.load_any_digest = False
        self.load_outcome: PlanRepositoryOutcome | None = None

    def store(
        self,
        plan: EncodedPlan,
        _key: SecretValue,
    ) -> PlanRepositoryOutcome:
        """Retain exact encoded bytes by digest."""
        self.stored = plan
        return PlanRepositoryOutcome(
            self.store_status,
            plan_bytes=plan.bytes,
        )

    def export(self, plan: EncodedPlan, path: Path) -> PlanRepositoryOutcome:
        """Retain the exact export request."""
        self.exported = (plan, path)
        return PlanRepositoryOutcome(self.export_status)

    def load(
        self,
        digest: str,
        _key: SecretValue,
        _now: datetime,
    ) -> PlanRepositoryOutcome:
        """Load one retained digest and ledger state."""
        if self.load_outcome is not None:
            return self.load_outcome
        if self.stored is None or (
            not self.load_any_digest and self.stored.digest != digest
        ):
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        return PlanRepositoryOutcome(
            PlanRepositoryStatus(self.state.value),
            plan_bytes=self.stored.bytes,
            state=self.state,
            authenticated=True,
        )

    def read_export(self, path: Path) -> PlanRepositoryOutcome:
        """Read one retained explicit export."""
        if self.exported is None or self.exported[1] != path:
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.AVAILABLE,
            plan_bytes=self.exported[0].bytes,
            state=PlanLedgerState.AVAILABLE,
        )


@dataclass(frozen=True)
class _AuditRecord:
    record_id: str = "audit-00000000000000000001"


class MemoryAuditJournal:
    """Hermetic fsync-complete audit append double."""

    def __init__(self) -> None:
        """Initialize an empty append history."""
        self.drafts = []

    def append(
        self,
        draft: AuditRecordDraft,
        *,
        sensitive_values: tuple[str, ...] = (),
        machine_paths: tuple[str, ...] = (),
    ) -> AuditRecord:
        """Append one fsync-complete draft."""
        self.drafts.append(draft)
        del sensitive_values, machine_paths
        return cast("AuditRecord", _AuditRecord())


class FailingAuditJournal(MemoryAuditJournal):
    """Audit double that proves Preview fails closed on durability loss."""

    @override
    def append(
        self,
        draft: AuditRecordDraft,
        *,
        sensitive_values: tuple[str, ...] = (),
        machine_paths: tuple[str, ...] = (),
    ) -> AuditRecord:
        """Raise the persistence failure exposed by the port."""
        del draft, sensitive_values, machine_paths
        msg = "fsync failed"
        raise OSError(msg)


def _as_plan_repository(value: object) -> PlanRepository:
    """Treat a deliberately partial test double as the complete storage port."""
    return cast("PlanRepository", value)


def _as_audit_journal(value: object) -> AuditJournal:
    """Treat a deliberately partial test double as the complete audit port."""
    return cast("AuditJournal", value)


def _as_plan_codec(value: object) -> PlanCodecPort:
    """Bind the concrete codec through its application port."""
    return cast("PlanCodecPort", value)


def _operations(
    repository: MemoryPlanRepository,
    audit: MemoryAuditJournal,
) -> RequestPlanOperations:
    """Bind hermetic doubles through their public port contracts."""
    return RequestPlanOperations(
        repository=_as_plan_repository(repository),
        audit=_as_audit_journal(audit),
        codec=_as_plan_codec(PlanCodec()),
    )


def _preview_request(
    child: ComposeChild,
    *,
    plan_out: Path | None = None,
) -> PreviewRequest:
    """Build one complete bundle Preview request for failure-path tests."""
    return PreviewRequest(
        composition=ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.MINIMUM,
            resource_scope=SCOPE,
            children=(child,),
            selected_location="us-central1",
        ),
        principal=PlanPrincipal("principal://accounts/123"),
        contact_binding=ContactBinding(
            StableSymbol("direct-user"),
            "principal://accounts/123",
            "hmac-sha256:" + ("b" * 64),
        ),
        installation_id="installation-123",
        authentication_key=SecretValue(b"k" * 32),
        identity_verified=True,
        contact_verified=True,
        keyring_mutation_capable=True,
        normalized_workload="compute-instance:n1-standard-8:1",
        now=NOW,
        plan_out=plan_out,
    )


def _preview_child() -> ComposeChild:
    """Build one complete mutation child."""
    return ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )


def test_minimum_composition_orders_mutations_and_retains_settled_no_op() -> None:
    """Minimum composes each exact child and keeps no-ops outside dispatch order."""
    direct = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
    )
    companion = ComposeChild(
        child_id="companion",
        slice_identity=_slice("GPUS-ALL-REGIONS-per-project", QuotaScope.GLOBAL),
        effective=QuotaQuantity(8, UNIT),
        usage=QuotaQuantity(2, UNIT),
        workload=QuotaQuantity(4, UNIT),
        preferred=QuotaQuantity(6, UNIT),
        granted=QuotaQuantity(6, UNIT),
        preference_settled=True,
        direct_accelerator_rank=1,
        scope_breadth_rank=3,
    )

    composition = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.MINIMUM,
            resource_scope=SCOPE,
            children=(companion, direct),
            selected_location="us-central1",
        )
    )

    assert composition.reached
    assert [child.child_id for child in composition.children] == [
        "direct",
        "companion",
    ]
    assert composition.children[0].target == QuotaQuantity(7, UNIT)
    assert not composition.children[0].no_op
    assert composition.children[1].target == QuotaQuantity(6, UNIT)
    assert composition.children[1].no_op
    assert [child.child_id for child in composition.dispatch_children] == ["direct"]


def test_mixed_bundle_plan_review_retains_verified_no_op_composition() -> None:
    """Portable review keeps no-op derivation facts outside dispatch children."""
    direct = _preview_child()
    companion = ComposeChild(
        child_id="companion",
        slice_identity=_slice("GPUS-ALL-REGIONS-per-project", QuotaScope.GLOBAL),
        effective=QuotaQuantity(8, UNIT),
        usage=QuotaQuantity(2, UNIT),
        workload=QuotaQuantity(4, UNIT),
        preferred=QuotaQuantity(6, UNIT),
        granted=QuotaQuantity(6, UNIT),
        preference_settled=True,
        direct_accelerator_rank=1,
        scope_breadth_rank=3,
        observed_at=NOW,
    )
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    request = replace(
        _preview_request(direct),
        composition=ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.MINIMUM,
            resource_scope=SCOPE,
            children=(companion, direct),
            selected_location="us-central1",
        ),
    )

    preview = operations.preview(request)
    review = operations.review(
        PlanReviewRequest(
            digest=preview.data.plan_digest,
            path=None,
            authentication_key=request.authentication_key,
            local_installation_id=request.installation_id,
            now=NOW,
        )
    )

    assert isinstance(preview.data.plan, QuotaRequestBundlePlan)
    assert [child.child_id for child in preview.data.plan.children] == ["direct"]
    assert len(preview.data.plan.no_op_children) == 1
    no_op = preview.data.plan.no_op_children[0]
    assert no_op.child_id == "companion"
    assert no_op.target == QuotaQuantity(6, UNIT)
    assert no_op.target_strategy is TargetStrategy.MINIMUM
    assert no_op.target_derivation == StableSymbol("usage-plus-workload")
    assert no_op.prior_desired == QuotaQuantity(6, UNIT)
    assert review.data.review is not None
    assert isinstance(review.data.review.plan, QuotaRequestBundlePlan)
    assert review.data.review.plan.no_op_children == (no_op,)


def test_single_preview_and_review_preserve_every_revalidation_fact() -> None:
    """A single Plan binds the same current safety facts as a bundle child."""
    evidence = EvidenceBinding(
        StableSymbol("effective"),
        "sha256:" + ("a" * 64),
        NOW,
    )
    child = ComposeChild(
        child_id="single",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(7, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        preferred=QuotaQuantity(6, UNIT),
        granted=QuotaQuantity(5, UNIT),
        preference_name="projects/123456789/locations/us-central1/preferences/single",
        preference_etag="etag-single",
        manual_target=QuotaQuantity(8, UNIT),
        direct_accelerator_rank=1,
        scope_breadth_rank=3,
        warnings=("manual-review-required",),
        observed_at=NOW,
        evidence=(evidence,),
    )
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    request = replace(
        _preview_request(child),
        composition=ComposeRequest(
            kind=PlanKind.SINGLE,
            strategy=TargetStrategy.MANUAL,
            resource_scope=SCOPE,
            children=(child,),
        ),
        normalized_workload="exact-slice",
    )

    preview = operations.preview(request)
    review = operations.review(
        PlanReviewRequest(
            digest=preview.data.plan_digest,
            path=None,
            authentication_key=request.authentication_key,
            local_installation_id=request.installation_id,
            now=NOW,
        )
    )

    assert isinstance(preview.data.plan, QuotaRequestPlan)
    planned = preview.data.plan.children[0]
    assert (
        planned.child_id,
        planned.usage,
        planned.workload,
        planned.prior_desired,
        planned.granted,
        planned.preference_name,
        planned.preference_etag,
        planned.target_strategy,
        planned.target_derivation,
        planned.direct_accelerator_rank,
        planned.scope_breadth_rank,
        planned.warnings,
        planned.evidence,
    ) == (
        "single",
        QuotaQuantity(3, UNIT),
        QuotaQuantity(4, UNIT),
        QuotaQuantity(6, UNIT),
        QuotaQuantity(5, UNIT),
        child.preference_name,
        "etag-single",
        TargetStrategy.MANUAL,
        StableSymbol("manual-absolute"),
        1,
        3,
        (StableSymbol("manual-review-required"),),
        (evidence,),
    )
    assert review.data.review is not None
    assert review.data.review.plan == preview.data.plan


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"fresh": False}, "stale-evidence"),
        ({"complete": False}, "incomplete-evidence"),
        ({"ambiguous": True}, "ambiguous-evidence"),
        ({"mutable": False}, "unsupported-slice"),
        ({"ongoing_rollout": True}, "ongoing-rollout"),
    ],
)
def test_composition_rejects_unsafe_child_before_any_mutation(
    change: dict[str, Any], reason: str
) -> None:
    """Every fail-closed preflight gate remains a zero-provider-write path."""
    sentinel = FailFastMutationPort()
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        **change,
    )

    composition = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.MINIMUM,
            resource_scope=SCOPE,
            children=(child,),
            selected_location="us-central1",
        )
    )

    assert not composition.reached
    assert composition.incapability_reasons == (reason,)
    assert sentinel.calls == 0


def test_manual_decrease_requires_expert_named_acknowledgements() -> None:
    """Dangerous decreases bind exact acknowledgement codes before Preview."""
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(10, UNIT),
        usage=QuotaQuantity(8, UNIT),
        workload=None,
        manual_target=QuotaQuantity(7, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
    )
    request = ComposeRequest(
        kind=PlanKind.SINGLE,
        strategy=TargetStrategy.MANUAL,
        resource_scope=SCOPE,
        children=(child,),
    )

    rejected = RequestPlanOperations.compose(request)
    accepted = RequestPlanOperations.compose(
        ComposeRequest(
            kind=request.kind,
            strategy=request.strategy,
            resource_scope=request.resource_scope,
            children=request.children,
            expert=True,
            acknowledgements=(
                "decrease-below-usage",
                "decrease-over-ten-percent",
            ),
        )
    )

    assert rejected.incapability_reasons == (
        "expert-path-required",
        "missing-acknowledgement:decrease-below-usage",
        "missing-acknowledgement:decrease-over-ten-percent",
    )
    assert accepted.reached
    assert accepted.children[0].required_acknowledgements == (
        "decrease-below-usage",
        "decrease-over-ten-percent",
    )


def test_unknown_acknowledgement_is_usage_error_before_audit_or_storage() -> None:
    """Unknown acknowledgement input cannot cross any durable write seam."""
    repository = MemoryPlanRepository()
    audit = MemoryAuditJournal()
    operations = _operations(repository, audit)
    child = _preview_child()
    request = _preview_request(child)
    request = replace(
        request,
        composition=replace(
            request.composition,
            acknowledgements=("decrease-a-little-bit",),
        ),
    )

    result = operations.preview(request)

    assert not result.boundary.reached
    assert result.outcome.exit_class is ExitClass.USAGE
    assert result.data.composition.incapability_reasons == (
        "unknown-acknowledgement:decrease-a-little-bit",
    )
    assert repository.stored is None
    assert repository.exported is None
    assert audit.drafts == []


def test_known_unneeded_acknowledgements_are_harmless() -> None:
    """The acknowledgement allowlist validates spelling, not necessity."""
    child = _preview_child()

    result = RequestPlanOperations.compose(
        replace(
            _preview_request(child).composition,
            acknowledgements=(
                "decrease-below-usage",
                "decrease-over-ten-percent",
                "unlimited-transition",
            ),
        )
    )

    assert result.reached
    assert result.children[0].required_acknowledgements == ()


def test_invalid_plan_kind_rejects_before_audit_or_storage() -> None:
    """A cast or otherwise unvalidated plan kind fails closed."""
    repository = MemoryPlanRepository()
    audit = MemoryAuditJournal()
    operations = _operations(repository, audit)
    child = _preview_child()
    request = _preview_request(child)
    request = replace(
        request,
        composition=replace(
            request.composition,
            kind=cast("PlanKind", "collection"),
        ),
    )

    result = operations.preview(request)

    assert not result.boundary.reached
    assert result.outcome.exit_class is ExitClass.USAGE
    assert result.data.composition.incapability_reasons == ("unsupported-plan-kind",)
    assert repository.stored is None
    assert repository.exported is None
    assert audit.drafts == []


@pytest.mark.parametrize(
    ("preview_change", "reason"),
    [
        ({"identity_verified": False}, "identity-unverified"),
        ({"contact_verified": False}, "contact-unverified"),
        ({"keyring_mutation_capable": False}, "keyring-incapable"),
    ],
)
def test_preview_preconditions_do_not_block_target_composition(
    preview_change: dict[str, Any], reason: str
) -> None:
    """Preview-only trust gates leave target derivation available."""
    repository = MemoryPlanRepository()
    audit = MemoryAuditJournal()
    operations = _operations(repository, audit)
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )
    composition = ComposeRequest(
        kind=PlanKind.BUNDLE,
        strategy=TargetStrategy.MINIMUM,
        resource_scope=SCOPE,
        children=(child,),
        selected_location="us-central1",
    )

    composed = RequestPlanOperations.compose(composition)
    preview = operations.preview(
        replace(
            _preview_request(child),
            composition=composition,
            **preview_change,
        )
    )

    assert composed.reached
    assert preview.data.composition.incapability_reasons == (reason,)
    assert repository.stored is None
    assert audit.drafts == []


def test_preview_issues_one_authenticated_bundle_handle_and_atomic_export(
    tmp_path: Path,
) -> None:
    """Preview audits first, stores one digest plan, and exports the same bytes."""
    repository = MemoryPlanRepository()
    audit = MemoryAuditJournal()
    sentinel = FailFastMutationPort()
    operations = _operations(repository, audit)
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
        evidence=(
            EvidenceBinding(
                StableSymbol("effective"),
                "sha256:" + ("a" * 64),
                NOW,
            ),
        ),
    )
    plan_out = tmp_path / "request.plan"

    result = operations.preview(
        PreviewRequest(
            composition=ComposeRequest(
                kind=PlanKind.BUNDLE,
                strategy=TargetStrategy.MINIMUM,
                resource_scope=SCOPE,
                children=(child,),
                selected_location="us-central1",
            ),
            principal=PlanPrincipal("principal://accounts/123"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/123",
                "hmac-sha256:" + ("b" * 64),
            ),
            installation_id="installation-123",
            authentication_key=SecretValue(b"k" * 32),
            identity_verified=True,
            contact_verified=True,
            keyring_mutation_capable=True,
            normalized_workload="compute-instance:n1-standard-8:1",
            now=NOW,
            plan_out=plan_out,
        )
    )

    assert result.boundary.reached
    assert result.data.plan_digest is not None
    assert result.data.plan_digest.startswith("sha256:")
    assert result.data.plan is not None
    assert result.data.plan.expires_at == NOW + timedelta(minutes=15)
    assert repository.stored is not None
    assert repository.exported == (repository.stored, plan_out)
    assert len(audit.drafts) == 1
    fact_names = tuple(fact.name for fact in audit.drafts[0].facts)
    assert fact_names == (
        AuditFactName.PLAN_SUBJECT,
        AuditFactName.TARGET_STRATEGY,
        AuditFactName.PLAN_DIGEST,
        AuditFactName.PLAN_CHILD,
    )
    assert audit.drafts[0].facts[-1].value.value.endswith("|applicable")
    assert sentinel.calls == 0


def test_all_no_op_preview_is_audited_without_plan_or_export(tmp_path: Path) -> None:
    """A wholly settled bundle reaches Preview but creates no Apply capability."""
    repository = MemoryPlanRepository()
    audit = MemoryAuditJournal()
    operations = _operations(repository, audit)
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(8, UNIT),
        usage=QuotaQuantity(2, UNIT),
        workload=QuotaQuantity(4, UNIT),
        preferred=QuotaQuantity(6, UNIT),
        granted=QuotaQuantity(6, UNIT),
        preference_settled=True,
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )

    result = operations.preview(
        PreviewRequest(
            composition=ComposeRequest(
                kind=PlanKind.BUNDLE,
                strategy=TargetStrategy.MINIMUM,
                resource_scope=SCOPE,
                children=(child,),
                selected_location="us-central1",
            ),
            principal=PlanPrincipal("principal://accounts/123"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/123",
                "hmac-sha256:" + ("b" * 64),
            ),
            installation_id="installation-123",
            authentication_key=SecretValue(b"k" * 32),
            identity_verified=True,
            contact_verified=True,
            keyring_mutation_capable=True,
            normalized_workload="compute-instance:n1-standard-8:1",
            now=NOW,
            plan_out=tmp_path / "must-not-exist.plan",
        )
    )

    assert result.boundary.reached
    assert result.data.plan is None
    assert not result.data.apply_capability
    assert repository.stored is None
    assert repository.exported is None
    assert len(audit.drafts) == 1
    assert audit.drafts[0].facts[-1].value.value.endswith("|no-op")


def test_minimum_sufficient_child_is_no_op_without_decrease_acknowledgements() -> None:
    """Minimum never turns an already sufficient slice into a decrease."""
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(10, UNIT),
        usage=QuotaQuantity(2, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )

    composition = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.MINIMUM,
            resource_scope=SCOPE,
            children=(child,),
            selected_location="us-central1",
        )
    )
    preview = operations.preview(_preview_request(child))

    assert composition.reached
    assert composition.children[0].no_op
    assert composition.children[0].required_acknowledgements == ()
    assert preview.boundary.reached
    assert preview.outcome.code.value == "verified-no-op"
    assert not preview.data.apply_capability
    assert repository.stored is None


def test_single_manual_preview_and_review_use_common_child_contract() -> None:
    """An exact-slice manual plan is portable and reviewable as one child."""
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    child = ComposeChild(
        child_id="exact-slice",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=None,
        manual_target=QuotaQuantity(8, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
        warnings=("remaining-companion-bottleneck",),
    )
    request = _preview_request(child)
    request = replace(
        request,
        composition=replace(
            request.composition,
            kind=PlanKind.SINGLE,
            strategy=TargetStrategy.MANUAL,
            selected_location=None,
        ),
        normalized_workload="exact-slice-manual",
    )

    preview = operations.preview(request)
    assert preview.boundary.reached
    assert preview.data.plan is not None
    assert preview.data.plan.kind is PlanKind.SINGLE
    assert len(preview.data.plan.children) == 1
    assert preview.data.plan.children[0].target_strategy is TargetStrategy.MANUAL
    assert (
        preview.data.plan.children[0].warnings[0].value
        == "remaining-companion-bottleneck"
    )
    review = operations.review(
        PlanReviewRequest(
            digest=preview.data.plan_digest,
            path=None,
            authentication_key=request.authentication_key,
            local_installation_id=request.installation_id,
            now=NOW,
        )
    )
    assert review.boundary.reached
    assert review.data.review is not None
    assert review.data.review.apply_capability


def test_preserve_headroom_and_unlimited_transition_remain_explicit() -> None:
    """Every non-default target strategy retains its own safety semantics."""
    child = _preview_child()
    preserved = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.PRESERVE_HEADROOM,
            resource_scope=SCOPE,
            children=(child,),
            selected_location="us-central1",
        )
    )
    unlimited = replace(
        child,
        workload=None,
        manual_target=QuotaQuantity(-1, UNIT),
    )
    rejected = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.SINGLE,
            strategy=TargetStrategy.MANUAL,
            resource_scope=SCOPE,
            children=(unlimited,),
        )
    )
    accepted = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.SINGLE,
            strategy=TargetStrategy.MANUAL,
            resource_scope=SCOPE,
            children=(unlimited,),
            expert=True,
            acknowledgements=("unlimited-transition",),
        )
    )

    assert preserved.children[0].target == QuotaQuantity(8, UNIT)
    assert rejected.incapability_reasons == (
        "expert-path-required",
        "missing-acknowledgement:unlimited-transition",
    )
    assert accepted.children[0].required_acknowledgements == ("unlimited-transition",)


def test_plan_review_preserves_foreign_expired_and_consumed_evidence(
    tmp_path: Path,
) -> None:
    """Trustworthy review succeeds while exact applicability reasons accumulate."""
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )
    plan_out = tmp_path / "portable.plan"
    preview = operations.preview(
        PreviewRequest(
            composition=ComposeRequest(
                kind=PlanKind.BUNDLE,
                strategy=TargetStrategy.MINIMUM,
                resource_scope=SCOPE,
                children=(child,),
                selected_location="us-central1",
            ),
            principal=PlanPrincipal("principal://accounts/123"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/123",
                "hmac-sha256:" + ("b" * 64),
            ),
            installation_id="installation-a",
            authentication_key=SecretValue(b"k" * 32),
            identity_verified=True,
            contact_verified=True,
            keyring_mutation_capable=True,
            normalized_workload="compute-instance:n1-standard-8:1",
            now=NOW,
            plan_out=plan_out,
        )
    )
    repository.state = PlanLedgerState.CONSUMED

    consumed = operations.review(
        PlanReviewRequest(
            digest=preview.data.plan_digest,
            path=None,
            authentication_key=SecretValue(b"k" * 32),
            local_installation_id="installation-a",
            now=NOW,
        )
    )
    consumed_export = operations.review(
        PlanReviewRequest(
            digest=None,
            path=plan_out,
            authentication_key=SecretValue(b"k" * 32),
            local_installation_id="installation-a",
            now=NOW,
        )
    )
    foreign_expired = operations.review(
        PlanReviewRequest(
            digest=None,
            path=plan_out,
            authentication_key=SecretValue(b"f" * 32),
            local_installation_id="installation-b",
            now=NOW + timedelta(minutes=15),
        )
    )

    assert consumed.boundary.reached
    assert consumed.data.review is not None
    assert consumed.data.review.incapability_reasons == (PlanIncapability.CONSUMED,)
    assert consumed_export.boundary.reached
    assert consumed_export.data.review is not None
    assert consumed_export.data.review.incapability_reasons == (
        PlanIncapability.CONSUMED,
    )
    assert foreign_expired.boundary.reached
    assert foreign_expired.data.review is not None
    assert foreign_expired.data.review.incapability_reasons == (
        PlanIncapability.EXPIRED,
        PlanIncapability.FOREIGN_OR_UNAUTHENTICATED,
        PlanIncapability.INSTALLATION_MISMATCH,
    )


@pytest.mark.parametrize(
    "local_outcome",
    [
        pytest.param(
            PlanRepositoryOutcome(PlanRepositoryStatus.MISSING),
            id="missing",
        ),
        pytest.param(
            PlanRepositoryOutcome(
                PlanRepositoryStatus.FAILED,
                state=PlanLedgerState.AVAILABLE,
                reason=StableSymbol("ledger-corrupt"),
                authenticated=True,
            ),
            id="corrupt",
        ),
        pytest.param(
            PlanRepositoryOutcome(
                PlanRepositoryStatus.AVAILABLE,
                authenticated=True,
            ),
            id="no-state",
        ),
    ],
)
def test_export_review_fails_closed_when_local_authority_is_unavailable(
    tmp_path: Path,
    local_outcome: PlanRepositoryOutcome,
) -> None:
    """An export is not Apply-capable without its local single-use authority."""
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    plan_out = tmp_path / "portable.plan"
    preview = operations.preview(
        _preview_request(_preview_child(), plan_out=plan_out)
    )
    repository.load_outcome = local_outcome

    result = operations.review(
        PlanReviewRequest(
            digest=None,
            path=plan_out,
            authentication_key=SecretValue(b"k" * 32),
            local_installation_id="installation-123",
            now=NOW,
        )
    )

    assert preview.data.plan_digest is not None
    assert result.boundary.reached
    assert result.data.review is not None
    assert not result.data.review.apply_capability
    assert result.data.review.incapability_reasons == (
        PlanIncapability.LOCAL_AUTHORITY_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    ("child_change", "strategy", "reason"),
    [
        ({"usage": None}, TargetStrategy.MINIMUM, "usage-evidence-missing"),
        (
            {"preferred": QuotaQuantity(5, UNIT)},
            TargetStrategy.MINIMUM,
            "lower-conflicting-intent",
        ),
        (
            {"scope_breadth_rank": 9},
            TargetStrategy.MINIMUM,
            "unsupported-scope-breadth-rank",
        ),
        ({"manual_target": None}, TargetStrategy.MANUAL, "manual-target-required"),
    ],
)
def test_target_and_ordering_failures_never_issue_a_plan(
    child_change: dict[str, Any],
    strategy: TargetStrategy,
    reason: str,
) -> None:
    """Usage, intent, ranking, and target failures stop before plan storage."""
    sentinel = FailFastMutationPort()
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    child = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        manual_target=QuotaQuantity(8, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )
    child = replace(child, **child_change)
    kind = PlanKind.SINGLE if strategy is TargetStrategy.MANUAL else PlanKind.BUNDLE
    result = operations.preview(
        PreviewRequest(
            composition=ComposeRequest(
                kind=kind,
                strategy=strategy,
                resource_scope=SCOPE,
                children=(child,),
                selected_location=(None if kind is PlanKind.SINGLE else "us-central1"),
            ),
            principal=PlanPrincipal("principal://accounts/123"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/123",
                "hmac-sha256:" + ("b" * 64),
            ),
            installation_id="installation-123",
            authentication_key=SecretValue(b"k" * 32),
            identity_verified=True,
            contact_verified=True,
            keyring_mutation_capable=True,
            normalized_workload="compute-instance:n1-standard-8:1",
            now=NOW,
        )
    )

    assert not result.boundary.reached
    assert reason in result.data.composition.incapability_reasons
    assert repository.stored is None
    assert sentinel.calls == 0


def test_unknown_target_strategy_and_later_stale_child_preflight_every_child() -> None:
    """No earlier valid child can cause issuance before all children pass."""
    repository = MemoryPlanRepository()
    sentinel = FailFastMutationPort()
    valid = ComposeChild(
        child_id="direct",
        slice_identity=_slice("GPUS-PER-GPU-FAMILY-per-project-region"),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(4, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )
    stale = replace(
        valid,
        child_id="companion",
        slice_identity=_slice(
            "GPUS-ALL-REGIONS-per-project",
            QuotaScope.GLOBAL,
        ),
        direct_accelerator_rank=1,
        scope_breadth_rank=3,
        fresh=False,
    )

    unknown = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=cast("TargetStrategy", "automatic"),
            resource_scope=SCOPE,
            children=(valid,),
            selected_location="us-central1",
        )
    )
    rejected = _operations(repository, MemoryAuditJournal())
    result = rejected.preview(
        PreviewRequest(
            composition=ComposeRequest(
                kind=PlanKind.BUNDLE,
                strategy=TargetStrategy.MINIMUM,
                resource_scope=SCOPE,
                children=(valid, stale),
                selected_location="us-central1",
            ),
            principal=PlanPrincipal("principal://accounts/123"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/123",
                "hmac-sha256:" + ("b" * 64),
            ),
            installation_id="installation-123",
            authentication_key=SecretValue(b"k" * 32),
            identity_verified=True,
            contact_verified=True,
            keyring_mutation_capable=True,
            normalized_workload="compute-instance:n1-standard-8:1",
            now=NOW,
        )
    )

    assert unknown.incapability_reasons == ("unsupported-target-strategy",)
    assert "stale-evidence" in result.data.composition.incapability_reasons
    assert repository.stored is None
    assert sentinel.calls == 0


def test_preview_local_failure_matrix_remains_no_write_and_incapable(
    tmp_path: Path,
) -> None:
    """Every local durability and integrity failure blocks Apply capability."""
    child = _preview_child()
    repository = MemoryPlanRepository()
    codec = _as_plan_codec(PlanCodec())
    cases: list[tuple[RequestPlanOperations, PreviewRequest, str]] = [
        (
            RequestPlanOperations(
                repository=_as_plan_repository(repository),
                audit=_as_audit_journal(MemoryAuditJournal()),
                codec=codec,
            ),
            _preview_request(replace(child, observed_at=None)),
            "preview-rejected",
        ),
        (
            RequestPlanOperations(
                repository=_as_plan_repository(repository),
                audit=None,
                codec=codec,
            ),
            _preview_request(child),
            "audit-unavailable",
        ),
        (
            RequestPlanOperations(
                repository=_as_plan_repository(repository),
                audit=_as_audit_journal(FailingAuditJournal()),
                codec=codec,
            ),
            _preview_request(child),
            "preview-audit-failed",
        ),
        (
            RequestPlanOperations(
                repository=None,
                audit=_as_audit_journal(MemoryAuditJournal()),
                codec=codec,
            ),
            _preview_request(child),
            "plan-repository-unavailable",
        ),
        (
            RequestPlanOperations(
                repository=_as_plan_repository(repository),
                audit=_as_audit_journal(MemoryAuditJournal()),
                codec=None,
            ),
            _preview_request(child),
            "plan-codec-unavailable",
        ),
    ]
    for operations, request, outcome in cases:
        result = operations.preview(request)
        assert not result.boundary.reached
        assert result.outcome.code.value == outcome
        assert not result.data.apply_capability

    store_failure = MemoryPlanRepository()
    store_failure.store_status = PlanRepositoryStatus.FAILED
    failed_store = _operations(store_failure, MemoryAuditJournal()).preview(
        _preview_request(child)
    )
    export_failure = MemoryPlanRepository()
    export_failure.export_status = PlanRepositoryStatus.FAILED
    failed_export = _operations(export_failure, MemoryAuditJournal()).preview(
        _preview_request(child, plan_out=tmp_path / "request.plan")
    )

    assert failed_store.outcome.code.value == "plan-store-failed"
    assert failed_export.outcome.code.value == "plan-export-failed"
    assert not failed_store.data.apply_capability
    assert not failed_export.data.apply_capability


def test_plan_review_rejects_invalid_selectors_bytes_and_digest() -> None:
    """Untrusted bytes never reach the trustworthy Review boundary."""
    repository = MemoryPlanRepository()
    operations = _operations(repository, MemoryAuditJournal())
    preview = operations.preview(_preview_request(_preview_child()))
    assert preview.data.plan_digest is not None
    digest = preview.data.plan_digest
    common = {
        "authentication_key": SecretValue(b"k" * 32),
        "local_installation_id": "installation-123",
        "now": NOW,
    }

    no_repository = RequestPlanOperations().review(
        PlanReviewRequest(digest=digest, path=None, **common)
    )
    invalid_selector = operations.review(
        PlanReviewRequest(digest=digest, path=cast("Path", object()), **common)
    )
    missing_key = operations.review(
        PlanReviewRequest(
            digest=digest,
            path=None,
            authentication_key=None,
            local_installation_id="installation-123",
            now=NOW,
        )
    )
    missing = operations.review(
        PlanReviewRequest(
            digest="sha256:" + ("f" * 64),
            path=None,
            **common,
        )
    )
    valid_encoded = repository.stored
    assert valid_encoded is not None
    repository.stored = EncodedPlan(b"not-json\n", valid_encoded.digest)
    invalid_bytes = operations.review(
        PlanReviewRequest(digest=valid_encoded.digest, path=None, **common)
    )
    repository.stored = valid_encoded
    repository.load_any_digest = True
    mismatched = operations.review(
        PlanReviewRequest(
            digest="sha256:" + ("e" * 64),
            path=None,
            **common,
        )
    )
    no_codec = RequestPlanOperations(
        repository=_as_plan_repository(repository),
        audit=None,
        codec=None,
    ).review(PlanReviewRequest(digest=digest, path=None, **common))

    assert no_repository.outcome.code.value == "plan-repository-unavailable"
    assert invalid_selector.outcome.code.value == "plan-selector-invalid"
    assert missing_key.outcome.code.value == "plan-authentication-key-missing"
    assert missing.outcome.code.value == "plan-not-readable"
    assert invalid_bytes.outcome.code.value == "plan-integrity-invalid"
    assert mismatched.outcome.code.value == "plan-digest-mismatch"
    assert no_codec.outcome.code.value == "plan-codec-unavailable"
