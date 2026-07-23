"""Bounded logical quota-query and product-snapshot contracts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogGroupId,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.quota_queries import (
    PROVIDER_INVENTORY_REVISION,
    V1_PROVIDER_SERVICES,
    IncompatibleSortUnitsError,
    IncompleteQuerySnapshotError,
    OpaqueQueryCursor,
    ProviderSourceCoverage,
    ProviderSourceCoverageState,
    QuerySnapshotMetadata,
    QuotaQuery,
    QuotaQueryFilters,
    QuotaQueryItem,
    QuotaQuerySnapshot,
    QuotaSort,
    QuotaSortField,
    SortDirection,
)
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    Reconciliation,
)

CURSOR_OFFSET = 25
OBSERVED_AT = datetime(2026, 7, 22, 8, tzinfo=UTC)


def _scope() -> ResourceScope:
    return ResourceScope(ResourceScopeKind.PROJECT, "projects/123")


def test_bare_query_uses_the_fixed_v1_provider_inventory() -> None:
    """An absent source-selecting filter federates both V1 providers."""
    query = QuotaQuery(resource_scope=_scope())

    assert query.services == V1_PROVIDER_SERVICES


def test_service_and_catalog_group_filters_normalize_and_infer_sources() -> None:
    """Input shorthand is accepted while durable query identity stays canonical."""
    generic = QuotaQuery(
        resource_scope=_scope(),
        filters=QuotaQueryFilters(services=("compute", "compute.googleapis.com")),
    )
    grouped = QuotaQuery(
        resource_scope=_scope(),
        filters=QuotaQueryFilters(catalog_groups=(CatalogGroupId.CLOUD_TPU_LEGACY,)),
    )

    assert grouped.filters.catalog_groups == (CatalogGroupId.CLOUD_TPU_LEGACY,)
    assert generic.filters.services == ("compute.googleapis.com",)
    assert generic.services == ("compute.googleapis.com",)
    assert grouped.services == ("tpu.googleapis.com",)


def test_query_rejects_services_outside_the_fixed_inventory() -> None:
    """An arbitrary service cannot broaden the fixed provider inventory."""
    with pytest.raises(ValueError, match="supported V1 provider"):
        QuotaQueryFilters(services=("container.googleapis.com",))


def test_intersecting_source_facets_may_prune_every_provider() -> None:
    """Contradictory source facets are a complete empty query, not guessed input."""
    query = QuotaQuery(
        resource_scope=_scope(),
        filters=QuotaQueryFilters(
            services=("compute",),
            catalog_groups=(CatalogGroupId.CLOUD_TPU_LEGACY,),
        ),
    )

    assert query.services == ()


def test_snapshot_metadata_binds_inventory_and_per_source_coverage() -> None:
    """Coverage distinguishes failed queried evidence from intentional pruning."""
    query = QuotaQuery(
        _scope(),
        filters=QuotaQueryFilters(services=("compute",)),
    )
    metadata = QuerySnapshotMetadata(
        snapshot_id="snapshot-coverage",
        query=query,
        catalog=CatalogMetadata(
            ACCELERATOR_CATALOG_SCHEMA,
            "2026-07-22",
            "sha256:" + "b" * 64,
        ),
        evidence_contract="cqmgr.quota-query-evidence/v1",
        observed_at=OBSERVED_AT,
        expires_at=OBSERVED_AT + timedelta(minutes=15),
        complete=True,
        inventory_revision=PROVIDER_INVENTORY_REVISION,
        source_coverage=(
            ProviderSourceCoverage.complete(
                "compute.googleapis.com",
                pages_attempted=2,
                pages_completed=2,
                observed_at=OBSERVED_AT,
            ),
            ProviderSourceCoverage.intentionally_unqueried("tpu.googleapis.com"),
        ),
    )

    assert metadata.queried_services == ("compute.googleapis.com",)
    assert metadata.source_coverage[1].state is (
        ProviderSourceCoverageState.INTENTIONALLY_UNQUERIED
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
    usage: int | None = None,
    desired: int | None = None,
    granted: int | None = None,
    reconciliation: Reconciliation = Reconciliation.UNKNOWN,
    grant_satisfaction: GrantSatisfaction = GrantSatisfaction.UNKNOWN,
    effective_confirmation: EffectiveConfirmation = EffectiveConfirmation.UNOBSERVED,
    evidence_observed_at: datetime = OBSERVED_AT,
    catalog_groups: tuple[CatalogGroupId, ...] = (),
) -> QuotaQueryItem:
    quota_unit = QuotaUnit(unit)
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
            None if effective is None else QuotaQuantity(effective, quota_unit)
        ),
        usage_value=None if usage is None else QuotaQuantity(usage, quota_unit),
        desired_value=None if desired is None else QuotaQuantity(desired, quota_unit),
        granted_value=None if granted is None else QuotaQuantity(granted, quota_unit),
        reconciliation=reconciliation,
        grant_satisfaction=grant_satisfaction,
        effective_confirmation=effective_confirmation,
        evidence_observed_at=evidence_observed_at,
        catalog_groups=catalog_groups,
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


def test_repeatable_filter_facets_have_one_canonical_query_identity() -> None:
    """Order and duplicate OR alternatives cannot change cursor-bound identity."""
    expected = QuotaQueryFilters(
        accelerators=(AcceleratorId("nvidia-l4"), AcceleratorId("nvidia-h100")),
        locations=("us-east1", "us-central1", "us-east1"),
        quota_scopes=(QuotaScope.ZONAL, QuotaScope.GLOBAL, QuotaScope.ZONAL),
        quota_pools=("spot", "standard", "spot"),
        reconciliations=(
            Reconciliation.FAILED,
            Reconciliation.SETTLED,
            Reconciliation.FAILED,
        ),
        grant_satisfactions=(
            GrantSatisfaction.PARTIAL,
            GrantSatisfaction.FULL,
            GrantSatisfaction.PARTIAL,
        ),
        effective_confirmations=(
            EffectiveConfirmation.CONFIRMED,
            EffectiveConfirmation.UNOBSERVED,
            EffectiveConfirmation.CONFIRMED,
        ),
    )
    reordered = QuotaQueryFilters(
        accelerators=(AcceleratorId("nvidia-h100"), AcceleratorId("nvidia-l4")),
        locations=("us-central1", "us-east1"),
        quota_scopes=(QuotaScope.GLOBAL, QuotaScope.ZONAL),
        quota_pools=("standard", "spot"),
        reconciliations=(Reconciliation.SETTLED, Reconciliation.FAILED),
        grant_satisfactions=(
            GrantSatisfaction.FULL,
            GrantSatisfaction.PARTIAL,
        ),
        effective_confirmations=(
            EffectiveConfirmation.UNOBSERVED,
            EffectiveConfirmation.CONFIRMED,
        ),
    )

    assert expected == reordered


def _metadata(query: QuotaQuery, *, complete: bool = True) -> QuerySnapshotMetadata:
    coverage = tuple(
        (
            ProviderSourceCoverage.complete(
                service,
                pages_attempted=1,
                pages_completed=1,
                observed_at=OBSERVED_AT,
            )
            if complete
            else ProviderSourceCoverage.incomplete(
                service,
                pages_attempted=1,
                pages_completed=0,
                observed_at=OBSERVED_AT,
            )
        )
        if service in query.services
        else ProviderSourceCoverage.intentionally_unqueried(service)
        for service in V1_PROVIDER_SERVICES
    )
    return QuerySnapshotMetadata(
        snapshot_id="snapshot-opaque-1",
        query=query,
        catalog=CatalogMetadata(
            ACCELERATOR_CATALOG_SCHEMA,
            "2026-07-22",
            "sha256:" + "b" * 64,
        ),
        evidence_contract="cqmgr.quota-query-evidence/v1",
        observed_at=OBSERVED_AT,
        expires_at=OBSERVED_AT + timedelta(minutes=15),
        complete=complete,
        source_coverage=coverage,
    )


def test_snapshot_metadata_binds_evidence_contract_and_bounded_lifetime() -> None:
    """A product cursor cannot outlive or silently change its evidence contract."""
    query = QuotaQuery(_scope(), filters=QuotaQueryFilters(services=("compute",)))
    metadata = _metadata(query)

    assert metadata.evidence_contract == "cqmgr.quota-query-evidence/v1"
    assert metadata.expires_at > metadata.observed_at
    with pytest.raises(ValueError, match="expires_at"):
        QuerySnapshotMetadata(
            snapshot_id=metadata.snapshot_id,
            query=query,
            catalog=metadata.catalog,
            evidence_contract=metadata.evidence_contract,
            observed_at=metadata.observed_at,
            expires_at=metadata.observed_at,
            complete=True,
            source_coverage=metadata.source_coverage,
        )


def test_sort_requires_complete_snapshot_and_uses_deterministic_text_ties() -> None:
    """NFC/casefold text ordering falls back to raw text then exact identity."""
    query = QuotaQuery(
        resource_scope=_scope(),
        filters=QuotaQueryFilters(services=("compute",)),
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
        filters=QuotaQueryFilters(services=("compute",)),
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
        filters=QuotaQueryFilters(
            catalog_groups=(CatalogGroupId.COMPUTE_ACCELERATORS,)
        ),
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
                catalog_groups=(CatalogGroupId.COMPUTE_ACCELERATORS,),
            ),
            _item(
                "tpu",
                display_name="TPU",
                accelerator=None,
                location="us-central1",
                unit="chip",
                catalog_groups=(CatalogGroupId.COMPUTE_ACCELERATORS,),
            ),
        ),
    )

    with pytest.raises(IncompatibleSortUnitsError, match="native unit"):
        snapshot.sorted_items()


def test_status_filters_and_all_public_sort_fields_share_the_query_contract() -> None:
    """Request axes and quantities remain filterable and deterministically sortable."""
    filters = QuotaQueryFilters(
        reconciliations=(Reconciliation.SETTLED,),
        grant_satisfactions=(GrantSatisfaction.FULL,),
        effective_confirmations=(EffectiveConfirmation.CONFIRMED,),
    )
    query = QuotaQuery(
        resource_scope=_scope(),
        filters=filters,
        sort=(
            QuotaSort(QuotaSortField.USAGE, SortDirection.DESC),
            QuotaSort(QuotaSortField.DESIRED),
            QuotaSort(QuotaSortField.GRANTED),
            QuotaSort(QuotaSortField.RECONCILIATION),
            QuotaSort(QuotaSortField.GRANT_SATISFACTION),
            QuotaSort(QuotaSortField.EFFECTIVE_CONFIRMATION),
            QuotaSort(QuotaSortField.EVIDENCE_AGE),
        ),
    )
    matching = _item(
        "matching",
        display_name="Matching",
        accelerator=None,
        location="us-central1",
        usage=8,
        desired=16,
        granted=16,
        reconciliation=Reconciliation.SETTLED,
        grant_satisfaction=GrantSatisfaction.FULL,
        effective_confirmation=EffectiveConfirmation.CONFIRMED,
    )
    unobserved = _item(
        "unobserved",
        display_name="Unobserved",
        accelerator=None,
        location="us-central1",
    )

    assert filters.matches(matching)
    assert not filters.matches(unobserved)
    snapshot = QuotaQuerySnapshot(_metadata(query), (unobserved, matching))
    assert snapshot.sorted_items() == (matching,)


def test_evidence_age_sorts_older_evidence_after_fresher_evidence() -> None:
    """Evidence age uses the snapshot observation time and keeps unknown age last."""
    query = QuotaQuery(
        resource_scope=_scope(),
        filters=QuotaQueryFilters(services=("compute",)),
        sort=(QuotaSort(QuotaSortField.EVIDENCE_AGE),),
    )
    fresh = _item(
        "fresh",
        display_name="Fresh",
        accelerator=None,
        location="us-central1",
        evidence_observed_at=OBSERVED_AT,
    )
    old = _item(
        "old",
        display_name="Old",
        accelerator=None,
        location="us-central1",
        evidence_observed_at=OBSERVED_AT - timedelta(hours=2),
    )

    assert [
        item.identity.quota_id
        for item in QuotaQuerySnapshot(_metadata(query), (old, fresh)).sorted_items()
    ] == ["fresh", "old"]


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


def test_query_item_retains_its_anchored_constraint_set() -> None:
    """Snapshot rows preserve related exact identities for cursor continuation."""
    item = _item(
        "H100",
        display_name="NVIDIA H100",
        accelerator="nvidia-h100",
        location="us-central1",
    )
    constraint_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-h100"),
        (ConstraintReference(item.identity),),
    )

    retained = replace(item, constraint_set=constraint_set)

    assert retained.constraint_set == constraint_set
    with pytest.raises(ValueError, match="accelerator"):
        replace(
            item,
            constraint_set=AcceleratorConstraintSet(
                AcceleratorId("nvidia-l4"),
                (ConstraintReference(item.identity),),
            ),
        )


def test_query_item_retains_multiple_anchored_sets_without_singular_guess() -> None:
    """A shared global companion preserves each regional constraint set."""
    regional_central = _item(
        "H100-CENTRAL",
        display_name="NVIDIA H100",
        accelerator="nvidia-h100",
        location="us-central1",
    )
    regional_east = _item(
        "H100-EAST",
        display_name="NVIDIA H100",
        accelerator="nvidia-h100",
        location="us-east1",
    )
    global_item = replace(
        regional_central,
        identity=replace(
            regional_central.identity,
            quota_id="GPU-GLOBAL",
            dimensions=NormalizedDimensions(()),
            quota_scope=QuotaScope.GLOBAL,
        ),
        location="global",
    )
    central_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-h100"),
        (
            ConstraintReference(global_item.identity),
            ConstraintReference(regional_central.identity),
        ),
    )
    east_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-h100"),
        (
            ConstraintReference(global_item.identity),
            ConstraintReference(regional_east.identity),
        ),
    )

    retained = replace(
        global_item,
        constraint_sets=(east_set, central_set),
        constraint_set=None,
    )

    assert retained.constraint_sets == (central_set, east_set)
    assert retained.constraint_set is None
    with pytest.raises(TypeError, match="constraint_sets"):
        replace(global_item, constraint_sets=[central_set])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="constraint_sets"):
        replace(global_item, constraint_sets=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ambiguous"):
        replace(
            global_item,
            constraint_sets=(central_set, east_set),
            constraint_set=central_set,
        )
    with pytest.raises(ValueError, match="must not repeat"):
        replace(global_item, constraint_sets=(central_set, central_set))
    with pytest.raises(ValueError, match="accelerator must match"):
        replace(
            global_item,
            constraint_sets=(
                AcceleratorConstraintSet(
                    AcceleratorId("nvidia-l4"),
                    (ConstraintReference(global_item.identity),),
                ),
            ),
        )
    with pytest.raises(ValueError, match="must reference the query item"):
        replace(
            global_item,
            constraint_sets=(
                AcceleratorConstraintSet(
                    AcceleratorId("nvidia-h100"),
                    (ConstraintReference(regional_central.identity),),
                ),
            ),
        )
