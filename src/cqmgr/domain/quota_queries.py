"""Provider-neutral bounded logical quota queries and product snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import cmp_to_key
from unicodedata import normalize

from cqmgr.domain.catalog import (
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogGroupId,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.diagnostics import DiagnosticCode
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    QuotaQuantity,
    QuotaScope,
)
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    Reconciliation,
)
from cqmgr.domain.time import require_utc

_MINIMUM_DNS_LABELS = 2
QUOTA_QUERY_EVIDENCE_CONTRACT = "cqmgr.quota-query-evidence/v1"
PROVIDER_INVENTORY_REVISION = "cqmgr.provider-inventory/v1"
V1_PROVIDER_SERVICES = (
    "compute.googleapis.com",
    "tpu.googleapis.com",
)
_SERVICE_SHORTHANDS = {
    "compute": "compute.googleapis.com",
    "tpu": "tpu.googleapis.com",
}
_CATALOG_GROUP_SERVICES = {
    CatalogGroupId.COMPUTE_ACCELERATORS: frozenset({"compute.googleapis.com"}),
    CatalogGroupId.CLOUD_TPU_LEGACY: frozenset({"tpu.googleapis.com"}),
}


class IncompleteQuerySnapshotError(ValueError):
    """Raised when global product sorting lacks complete collected evidence."""


class IncompatibleSortUnitsError(ValueError):
    """Raised when a numeric sort would compare distinct native quota units."""


class SortDirection(StrEnum):
    """Stable public sort direction."""

    ASC = "asc"
    DESC = "desc"


class QuotaSortField(StrEnum):
    """Provider-neutral quota fields supported by the domain snapshot."""

    QUOTA_ID = "quota-id"
    DISPLAY_NAME = "display-name"
    SERVICE = "service"
    ACCELERATOR = "accelerator"
    LOCATION = "location"
    QUOTA_SCOPE = "quota-scope"
    QUOTA_POOL = "quota-pool"
    EFFECTIVE = "effective"
    USAGE = "usage"
    DESIRED = "desired"
    GRANTED = "granted"
    RECONCILIATION = "reconciliation"
    GRANT_SATISFACTION = "grant-satisfaction"
    EFFECTIVE_CONFIRMATION = "effective-confirmation"
    EVIDENCE_AGE = "evidence-age"


@dataclass(frozen=True, slots=True)
class QuotaSort:
    """One ordered sort priority."""

    field: QuotaSortField
    direction: SortDirection = SortDirection.ASC

    def __post_init__(self) -> None:
        """Require public sort field and direction values."""
        if not isinstance(self.field, QuotaSortField):
            msg = "sort field must be a QuotaSortField"
            raise TypeError(msg)
        if not isinstance(self.direction, SortDirection):
            msg = "sort direction must be a SortDirection"
            raise TypeError(msg)


class ProviderSourceCoverageState(StrEnum):
    """Collection disposition for one fixed V1 provider source."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INTENTIONALLY_UNQUERIED = "intentionally-unqueried"


