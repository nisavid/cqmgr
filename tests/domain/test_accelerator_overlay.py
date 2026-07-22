"""Maintained accelerator semantic-overlay contracts."""

from dataclasses import replace
from datetime import date

import pytest

from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    AmbiguousOverlayMatchError,
    DimensionSelector,
    GpuWorkloadRequirement,
    OverlayMapping,
    ProvisioningModel,
    QuotaSelector,
    ResolutionFailureReason,
    ResolvedQuotaRequirement,
    SemanticAcceleratorOverlay,
    TpuWorkloadRequirement,
    WorkloadCatalogEvidence,
    WorkloadResolutionError,
)
from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorAttachment,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogGroupId,
    CatalogLocationCoverage,
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
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

REVIEW_DATE = date(2026, 7, 14)


def _predicate_values(item: QuotaQueryItem) -> tuple[bool, bool, bool, bool]:
    predicates = item.predicates
    return (
        predicates.discovered,
        predicates.cataloged,
        predicates.guided,
        predicates.mutable,
    )


def _mapping(accelerator: str) -> OverlayMapping:
    source = "https://docs.cloud.google.com/compute/resource-usage"
    return OverlayMapping(
        group_id=CatalogGroupId.COMPUTE_ACCELERATORS,
        accelerator_id=AcceleratorId(accelerator),
        management_plane=ManagementPlane.COMPUTE,
        workload_consumers=(WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE),
        selector=QuotaSelector(
            service="compute.googleapis.com",
            quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
            quota_display_name="GPUs per family per region",
            dimensions=(
                DimensionSelector("gpu_family", "NVIDIA_H100"),
                DimensionSelector("region"),
            ),
            native_unit=QuotaUnit("1"),
            quota_scope=QuotaScope.REGIONAL,
            location_dimension="region",
        ),
        quota_pool="standard",
        conversion=UnitConversionEvidence("card", QuotaUnit("1"), 1, source),
        companion_selectors=(),
        source_url=source,
        reviewed_on=REVIEW_DATE,
    )


def test_overlay_metadata_is_derived_from_canonical_immutable_content() -> None:
    """Input ordering cannot change identity, while semantic content changes can."""
    first = _mapping("nvidia-h100")
    second = _mapping("nvidia-h100-variant")

    forward = SemanticAcceleratorOverlay((first, second))
    reversed_ = SemanticAcceleratorOverlay((second, first))
    changed = SemanticAcceleratorOverlay((first,))
    compatibility_changed = SemanticAcceleratorOverlay(
        (replace(first, machine_types=("a3-highgpu-4g",)), second)
    )

    assert forward.metadata.schema == ACCELERATOR_CATALOG_SCHEMA
    assert forward.metadata.revision == REVIEW_DATE.isoformat()
    assert forward.metadata == reversed_.metadata
    assert forward.mappings == reversed_.mappings
    assert changed.metadata.content_digest != forward.metadata.content_digest
    assert (
        compatibility_changed.metadata.content_digest != forward.metadata.content_digest
    )


def test_provisioning_model_vocabulary_is_distinct_from_quota_pools() -> None:
    """V1 workload modes are not aliases for standard/preemptible quota pools."""
    assert tuple(model.value for model in ProvisioningModel) == (
        "standard",
        "spot",
        "flex-start",
        "reservation-bound",
    )


def test_maintained_mappings_have_stable_groups_and_reviewable_official_sources() -> (
    None
):
    """Every maintained semantic claim carries first-party source provenance."""
    groups = {mapping.group_id for mapping in MAINTAINED_ACCELERATOR_OVERLAY.mappings}

    assert groups == {
        CatalogGroupId.COMPUTE_ACCELERATORS,
        CatalogGroupId.CLOUD_TPU_LEGACY,
    }
    assert all(
        mapping.source_url.startswith("https://docs.cloud.google.com/")
        and mapping.reviewed_on == REVIEW_DATE
        for mapping in MAINTAINED_ACCELERATOR_OVERLAY.mappings
    )
    legacy = next(
        mapping
        for mapping in MAINTAINED_ACCELERATOR_OVERLAY.mappings
        if mapping.group_id is CatalogGroupId.CLOUD_TPU_LEGACY
    )
    assert legacy.selector.quota_id is None
    assert legacy.selector.native_unit == QuotaUnit("core")
    assert legacy.selector.quota_scope is QuotaScope.ZONAL


