"""Independent accelerator-catalog facts and conjunctive filtering."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class CatalogPredicates:
    """Four independent facts about one discovered quota slice."""

    discovered: bool
    cataloged: bool
    guided: bool
    mutable: bool

    def __post_init__(self) -> None:
        """Require explicit product booleans without truth-value coercion."""
        for field in fields(self):
            if not isinstance(getattr(self, field.name), bool):
                msg = f"{field.name} must be bool"
                raise TypeError(msg)

    def matches(self, catalog_filter: CatalogFilter) -> bool:
        """Return whether every selected catalog facet matches this value."""
        if not isinstance(catalog_filter, CatalogFilter):
            msg = "catalog_filter must be a CatalogFilter"
            raise TypeError(msg)
        return catalog_filter.matches(self)


@dataclass(frozen=True, slots=True)
class CatalogFilter:
    """Optional catalog facets combined using logical conjunction."""

    discovered: bool | None = None
    cataloged: bool | None = None
    guided: bool | None = None
    mutable: bool | None = None

    def __post_init__(self) -> None:
        """Reject truth-like values instead of coercing provider data."""
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None and not isinstance(value, bool):
                msg = f"{field.name} filter must be bool or None"
                raise TypeError(msg)

    def matches(self, predicates: CatalogPredicates) -> bool:
        """Return whether all selected facets equal their independent facts."""
        if not isinstance(predicates, CatalogPredicates):
            msg = "predicates must be CatalogPredicates"
            raise TypeError(msg)
        return all(
            expected is None or expected is getattr(predicates, field.name)
            for field in fields(self)
            for expected in (getattr(self, field.name),)
        )