@dataclass(frozen=True, slots=True)
class ProviderSourceCoverage:
    """Page coverage and observation time for one fixed provider source."""

    service: str
    state: ProviderSourceCoverageState
    pages_attempted: int
    pages_completed: int
    observed_at: datetime | None
    page_cap_reached: bool = False
    diagnostic_codes: tuple[DiagnosticCode, ...] = ()

    def __post_init__(self) -> None:  # noqa: C901
        """Keep queried evidence distinct from intentional provider pruning."""
        if self.service not in V1_PROVIDER_SERVICES:
            msg = "source coverage service must be a supported V1 provider"
            raise ValueError(msg)
        if not isinstance(self.state, ProviderSourceCoverageState):
            msg = "source coverage state must be ProviderSourceCoverageState"
            raise TypeError(msg)
        counts = (self.pages_attempted, self.pages_completed)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in counts
        ):
            msg = "source coverage page counts must be integers"
            raise TypeError(msg)
        if not 0 <= self.pages_completed <= self.pages_attempted:
            msg = "source coverage pages must satisfy completed <= attempted"
            raise ValueError(msg)
        if self.state is ProviderSourceCoverageState.INTENTIONALLY_UNQUERIED:
            if (
                counts != (0, 0)
                or self.observed_at is not None
                or self.page_cap_reached
                or self.diagnostic_codes
            ):
                msg = "intentionally unqueried source must contain no read evidence"
                raise ValueError(msg)
        elif self.observed_at is None:
            msg = "queried source coverage requires an observation time"
            raise ValueError(msg)
        else:
            require_utc(self.observed_at, "source coverage observed_at")
        if self.state is ProviderSourceCoverageState.COMPLETE and (
            self.pages_attempted != self.pages_completed
            or self.page_cap_reached
            or self.diagnostic_codes
        ):
            msg = "complete source coverage cannot retain evidence gaps"
            raise ValueError(msg)
        if not isinstance(self.page_cap_reached, bool):
            msg = "source coverage page_cap_reached must be bool"
            raise TypeError(msg)
        if not isinstance(self.diagnostic_codes, tuple) or any(
            not isinstance(code, DiagnosticCode) for code in self.diagnostic_codes
        ):
            msg = "source coverage diagnostic codes must be typed"
            raise TypeError(msg)
        if tuple(sorted(set(self.diagnostic_codes), key=lambda code: code.value)) != (
            self.diagnostic_codes
        ):
            msg = "source coverage diagnostic codes must be unique canonical values"
            raise ValueError(msg)

    @classmethod
    def complete(  # noqa: PLR0913
        cls,
        service: str,
        *,
        pages_attempted: int,
        pages_completed: int,
        observed_at: datetime,
        page_cap_reached: bool = False,
        diagnostic_codes: tuple[DiagnosticCode, ...] = (),
    ) -> ProviderSourceCoverage:
        """Construct complete queried-provider coverage."""
        return cls(
            service,
            ProviderSourceCoverageState.COMPLETE,
            pages_attempted,
            pages_completed,
            observed_at,
            page_cap_reached,
            diagnostic_codes,
        )

    @classmethod
    def incomplete(  # noqa: PLR0913
        cls,
        service: str,
        *,
        pages_attempted: int,
        pages_completed: int,
        observed_at: datetime,
        page_cap_reached: bool = False,
        diagnostic_codes: tuple[DiagnosticCode, ...] = (),
    ) -> ProviderSourceCoverage:
        """Construct incomplete but usable queried-provider coverage."""
        return cls(
            service,
            ProviderSourceCoverageState.INCOMPLETE,
            pages_attempted,
            pages_completed,
            observed_at,
            page_cap_reached,
            diagnostic_codes,
        )

    @classmethod
    def intentionally_unqueried(cls, service: str) -> ProviderSourceCoverage:
        """Construct explicit evidence that a fixed provider was pruned."""
        return cls(
            service,
            ProviderSourceCoverageState.INTENTIONALLY_UNQUERIED,
            0,
            0,
            None,
        )


