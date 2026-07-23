"""Lossless structured serialization for provider numeric evidence."""

from datetime import UTC, datetime

from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.application.operations.quotas import QuotaInspectData
from cqmgr.domain.catalog import CatalogPredicates
from cqmgr.domain.quota_queries import QuotaQueryItem
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    MonitoringValue,
    MonitoringValueKind,
    NormalizedDimensions,
    QuotaPreferenceEvidence,
    QuotaPreferenceOrigin,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import (
    Completeness,
    EvidenceGap,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 23, tzinfo=UTC)
MAX_INT64 = (1 << 63) - 1
LOCAL_COUNT = 2


def test_provider_integers_use_lossless_decimal_strings() -> None:
    """Provider int64 values never cross JSON boundaries as lossy numbers."""
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    identity = EffectiveQuotaSliceIdentity(
        scope,
        "compute.googleapis.com",
        "GPUS-ALL-REGIONS-per-project",
        NormalizedDimensions(()),
        QuotaScope.GLOBAL,
    )
    preference = QuotaPreferenceEvidence(
        provider_name="projects/123/locations/global/quotaPreferences/public",
        identity=identity,
        preferred_value=MAX_INT64,
        granted_value=MAX_INT64 - 1,
        etag=None,
        reconciling=False,
        state_detail=None,
        trace_id=None,
        create_time=None,
        update_time=None,
        request_origin=ProviderSymbol(
            QuotaPreferenceOrigin.CLOUD_CONSOLE.value,
            QuotaPreferenceOrigin,
        ),
    )
    item = QuotaQueryItem(
        identity=identity,
        display_name=None,
        accelerator_id=None,
        location="global",
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=False,
            guided=False,
            mutable=False,
        ),
        effective_value=QuotaQuantity(MAX_INT64, QuotaUnit("{requests}")),
    )
    inspect_data = QuotaInspectData(
        identity=identity,
        evidence=None,
        item=item,
        preference=preference,
        usage=None,
        status=None,
        constraint_set=None,
    )
    result = OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=scope,
        boundary=OperationBoundary(
            StableSymbol("exact-slice-inspected"),
            reached=True,
        ),
        outcome=Outcome(StableSymbol("slice-inspected"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data={
            "quantity": QuotaQuantity(MAX_INT64, QuotaUnit("1")),
            "monitoring": MonitoringValue(MonitoringValueKind.INT64, MAX_INT64),
            "inspect": inspect_data,
            "local_count": LOCAL_COUNT,
        },
    )

    data = operation_result_mapping(result)["data"]

    assert isinstance(data, dict)
    assert data["quantity"] == {"value": str(MAX_INT64), "unit": "1"}
    assert data["monitoring"] == {"kind": "int64", "value": str(MAX_INT64)}
    serialized_inspect = data["inspect"]
    assert isinstance(serialized_inspect, dict)
    serialized_preference = serialized_inspect["preference"]
    assert isinstance(serialized_preference, dict)
    assert serialized_preference["preferred_value"] == {
        "value": str(MAX_INT64),
        "unit": "{requests}",
    }
    assert serialized_preference["granted_value"] == {
        "value": str(MAX_INT64 - 1),
        "unit": "{requests}",
    }
    assert data["local_count"] == LOCAL_COUNT


def test_preference_values_do_not_borrow_unit_for_mismatched_identity() -> None:
    """Structured preference values require the preference's exact slice identity."""
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    identity = EffectiveQuotaSliceIdentity(
        scope,
        "compute.googleapis.com",
        "GPUS-ALL-REGIONS-per-project",
        NormalizedDimensions(()),
        QuotaScope.GLOBAL,
    )
    preference_identity = EffectiveQuotaSliceIdentity(
        scope,
        "compute.googleapis.com",
        "CPUS-ALL-REGIONS-per-project",
        NormalizedDimensions(()),
        QuotaScope.GLOBAL,
    )
    preference = QuotaPreferenceEvidence(
        provider_name="projects/123/locations/global/quotaPreferences/public",
        identity=preference_identity,
        preferred_value=MAX_INT64,
        granted_value=None,
        etag=None,
        reconciling=False,
        state_detail=None,
        trace_id=None,
        create_time=None,
        update_time=None,
        request_origin=ProviderSymbol(
            QuotaPreferenceOrigin.CLOUD_CONSOLE.value,
            QuotaPreferenceOrigin,
        ),
    )
    item = QuotaQueryItem(
        identity=identity,
        display_name=None,
        accelerator_id=None,
        location="global",
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=False,
            guided=False,
            mutable=False,
        ),
        effective_value=QuotaQuantity(MAX_INT64, QuotaUnit("{cores}")),
    )
    result = OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=scope,
        boundary=OperationBoundary(
            StableSymbol("exact-slice-inspected"),
            reached=False,
        ),
        outcome=Outcome(
            StableSymbol("incomplete-evidence"),
            ExitClass.INCOMPLETE_EVIDENCE,
        ),
        completeness=Completeness.incomplete(
            EvidenceGap(
                StableSymbol("cloud-quotas"),
                StableSymbol("native-unit-unavailable"),
            )
        ),
        started_at=NOW,
        finished_at=NOW,
        data=QuotaInspectData(
            identity=identity,
            evidence=None,
            item=item,
            preference=preference,
            usage=None,
            status=None,
            constraint_set=None,
        ),
    )

    data = operation_result_mapping(result)["data"]

    assert isinstance(data, dict)
    serialized_preference = data["preference"]
    assert isinstance(serialized_preference, dict)
    assert serialized_preference["preferred_value"] == {
        "value": str(MAX_INT64),
        "unit": None,
    }
    assert serialized_preference["granted_value"] is None
