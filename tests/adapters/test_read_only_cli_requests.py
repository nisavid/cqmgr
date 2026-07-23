"""CLI primitive-to-domain parsing contracts for read-only operations."""

from __future__ import annotations

import pytest

from cqmgr.adapters.cli.read_only_requests import (
    parse_cloud_tpu_slice_requirement,
    parse_compute_instance_requirement,
    parse_dimensions,
    parse_quota_query,
    parse_read_only_quota_query,
)
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CandidateLocations,
    ProvisioningModel,
)
from cqmgr.domain.catalog import CatalogGroupId
from cqmgr.domain.quota_queries import QuotaSortField, SortDirection
from cqmgr.domain.quotas import QuotaScope
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    Reconciliation,
)

SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
EXPECTED_INSTANCE_COUNT = 2


def test_quota_query_parses_every_documented_filter_and_sort() -> None:
    """CLI primitives become the normalized complete V1 query contract."""
    query = parse_quota_query(
        SCOPE,
        services=("tpu", "compute.googleapis.com"),
        catalog_groups=("cloud-tpu-legacy", "compute-accelerators"),
        accelerators=("nvidia-h100",),
        locations=("us-central1", "us-east1-b"),
        quota_scopes=("regional", "zonal"),
        quota_pools=("standard", "preemptible"),
        cataloged="true",
        guided="false",
        mutable="true",
        reconciliations=("settled", "reconciling"),
        grant_satisfactions=("full", "partial"),
        effective_confirmations=("confirmed", "stale"),
        text="H100",
        sorts=("service:desc", "quota-id"),
    )

    assert query.services == ("compute.googleapis.com", "tpu.googleapis.com")
    assert query.filters.catalog_groups == (
        CatalogGroupId.CLOUD_TPU_LEGACY,
        CatalogGroupId.COMPUTE_ACCELERATORS,
    )
    assert query.filters.accelerators[0].value == "nvidia-h100"
    assert query.filters.locations == ("us-central1", "us-east1-b")
    assert query.filters.quota_scopes == (QuotaScope.REGIONAL, QuotaScope.ZONAL)
    assert query.filters.quota_pools == ("preemptible", "standard")
    assert query.filters.cataloged is True
    assert query.filters.guided is False
    assert query.filters.mutable is True
    assert query.filters.reconciliations == (
        Reconciliation.RECONCILING,
        Reconciliation.SETTLED,
    )
    assert query.filters.grant_satisfactions == (
        GrantSatisfaction.FULL,
        GrantSatisfaction.PARTIAL,
    )
    assert query.filters.effective_confirmations == (
        EffectiveConfirmation.CONFIRMED,
        EffectiveConfirmation.STALE,
    )
    assert query.filters.text == "H100"
    assert query.sort[0].field is QuotaSortField.SERVICE
    assert query.sort[0].direction is SortDirection.DESC
    assert query.sort[1].field is QuotaSortField.QUOTA_ID
    assert query.sort[1].direction is SortDirection.ASC


def test_query_source_facets_prune_the_inferred_provider_set() -> None:
    """Service shorthand and catalog groups select actual V1 provider subsets."""
    compute = parse_quota_query(SCOPE, services=("compute",))
    legacy_tpu = parse_quota_query(SCOPE, catalog_groups=("cloud-tpu-legacy",))

    assert compute.services == ("compute.googleapis.com",)
    assert legacy_tpu.services == ("tpu.googleapis.com",)


def test_scope_neutral_query_parsing_defers_resource_scope_to_the_facade() -> None:
    """CLI parsing preserves filters and sort without inventing a project."""
    parsed = parse_read_only_quota_query(
        services=("compute",),
        locations=("us-central1",),
        sorts=("quota-id:desc",),
    )

    assert parsed.filters.services == ("compute.googleapis.com",)
    assert parsed.filters.locations == ("us-central1",)
    assert parsed.sort[0].field is QuotaSortField.QUOTA_ID
    assert parsed.sort[0].direction is SortDirection.DESC


@pytest.mark.parametrize(
    ("locations", "all_compatible"),
    [
        (("us-central1-a",), True),
        ((), False),
    ],
)
def test_workload_requirements_require_exactly_one_location_mode(
    locations: tuple[str, ...],
    all_compatible: bool,  # noqa: FBT001
) -> None:
    """Candidate locations and all-compatible search cannot be silently combined."""
    with pytest.raises(ValueError, match="exactly one location mode"):
        parse_compute_instance_requirement(
            machine_type="a3-highgpu-8g",
            instance_count="1",
            provisioning_model="standard",
            locations=locations,
            all_compatible=all_compatible,
        )


def test_workload_parsers_preserve_typed_shapes_and_positive_counts() -> None:
    """Compute and TPU primitives produce typed workload-first requirements."""
    compute = parse_compute_instance_requirement(
        machine_type="a3-highgpu-8g",
        instance_count="2",
        provisioning_model="spot",
        locations=("us-central1-a",),
        all_compatible=False,
    )
    tpu = parse_cloud_tpu_slice_requirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count="1",
        provisioning_model="standard",
        locations=(),
        all_compatible=True,
    )

    assert compute.instance_count == EXPECTED_INSTANCE_COUNT
    assert compute.provisioning_model is ProvisioningModel.SPOT
    assert compute.locations == CandidateLocations(("us-central1-a",))
    assert tpu.slice_count == 1
    assert tpu.locations == AllCompatibleLocations()
    with pytest.raises(ValueError, match="positive integer"):
        parse_cloud_tpu_slice_requirement(
            accelerator_type="v6e-8",
            topology="2x4",
            runtime_version="tpu-vm-base",
            slice_count="0",
            provisioning_model="standard",
            locations=("us-central1-b",),
            all_compatible=False,
        )


def test_dimension_parser_normalizes_and_rejects_malformed_selectors() -> None:
    """Exact dimension primitives retain canonical key ordering without guessing."""
    dimensions = parse_dimensions(("region=us-central1", "gpu_family=NVIDIA_H100"))

    assert dimensions.items == (
        ("gpu_family", "NVIDIA_H100"),
        ("region", "us-central1"),
    )
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_dimensions(("region",))


def test_query_and_workload_parsers_reject_noncanonical_primitives() -> None:
    """Primitive decoders fail closed instead of accepting lossy coercions."""
    with pytest.raises(ValueError, match="cataloged must be true or false"):
        parse_read_only_quota_query(cataloged="maybe")
    with pytest.raises(ValueError, match="sort must use"):
        parse_read_only_quota_query(sorts=("quota-id:",))
    with pytest.raises(ValueError, match="positive integer"):
        parse_compute_instance_requirement(
            machine_type="a3-highgpu-8g",
            instance_count="1.0",
            provisioning_model="standard",
            locations=("us-central1-a",),
            all_compatible=False,
        )
    with pytest.raises(TypeError, match="all-compatible must be boolean"):
        parse_compute_instance_requirement(
            machine_type="a3-highgpu-8g",
            instance_count="1",
            provisioning_model="standard",
            locations=("us-central1-a",),
            all_compatible=object(),  # type: ignore[arg-type]
        )
