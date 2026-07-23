"""Application contract for read-only Spot obtainability comparison."""

from __future__ import annotations

# ruff: noqa: PLR2004
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.application.operations.obtainability import (
    AdviceSupport,
    ObtainabilityCompareRequest,
    ObtainabilityOperations,
    candidates_from_resolved_workload,
)
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.application.ports.obtainability import (
    CapacityAdviceReader,
    CapacityHistoryReader,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
    QuotaConstraintRequirement,
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
    result = asyncio.run(
        ObtainabilityOperations(
            ScriptedReader([]),
            ScriptedReader([]),
            clock=lambda: NOW,
        ).compare(
            ObtainabilityCompareRequest(
                _context(),
                candidates,
                support=AdviceSupport(
                    current_advice_supported=False,
                    history_supported=False,
                ),
                resolver_provenance=resolved,
            )
        )
    )
    assert result.data.resolver_provenance is resolved


def test_current_and_history_ports_are_independently_disableable() -> None:
    """Disabling current advice does not prevent a supported history read."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        1,
        DistributionShape.ANY,
    )
    advice: ScriptedReader[CapacityAdvice] = ScriptedReader([])
    history = ScriptedReader([_read(_history("a3-highgpu-8g", "1.00"))])

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            ObtainabilityCompareRequest(
                _context(),
                (candidate,),
                support=AdviceSupport(
                    current_advice_supported=False,
                    history_supported=True,
                ),
            )
        )
    )

    assert advice.requests == []
    assert len(history.requests) == 1
    assert result.data.candidates[0].history is not None


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
            ObtainabilityCompareRequest(_context(), (first, second))
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
        product_id="tpu-v6e",
        service="compute.googleapis.com",
        cataloged=True,
        current_advice_supported=False,
        history_supported=False,
        reasons=("tpu-advice-unsupported",),
    )

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            ObtainabilityCompareRequest(
                _context(),
                (candidate,),
                support=AdviceSupport(
                    current_advice_supported=False,
                    history_supported=False,
                ),
                catalog_coverage=(coverage,),
            )
        )
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.data.catalog_coverage == (coverage,)
    assert result.data.candidates[0].unranked_reasons == (
        UnrankedReason.CURRENT_ADVICE_UNSUPPORTED,
        UnrankedReason.HISTORY_UNSUPPORTED,
    )
    assert advice.requests == history.requests == []


def test_n1_attached_gpu_keeps_current_advice_and_exact_history_reason() -> None:
    """N1 attached GPUs retain current advice without invented history."""
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
    advice = ScriptedReader([_read(CapacityAdvice(Decimal("0.8"), "3600s", (), NOW))])
    history: ScriptedReader[CapacityHistory] = ScriptedReader([])

    result = asyncio.run(
        ObtainabilityOperations(advice, history, clock=lambda: NOW).compare(
            ObtainabilityCompareRequest(_context(), (candidate,))
        )
    )

    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert result.data.candidates[0].advice is not None
    assert result.data.candidates[0].unranked_reasons == (
        UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU,
    )
    assert history.requests == []


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
            ObtainabilityCompareRequest(_context(), (candidate,))
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
            ObtainabilityCompareRequest(_context(), (candidate,))
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

    with pytest.raises(ValueError, match="typed candidates"):
        ObtainabilityCompareRequest(_context(), ())
    with pytest.raises(ValueError, match="unique"):
        ObtainabilityCompareRequest(_context(), (first, first))
    with pytest.raises(ValueError, match="keep one exact"):
        ObtainabilityCompareRequest(_context(), (first, second))
    with pytest.raises(TypeError, match="support"):
        ObtainabilityCompareRequest(
            _context(),
            (first,),
            support=cast("AdviceSupport", "bad"),
        )
    with pytest.raises(TypeError, match="catalog coverage"):
        ObtainabilityCompareRequest(
            _context(),
            (first,),
            catalog_coverage=cast(
                "tuple[ObtainabilityProductCoverage, ...]",
                ("bad",),
            ),
        )
    with pytest.raises(TypeError, match="resolver provenance"):
        ObtainabilityCompareRequest(
            _context(),
            (first,),
            resolver_provenance=cast("ResolvedWorkloadRequirement", "bad"),
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
