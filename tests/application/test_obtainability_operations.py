"""Application contract for read-only Spot obtainability comparison."""

from __future__ import annotations

# ruff: noqa: FBT003, PLR2004
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.application.operations.obtainability import (
    AdviceSupport,
    ObtainabilityCandidateEligibility,
    ObtainabilityCompareRequest,
    ObtainabilityEligibility,
    ObtainabilityOperations,
    PreparedObtainabilityComparison,
    candidates_from_resolved_workload,
    eligibility_from_resolved_workload,
    prepare_obtainability_comparison,
)
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.application.ports.obtainability import (
    CapacityAdviceReader,
    CapacityHistoryReader,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
    QuotaConstraintRequirement,
    ResolutionFailureReason,
    ResolvedWorkloadLocation,
    ResolvedWorkloadRequirement,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import (
    AcceleratorConstraintSet,
    AcceleratorId,
    ManagementPlane,
    UnitConversionEvidence,
    WorkloadConsumer,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.identity import ADCIdentityEvidence, CredentialKind
from cqmgr.domain.obtainability import (
    CapacityAdvice,
    CapacityHistory,
    DistributionShape,
    GpuAttachment,
    ObtainabilityCandidate,
    ObtainabilityProductCoverage,
    PreemptionInterval,
    PriceInterval,
    SpotMachineConfiguration,
    UnrankedReason,
)
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    ProviderRead,
    ProviderReadCoverage,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import ExitClass
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from cqmgr.application.ports.obtainability import (
        CapacityAdviceReadRequest,
        CapacityHistoryReadRequest,
    )

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


class MissingAdviceReader(CapacityAdviceReader):
    """Exercise the current-advice port's fail-closed inherited default."""


class MissingHistoryReader(CapacityHistoryReader):
    """Exercise the history port's fail-closed inherited default."""


def _invalid_advice_request() -> CapacityAdviceReadRequest:
    return cast("CapacityAdviceReadRequest", object())


def _invalid_history_request() -> CapacityHistoryReadRequest:
    return cast("CapacityHistoryReadRequest", object())


def test_obtainability_reader_protocol_defaults_fail_closed() -> None:
    """Explicit implementations cannot silently inherit ellipsis results."""
    with pytest.raises(NotImplementedError):
        asyncio.run(MissingAdviceReader().read(_invalid_advice_request()))
    with pytest.raises(NotImplementedError):
        asyncio.run(MissingHistoryReader().read(_invalid_history_request()))


def _context() -> ProviderReadContext:
    return ProviderReadContext(
        CanonicalProject(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
            "public-schema-project",
            "Public schema project",
        ),
        ADCIdentityEvidence(
            CredentialKind.SERVICE_ACCOUNT,
            acting_principal=None,
            stable_principal=None,
        ),
        100.0,
        CancellationToken(),
    )


class ScriptedReader[ValueT]:
    """Return scripted normalized provider reads and record exact requests."""

    def __init__(self, reads: list[ProviderRead[ValueT]]) -> None:
        """Initialize scripted values and an exact request ledger."""
        self.reads = reads
        self.requests: list[object] = []

    async def read(self, request: object) -> ProviderRead[ValueT]:
        """Record and return the next scripted read."""
        self.requests.append(request)
        return self.reads.pop(0)


def _history(machine_type: str, price: str) -> CapacityHistory:
    rates = tuple(
        PreemptionInterval(
            NOW - timedelta(days=31 - day),
            NOW - timedelta(days=30 - day),
            Decimal(day) / Decimal(100),
        )
        for day in range(1, 31)
    )
    return CapacityHistory(
        machine_type,
        "us-central1",
        rates,
        (
            PriceInterval(
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                Decimal(price),
            ),
        ),
        NOW,
    )


def _read[ValueT](value: ValueT) -> ProviderRead[ValueT]:
    return ProviderRead((value,), ProviderReadCoverage(1, 1), NOW)


def _resolved_location(location: str) -> ResolvedWorkloadLocation:
    region = location.rsplit("-", maxsplit=1)[0]
    identity = EffectiveQuotaSliceIdentity(
        ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
        "compute.googleapis.com",
        "GPUS-PER-GPU-FAMILY-per-project-region",
        NormalizedDimensions((("gpu_family", "NVIDIA_H100"), ("region", region))),
        QuotaScope.REGIONAL,
    )
    conversion = UnitConversionEvidence(
        source_unit="card",
        quota_unit=QuotaUnit("1"),
        quota_units_per_source=1,
        source_reference="https://docs.cloud.google.com/compute/resource-usage",
    )
    constraint_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-h100"),
        (ConstraintReference(identity),),
    )
    return ResolvedWorkloadLocation(
        location=location,
        disposition=WorkloadLocationDisposition.COMPATIBLE,
        accelerator_id=AcceleratorId("nvidia-h100"),
        owning_service="compute.googleapis.com",
        management_plane=ManagementPlane.COMPUTE,
        supported_consumers=(WorkloadConsumer.COMPUTE_ENGINE,),
        quota_pool="preemptible",
        deployable_accelerator_quantity=8,
        constraint_set=constraint_set,
        constraint_requirements=(
            QuotaConstraintRequirement(
                identity,
                8,
                QuotaQuantity(8, QuotaUnit("1")),
                conversion,
            ),
        ),
        coverage=(),
    )


def _resolved_candidates(
    candidates: tuple[ObtainabilityCandidate, ...],
) -> ResolvedWorkloadRequirement:
    """Bind one fixed candidate set to compatible Spot Compute evidence."""
    first = candidates[0]
    locations = tuple(
        dict.fromkeys(
            location
            for candidate in candidates
            for location in candidate.zones or (candidate.endpoint_region,)
        )
    )
    requirement = ComputeInstanceRequirement(
        first.machine.machine_type,
        first.vm_count,
        ProvisioningModel.SPOT,
        CandidateLocations(locations),
    )
    return ResolvedWorkloadRequirement(
        requirement,
        tuple(_resolved_location(location) for location in locations),
        None,
    )


def _request(
    candidates: tuple[ObtainabilityCandidate, ...],
    *,
    resolved: ResolvedWorkloadRequirement | None = None,
) -> ObtainabilityCompareRequest:
    """Build the mandatory resolver-bound application request."""
    return ObtainabilityCompareRequest(
        _context(),
        candidates,
        resolver_provenance=resolved or _resolved_candidates(candidates),
    )


def test_all_compatible_expansion_keeps_resolver_provenance_and_exact_zones() -> None:
    """Only resolver-proven compatible Compute locations become exact candidates."""
    requirement = ComputeInstanceRequirement(
        machine_type="a3-highgpu-8g",
        instance_count=2,
        provisioning_model=ProvisioningModel.SPOT,
        locations=AllCompatibleLocations(),
    )
    resolved = ResolvedWorkloadRequirement(
        requirement,
        (_resolved_location("us-central1-a"), _resolved_location("us-east1-b")),
        all_compatible_locations_exhaustive=True,
    )

    candidates = candidates_from_resolved_workload(
        resolved,
        machine=SpotMachineConfiguration("a3-highgpu-8g"),
        distribution_shape=DistributionShape.ANY_SINGLE_ZONE,
    )

    assert tuple(item.endpoint_region for item in candidates) == (
        "us-central1",
        "us-east1",
    )
    assert tuple(item.zones for item in candidates) == (
        ("us-central1-a",),
        ("us-east1-b",),
    )
    assert all(item.vm_count == 2 for item in candidates)
    request = _request(candidates, resolved=resolved)

    assert request.resolver_provenance is resolved


def test_resolver_eligibility_gates_advice_and_retains_exact_coverage() -> None:
    """Only cataloged Spot Compute evidence may authorize provider advice."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        2,
        DistributionShape.BALANCED,
    )
    requirement = ComputeInstanceRequirement(
        candidate.machine.machine_type,
        candidate.vm_count,
        ProvisioningModel.SPOT,
        CandidateLocations((candidate.endpoint_region,)),
    )
    eligible = ResolvedWorkloadRequirement(
        requirement,
        (_resolved_location(candidate.endpoint_region),),
        None,
    )

    eligibility = eligibility_from_resolved_workload(eligible, (candidate,))

    assert eligibility == ObtainabilityEligibility(
        AdviceSupport(),
        (
            ObtainabilityProductCoverage(
                "a3-highgpu-8g",
                "compute.googleapis.com",
                True,
                True,
                True,
            ),
        ),
        (ObtainabilityCandidateEligibility(candidate.candidate_id, True),),
    )

    non_compute = replace(
        eligible,
        locations=(
            replace(
                eligible.locations[0],
                management_plane=ManagementPlane.TPU,
                owning_service="tpu.googleapis.com",
            ),
        ),
    )
    ineligible = eligibility_from_resolved_workload(non_compute, (candidate,))

    assert ineligible.support == AdviceSupport()
    assert ineligible.catalog_coverage == (
        ObtainabilityProductCoverage(
            "a3-highgpu-8g",
            "compute.googleapis.com",
            False,
            False,
            False,
            ("configuration-not-cataloged-for-spot-compute",),
        ),
    )
    assert ineligible.candidates == (
        ObtainabilityCandidateEligibility(
            candidate.candidate_id,
            False,
            (UnrankedReason.NON_COMPUTE_MANAGEMENT_PLANE,),
        ),
    )


def test_unproven_attachments_are_not_resolver_eligible() -> None:
    """Resolver inputs that omit attachments cannot authorize exact advice."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration(
            "n1-standard-8",
            GpuAttachment("nvidia-tesla-t4", 1),
        ),
        1,
        DistributionShape.ANY,
    )
    requirement = ComputeInstanceRequirement(
        candidate.machine.machine_type,
        candidate.vm_count,
        ProvisioningModel.SPOT,
        CandidateLocations((candidate.endpoint_region,)),
    )
    resolved = ResolvedWorkloadRequirement(
        requirement,
        (_resolved_location(candidate.endpoint_region),),
        None,
    )

    eligibility = eligibility_from_resolved_workload(resolved, (candidate,))

    assert eligibility.support == AdviceSupport(True, False)
    assert eligibility.catalog_coverage == (
        ObtainabilityProductCoverage(
            "n1-standard-8+nvidia-tesla-t4x1",
            "compute.googleapis.com",
            False,
            False,
            False,
            ("configuration-not-cataloged-for-spot-compute",),
        ),
    )
    assert eligibility.candidates == (
        ObtainabilityCandidateEligibility(
            candidate.candidate_id,
            False,
            (UnrankedReason.CATALOG_UNSUPPORTED,),
        ),
    )

    local_ssd = replace(
        candidate,
        machine=SpotMachineConfiguration("n1-standard-8", local_ssd_count=1),
    )
    local_ssd_eligibility = eligibility_from_resolved_workload(
        resolved,
        (local_ssd,),
    )

    assert local_ssd_eligibility.candidates[0].reasons == (
        UnrankedReason.CATALOG_UNSUPPORTED,
    )


