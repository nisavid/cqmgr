"""Fail-closed operation-result and Watch-record contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.redaction import RedactedText
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
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import QuotaRequestStatus, Reconciliation, WatchCondition

NOW = datetime(2026, 7, 21, tzinfo=UTC)
UNIT = QuotaUnit("1")
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
OTHER_SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/987654321")
SLICE = EffectiveQuotaSliceIdentity(
    resource_scope=SCOPE,
    service="compute.googleapis.com",
    quota_id="GPUS-PER-PROJECT",
    dimensions=NormalizedDimensions(),
    quota_scope=QuotaScope.GLOBAL,
)


def _status() -> QuotaRequestStatus:
    return QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=QuotaQuantity(4, UNIT),
        desired=QuotaQuantity(8, UNIT),
        granted=QuotaQuantity(8, UNIT),
        effective=QuotaQuantity(8, UNIT),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )


def _pending_status() -> QuotaRequestStatus:
    return QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=QuotaQuantity(4, UNIT),
        desired=QuotaQuantity(8, UNIT),
        granted=QuotaQuantity(8, UNIT),
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )


def _watch_request() -> WatchRequestIdentity:
    return WatchRequestIdentity(
        resource_scope=SCOPE,
        condition=WatchCondition.FULFILLED,
        intent_id="sha256:opaque-plan-digest",
        target=QuotaQuantity(8, UNIT),
        provider_preference=ProviderPreferenceIdentity(
            canonical_name=(
                "projects/123456789/locations/global/quotaPreferences/gpu-global"
            ),
            slice_identity=SLICE,
        ),
    )


def _result(
    *,
    boundary: OperationBoundary,
    outcome: Outcome,
    completeness: Completeness,
) -> OperationResult[None]:
    return OperationResult(
        operation=OperationName("request.watch"),
        resource_scope=SCOPE,
        boundary=boundary,
        outcome=outcome,
        completeness=completeness,
        started_at=NOW,
        finished_at=NOW,
        data=None,
    )


def _terminal_result() -> OperationResult[None]:
    return _result(
        boundary=OperationBoundary(
            StableSymbol(WatchCondition.FULFILLED.value), reached=False
        ),
        outcome=Outcome(StableSymbol("watch-timeout"), ExitClass.TIMEOUT),
        completeness=Completeness.complete(),
    )


def test_operation_names_are_distinct_from_kebab_case_symbols() -> None:
    """Dotted operation names do not relax stable enum grammar."""
    assert OperationName("request.watch").value == "request.watch"
    assert StableSymbol("watch-timeout").value == "watch-timeout"
    with pytest.raises(ValueError, match="stable symbol"):
        StableSymbol("watch.timeout")


def test_result_values_reject_unknown_closed_exit_classes() -> None:
    """Raw integers cannot enter the closed global exit vocabulary."""
    with pytest.raises(TypeError, match="ExitClass"):
        Outcome(StableSymbol("unknown"), cast("ExitClass", 42))


def test_completeness_distinguishes_partial_from_unavailable_evidence() -> None:
    """Exit six belongs exactly to usable incomplete observations."""
    gap = EvidenceGap(StableSymbol("provider"), StableSymbol("unavailable"))
    boundary = OperationBoundary(StableSymbol("fulfilled"), reached=False)

    partial = _result(
        boundary=boundary,
        outcome=Outcome(
            StableSymbol("incomplete-evidence"), ExitClass.INCOMPLETE_EVIDENCE
        ),
        completeness=Completeness.incomplete(gap),
    )
    unavailable = _result(
        boundary=boundary,
        outcome=Outcome(
            StableSymbol("provider-unavailable"), ExitClass.OPERATIONAL_FAILURE
        ),
        completeness=Completeness.unavailable(gap),
    )

    assert partial.completeness.has_partial_data
    assert not unavailable.completeness.has_partial_data
    with pytest.raises(ValueError, match="exit class 6"):
        _result(
            boundary=boundary,
            outcome=Outcome(
                StableSymbol("incomplete-evidence"), ExitClass.INCOMPLETE_EVIDENCE
            ),
            completeness=Completeness.complete(),
        )
    with pytest.raises(ValueError, match="cannot be reached"):
        _result(
            boundary=OperationBoundary(StableSymbol("fulfilled"), reached=True),
            outcome=Outcome(
                StableSymbol("incomplete-evidence"), ExitClass.INCOMPLETE_EVIDENCE
            ),
            completeness=Completeness.incomplete(gap),
        )


def test_provenance_carries_safe_interval_status_and_request_identity() -> None:
    """V1 provenance can represent source intervals and safe lifecycle facts."""
    provenance = Provenance(
        source=StableSymbol("cloud-quotas"),
        observed_at=NOW,
        coverage=StableSymbol("complete"),
        interval_started_at=NOW - timedelta(minutes=5),
        interval_finished_at=NOW,
        lifecycle_or_preview_status=RedactedText("settled"),
        request_identity=RedactedText("quotaPreferences/gpu-global"),
    )

    assert provenance.interval_finished_at == NOW
    assert str(provenance.lifecycle_or_preview_status) == "settled"
    with pytest.raises(ValueError, match="interval_finished_at"):
        Provenance(
            source=StableSymbol("monitoring"),
            observed_at=NOW,
            coverage=StableSymbol("complete"),
            interval_started_at=NOW,
            interval_finished_at=NOW - timedelta(seconds=1),
        )


def test_watch_events_require_complete_typed_request_and_status() -> None:
    """A Watch record cannot carry an empty or provider-defined identity."""
    event = WatchEvent(
        stream_id="stream-1",
        sequence=0,
        event=StableSymbol("status-changed"),
        resume="cqmgr.watch-resume/v1:opaque",
        observed_at=NOW,
        request=_watch_request(),
        status=_status(),
    )

    assert event.request.provider_preference.slice_identity == SLICE
    with pytest.raises(TypeError, match="WatchRequestIdentity"):
        WatchEvent(
            stream_id="stream-1",
            sequence=0,
            event=StableSymbol("status-changed"),
            resume="opaque",
            observed_at=NOW,
            request=cast("WatchRequestIdentity", None),
            status=_status(),
        )


def test_watch_event_rejects_status_for_a_different_target() -> None:
    """Every event binds status evidence to its complete watched intent."""
    mismatched = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=QuotaQuantity(4, UNIT),
        desired=QuotaQuantity(9, UNIT),
        granted=QuotaQuantity(9, UNIT),
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )

    with pytest.raises(ValueError, match="target"):
        WatchEvent(
            stream_id="stream-1",
            sequence=0,
            event=StableSymbol("status-changed"),
            resume="opaque",
            observed_at=NOW,
            request=_watch_request(),
            status=mismatched,
        )


def test_preference_name_is_bound_to_its_exact_resource_scope() -> None:
    """A canonical preference name cannot contradict its typed slice identity."""
    with pytest.raises(ValueError, match="resource scope"):
        ProviderPreferenceIdentity(
            canonical_name=(
                "projects/987654321/locations/global/quotaPreferences/gpu-global"
            ),
            slice_identity=SLICE,
        )


@pytest.mark.parametrize(
    "contradictory",
    [
        replace(_terminal_result(), operation=OperationName("quota.inspect")),
        replace(_terminal_result(), resource_scope=OTHER_SCOPE),
        replace(
            _terminal_result(),
            boundary=OperationBoundary(
                StableSymbol(WatchCondition.GRANTED.value), reached=False
            ),
        ),
    ],
)
def test_terminal_result_is_bound_to_the_watched_request(
    contradictory: OperationResult[None],
) -> None:
    """A terminal record cannot substitute another operation, scope, or condition."""
    with pytest.raises(ValueError, match="terminal Watch result"):
        WatchEvent(
            stream_id="stream-1",
            sequence=1,
            event=StableSymbol("terminal"),
            resume="opaque",
            observed_at=NOW,
            request=_watch_request(),
            status=_pending_status(),
            result=contradictory,
        )


def test_terminal_outcome_matches_the_attached_status_disposition() -> None:
    """Terminal success, unmet, and pending outcomes cannot contradict status."""
    success = _result(
        boundary=OperationBoundary(
            StableSymbol(WatchCondition.FULFILLED.value), reached=True
        ),
        outcome=Outcome(StableSymbol("fulfilled"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
    )
    unmet = _result(
        boundary=OperationBoundary(
            StableSymbol(WatchCondition.FULFILLED.value), reached=False
        ),
        outcome=Outcome(
            StableSymbol("requested-outcome-unmet"),
            ExitClass.REQUESTED_OUTCOME_UNMET,
        ),
        completeness=Completeness.complete(),
    )

    with pytest.raises(ValueError, match="status disposition"):
        WatchEvent(
            "stream-1",
            1,
            StableSymbol("terminal"),
            "opaque",
            NOW,
            _watch_request(),
            _pending_status(),
            success,
        )
    with pytest.raises(ValueError, match="status disposition"):
        WatchEvent(
            "stream-1",
            1,
            StableSymbol("terminal"),
            "opaque",
            NOW,
            _watch_request(),
            _status(),
            unmet,
        )
