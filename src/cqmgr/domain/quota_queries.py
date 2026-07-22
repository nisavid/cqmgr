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
from cqmgr.domain.quotas import (
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


@dataclass(frozen=True, slots=True)
class ServiceSource:
    """One exact canonical provider service selected as a query source."""

    service: str

    def __post_init__(self) -> None:
        """Reject display names and noncanonical service text."""
        if not _is_canonical_service_dns(self.service):
            msg = "service source must be a lowercase canonical service DNS name"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CatalogGroupSource:
    """One stable accelerator catalog group selected as a query source."""

    group_id: CatalogGroupId

    def __post_init__(self) -> None:
        """Accept only the fixed public catalog-group identity."""
        if not isinstance(self.group_id, CatalogGroupId):
            msg = "group_id must be a CatalogGroupId"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class QuotaQueryFilters:
    """Repeatable query facets whose values are OR alternatives."""

    services: tuple[str, ...] = ()
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

    def __post_init__(self) -> None:
        """Require canonical service facets without changing their order."""
        if not isinstance(self.services, tuple) or any(
            not _is_canonical_service_dns(service) for service in self.services
        ):
            msg = "service filters must be canonical service DNS names"
            raise ValueError(msg)
        if not isinstance(self.accelerators, tuple) or any(
            not isinstance(accelerator, AcceleratorId)
            for accelerator in self.accelerators
        ):
            msg = "accelerator filters must be AcceleratorId values"
            raise TypeError(msg)
        if not isinstance(self.locations, tuple) or any(
            not _is_canonical_location(location) for location in self.locations
        ):
            msg = "location filters must be canonical location values"
            raise ValueError(msg)
        if not isinstance(self.quota_scopes, tuple) or any(
            not isinstance(scope, QuotaScope) for scope in self.quota_scopes
        ):
            msg = "quota scope filters must be QuotaScope values"
            raise TypeError(msg)
        if not isinstance(self.quota_pools, tuple) or any(
            not _is_stable_id(pool) for pool in self.quota_pools
        ):
            msg = "quota pool filters must be lowercase identifiers"
            raise ValueError(msg)
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
        if self.text is not None and (
            not isinstance(self.text, str) or not normalize("NFC", self.text)
        ):
            msg = "text filter must be non-empty Unicode text"
            raise ValueError(msg)

    def matches(self, item: QuotaQueryItem) -> bool:
        """Combine OR alternatives within facets and AND across facets."""
        if not isinstance(item, QuotaQueryItem):
            msg = "query item must be a QuotaQueryItem"
            raise TypeError(msg)
        checks = (
            not self.services or item.identity.service in self.services,
            not self.accelerators or item.accelerator_id in self.accelerators,
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
    constraint_set: AcceleratorConstraintSet | None = None

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
        _require_constraint_set(self.accelerator_id, self.constraint_set)


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


def _require_query_item_status_types(
    statuses: tuple[tuple[str, object, type[StrEnum]], ...],
) -> None:
    for name, value, enum_type in statuses:
        if not isinstance(value, enum_type):
            msg = f"{name} must use {enum_type.__name__}"
            raise TypeError(msg)


def _require_constraint_set(
    accelerator_id: AcceleratorId | None,
    constraint_set: AcceleratorConstraintSet | None,
) -> None:
    if constraint_set is None:
        return
    if not isinstance(constraint_set, AcceleratorConstraintSet):
        msg = "constraint_set must be AcceleratorConstraintSet or None"
        raise TypeError(msg)
    if accelerator_id != constraint_set.accelerator_id:
        msg = "constraint_set accelerator must match the query item"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class QuotaQuery:
    """One resource scope, one source, and independent local product filters."""

    resource_scope: ResourceScope
    source: ServiceSource | CatalogGroupSource
    filters: QuotaQueryFilters = field(default_factory=QuotaQueryFilters)
    sort: tuple[QuotaSort, ...] = ()

    def __post_init__(self) -> None:
        """Require exactly one typed source rather than inferring from facets."""
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "resource_scope must be a ResourceScope"
            raise TypeError(msg)
        if not isinstance(self.source, (ServiceSource, CatalogGroupSource)):
            msg = "source must be a ServiceSource or CatalogGroupSource"
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
        selected_services = (
            frozenset({self.source.service})
            if isinstance(self.source, ServiceSource)
            else _CATALOG_GROUP_SERVICES[self.source.group_id]
        )
        if any(service not in selected_services for service in self.filters.services):
            source_kind = (
                "service source"
                if isinstance(self.source, ServiceSource)
                else "catalog group"
            )
            msg = f"service filters must remain within the selected {source_kind}"
            raise ValueError(msg)

    @property
    def services(self) -> tuple[str, ...]:
        """Return the stable provider services required by the selected source."""
        if isinstance(self.source, ServiceSource):
            return (self.source.service,)
        return tuple(sorted(_CATALOG_GROUP_SERVICES[self.source.group_id]))


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

    def __post_init__(self) -> None:
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