def test_prepared_comparison_rejects_unbound_or_untyped_values() -> None:
    """Prepared execution binds exact candidates, eligibility, and provenance."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        2,
        DistributionShape.BALANCED,
    )
    requirement = ComputeInstanceRequirement(
        candidate.machine.machine_type,
        candidate.vm_count,
        ProvisioningModel.SPOT,
        CandidateLocations((candidate.endpoint_region,)),
    )
    resolved = ResolvedWorkloadRequirement(
        requirement,
        (_resolved_location(candidate.endpoint_region),),
        None,
    )
    prepared = prepare_obtainability_comparison(resolved, (candidate,))

    with pytest.raises(ValueError, match="requires exact candidates"):
        PreparedObtainabilityComparison((), prepared.eligibility, resolved)
    with pytest.raises(TypeError, match="eligibility must be typed"):
        PreparedObtainabilityComparison(
            (candidate,),
            cast("ObtainabilityEligibility", "bad"),
            resolved,
        )
    with pytest.raises(TypeError, match="provenance must be typed"):
        PreparedObtainabilityComparison(
            (candidate,),
            prepared.eligibility,
            cast("ResolvedWorkloadRequirement", "bad"),
        )
    mismatched = replace(candidate, endpoint_region="us-east1", zones=())
    with pytest.raises(ValueError, match="bind candidates in order"):
        PreparedObtainabilityComparison(
            (mismatched,),
            prepared.eligibility,
            resolved,
        )
    fabricated = replace(
        prepared.eligibility,
        support=AdviceSupport(False, False),
    )
    with pytest.raises(ValueError, match="match resolver evidence"):
        PreparedObtainabilityComparison(
            (candidate,),
            fabricated,
            resolved,
        )


def test_resolver_eligibility_rejects_unbound_request_shapes() -> None:
    """Eligibility is defined only for one exact matching Compute request."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        2,
        DistributionShape.BALANCED,
    )
    requirement = ComputeInstanceRequirement(
        candidate.machine.machine_type,
        candidate.vm_count,
        ProvisioningModel.SPOT,
        CandidateLocations((candidate.endpoint_region,)),
    )
    resolved = ResolvedWorkloadRequirement(
        requirement,
        (_resolved_location(candidate.endpoint_region),),
        None,
    )
    tpu = CloudTpuSliceRequirement(
        "v6e-8",
        "2x4",
        "tpu-vm-base",
        1,
        ProvisioningModel.SPOT,
        AllCompatibleLocations(),
    )

    with pytest.raises(TypeError, match="compute-instance resolution"):
        eligibility_from_resolved_workload(
            ResolvedWorkloadRequirement(tpu, (), True),
            (candidate,),
        )
    with pytest.raises(ValueError, match="requires exact candidates"):
        eligibility_from_resolved_workload(resolved, ())
    with pytest.raises(ValueError, match="one fixed request shape"):
        eligibility_from_resolved_workload(
            resolved,
            (candidate, replace(candidate, vm_count=3)),
        )
    with pytest.raises(ValueError, match="request shapes must match"):
        eligibility_from_resolved_workload(
            replace(
                resolved,
                requirement=replace(requirement, machine_type="a4-highgpu-8g"),
            ),
            (candidate,),
        )


