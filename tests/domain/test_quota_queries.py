"""Bounded logical quota-query and product-snapshot contracts."""

from typing import cast

import pytest

from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorId,
    CatalogGroupId,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.quota_queries import (
    CatalogGroupSource,
    IncompatibleSortUnitsError,
    IncompleteQuerySnapshotError,
    OpaqueQueryCursor,
    QuerySnapshotMetadata,
    QuotaQuery,
    QuotaQueryFilters,
    QuotaQueryItem,
    QuotaQuerySnapshot,
    QuotaSort,
    QuotaSortField,
    ServiceSource,
    SortDirection,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

CURSOR_OFFSET = 25


def _scope() -> ResourceScope:
    return ResourceScope(ResourceScopeKind.PROJECT, "projects/123")


def test_query_has_exactly_one_typed_service_or_catalog_group_source() -> None:
    """Source selection stays independent from repeatable service filters."""
    generic = QuotaQuery(
        resource_scope=_scope(),
        source=ServiceSource("compute.googleapis.com"),
        filters=QuotaQueryFilters(services=("compute.googleapis.com",)),
    )
    grouped = QuotaQuery(
        resource_scope=_scope(),
        source=CatalogGroupSource(CatalogGroupId.COMPUTE_ACCELERATORS),
        filters=QuotaQueryFilters(services=("compute.googleapis.com",)),
    )

    assert generic.source == ServiceSource("compute.googleapis.com")
    assert grouped.source == CatalogGroupSource(CatalogGroupId.COMPUTE_ACCELERATORS)
    assert grouped.filters.services == ("compute.googleapis.com",)


def test_query_rejects_untyped_or_noncanonical_source() -> None:
    """A query never infers a source from filters or arbitrary text."""
    with pytest.raises(ValueError, match="canonical service DNS"):
        ServiceSource("Compute.GoogleApis.com")
    with pytest.raises(TypeError, match="ServiceSource or CatalogGroupSource"):
        QuotaQuery(
            resource_scope=_scope(),
            source=cast("ServiceSource", "compute.googleapis.com"),
        )


def test_query_rejects_filters_outside_the_selected_source() -> None:
    """A source cannot silently broaden into another provider or catalog group."""
    with pytest.raises(ValueError, match="selected service source"):
        QuotaQuery(
            resource_scope=_scope(),
            source=ServiceSource("compute.googleapis.com"),
            filters=QuotaQueryFilters(services=("tpu.googleapis.com",)),
        )
    with pytest.raises(ValueError, match="selected catalog group"):
        QuotaQuery(
            resource_scope=_scope(),
            source=CatalogGroupSource(CatalogGroupId.COMPUTE_ACCELERATORS),
            filters=QuotaQueryFilters(services=("tpu.googleapis.com",)),
        )


def test_query_rejects_noncanonical_locations() -> None:
    """Location facets accept provider location IDs rather than arbitrary text."""
    with pytest.raises(ValueError, match="canonical location"):
        QuotaQueryFilters(locations=("us central1",))


def _item(  # noqa: PLR0913
    quota_id: str,
    *,
    display_name: str | None,
    accelerator: str | None,
    location: str,
    unit: str = "count",
    effective: int | None = 1,
    cataloged: bool = True,
) -> QuotaQueryItem:
    return QuotaQueryItem(
        identity=EffectiveQuotaSliceIdentity(
            resource_scope=_scope(),
            service="compute.googleapis.com",
            quota_id=quota_id,
            dimensions=NormalizedDimensions((("region", location),)),
            quota_scope=QuotaScope.REGIONAL,
        ),
        display_name=display_name,
        accelerator_id=None if accelerator is None else AcceleratorId(accelerator),
        location=location,
        quota_pool="standard",
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=cataloged,
            guided=cataloged,
            mutable=False,
        ),
        effective_value=(
            None if effective is None else QuotaQuantity(effective, QuotaUnit(unit))
        ),
    )


