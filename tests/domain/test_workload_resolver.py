"""Workload-first specialized-hardware resolution contracts."""

from datetime import date

from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    CompanionRequirementMapping,
    ComputeInstanceRequirement,
    DimensionSelector,
    OverlayMapping,
    ProvisioningModel,
    QuotaSelector,
    ResolutionFailureReason,
    SemanticAcceleratorOverlay,
    SpecializedHardwareCatalog,
    SpecializedHardwareRecord,
    WorkloadCatalogEvidence,
    WorkloadLocationDisposition,
    WorkloadQuantityBasis,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogGroupId,
    CatalogLifecycle,
    CatalogLocationCoverage,
    ComputeAcceleratorType,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
    ManagementPlane,
    TpuAcceleratorConfig,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
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
from cqmgr.domain.quotas import (
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
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

DEPLOYABLE_QUANTITY = 16
CONSTRAINT_COUNT = 2
N1_INSTANCE_COUNT = 3
N1_ATTACHED_ACCELERATOR_COUNT = 2


def _quota(
    quota_id: str,
    *,
    dimensions: tuple[tuple[str, str], ...],
    scope: QuotaScope,
    effective: int = 64,
    display_name: str | None = None,
) -> EffectiveQuotaEvidence:
    return EffectiveQuotaEvidence(
        identity=EffectiveQuotaSliceIdentity(
            resource_scope=ResourceScope(
                ResourceScopeKind.PROJECT, "projects/123456789"
            ),
            service="compute.googleapis.com",
            quota_id=quota_id,
            dimensions=NormalizedDimensions(dimensions),
            quota_scope=scope,
        ),
        effective_value=QuotaQuantity(effective, QuotaUnit("1")),
        metric="compute.googleapis.com/quota",
        declared_dimensions=tuple(key for key, _value in dimensions),
        applicable_locations=(
            ("global",) if scope is QuotaScope.GLOBAL else (dict(dimensions)["region"],)
        ),
        eligibility=QuotaIncreaseEligibility(
            eligible=True,
            reason=ProviderSymbol(
                "INELIGIBILITY_REASON_UNSPECIFIED", QuotaIneligibilityReason
            ),
        ),
        fixed=False,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
        quota_display_name=display_name
        or (
            "GPUs (all regions)"
            if scope is QuotaScope.GLOBAL
            else "GPUs per family per region"
        ),
    )


def test_compute_instance_derives_attachment_consumers_and_each_candidate() -> None:
    """Machine evidence, not caller selectors, determines deployable GPU demand."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=2,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a", "us-east1-b")),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-east1-b",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=tuple(
            coverage
            for zone in ("us-central1-a", "us-east1-b")
            for coverage in (
                CatalogLocationCoverage(
                    CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
                CatalogLocationCoverage(
                    CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
            )
        ),
        compute_accelerator_types=tuple(
            ComputeAcceleratorType("nvidia-b200", zone, None)
            for zone in ("us-central1-a", "us-east1-b")
        ),
    )
    quotas = (
        _quota(
            "GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=(("gpu_family", "NVIDIA_B200"), ("region", "us-central1")),
            scope=QuotaScope.REGIONAL,
        ),
        _quota(
            "GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=(("gpu_family", "NVIDIA_B200"), ("region", "us-east1")),
            scope=QuotaScope.REGIONAL,
        ),
        _quota("GPUS-ALL-REGIONS-per-project", dimensions=(), scope=QuotaScope.GLOBAL),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, quotas, catalog)

    assert tuple(item.location for item in result.locations) == (
        "us-central1-a",
        "us-east1-b",
    )
    assert all(
        item.deployable_accelerator_quantity == DEPLOYABLE_QUANTITY
        for item in result.locations
    )
    assert all(
        tuple(value.required for value in item.constraint_requirements)
        == (QuotaQuantity(DEPLOYABLE_QUANTITY, QuotaUnit("1")),) * CONSTRAINT_COUNT
        for item in result.locations
    )
    assert all(item.quota_pool == "standard" for item in result.locations)
    assert all(
        item.supported_consumers
        == (WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE)
        for item in result.locations
    )
    assert all(item.constraint_set is not None for item in result.locations)
    assert all(
        len(item.constraint_set.references) == CONSTRAINT_COUNT
        for item in result.locations
        if item.constraint_set is not None
    )


def _n1_t4_spot_catalog(
    *,
    attachment_count: int = 2,
    accelerator_state: LocationCoverageState = LocationCoverageState.SUCCESS,
) -> WorkloadCatalogEvidence:
    accelerator_diagnostics = (
        ()
        if accelerator_state is LocationCoverageState.SUCCESS
        else (
            Diagnostic(
                DiagnosticCode("accelerator-location-read-failed"),
                Severity.ERROR,
                DiagnosticPhase("provider-read"),
                DiagnosticSource("compute"),
                RetryDisposition.AFTER_REFRESH,
                RedactedText("The accelerator catalog could not be read."),
            ),
        )
    )
    return WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "n1-standard-16",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-tesla-t4", attachment_count),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                accelerator_state,
                accelerator_diagnostics,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType("nvidia-tesla-t4", "us-central1-a", None),
        ),
    )


def _n1_t4_spot_quotas() -> tuple[EffectiveQuotaEvidence, ...]:
    return (
        _quota(
            "PREEMPTIBLE_NVIDIA_T4_GPUS",
            dimensions=(("region", "us-central1"),),
            scope=QuotaScope.REGIONAL,
            display_name="Preemptible NVIDIA T4 GPUs",
        ),
        _quota("GPUS-ALL-REGIONS-per-project", dimensions=(), scope=QuotaScope.GLOBAL),
    )


def test_n1_spot_requirement_derives_exact_requested_attachment() -> None:
    """A public resolver input proves one exact N1 attached-GPU request."""
    requirement = ComputeInstanceRequirement(
        machine_type="n1-standard-16",
        instance_count=N1_INSTANCE_COUNT,
        provisioning_model=ProvisioningModel.SPOT,
        locations=CandidateLocations(("us-central1-a",)),
        attached_accelerator_type="nvidia-tesla-t4",
        attached_accelerator_count=N1_ATTACHED_ACCELERATOR_COUNT,
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(
        requirement,
        _n1_t4_spot_quotas(),
        _n1_t4_spot_catalog(),
    )

    location = result.locations[0]
    assert location.disposition is WorkloadLocationDisposition.COMPATIBLE
    assert location.accelerator_id == AcceleratorId("nvidia-t4")
    expected_quantity = N1_INSTANCE_COUNT * N1_ATTACHED_ACCELERATOR_COUNT
    assert location.deployable_accelerator_quantity == expected_quantity
    assert location.attached_accelerator_type == "nvidia-tesla-t4"
    assert location.attached_accelerator_count == N1_ATTACHED_ACCELERATOR_COUNT
    assert all(
        item.source_quantity == expected_quantity
        for item in location.constraint_requirements
    )


def test_n1_spot_requirement_fails_closed_on_attachment_mismatch() -> None:
    """A mismatched requested count cannot inherit N1 T4 quota semantics."""
    requirement = ComputeInstanceRequirement(
        machine_type="n1-standard-16",
        instance_count=N1_INSTANCE_COUNT,
        provisioning_model=ProvisioningModel.SPOT,
        locations=CandidateLocations(("us-central1-a",)),
        attached_accelerator_type="nvidia-tesla-t4",
        attached_accelerator_count=1,
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(
        requirement,
        _n1_t4_spot_quotas(),
        _n1_t4_spot_catalog(),
    )

    location = result.locations[0]
    assert location.disposition is WorkloadLocationDisposition.INCOMPATIBLE
    assert location.failure_reason is ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY


def test_n1_spot_requirement_fails_closed_on_incomplete_attachment_evidence() -> None:
    """Incomplete accelerator coverage cannot prove an N1 T4 attachment."""
    requirement = ComputeInstanceRequirement(
        machine_type="n1-standard-16",
        instance_count=N1_INSTANCE_COUNT,
        provisioning_model=ProvisioningModel.SPOT,
        locations=CandidateLocations(("us-central1-a",)),
        attached_accelerator_type="nvidia-tesla-t4",
        attached_accelerator_count=N1_ATTACHED_ACCELERATOR_COUNT,
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(
        requirement,
        _n1_t4_spot_quotas(),
        _n1_t4_spot_catalog(
            accelerator_state=LocationCoverageState.FAILED,
        ),
    )

    location = result.locations[0]
    assert location.disposition is WorkloadLocationDisposition.INCOMPLETE
    assert location.failure_reason is ResolutionFailureReason.MISSING_LOCATION_EVIDENCE


def test_compute_region_candidate_requires_consistent_child_zone_evidence() -> None:
    """A region resolves without choosing a zone when every child proves one shape."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=2,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1", "us-central1-a")),
    )
    zones = ("us-central1-a", "us-central1-b")
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=tuple(
            ComputeMachineType(
                "a4-highgpu-8g",
                zone,
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            )
            for zone in zones
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=tuple(
            coverage
            for zone in zones
            for coverage in (
                CatalogLocationCoverage(
                    CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
                CatalogLocationCoverage(
                    CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
            )
        ),
        compute_accelerator_types=tuple(
            ComputeAcceleratorType("nvidia-b200", zone, None) for zone in zones
        ),
    )
    quotas = (
        _quota(
            "GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=(("gpu_family", "NVIDIA_B200"), ("region", "us-central1")),
            scope=QuotaScope.REGIONAL,
        ),
        _quota("GPUS-ALL-REGIONS-per-project", dimensions=(), scope=QuotaScope.GLOBAL),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, quotas, catalog)

    assert tuple(item.location for item in result.locations) == (
        "us-central1",
        "us-central1-a",
    )
    assert result.locations[0].disposition is WorkloadLocationDisposition.COMPATIBLE
    assert result.locations[1].disposition is WorkloadLocationDisposition.COMPATIBLE
    assert result.locations[0].deployable_accelerator_quantity == DEPLOYABLE_QUANTITY
    assert tuple(item.location for item in result.locations[0].coverage) == (
        "us-central1-a",
        "us-central1-a",
        "us-central1-b",
        "us-central1-b",
    )


def test_compute_region_candidate_fails_closed_on_conflicting_child_shapes() -> None:
    """Different child-zone quantities cannot be hidden behind one region result."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1",)),
    )
    zones_and_counts = (("us-central1-a", 8), ("us-central1-b", 4))
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=tuple(
            ComputeMachineType(
                "a4-highgpu-8g",
                zone,
                (AcceleratorAttachment("nvidia-b200", count),),
                None,
            )
            for zone, count in zones_and_counts
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=tuple(
            coverage
            for zone, _count in zones_and_counts
            for coverage in (
                CatalogLocationCoverage(
                    CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
                CatalogLocationCoverage(
                    CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
            )
        ),
        compute_accelerator_types=tuple(
            ComputeAcceleratorType("nvidia-b200", zone, None)
            for zone, _count in zones_and_counts
        ),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, (), catalog)

    assert result.locations[0].location == "us-central1"
    assert result.locations[0].disposition is WorkloadLocationDisposition.AMBIGUOUS


def test_compute_instance_derives_an_independent_instance_count_companion() -> None:
    """A companion uses its own workload basis and native-unit conversion."""
    source = "https://docs.cloud.google.com/compute/resource-usage"
    overlay = SemanticAcceleratorOverlay(
        (
            OverlayMapping(
                group_id=CatalogGroupId.COMPUTE_ACCELERATORS,
                accelerator_id=AcceleratorId("synthetic-gpu"),
                management_plane=ManagementPlane.COMPUTE,
                workload_consumers=(WorkloadConsumer.COMPUTE_ENGINE,),
                selector=QuotaSelector(
                    service="compute.googleapis.com",
                    quota_id="SYNTHETIC-GPUS-per-project-region",
                    quota_display_name=None,
                    dimensions=(
                        DimensionSelector("gpu_family", "SYNTHETIC"),
                        DimensionSelector("region"),
                    ),
                    native_unit=QuotaUnit("1"),
                    quota_scope=QuotaScope.REGIONAL,
                    location_dimension="region",
                ),
                quota_pool="standard",
                conversion=UnitConversionEvidence(
                    "card",
                    QuotaUnit("1"),
                    1,
                    source,
                ),
                companion_requirements=(
                    CompanionRequirementMapping(
                        selector=QuotaSelector(
                            service="compute.googleapis.com",
                            quota_id="SYNTHETIC-CPUS-per-project-region",
                            quota_display_name=None,
                            dimensions=(DimensionSelector("region"),),
                            native_unit=QuotaUnit("1"),
                            quota_scope=QuotaScope.REGIONAL,
                            location_dimension="region",
                        ),
                        quantity_basis=WorkloadQuantityBasis.INSTANCE_COUNT,
                        conversion=UnitConversionEvidence(
                            "instance",
                            QuotaUnit("1"),
                            4,
                            source,
                        ),
                    ),
                ),
                source_url=source,
                reviewed_on=date(2026, 7, 23),
                machine_types=("synthetic-gpu-2g",),
                provider_accelerator_types=("synthetic-gpu",),
            ),
        )
    )
    requirement = ComputeInstanceRequirement(
        machine_type="synthetic-gpu-2g",
        instance_count=3,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "synthetic-gpu-2g",
                "us-central1-a",
                (AcceleratorAttachment("synthetic-gpu", 2),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType("synthetic-gpu", "us-central1-a", None),
        ),
    )
    quotas = (
        _quota(
            "SYNTHETIC-GPUS-per-project-region",
            dimensions=(("gpu_family", "SYNTHETIC"), ("region", "us-central1")),
            scope=QuotaScope.REGIONAL,
        ),
        _quota(
            "SYNTHETIC-CPUS-per-project-region",
            dimensions=(("region", "us-central1"),),
            scope=QuotaScope.REGIONAL,
        ),
    )

    result = overlay.resolve(requirement, quotas, catalog)

    by_quota_id = {
        item.identity.quota_id: item
        for item in result.locations[0].constraint_requirements
    }
    primary = by_quota_id["SYNTHETIC-GPUS-per-project-region"]
    companion = by_quota_id["SYNTHETIC-CPUS-per-project-region"]
    assert (primary.source_quantity, primary.required.value) == (6, 6)
    assert primary.conversion.source_unit == "card"
    assert (companion.source_quantity, companion.required.value) == (3, 12)
    assert companion.conversion.source_unit == "instance"


def test_compute_instance_fails_closed_without_declared_accelerator() -> None:
    """A machine attachment alone does not invent provider catalog guidance."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.EMPTY,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
        compute_accelerator_types=(),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, (), catalog)

    assert result.locations[0].disposition is WorkloadLocationDisposition.INCOMPATIBLE
    failure_reason = result.locations[0].failure_reason
    assert failure_reason is not None
    assert failure_reason.value == "unsupported-compatibility"


def test_compute_instance_fails_closed_when_native_quantity_overflows() -> None:
    """Oversized workload counts become a structured conversion failure."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=2**63,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType("nvidia-b200", "us-central1-a", None),
        ),
    )
    quotas = (
        _quota(
            "GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=(("gpu_family", "NVIDIA_B200"), ("region", "us-central1")),
            scope=QuotaScope.REGIONAL,
        ),
        _quota("GPUS-ALL-REGIONS-per-project", dimensions=(), scope=QuotaScope.GLOBAL),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, quotas, catalog)

    location = result.locations[0]
    assert location.disposition is WorkloadLocationDisposition.INCOMPATIBLE
    assert location.failure_reason is not None
    assert location.failure_reason.value == "unsupported-conversion"
    assert location.constraint_requirements == ()


def test_cloud_tpu_slice_derives_native_quantity_from_catalog_shape() -> None:
    """TPU topology and provider type determine cores per slice before scaling."""
    requirement = CloudTpuSliceRequirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count=2,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-b",)),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(),
        tpu_locations=(
            TpuLocation("projects/123456789/locations/us-central1-b", "us-central1-b"),
        ),
        tpu_accelerator_types=(
            TpuAcceleratorType(
                "projects/123456789/locations/us-central1-b/acceleratorTypes/v6e-8",
                "us-central1-b",
                "v6e-8",
                (TpuAcceleratorConfig("V6E", "2x4"),),
            ),
        ),
        tpu_runtime_versions=(
            TpuRuntimeVersion(
                "projects/123456789/locations/us-central1-b/runtimeVersions/tpu-vm-base",
                "us-central1-b",
                "tpu-vm-base",
            ),
        ),
        coverage=tuple(
            CatalogLocationCoverage(
                source,
                "us-central1-b",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            )
            for source in (
                CatalogEvidenceSource.TPU_LOCATIONS,
                CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
                CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
            )
        ),
    )
    quota = EffectiveQuotaEvidence(
        identity=EffectiveQuotaSliceIdentity(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
            "tpu.googleapis.com",
            "provider-discovered-v6e-id",
            NormalizedDimensions((("zone", "us-central1-b"),)),
            QuotaScope.ZONAL,
        ),
        effective_value=QuotaQuantity(64, QuotaUnit("core")),
        metric="tpu.googleapis.com/quota",
        declared_dimensions=("zone",),
        applicable_locations=("us-central1-b",),
        eligibility=QuotaIncreaseEligibility(
            eligible=True,
            reason=ProviderSymbol(
                "INELIGIBILITY_REASON_UNSPECIFIED", QuotaIneligibilityReason
            ),
        ),
        fixed=False,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
        quota_display_name="TPU v6e cores per project per zone",
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, (quota,), catalog)

    location = result.locations[0]
    assert location.deployable_accelerator_quantity == DEPLOYABLE_QUANTITY
    assert tuple(item.required for item in location.constraint_requirements) == (
        QuotaQuantity(DEPLOYABLE_QUANTITY, QuotaUnit("core")),
    )
    assert location.owning_service == "tpu.googleapis.com"
    assert location.supported_consumers == (WorkloadConsumer.CLOUD_TPU_API,)


def test_catalog_keeps_a4_and_new_provider_hardware_distinct_from_guidance() -> None:
    """Exhaustive discovery includes unknown hardware without inventing semantics."""
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(),
        tpu_locations=(
            TpuLocation("projects/123456789/locations/us-central1-b", "us-central1-b"),
        ),
        tpu_accelerator_types=(
            TpuAcceleratorType(
                "projects/123456789/locations/us-central1-b/acceleratorTypes/v6e-8",
                "us-central1-b",
                "v6e-8",
                (TpuAcceleratorConfig("V6E", "2x4"),),
            ),
        ),
        tpu_runtime_versions=(
            TpuRuntimeVersion(
                "projects/123456789/locations/us-central1-b/runtimeVersions/tpu-vm-base",
                "us-central1-b",
                "tpu-vm-base",
            ),
        ),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-east1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "global",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.EMPTY,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_LOCATIONS,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType(
                "nvidia-b200",
                "us-central1-a",
                ProviderSymbol("ACTIVE", CatalogLifecycle),
            ),
            ComputeAcceleratorType(
                "provider-next-x",
                "us-east1-b",
                ProviderSymbol("PROVIDER_FUTURE_STATE", CatalogLifecycle),
            ),
        ),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.discover_specialized_hardware(catalog)

    assert result.exhaustive
    assert tuple(
        (item.provider_accelerator_type, item.guided) for item in result.records
    ) == (
        ("nvidia-b200", True),
        ("provider-next-x", False),
        ("v6e-8", True),
    )
    unknown = result.records[1]
    assert unknown.accelerator_id is None
    assert unknown.lifecycle is not None
    assert unknown.lifecycle.raw == "PROVIDER_FUTURE_STATE"


def test_specialized_hardware_exhaustiveness_requires_record_source_coverage() -> None:
    """Complete coverage for another zone cannot prove a discovered record."""
    coverage = tuple(
        CatalogLocationCoverage(
            source,
            "us-east1-b"
            if source is CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES
            else "global",
            LocationCoverageExpectation.EXPECTED,
            LocationCoverageState.EMPTY,
        )
        for source in CatalogEvidenceSource
    )
    record = SpecializedHardwareRecord(
        service="compute.googleapis.com",
        provider_accelerator_type="nvidia-b200",
        location="us-central1-a",
        accelerator_id=AcceleratorId("nvidia-b200"),
        guided=True,
        lifecycle=None,
    )

    result = SpecializedHardwareCatalog((record,), coverage, exhaustive=False)

    assert not result.exhaustive


def test_all_compatible_keeps_incomplete_locations_without_ranking() -> None:
    """All-compatible enumeration reports gaps beside independently usable zones."""
    gap = Diagnostic(
        DiagnosticCode("catalog-location-unscanned"),
        Severity.ERROR,
        DiagnosticPhase("provider-read"),
        DiagnosticSource("compute"),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("The provider scope could not be scanned."),
    )
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=AllCompatibleLocations(),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-east1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.NOT_SCANNED,
                (gap,),
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-east1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.NOT_SCANNED,
                (gap,),
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType("nvidia-b200", "us-central1-a", None),
        ),
    )
    quotas = (
        _quota(
            "GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=(("gpu_family", "NVIDIA_B200"), ("region", "us-central1")),
            scope=QuotaScope.REGIONAL,
        ),
        _quota("GPUS-ALL-REGIONS-per-project", dimensions=(), scope=QuotaScope.GLOBAL),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, quotas, catalog)

    assert tuple(item.location for item in result.locations) == (
        "us-central1-a",
        "us-east1-b",
    )
    assert tuple(item.disposition for item in result.locations) == (
        WorkloadLocationDisposition.COMPATIBLE,
        WorkloadLocationDisposition.INCOMPLETE,
    )
    assert result.all_compatible_locations_exhaustive is False


def test_compute_all_compatible_honors_global_accelerator_scan_failure() -> None:
    """A failed global accelerator scan prevents an exhaustive location claim."""
    gap = Diagnostic(
        DiagnosticCode("accelerator-catalog-unscanned"),
        Severity.ERROR,
        DiagnosticPhase("provider-read"),
        DiagnosticSource("compute"),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("The accelerator catalog could not be scanned completely."),
    )
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=AllCompatibleLocations(),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "global",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.NOT_SCANNED,
                (gap,),
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType("nvidia-b200", "us-central1-a", None),
        ),
    )
    quotas = (
        _quota(
            "GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=(("gpu_family", "NVIDIA_B200"), ("region", "us-central1")),
            scope=QuotaScope.REGIONAL,
        ),
        _quota("GPUS-ALL-REGIONS-per-project", dimensions=(), scope=QuotaScope.GLOBAL),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, quotas, catalog)

    assert result.all_compatible_locations_exhaustive is False


def test_compute_all_compatible_honors_global_empty_accelerator_catalog() -> None:
    """A complete global empty result proves no zone has an accelerator type."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=AllCompatibleLocations(),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "global",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.EMPTY,
            ),
        ),
        compute_accelerator_types=(),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, (), catalog)

    assert result.all_compatible_locations_exhaustive is True
    assert tuple(item.location for item in result.locations) == ("us-central1-a",)
    location = result.locations[0]
    assert location.disposition is WorkloadLocationDisposition.INCOMPATIBLE
    assert location.failure_reason is not None
    assert location.failure_reason.value == "unsupported-compatibility"
    assert tuple(item.location for item in location.coverage) == (
        "us-central1-a",
        "global",
    )