def test_resolver_eligibility_retains_nonspot_and_machine_support_reasons() -> None:
    """Coverage distinguishes Spot eligibility from provider machine support."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        2,
        DistributionShape.BALANCED,
    )
    standard = ComputeInstanceRequirement(
        candidate.machine.machine_type,
        candidate.vm_count,
        ProvisioningModel.STANDARD,
        CandidateLocations((candidate.endpoint_region,)),
    )
    standard_resolved = ResolvedWorkloadRequirement(
        standard,
        (_resolved_location(candidate.endpoint_region),),
        None,
    )

    nonspot = eligibility_from_resolved_workload(
        standard_resolved,
        (candidate,),
    )

    assert nonspot.candidates[0].reasons == (UnrankedReason.SPOT_UNSUPPORTED,)

    custom_candidate = replace(
        candidate,
        machine=SpotMachineConfiguration("custom-8-32768"),
    )
    custom_resolved = replace(
        standard_resolved,
        requirement=replace(
            standard,
            machine_type=custom_candidate.machine.machine_type,
            provisioning_model=ProvisioningModel.SPOT,
        ),
    )

    custom = eligibility_from_resolved_workload(
        custom_resolved,
        (custom_candidate,),
    )

    assert custom.support == AdviceSupport(False, False)
    assert custom.catalog_coverage[0].reasons == (
        "current-advice-unsupported-for-machine-configuration",
    )


def test_ineligible_candidate_never_reaches_advice_or_history_readers() -> None:
    """Resolver rejection remains visible without starting provider advice."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("uncataloged-machine"),
        1,
        DistributionShape.ANY,
    )
    base = _resolved_candidates((candidate,))
    resolved = replace(
        base,
        locations=(
            replace(
                base.locations[0],
                disposition=WorkloadLocationDisposition.INCOMPATIBLE,
                accelerator_id=None,
                owning_service=None,
                management_plane=None,
                supported_consumers=(),
                quota_pool=None,
                deployable_accelerator_quantity=None,
                constraint_set=None,
                constraint_requirements=(),
                failure_reason=ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY,
            ),
        ),
    )
    eligibility = eligibility_from_resolved_workload(resolved, (candidate,))
    advice: ScriptedReader[CapacityAdvice] = ScriptedReader([])
    history: ScriptedReader[CapacityHistory] = ScriptedReader([])

    result = asyncio.run(
        ObtainabilityOperations(
            advice,
            history,
            clock=lambda: NOW,
        ).compare(_request((candidate,), resolved=resolved))
    )

    assert advice.requests == []
    assert history.requests == []
    assert result.data.catalog_coverage == eligibility.catalog_coverage
    assert result.data.candidates[0].unranked_reasons == (
        UnrankedReason.CATALOG_UNSUPPORTED,
        UnrankedReason.NON_COMPUTE_MANAGEMENT_PLANE,
        UnrankedReason.CURRENT_ADVICE_UNSUPPORTED,
        UnrankedReason.HISTORY_UNSUPPORTED,
    )