def _evidence(  # noqa: PLR0913
    quota_id: str,
    *,
    service: str,
    dimensions: tuple[tuple[str, str], ...],
    scope: QuotaScope,
    unit: str,
    locations: tuple[str, ...],
    display_name: str,
    fixed: bool = False,
) -> EffectiveQuotaEvidence:
    identity = EffectiveQuotaSliceIdentity(
        resource_scope=ResourceScope(ResourceScopeKind.PROJECT, "projects/123"),
        service=service,
        quota_id=quota_id,
        dimensions=NormalizedDimensions(dimensions),
        quota_scope=scope,
    )
    return EffectiveQuotaEvidence(
        identity=identity,
        effective_value=QuotaQuantity(8, QuotaUnit(unit)),
        metric=f"{service}/quota",
        declared_dimensions=tuple(key for key, _value in dimensions),
        applicable_locations=locations,
        eligibility=QuotaIncreaseEligibility(
            eligible=not fixed,
            reason=ProviderSymbol(
                "INELIGIBILITY_REASON_UNSPECIFIED", QuotaIneligibilityReason
            ),
        ),
        fixed=fixed,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
        quota_display_name=display_name,
    )


def test_overlay_join_preserves_four_independent_catalog_and_mutability_states() -> (
    None
):
    """Recognition and guidance never create discovery or fresh mutability."""
    guided = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )
    unknown = _evidence(
        "FUTURE-QUOTA",
        service="future.googleapis.com",
        dimensions=(),
        scope=QuotaScope.UNKNOWN,
        unit="future-unit",
        locations=("global",),
        display_name="Future quota",
    )

    guided_immutable = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        guided, freshly_validated_mutable=False
    )
    unguided_overlay = SemanticAcceleratorOverlay(
        (replace(_mapping("nvidia-h100"), conversion=None),)
    )
    cataloged_unguided = unguided_overlay.classify(
        guided, freshly_validated_mutable=False
    )
    discovered_only = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        unknown, freshly_validated_mutable=False
    )
    generic_mutable = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        unknown, freshly_validated_mutable=True
    )

    assert guided_immutable.identity is guided.identity
    assert _predicate_values(guided_immutable) == (True, True, True, False)
    assert _predicate_values(cataloged_unguided) == (True, True, False, False)
    assert _predicate_values(discovered_only) == (True, False, False, False)
    assert _predicate_values(generic_mutable) == (True, False, False, True)


def test_exact_quota_id_is_authoritative_over_provider_display_name() -> None:
    """A renamed display label cannot defeat an exact maintained quota ID."""
    evidence = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="Provider-renamed label",
    )

    item = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        evidence, freshly_validated_mutable=False
    )

    assert item.predicates.cataloged
    assert item.accelerator_id == AcceleratorId("nvidia-h100")


def test_selector_rejects_unmaintained_extra_dimensions() -> None:
    """An extra provider dimension is a distinct slice, not a close match."""
    evidence = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(
            ("gpu_family", "NVIDIA_H100"),
            ("region", "us-central1"),
            ("workload_type", "future"),
        ),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )

    item = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        evidence, freshly_validated_mutable=False
    )

    assert _predicate_values(item) == (True, False, False, False)


