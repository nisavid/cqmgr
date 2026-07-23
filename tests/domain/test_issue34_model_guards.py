"""Failure-closed model contracts for issue 34 catalog and quota queries."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorAttachment,
    AcceleratorCatalogEntry,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogGroupId,
    CatalogLifecycle,
    CatalogLocationCoverage,
    CatalogMetadata,
    CatalogPredicates,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
    ManagementPlane,
    TpuAcceleratorConfig,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
    UnitConversionEvidence,
    WorkloadConsumer,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.quota_queries import (
    QUOTA_QUERY_EVIDENCE_CONTRACT,
    V1_PROVIDER_SERVICES,
    OpaqueQueryCursor,
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
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    Reconciliation,
)

OBSERVED_AT = datetime(2026, 7, 22, 8, tzinfo=UTC)


def _scope() -> ResourceScope:
    return ResourceScope(ResourceScopeKind.PROJECT, "projects/123")


def _identity(quota_id: str = "GPUS-PER-REGION") -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        _scope(),
        "compute.googleapis.com",
        quota_id,
        NormalizedDimensions((("region", "us-central1"),)),
        QuotaScope.REGIONAL,
    )


def _diagnostic() -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode("catalog-location-failed"),
        severity=Severity.ERROR,
        phase=DiagnosticPhase("catalog-read"),
        source=DiagnosticSource("catalog"),
        retry=RetryDisposition.AFTER_BACKOFF,
        message=RedactedText("The catalog read failed."),
    )


def _conversion(unit: str = "card") -> UnitConversionEvidence:
    return UnitConversionEvidence("machine", QuotaUnit(unit), 8, "provider-doc")


def _entry() -> AcceleratorCatalogEntry:
    return AcceleratorCatalogEntry(
        CatalogGroupId.COMPUTE_ACCELERATORS,
        AcceleratorId("nvidia-h100"),
        ManagementPlane.COMPUTE,
        (WorkloadConsumer.COMPUTE_ENGINE,),
        QuotaUnit("card"),
        _conversion(),
    )


def _item(**changes: object) -> QuotaQueryItem:
    values: dict[str, object] = {
        "identity": _identity(),
        "display_name": "NVIDIA H100",
        "accelerator_id": AcceleratorId("nvidia-h100"),
        "location": "us-central1",
        "quota_pool": "standard",
        "predicates": CatalogPredicates(
            discovered=True,
            cataloged=True,
            guided=True,
            mutable=False,
        ),
        "effective_value": QuotaQuantity(8, QuotaUnit("card")),
        "usage_value": QuotaQuantity(4, QuotaUnit("card")),
        "desired_value": QuotaQuantity(8, QuotaUnit("card")),
        "granted_value": QuotaQuantity(8, QuotaUnit("card")),
        "reconciliation": Reconciliation.SETTLED,
        "grant_satisfaction": GrantSatisfaction.FULL,
        "effective_confirmation": EffectiveConfirmation.CONFIRMED,
        "evidence_observed_at": OBSERVED_AT,
        "constraint_set": None,
    }
    values.update(changes)
    return QuotaQueryItem(**values)  # type: ignore[arg-type]


def _metadata(query: QuotaQuery | None = None) -> QuerySnapshotMetadata:
    query = query or QuotaQuery(
        _scope(), filters=QuotaQueryFilters(services=("compute",))
    )
    return QuerySnapshotMetadata(
        snapshot_id="snapshot-1",
        query=query,
        catalog=CatalogMetadata(
            ACCELERATOR_CATALOG_SCHEMA,
            "2026-07-22",
            "sha256:" + "a" * 64,
        ),
        evidence_contract=QUOTA_QUERY_EVIDENCE_CONTRACT,
        observed_at=OBSERVED_AT,
        expires_at=OBSERVED_AT + timedelta(minutes=15),
        complete=True,
        source_coverage=tuple(
            ProviderSourceCoverage.complete(
                service,
                pages_attempted=1,
                pages_completed=1,
                observed_at=OBSERVED_AT,
            )
            if service in query.services
            else ProviderSourceCoverage.intentionally_unqueried(service)
            for service in V1_PROVIDER_SERVICES
        ),
    )


def test_catalog_identity_and_conversion_reject_ambiguous_values() -> None:
    """Public IDs, digest identity, and guided units require exact typed evidence."""
    with pytest.raises(ValueError, match="kebab-case"):
        AcceleratorId("NVIDIA_H100")
    with pytest.raises(TypeError, match="content_digest"):
        CatalogMetadata(ACCELERATOR_CATALOG_SCHEMA, "r1", cast("str", 1))
    with pytest.raises(ValueError, match="source_unit"):
        replace(_conversion(), source_unit="")
    with pytest.raises(TypeError, match="QuotaUnit"):
        replace(_conversion(), quota_unit=cast("QuotaUnit", "card"))
    for invalid_count in (True, 0, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            replace(_conversion(), quota_units_per_source=cast("int", invalid_count))
    with pytest.raises(ValueError, match="source_reference"):
        replace(_conversion(), source_reference="")


def test_catalog_entry_rejects_untyped_or_inconsistent_guidance() -> None:
    """Guidance cannot mix planes, consumers, units, or conversion evidence."""
    entry = _entry()
    invalid_types = (
        ("group_id", "compute-accelerators", TypeError),
        ("accelerator_id", "nvidia-h100", TypeError),
        ("management_plane", "compute", TypeError),
        ("native_quota_unit", "card", TypeError),
        ("conversion", "eight", TypeError),
    )
    for field_name, value, error_type in invalid_types:
        with pytest.raises(error_type):
            replace(entry, **{field_name: value})  # type: ignore[bad-argument-type]
    for consumers in ((), (WorkloadConsumer.GKE, WorkloadConsumer.GKE), ("gke",)):
        with pytest.raises(ValueError, match="unique WorkloadConsumer"):
            replace(
                entry,
                workload_consumers=cast("tuple[WorkloadConsumer, ...]", consumers),
            )
    with pytest.raises(ValueError, match="native quota unit"):
        replace(entry, native_quota_unit=QuotaUnit("chip"))
    assert entry.require_guided_conversion() == _conversion()


def test_constraint_and_attachment_models_reject_lossy_shapes() -> None:
    """Constraint references and accelerator counts stay exact and nonempty."""
    reference = ConstraintReference(_identity())
    constraints = AcceleratorConstraintSet(AcceleratorId("nvidia-h100"), (reference,))
    with pytest.raises(TypeError, match="accelerator_id"):
        replace(constraints, accelerator_id=cast("AcceleratorId", "nvidia-h100"))
    for references in (
        (),
        (reference, reference),
        (cast("ConstraintReference", "ref"),),
    ):
        with pytest.raises(ValueError, match="unique ConstraintReference"):
            replace(constraints, references=references)

    with pytest.raises(ValueError, match="non-empty"):
        AcceleratorAttachment("", 1)
    for count in (True, 0, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            AcceleratorAttachment("nvidia-h100", cast("int", count))


def test_provider_catalog_records_reject_untyped_nested_evidence() -> None:
    """Provider records preserve canonical locations and typed nested collections."""
    machine = ComputeMachineType(
        "a3-highgpu-8g",
        "us-central1-a",
        (AcceleratorAttachment("nvidia-h100", 8),),
        ProviderSymbol("ACTIVE", CatalogLifecycle),
    )
    with pytest.raises(TypeError, match="guest_accelerators"):
        replace(
            machine,
            guest_accelerators=cast("tuple[AcceleratorAttachment, ...]", ("gpu",)),
        )
    with pytest.raises(TypeError, match="lifecycle"):
        replace(machine, lifecycle=cast("ProviderSymbol[CatalogLifecycle]", "ACTIVE"))
    with pytest.raises(TypeError, match="lifecycle"):
        replace(
            machine,
            lifecycle=cast(
                "ProviderSymbol[CatalogLifecycle]",
                ProviderSymbol("ACTIVE", WorkloadConsumer),
            ),
        )

    for field_name in ("version", "topology"):
        with pytest.raises(ValueError, match="non-empty"):
            replace(TpuAcceleratorConfig("v6e", "2x4"), **{field_name: ""})
    accelerator = TpuAcceleratorType(
        "projects/123/locations/us-central1-b/acceleratorTypes/v6e-8",
        "us-central1-b",
        "v6e-8",
        (TpuAcceleratorConfig("v6e", "2x4"),),
    )
    with pytest.raises(TypeError, match="configurations"):
        replace(
            accelerator,
            configurations=cast("tuple[TpuAcceleratorConfig, ...]", ("shape",)),
        )
    with pytest.raises(ValueError, match="accelerator_type"):
        replace(accelerator, accelerator_type="")
    with pytest.raises(ValueError, match="canonical location"):
        TpuLocation("projects/123/locations/us-central1-a", "US-CENTRAL1-A")
    runtime = TpuRuntimeVersion("runtime/1", "us-central1-a", "tpu-vm")
    with pytest.raises(ValueError, match="runtime version"):
        replace(runtime, version="")


def test_catalog_coverage_requires_typed_outcomes_and_failure_reasons() -> None:
    """Missing reads carry diagnostics while authoritative success cannot."""
    success = CatalogLocationCoverage(
        CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
        "us-central1-a",
        LocationCoverageExpectation.REQUESTED,
        LocationCoverageState.SUCCESS,
    )
    invalid_types = (
        ("source", "compute-machine-types"),
        ("expectation", "requested"),
        ("state", "success"),
        ("diagnostics", ("failure",)),
    )
    for field_name, value in invalid_types:
        with pytest.raises(TypeError):
            replace(success, **{field_name: value})  # type: ignore[bad-argument-type]
    with pytest.raises(ValueError, match="requires a reason"):
        replace(success, state=LocationCoverageState.FAILED)
    with pytest.raises(ValueError, match="cannot carry"):
        replace(success, diagnostics=(_diagnostic(),))
    assert replace(
        success,
        state=LocationCoverageState.UNSUPPORTED,
        diagnostics=(_diagnostic(),),
    ).complete


def test_query_sort_and_filters_require_public_typed_values() -> None:
    """Query options reject display text, coercion, and cross-domain values."""
    with pytest.raises(TypeError, match="sort field"):
        QuotaSort(cast("QuotaSortField", "service"))
    with pytest.raises(TypeError, match="sort direction"):
        QuotaSort(QuotaSortField.SERVICE, cast("SortDirection", "asc"))
    with pytest.raises(ValueError, match="supported V1 provider"):
        QuotaQueryFilters(services=("storage.googleapis.com",))

    invalid_filters = (
        {"services": ["compute.googleapis.com"]},
        {"catalog_groups": ("compute-accelerators",)},
        {"accelerators": ("nvidia-h100",)},
        {"locations": ("us-central1-",)},
        {"quota_scopes": ("regional",)},
        {"quota_pools": ("Standard",)},
        {"cataloged": 1},
        {"reconciliations": ("settled",)},
        {"grant_satisfactions": ("full",)},
        {"effective_confirmations": ("confirmed",)},
        {"text": ""},
    )
    for values in invalid_filters:
        with pytest.raises((TypeError, ValueError)):
            QuotaQueryFilters(**values)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="QuotaQueryItem"):
        QuotaQueryFilters().matches(cast("QuotaQueryItem", object()))


def test_query_item_rejects_untrusted_product_evidence() -> None:
    """Snapshot rows cannot accept untyped identities, facets, quantities, or status."""
    invalid_values = (
        ("identity", "slice", TypeError),
        ("display_name", "", ValueError),
        ("accelerator_id", "nvidia-h100", TypeError),
        ("location", "US-CENTRAL1", ValueError),
        ("quota_pool", "Standard", ValueError),
        ("predicates", object(), TypeError),
        ("usage_value", 4, TypeError),
        ("reconciliation", "settled", TypeError),
        ("grant_satisfaction", "full", TypeError),
        ("effective_confirmation", "confirmed", TypeError),
        ("evidence_observed_at", OBSERVED_AT.replace(tzinfo=None), ValueError),
        ("constraint_set", "constraints", TypeError),
    )
    for field_name, value, error_type in invalid_values:
        with pytest.raises(error_type):
            _item(**{field_name: value})
    with pytest.raises(ValueError, match="one native quota unit"):
        _item(usage_value=QuotaQuantity(4, QuotaUnit("chip")))
    with pytest.raises(ValueError, match="usage_value must be non-negative"):
        _item(usage_value=QuotaQuantity(-1, QuotaUnit("card")))
    mismatched = AcceleratorConstraintSet(
        AcceleratorId("nvidia-l4"),
        (ConstraintReference(_identity()),),
    )
    with pytest.raises(ValueError, match="accelerator must match"):
        _item(constraint_set=mismatched)


def test_query_snapshot_binding_rejects_ambiguous_state() -> None:
    """Snapshot identity, lifetime, cursor position, and item types fail closed."""
    query = QuotaQuery(_scope(), filters=QuotaQueryFilters(services=("compute",)))
    with pytest.raises(TypeError, match="resource_scope"):
        replace(query, resource_scope=cast("ResourceScope", "projects/123"))
    with pytest.raises(TypeError, match="filters"):
        replace(query, filters=cast("QuotaQueryFilters", object()))
    with pytest.raises(TypeError, match="sort"):
        replace(query, sort=cast("tuple[QuotaSort, ...]", ("service",)))
    duplicate = QuotaSort(QuotaSortField.SERVICE)
    with pytest.raises(ValueError, match="must not be repeated"):
        replace(query, sort=(duplicate, duplicate))

    metadata = _metadata(query)
    invalid_metadata = (
        ("snapshot_id", "", ValueError),
        ("query", object(), TypeError),
        ("catalog", object(), TypeError),
        ("evidence_contract", "cqmgr.quota-query-evidence/v2", ValueError),
        ("complete", 1, TypeError),
    )
    for field_name, value, error_type in invalid_metadata:
        with pytest.raises(error_type):
            replace(metadata, **{field_name: value})  # type: ignore[bad-argument-type]

    for field_name, value in (("value", ""), ("snapshot_id", ""), ("offset", -1)):
        with pytest.raises(ValueError, match=r"non-empty|non-negative"):
            replace(
                OpaqueQueryCursor("opaque", "snapshot-1", 0),
                **{field_name: value},  # type: ignore[bad-argument-type]
            )
    with pytest.raises(TypeError, match="metadata"):
        QuotaQuerySnapshot(cast("QuerySnapshotMetadata", object()), ())
    with pytest.raises(TypeError, match="items"):
        QuotaQuerySnapshot(metadata, cast("tuple[QuotaQueryItem, ...]", ("item",)))
    with pytest.raises(ValueError, match="bound query source"):
        QuotaQuerySnapshot(
            metadata,
            (
                _item(
                    identity=replace(
                        _identity(),
                        resource_scope=ResourceScope(
                            ResourceScopeKind.PROJECT, "projects/987"
                        ),
                    )
                ),
            ),
        )
    with pytest.raises(ValueError, match="bound query source"):
        QuotaQuerySnapshot(
            metadata,
            (_item(identity=replace(_identity(), service="tpu.googleapis.com")),),
        )
    item = _item()
    with pytest.raises(ValueError, match="must be unique"):
        QuotaQuerySnapshot(metadata, (item, item))


def test_filters_match_text_dimensions_and_every_independent_facet() -> None:
    """Text and facet filters remain conjunctive across normalized evidence."""
    item = _item()
    matching = QuotaQueryFilters(
        services=("compute.googleapis.com",),
        accelerators=(AcceleratorId("nvidia-h100"),),
        locations=("us-central1",),
        quota_scopes=(QuotaScope.REGIONAL,),
        quota_pools=("standard",),
        cataloged=True,
        guided=True,
        mutable=False,
        reconciliations=(Reconciliation.SETTLED,),
        grant_satisfactions=(GrantSatisfaction.FULL,),
        effective_confirmations=(EffectiveConfirmation.CONFIRMED,),
        text="REGION",
    )
    assert matching.matches(item)
    assert not replace(matching, text="not-present").matches(item)
    assert not replace(matching, services=("tpu.googleapis.com",)).matches(item)


def test_snapshot_sorting_covers_equal_missing_text_and_identity_ties() -> None:
    """Sorting is stable for equal values and retains missing evidence last."""
    query = QuotaQuery(
        _scope(),
        filters=QuotaQueryFilters(services=("compute",)),
        sort=(QuotaSort(QuotaSortField.DISPLAY_NAME),),
    )
    left = _item(identity=_identity("A"), display_name="Same")
    right = _item(identity=_identity("B"), display_name="Same")
    missing = _item(identity=_identity("0"), display_name=None)
    snapshot = QuotaQuerySnapshot(_metadata(query), (missing, right, left))
    assert snapshot.sorted_items() == (left, right, missing)