def test_ineligible_candidate_does_not_suppress_queryable_sibling() -> None:
    """Candidate coverage remains independent within one fixed comparison."""
    eligible = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        1,
        DistributionShape.ANY,
    )
    ineligible = replace(eligible, endpoint_region="us-east1")
    base = _resolved_candidates((eligible, ineligible))
    unresolved = replace(
        base.locations[1],
        disposition=WorkloadLocationDisposition.INCOMPATIBLE,
        accelerator_id=None,
        owning_service=None,
        management_plane=None,
        supported_consumers=(),
        quota_pool=None,
        deployable_accelerator_quantity=None,
        constraint_set=None,
        constraint_requirements=(),
        failure_reason=ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY,
    )
    resolved = replace(base, locations=(base.locations[0], unresolved))
    advice = ScriptedReader([_read(CapacityAdvice(Decimal("0.8"), "3600s", (), NOW))])
    history = ScriptedReader([_read(_history("a3-highgpu-8g", "1.00"))])

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((eligible, ineligible), resolved=resolved)
        )
    )

    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert result.data.catalog_coverage == (
        ObtainabilityProductCoverage(
            "a3-highgpu-8g",
            "compute.googleapis.com",
            True,
            True,
            True,
        ),
    )
    assert len(advice.requests) == 1
    assert advice.requests[0].candidate == eligible  # type: ignore[union-attr]
    assessed = {item.candidate.candidate_id: item for item in result.data.candidates}
    assert assessed[eligible.candidate_id].advice is not None
    assert assessed[ineligible.candidate_id].advice is None
    assert (
        UnrankedReason.CATALOG_UNSUPPORTED
        in assessed[ineligible.candidate_id].unranked_reasons
    )