@dataclass(frozen=True, slots=True)
class QuotaQueryFilters:
    """Repeatable query facets whose values are OR alternatives."""

    services: tuple[str, ...] = ()
    catalog_groups: tuple[CatalogGroupId, ...] = ()
    accelerators: tuple[AcceleratorId, ...] = ()
    locations: tuple[str, ...] = ()
    quota_scopes: tuple[QuotaScope, ...] = ()
    quota_pools: tuple[str, ...] = ()
    cataloged: bool | None = None
    guided: bool | None = None
    mutable: bool | None = None
    reconciliations: tuple[Reconciliation, ...] = ()
    grant_satisfactions: tuple[GrantSatisfaction, ...] = ()
    effective_confirmations: tuple[EffectiveConfirmation, ...] = ()
    text: str | None = None

    def __post_init__(self) -> None:  # noqa: C901
        """Normalize repeatable OR alternatives into one durable query identity."""
        if not isinstance(self.services, tuple):
            msg = "service filters must be a tuple"
            raise TypeError(msg)
        object.__setattr__(
            self,
            "services",
            tuple(
                sorted({_canonical_service_selector(value) for value in self.services})
            ),
        )
        if not isinstance(self.catalog_groups, tuple) or any(
            not isinstance(group, CatalogGroupId) for group in self.catalog_groups
        ):
            msg = "catalog group filters must contain CatalogGroupId values"
            raise TypeError(msg)
        object.__setattr__(
            self,
            "catalog_groups",
            tuple(sorted(set(self.catalog_groups), key=lambda value: value.value)),
        )
        if not isinstance(self.accelerators, tuple) or any(
            not isinstance(accelerator, AcceleratorId)
            for accelerator in self.accelerators
        ):
            msg = "accelerator filters must be AcceleratorId values"
            raise TypeError(msg)
        object.__setattr__(
            self,
            "accelerators",
            tuple(sorted(set(self.accelerators), key=lambda value: value.value)),
        )
        if not isinstance(self.locations, tuple) or any(
            not _is_canonical_location(location) for location in self.locations
        ):
            msg = "location filters must be canonical location values"
            raise ValueError(msg)
        object.__setattr__(self, "locations", tuple(sorted(set(self.locations))))
        if not isinstance(self.quota_scopes, tuple) or any(
            not isinstance(scope, QuotaScope) for scope in self.quota_scopes
        ):
            msg = "quota scope filters must be QuotaScope values"
            raise TypeError(msg)
        object.__setattr__(
            self,
            "quota_scopes",
            tuple(sorted(set(self.quota_scopes), key=lambda value: value.value)),
        )
        if not isinstance(self.quota_pools, tuple) or any(
            not _is_stable_id(pool) for pool in self.quota_pools
        ):
            msg = "quota pool filters must be lowercase identifiers"
            raise ValueError(msg)
        object.__setattr__(self, "quota_pools", tuple(sorted(set(self.quota_pools))))
        for name, value in (
            ("cataloged", self.cataloged),
            ("guided", self.guided),
            ("mutable", self.mutable),
        ):
            if value is not None and not isinstance(value, bool):
                msg = f"{name} filter must be bool or None"
                raise TypeError(msg)
        _require_status_filter_types(
            (
                ("reconciliations", self.reconciliations, Reconciliation),
                ("grant_satisfactions", self.grant_satisfactions, GrantSatisfaction),
                (
                    "effective_confirmations",
                    self.effective_confirmations,
                    EffectiveConfirmation,
                ),
            )
        )
        for field_name in (
            "reconciliations",
            "grant_satisfactions",
            "effective_confirmations",
        ):
            values = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                tuple(sorted(set(values), key=lambda value: value.value)),
            )
        if self.text is not None and (
            not isinstance(self.text, str) or not normalize("NFC", self.text)
        ):
            msg = "text filter must be non-empty Unicode text"
            raise ValueError(msg)
        if self.text is not None:
            object.__setattr__(self, "text", normalize("NFC", self.text))

    def matches(self, item: QuotaQueryItem) -> bool:
        """Combine OR alternatives within facets and AND across facets."""
        if not isinstance(item, QuotaQueryItem):
            msg = "query item must be a QuotaQueryItem"
            raise TypeError(msg)
        checks = (
            not self.services or item.identity.service in self.services,
            not self.catalog_groups
            or bool(set(self.catalog_groups).intersection(item.catalog_groups)),
            not self.accelerators
            or bool(set(self.accelerators).intersection(_item_accelerator_ids(item))),
            not self.locations or item.location in self.locations,
            not self.quota_scopes or item.identity.quota_scope in self.quota_scopes,
            not self.quota_pools or item.quota_pool in self.quota_pools,
            self.cataloged is None or item.predicates.cataloged is self.cataloged,
            self.guided is None or item.predicates.guided is self.guided,
            self.mutable is None or item.predicates.mutable is self.mutable,
            not self.reconciliations or item.reconciliation in self.reconciliations,
            not self.grant_satisfactions
            or item.grant_satisfaction in self.grant_satisfactions,
            not self.effective_confirmations
            or item.effective_confirmation in self.effective_confirmations,
            self.text is None or _matches_text(item, self.text),
        )
        return all(checks)