def test_overlay_returns_exact_regional_and_global_constraint_references() -> None:
    """Companion constraints remain references to authoritative live slices."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )
    global_ = _evidence(
        "GPUS-ALL-REGIONS-per-project",
        service="compute.googleapis.com",
        dimensions=(),
        scope=QuotaScope.GLOBAL,
        unit="1",
        locations=("global",),
        display_name="GPUs (all regions)",
    )

    unrelated_region = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-east1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-east1",),
        display_name="GPUs per family per region",
    )
    unrelated_scope = replace(
        global_,
        identity=replace(
            global_.identity,
            resource_scope=ResourceScope(ResourceScopeKind.PROJECT, "projects/456"),
        ),
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.constraint_set(
        AcceleratorId("nvidia-h100"),
        regional,
        (unrelated_region, unrelated_scope, global_, regional),
    )

    assert result is not None
    assert tuple(reference.slice_identity for reference in result.references) == (
        global_.identity,
        regional.identity,
    )

    assert (
        MAINTAINED_ACCELERATOR_OVERLAY.constraint_set(
            AcceleratorId("nvidia-h100"), regional, (regional,)
        )
        is None
    )
    with pytest.raises(AmbiguousOverlayMatchError):
        MAINTAINED_ACCELERATOR_OVERLAY.constraint_set(
            AcceleratorId("nvidia-h100"),
            regional,
            (regional, global_, global_),
        )


def _gpu_requirement() -> GpuWorkloadRequirement:
    return GpuWorkloadRequirement(
        accelerator_id=AcceleratorId("nvidia-h100"),
        workload_consumer=WorkloadConsumer.COMPUTE_ENGINE,
        accelerator_count=8,
        machine_type="a3-highgpu-8g",
        provisioning_model=ProvisioningModel.STANDARD,
        region="us-central1",
        zone="us-central1-a",
    )


def _gpu_catalog_evidence(*, include_machine: bool = True) -> WorkloadCatalogEvidence:
    machines = (
        (
            ComputeMachineType(
                name="a3-highgpu-8g",
                zone="us-central1-a",
                guest_accelerators=(AcceleratorAttachment("nvidia-h100-80gb", 8),),
                lifecycle=None,
            ),
        )
        if include_machine
        else ()
    )
    return WorkloadCatalogEvidence(
        compute_machine_types=machines,
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                source=CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                location="us-central1-a",
                expectation=LocationCoverageExpectation.REQUESTED,
                state=LocationCoverageState.SUCCESS,
            ),
        ),
    )


def test_gpu_resolver_returns_native_amount_owner_and_exact_constraints() -> None:
    """Resolution reports quota requirements and never a capacity conclusion."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )
    global_ = _evidence(
        "GPUS-ALL-REGIONS-per-project",
        service="compute.googleapis.com",
        dimensions=(),
        scope=QuotaScope.GLOBAL,
        unit="1",
        locations=("global",),
        display_name="GPUs (all regions)",
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(
        _gpu_requirement(), (regional, global_), _gpu_catalog_evidence()
    )

    assert isinstance(result, ResolvedQuotaRequirement)
    assert result.owning_service == "compute.googleapis.com"
    assert result.required_amount == QuotaQuantity(8, QuotaUnit("1"))
    assert result.conversion.source_unit == "card"
    assert tuple(
        reference.slice_identity for reference in result.constraint_set.references
    ) == (global_.identity, regional.identity)


def test_gpu_resolver_fails_when_a_required_companion_slice_is_missing() -> None:
    """A primary slice alone cannot satisfy a maintained constraint relationship."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )

    with pytest.raises(WorkloadResolutionError) as missing:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            _gpu_requirement(), (regional,), _gpu_catalog_evidence()
        )

    assert missing.value.reason is ResolutionFailureReason.PROVIDER_IDENTITY

    global_ = _evidence(
        "GPUS-ALL-REGIONS-per-project",
        service="compute.googleapis.com",
        dimensions=(),
        scope=QuotaScope.GLOBAL,
        unit="1",
        locations=("global",),
        display_name="GPUs (all regions)",
    )
    with pytest.raises(WorkloadResolutionError) as ambiguous:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            _gpu_requirement(),
            (regional, global_, global_),
            _gpu_catalog_evidence(),
        )

    assert ambiguous.value.reason is ResolutionFailureReason.AMBIGUOUS


def test_gpu_resolver_fails_when_an_exact_constraint_is_ineligible() -> None:
    """Guidance stops when any independent limiting slice is ineligible."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
        fixed=True,
    )
    global_ = _evidence(
        "GPUS-ALL-REGIONS-per-project",
        service="compute.googleapis.com",
        dimensions=(),
        scope=QuotaScope.GLOBAL,
        unit="1",
        locations=("global",),
        display_name="GPUs (all regions)",
    )

    with pytest.raises(WorkloadResolutionError) as rejected:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            _gpu_requirement(), (regional, global_), _gpu_catalog_evidence()
        )

    assert rejected.value.reason is ResolutionFailureReason.INELIGIBLE


