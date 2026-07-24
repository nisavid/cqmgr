"""Async protected request preparation shared by CLI and TUI."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cqmgr.application.operations.lifecycle_requests import (
    LifecycleCompositionEvidence,
    LifecycleCompositionIntent,
    LifecycleRequestOperations,
    ReadOnlyLifecycleCompositionReader,
)
from cqmgr.application.operations.plans import ComposeChild
from cqmgr.application.operations.quotas import QuotaInspectData
from cqmgr.application.operations.read_only import (
    QuotaInspectSelector,
    ReadOnlyScopeInput,
)
from cqmgr.application.operations.trust import LoadedInstallationTrust
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.accelerator_overlay import (
    CandidateLocations,
    ComputeInstanceRequirement,
    ProvisioningModel,
    QuotaConstraintAssessment,
    QuotaConstraintRequirement,
    ResolvedWorkloadLocation,
    ResolvedWorkloadRequirement,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import (
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogPredicates,
    ManagementPlane,
    UnitConversionEvidence,
    WorkloadConsumer,
)
from cqmgr.domain.identity import (
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
    ProviderIdentityEvidence,
)
from cqmgr.domain.plans import PlanKind, PlanPrincipal, TargetStrategy
from cqmgr.domain.quota_queries import QuotaQueryItem
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaContainerType,
    QuotaIncreaseEligibility,
    QuotaIneligibilityReason,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 24, 15, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
UNIT = QuotaUnit("1")
KEY = SecretValue(b"k" * 32)
DEADLINE = 100.0


class _Trust:
    def __init__(self) -> None:
        self.loads = 0

    def load(self) -> LoadedInstallationTrust:
        self.loads += 1
        return LoadedInstallationTrust(
            "installation-test",
            KEY,
            keyring_mutation_capable=True,
        )


class _Reader:
    def __init__(self, child: ComposeChild) -> None:
        self.child = child
        self.intents: list[LifecycleCompositionIntent] = []

    async def read(
        self,
        intent: LifecycleCompositionIntent,
        *,
        deadline: float,
    ) -> LifecycleCompositionEvidence:
        assert deadline == DEADLINE
        self.intents.append(intent)
        return LifecycleCompositionEvidence(
            kind=PlanKind.SINGLE,
            resource_scope=SCOPE,
            children=(self.child,),
            selected_location=None,
            principal=PlanPrincipal("principal://accounts/123"),
            identity_verified=True,
            normalized_workload="exact-slice",
        )


class _ReadOnly:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[QuotaInspectSelector, float, ReadOnlyScopeInput]] = []

    async def inspect(
        self,
        selector: QuotaInspectSelector,
        *,
        deadline: float,
        scope_input: ReadOnlyScopeInput,
    ) -> object:
        self.calls.append((selector, deadline, scope_input))
        return self.result


class _WorkloadReadOnly:
    def __init__(
        self,
        resolved: ResolvedWorkloadRequirement,
        results: dict[str, object],
    ) -> None:
        self.resolved = resolved
        self.results = results
        self.resolve_calls: list[
            tuple[ComputeInstanceRequirement, float, ReadOnlyScopeInput]
        ] = []
        self.inspect_calls: list[
            tuple[QuotaInspectSelector, float, ReadOnlyScopeInput]
        ] = []

    async def resolve(
        self,
        workload: ComputeInstanceRequirement,
        *,
        deadline: float,
        scope_input: ReadOnlyScopeInput,
    ) -> object:
        self.resolve_calls.append((workload, deadline, scope_input))
        return SimpleNamespace(
            succeeded=True,
            data=self.resolved,
            outcome=SimpleNamespace(code=StableSymbol("workload-resolved")),
        )

    async def inspect(
        self,
        selector: QuotaInspectSelector,
        *,
        deadline: float,
        scope_input: ReadOnlyScopeInput,
    ) -> object:
        self.inspect_calls.append((selector, deadline, scope_input))
        return self.results[selector.quota_id]


def _child() -> ComposeChild:
    return ComposeChild(
        child_id="single",
        slice_identity=EffectiveQuotaSliceIdentity(
            SCOPE,
            "compute.googleapis.com",
            "GPU-DIRECT",
            NormalizedDimensions((("region", "us-central1"),)),
            QuotaScope.REGIONAL,
        ),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(2, UNIT),
        workload=None,
        manual_target=QuotaQuantity(8, UNIT),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        observed_at=NOW,
    )


def _inspect_result(
    identity: EffectiveQuotaSliceIdentity,
    *,
    effective_value: int,
    usage_value: int,
) -> object:
    effective = EffectiveQuotaEvidence(
        identity=identity,
        effective_value=QuotaQuantity(effective_value, UNIT),
        metric=f"{identity.service}/{identity.quota_id}",
        declared_dimensions=tuple(key for key, _value in identity.dimensions.items),
        applicable_locations=("us-central1",),
        eligibility=QuotaIncreaseEligibility(
            eligible=True,
            reason=ProviderSymbol("OTHER", QuotaIneligibilityReason),
        ),
        fixed=False,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
    )
    item = QuotaQueryItem(
        identity=identity,
        display_name=None,
        accelerator_id=None,
        location="us-central1",
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=True,
            guided=True,
            mutable=True,
        ),
        effective_value=effective.effective_value,
        usage_value=QuotaQuantity(usage_value, UNIT),
        evidence_observed_at=NOW,
    )
    return SimpleNamespace(
        succeeded=True,
        data=QuotaInspectData(identity, effective, item, None, None, None, None),
        identity_evidence=ProviderIdentityEvidence(
            CredentialKind.SERVICE_ACCOUNT,
            PrincipalVerification.VERIFIED,
            PrincipalIdentity("principal://accounts/123"),
        ),
        outcome=SimpleNamespace(code=StableSymbol("exact-slice-inspected")),
    )


def test_prepare_preserves_expert_and_builds_protected_preview() -> None:
    """Async evidence and active trust produce one complete Preview request."""
    trust = _Trust()
    reader = _Reader(_child())
    operations = LifecycleRequestOperations(
        reader,
        trust,
        now=lambda: NOW,
    )
    intent = LifecycleCompositionIntent(
        scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        selector=QuotaInspectSelector(
            "compute.googleapis.com",
            "GPU-DIRECT",
            "us-central1",
            NormalizedDimensions((("region", "us-central1"),)),
        ),
        workload=None,
        target_strategy=TargetStrategy.MANUAL,
        targets=((None, "8"),),
        acknowledgements=("decrease-over-ten-percent",),
        expert=True,
        quota_contact=SecretValue(b"operator@example.com"),
        plan_out=Path("request.plan"),
    )

    prepared = asyncio.run(
        operations.prepare(intent, deadline=DEADLINE, require_preview=True)
    )

    assert prepared.composition.expert is True
    assert prepared.composition.acknowledgements == ("decrease-over-ten-percent",)
    assert prepared.preview is not None
    assert prepared.preview.composition is prepared.composition
    assert prepared.preview.installation_id == "installation-test"
    assert prepared.preview.authentication_key.reveal() == KEY.reveal()
    assert prepared.preview.plan_out == Path("request.plan")
    assert prepared.preview.contact_binding.source.value == "per-operation-input"
    assert prepared.preview.contact_binding.source_identity.startswith(
        "input:hmac-sha256:"
    )
    assert "operator@example.com" not in repr(prepared)
    assert trust.loads == 1
    assert reader.intents == [intent]


def test_preview_requires_active_trust_before_provider_reads() -> None:
    """Missing installation authority stops Preview before fresh provider access."""

    class _MissingTrust:
        def load(self) -> LoadedInstallationTrust:
            message = "installation trust is missing"
            raise RuntimeError(message)

    reader = _Reader(_child())
    operations = LifecycleRequestOperations(
        reader,
        _MissingTrust(),
        now=lambda: NOW,
    )
    intent = LifecycleCompositionIntent(
        scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        selector=QuotaInspectSelector(
            "compute.googleapis.com",
            "GPU-DIRECT",
            "us-central1",
        ),
        workload=None,
        target_strategy=TargetStrategy.MANUAL,
        targets=((None, "8"),),
        quota_contact=SecretValue(b"operator@example.com"),
    )

    with pytest.raises(RuntimeError, match="trust is missing"):
        asyncio.run(
            operations.prepare(
                intent,
                deadline=DEADLINE,
                require_preview=True,
            )
        )

    assert reader.intents == []


def test_composition_evidence_requires_verified_stable_principal() -> None:
    """Verified evidence cannot omit its stable acting principal."""
    with pytest.raises(ValueError, match="principal"):
        LifecycleCompositionEvidence(
            kind=PlanKind.SINGLE,
            resource_scope=SCOPE,
            children=(_child(),),
            selected_location=None,
            principal=None,
            identity_verified=True,
            normalized_workload="exact-slice",
        )

    with pytest.raises(ValueError, match="stable"):
        PrincipalIdentity("operator@example.com")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("scope_input", object(), TypeError),
        ("selector", None, ValueError),
        ("target_strategy", object(), TypeError),
        ("targets", [], TypeError),
        ("acknowledgements", ("",), ValueError),
        ("expert", 1, TypeError),
        ("quota_contact", "operator@example.com", TypeError),
        ("plan_out", "request.plan", TypeError),
    ],
)
def test_composition_intent_rejects_untyped_or_ambiguous_input(
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    """Surface-neutral intent rejects shapes that could diverge by adapter."""
    values: dict[str, Any] = {
        "scope_input": ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        "selector": QuotaInspectSelector(
            "compute.googleapis.com",
            "GPU-DIRECT",
            "us-central1",
        ),
        "workload": None,
        "target_strategy": TargetStrategy.MANUAL,
        "targets": ((None, "8"),),
    }
    values[field] = value

    with pytest.raises(error):
        LifecycleCompositionIntent(**values)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("kind", object(), TypeError),
        ("resource_scope", object(), TypeError),
        ("children", [object()], TypeError),
        ("principal", object(), TypeError),
        ("identity_verified", 1, TypeError),
        ("normalized_workload", "", ValueError),
    ],
)
def test_composition_evidence_rejects_untyped_or_incomplete_input(
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    """Fresh evidence remains typed before either surface can compose a Plan."""
    values: dict[str, Any] = {
        "kind": PlanKind.SINGLE,
        "resource_scope": SCOPE,
        "children": (_child(),),
        "selected_location": None,
        "principal": PlanPrincipal("principal://accounts/123"),
        "identity_verified": True,
        "normalized_workload": "exact-slice",
    }
    values[field] = value

    with pytest.raises(error):
        LifecycleCompositionEvidence(**values)


def test_prepare_compose_only_and_missing_contact_never_invent_preview() -> None:
    """Compose is trust-independent and Preview never invents contact input."""
    trust = _Trust()
    reader = _Reader(_child())
    operations = LifecycleRequestOperations(reader, trust, now=lambda: NOW)
    intent = LifecycleCompositionIntent(
        scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        selector=QuotaInspectSelector(
            "compute.googleapis.com",
            "GPU-DIRECT",
            "us-central1",
        ),
        workload=None,
        target_strategy=TargetStrategy.MANUAL,
        targets=((None, "8"),),
    )

    compose_only = asyncio.run(
        operations.prepare(intent, deadline=DEADLINE, require_preview=False)
    )
    missing_contact = asyncio.run(
        operations.prepare(intent, deadline=DEADLINE, require_preview=True)
    )

    assert compose_only.preview is None
    assert missing_contact.preview is None
    assert trust.loads == 1


def test_read_only_reader_converts_exact_inspect_without_write_capability() -> None:
    """Exact inspection becomes one fresh mutable child and stable principal."""
    child = _child()
    identity = child.slice_identity
    effective = EffectiveQuotaEvidence(
        identity=identity,
        effective_value=child.effective,
        metric="compute.googleapis.com/GPU-DIRECT",
        declared_dimensions=("region",),
        applicable_locations=("us-central1",),
        eligibility=QuotaIncreaseEligibility(
            eligible=True,
            reason=ProviderSymbol("OTHER", QuotaIneligibilityReason),
        ),
        fixed=False,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
    )
    item = QuotaQueryItem(
        identity=identity,
        display_name=None,
        accelerator_id=None,
        location="us-central1",
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=False,
            guided=False,
            mutable=True,
        ),
        effective_value=child.effective,
        usage_value=child.usage,
        evidence_observed_at=NOW,
    )
    principal = PrincipalIdentity("principal://accounts/123")
    data = QuotaInspectData(
        identity,
        effective,
        item,
        None,
        None,
        None,
        None,
    )
    result = SimpleNamespace(
        succeeded=True,
        data=data,
        identity_evidence=ProviderIdentityEvidence(
            CredentialKind.SERVICE_ACCOUNT,
            PrincipalVerification.VERIFIED,
            principal,
        ),
        outcome=SimpleNamespace(code=StableSymbol("exact-slice-inspected")),
    )
    read_only = _ReadOnly(result)
    reader = ReadOnlyLifecycleCompositionReader(read_only)
    intent = LifecycleCompositionIntent(
        scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        selector=QuotaInspectSelector(
            identity.service,
            identity.quota_id,
            "us-central1",
            identity.dimensions,
        ),
        workload=None,
        target_strategy=TargetStrategy.MANUAL,
        targets=((None, "8"),),
    )

    prepared = asyncio.run(reader.read(intent, deadline=DEADLINE))

    assert prepared.kind is PlanKind.SINGLE
    assert prepared.principal == PlanPrincipal(principal.value)
    assert prepared.identity_verified
    assert prepared.children[0].manual_target == QuotaQuantity(8, UNIT)
    assert prepared.children[0].mutable
    assert prepared.children[0].observed_at == NOW
    assert read_only.calls == [(intent.selector, DEADLINE, intent.scope_input)]


def test_read_only_reader_refreshes_every_workload_constraint() -> None:
    """A resolved bundle is composed only from fresh exact child inspections."""
    direct_identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        "GPU-DIRECT",
        NormalizedDimensions(
            (("gpu_family", "NVIDIA_H100"), ("region", "us-central1"))
        ),
        QuotaScope.REGIONAL,
    )
    companion_identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        "GPU-COMPANION",
        NormalizedDimensions((("region", "us-central1"),)),
        QuotaScope.REGIONAL,
    )
    accelerator_id = AcceleratorId("nvidia-h100")
    conversion = UnitConversionEvidence(
        "card",
        UNIT,
        1,
        "https://cloud.google.com/compute/docs/resource-usage",
    )
    requirements = tuple(
        QuotaConstraintRequirement(
            identity,
            required,
            QuotaQuantity(required, UNIT),
            conversion,
        )
        for identity, required in (
            (direct_identity, 8),
            (companion_identity, 2),
        )
    )
    assessments = tuple(
        QuotaConstraintAssessment(
            requirement.identity,
            QuotaQuantity(effective, UNIT),
            QuotaQuantity(usage, UNIT),
            requirement.required,
            permits,
        )
        for requirement, effective, usage, permits in (
            (requirements[0], 12, 1, True),
            (requirements[1], 4, 0, True),
        )
    )
    workload = ComputeInstanceRequirement(
        "a3-highgpu-8g",
        1,
        ProvisioningModel.STANDARD,
        CandidateLocations(("us-central1-a",)),
        "nvidia-h100-80gb",
        8,
    )
    location = ResolvedWorkloadLocation(
        "us-central1-a",
        WorkloadLocationDisposition.COMPATIBLE,
        accelerator_id,
        "compute.googleapis.com",
        ManagementPlane.COMPUTE,
        (WorkloadConsumer.COMPUTE_ENGINE,),
        "standard",
        8,
        AcceleratorConstraintSet(
            accelerator_id,
            (
                ConstraintReference(direct_identity),
                ConstraintReference(companion_identity),
            ),
        ),
        requirements,
        (),
        assessments,
        attached_accelerator_type="nvidia-h100-80gb",
        attached_accelerator_count=8,
    )
    resolved = ResolvedWorkloadRequirement(workload, (location,), None)
    read_only = _WorkloadReadOnly(
        resolved,
        {
            direct_identity.quota_id: _inspect_result(
                direct_identity,
                effective_value=12,
                usage_value=1,
            ),
            companion_identity.quota_id: _inspect_result(
                companion_identity,
                effective_value=4,
                usage_value=0,
            ),
        },
    )
    reader = ReadOnlyLifecycleCompositionReader(read_only)
    intent = LifecycleCompositionIntent(
        scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        selector=None,
        workload=workload,
        target_strategy=TargetStrategy.MANUAL,
        targets=(("direct", "16"), ("companion", "4")),
    )

    evidence = asyncio.run(reader.read(intent, deadline=DEADLINE))

    assert evidence.kind is PlanKind.BUNDLE
    assert evidence.selected_location == "us-central1-a"
    assert evidence.principal == PlanPrincipal("principal://accounts/123")
    assert tuple(child.child_id for child in evidence.children) == (
        "direct",
        "companion",
    )
    assert tuple(child.workload for child in evidence.children) == (
        QuotaQuantity(8, UNIT),
        QuotaQuantity(2, UNIT),
    )
    assert tuple(child.manual_target for child in evidence.children) == (
        QuotaQuantity(16, UNIT),
        QuotaQuantity(4, UNIT),
    )
    assert read_only.resolve_calls == [(workload, DEADLINE, intent.scope_input)]
    assert tuple(call[0].quota_id for call in read_only.inspect_calls) == (
        direct_identity.quota_id,
        companion_identity.quota_id,
    )