def _item_accelerator_ids(item: QuotaQueryItem) -> frozenset[AcceleratorId]:
    """Return every accelerator identity represented by one exact slice."""
    values = {constraint_set.accelerator_id for constraint_set in item.constraint_sets}
    if item.accelerator_id is not None:
        values.add(item.accelerator_id)
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class QuotaQueryItem:
    """One exact quota slice with product metadata used by browse queries."""

    identity: EffectiveQuotaSliceIdentity
    display_name: str | None
    accelerator_id: AcceleratorId | None
    location: str | None
    quota_pool: str | None
    predicates: CatalogPredicates
    effective_value: QuotaQuantity | None
    usage_value: QuotaQuantity | None = None
    desired_value: QuotaQuantity | None = None
    granted_value: QuotaQuantity | None = None
    reconciliation: Reconciliation = Reconciliation.UNKNOWN
    grant_satisfaction: GrantSatisfaction = GrantSatisfaction.UNKNOWN
    effective_confirmation: EffectiveConfirmation = EffectiveConfirmation.UNOBSERVED
    evidence_observed_at: datetime | None = None
    constraint_sets: tuple[AcceleratorConstraintSet, ...] = ()
    constraint_set: AcceleratorConstraintSet | None = None
    catalog_groups: tuple[CatalogGroupId, ...] = ()

    def __post_init__(self) -> None:
        """Keep optional product metadata separate from canonical slice identity."""
        if not isinstance(self.identity, EffectiveQuotaSliceIdentity):
            msg = "query item identity must be an EffectiveQuotaSliceIdentity"
            raise TypeError(msg)
        for name, value in (("display_name", self.display_name),):
            if value is not None and (not isinstance(value, str) or not value):
                msg = f"{name} must be non-empty text or None"
                raise ValueError(msg)
        if self.accelerator_id is not None and not isinstance(
            self.accelerator_id, AcceleratorId
        ):
            msg = "accelerator_id must be an AcceleratorId or None"
            raise TypeError(msg)
        if self.location is not None and not _is_canonical_location(self.location):
            msg = "location must be canonical or None"
            raise ValueError(msg)
        if self.quota_pool is not None and not _is_stable_id(self.quota_pool):
            msg = "quota_pool must be a lowercase identifier or None"
            raise ValueError(msg)
        if not isinstance(self.predicates, CatalogPredicates):
            msg = "predicates must be CatalogPredicates"
            raise TypeError(msg)
        _require_query_item_quantities(
            (
                ("effective_value", self.effective_value),
                ("usage_value", self.usage_value),
                ("desired_value", self.desired_value),
                ("granted_value", self.granted_value),
            )
        )
        _require_query_item_status_types(
            (
                ("reconciliation", self.reconciliation, Reconciliation),
                ("grant_satisfaction", self.grant_satisfaction, GrantSatisfaction),
                (
                    "effective_confirmation",
                    self.effective_confirmation,
                    EffectiveConfirmation,
                ),
            )
        )
        if self.evidence_observed_at is not None:
            require_utc(self.evidence_observed_at, "evidence_observed_at")
        if not isinstance(self.catalog_groups, tuple) or any(
            not isinstance(group, CatalogGroupId) for group in self.catalog_groups
        ):
            msg = "catalog_groups must contain CatalogGroupId values"
            raise TypeError(msg)
        canonical_groups = tuple(
            sorted(set(self.catalog_groups), key=lambda group: group.value)
        )
        object.__setattr__(self, "catalog_groups", canonical_groups)
        constraint_sets = _normalize_constraint_sets(
            self.identity,
            self.accelerator_id,
            self.constraint_sets,
            self.constraint_set,
        )
        object.__setattr__(self, "constraint_sets", constraint_sets)
        object.__setattr__(
            self,
            "constraint_set",
            constraint_sets[0] if len(constraint_sets) == 1 else None,
        )


def _require_status_filter_types(
    filters: tuple[tuple[str, object, type[StrEnum]], ...],
) -> None:
    for name, values, enum_type in filters:
        if not isinstance(values, tuple) or any(
            not isinstance(value, enum_type) for value in values
        ):
            msg = f"{name} must contain typed status values"
            raise TypeError(msg)


def _require_query_item_quantities(
    quantities: tuple[tuple[str, QuotaQuantity | None], ...],
) -> None:
    for name, value in quantities:
        if value is not None and not isinstance(value, QuotaQuantity):
            msg = f"{name} must be QuotaQuantity or None"
            raise TypeError(msg)
    if len({value.unit for _, value in quantities if value is not None}) > 1:
        msg = "query item quantities must use one native quota unit"
        raise ValueError(msg)
    usage = dict(quantities)["usage_value"]
    if usage is not None and usage.value < 0:
        msg = "query item usage_value must be non-negative"
        raise ValueError(msg)


def _require_query_item_status_types(
    statuses: tuple[tuple[str, object, type[StrEnum]], ...],
) -> None:
    for name, value, enum_type in statuses:
        if not isinstance(value, enum_type):
            msg = f"{name} must use {enum_type.__name__}"
            raise TypeError(msg)