def test_gpu_resolver_requires_exact_fixed_shape_count_and_coherent_location() -> None:
    """A fixed machine shape cannot be resized or paired with another region."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )

    with pytest.raises(WorkloadResolutionError) as count:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            replace(_gpu_requirement(), accelerator_count=4),
            (regional,),
            _gpu_catalog_evidence(),
        )
    assert count.value.reason is ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY

    with pytest.raises(ValueError, match="zone must belong to its explicit region"):
        replace(_gpu_requirement(), zone="us-east1-b")

    with pytest.raises(WorkloadResolutionError) as flex_start:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            replace(
                _gpu_requirement(),
                provisioning_model=ProvisioningModel.FLEX_START,
            ),
            (regional,),
            _gpu_catalog_evidence(),
        )
    assert flex_start.value.reason is ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY


def test_tpu_requirement_makes_management_plane_specific_shape_explicit() -> None:
    """Legacy TPU requires topology/runtime while Compute TPU requires a machine."""
    legacy = TpuWorkloadRequirement(
        management_plane=ManagementPlane.TPU,
        accelerator_id=AcceleratorId("tpu-v6e"),
        workload_consumer=WorkloadConsumer.CLOUD_TPU_API,
        accelerator_count=8,
        provisioning_model=ProvisioningModel.STANDARD,
        region=None,
        zone="us-central1-b",
        machine_type=None,
        topology="2x4",
        runtime_version="tpu-vm-base",
    )

    assert legacy.management_plane is ManagementPlane.TPU
    with pytest.raises(ValueError, match="legacy TPU"):
        TpuWorkloadRequirement(
            management_plane=ManagementPlane.TPU,
            accelerator_id=AcceleratorId("tpu-v6e"),
            workload_consumer=WorkloadConsumer.CLOUD_TPU_API,
            accelerator_count=8,
            provisioning_model=ProvisioningModel.STANDARD,
            region=None,
            zone="us-central1-b",
            machine_type="ct6e-standard-4t",
            topology=None,
            runtime_version=None,
        )

    with pytest.raises(ValueError, match="does not match"):
        replace(legacy, workload_consumer=WorkloadConsumer.GKE)


def _compute_tpu_requirement(
    workload_consumer: WorkloadConsumer,
) -> TpuWorkloadRequirement:
    return TpuWorkloadRequirement(
        management_plane=ManagementPlane.COMPUTE,
        accelerator_id=AcceleratorId("tpu-v6e"),
        workload_consumer=workload_consumer,
        accelerator_count=4,
        provisioning_model=ProvisioningModel.STANDARD,
        region="us-central1",
        zone="us-central1-b",
        machine_type="ct6e-standard-4t",
        topology=None,
        runtime_version=None,
    )


def _compute_tpu_catalog_evidence() -> WorkloadCatalogEvidence:
    return WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                name="ct6e-standard-4t",
                zone="us-central1-b",
                guest_accelerators=(AcceleratorAttachment("tpu-v6e", 4),),
                lifecycle=None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                source=CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                location="us-central1-b",
                expectation=LocationCoverageExpectation.REQUESTED,
                state=LocationCoverageState.SUCCESS,
            ),
        ),
    )


@pytest.mark.parametrize(
    "workload_consumer",
    [WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE],
)
def test_compute_tpu_resolver_uses_compute_owned_quota_for_both_consumers(
    workload_consumer: WorkloadConsumer,
) -> None:
    """Direct Compute and GKE TPU workloads share one Compute-owned mapping."""
    regional = _evidence(
        "provider-discovered-ct6e-id",
        service="compute.googleapis.com",
        dimensions=(("region", "us-central1"), ("tpu_family", "CT6E")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="TPUs per TPU family",
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(
        _compute_tpu_requirement(workload_consumer),
        (regional,),
        _compute_tpu_catalog_evidence(),
    )

    assert result.owning_service == "compute.googleapis.com"
    assert result.required_amount == QuotaQuantity(4, QuotaUnit("1"))
    assert result.conversion.source_unit == "chip"
    assert result.constraint_set.references == (ConstraintReference(regional.identity),)


def _legacy_tpu_requirement() -> TpuWorkloadRequirement:
    return TpuWorkloadRequirement(
        management_plane=ManagementPlane.TPU,
        accelerator_id=AcceleratorId("tpu-v6e"),
        workload_consumer=WorkloadConsumer.CLOUD_TPU_API,
        accelerator_count=8,
        provisioning_model=ProvisioningModel.STANDARD,
        region=None,
        zone="us-central1-b",
        machine_type=None,
        topology="2x4",
        runtime_version="tpu-vm-base",
    )


def _legacy_tpu_catalog_evidence() -> WorkloadCatalogEvidence:
    successful_sources = (
        CatalogEvidenceSource.TPU_LOCATIONS,
        CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
        CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
    )
    return WorkloadCatalogEvidence(
        compute_machine_types=(),
        tpu_locations=(
            TpuLocation(
                name="projects/123/locations/us-central1-b",
                location_id="us-central1-b",
            ),
        ),
        tpu_accelerator_types=(
            TpuAcceleratorType(
                name=("projects/123/locations/us-central1-b/acceleratorTypes/v6e-8"),
                zone="us-central1-b",
                accelerator_type="v6e-8",
                configurations=(TpuAcceleratorConfig("V6E", "2x4"),),
            ),
        ),
        tpu_runtime_versions=(
            TpuRuntimeVersion(
                name=(
                    "projects/123/locations/us-central1-b/runtimeVersions/tpu-vm-base"
                ),
                zone="us-central1-b",
                version="tpu-vm-base",
            ),
        ),
        coverage=tuple(
            CatalogLocationCoverage(
                source=source,
                location="us-central1-b",
                expectation=LocationCoverageExpectation.REQUESTED,
                state=LocationCoverageState.SUCCESS,
            )
            for source in successful_sources
        ),
    )


def test_legacy_tpu_resolver_returns_zonal_native_core_requirement() -> None:
    """Legacy TPU resolution retains its owning service, zone, and core unit."""
    zonal = _evidence(
        "provider-discovered-v6e-id",
        service="tpu.googleapis.com",
        dimensions=(("zone", "us-central1-b"),),
        scope=QuotaScope.ZONAL,
        unit="core",
        locations=("us-central1-b",),
        display_name="TPU v6e cores per project per zone",
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.resolve(
        _legacy_tpu_requirement(),
        (zonal,),
        _legacy_tpu_catalog_evidence(),
    )

    assert result.owning_service == "tpu.googleapis.com"
    assert result.required_amount == QuotaQuantity(8, QuotaUnit("core"))
    assert result.conversion.source_unit == "core"
    assert result.constraint_set.references == (ConstraintReference(zonal.identity),)


def test_resolver_fails_closed_for_missing_compatibility_and_provider_identity() -> (
    None
):
    """Missing catalog shape or exact quota identity never produces guidance."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )
    wrong_identity = _evidence(
        "SIMILAR-BUT-WRONG",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )

    with pytest.raises(WorkloadResolutionError) as compatibility:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            _gpu_requirement(),
            (regional,),
            _gpu_catalog_evidence(include_machine=False),
        )
    assert (
        compatibility.value.reason is ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY
    )

    with pytest.raises(WorkloadResolutionError) as identity:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            _gpu_requirement(), (wrong_identity,), _gpu_catalog_evidence()
        )
    assert identity.value.reason is ResolutionFailureReason.PROVIDER_IDENTITY


