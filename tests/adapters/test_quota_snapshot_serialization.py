"""Canonical safe serialization for retained quota-query snapshots."""

import json
from dataclasses import replace
from datetime import UTC, datetime

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
    AcceleratorId,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.quota_queries import (
    QUOTA_QUERY_EVIDENCE_CONTRACT,
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
        source=ServiceSource("compute.googleapis.com"),
        filters=QuotaQueryFilters(
            services=("compute.googleapis.com",),
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
    item = QuotaQueryItem(
        identity=EffectiveQuotaSliceIdentity(
            resource_scope=scope,
            service="compute.googleapis.com",
            quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
            dimensions=NormalizedDimensions(
                (("gpu_family", "NVIDIA_H100"), ("region", "us-central1"))
            ),
            quota_scope=QuotaScope.REGIONAL,
        ),
        display_name="NVIDIA H100 GPUs",
        accelerator_id=AcceleratorId("nvidia-h100"),
        location="us-central1",
        quota_pool="standard",
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
        ),
        items=(item,),
    )


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
    assert json.loads(encoded)["schema"] == "cqmgr.quota-query-snapshot/v1"
    for forbidden in (
        "provider-page-token",
        "private-credential",
        "contact@example.com",
        "/Users/private",
        "native-keyring",
    ):
        assert forbidden not in encoded.decode()


@pytest.mark.parametrize(
    "document",
    [
        b'{"schema":"cqmgr.quota-query-snapshot/v2"}\n',
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
        if b"/v2" in document
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


def test_cursor_binding_decoder_rejects_noncanonical_bytes() -> None:
    """Internal cursor bindings also have exactly one persisted byte shape."""
    canonical = encode_cursor_binding("snapshot-public-1", 1)
    assert decode_cursor_binding(canonical) == ("snapshot-public-1", 1)

    noncanonical = canonical.removesuffix(b"\n")
    with pytest.raises(QuotaSnapshotStoredDataError, match="not canonical"):
        decode_cursor_binding(noncanonical)


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