def _normalize_constraint_sets(
    identity: EffectiveQuotaSliceIdentity,
    accelerator_id: AcceleratorId | None,
    constraint_sets: tuple[AcceleratorConstraintSet, ...],
    constraint_set: AcceleratorConstraintSet | None,
) -> tuple[AcceleratorConstraintSet, ...]:
    if not isinstance(constraint_sets, tuple) or any(
        not isinstance(item, AcceleratorConstraintSet) for item in constraint_sets
    ):
        msg = "constraint_sets must contain AcceleratorConstraintSet values"
        raise TypeError(msg)
    if constraint_set is not None and not isinstance(
        constraint_set, AcceleratorConstraintSet
    ):
        msg = "constraint_set must be AcceleratorConstraintSet or None"
        raise TypeError(msg)
    if constraint_set is not None:
        if constraint_sets and constraint_sets != (constraint_set,):
            msg = "singular constraint_set is ambiguous with constraint_sets"
            raise ValueError(msg)
        constraint_sets = (constraint_set,)
    if len(set(constraint_sets)) != len(constraint_sets):
        msg = "constraint_sets must not repeat an anchored relationship"
        raise ValueError(msg)
    if accelerator_id is not None and any(
        item.accelerator_id != accelerator_id for item in constraint_sets
    ):
        msg = "constraint set accelerator must match the query item"
        raise ValueError(msg)
    if any(
        ConstraintReference(identity) not in item.references for item in constraint_sets
    ):
        msg = "every constraint set must reference the query item identity"
        raise ValueError(msg)
    return tuple(sorted(constraint_sets, key=_constraint_set_key))


def _constraint_set_key(value: AcceleratorConstraintSet) -> tuple[object, ...]:
    return tuple(
        (
            reference.slice_identity.resource_scope.canonical_name,
            reference.slice_identity.service,
            reference.slice_identity.quota_id,
            reference.slice_identity.dimensions.items,
            reference.slice_identity.quota_scope.value,
        )
        for reference in value.references
    )


@dataclass(frozen=True, slots=True)
class QuotaQuery:
    """One resource scope over the fixed V1 inventory with normalized filters."""

    resource_scope: ResourceScope
    filters: QuotaQueryFilters = field(default_factory=QuotaQueryFilters)
    sort: tuple[QuotaSort, ...] = ()

    def __post_init__(self) -> None:
        """Infer the provider subset only from source-selecting query filters."""
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "resource_scope must be a ResourceScope"
            raise TypeError(msg)
        if not isinstance(self.filters, QuotaQueryFilters):
            msg = "filters must be QuotaQueryFilters"
            raise TypeError(msg)
        if not isinstance(self.sort, tuple) or any(
            not isinstance(sort, QuotaSort) for sort in self.sort
        ):
            msg = "sort must be QuotaSort values"
            raise TypeError(msg)
        if len({sort.field for sort in self.sort}) != len(self.sort):
            msg = "sort fields must not be repeated"
            raise ValueError(msg)

    @property
    def services(self) -> tuple[str, ...]:
        """Return providers that can satisfy every source-selecting facet."""
        selected = set(V1_PROVIDER_SERVICES)
        if self.filters.services:
            selected.intersection_update(self.filters.services)
        if self.filters.catalog_groups:
            selected.intersection_update(
                service
                for group in self.filters.catalog_groups
                for service in _CATALOG_GROUP_SERVICES[group]
            )
        return tuple(service for service in V1_PROVIDER_SERVICES if service in selected)