def test_location_ineligibility_does_not_change_product_coverage() -> None:
    """A rejected-only resolution cannot assert affirmative product coverage."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        1,
        DistributionShape.ANY,
    )
    base = _resolved_candidates((candidate,))
    unresolved = replace(
        base.locations[0],
        disposition=WorkloadLocationDisposition.INCOMPATIBLE,
        accelerator_id=None,
        owning_service=None,
        management_plane=None,
        supported_consumers=(),
        quota_pool=None,
        deployable_accelerator_quantity=None,
        constraint_set=None,
        constraint_requirements=(),
        failure_reason=ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY,
    )
    resolved = replace(base, locations=(unresolved,))
    advice = ScriptedReader([])
    history = ScriptedReader([])

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((candidate,), resolved=resolved)
        )
    )

    assert result.data.catalog_coverage == (
        ObtainabilityProductCoverage(
            "a3-highgpu-8g",
            "compute.googleapis.com",
            False,
            False,
            False,
            ("no-compatible-locations-proven",),
        ),
    )
    assert advice.requests == []
    assert history.requests == []
    assessed = result.data.candidates[0]
    assert assessed.advice is None
    assert UnrankedReason.CATALOG_UNSUPPORTED in assessed.unranked_reasons


def test_compare_ranks_complete_candidates_and_preserves_regional_score() -> None:
    """Application results expose rank derivations without copying scores to shards."""
    first = ObtainabilityCandidate(
        "us-central1",
        ("us-central1-a",),
        SpotMachineConfiguration("a3-highgpu-8g"),
        4,
        DistributionShape.ANY_SINGLE_ZONE,
    )
    second = ObtainabilityCandidate(
        "us-east1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        4,
        DistributionShape.ANY_SINGLE_ZONE,
    )
    advice = ScriptedReader(
        [
            _read(CapacityAdvice(Decimal("0.8"), "3600s", (), NOW)),
            _read(CapacityAdvice(Decimal("0.5"), "3600s", (), NOW)),
        ]
    )
    history = ScriptedReader(
        [
            _read(_history("a3-highgpu-8g", "1.50")),
            _read(_history("a3-highgpu-8g", "1.50")),
            _read(_history("a3-highgpu-8g", "1.00")),
        ]
    )

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((first, second))
        )
    )

    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert result.data.candidates[0].candidate == first
    assert result.data.candidates[0].rank == 1
    assert result.data.candidates[1].candidate == second
    assert result.data.candidates[1].rank == 2
    assert result.data.no_capacity_guarantee
    assert len(history.requests) == 3


def test_unsupported_cataloged_hardware_stays_visible_without_provider_calls() -> None:
    """Catalog presence is visible but never treated as provider-advice support."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("ct6e-standard-4t"),
        1,
        DistributionShape.ANY_SINGLE_ZONE,
    )
    advice: ScriptedReader[CapacityAdvice] = ScriptedReader([])
    history: ScriptedReader[CapacityHistory] = ScriptedReader([])
    coverage = ObtainabilityProductCoverage(
        product_id="ct6e-standard-4t",
        service="compute.googleapis.com",
        cataloged=True,
        current_advice_supported=False,
        history_supported=False,
        reasons=("current-advice-unsupported-for-machine-configuration",),
    )

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((candidate,))
        )
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.data.catalog_coverage == (coverage,)
    assert result.data.candidates[0].unranked_reasons == (
        UnrankedReason.CURRENT_ADVICE_UNSUPPORTED,
        UnrankedReason.HISTORY_UNSUPPORTED,
    )
    assert advice.requests == history.requests == []


