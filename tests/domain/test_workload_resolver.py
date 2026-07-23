"""Workload-first specialized-hardware resolution contracts."""

from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
    SpecializedHardwareCatalog,
    SpecializedHardwareRecord,
    WorkloadCatalogEvidence,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogLifecycle,
    CatalogLocationCoverage,
    ComputeAcceleratorType,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
    TpuAcceleratorConfig,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
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


def _quota(
    quota_id: str,
    *,
    dimensions: tuple[tuple[str, str], ...],
    scope: QuotaScope,
    effective: int = 64,
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
        quota_display_name=(
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