@dataclass(frozen=True, slots=True)
class QuerySnapshotMetadata:
    """Immutable binding for one complete or incomplete product collection."""

    snapshot_id: str
    query: QuotaQuery
    catalog: CatalogMetadata
    evidence_contract: str
    observed_at: datetime
    expires_at: datetime
    complete: bool
    inventory_revision: str = PROVIDER_INVENTORY_REVISION
    source_coverage: tuple[ProviderSourceCoverage, ...] = ()

    def __post_init__(self) -> None:  # noqa: C901
        """Bind source, filters, sort, and catalog revision to one snapshot ID."""
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            msg = "snapshot_id must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.query, QuotaQuery):
            msg = "snapshot query must be a QuotaQuery"
            raise TypeError(msg)
        if not isinstance(self.catalog, CatalogMetadata):
            msg = "snapshot catalog must be CatalogMetadata"
            raise TypeError(msg)
        if self.evidence_contract != QUOTA_QUERY_EVIDENCE_CONTRACT:
            msg = f"unsupported evidence contract: {self.evidence_contract!r}"
            raise ValueError(msg)
        require_utc(self.observed_at, "observed_at")
        require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.observed_at:
            msg = "snapshot expires_at must follow observed_at"
            raise ValueError(msg)
        if not isinstance(self.complete, bool):
            msg = "snapshot complete must be bool"
            raise TypeError(msg)
        if self.inventory_revision != PROVIDER_INVENTORY_REVISION:
            msg = (
                f"unsupported provider inventory revision: {self.inventory_revision!r}"
            )
            raise ValueError(msg)
        coverage = self.source_coverage
        if (
            not isinstance(coverage, tuple)
            or tuple(item.service for item in coverage) != V1_PROVIDER_SERVICES
            or len(set(coverage)) != len(coverage)
        ):
            msg = "snapshot coverage must contain each fixed V1 provider in order"
            raise ValueError(msg)
        queried = tuple(
            item.service
            for item in coverage
            if item.state is not ProviderSourceCoverageState.INTENTIONALLY_UNQUERIED
        )
        if queried != self.query.services:
            msg = "snapshot coverage must match the inferred queried provider subset"
            raise ValueError(msg)
        coverage_complete = all(
            item.state is ProviderSourceCoverageState.COMPLETE
            for item in coverage
            if item.service in queried
        )
        if self.complete is not coverage_complete:
            msg = "snapshot completeness must match queried-provider coverage"
            raise ValueError(msg)

    @property
    def queried_services(self) -> tuple[str, ...]:
        """Return the exact provider subset bound into this snapshot."""
        return self.query.services


@dataclass(frozen=True, slots=True)
class OpaqueQueryCursor:
    """Opaque product continuation bound to one immutable snapshot offset."""

    value: str
    snapshot_id: str
    offset: int

    def __post_init__(self) -> None:
        """Reject empty handles and invalid logical offsets."""
        if not isinstance(self.value, str) or not self.value:
            msg = "cursor value must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            msg = "cursor snapshot_id must be non-empty"
            raise ValueError(msg)
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or self.offset < 0
        ):
            msg = "cursor offset must be a non-negative integer"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class QuotaQuerySnapshot:
    """Collected product evidence that can be globally filtered and sorted."""

    metadata: QuerySnapshotMetadata
    items: tuple[QuotaQueryItem, ...]

    def __post_init__(self) -> None:
        """Require typed immutable snapshot evidence."""
        if not isinstance(self.metadata, QuerySnapshotMetadata):
            msg = "metadata must be QuerySnapshotMetadata"
            raise TypeError(msg)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, QuotaQueryItem) for item in self.items
        ):
            msg = "items must be QuotaQueryItem values"
            raise TypeError(msg)
        query = self.metadata.query
        if any(
            item.identity.resource_scope != query.resource_scope
            or item.identity.service not in query.services
            for item in self.items
        ):
            msg = "snapshot items must remain within the bound query source"
            raise ValueError(msg)
        identities = tuple(item.identity for item in self.items)
        if len(set(identities)) != len(identities):
            msg = "snapshot item identities must be unique"
            raise ValueError(msg)

    def sorted_items(self) -> tuple[QuotaQueryItem, ...]:
        """Filter and globally sort only a complete collected snapshot."""
        if not self.metadata.complete:
            msg = "global sorting requires a complete collected snapshot"
            raise IncompleteQuerySnapshotError(msg)
        filtered = tuple(
            item for item in self.items if self.metadata.query.filters.matches(item)
        )
        _validate_sort_units(filtered, self.metadata.query.sort)
        return tuple(sorted(filtered, key=cmp_to_key(self._compare)))

    def _compare(self, left: QuotaQueryItem, right: QuotaQueryItem) -> int:
        for sort in self.metadata.query.sort:
            result = _compare_field(left, right, sort.field, sort.direction)
            if result:
                return result
        return _compare_values(_identity_key(left), _identity_key(right))


def _validate_sort_units(
    items: tuple[QuotaQueryItem, ...],
    sorts: tuple[QuotaSort, ...],
) -> None:
    numeric_fields = {
        QuotaSortField.EFFECTIVE,
        QuotaSortField.USAGE,
        QuotaSortField.DESIRED,
        QuotaSortField.GRANTED,
    }
    for sort in sorts:
        if sort.field not in numeric_fields:
            continue
        units = {
            value.unit
            for item in items
            if isinstance((value := _field_value(item, sort.field)), QuotaQuantity)
        }
        if len(units) > 1:
            msg = "numeric sorting cannot compare more than one native unit"
            raise IncompatibleSortUnitsError(msg)


