"""Independent accelerator-catalog predicate contracts."""

from itertools import product
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cqmgr.domain.catalog import CatalogFilter, CatalogPredicates

PREDICATE_COMBINATION_COUNT = 16


def _predicates(values: tuple[bool, bool, bool, bool]) -> CatalogPredicates:
    """Construct predicates from a compact parametrized truth tuple."""
    discovered, cataloged, guided, mutable = values
    return CatalogPredicates(
        discovered=discovered,
        cataloged=cataloged,
        guided=guided,
        mutable=mutable,
    )


@given(
    discovered=st.booleans(),
    cataloged=st.booleans(),
    guided=st.booleans(),
    mutable=st.booleans(),
)
def test_catalog_predicates_preserve_every_independent_fact(
    *,
    discovered: bool,
    cataloged: bool,
    guided: bool,
    mutable: bool,
) -> None:
    """No catalog fact silently implies or rewrites another fact."""
    predicates = CatalogPredicates(
        discovered=discovered,
        cataloged=cataloged,
        guided=guided,
        mutable=mutable,
    )

    assert (
        predicates.discovered,
        predicates.cataloged,
        predicates.guided,
        predicates.mutable,
    ) == (discovered, cataloged, guided, mutable)


def test_all_catalog_predicate_combinations_remain_distinct() -> None:
    """The model represents all sixteen combinations without collapsing states."""
    combinations = {
        _predicates(cast("tuple[bool, bool, bool, bool]", values))
        for values in product((False, True), repeat=4)
    }

    assert len(combinations) == PREDICATE_COMBINATION_COUNT


@pytest.mark.parametrize(
    ("predicates", "expected"),
    [
        (_predicates((True, False, False, True)), True),
        (_predicates((True, True, False, True)), False),
        (_predicates((True, False, True, True)), False),
        (_predicates((True, False, False, False)), False),
        (_predicates((False, False, False, True)), False),
    ],
)
def test_catalog_filter_combines_selected_facets_with_and(
    predicates: CatalogPredicates,
    *,
    expected: bool,
) -> None:
    """Every selected facet must match; unselected facets remain unconstrained."""
    catalog_filter = CatalogFilter(
        discovered=True,
        cataloged=False,
        guided=False,
        mutable=True,
    )

    assert catalog_filter.matches(predicates) is expected
    assert predicates.matches(catalog_filter) is expected


def test_empty_catalog_filter_matches_every_combination() -> None:
    """An empty filter adds no implied catalog requirements."""
    catalog_filter = CatalogFilter()

    assert all(
        catalog_filter.matches(
            _predicates(cast("tuple[bool, bool, bool, bool]", values))
        )
        for values in product((False, True), repeat=4)
    )


def test_unselected_catalog_facets_remain_unconstrained() -> None:
    """A partial filter ignores omitted facts in both matching directions."""
    catalog_filter = CatalogFilter(discovered=True, mutable=False)
    first = _predicates((True, False, False, False))
    second = _predicates((True, True, True, False))

    assert catalog_filter.matches(first)
    assert catalog_filter.matches(second)
    assert first.matches(catalog_filter)
    assert second.matches(catalog_filter)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_catalog_predicate_rejects_non_boolean_fact(value: object) -> None:
    """Truth-like provider values cannot silently become product facts."""
    with pytest.raises(TypeError, match="must be bool"):
        CatalogPredicates(
            discovered=cast("bool", value),
            cataloged=False,
            guided=False,
            mutable=False,
        )


def test_catalog_filter_rejects_non_boolean_facet() -> None:
    """Selected filter facets require exact booleans or omission."""
    with pytest.raises(TypeError, match="filter must be bool or None"):
        CatalogFilter(guided=cast("bool", "true"))


def test_catalog_matches_rejects_wrong_domain_type() -> None:
    """Catalog matching never coerces unrelated values into predicates."""
    predicates = _predicates((True, False, False, True))
    catalog_filter = CatalogFilter(mutable=True)

    with pytest.raises(TypeError, match="CatalogFilter"):
        predicates.matches(cast("CatalogFilter", object()))
    with pytest.raises(TypeError, match="CatalogPredicates"):
        catalog_filter.matches(cast("CatalogPredicates", object()))