def test_resolver_fails_closed_for_ambiguity_missing_location_and_conversion() -> None:
    """Every unresolved safety axis has an explicit non-capacity failure reason."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        service="compute.googleapis.com",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
        unit="1",
        locations=("us-central1",),
        display_name="GPUs per family per region",
    )
    without_coverage = WorkloadCatalogEvidence(
        compute_machine_types=_gpu_catalog_evidence().compute_machine_types,
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(),
    )
    legacy = _evidence(
        "provider-discovered-v6e-id",
        service="tpu.googleapis.com",
        dimensions=(("zone", "us-central1-b"),),
        scope=QuotaScope.ZONAL,
        unit="core",
        locations=("us-central1-b",),
        display_name="TPU v6e cores per project per zone",
    )

    with pytest.raises(WorkloadResolutionError) as ambiguous:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            _gpu_requirement(), (regional, regional), _gpu_catalog_evidence()
        )
    assert ambiguous.value.reason is ResolutionFailureReason.AMBIGUOUS

    with pytest.raises(WorkloadResolutionError) as coverage:
        MAINTAINED_ACCELERATOR_OVERLAY.resolve(
            _gpu_requirement(), (regional,), without_coverage
        )
    assert coverage.value.reason is ResolutionFailureReason.MISSING_LOCATION_EVIDENCE

    unsupported_mapping = replace(
        next(
            mapping
            for mapping in MAINTAINED_ACCELERATOR_OVERLAY.mappings
            if mapping.accelerator_id == AcceleratorId("tpu-v6e")
            and mapping.management_plane is ManagementPlane.TPU
        ),
        conversion=None,
    )
    with pytest.raises(WorkloadResolutionError) as conversion:
        SemanticAcceleratorOverlay((unsupported_mapping,)).resolve(
            _legacy_tpu_requirement(),
            (legacy,),
            _legacy_tpu_catalog_evidence(),
        )
    assert conversion.value.reason is ResolutionFailureReason.UNSUPPORTED_CONVERSION