def test_repeatable_filter_values_are_or_and_distinct_facets_are_and() -> None:
    """Accelerator alternatives do not weaken the independent location facet."""
    filters = QuotaQueryFilters(
        accelerators=(AcceleratorId("nvidia-h100"), AcceleratorId("nvidia-l4")),
        locations=("us-central1",),
        cataloged=True,
    )

    assert filters.matches(
        _item(
            "H100",
            display_name="NVIDIA H100",
            accelerator="nvidia-h100",
            location="us-central1",
        )
    )
    assert filters.matches(
        _item(
            "L4",
            display_name="NVIDIA L4",
            accelerator="nvidia-l4",
            location="us-central1",
        )
    )
    assert not filters.matches(
        _item(
            "H100",
            display_name="NVIDIA H100",
            accelerator="nvidia-h100",
            location="us-east1",
        )
    )


def _metadata(query: QuotaQuery, *, complete: bool = True) -> QuerySnapshotMetadata:
    return QuerySnapshotMetadata(
        snapshot_id="snapshot-opaque-1",
        query=query,
        catalog=CatalogMetadata(
            ACCELERATOR_CATALOG_SCHEMA,
            "2026-07-22",
            "sha256:" + "b" * 64,
        ),
        complete=complete,
    )


def test_sort_requires_complete_snapshot_and_uses_deterministic_text_ties() -> None:
    """NFC/casefold text ordering falls back to raw text then exact identity."""
    query = QuotaQuery(
        resource_scope=_scope(),
        source=ServiceSource("compute.googleapis.com"),
        sort=(QuotaSort(QuotaSortField.DISPLAY_NAME, SortDirection.ASC),),
    )
    rows = (
        _item("z", display_name=None, accelerator=None, location="us-central1"),
        _item("b", display_name="éclair", accelerator=None, location="us-central1"),
        _item("a", display_name="Éclair", accelerator=None, location="us-central1"),
    )

    with pytest.raises(IncompleteQuerySnapshotError, match="complete"):
        QuotaQuerySnapshot(_metadata(query, complete=False), rows).sorted_items()

    sorted_rows = QuotaQuerySnapshot(_metadata(query), rows).sorted_items()

    assert [row.identity.quota_id for row in sorted_rows] == ["a", "b", "z"]


def test_descending_sort_keeps_missing_values_last() -> None:
    """Direction changes known-value order without promoting missing evidence."""
    query = QuotaQuery(
        resource_scope=_scope(),
        source=ServiceSource("compute.googleapis.com"),
        sort=(QuotaSort(QuotaSortField.EFFECTIVE, SortDirection.DESC),),
    )
    rows = (
        _item(
            "missing",
            display_name="Missing",
            accelerator=None,
            location="us-central1",
            effective=None,
        ),
        _item(
            "one",
            display_name="One",
            accelerator=None,
            location="us-central1",
            effective=1,
        ),
        _item(
            "two",
            display_name="Two",
            accelerator=None,
            location="us-central1",
            effective=2,
        ),
    )

    sorted_rows = QuotaQuerySnapshot(_metadata(query), rows).sorted_items()

    assert [row.identity.quota_id for row in sorted_rows] == ["two", "one", "missing"]


def test_numeric_sort_rejects_comparison_across_native_units() -> None:
    """A global sort never orders core, chip, or card counts as one quantity."""
    query = QuotaQuery(
        resource_scope=_scope(),
        source=CatalogGroupSource(CatalogGroupId.COMPUTE_ACCELERATORS),
        sort=(QuotaSort(QuotaSortField.EFFECTIVE, SortDirection.DESC),),
    )
    snapshot = QuotaQuerySnapshot(
        _metadata(query),
        (
            _item(
                "gpu",
                display_name="GPU",
                accelerator=None,
                location="us-central1",
                unit="card",
            ),
            _item(
                "tpu",
                display_name="TPU",
                accelerator=None,
                location="us-central1",
                unit="chip",
            ),
        ),
    )

    with pytest.raises(IncompatibleSortUnitsError, match="native unit"):
        snapshot.sorted_items()


def test_opaque_cursor_metadata_binds_snapshot_and_offset_without_provider_token() -> (
    None
):
    """The public cursor is an opaque product value, not provider continuation."""
    cursor = OpaqueQueryCursor(
        value="cqmgr-cursor-opaque-value",
        snapshot_id="snapshot-opaque-1",
        offset=CURSOR_OFFSET,
    )

    assert cursor.value == "cqmgr-cursor-opaque-value"
    assert cursor.snapshot_id == "snapshot-opaque-1"
    assert cursor.offset == CURSOR_OFFSET