def test_compute_global_empty_fails_closed_when_accelerator_values_exist() -> None:
    """Contradictory global-empty coverage cannot hide a discovered declaration."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=AllCompatibleLocations(),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a4-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-b200", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "global",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.EMPTY,
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType("nvidia-b200", "us-central1-a", None),
        ),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, (), catalog)

    assert result.all_compatible_locations_exhaustive is False
    location = result.locations[0]
    assert location.disposition is WorkloadLocationDisposition.INCOMPLETE
    assert location.failure_reason is not None
    assert location.failure_reason.value == "missing-location-evidence"


def test_cloud_tpu_all_compatible_requires_every_location_subsource() -> None:
    """TPU enumeration is not exhaustive when runtime coverage is incomplete."""
    gap = Diagnostic(
        DiagnosticCode("runtime-version-location-unscanned"),
        Severity.ERROR,
        DiagnosticPhase("provider-read"),
        DiagnosticSource("cloud-tpu"),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("The runtime-version catalog could not be scanned."),
    )
    requirement = CloudTpuSliceRequirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=AllCompatibleLocations(),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(),
        tpu_locations=(
            TpuLocation("projects/123456789/locations/us-central1-b", "us-central1-b"),
        ),
        tpu_accelerator_types=(
            TpuAcceleratorType(
                "projects/123456789/locations/us-central1-b/acceleratorTypes/v6e-8",
                "us-central1-b",
                "v6e-8",
                (TpuAcceleratorConfig("V6E", "2x4"),),
            ),
        ),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_LOCATIONS,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.NOT_SCANNED,
                (gap,),
            ),
        ),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, (), catalog)

    assert result.all_compatible_locations_exhaustive is False
    assert tuple(item.location for item in result.locations) == ("us-central1-b",)
    assert result.locations[0].disposition is WorkloadLocationDisposition.INCOMPLETE


def test_cloud_tpu_all_compatible_can_prove_an_empty_inventory() -> None:
    """A complete empty location inventory needs no per-location child reads."""
    requirement = CloudTpuSliceRequirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=AllCompatibleLocations(),
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_LOCATIONS,
                "global",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.EMPTY,
            ),
        ),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(requirement, (), catalog)

    assert result.locations == ()
    assert result.all_compatible_locations_exhaustive is True
