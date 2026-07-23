"""Maintained accelerator semantic-overlay contracts."""

from dataclasses import replace
from datetime import date

import pytest

from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    AmbiguousOverlayMatchError,
    DimensionSelector,
    OverlayMapping,
    ProvisioningModel,
    QuotaSelector,
    SemanticAcceleratorOverlay,
)
from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorId,
    CatalogGroupId,
    ManagementPlane,
    UnitConversionEvidence,
    WorkloadConsumer,
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
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

REVIEW_DATE = date(2026, 7, 14)
SOURCE = "https://docs.cloud.google.com/compute/resource-usage"


def _mapping(accelerator: str) -> OverlayMapping:
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
        conversion=UnitConversionEvidence("card", QuotaUnit("1"), 1, SOURCE),
        companion_selectors=(),
        source_url=SOURCE,
        reviewed_on=REVIEW_DATE,
        provisioning_models=(ProvisioningModel.STANDARD,),
    )


def _evidence(
    quota_id: str,
    *,
    dimensions: tuple[tuple[str, str], ...],
    scope: QuotaScope,
) -> EffectiveQuotaEvidence:
    return EffectiveQuotaEvidence(
        identity=EffectiveQuotaSliceIdentity(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
            "compute.googleapis.com",
            quota_id,
            NormalizedDimensions(dimensions),
            scope,
        ),
        effective_value=QuotaQuantity(64, QuotaUnit("1")),
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


def test_overlay_identity_is_canonical_and_content_addressed() -> None:
    """Input ordering cannot alter catalog identity, but semantics can."""
    first = _mapping("nvidia-h100")
    second = _mapping("nvidia-h100-variant")

    forward = SemanticAcceleratorOverlay((first, second))
    reverse = SemanticAcceleratorOverlay((second, first))
    pool_changed = SemanticAcceleratorOverlay(
        (replace(first, quota_pool="preemptible"), second)
    )
    compatibility_changed = SemanticAcceleratorOverlay(
        (replace(first, machine_types=("a3-highgpu-4g",)), second)
    )

    assert forward.metadata.schema == ACCELERATOR_CATALOG_SCHEMA
    assert forward.metadata.revision == REVIEW_DATE.isoformat()
    assert forward.metadata == reverse.metadata
    assert forward.mappings == reverse.mappings
    assert pool_changed.metadata.content_digest != forward.metadata.content_digest
    assert (
        compatibility_changed.metadata.content_digest != forward.metadata.content_digest
    )


def test_provisioning_models_are_distinct_from_quota_pools() -> None:
    """Workload modes are not aliases for standard/preemptible quota pools."""
    assert tuple(model.value for model in ProvisioningModel) == (
        "standard",
        "spot",
        "flex-start",
        "reservation-bound",
    )


def test_maintained_catalog_includes_guided_a4_with_supported_consumers() -> None:
    """Maintained mappings retain stable groups, sources, and A4 guidance."""
    groups = {mapping.group_id for mapping in MAINTAINED_ACCELERATOR_OVERLAY.mappings}
    mapping = next(
        item
        for item in MAINTAINED_ACCELERATOR_OVERLAY.mappings
        if item.machine_types == ("a4-highgpu-8g",)
    )

    assert groups == {
        CatalogGroupId.COMPUTE_ACCELERATORS,
        CatalogGroupId.CLOUD_TPU_LEGACY,
    }
    assert all(
        item.source_url.startswith("https://docs.cloud.google.com/")
        and item.reviewed_on == REVIEW_DATE
        for item in MAINTAINED_ACCELERATOR_OVERLAY.mappings
    )
    assert mapping.accelerator_id == AcceleratorId("nvidia-b200")
    assert mapping.provider_accelerator_types == ("nvidia-b200",)
    assert mapping.workload_consumers == (
        WorkloadConsumer.COMPUTE_ENGINE,
        WorkloadConsumer.GKE,
    )
    assert mapping.guided


def test_classification_keeps_discovery_guidance_and_mutability_independent() -> None:
    """Unknown provider truth remains visible without invented catalog semantics."""
    guided = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
    )
    unknown = replace(
        guided,
        identity=replace(guided.identity, quota_id="PROVIDER-FUTURE-QUOTA"),
    )

    guided_item = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        guided, freshly_validated_mutable=False
    )
    unknown_item = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        unknown, freshly_validated_mutable=True
    )

    assert guided_item.predicates.discovered
    assert guided_item.predicates.cataloged
    assert guided_item.predicates.guided
    assert not guided_item.predicates.mutable
    assert unknown_item.predicates.discovered
    assert not unknown_item.predicates.cataloged
    assert not unknown_item.predicates.guided
    assert unknown_item.predicates.mutable


def test_exact_quota_id_is_authoritative_over_provider_display_name() -> None:
    """A renamed display label cannot defeat an exact maintained quota ID."""
    evidence = replace(
        _evidence(
            "GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
            scope=QuotaScope.REGIONAL,
        ),
        quota_display_name="Provider-renamed label",
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
        dimensions=(
            ("gpu_family", "NVIDIA_H100"),
            ("region", "us-central1"),
            ("workload_type", "future"),
        ),
        scope=QuotaScope.REGIONAL,
    )

    item = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        evidence, freshly_validated_mutable=False
    )

    assert item.predicates.discovered
    assert not item.predicates.cataloged
    assert not item.predicates.guided
    assert not item.predicates.mutable


def test_constraint_set_keeps_global_and_regional_slices_independent() -> None:
    """A shared global companion does not combine alternative locations."""
    regional = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
    )
    global_ = _evidence(
        "GPUS-ALL-REGIONS-per-project",
        dimensions=(),
        scope=QuotaScope.GLOBAL,
    )

    result = MAINTAINED_ACCELERATOR_OVERLAY.constraint_set(
        AcceleratorId("nvidia-h100"), regional, (regional, global_)
    )

    assert result is not None
    assert tuple(item.slice_identity for item in result.references) == (
        global_.identity,
        regional.identity,
    )
    with pytest.raises(AmbiguousOverlayMatchError):
        MAINTAINED_ACCELERATOR_OVERLAY.constraint_set(
            AcceleratorId("nvidia-h100"),
            regional,
            (regional, global_, global_),
        )


def test_global_companion_exposes_each_region_anchored_constraint_set() -> None:
    """A shared global slice relates regions without combining alternatives."""
    central = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-central1")),
        scope=QuotaScope.REGIONAL,
    )
    east = _evidence(
        "GPUS-PER-GPU-FAMILY-per-project-region",
        dimensions=(("gpu_family", "NVIDIA_H100"), ("region", "us-east1")),
        scope=QuotaScope.REGIONAL,
    )
    global_ = _evidence(
        "GPUS-ALL-REGIONS-per-project",
        dimensions=(),
        scope=QuotaScope.GLOBAL,
    )

    results = MAINTAINED_ACCELERATOR_OVERLAY.constraint_sets(
        global_,
        (east, global_, central),
    )
    classified = MAINTAINED_ACCELERATOR_OVERLAY.classify(
        global_, freshly_validated_mutable=False
    )

    assert classified.predicates.cataloged
    assert classified.predicates.guided
    assert classified.accelerator_id is None
    assert tuple(
        tuple(reference.slice_identity for reference in result.references)
        for result in results
    ) == (
        (global_.identity, central.identity),
        (global_.identity, east.identity),
    )