def test_unproven_n1_attachment_never_reaches_provider_readers() -> None:
    """The application gate fails closed until attachment-aware catalog proof exists."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        ("us-central1-a",),
        SpotMachineConfiguration(
            "n1-standard-16",
            gpu=GpuAttachment("nvidia-tesla-t4", 2),
        ),
        3,
        DistributionShape.ANY_SINGLE_ZONE,
    )
    advice: ScriptedReader[CapacityAdvice] = ScriptedReader([])
    history: ScriptedReader[CapacityHistory] = ScriptedReader([])

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((candidate,))
        )
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.data.candidates[0].advice is None
    assert result.data.candidates[0].unranked_reasons == (
        UnrankedReason.CATALOG_UNSUPPORTED,
        UnrankedReason.CURRENT_ADVICE_UNSUPPORTED,
        UnrankedReason.HISTORY_UNSUPPORTED,
        UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU,
    )
    assert advice.requests == history.requests == []


def test_proven_n1_attachment_queries_current_advice_without_history() -> None:
    """Exact resolver attachment evidence permits current N1 advice only."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        ("us-central1-a",),
        SpotMachineConfiguration(
            "n1-standard-16",
            gpu=GpuAttachment("nvidia-tesla-t4", 2),
        ),
        3,
        DistributionShape.ANY_SINGLE_ZONE,
    )
    gpu = candidate.machine.gpu
    assert gpu is not None
    requirement = ComputeInstanceRequirement(
        candidate.machine.machine_type,
        candidate.vm_count,
        ProvisioningModel.SPOT,
        CandidateLocations(candidate.zones),
        attached_accelerator_type=gpu.accelerator_type,
        attached_accelerator_count=gpu.count,
    )
    base = _resolved_location(candidate.zones[0])
    attachment_id = AcceleratorId("nvidia-t4")
    attachment_quantity = gpu.count * candidate.vm_count
    constraint = base.constraint_requirements[0]
    assert base.constraint_set is not None
    resolved = ResolvedWorkloadRequirement(
        requirement,
        (
            replace(
                base,
                accelerator_id=attachment_id,
                deployable_accelerator_quantity=attachment_quantity,
                constraint_set=replace(
                    base.constraint_set,
                    accelerator_id=attachment_id,
                ),
                constraint_requirements=(
                    replace(
                        constraint,
                        source_quantity=attachment_quantity,
                        required=QuotaQuantity(
                            attachment_quantity,
                            constraint.required.unit,
                        ),
                    ),
                ),
                attached_accelerator_type=gpu.accelerator_type,
                attached_accelerator_count=gpu.count,
            ),
        ),
        None,
    )
    advice = ScriptedReader([_read(CapacityAdvice(Decimal("0.8"), "3600s", (), NOW))])
    history: ScriptedReader[CapacityHistory] = ScriptedReader([])

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((candidate,), resolved=resolved)
        )
    )

    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert result.data.catalog_coverage == (
        ObtainabilityProductCoverage(
            "n1-standard-16+nvidia-tesla-t4x2",
            "compute.googleapis.com",
            True,
            True,
            False,
            ("history-unsupported-n1-attached-gpu",),
        ),
    )
    assert result.data.candidates[0].advice is not None
    assert result.data.candidates[0].unranked_reasons == (
        UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU,
    )
    assert len(advice.requests) == 1
    assert history.requests == []

    for mismatched_machine in (
        SpotMachineConfiguration("n1-standard-16"),
        SpotMachineConfiguration(
            "n1-standard-16",
            gpu=GpuAttachment("nvidia-tesla-t4", 1),
        ),
    ):
        mismatched = replace(candidate, machine=mismatched_machine)
        mismatched_advice: ScriptedReader[CapacityAdvice] = ScriptedReader([])
        mismatched_history: ScriptedReader[CapacityHistory] = ScriptedReader([])

        rejected = asyncio.run(
            ObtainabilityOperations(
                mismatched_advice,
                mismatched_history,
                clock=lambda: NOW,
            ).compare(_request((mismatched,), resolved=resolved))
        )

        assert rejected.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
        assert UnrankedReason.CATALOG_UNSUPPORTED in (
            rejected.data.candidates[0].unranked_reasons
        )
        assert mismatched_advice.requests == mismatched_history.requests == []