def _compare_field(
    left: QuotaQueryItem,
    right: QuotaQueryItem,
    sort_field: QuotaSortField,
    direction: SortDirection,
) -> int:
    left_value = _field_value(left, sort_field)
    right_value = _field_value(right, sort_field)
    if left_value is None or right_value is None:
        if left_value is right_value:
            return 0
        return 1 if left_value is None else -1
    if isinstance(left_value, QuotaQuantity) and isinstance(right_value, QuotaQuantity):
        result = _compare_values(left_value.value, right_value.value)
    elif isinstance(left_value, str) and isinstance(right_value, str):
        result = _compare_values(_text_key(left_value), _text_key(right_value))
    elif isinstance(left_value, datetime) and isinstance(right_value, datetime):
        # A smaller evidence age means a later observation timestamp.
        result = -_compare_values(left_value, right_value)
    else:
        msg = "sort values must have one coherent domain type"
        raise TypeError(msg)
    return -result if direction is SortDirection.DESC else result


def _field_value(
    item: QuotaQueryItem, sort_field: QuotaSortField
) -> str | QuotaQuantity | datetime | None:
    values: dict[QuotaSortField, str | QuotaQuantity | datetime | None] = {
        QuotaSortField.QUOTA_ID: item.identity.quota_id,
        QuotaSortField.DISPLAY_NAME: item.display_name,
        QuotaSortField.SERVICE: item.identity.service,
        QuotaSortField.ACCELERATOR: (
            None if item.accelerator_id is None else item.accelerator_id.value
        ),
        QuotaSortField.LOCATION: item.location,
        QuotaSortField.QUOTA_SCOPE: item.identity.quota_scope.value,
        QuotaSortField.QUOTA_POOL: item.quota_pool,
        QuotaSortField.EFFECTIVE: item.effective_value,
        QuotaSortField.USAGE: item.usage_value,
        QuotaSortField.DESIRED: item.desired_value,
        QuotaSortField.GRANTED: item.granted_value,
        QuotaSortField.RECONCILIATION: item.reconciliation.value,
        QuotaSortField.GRANT_SATISFACTION: item.grant_satisfaction.value,
        QuotaSortField.EFFECTIVE_CONFIRMATION: item.effective_confirmation.value,
        QuotaSortField.EVIDENCE_AGE: item.evidence_observed_at,
    }
    return values[sort_field]


def _identity_key(item: QuotaQueryItem) -> tuple[object, ...]:
    identity = item.identity
    return (
        identity.resource_scope.kind.value,
        identity.resource_scope.canonical_name,
        identity.service,
        identity.quota_id,
        identity.dimensions.items,
        identity.quota_scope.value,
    )


def _compare_values(left: object, right: object) -> int:
    if left == right:
        return 0
    return -1 if left < right else 1  # type: ignore[operator]


def _text_key(value: str) -> tuple[str, str]:
    normalized = normalize("NFC", value)
    return (normalized.casefold(), normalized)


def _matches_text(item: QuotaQueryItem, text: str) -> bool:
    needle = normalize("NFC", text).casefold()
    values = [item.identity.quota_id, item.display_name or ""]
    values.extend(value for pair in item.identity.dimensions.items for value in pair)
    return any(needle in normalize("NFC", value).casefold() for value in values)


def _is_canonical_location(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or value != value.lower()
    ):
        return False
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    return (
        value[0].isalnum()
        and value[-1].isalnum()
        and all(character in allowed for character in value)
    )


def _is_stable_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and value == value.lower()
        and all(component and component.isalnum() for component in value.split("-"))
    )


def _is_canonical_service_dns(service: object) -> bool:
    if (
        not isinstance(service, str)
        or not service.isascii()
        or service != service.lower()
    ):
        return False
    labels = service.split(".")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    return len(labels) >= _MINIMUM_DNS_LABELS and all(
        label
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character in allowed for character in label)
        for label in labels
    )


def _canonical_service_selector(service: object) -> str:
    """Normalize supported CLI shorthand and reject providers outside V1."""
    if not isinstance(service, str):
        msg = "service selector must be text"
        raise TypeError(msg)
    canonical = _SERVICE_SHORTHANDS.get(service, service)
    if canonical not in V1_PROVIDER_SERVICES:
        msg = "service selector must name a supported V1 provider"
        raise ValueError(msg)
    return canonical
