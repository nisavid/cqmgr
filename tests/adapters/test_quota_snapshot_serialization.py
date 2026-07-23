"""Canonical safe serialization for retained quota-query snapshots."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from cqmgr.adapters.serialization.quota_snapshots import (
    decode_cursor_binding,
    decode_snapshot_record,
    encode_cursor_binding,
    encode_snapshot_record,
)
from cqmgr.application.ports.quota_snapshots import (
    QuotaSnapshotStoredDataError,
    UnsupportedQuotaSnapshotSchemaError,
)
from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogGroupId,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.quota_queries import (
    QUOTA_QUERY_EVIDENCE_CONTRACT,
    V1_PROVIDER_SERVICES,
    ProviderSourceCoverage,
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


def quota_snapshot(*, complete: bool = True) -> QuotaQuerySnapshot:
    """Return normalized safe evidence with every persisted field populated."""
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    query = QuotaQuery(
        resource_scope=scope,
        filters=QuotaQueryFilters(
            services=("compute.googleapis.com",),
            catalog_groups=(CatalogGroupId.COMPUTE_ACCELERATORS,),
            accelerators=(AcceleratorId("nvidia-h100"),),
            locations=("us-central1",),
            quota_scopes=(QuotaScope.REGIONAL,),
            quota_pools=("standard",),
            cataloged=True,
            guided=False,
            mutable=None,
            reconciliations=(Reconciliation.RECONCILING,),
            grant_satisfactions=(GrantSatisfaction.FULL,),
            effective_confirmations=(EffectiveConfirmation.CONFIRMED,),
            text="H100",
        ),
        sort=(QuotaSort(QuotaSortField.EFFECTIVE, SortDirection.DESC),),
    )
    identity = EffectiveQuotaSliceIdentity(
        resource_scope=scope,
        service="compute.googleapis.com",
        quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
        dimensions=NormalizedDimensions(
            (("gpu_family", "NVIDIA_H100"), ("region", "us-central1"))
        ),
        quota_scope=QuotaScope.REGIONAL,
    )
    item = QuotaQueryItem(
        identity=identity,
        display_name="NVIDIA H100 GPUs",
        accelerator_id=AcceleratorId("nvidia-h100"),
        location="us-central1",
        quota_pool="standard",
        catalog_groups=(CatalogGroupId.COMPUTE_ACCELERATORS,),
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=True,
            guided=False,
            mutable=False,
        ),
        effective_value=QuotaQuantity(64, QuotaUnit("1")),
        usage_value=QuotaQuantity(32, QuotaUnit("1")),
        desired_value=QuotaQuantity(64, QuotaUnit("1")),
        granted_value=QuotaQuantity(64, QuotaUnit("1")),
        reconciliation=Reconciliation.RECONCILING,
        grant_satisfaction=GrantSatisfaction.FULL,
        effective_confirmation=EffectiveConfirmation.CONFIRMED,
        evidence_observed_at=datetime(2026, 7, 22, 7, 59, tzinfo=UTC),
        constraint_set=AcceleratorConstraintSet(
            AcceleratorId("nvidia-h100"),
            (ConstraintReference(identity),),
        ),
    )
    return QuotaQuerySnapshot(
        metadata=QuerySnapshotMetadata(
            snapshot_id="snapshot-public-1",
            query=query,
            catalog=CatalogMetadata(
                ACCELERATOR_CATALOG_SCHEMA,
                "2026-07-22",
                "sha256:" + "b" * 64,
            ),
            evidence_contract=QUOTA_QUERY_EVIDENCE_CONTRACT,
            observed_at=datetime(2026, 7, 22, 8, tzinfo=UTC),
            expires_at=datetime(2026, 7, 22, 9, tzinfo=UTC),
            complete=complete,
            source_coverage=tuple(
                (
                    ProviderSourceCoverage.complete(
                        service,
                        pages_attempted=2,
                        pages_completed=2,
                        observed_at=datetime(2026, 7, 22, 8, tzinfo=UTC),
                    )
                    if complete
                    else ProviderSourceCoverage.incomplete(
                        service,
                        pages_attempted=2,
                        pages_completed=1,
                        observed_at=datetime(2026, 7, 22, 8, tzinfo=UTC),
                    )
                )
                if service == "compute.googleapis.com"
                else ProviderSourceCoverage.intentionally_unqueried(service)
                for service in V1_PROVIDER_SERVICES
            ),
        ),
        items=(item,),
    )


def _legacy_snapshot_record(
    schema: str,
    *,
    source_kind: str = "service",
    source_value: str = "compute.googleapis.com",
    filter_services: tuple[str, ...] | None = None,
) -> bytes:
    """Return canonical bytes matching the retained v1 or v2 release shape."""
    document = json.loads(encode_snapshot_record(quota_snapshot()))
    document["schema"] = schema
    snapshot = document["snapshot"]
    metadata = snapshot["metadata"]
    metadata.pop("inventory_revision")
    metadata.pop("source_coverage")
    snapshot.pop("ordered_slice_identities")
    query = metadata["query"]
    query["source"] = {"kind": source_kind, "value": source_value}
    query["filters"].pop("catalog_groups")
    if filter_services is not None:
        query["filters"]["services"] = list(filter_services)
    for item in snapshot["items"]:
        item.pop("catalog_groups")
        if schema.endswith("/v1"):
            constraint_sets = item.pop("constraint_sets")
            item["constraint_set"] = (
                constraint_sets[0] if len(constraint_sets) == 1 else None
            )
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


@pytest.mark.parametrize("complete", [True, False])
def test_snapshot_record_round_trips_as_canonical_safe_json(
    complete: bool,  # noqa: FBT001
) -> None:
    """Complete and incomplete normalized evidence retain one exact safe shape."""
    snapshot = quota_snapshot(complete=complete)

    encoded = encode_snapshot_record(snapshot)

    assert encoded.endswith(b"\n")
    assert encoded == encode_snapshot_record(decode_snapshot_record(encoded))
    assert decode_snapshot_record(encoded) == snapshot
    document = json.loads(encoded)
    assert document["schema"] == "cqmgr.quota-query-snapshot/v3"
    assert document["snapshot"]["metadata"]["inventory_revision"] == (
        "cqmgr.provider-inventory/v1"
    )
    assert document["snapshot"]["ordered_slice_identities"] == [
        document["snapshot"]["items"][0]["identity"]
    ]
    assert "constraint_sets" in document["snapshot"]["items"][0]
    assert "constraint_set" not in document["snapshot"]["items"][0]
    for forbidden in (
        "provider-page-token",
        "private-credential",
        "contact@example.com",
        "/Users/private",
        "native-keyring",
    ):
        assert forbidden not in encoded.decode()


@pytest.mark.parametrize(
    "schema",
    ["cqmgr.quota-query-snapshot/v1", "cqmgr.quota-query-snapshot/v2"],
)
def test_snapshot_decoder_migrates_supported_legacy_snapshot(schema: str) -> None:
    """Unexpired V1-provider snapshots remain usable after a schema upgrade."""
    decoded = decode_snapshot_record(_legacy_snapshot_record(schema))

    assert decoded.metadata.query.services == ("compute.googleapis.com",)
    assert decoded.metadata.complete is True
    assert decoded.metadata.source_coverage == (
        ProviderSourceCoverage.complete(
            "compute.googleapis.com",
            pages_attempted=0,
            pages_completed=0,
            observed_at=decoded.metadata.observed_at,
        ),
        ProviderSourceCoverage.intentionally_unqueried("tpu.googleapis.com"),
    )
    assert decoded.items[0].constraint_sets == (decoded.items[0].constraint_set,)
    migrated = json.loads(encode_snapshot_record(decoded))
    assert migrated["schema"] == "cqmgr.quota-query-snapshot/v3"
    assert migrated["snapshot"]["ordered_slice_identities"] == [
        migrated["snapshot"]["items"][0]["identity"]
    ]


def test_snapshot_decoder_migrates_supported_legacy_catalog_group() -> None:
    """A legacy group source becomes the equivalent fixed-inventory facet."""
    decoded = decode_snapshot_record(
        _legacy_snapshot_record(
            "cqmgr.quota-query-snapshot/v2",
            source_kind="catalog-group",
            source_value=CatalogGroupId.COMPUTE_ACCELERATORS.value,
        )
    )

    assert decoded.metadata.query.filters.catalog_groups == (
        CatalogGroupId.COMPUTE_ACCELERATORS,
    )
    assert decoded.metadata.query.services == ("compute.googleapis.com",)
    assert decoded.items[0].catalog_groups == (CatalogGroupId.COMPUTE_ACCELERATORS,)


@pytest.mark.parametrize(
    ("source_kind", "source_value"),
    [
        ("service", "example.googleapis.com"),
        ("catalog-group", "unknown-accelerators"),
        ("unknown", "compute.googleapis.com"),
    ],
)
def test_snapshot_decoder_rejects_incompatible_legacy_source(
    source_kind: str,
    source_value: str,
) -> None:
    """Legacy data migrates only when its source belongs to the fixed inventory."""
    encoded = _legacy_snapshot_record(
        "cqmgr.quota-query-snapshot/v2",
        source_kind=source_kind,
        source_value=source_value,
    )

    with pytest.raises(QuotaSnapshotStoredDataError, match="source"):
        decode_snapshot_record(encoded)


def test_snapshot_decoder_rejects_legacy_source_filter_conflict() -> None:
    """A legacy source cannot be remapped to a different fixed provider."""
    encoded = _legacy_snapshot_record(
        "cqmgr.quota-query-snapshot/v2",
        filter_services=("tpu.googleapis.com",),
    )

    with pytest.raises(QuotaSnapshotStoredDataError, match="conflicts"):
        decode_snapshot_record(encoded)


def test_snapshot_decoder_rejects_noncanonical_legacy_bytes() -> None:
    """A supported legacy schema does not weaken canonical-byte validation."""
    canonical = _legacy_snapshot_record("cqmgr.quota-query-snapshot/v2")
    noncanonical = canonical.replace(b'"schema":', b'"schema": ')

    with pytest.raises(QuotaSnapshotStoredDataError, match="not canonical"):
        decode_snapshot_record(noncanonical)


@pytest.mark.parametrize(
    "document",
    [
        b'{"schema":"cqmgr.quota-query-snapshot/v4"}\n',
        b'{"schema":"cqmgr.quota-query-snapshot/v0","snapshot":{}}\n',
        b'{"schema":"cqmgr.quota-query-snapshot/v1","unknown":true}\n',
        b"not-json\n",
    ],
)
def test_snapshot_decoder_rejects_newer_unknown_and_corrupt_state(
    document: bytes,
) -> None:
    """Stored schema drift and corruption fail closed without partial evidence."""
    expected = (
        UnsupportedQuotaSnapshotSchemaError
        if b"/v4" in document
        else QuotaSnapshotStoredDataError
    )
    with pytest.raises(expected):
        decode_snapshot_record(document)


def test_snapshot_decoder_rejects_noncanonical_bytes() -> None:
    """Whitespace changes cannot create a second representation of one record."""
    canonical = encode_snapshot_record(quota_snapshot())
    noncanonical = canonical.replace(b'"schema":', b'"schema": ')

    with pytest.raises(QuotaSnapshotStoredDataError, match="not canonical"):
        decode_snapshot_record(noncanonical)


def test_snapshot_decoder_rejects_negative_retained_usage() -> None:
    """The current snapshot generation cannot restore impossible quota usage."""
    document = json.loads(encode_snapshot_record(quota_snapshot()))
    item = document["snapshot"]["items"][0]
    item["usage_value"]["value"] = "-1"
    encoded = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()

    with pytest.raises(QuotaSnapshotStoredDataError, match="malformed"):
        decode_snapshot_record(encoded)


def test_cursor_binding_decoder_rejects_noncanonical_bytes() -> None:
    """Internal cursor bindings also have exactly one persisted byte shape."""
    canonical = encode_cursor_binding("snapshot-public-1", 1)
    assert decode_cursor_binding(canonical) == ("snapshot-public-1", 1)

    noncanonical = canonical.removesuffix(b"\n")
    with pytest.raises(QuotaSnapshotStoredDataError, match="not canonical"):
        decode_cursor_binding(noncanonical)


def test_snapshot_codec_round_trips_catalog_group_and_constraint_references() -> None:
    """Retained rows preserve guided group selection and exact related slices."""
    snapshot = quota_snapshot()
    item = snapshot.items[0]
    accelerator_id = cast("AcceleratorId", item.accelerator_id)
    first_companion = replace(item.identity, quota_id="GPUS-ALL-REGIONS-primary")
    second_companion = replace(item.identity, quota_id="GPUS-ALL-REGIONS-secondary")
    first_set = AcceleratorConstraintSet(
        accelerator_id,
        (ConstraintReference(item.identity), ConstraintReference(first_companion)),
    )
    second_set = AcceleratorConstraintSet(
        accelerator_id,
        (ConstraintReference(item.identity), ConstraintReference(second_companion)),
    )
    constrained = replace(
        item,
        constraint_set=None,
        constraint_sets=(second_set, first_set),
    )
    grouped = replace(
        snapshot,
        metadata=replace(
            snapshot.metadata,
            query=replace(
                snapshot.metadata.query,
                filters=replace(
                    snapshot.metadata.query.filters,
                    catalog_groups=(CatalogGroupId.COMPUTE_ACCELERATORS,),
                ),
            ),
        ),
        items=(constrained,),
    )

    encoded = encode_snapshot_record(grouped)

    assert decode_snapshot_record(encoded) == grouped
    assert len(json.loads(encoded)["snapshot"]["items"][0]["constraint_sets"]) == len(
        grouped.items[0].constraint_sets
    )


@pytest.mark.parametrize("schema", ["v1", "v2"])
def test_snapshot_codec_rejects_superseded_source_bound_schemas(schema: str) -> None:
    """Pre-federation query snapshots fail closed instead of gaining new meaning."""
    document = json.loads(encode_snapshot_record(quota_snapshot()))
    document["schema"] = f"cqmgr.quota-query-snapshot/{schema}"
    encoded = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()

    with pytest.raises(QuotaSnapshotStoredDataError):
        decode_snapshot_record(encoded)


def test_codec_entrypoints_reject_wrong_types_and_invalid_cursor_values() -> None:
    """Codec boundaries never coerce application objects or cursor positions."""
    with pytest.raises(TypeError, match="QuotaQuerySnapshot"):
        encode_snapshot_record(cast("QuotaQuerySnapshot", object()))
    with pytest.raises(TypeError, match="must be bytes"):
        decode_snapshot_record(cast("bytes", "json"))
    with pytest.raises(TypeError, match="must be bytes"):
        decode_cursor_binding(cast("bytes", "json"))
    for snapshot_id, offset in (("", 0), ("snapshot-1", -1), ("snapshot-1", True)):
        with pytest.raises(ValueError, match="cursor"):
            encode_cursor_binding(snapshot_id, cast("int", offset))


@pytest.mark.parametrize(
    "document",
    [
        {"schema": "cqmgr.quota-query-cursor/v2", "snapshot_id": "s", "offset": 0},
        {"schema": "cqmgr.quota-query-cursor/v0", "snapshot_id": "s", "offset": 0},
        {"schema": "cqmgr.quota-query-cursor/v1", "snapshot_id": "s", "offset": -1},
        {"schema": "cqmgr.quota-query-cursor/v1", "snapshot_id": 1, "offset": 0},
        {"schema": "cqmgr.quota-query-cursor/v1", "snapshot_id": "s"},
    ],
)
def test_cursor_binding_decoder_rejects_untrusted_state(
    document: dict[str, object],
) -> None:
    """Unknown schemas and malformed local bindings fail closed."""
    encoded = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    expected = (
        UnsupportedQuotaSnapshotSchemaError
        if document["schema"] == "cqmgr.quota-query-cursor/v2"
        else QuotaSnapshotStoredDataError
    )
    with pytest.raises(expected):
        decode_cursor_binding(encoded)

    with pytest.raises(QuotaSnapshotStoredDataError, match="malformed"):
        decode_cursor_binding(b"not-json\n")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("snapshot", "metadata", "query", "filters", "services"), ["unknown"]),
        (("snapshot", "metadata", "observed_at"), "2026-07-22T08:00:00+01:00"),
        (("snapshot", "items", 0, "identity", "dimensions"), [["region"]]),
        (("snapshot", "items", 0, "effective_value", "value"), "01"),
        (("snapshot", "items", 0, "predicates", "guided"), 1),
        (("snapshot", "items"), {}),
    ],
)
def test_snapshot_decoder_rejects_semantically_invalid_canonical_state(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    """Canonical JSON syntax cannot legitimize invalid retained evidence."""
    document = json.loads(encode_snapshot_record(quota_snapshot()))
    target: object = document
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    encoded = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()

    with pytest.raises(QuotaSnapshotStoredDataError):
        decode_snapshot_record(encoded)


def test_snapshot_decoder_rejects_unsafe_retained_text() -> None:
    """Unsafe path material is rejected even when injected directly on disk."""
    document = json.loads(encode_snapshot_record(quota_snapshot()))
    document["snapshot"]["items"][0]["display_name"] = "/Users/private/quota.json"
    encoded = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()

    with pytest.raises(QuotaSnapshotStoredDataError, match="unsafe evidence"):
        decode_snapshot_record(encoded)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "/Users/private/quota.json",
        "contact@example.com",
        "native-keyring",
        "ya29.provider-credential",
    ],
)
def test_snapshot_encoder_rejects_unsafe_evidence_text(unsafe_text: str) -> None:
    """Retained evidence cannot carry paths, contacts, or credential material."""
    snapshot = quota_snapshot()
    unsafe = replace(
        snapshot,
        items=(replace(snapshot.items[0], display_name=unsafe_text),),
    )

    with pytest.raises(ValueError, match="unsafe evidence text"):
        encode_snapshot_record(unsafe)