def test_advice_permission_failure_retains_independent_history() -> None:
    """Missing advice permission does not suppress accessible history evidence."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        1,
        DistributionShape.ANY,
    )
    diagnostic = Diagnostic(
        DiagnosticCode("provider-read-authorization-failed"),
        Severity.ERROR,
        DiagnosticPhase("capacity-advice"),
        DiagnosticSource("compute-spot-advice"),
        RetryDisposition.NEVER,
        RedactedText("Grant the required read-only provider permission, then retry."),
    )
    advice: ScriptedReader[CapacityAdvice] = ScriptedReader(
        [
            ProviderRead(
                (),
                ProviderReadCoverage(1, 0),
                NOW,
                (diagnostic,),
                ("compute.googleapis.com",),
            )
        ]
    )
    history = ScriptedReader([_read(_history("a3-highgpu-8g", "1.00"))])

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((candidate,))
        )
    )

    assert result.outcome.exit_class is ExitClass.AUTHORIZATION
    assert not result.completeness.is_complete
    assert result.data.candidates[0].advice is None
    assert result.data.candidates[0].history is not None
    assert result.data.candidates[0].unranked_reasons == (
        UnrankedReason.ADVICE_UNAVAILABLE,
        UnrankedReason.CURRENT_PRICE_UNAVAILABLE,
    )
    assert len(history.requests) == 1


def test_incomplete_history_is_a_typed_evidence_failure() -> None:
    """A failed independent history port keeps current advice without ranking."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        1,
        DistributionShape.ANY,
    )
    advice = ScriptedReader([_read(CapacityAdvice(Decimal("0.8"), "3600s", (), NOW))])
    history: ScriptedReader[CapacityHistory] = ScriptedReader(
        [
            ProviderRead(
                (),
                ProviderReadCoverage(1, 0),
                NOW,
                (),
            )
        ]
    )

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            _request((candidate,))
        )
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.data.candidates[0].advice is not None
    assert result.data.candidates[0].history is None


