"""Pure domain contracts for local audit facts and bounded queries."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cqmgr.domain.audit import (
    MAX_AUDIT_QUERY_LIMIT,
    AuditFact,
    AuditFailureCode,
    AuditQuery,
    AuditRecord,
    AuditRecordDraft,
    AuditRecordKind,
    AuditVerification,
    AuditVerificationFailure,
)
from cqmgr.domain.diagnostics import DiagnosticCode
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import OperationName, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

_NOW = datetime(2026, 7, 21, tzinfo=UTC)
_NAIVE = _NOW.replace(tzinfo=None)


def _draft() -> AuditRecordDraft:
    return AuditRecordDraft(
        kind=AuditRecordKind.APPLY_INTENT,
        operation=OperationName("plan.apply"),
        resource_scope=ResourceScope(ResourceScopeKind.PROJECT, "projects/123"),
        occurred_at=_NOW,
        outcome=StableSymbol("submitted"),
        correlation_id=RedactedText("sha256:opaque"),
        diagnostic_codes=(DiagnosticCode("audit-safe"),),
        facts=(AuditFact(StableSymbol("preference"), RedactedText("safe")),),
    )


def _record() -> AuditRecord:
    return AuditRecord(
        record_id="audit-00000000000000000001",
        sequence=1,
        segment=1,
        draft=_draft(),
        previous_hash="sha256:" + ("0" * 64),
        record_hash="sha256:" + ("1" * 64),
    )


def test_safe_audit_draft_preserves_typed_operation_facts() -> None:
    """A complete pre-Apply intent carries only safe typed evidence."""
    draft = _draft()

    assert draft.kind is AuditRecordKind.APPLY_INTENT
    assert draft.resource_scope is not None
    assert draft.resource_scope.canonical_name == "projects/123"
    assert draft.diagnostic_codes[0].value == "audit-safe"
    assert draft.facts[0].value.value == "safe"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kind", "apply-intent", "kind"),
        ("operation", "plan.apply", "operation"),
        ("resource_scope", "projects/123", "resource scope"),
        ("outcome", "submitted", "outcome"),
        ("correlation_id", "opaque", "correlation"),
        ("diagnostic_codes", [], "diagnostic codes"),
        ("diagnostic_codes", ("audit-safe",), "diagnostic codes"),
        ("facts", [], "facts"),
        ("facts", ("safe",), "facts"),
    ],
)
def test_audit_draft_rejects_raw_or_cross_wired_values(
    field: str,
    value: object,
    message: str,
) -> None:
    """Raw provider values cannot enter a durable audit draft by accident."""
    with pytest.raises(TypeError, match=message):
        replace(_draft(), **{field: value})  # type: ignore[arg-type]


def test_audit_draft_requires_an_aware_utc_timestamp() -> None:
    """Local or naive timestamps cannot masquerade as audit observation time."""
    with pytest.raises(ValueError, match="aware UTC"):
        replace(_draft(), occurred_at=_NAIVE)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("name", "preference", "name"),
        ("value", "safe", "value"),
    ],
)
def test_audit_fact_rejects_raw_values(name: str, value: object, message: str) -> None:
    """Fact names and values stay inside their stable and scrubbed types."""
    arguments: dict[str, object] = {
        "name": StableSymbol("preference"),
        "value": RedactedText("safe"),
    }
    arguments[name] = value
    with pytest.raises(TypeError, match=message):
        AuditFact(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "exception", "message"),
    [
        ("record_id", 1, TypeError, "record identity"),
        ("record_id", "audit-1", ValueError, "record identity"),
        ("sequence", True, TypeError, "sequence"),
        ("sequence", 0, ValueError, "sequence"),
        ("segment", True, TypeError, "segment"),
        ("segment", 0, ValueError, "segment"),
        ("draft", "draft", TypeError, "draft"),
        ("previous_hash", 1, TypeError, "previous hash"),
        ("previous_hash", "sha256:invalid", ValueError, "previous hash"),
        ("record_hash", 1, TypeError, "record hash"),
        ("record_hash", "sha256:invalid", ValueError, "record hash"),
    ],
)
def test_audit_record_rejects_noncanonical_runtime_invariants(
    field: str,
    value: object,
    exception: type[Exception],
    message: str,
) -> None:
    """Records cannot exist with identities or chain metadata that cannot persist."""
    with pytest.raises(exception, match=message):
        replace(_record(), **{field: value})  # type: ignore[arg-type]


def test_audit_query_accepts_closed_utc_range_and_bounded_limit() -> None:
    """The bounded query carries exact typed filters and observation times."""
    query = AuditQuery(
        operations=(OperationName("plan.apply"),),
        outcomes=(StableSymbol("submitted"),),
        since=_NOW,
        until=_NOW + timedelta(seconds=1),
        limit=MAX_AUDIT_QUERY_LIMIT,
        cursor="opaque",
    )

    assert query.limit == MAX_AUDIT_QUERY_LIMIT
    assert query.cursor == "opaque"


@pytest.mark.parametrize(
    ("field", "value", "exception", "message"),
    [
        ("operations", [], TypeError, "operations"),
        ("operations", ("plan.apply",), TypeError, "operations"),
        ("outcomes", [], TypeError, "outcomes"),
        ("outcomes", ("submitted",), TypeError, "outcomes"),
        ("since", _NAIVE, ValueError, "aware UTC"),
        ("until", _NAIVE, ValueError, "aware UTC"),
        ("limit", True, TypeError, "integer"),
        ("limit", 0, ValueError, "1 through 1000"),
        ("limit", 1001, ValueError, "1 through 1000"),
        ("cursor", 1, TypeError, "cursor"),
    ],
)
def test_audit_query_rejects_unbounded_or_cross_wired_values(
    field: str,
    value: object,
    exception: type[Exception],
    message: str,
) -> None:
    """Audit query inputs fail closed instead of guessing or widening scope."""
    with pytest.raises(exception, match=message):
        replace(AuditQuery(), **{field: value})  # type: ignore[arg-type]


def test_audit_query_rejects_reverse_time_range() -> None:
    """The end of a bounded query cannot precede its beginning."""
    with pytest.raises(ValueError, match="cannot precede"):
        AuditQuery(since=_NOW, until=_NOW - timedelta(seconds=1))


def test_verification_requires_exactly_one_validity_shape() -> None:
    """A valid range has no failure and an invalid range has one exact failure."""
    failure = AuditVerificationFailure(
        AuditFailureCode.RECORD_HASH_MISMATCH,
        segment=1,
        sequence=2,
        record_id="audit-2",
    )

    assert (
        AuditVerification(
            valid=False,
            verified_from=None,
            verified_through=None,
            failure=failure,
        ).failure
        is failure
    )
    with pytest.raises(ValueError, match="must have no failure"):
        AuditVerification(
            valid=True, verified_from=None, verified_through=None, failure=failure
        )
    with pytest.raises(ValueError, match="must have no failure"):
        AuditVerification(valid=False, verified_from=None, verified_through=None)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("code", "record-hash-mismatch", "code"),
        ("segment", True, "segment"),
        ("segment", -1, "segment"),
        ("sequence", True, "sequence"),
        ("sequence", 0, "sequence"),
        ("record_id", 1, "record identity"),
    ],
)
def test_verification_failure_rejects_cross_wired_location_values(
    field: str,
    value: object,
    message: str,
) -> None:
    """Integrity failures retain typed codes and exact non-negative locations."""
    failure = AuditVerificationFailure(
        AuditFailureCode.RECORD_HASH_MISMATCH,
        segment=1,
        sequence=2,
        record_id="audit-00000000000000000002",
    )

    with pytest.raises((TypeError, ValueError), match=message):
        replace(failure, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("valid", 1, "valid"),
        ("verified_from", 1, "verified from"),
        ("verified_through", 1, "verified through"),
        ("failure", "failure", "failure"),
    ],
)
def test_verification_rejects_cross_wired_range_values(
    field: str,
    value: object,
    message: str,
) -> None:
    """Verification ranges retain their closed runtime result shape."""
    with pytest.raises(TypeError, match=message):
        replace(
            AuditVerification(valid=True, verified_from=None, verified_through=None),
            **{field: value},  # type: ignore[arg-type]
        )
