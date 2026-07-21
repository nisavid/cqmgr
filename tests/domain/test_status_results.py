"""Provider-neutral status and operation-result contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
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
    Provenance,
    ProviderPreferenceIdentity,
    StableSymbol,
    WatchEvent,
    WatchRequestIdentity,
)
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    EffectiveConfirmation,
    Headline,
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
    WatchDisposition,
    derive_grant_satisfaction,
)

UNIT = QuotaUnit("1")
NOW = datetime(2026, 7, 21, tzinfo=UTC)
NAIVE = NOW.replace(tzinfo=None)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
SLICE = EffectiveQuotaSliceIdentity(
    resource_scope=SCOPE,
    service="compute.googleapis.com",
    quota_id="GPUS-PER-PROJECT",
    dimensions=NormalizedDimensions(),
    quota_scope=QuotaScope.GLOBAL,
)


def quantity(value: int) -> QuotaQuantity:
    """Build one dimensionless quota quantity."""
    return QuotaQuantity(value, UNIT)


def watch_request() -> WatchRequestIdentity:
    """Build one complete watched request identity."""
    return WatchRequestIdentity(
        resource_scope=SCOPE,
        condition=WatchCondition.FULFILLED,
        intent_id="sha256:opaque-plan-digest",
        target=quantity(8),
        provider_preference=ProviderPreferenceIdentity(
            canonical_name=(
                "projects/123456789/locations/global/quotaPreferences/gpu-global"
            ),
            slice_identity=SLICE,
        ),
    )


def fulfilled_status() -> QuotaRequestStatus:
    """Build status bound to the watched request target."""
    return QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(8),
        effective=quantity(8),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )


def pending_fulfillment_status() -> QuotaRequestStatus:
    """Build a full grant still awaiting effective confirmation."""
    return QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(8),
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )


@given(
    baseline=st.integers(min_value=-(2**63), max_value=(2**63) - 1_000_002),
    offset=st.integers(min_value=2, max_value=1_000_000),
)
def test_grant_satisfaction_uses_the_pre_request_baseline(
    baseline: int, offset: int
) -> None:
    """None and partial are relative to the requested absolute change."""
    target = baseline + offset

    unknown = derive_grant_satisfaction(quantity(baseline), quantity(target), None)
    assert unknown.value == "unknown"
    assert (
        derive_grant_satisfaction(
            quantity(baseline), quantity(target), quantity(baseline)
        ).value
        == "none"
    )
    assert (
        derive_grant_satisfaction(
            quantity(baseline), quantity(target), quantity(baseline + 1)
        ).value
        == "partial"
    )
    assert (
        derive_grant_satisfaction(
            quantity(baseline), quantity(target), quantity(target)
        ).value
        == "full"
    )
    assert (
        derive_grant_satisfaction(
            quantity(baseline), quantity(target), quantity(target + 1)
        ).value
        == "unknown"
    )


def test_status_axes_remain_independent_and_derive_headlines() -> None:
    """Granted and fulfilled remain stronger derived conditions."""
    granted = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(8),
        effective=quantity(7),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )
    fulfilled = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(8),
        effective=quantity(8),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )

    assert granted.effective_confirmation is EffectiveConfirmation.MISMATCH
    assert granted.is_granted
    assert not granted.is_fulfilled
    assert granted.headline is Headline.GRANTED
    assert fulfilled.is_fulfilled
    assert fulfilled.headline is Headline.FULFILLED
    assert fulfilled.watch(WatchCondition.FULFILLED) is WatchDisposition.REACHED


def test_status_derivation_rejects_or_preserves_unclassifiable_evidence() -> None:
    """Missing baselines stay unknown and cross-unit facts fail closed."""
    count = QuotaUnit("count")

    assert derive_grant_satisfaction(None, quantity(8), quantity(7)).value == "unknown"
    assert (
        derive_grant_satisfaction(
            quantity(4), quantity(8), QuotaQuantity(6, count)
        ).value
        == "unknown"
    )
    with pytest.raises(ValueError, match="one explicit unit"):
        QuotaRequestStatus.derive(
            reconciliation=Reconciliation.SETTLED,
            baseline=quantity(4),
            desired=quantity(8),
            granted=QuotaQuantity(8, count),
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )
    with pytest.raises(ValueError, match="status_observed_at"):
        QuotaRequestStatus.derive(
            reconciliation=Reconciliation.UNKNOWN,
            baseline=None,
            desired=quantity(8),
            granted=None,
            effective=None,
            status_observed_at=NAIVE,
            effective_observed_at=None,
        )
    with pytest.raises(ValueError, match="effective_observed_at"):
        QuotaRequestStatus.derive(
            reconciliation=Reconciliation.UNKNOWN,
            baseline=None,
            desired=quantity(8),
            granted=None,
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=NAIVE,
        )
    provider_state = ProviderSymbol("FUTURE_STATE", Reconciliation)
    unknown = QuotaRequestStatus.derive(
        reconciliation=provider_state,
        baseline=None,
        desired=quantity(8),
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )
    assert unknown.reconciliation is Reconciliation.UNKNOWN
    assert unknown.provider_reconciliation is provider_state


@pytest.mark.parametrize(
    ("reconciliation", "headline"),
    [
        (Reconciliation.SUBMITTED, Headline.SUBMITTED),
        (Reconciliation.RECONCILING, Headline.RECONCILING),
        (Reconciliation.SETTLED, Headline.REQUEST_SETTLED),
        (Reconciliation.FAILED, Headline.FAILED),
        (Reconciliation.SUPERSEDED, Headline.SUPERSEDED),
        (Reconciliation.UNKNOWN, Headline.UNKNOWN),
    ],
)
def test_status_headline_preserves_reconciliation(
    reconciliation: Reconciliation, headline: Headline
) -> None:
    """A concise headline never flattens the underlying axes."""
    status = QuotaRequestStatus.derive(
        reconciliation=reconciliation,
        baseline=quantity(4),
        desired=quantity(8),
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )

    assert status.headline is headline


def test_effective_freshness_and_watch_disposition_fail_closed() -> None:
    """Stale evidence cannot fulfill, and conclusive adverse states stop Watch."""
    stale = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(8),
        effective=quantity(8),
        status_observed_at=NOW,
        effective_observed_at=NOW - timedelta(seconds=1),
    )
    partial = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(6),
        effective=quantity(6),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )

    assert stale.effective_confirmation is EffectiveConfirmation.STALE
    assert stale.watch(WatchCondition.GRANTED) is WatchDisposition.REACHED
    assert stale.watch(WatchCondition.FULFILLED) is WatchDisposition.PENDING
    assert partial.watch(WatchCondition.GRANTED) is WatchDisposition.UNMET
    assert partial.watch(WatchCondition.FULFILLED) is WatchDisposition.UNMET
    failed = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.FAILED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )
    assert failed.watch(WatchCondition.GRANTED) is WatchDisposition.UNMET


def test_result_invariants_bind_success_completeness_and_boundary() -> None:
    """Exit zero is exactly a reached boundary with complete evidence."""
    boundary = OperationBoundary(StableSymbol("inspect-slice"), reached=True)
    result = OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=None,
        boundary=boundary,
        outcome=Outcome(StableSymbol("inspected"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data={"slice": "opaque"},
    )

    assert result.succeeded
    with pytest.raises(ValueError, match="success boundary"):
        OperationResult(
            operation=result.operation,
            resource_scope=None,
            boundary=OperationBoundary(boundary.condition, reached=False),
            outcome=result.outcome,
            completeness=result.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
        )
    with pytest.raises(ValueError, match="finished_at"):
        OperationResult(
            operation=result.operation,
            resource_scope=None,
            boundary=boundary,
            outcome=result.outcome,
            completeness=result.completeness,
            started_at=NOW,
            finished_at=NOW - timedelta(seconds=1),
            data=None,
        )


def test_incomplete_results_name_gaps_and_use_exit_six() -> None:
    """Incomplete evidence stays usable, visible, and non-successful."""
    gap = EvidenceGap(StableSymbol("monitoring-usage"), StableSymbol("source-failed"))
    incomplete = Completeness.incomplete(gap)
    result = OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol("inspect-slice"), reached=False),
        outcome=Outcome(
            StableSymbol("incomplete-evidence"), ExitClass.INCOMPLETE_EVIDENCE
        ),
        completeness=incomplete,
        started_at=NOW,
        finished_at=NOW,
        data=("usable-slice",),
    )

    assert not result.succeeded
    assert result.completeness.gaps == (gap,)
    with pytest.raises(ValueError, match="exit class 6"):
        OperationResult(
            operation=result.operation,
            resource_scope=None,
            boundary=result.boundary,
            outcome=Outcome(StableSymbol("wrong"), ExitClass.OPERATIONAL_FAILURE),
            completeness=incomplete,
            started_at=NOW,
            finished_at=NOW,
            data=None,
        )
    with pytest.raises(ValueError, match="gap"):
        Completeness(is_complete=False)
    with pytest.raises(ValueError, match="complete evidence"):
        Completeness(is_complete=True, gaps=(gap,))


def test_provenance_and_watch_event_require_utc_and_terminal_result() -> None:
    """Versioned events carry ordered checkpoints and one terminal result."""
    provenance = Provenance(
        source=StableSymbol("cloud-quotas"),
        observed_at=NOW,
        coverage=StableSymbol("complete"),
    )
    terminal_result = OperationResult(
        operation=OperationName("request.watch"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("fulfilled"), reached=False),
        outcome=Outcome(StableSymbol("watch-timeout"), ExitClass.TIMEOUT),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=None,
        provenance=(provenance,),
    )
    terminal = WatchEvent(
        stream_id="stream-1",
        sequence=4,
        event=StableSymbol("terminal"),
        resume="cqmgr.watch-resume/v1:opaque",
        observed_at=NOW,
        request=watch_request(),
        status=pending_fulfillment_status(),
        result=terminal_result,
    )

    assert terminal.result is terminal_result
    with pytest.raises(ValueError, match="terminal"):
        WatchEvent(
            stream_id="stream-1",
            sequence=0,
            event=StableSymbol("status-changed"),
            resume="opaque",
            observed_at=NOW,
            request=watch_request(),
            status=fulfilled_status(),
            result=terminal_result,
        )
    with pytest.raises(ValueError, match="UTC"):
        Provenance(
            source=StableSymbol("cloud-quotas"),
            observed_at=NAIVE,
            coverage=StableSymbol("complete"),
        )


def test_watch_event_rejects_invalid_stream_controls() -> None:
    """Stream ordering, resume identity, and source time fail closed."""
    with pytest.raises(ValueError, match="stream_id"):
        WatchEvent(
            "",
            0,
            StableSymbol("status-changed"),
            "opaque",
            NOW,
            watch_request(),
            fulfilled_status(),
        )
    with pytest.raises(ValueError, match="sequence"):
        WatchEvent(
            "stream-1",
            -1,
            StableSymbol("status-changed"),
            "opaque",
            NOW,
            watch_request(),
            fulfilled_status(),
        )
    with pytest.raises(ValueError, match="resume"):
        WatchEvent(
            "stream-1",
            0,
            StableSymbol("status-changed"),
            "",
            NOW,
            watch_request(),
            fulfilled_status(),
        )
    with pytest.raises(ValueError, match="observed_at"):
        WatchEvent(
            "stream-1",
            0,
            StableSymbol("status-changed"),
            "opaque",
            NAIVE,
            watch_request(),
            fulfilled_status(),
        )