def test_obtainability_request_rejects_mixed_or_untyped_comparisons() -> None:
    """The operation boundary requires one fixed exact request shape."""
    first = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        1,
        DistributionShape.ANY,
    )
    second = replace(first, endpoint_region="us-east1", vm_count=2)

    with pytest.raises(TypeError, match="resolver provenance"):
        ObtainabilityCompareRequest(_context(), (first,))
    with pytest.raises(ValueError, match="typed candidates"):
        ObtainabilityCompareRequest(_context(), ())
    with pytest.raises(ValueError, match="unique"):
        ObtainabilityCompareRequest(_context(), (first, first))
    with pytest.raises(ValueError, match="keep one exact"):
        ObtainabilityCompareRequest(_context(), (first, second))
    with pytest.raises(TypeError, match="resolver provenance"):
        ObtainabilityCompareRequest(
            _context(),
            (first,),
            resolver_provenance=cast("ResolvedWorkloadRequirement", "bad"),
        )
    mismatched = _resolved_candidates((first,))
    with pytest.raises(ValueError, match="request shapes must match"):
        ObtainabilityCompareRequest(
            _context(),
            (first,),
            resolver_provenance=replace(
                mismatched,
                requirement=replace(
                    cast("ComputeInstanceRequirement", mismatched.requirement),
                    instance_count=2,
                ),
            ),
        )
    with pytest.raises(TypeError, match="flags"):
        AdviceSupport(current_advice_supported=cast("bool", 1))


def test_resolver_expansion_rejects_wrong_workload_and_empty_compatibility() -> None:
    """All-compatible expansion never guesses across workload kinds or shapes."""
    spot = ComputeInstanceRequirement(
        "a3-highgpu-8g",
        2,
        ProvisioningModel.SPOT,
        AllCompatibleLocations(),
    )
    standard = replace(spot, provisioning_model=ProvisioningModel.STANDARD)
    tpu = CloudTpuSliceRequirement(
        "v6e-8",
        "2x4",
        "tpu-vm-base",
        1,
        ProvisioningModel.SPOT,
        AllCompatibleLocations(),
    )
    machine = SpotMachineConfiguration("a3-highgpu-8g")

    with pytest.raises(TypeError, match="compute-instance"):
        candidates_from_resolved_workload(
            ResolvedWorkloadRequirement(
                tpu,
                (),
                all_compatible_locations_exhaustive=True,
            ),
            machine=machine,
            distribution_shape=DistributionShape.ANY,
        )
    with pytest.raises(ValueError, match="Spot"):
        candidates_from_resolved_workload(
            ResolvedWorkloadRequirement(
                standard,
                (),
                all_compatible_locations_exhaustive=True,
            ),
            machine=machine,
            distribution_shape=DistributionShape.ANY,
        )
    with pytest.raises(ValueError, match="machine types"):
        candidates_from_resolved_workload(
            ResolvedWorkloadRequirement(
                spot,
                (),
                all_compatible_locations_exhaustive=True,
            ),
            machine=SpotMachineConfiguration("n2-standard-4"),
            distribution_shape=DistributionShape.ANY,
        )
    with pytest.raises(ValueError, match="compatible Compute"):
        candidates_from_resolved_workload(
            ResolvedWorkloadRequirement(
                spot,
                (),
                all_compatible_locations_exhaustive=True,
            ),
            machine=machine,
            distribution_shape=DistributionShape.ANY,
        )
