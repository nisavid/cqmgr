"""Aggregate Watch and resume-lineage boundaries reject cross-wired evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    UnknownDispatchResolution,
)
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
)
from cqmgr.domain.watch import (
    WatchAggregate,
    WatchCheckpoint,
    WatchChildIdentity,
    WatchChildLineage,
    WatchChildSummary,
    WatchEventKind,
    WatchResultData,
    WatchResumeClaims,
    WatchStreamEvent,
    WatchSubject,
)

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
OTHER_SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/987654321")
UNIT = QuotaUnit("1")


def _child(
    child_id: str = "direct",
    order: int = 0,
    *,
    disposition: ApplyChildDisposition = ApplyChildDisposition.ACCEPTED,
    resolution: UnknownDispatchResolution | None = None,
) -> WatchChildIdentity:
    return WatchChildIdentity(
        child_id=child_id,
        order=order,
        slice_identity=EffectiveQuotaSliceIdentity(
            resource_scope=SCOPE,
            service="compute.googleapis.com",
            quota_id=f"quota-{child_id}",
            dimensions=NormalizedDimensions((("region", "us-central1"),)),
            quota_scope=QuotaScope.REGIONAL,
        ),
        target=QuotaQuantity(8, UNIT),
        baseline=QuotaQuantity(4, UNIT),
        disposition=disposition,
        preference_identity=(
            f"{SCOPE.canonical_name}/locations/global/quotaPreferences/{child_id}"
        ),
        lineage_etag=f"etag-{child_id}",
        lineage_trace_id=None,
        unknown_resolution=resolution,
        resolution_checkpoint=1 if resolution is not None else 0,
    )


def _status(
    child: WatchChildIdentity,
    *,
    desired: QuotaQuantity | None = None,
    baseline: QuotaQuantity | None = None,
) -> QuotaRequestStatus:
    return QuotaRequestStatus.derive(
        reconciliation=Reconciliation.RECONCILING,
        baseline=child.baseline if baseline is None else baseline,
        desired=child.target if desired is None else desired,
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )


def _subject(
    children: tuple[WatchChildIdentity, ...] | None = None,
    *,
    kind: PlanKind = PlanKind.SINGLE,
) -> WatchSubject:
    return WatchSubject(
        kind=kind,
        resource_scope=SCOPE,
        condition=WatchCondition.FULFILLED,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=(_child(),) if children is None else children,
    )


def _aggregate(subject: WatchSubject) -> WatchAggregate:
    return WatchAggregate.derive(
        subject,
        tuple(
            WatchChildSummary(
                child,
                _status(child) if child.watchable else None,
            )
            for child in subject.children
        ),
    )


def _checkpoint() -> WatchCheckpoint:
    subject = _subject()
    return WatchCheckpoint(
        checkpoint_id="checkpoint-123",
        installation_id="installation-123",
        subject=subject,
        aggregate=_aggregate(subject),
        lineages=(WatchChildLineage("direct", "etag-direct", None),),
        sequence=3,
        saved_at=NOW,
    )


def _terminal_result(
    subject: WatchSubject,
    aggregate: WatchAggregate,
    resume: str,
) -> OperationResult[WatchResultData]:
    return OperationResult(
        operation=OperationName("request.watch"),
        resource_scope=subject.resource_scope,
        boundary=OperationBoundary(StableSymbol(subject.condition.value), reached=True),
        outcome=Outcome(StableSymbol("watch-complete"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=WatchResultData(
            subject=subject,
            aggregate=aggregate,
            resume=resume,
            deadline=NOW,
            elapsed_seconds=0.0,
            last_material_observed_at=NOW,
        ),
    )


@pytest.mark.parametrize(
    ("field_name", "value", "error", "match"),
    [
        ("child_id", "", ValueError, "child_id"),
        ("order", True, ValueError, "order"),
        ("slice_identity", None, TypeError, "slice_identity"),
        ("target", None, TypeError, "target"),
        (
            "baseline",
            QuotaQuantity(4, QuotaUnit("count")),
            ValueError,
            "baseline",
        ),
        ("disposition", "accepted", TypeError, "disposition"),
        ("preference_identity", "", ValueError, "preference_identity"),
        ("lineage_etag", "", ValueError, "lineage_etag"),
        (
            "unknown_resolution",
            UnknownDispatchResolution.ACCEPTED,
            ValueError,
            "unknown resolution",
        ),
        ("resolution_checkpoint", -1, ValueError, "resolution checkpoint"),
        ("resolution_checkpoint", 1, ValueError, "resolution checkpoint"),
    ],
)
def test_watch_child_rejects_unbound_apply_evidence(
    field_name: str,
    value: object,
    error: type[Exception],
    match: str,
) -> None:
    """Every child remains bound to exact typed Apply evidence."""
    with pytest.raises(error, match=match):
        replace(
            _child(),
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_unknown_watch_child_rejects_untyped_resolution() -> None:
    """Unknown resolution evidence uses the closed durable disposition."""
    child = _child(disposition=ApplyChildDisposition.UNKNOWN)

    with pytest.raises(ValueError, match="unknown resolution"):
        replace(
            child,
            unknown_resolution=cast("Any", "accepted"),
            resolution_checkpoint=1,
        )


def test_accepted_watch_child_requires_provider_lineage() -> None:
    """A watchable child always carries an etag or stable trace."""
    with pytest.raises(ValueError, match="provider lineage"):
        replace(_child(), lineage_etag=None, lineage_trace_id=None)


@pytest.mark.parametrize(
    ("field_name", "value", "error", "match"),
    [
        ("kind", "single", TypeError, "PlanKind"),
        ("resource_scope", None, TypeError, "resource_scope"),
        ("condition", "fulfilled", TypeError, "Watch condition"),
        ("intent_id", "", ValueError, "intent_id"),
        ("plan_digest", "", ValueError, "plan_digest"),
        ("children", [], ValueError, "ordered children"),
        ("resolution_checkpoint", -1, ValueError, "resolution checkpoint"),
    ],
)
def test_watch_subject_rejects_untyped_or_incomplete_identity(
    field_name: str,
    value: object,
    error: type[Exception],
    match: str,
) -> None:
    """A subject fully identifies one typed intent and accepted Watch set."""
    with pytest.raises(error, match=match):
        replace(
            _subject(),
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_watch_subject_rejects_duplicate_children() -> None:
    """Ordered bundle children have unique durable identities."""
    duplicate = replace(_child(), order=1)

    with pytest.raises(ValueError, match="identities must be unique"):
        _subject((_child(), duplicate), kind=PlanKind.BUNDLE)


def test_watch_subject_rejects_child_from_another_resource_scope() -> None:
    """Every watched child belongs to the subject resource scope."""
    child = _child()
    foreign = replace(
        child,
        slice_identity=replace(child.slice_identity, resource_scope=OTHER_SCOPE),
    )

    with pytest.raises(ValueError, match="resource scope"):
        _subject((foreign,))


@pytest.mark.parametrize(
    ("child", "status", "error", "match"),
    [
        (cast("Any", None), None, TypeError, "summary child"),
        (_child(), cast("Any", "pending"), TypeError, "summary status"),
        (
            _child(disposition=ApplyChildDisposition.FAILED),
            _status(_child(disposition=ApplyChildDisposition.FAILED)),
            ValueError,
            "non-watchable",
        ),
        (
            _child(),
            _status(_child(), desired=QuotaQuantity(9, UNIT)),
            ValueError,
            "bound target",
        ),
    ],
)
def test_watch_summary_rejects_cross_wired_status(
    child: WatchChildIdentity,
    status: QuotaRequestStatus | None,
    error: type[Exception],
    match: str,
) -> None:
    """Observed status remains bound to one watchable child and target."""
    with pytest.raises(error, match=match):
        WatchChildSummary(child, status)


def test_watch_aggregate_rejects_summaries_from_another_subject() -> None:
    """Aggregate summaries preserve the complete subject child sequence."""
    subject = _subject()
    foreign = _child("foreign")

    with pytest.raises(ValueError, match="complete subject"):
        WatchAggregate.derive(subject, (WatchChildSummary(foreign, _status(foreign)),))


@pytest.mark.parametrize(
    ("field_name", "value", "match"),
    [
        ("child_id", "", "child_id"),
        ("etag", "", "etag"),
        ("trace_id", "", "trace_id"),
    ],
)
def test_watch_lineage_rejects_empty_identity_values(
    field_name: str,
    value: str,
    match: str,
) -> None:
    """Present lineage fields are always non-empty."""
    with pytest.raises(ValueError, match=match):
        replace(
            WatchChildLineage("direct", "etag-direct", None),
            **{field_name: value},
        )


def test_watch_lineage_requires_etag_or_stable_trace() -> None:
    """A checkpoint cannot invent a child without provider lineage."""
    with pytest.raises(ValueError, match="etag or stable trace"):
        WatchChildLineage("direct", None, None)


@pytest.mark.parametrize("field_name", ["checkpoint_id", "installation_id"])
def test_watch_checkpoint_requires_complete_local_identity(field_name: str) -> None:
    """Durable checkpoints identify their installation and local record."""
    with pytest.raises(ValueError, match=field_name):
        replace(
            _checkpoint(),
            **{field_name: ""},  # type: ignore[bad-argument-type]
        )


def test_watch_checkpoint_requires_typed_subject_and_aggregate() -> None:
    """Checkpoint storage accepts only typed subject and aggregate evidence."""
    with pytest.raises(TypeError, match="subject and aggregate"):
        replace(_checkpoint(), subject=cast("Any", None))


def test_watch_checkpoint_rejects_aggregate_from_another_subject() -> None:
    """Checkpoint aggregate evidence remains bound to its exact subject."""
    checkpoint = _checkpoint()
    foreign = _child("foreign")
    aggregate = replace(
        checkpoint.aggregate,
        children=(WatchChildSummary(foreign, _status(foreign)),),
    )

    with pytest.raises(ValueError, match="aggregate must match"):
        replace(checkpoint, aggregate=aggregate)


@pytest.mark.parametrize(
    "lineages",
    [
        cast("Any", []),
        (cast("Any", "not-lineage"),),
        (WatchChildLineage("foreign", "etag-foreign", None),),
    ],
)
def test_watch_checkpoint_rejects_cross_wired_lineage_set(lineages: object) -> None:
    """Checkpoint lineage order exactly matches the accepted Watch set."""
    with pytest.raises(ValueError, match="lineages must match"):
        replace(
            _checkpoint(),
            lineages=lineages,  # type: ignore[bad-argument-type]
        )


def test_watch_checkpoint_rejects_negative_sequence() -> None:
    """Resume ordering never accepts a negative checkpoint sequence."""
    with pytest.raises(ValueError, match="sequence"):
        replace(_checkpoint(), sequence=-1)


@pytest.mark.parametrize(
    ("field_name", "value", "error", "match"),
    [
        ("installation_id", "", ValueError, "installation_id"),
        ("checkpoint_id", "", ValueError, "checkpoint_id"),
        ("intent_id", "", ValueError, "intent_id"),
        ("subject_digest", "", ValueError, "subject_digest"),
        ("condition", "fulfilled", TypeError, "condition"),
        ("resolution_checkpoint", -1, ValueError, "resolution_checkpoint"),
        ("sequence", -1, ValueError, "sequence"),
    ],
)
def test_watch_resume_claims_require_complete_typed_controls(
    field_name: str,
    value: object,
    error: type[Exception],
    match: str,
) -> None:
    """Opaque claims retain every typed subject and checkpoint control."""
    claims = WatchResumeClaims(
        installation_id="installation-123",
        checkpoint_id="checkpoint-123",
        intent_id="sha256:" + ("a" * 64),
        subject_digest="sha256:" + ("b" * 64),
        condition=WatchCondition.FULFILLED,
        resolution_checkpoint=1,
        sequence=3,
    )

    with pytest.raises(error, match=match):
        replace(
            claims,
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def _initial_event() -> WatchStreamEvent:
    subject = _subject()
    return WatchStreamEvent(
        stream_id="stream-123",
        sequence=0,
        event=WatchEventKind.INITIAL,
        resume="cqmgr.watch-resume/v1:opaque",
        observed_at=NOW,
        subject=subject,
        aggregate=_aggregate(subject),
    )


@pytest.mark.parametrize(
    ("field_name", "value", "error", "match"),
    [
        ("stream_id", "", ValueError, "stream_id"),
        ("sequence", -1, ValueError, "sequence"),
        ("event", "initial", TypeError, "Watch event"),
        ("resume", "", ValueError, "resume token"),
        ("subject", None, TypeError, "subject and aggregate"),
        ("diagnostics", [], TypeError, "diagnostics"),
    ],
)
def test_watch_event_rejects_untyped_stream_controls(
    field_name: str,
    value: object,
    error: type[Exception],
    match: str,
) -> None:
    """Every stream record carries typed ordering and subject evidence."""
    with pytest.raises(error, match=match):
        replace(
            _initial_event(),
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_watch_event_rejects_aggregate_from_another_subject() -> None:
    """An event cannot substitute another subject's aggregate."""
    event = _initial_event()
    foreign = _child("foreign")
    aggregate = replace(
        event.aggregate,
        children=(WatchChildSummary(foreign, _status(foreign)),),
    )

    with pytest.raises(ValueError, match="aggregate must match"):
        replace(event, aggregate=aggregate)


def test_material_watch_event_requires_a_subject_child_id() -> None:
    """Material child changes identify exactly one subject child."""
    with pytest.raises(ValueError, match="must name exactly one child_id"):
        replace(_initial_event(), event=WatchEventKind.CHILD_STATUS_CHANGED)


def test_material_watch_event_rejects_foreign_child_id() -> None:
    """A material event cannot name a child outside its subject."""
    with pytest.raises(ValueError, match="child_id must belong"):
        replace(
            _initial_event(),
            event=WatchEventKind.CHILD_STATUS_CHANGED,
            child_id="foreign",
        )


def test_terminal_watch_event_requires_exactly_one_result() -> None:
    """Only a terminal event carries the terminal operation result."""
    with pytest.raises(ValueError, match="terminal Watch event carries a result"):
        replace(_initial_event(), event=WatchEventKind.TERMINAL)


def test_terminal_watch_result_must_match_the_event_resume() -> None:
    """Terminal result and event retain one exact resume identity."""
    event = _initial_event()
    result = _terminal_result(event.subject, event.aggregate, "resume-for-result")

    with pytest.raises(ValueError, match="result must match"):
        replace(
            event,
            event=WatchEventKind.TERMINAL,
            resume="resume-for-event",
            result=result,
        )
