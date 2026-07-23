"""Fail-closed validation for operation results and Watch events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
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
from cqmgr.domain.schemas import OPERATION_RESULT_SCHEMA, WATCH_EVENT_SCHEMA
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
GAP = EvidenceGap(StableSymbol("cloud-quotas"), StableSymbol("source-failed"))
STATUS_CHANGED = StableSymbol("status-changed")


def _quantity(value: int) -> QuotaQuantity:
    return QuotaQuantity(value, UNIT)


def _preference(*, resource_scope: ResourceScope = SCOPE) -> ProviderPreferenceIdentity:
    slice_identity = EffectiveQuotaSliceIdentity(
        resource_scope=resource_scope,
        service="compute.googleapis.com",
        quota_id="GPUS-PER-PROJECT",
        dimensions=NormalizedDimensions(),
        quota_scope=QuotaScope.GLOBAL,
    )
    return ProviderPreferenceIdentity(
        canonical_name=(
            f"{resource_scope.canonical_name}/locations/global/"
            "quotaPreferences/gpu-global"
        ),
        slice_identity=slice_identity,
    )


def _request() -> WatchRequestIdentity:
    return WatchRequestIdentity(
        resource_scope=SCOPE,
        condition=WatchCondition.FULFILLED,
        intent_id="sha256:opaque-plan-digest",
        target=_quantity(8),
        provider_preference=_preference(),
    )


def _status(*, desired: int = 8) -> QuotaRequestStatus:
    return QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=_quantity(4),
        desired=_quantity(desired),
        granted=_quantity(desired),
        effective=_quantity(desired),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )


def _diagnostic() -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode("provider-response"),
        severity=Severity.INFO,
        phase=DiagnosticPhase("observe"),
        source=DiagnosticSource("cloud-quotas"),
        retry=RetryDisposition.NEVER,
        message=RedactedText("safe provider response"),
    )


def _result() -> OperationResult[None]:
    return OperationResult(
        operation=OperationName("request.watch"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("fulfilled"), reached=True),
        outcome=Outcome(StableSymbol("fulfilled"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=None,
        diagnostics=(_diagnostic(),),
        provenance=(
            Provenance(
                source=StableSymbol("cloud-quotas"),
                observed_at=NOW,
                coverage=StableSymbol("complete"),
            ),
        ),
    )


def _watch_event(
    *,
    event: StableSymbol = STATUS_CHANGED,
    result: OperationResult[None] | None = None,
) -> WatchEvent[None]:
    return WatchEvent(
        stream_id="stream-1",
        sequence=0,
        event=event,
        resume="cqmgr.watch-resume/v1:opaque",
        observed_at=NOW,
        request=_request(),
        status=_status(),
        result=result,
        diagnostics=(_diagnostic(),),
    )


def test_symbols_and_operation_names_use_separate_stable_grammars() -> None:
    """Operation names allow dotted segments while result symbols do not."""
    assert StableSymbol("incomplete-evidence").value == "incomplete-evidence"
    assert OperationName("request.watch").value == "request.watch"

    with pytest.raises(ValueError, match="stable symbol"):
        StableSymbol(cast("str", 7))
    with pytest.raises(ValueError, match="operation name"):
        OperationName("Request.watch")
    with pytest.raises(ValueError, match="operation name"):
        OperationName(cast("str", 7))


def test_boundary_outcome_and_gap_values_require_their_public_types() -> None:
    """Result control values cannot be supplied as raw strings or numbers."""
    with pytest.raises(TypeError, match="boundary condition"):
        OperationBoundary(cast("StableSymbol", "fulfilled"), reached=True)
    with pytest.raises(TypeError, match="boundary reached"):
        OperationBoundary(StableSymbol("fulfilled"), reached=cast("bool", 1))
    with pytest.raises(TypeError, match="outcome code"):
        Outcome(cast("StableSymbol", "fulfilled"), ExitClass.SUCCESS)
    with pytest.raises(TypeError, match="evidence gap"):
        EvidenceGap(cast("StableSymbol", "cloud-quotas"), StableSymbol("failed"))
    with pytest.raises(TypeError, match="evidence gap"):
        EvidenceGap(StableSymbol("cloud-quotas"), cast("StableSymbol", "failed"))


def test_exit_classes_have_one_operation_independent_numeric_contract() -> None:
    """Every supported process result has its documented stable number."""
    assert {exit_class.name: exit_class.value for exit_class in ExitClass} == {
        "SUCCESS": 0,
        "USAGE": 2,
        "REJECTED_PRECONDITION": 3,
        "AUTHORIZATION": 4,
        "STALE_OR_CONFLICTING": 5,
        "INCOMPLETE_EVIDENCE": 6,
        "REQUESTED_OUTCOME_UNMET": 7,
        "TIMEOUT": 8,
        "OPERATIONAL_FAILURE": 9,
        "INTERRUPTED": 130,
    }


def test_completeness_rejects_ambiguous_flags_and_gap_collections() -> None:
    """Evidence is exactly complete, partially usable, or unavailable."""
    with pytest.raises(TypeError, match="flags"):
        Completeness(is_complete=cast("bool", 1))
    with pytest.raises(TypeError, match="flags"):
        Completeness(is_complete=True, has_partial_data=cast("bool", 1))
    with pytest.raises(TypeError, match="tuple"):
        Completeness(is_complete=False, gaps=cast("tuple[EvidenceGap, ...]", [GAP]))
    with pytest.raises(TypeError, match="EvidenceGap"):
        Completeness(
            is_complete=False,
            gaps=(cast("EvidenceGap", "cloud-quotas-failed"),),
        )
    with pytest.raises(ValueError, match="partial data"):
        Completeness(is_complete=True, has_partial_data=True)

    assert Completeness.incomplete(GAP).has_partial_data
    assert not Completeness.unavailable(GAP).has_partial_data


@pytest.mark.parametrize(
    "exit_class",
    [ExitClass.INCOMPLETE_EVIDENCE, ExitClass.TIMEOUT, ExitClass.INTERRUPTED],
)
def test_partial_evidence_allows_incomplete_or_invocation_stop_exits(
    exit_class: ExitClass,
) -> None:
    """A stopped invocation may retain usable evidence without becoming exit 6."""
    result = OperationResult(
        operation=OperationName("quota.list"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("logical-page-read"), reached=False),
        outcome=Outcome(StableSymbol("operation-stopped"), exit_class),
        completeness=Completeness.incomplete(GAP),
        started_at=NOW,
        finished_at=NOW,
        data=None,
    )

    assert result.completeness.has_partial_data


def test_partial_evidence_rejects_other_non_success_exits() -> None:
    """Usable incomplete evidence cannot be mislabeled as an unrelated failure."""
    with pytest.raises(ValueError, match="partial evidence"):
        OperationResult(
            operation=OperationName("quota.list"),
            resource_scope=SCOPE,
            boundary=OperationBoundary(
                StableSymbol("logical-page-read"),
                reached=False,
            ),
            outcome=Outcome(
                StableSymbol("provider-failed"),
                ExitClass.OPERATIONAL_FAILURE,
            ),
            completeness=Completeness.incomplete(GAP),
            started_at=NOW,
            finished_at=NOW,
            data=None,
        )


def test_provenance_requires_typed_utc_safe_source_evidence() -> None:
    """Source intervals and provider text remain typed, ordered, and UTC."""
    with pytest.raises(TypeError, match="source and coverage"):
        Provenance(
            source=cast("StableSymbol", "cloud-quotas"),
            observed_at=NOW,
            coverage=StableSymbol("complete"),
        )
    with pytest.raises(TypeError, match="source and coverage"):
        Provenance(
            source=StableSymbol("cloud-quotas"),
            observed_at=NOW,
            coverage=cast("StableSymbol", "complete"),
        )
    with pytest.raises(TypeError, match="datetime"):
        Provenance(
            source=StableSymbol("cloud-quotas"),
            observed_at=cast("datetime", "2026-07-21T00:00:00Z"),
            coverage=StableSymbol("complete"),
        )
    with pytest.raises(ValueError, match="aware UTC"):
        Provenance(
            source=StableSymbol("cloud-quotas"),
            observed_at=NOW.astimezone(timezone(timedelta(hours=1))),
            coverage=StableSymbol("complete"),
        )
    with pytest.raises(TypeError, match="interval_started_at"):
        Provenance(
            source=StableSymbol("cloud-quotas"),
            observed_at=NOW,
            coverage=StableSymbol("complete"),
            interval_started_at=cast("datetime", "not-a-timestamp"),
        )
    with pytest.raises(TypeError, match="RedactedText"):
        Provenance(
            source=StableSymbol("cloud-quotas"),
            observed_at=NOW,
            coverage=StableSymbol("complete"),
            lifecycle_or_preview_status=cast("RedactedText", "settled"),
        )
    with pytest.raises(TypeError, match="RedactedText"):
        Provenance(
            source=StableSymbol("cloud-quotas"),
            observed_at=NOW,
            coverage=StableSymbol("complete"),
            request_identity=cast("RedactedText", "quotaPreferences/gpu-global"),
        )


def test_operation_results_reject_untyped_record_fields() -> None:
    """The V1 result record fails closed before interpreting its payload."""
    valid = _result()
    common = {
        "resource_scope": valid.resource_scope,
        "boundary": valid.boundary,
        "outcome": valid.outcome,
        "completeness": valid.completeness,
        "started_at": valid.started_at,
        "finished_at": valid.finished_at,
        "data": None,
    }

    with pytest.raises(TypeError, match="operation must"):
        OperationResult(operation=cast("OperationName", "request.watch"), **common)
    with pytest.raises(TypeError, match="resource_scope"):
        OperationResult(
            operation=valid.operation,
            resource_scope=cast("ResourceScope", "projects/123456789"),
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
        )
    with pytest.raises(TypeError, match="boundary must"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=cast("OperationBoundary", "fulfilled"),
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
        )
    with pytest.raises(TypeError, match="outcome must"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=cast("Outcome", "fulfilled"),
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
        )
    with pytest.raises(TypeError, match="completeness must"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=cast("Completeness", "complete"),
            started_at=NOW,
            finished_at=NOW,
            data=None,
        )


def test_operation_results_require_typed_collections_and_times() -> None:
    """Ordered diagnostics, provenance, and observation times stay structured."""
    valid = _result()
    with pytest.raises(TypeError, match="diagnostics"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
            diagnostics=cast("tuple[Diagnostic, ...]", [_diagnostic()]),
        )
    with pytest.raises(TypeError, match="diagnostics"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
            diagnostics=(cast("Diagnostic", "unsafe diagnostic"),),
        )
    with pytest.raises(TypeError, match="provenance"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
            provenance=cast("tuple[Provenance, ...]", [valid.provenance[0]]),
        )
    with pytest.raises(TypeError, match="provenance"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
            provenance=(cast("Provenance", "unsafe provenance"),),
        )
    with pytest.raises(TypeError, match="started_at"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=cast("datetime", "2026-07-21T00:00:00Z"),
            finished_at=NOW,
            data=None,
        )
    with pytest.raises(TypeError, match="identity_evidence"):
        OperationResult(
            operation=valid.operation,
            resource_scope=SCOPE,
            boundary=valid.boundary,
            outcome=valid.outcome,
            completeness=valid.completeness,
            started_at=NOW,
            finished_at=NOW,
            data=None,
            identity_evidence="unsafe identity evidence",  # type: ignore[arg-type]
        )

    assert valid.schema == OPERATION_RESULT_SCHEMA


def test_provider_preference_identity_requires_an_exact_slice() -> None:
    """Provider resources cannot stand in for canonical effective slices."""
    with pytest.raises(ValueError, match="canonical_name"):
        ProviderPreferenceIdentity(cast("str", 7), SLICE)
    with pytest.raises(ValueError, match="canonical_name"):
        ProviderPreferenceIdentity("", SLICE)
    with pytest.raises(TypeError, match="slice_identity"):
        ProviderPreferenceIdentity(
            "projects/123456789/locations/global/quotaPreferences/gpu-global",
            cast("EffectiveQuotaSliceIdentity", "compute.googleapis.com/gpus"),
        )


def test_watch_request_identity_binds_all_fields_to_one_scope() -> None:
    """Watch identity is complete, typed, and tied to its exact preference."""
    with pytest.raises(TypeError, match="resource_scope"):
        WatchRequestIdentity(
            cast("ResourceScope", "projects/123456789"),
            WatchCondition.FULFILLED,
            "intent",
            _quantity(8),
            _preference(),
        )
    with pytest.raises(TypeError, match="condition"):
        WatchRequestIdentity(
            SCOPE,
            cast("WatchCondition", "fulfilled"),
            "intent",
            _quantity(8),
            _preference(),
        )
    with pytest.raises(ValueError, match="intent_id"):
        WatchRequestIdentity(
            SCOPE,
            WatchCondition.FULFILLED,
            cast("str", 7),
            _quantity(8),
            _preference(),
        )
    with pytest.raises(ValueError, match="intent_id"):
        WatchRequestIdentity(
            SCOPE,
            WatchCondition.FULFILLED,
            "",
            _quantity(8),
            _preference(),
        )
    with pytest.raises(TypeError, match="target"):
        WatchRequestIdentity(
            SCOPE,
            WatchCondition.FULFILLED,
            "intent",
            cast("QuotaQuantity", 8),
            _preference(),
        )
    with pytest.raises(TypeError, match="provider_preference"):
        WatchRequestIdentity(
            SCOPE,
            WatchCondition.FULFILLED,
            "intent",
            _quantity(8),
            cast("ProviderPreferenceIdentity", "gpu-global"),
        )
    with pytest.raises(ValueError, match="resource scope"):
        WatchRequestIdentity(
            SCOPE,
            WatchCondition.FULFILLED,
            "intent",
            _quantity(8),
            _preference(resource_scope=OTHER_SCOPE),
        )


def test_watch_event_stream_controls_fail_closed() -> None:
    """Stream order, event kind, resume token, and source time are explicit."""
    request = _request()
    status = _status()
    boolean_sequence = cast("int", bool(1))
    with pytest.raises(ValueError, match="stream_id"):
        WatchEvent(
            cast("str", 7), 0, StableSymbol("status-changed"), "r", NOW, request, status
        )
    with pytest.raises(ValueError, match="sequence"):
        WatchEvent(
            "stream",
            boolean_sequence,
            StableSymbol("status-changed"),
            "r",
            NOW,
            request,
            status,
        )
    with pytest.raises(ValueError, match="sequence"):
        WatchEvent(
            "stream",
            cast("int", "0"),
            StableSymbol("status-changed"),
            "r",
            NOW,
            request,
            status,
        )
    with pytest.raises(TypeError, match="event"):
        WatchEvent(
            "stream",
            0,
            cast("StableSymbol", "status-changed"),
            "r",
            NOW,
            request,
            status,
        )
    with pytest.raises(ValueError, match="resume"):
        WatchEvent(
            "stream",
            0,
            StableSymbol("status-changed"),
            cast("str", 7),
            NOW,
            request,
            status,
        )


def test_watch_events_require_typed_status_results_and_diagnostics() -> None:
    """Every observation preserves typed status evidence and safe diagnostics."""
    request = _request()
    status = _status()
    with pytest.raises(TypeError, match="status"):
        WatchEvent(
            "stream",
            0,
            StableSymbol("status-changed"),
            "resume",
            NOW,
            request,
            cast("QuotaRequestStatus", "settled"),
        )
    with pytest.raises(TypeError, match="result"):
        WatchEvent(
            "stream",
            0,
            StableSymbol("terminal"),
            "resume",
            NOW,
            request,
            status,
            cast("OperationResult[None]", "fulfilled"),
        )
    with pytest.raises(TypeError, match="diagnostics"):
        WatchEvent(
            "stream",
            0,
            StableSymbol("status-changed"),
            "resume",
            NOW,
            request,
            status,
            diagnostics=cast("tuple[Diagnostic, ...]", [_diagnostic()]),
        )
    with pytest.raises(TypeError, match="diagnostics"):
        WatchEvent(
            "stream",
            0,
            StableSymbol("status-changed"),
            "resume",
            NOW,
            request,
            status,
            diagnostics=(cast("Diagnostic", "unsafe diagnostic"),),
        )


def test_only_terminal_watch_events_carry_operation_results() -> None:
    """A stream checkpoint has no result and its terminal record has exactly one."""
    checkpoint = _watch_event()
    terminal = _watch_event(event=StableSymbol("terminal"), result=_result())

    assert checkpoint.result is None
    assert checkpoint.schema == WATCH_EVENT_SCHEMA
    assert terminal.result is not None
    with pytest.raises(ValueError, match="exactly the terminal"):
        _watch_event(event=StableSymbol("terminal"))
