"""Strict canonical JSON codec for retained quota-query snapshots."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import cast

from cqmgr.application.ports.quota_snapshots import (
    QuotaSnapshotStoredDataError,
    UnsupportedQuotaSnapshotSchemaError,
)
from cqmgr.domain.catalog import (
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogGroupId,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.diagnostics import DiagnosticCode
from cqmgr.domain.quota_queries import (
    V1_PROVIDER_SERVICES,
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

QUOTA_QUERY_SNAPSHOT_SCHEMA = "cqmgr.quota-query-snapshot/v3"
QUOTA_QUERY_CURSOR_SCHEMA = "cqmgr.quota-query-cursor/v1"
_LEGACY_SNAPSHOT_SCHEMAS = frozenset(
    {
        "cqmgr.quota-query-snapshot/v1",
        "cqmgr.quota-query-snapshot/v2",
    }
)
_LEGACY_CATALOG_GROUP_SERVICES = {
    CatalogGroupId.COMPUTE_ACCELERATORS: "compute.googleapis.com",
    CatalogGroupId.CLOUD_TPU_LEGACY: "tpu.googleapis.com",
}
_SCHEMA = re.compile(r"cqmgr\.quota-query-snapshot/v([0-9]+)\Z")
_CURSOR_SCHEMA = re.compile(r"cqmgr\.quota-query-cursor/v([0-9]+)\Z")
_ABSOLUTE_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/]")
_CONTACT = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_CREDENTIAL_MARKERS = (
    "-----begin ",
    "native-keyring",
    "private-credential",
    "provider-page-token",
    "ya29.",
)
_DIMENSION_PAIR_SIZE = 2
_LATEST_SNAPSHOT_SCHEMA_VERSION = 3
_LATEST_CURSOR_SCHEMA_VERSION = 1


def encode_snapshot_record(snapshot: QuotaQuerySnapshot) -> bytes:
    """Return deterministic UTF-8 JSON containing only normalized evidence."""
    if not isinstance(snapshot, QuotaQuerySnapshot):
        msg = "snapshot codec requires QuotaQuerySnapshot"
        raise TypeError(msg)
    return _encode_snapshot_record(snapshot, QUOTA_QUERY_SNAPSHOT_SCHEMA)


def _encode_snapshot_record(snapshot: QuotaQuerySnapshot, schema: str) -> bytes:
    """Encode one supported snapshot generation for canonicality checks."""
    if schema != QUOTA_QUERY_SNAPSHOT_SCHEMA:
        msg = "cannot encode a superseded quota snapshot schema"
        raise ValueError(msg)
    document: dict[str, object] = {
        "schema": schema,
        "snapshot": _snapshot(snapshot),
    }
    if _contains_unsafe_evidence_text(document):
        msg = "snapshot contains unsafe evidence text"
        raise ValueError(msg)
    return _encode_canonical_document(document)


def _encode_canonical_document(document: dict[str, object]) -> bytes:
    """Encode one already validated record without changing array order."""
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def decode_snapshot_record(data: bytes) -> QuotaQuerySnapshot:
    """Decode one exact supported schema or fail closed."""
    if not isinstance(data, bytes):
        msg = "stored quota snapshot must be bytes"
        raise TypeError(msg)
    try:
        raw = json.loads(data)
        document = _object(raw, "snapshot document")
        if _contains_unsafe_evidence_text(document):
            msg = "stored quota snapshot contains unsafe evidence text"
            raise QuotaSnapshotStoredDataError(msg)  # noqa: TRY301
        schema = document.get("schema")
        if (
            isinstance(schema, str)
            and (match := _SCHEMA.fullmatch(schema))
            and int(match.group(1)) > _LATEST_SNAPSHOT_SCHEMA_VERSION
        ):
            msg = "stored quota snapshot uses a newer schema"
            raise UnsupportedQuotaSnapshotSchemaError(msg)  # noqa: TRY301
        _keys(
            document,
            {"schema", "snapshot"},
            "snapshot document",
        )
        if (
            schema != QUOTA_QUERY_SNAPSHOT_SCHEMA
            and schema not in _LEGACY_SNAPSHOT_SCHEMAS
        ):
            msg = "stored quota snapshot schema is invalid"
            raise QuotaSnapshotStoredDataError(msg)  # noqa: TRY301
        if schema == QUOTA_QUERY_SNAPSHOT_SCHEMA:
            snapshot = _decode_snapshot(document["snapshot"])
            canonical = _encode_snapshot_record(snapshot, cast("str", schema))
        else:
            snapshot = _decode_legacy_snapshot(
                document["snapshot"],
                schema=cast("str", schema),
            )
            canonical = _encode_canonical_document(document)
    except (QuotaSnapshotStoredDataError, UnsupportedQuotaSnapshotSchemaError):
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        msg = "stored quota snapshot is malformed"
        raise QuotaSnapshotStoredDataError(msg) from error
    else:
        if canonical != data:
            msg = "stored quota snapshot is not canonical"
            raise QuotaSnapshotStoredDataError(msg)
        return snapshot


def encode_cursor_binding(snapshot_id: str, offset: int) -> bytes:
    """Encode an internal cursor binding without the public random handle."""
    if not isinstance(snapshot_id, str) or not snapshot_id:
        msg = "cursor snapshot_id must be non-empty"
        raise ValueError(msg)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        msg = "cursor offset must be a non-negative integer"
        raise ValueError(msg)
    document = {
        "schema": QUOTA_QUERY_CURSOR_SCHEMA,
        "snapshot_id": snapshot_id,
        "offset": offset,
    }
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def decode_cursor_binding(data: bytes) -> tuple[str, int]:
    """Decode one strict internal cursor binding or fail closed."""
    if not isinstance(data, bytes):
        msg = "stored quota cursor must be bytes"
        raise TypeError(msg)
    try:
        document = _object(json.loads(data), "cursor document")
        schema = document.get("schema")
        if (
            isinstance(schema, str)
            and (match := _CURSOR_SCHEMA.fullmatch(schema))
            and int(match.group(1)) > _LATEST_CURSOR_SCHEMA_VERSION
        ):
            msg = "stored quota cursor uses a newer schema"
            raise UnsupportedQuotaSnapshotSchemaError(msg)  # noqa: TRY301
        _keys(document, {"schema", "snapshot_id", "offset"}, "cursor document")
        if schema != QUOTA_QUERY_CURSOR_SCHEMA:
            msg = "stored quota cursor schema is invalid"
            raise QuotaSnapshotStoredDataError(msg)  # noqa: TRY301
        snapshot_id = _string(document["snapshot_id"], "cursor snapshot_id")
        offset = document["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            msg = "stored quota cursor offset is invalid"
            raise QuotaSnapshotStoredDataError(msg)  # noqa: TRY301
        result = (snapshot_id, offset)
    except (QuotaSnapshotStoredDataError, UnsupportedQuotaSnapshotSchemaError):
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        msg = "stored quota cursor is malformed"
        raise QuotaSnapshotStoredDataError(msg) from error
    else:
        if encode_cursor_binding(*result) != data:
            msg = "stored quota cursor is not canonical"
            raise QuotaSnapshotStoredDataError(msg)
        return result


def _snapshot(snapshot: QuotaQuerySnapshot) -> dict[str, object]:
    metadata = snapshot.metadata
    return {
        "metadata": {
            "snapshot_id": metadata.snapshot_id,
            "query": _query(metadata.query),
            "catalog": {
                "schema": metadata.catalog.schema,
                "revision": metadata.catalog.revision,
                "content_digest": metadata.catalog.content_digest,
            },
            "evidence_contract": metadata.evidence_contract,
            "inventory_revision": metadata.inventory_revision,
            "source_coverage": [
                _source_coverage(item) for item in metadata.source_coverage
            ],
            "observed_at": _timestamp(metadata.observed_at),
            "expires_at": _timestamp(metadata.expires_at),
            "complete": metadata.complete,
        },
        "ordered_slice_identities": [
            _identity(item.identity) for item in snapshot.items
        ],
        "items": [_item(item) for item in snapshot.items],
    }


def _query(query: QuotaQuery) -> dict[str, object]:
    filters = query.filters
    return {
        "resource_scope": _scope(query.resource_scope),
        "filters": {
            "services": list(filters.services),
            "catalog_groups": [value.value for value in filters.catalog_groups],
            "accelerators": [value.value for value in filters.accelerators],
            "locations": list(filters.locations),
            "quota_scopes": [value.value for value in filters.quota_scopes],
            "quota_pools": list(filters.quota_pools),
            "cataloged": filters.cataloged,
            "guided": filters.guided,
            "mutable": filters.mutable,
            "reconciliations": [value.value for value in filters.reconciliations],
            "grant_satisfactions": [
                value.value for value in filters.grant_satisfactions
            ],
            "effective_confirmations": [
                value.value for value in filters.effective_confirmations
            ],
            "text": filters.text,
        },
        "sort": [
            {"field": value.field.value, "direction": value.direction.value}
            for value in query.sort
        ],
    }


def _item(item: QuotaQueryItem) -> dict[str, object]:
    quantity = item.effective_value
    encoded: dict[str, object] = {
        "identity": _identity(item.identity),
        "display_name": item.display_name,
        "accelerator_id": (
            None if item.accelerator_id is None else item.accelerator_id.value
        ),
        "location": item.location,
        "quota_pool": item.quota_pool,
        "catalog_groups": [value.value for value in item.catalog_groups],
        "predicates": {
            "discovered": item.predicates.discovered,
            "cataloged": item.predicates.cataloged,
            "guided": item.predicates.guided,
            "mutable": item.predicates.mutable,
        },
        "effective_value": (
            None
            if quantity is None
            else {"value": quantity.base10, "unit": quantity.unit.symbol}
        ),
        "usage_value": _quantity(item.usage_value),
        "desired_value": _quantity(item.desired_value),
        "granted_value": _quantity(item.granted_value),
        "reconciliation": item.reconciliation.value,
        "grant_satisfaction": item.grant_satisfaction.value,
        "effective_confirmation": item.effective_confirmation.value,
        "evidence_observed_at": (
            None
            if item.evidence_observed_at is None
            else _timestamp(item.evidence_observed_at)
        ),
    }
    encoded["constraint_sets"] = [
        _constraint_set(constraint_set) for constraint_set in item.constraint_sets
    ]
    return encoded


def _source_coverage(value: ProviderSourceCoverage) -> dict[str, object]:
    return {
        "service": value.service,
        "state": value.state.value,
        "pages_attempted": value.pages_attempted,
        "pages_completed": value.pages_completed,
        "page_cap_reached": value.page_cap_reached,
        "observed_at": (
            None if value.observed_at is None else _timestamp(value.observed_at)
        ),
        "diagnostic_codes": [code.value for code in value.diagnostic_codes],
    }


def _constraint_set(
    value: AcceleratorConstraintSet | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "accelerator_id": value.accelerator_id.value,
        "references": [
            _identity(reference.slice_identity) for reference in value.references
        ],
    }


def _quantity(value: QuotaQuantity | None) -> dict[str, str] | None:
    return None if value is None else {"value": value.base10, "unit": value.unit.symbol}


def _identity(identity: EffectiveQuotaSliceIdentity) -> dict[str, object]:
    return {
        "resource_scope": _scope(identity.resource_scope),
        "service": identity.service,
        "quota_id": identity.quota_id,
        "dimensions": [list(value) for value in identity.dimensions.items],
        "quota_scope": identity.quota_scope.value,
    }


def _scope(scope: ResourceScope) -> dict[str, str]:
    return {"kind": scope.kind.value, "name": scope.canonical_name}


def _decode_snapshot(value: object) -> QuotaQuerySnapshot:
    table = _exact_object(
        value,
        {"metadata", "ordered_slice_identities", "items"},
        "snapshot",
    )
    metadata = _exact_object(
        table["metadata"],
        {
            "snapshot_id",
            "query",
            "catalog",
            "evidence_contract",
            "inventory_revision",
            "source_coverage",
            "observed_at",
            "expires_at",
            "complete",
        },
        "metadata",
    )
    catalog = _exact_object(
        metadata["catalog"], {"schema", "revision", "content_digest"}, "catalog"
    )
    snapshot = QuotaQuerySnapshot(
        metadata=QuerySnapshotMetadata(
            snapshot_id=_string(metadata["snapshot_id"], "snapshot_id"),
            query=_decode_query(metadata["query"]),
            catalog=CatalogMetadata(
                _string(catalog["schema"], "catalog.schema"),
                _string(catalog["revision"], "catalog.revision"),
                _string(catalog["content_digest"], "catalog.content_digest"),
            ),
            evidence_contract=_string(
                metadata["evidence_contract"], "evidence_contract"
            ),
            inventory_revision=_string(
                metadata["inventory_revision"], "inventory_revision"
            ),
            source_coverage=tuple(
                _decode_source_coverage(item)
                for item in _list(metadata["source_coverage"], "source_coverage")
            ),
            observed_at=_datetime(metadata["observed_at"], "observed_at"),
            expires_at=_datetime(metadata["expires_at"], "expires_at"),
            complete=_bool(metadata["complete"], "complete"),
        ),
        items=tuple(_decode_item(item) for item in _list(table["items"], "items")),
    )
    ordered_identities = tuple(
        _decode_identity(item)
        for item in _list(
            table["ordered_slice_identities"],
            "ordered_slice_identities",
        )
    )
    if ordered_identities != tuple(item.identity for item in snapshot.items):
        msg = "stored ordered slice identities do not match snapshot items"
        raise QuotaSnapshotStoredDataError(msg)
    return snapshot


def _decode_legacy_snapshot(
    value: object,
    *,
    schema: str,
) -> QuotaQuerySnapshot:
    table = _exact_object(value, {"metadata", "items"}, "snapshot")
    metadata = _exact_object(
        table["metadata"],
        {
            "snapshot_id",
            "query",
            "catalog",
            "evidence_contract",
            "observed_at",
            "expires_at",
            "complete",
        },
        "metadata",
    )
    catalog = _exact_object(
        metadata["catalog"], {"schema", "revision", "content_digest"}, "catalog"
    )
    observed_at = _datetime(metadata["observed_at"], "observed_at")
    complete = _bool(metadata["complete"], "complete")
    query, catalog_groups = _decode_legacy_query(metadata["query"])
    source_coverage = tuple(
        (
            (
                ProviderSourceCoverage.complete(
                    service,
                    pages_attempted=0,
                    pages_completed=0,
                    observed_at=observed_at,
                )
                if complete
                else ProviderSourceCoverage.incomplete(
                    service,
                    pages_attempted=0,
                    pages_completed=0,
                    observed_at=observed_at,
                )
            )
            if service in query.services
            else ProviderSourceCoverage.intentionally_unqueried(service)
        )
        for service in V1_PROVIDER_SERVICES
    )
    return QuotaQuerySnapshot(
        metadata=QuerySnapshotMetadata(
            snapshot_id=_string(metadata["snapshot_id"], "snapshot_id"),
            query=query,
            catalog=CatalogMetadata(
                _string(catalog["schema"], "catalog.schema"),
                _string(catalog["revision"], "catalog.revision"),
                _string(catalog["content_digest"], "catalog.content_digest"),
            ),
            evidence_contract=_string(
                metadata["evidence_contract"], "evidence_contract"
            ),
            observed_at=observed_at,
            expires_at=_datetime(metadata["expires_at"], "expires_at"),
            complete=complete,
            source_coverage=source_coverage,
        ),
        items=tuple(
            _decode_legacy_item(
                item,
                schema=schema,
                catalog_groups=catalog_groups,
            )
            for item in _list(table["items"], "items")
        ),
    )


def _decode_legacy_query(
    value: object,
) -> tuple[QuotaQuery, tuple[CatalogGroupId, ...]]:
    table = _exact_object(
        value, {"resource_scope", "source", "filters", "sort"}, "query"
    )
    source = _exact_object(table["source"], {"kind", "value"}, "query.source")
    source_kind = _string(source["kind"], "query.source.kind")
    source_value = _string(source["value"], "query.source.value")
    filters = _exact_object(
        table["filters"],
        {
            "services",
            "accelerators",
            "locations",
            "quota_scopes",
            "quota_pools",
            "cataloged",
            "guided",
            "mutable",
            "reconciliations",
            "grant_satisfactions",
            "effective_confirmations",
            "text",
        },
        "query.filters",
    )
    legacy_filters = QuotaQueryFilters(
        services=_strings(filters["services"], "services"),
        accelerators=tuple(
            AcceleratorId(item)
            for item in _strings(filters["accelerators"], "accelerators")
        ),
        locations=_strings(filters["locations"], "locations"),
        quota_scopes=tuple(
            QuotaScope(item)
            for item in _strings(filters["quota_scopes"], "quota_scopes")
        ),
        quota_pools=_strings(filters["quota_pools"], "quota_pools"),
        cataloged=_optional_bool(filters["cataloged"], "cataloged"),
        guided=_optional_bool(filters["guided"], "guided"),
        mutable=_optional_bool(filters["mutable"], "mutable"),
        reconciliations=tuple(
            Reconciliation(item)
            for item in _strings(filters["reconciliations"], "reconciliations")
        ),
        grant_satisfactions=tuple(
            GrantSatisfaction(item)
            for item in _strings(filters["grant_satisfactions"], "grant_satisfactions")
        ),
        effective_confirmations=tuple(
            EffectiveConfirmation(item)
            for item in _strings(
                filters["effective_confirmations"], "effective_confirmations"
            )
        ),
        text=_optional_string(filters["text"], "text"),
    )
    if source_kind == "service":
        if source_value not in V1_PROVIDER_SERVICES:
            msg = "stored quota query source is outside the V1 provider inventory"
            raise QuotaSnapshotStoredDataError(msg)
        selected_service = source_value
        catalog_groups: tuple[CatalogGroupId, ...] = ()
        migrated_services = (selected_service,)
    elif source_kind == "catalog-group":
        try:
            catalog_group = CatalogGroupId(source_value)
            selected_service = _LEGACY_CATALOG_GROUP_SERVICES[catalog_group]
        except (KeyError, ValueError) as error:
            msg = "stored quota query source is outside the V1 provider inventory"
            raise QuotaSnapshotStoredDataError(msg) from error
        catalog_groups = (catalog_group,)
        migrated_services = legacy_filters.services
    else:
        msg = "stored quota query source is invalid"
        raise QuotaSnapshotStoredDataError(msg)
    if legacy_filters.services and selected_service not in legacy_filters.services:
        msg = "stored quota query source conflicts with its service filters"
        raise QuotaSnapshotStoredDataError(msg)
    migrated_filters = QuotaQueryFilters(
        services=migrated_services,
        catalog_groups=catalog_groups,
        accelerators=legacy_filters.accelerators,
        locations=legacy_filters.locations,
        quota_scopes=legacy_filters.quota_scopes,
        quota_pools=legacy_filters.quota_pools,
        cataloged=legacy_filters.cataloged,
        guided=legacy_filters.guided,
        mutable=legacy_filters.mutable,
        reconciliations=legacy_filters.reconciliations,
        grant_satisfactions=legacy_filters.grant_satisfactions,
        effective_confirmations=legacy_filters.effective_confirmations,
        text=legacy_filters.text,
    )
    query = QuotaQuery(
        resource_scope=_decode_scope(table["resource_scope"]),
        filters=migrated_filters,
        sort=tuple(_decode_sort(item) for item in _list(table["sort"], "query.sort")),
    )
    if query.services != (selected_service,):
        msg = "stored quota query cannot be represented by the V1 provider inventory"
        raise QuotaSnapshotStoredDataError(msg)
    return query, catalog_groups


def _decode_query(value: object) -> QuotaQuery:
    table = _exact_object(value, {"resource_scope", "filters", "sort"}, "query")
    filters = _exact_object(
        table["filters"],
        {
            "services",
            "catalog_groups",
            "accelerators",
            "locations",
            "quota_scopes",
            "quota_pools",
            "cataloged",
            "guided",
            "mutable",
            "reconciliations",
            "grant_satisfactions",
            "effective_confirmations",
            "text",
        },
        "query.filters",
    )
    return QuotaQuery(
        resource_scope=_decode_scope(table["resource_scope"]),
        filters=QuotaQueryFilters(
            services=_strings(filters["services"], "services"),
            catalog_groups=tuple(
                CatalogGroupId(item)
                for item in _strings(
                    filters["catalog_groups"],
                    "catalog_groups",
                )
            ),
            accelerators=tuple(
                AcceleratorId(item)
                for item in _strings(filters["accelerators"], "accelerators")
            ),
            locations=_strings(filters["locations"], "locations"),
            quota_scopes=tuple(
                QuotaScope(item)
                for item in _strings(filters["quota_scopes"], "quota_scopes")
            ),
            quota_pools=_strings(filters["quota_pools"], "quota_pools"),
            cataloged=_optional_bool(filters["cataloged"], "cataloged"),
            guided=_optional_bool(filters["guided"], "guided"),
            mutable=_optional_bool(filters["mutable"], "mutable"),
            reconciliations=tuple(
                Reconciliation(item)
                for item in _strings(filters["reconciliations"], "reconciliations")
            ),
            grant_satisfactions=tuple(
                GrantSatisfaction(item)
                for item in _strings(
                    filters["grant_satisfactions"], "grant_satisfactions"
                )
            ),
            effective_confirmations=tuple(
                EffectiveConfirmation(item)
                for item in _strings(
                    filters["effective_confirmations"], "effective_confirmations"
                )
            ),
            text=_optional_string(filters["text"], "text"),
        ),
        sort=tuple(_decode_sort(item) for item in _list(table["sort"], "query.sort")),
    )


def _decode_sort(value: object) -> QuotaSort:
    table = _exact_object(value, {"field", "direction"}, "sort")
    return QuotaSort(
        QuotaSortField(_string(table["field"], "sort.field")),
        SortDirection(_string(table["direction"], "sort.direction")),
    )


def _decode_item(value: object) -> QuotaQueryItem:
    return _decode_item_record(
        value,
        constraint_key="constraint_sets",
        legacy_catalog_groups=None,
    )


def _decode_legacy_item(
    value: object,
    *,
    schema: str,
    catalog_groups: tuple[CatalogGroupId, ...],
) -> QuotaQueryItem:
    return _decode_item_record(
        value,
        constraint_key=(
            "constraint_set" if schema.endswith("/v1") else "constraint_sets"
        ),
        legacy_catalog_groups=catalog_groups,
    )


def _decode_item_record(
    value: object,
    *,
    constraint_key: str,
    legacy_catalog_groups: tuple[CatalogGroupId, ...] | None,
) -> QuotaQueryItem:
    keys = {
        "identity",
        "display_name",
        "accelerator_id",
        "location",
        "quota_pool",
        "predicates",
        "effective_value",
        "usage_value",
        "desired_value",
        "granted_value",
        "reconciliation",
        "grant_satisfaction",
        "effective_confirmation",
        "evidence_observed_at",
        constraint_key,
    }
    if legacy_catalog_groups is None:
        keys.add("catalog_groups")
    table = _exact_object(
        value,
        keys,
        "item",
    )
    predicates = _exact_object(
        table["predicates"],
        {"discovered", "cataloged", "guided", "mutable"},
        "item.predicates",
    )
    accelerator = _optional_string(table["accelerator_id"], "accelerator_id")
    quantity = table["effective_value"]
    return QuotaQueryItem(
        identity=_decode_identity(table["identity"]),
        display_name=_optional_string(table["display_name"], "display_name"),
        accelerator_id=None if accelerator is None else AcceleratorId(accelerator),
        location=_optional_string(table["location"], "location"),
        quota_pool=_optional_string(table["quota_pool"], "quota_pool"),
        catalog_groups=(
            tuple(
                CatalogGroupId(item)
                for item in _strings(table["catalog_groups"], "catalog_groups")
            )
            if legacy_catalog_groups is None
            else legacy_catalog_groups
        ),
        predicates=CatalogPredicates(
            _bool(predicates["discovered"], "discovered"),
            _bool(predicates["cataloged"], "cataloged"),
            _bool(predicates["guided"], "guided"),
            _bool(predicates["mutable"], "mutable"),
        ),
        effective_value=None if quantity is None else _decode_quantity(quantity),
        usage_value=_optional_quantity(table["usage_value"]),
        desired_value=_optional_quantity(table["desired_value"]),
        granted_value=_optional_quantity(table["granted_value"]),
        reconciliation=Reconciliation(
            _string(table["reconciliation"], "reconciliation")
        ),
        grant_satisfaction=GrantSatisfaction(
            _string(table["grant_satisfaction"], "grant_satisfaction")
        ),
        effective_confirmation=EffectiveConfirmation(
            _string(table["effective_confirmation"], "effective_confirmation")
        ),
        evidence_observed_at=(
            None
            if table["evidence_observed_at"] is None
            else _datetime(table["evidence_observed_at"], "evidence_observed_at")
        ),
        constraint_sets=(
            (
                ()
                if table[constraint_key] is None
                else (_decode_constraint_set(table[constraint_key]),)
            )
            if constraint_key == "constraint_set"
            else tuple(
                _decode_constraint_set(item)
                for item in _list(table[constraint_key], constraint_key)
            )
        ),
    )


def _decode_source_coverage(value: object) -> ProviderSourceCoverage:
    table = _exact_object(
        value,
        {
            "service",
            "state",
            "pages_attempted",
            "pages_completed",
            "page_cap_reached",
            "observed_at",
            "diagnostic_codes",
        },
        "source_coverage",
    )
    attempted = table["pages_attempted"]
    completed = table["pages_completed"]
    if any(
        isinstance(count, bool) or not isinstance(count, int)
        for count in (attempted, completed)
    ):
        msg = "stored source coverage page counts are invalid"
        raise QuotaSnapshotStoredDataError(msg)
    attempted_count = cast("int", attempted)
    completed_count = cast("int", completed)
    observed = table["observed_at"]
    return ProviderSourceCoverage(
        service=_string(table["service"], "source_coverage.service"),
        state=ProviderSourceCoverageState(
            _string(table["state"], "source_coverage.state")
        ),
        pages_attempted=attempted_count,
        pages_completed=completed_count,
        page_cap_reached=_bool(
            table["page_cap_reached"],
            "source_coverage.page_cap_reached",
        ),
        observed_at=(
            None
            if observed is None
            else _datetime(observed, "source_coverage.observed_at")
        ),
        diagnostic_codes=tuple(
            DiagnosticCode(item)
            for item in _strings(
                table["diagnostic_codes"],
                "source_coverage.diagnostic_codes",
            )
        ),
    )


def _decode_constraint_set(value: object) -> AcceleratorConstraintSet:
    table = _exact_object(
        value,
        {"accelerator_id", "references"},
        "constraint_set",
    )
    return AcceleratorConstraintSet(
        AcceleratorId(_string(table["accelerator_id"], "constraint accelerator_id")),
        tuple(
            ConstraintReference(_decode_identity(item))
            for item in _list(table["references"], "constraint references")
        ),
    )


def _decode_identity(value: object) -> EffectiveQuotaSliceIdentity:
    table = _exact_object(
        value,
        {"resource_scope", "service", "quota_id", "dimensions", "quota_scope"},
        "identity",
    )
    dimensions = []
    for raw_pair in _list(table["dimensions"], "dimensions"):
        pair = _list(raw_pair, "dimension")
        if len(pair) != _DIMENSION_PAIR_SIZE:
            msg = "stored dimension pair is invalid"
            raise QuotaSnapshotStoredDataError(msg)
        dimensions.append(
            (_string(pair[0], "dimension key"), _string(pair[1], "dimension value"))
        )
    return EffectiveQuotaSliceIdentity(
        resource_scope=_decode_scope(table["resource_scope"]),
        service=_string(table["service"], "service"),
        quota_id=_string(table["quota_id"], "quota_id"),
        dimensions=NormalizedDimensions(dimensions),
        quota_scope=QuotaScope(_string(table["quota_scope"], "quota_scope")),
    )


def _decode_scope(value: object) -> ResourceScope:
    table = _exact_object(value, {"kind", "name"}, "resource_scope")
    return ResourceScope(
        ResourceScopeKind(_string(table["kind"], "resource_scope.kind")),
        _string(table["name"], "resource_scope.name"),
    )


def _decode_quantity(value: object) -> QuotaQuantity:
    table = _exact_object(value, {"value", "unit"}, "effective_value")
    raw = _string(table["value"], "effective_value.value")
    parsed = int(raw)
    if str(parsed) != raw:
        msg = "stored quota quantity is not canonical"
        raise QuotaSnapshotStoredDataError(msg)
    return QuotaQuantity(parsed, QuotaUnit(_string(table["unit"], "unit")))


def _optional_quantity(value: object) -> QuotaQuantity | None:
    return None if value is None else _decode_quantity(value)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _datetime(value: object, location: str) -> datetime:
    raw = _string(value, location)
    if not raw.endswith("Z"):
        msg = f"stored {location} must be UTC"
        raise QuotaSnapshotStoredDataError(msg)
    parsed = datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    if parsed.tzinfo != UTC:
        msg = f"stored {location} must be UTC"
        raise QuotaSnapshotStoredDataError(msg)
    return parsed


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        msg = f"stored {location} must be an object"
        raise QuotaSnapshotStoredDataError(msg)
    return cast("dict[str, object]", value)


def _keys(table: dict[str, object], expected: set[str], location: str) -> None:
    if set(table) != expected:
        msg = f"stored {location} fields are invalid"
        raise QuotaSnapshotStoredDataError(msg)


def _exact_object(
    value: object, expected: set[str], location: str
) -> dict[str, object]:
    table = _object(value, location)
    _keys(table, expected, location)
    return table


def _list(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"stored {location} must be an array"
        raise QuotaSnapshotStoredDataError(msg)
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str):
        msg = f"stored {location} must be a string"
        raise QuotaSnapshotStoredDataError(msg)
    return value


def _optional_string(value: object, location: str) -> str | None:
    return None if value is None else _string(value, location)


def _strings(value: object, location: str) -> tuple[str, ...]:
    return tuple(_string(item, location) for item in _list(value, location))


def _bool(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        msg = f"stored {location} must be boolean"
        raise QuotaSnapshotStoredDataError(msg)
    return value


def _optional_bool(value: object, location: str) -> bool | None:
    return None if value is None else _bool(value, location)


def _contains_unsafe_evidence_text(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_unsafe_evidence_text(key) or _contains_unsafe_evidence_text(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_unsafe_evidence_text(item) for item in value)
    if not isinstance(value, str):
        return False
    lowered = value.casefold()
    return (
        value.startswith(("/", "~/", "\\\\"))
        or _ABSOLUTE_WINDOWS_PATH.match(value) is not None
        or _CONTACT.search(value) is not None
        or any(marker in lowered for marker in _CREDENTIAL_MARKERS)
    )
