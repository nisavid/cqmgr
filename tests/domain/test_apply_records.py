"""Durable non-atomic Apply record transitions."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    ApplyChildRecord,
    ApplyRecord,
    ApplyRecordState,
    UnknownDispatchResolution,
    UnknownResolutionEvidence,
)
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 24, 1, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
UNIT = QuotaUnit("1")


def _child(child_id: str) -> ApplyChildRecord:
    return ApplyChildRecord(
        child_id=child_id,
        slice_identity=EffectiveQuotaSliceIdentity(
            SCOPE,
            "compute.googleapis.com",
            f"quota-{child_id}",
            NormalizedDimensions((("region", "us-central1"),)),
            QuotaScope.REGIONAL,
        ),
        target=QuotaQuantity(8, UNIT),
        preference_identity=(
            f"{SCOPE.canonical_name}/locations/global/quotaPreferences/{child_id}"
        ),
        etag=None,
    )


def _record() -> ApplyRecord:
    return ApplyRecord(
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        kind=PlanKind.BUNDLE,
        resource_scope=SCOPE,
        created_at=NOW,
        children=(_child("direct"), _child("companion")),
    )


@pytest.mark.parametrize(
    ("field_name", "value", "error", "message"),
    [
        ("intent_id", "", ValueError, "intent_id"),
        ("child_id", "", ValueError, "child_id"),
        ("resolution", "accepted", TypeError, "UnknownDispatchResolution"),
        ("recorded_at", NOW.replace(tzinfo=None), ValueError, "aware UTC"),
        ("checkpoint", 2, ValueError, "checkpoint"),
    ],
)
def test_unknown_resolution_evidence_rejects_cross_wired_journal_values(
    field_name: str,
    value: object,
    error: type[Exception],
    message: str,
) -> None:
    """The append-only journal accepts only exact single-assignment evidence."""
    evidence = UnknownResolutionEvidence(
        "sha256:" + ("a" * 64),
        "direct",
        UnknownDispatchResolution.ACCEPTED,
        NOW,
    )
    with pytest.raises(error, match=message):
        replace(
            evidence,
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_ordered_apply_record_preserves_accepted_then_failed_and_unattempted() -> None:
    """A later failure never rewrites an earlier accepted child."""
    record = _record()
    record = record.record_dispatch_intent("direct", NOW)
    record = record.record_outcome(
        "direct",
        ApplyChildDisposition.ACCEPTED,
        StableSymbol("submitted"),
        NOW,
    )
    record = record.record_dispatch_intent("companion", NOW)
    record = record.record_outcome(
        "companion",
        ApplyChildDisposition.FAILED,
        StableSymbol("conflicting"),
        NOW,
    )
    record = record.finalize(NOW)

    assert record.state is ApplyRecordState.FAILED
    assert tuple(child.disposition for child in record.children) == (
        ApplyChildDisposition.ACCEPTED,
        ApplyChildDisposition.FAILED,
    )


def test_interrupted_dispatch_becomes_unknown_and_never_dispatches_again() -> None:
    """Recovery converts intent-without-outcome to unknown without redispatch."""
    record = _record().record_dispatch_intent("direct", NOW)

    recovered = record.recover_interrupted(NOW)

    assert recovered.children[0].disposition is ApplyChildDisposition.UNKNOWN
    assert recovered.children[1].disposition is ApplyChildDisposition.UNATTEMPTED
    assert recovered.state is ApplyRecordState.UNKNOWN
    with pytest.raises(ValueError, match="cannot be dispatched"):
        recovered.record_dispatch_intent("direct", NOW)


def test_unknown_resolution_is_append_only_and_single_assignment() -> None:
    """Read-after-unknown proof cannot rewrite or conflict with history."""
    unknown = _record().record_dispatch_intent("direct", NOW).recover_interrupted(NOW)

    resolved = unknown.resolve_unknown(
        "direct", UnknownDispatchResolution.ACCEPTED, NOW
    )

    assert resolved.children[0].disposition is ApplyChildDisposition.UNKNOWN
    assert resolved.children[0].unknown_resolution is UnknownDispatchResolution.ACCEPTED
    assert (
        resolved.resolve_unknown("direct", UnknownDispatchResolution.ACCEPTED, NOW)
        == resolved
    )
    with pytest.raises(ValueError, match="conflicting"):
        resolved.resolve_unknown("direct", UnknownDispatchResolution.FAILED, NOW)


@pytest.mark.parametrize(
    ("field_name", "value", "error", "message"),
    [
        ("child_id", "", ValueError, "child_id"),
        ("slice_identity", "slice", TypeError, "slice_identity"),
        ("target", "target", TypeError, "target"),
        ("preference_identity", "", ValueError, "preference_identity"),
        ("etag", "", ValueError, "etag"),
        ("dispatch_intent_at", NOW.replace(tzinfo=None), ValueError, "aware UTC"),
        ("disposition", "accepted", TypeError, "disposition"),
        ("provider_outcome", "submitted", TypeError, "provider_outcome"),
        ("unknown_resolution", "accepted", TypeError, "unknown resolution"),
        (
            "resolution_recorded_at",
            NOW,
            ValueError,
            "resolution timestamp",
        ),
    ],
)
def test_apply_child_rejects_cross_wired_state(
    field_name: str,
    value: object,
    error: type[Exception],
    message: str,
) -> None:
    """Malformed persistence values cannot create an Apply child."""
    with pytest.raises(error, match=message):
        replace(
            _child("direct"),
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_apply_child_rejects_impossible_terminal_histories() -> None:
    """Dispatch, outcome, and resolution evidence remain coherent."""
    child = _child("direct")
    with pytest.raises(ValueError, match="terminal dispatched"):
        replace(child, disposition=ApplyChildDisposition.ACCEPTED)
    with pytest.raises(ValueError, match="unattempted"):
        replace(
            child,
            disposition=ApplyChildDisposition.UNATTEMPTED,
            dispatch_intent_at=NOW,
        )
    with pytest.raises(ValueError, match="unknown resolution requires"):
        replace(
            child,
            unknown_resolution=UnknownDispatchResolution.ACCEPTED,
            resolution_recorded_at=NOW,
        )


@pytest.mark.parametrize(
    ("field_name", "value", "error", "message"),
    [
        ("intent_id", "", ValueError, "intent_id"),
        ("plan_digest", "", ValueError, "plan_digest"),
        ("kind", "bundle", TypeError, "kind"),
        ("resource_scope", "scope", TypeError, "resource_scope"),
        ("created_at", NOW.replace(tzinfo=None), ValueError, "aware UTC"),
        ("children", (), ValueError, "children"),
        ("state", "accepted", TypeError, "state"),
        ("finished_at", NOW.replace(tzinfo=None), ValueError, "aware UTC"),
        ("revision", True, TypeError, "revision"),
        ("revision", -1, ValueError, "negative"),
    ],
)
def test_apply_record_rejects_cross_wired_state(
    field_name: str,
    value: object,
    error: type[Exception],
    message: str,
) -> None:
    """Malformed aggregate persistence state fails closed."""
    with pytest.raises(error, match=message):
        replace(
            _record(),
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_apply_record_rejects_duplicate_or_foreign_children() -> None:
    """Every ordered child is unique and bound to the aggregate scope."""
    record = _record()
    with pytest.raises(ValueError, match="unique"):
        replace(record, children=(record.children[0], record.children[0]))
    other_scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/987654321")
    foreign = replace(
        record.children[0],
        slice_identity=replace(
            record.children[0].slice_identity,
            resource_scope=other_scope,
        ),
    )
    with pytest.raises(ValueError, match="resource scope"):
        replace(record, children=(foreign,))
    with pytest.raises(ValueError, match="finished_at"):
        replace(record, state=ApplyRecordState.ACCEPTED)
    with pytest.raises(ValueError, match="finished_at"):
        replace(record, finished_at=NOW)


def test_apply_transition_guards_reject_reorder_redispatch_and_early_finish() -> None:
    """Only the next child may advance, exactly once, before finalization."""
    record = _record()
    with pytest.raises(ValueError, match="prior acceptance"):
        record.record_dispatch_intent("companion", NOW)
    with pytest.raises(ValueError, match="unknown Apply child"):
        record.record_dispatch_intent("missing", NOW)
    with pytest.raises(ValueError, match="finish"):
        record.finalize(NOW)
    intended = record.record_dispatch_intent("direct", NOW)
    with pytest.raises(ValueError, match="more than once"):
        intended.record_dispatch_intent("direct", NOW)
    with pytest.raises(ValueError, match="unattempted"):
        intended.record_outcome(
            "direct",
            ApplyChildDisposition.UNATTEMPTED,
            StableSymbol("blocked"),
            NOW,
        )
    with pytest.raises(ValueError, match="unresolved dispatch"):
        record.record_outcome(
            "direct",
            ApplyChildDisposition.ACCEPTED,
            StableSymbol("submitted"),
            NOW,
        )
    with pytest.raises(ValueError, match="only an unknown"):
        intended.record_outcome(
            "direct",
            ApplyChildDisposition.ACCEPTED,
            StableSymbol("submitted"),
            NOW,
        ).resolve_unknown("direct", UnknownDispatchResolution.ACCEPTED, NOW)


def test_terminal_and_critical_transitions_are_stable() -> None:
    """Terminal finalization is idempotent and critical evidence is explicit."""
    accepted = (
        _record()
        .record_dispatch_intent("direct", NOW)
        .record_outcome(
            "direct",
            ApplyChildDisposition.ACCEPTED,
            StableSymbol("submitted"),
            NOW,
        )
        .record_dispatch_intent("companion", NOW)
        .record_outcome(
            "companion",
            ApplyChildDisposition.ACCEPTED,
            StableSymbol("submitted"),
            NOW,
        )
        .finalize(NOW)
    )
    assert accepted.finalize(NOW) is accepted
    assert accepted.mark_critical_unknown(NOW).state is (
        ApplyRecordState.CRITICAL_UNKNOWN
    )
    with pytest.raises(ValueError, match="terminal"):
        accepted.record_dispatch_intent("direct", NOW)
    with pytest.raises(ValueError, match="unknown Apply child"):
        accepted.resolve_unknown(
            cast("str", "missing"),
            UnknownDispatchResolution.ACCEPTED,
            NOW,
        )
