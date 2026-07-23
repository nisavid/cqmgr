"""CLI parsing and presentation contracts for local audit operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cqmgr.adapters.cli.audit import (
    AuditPresentation,
    emit_audit_result,
    parse_audit_query,
)
from cqmgr.adapters.persistence.audit import FilesystemAuditJournal
from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.application.operations.audit import (
    AuditInspectData,
    AuditListData,
    AuditVerifyData,
)
from cqmgr.domain.audit import (
    AuditFact,
    AuditFactName,
    AuditFailureCode,
    AuditQuery,
    AuditRecordDraft,
    AuditRecordKind,
    AuditVerification,
    AuditVerificationFailure,
)
from cqmgr.domain.redaction import RedactedText
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

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 7, 23, 18, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
_LIMIT = 20


def test_parse_audit_query_normalizes_repeated_symbols_and_rfc3339_times() -> None:
    """CLI primitives become the bounded typed audit-query contract."""
    query = parse_audit_query(
        operations=("request.preview", "plan.apply"),
        outcomes=("plan-created", "submitted"),
        since="2026-07-23T14:00:00-04:00",
        until="2026-07-23T19:00:00Z",
        limit=str(_LIMIT),
        cursor="opaque-cursor",
    )

    assert query.operations == (
        OperationName("request.preview"),
        OperationName("plan.apply"),
    )
    assert query.outcomes == (StableSymbol("plan-created"), StableSymbol("submitted"))
    assert query.since == datetime(2026, 7, 23, 18, tzinfo=UTC)
    assert query.until == datetime(2026, 7, 23, 19, tzinfo=UTC)
    assert query.limit == _LIMIT
    assert query.cursor == "opaque-cursor"


@pytest.mark.parametrize("value", ["2026-07-23 18:00:00Z", "not-a-timestamp"])
def test_parse_audit_query_rejects_non_rfc3339_timestamps(value: str) -> None:
    """Audit time filters never fall back to an ambiguous local timestamp."""
    with pytest.raises(ValueError, match="RFC 3339"):
        parse_audit_query(since=value)


def test_json_audit_presentation_uses_the_canonical_result_mapping(
    capsys: object,
) -> None:
    """JSON preserves the exact shared envelope without separate diagnostics."""
    result = OperationResult(
        operation=OperationName("audit.list"),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol("audit-query-read"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=AuditListData(AuditQuery(limit=10), (), None),
    )

    exit_class = emit_audit_result(
        result,
        AuditPresentation(output="json", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == 0
    assert captured.err == ""
    assert json.loads(captured.out) == operation_result_mapping(result)


def test_human_audit_list_preserves_safe_record_identity_and_chain_facts(
    tmp_path: Path,
    capsys: object,
) -> None:
    """Human success output preserves typed audit facts without raw local paths."""
    machine_path = str(tmp_path / "private" / "audit.json")
    journal = FilesystemAuditJournal(tmp_path)
    record = journal.append(
        AuditRecordDraft(
            kind=AuditRecordKind.PREVIEW_EVIDENCE,
            operation=OperationName("request.preview"),
            resource_scope=SCOPE,
            occurred_at=NOW,
            outcome=StableSymbol("plan-created"),
            facts=(
                AuditFact(
                    AuditFactName.SOURCE,
                    RedactedText(
                        f"manifest={machine_path}", machine_paths=(machine_path,)
                    ),
                ),
            ),
        ),
        machine_paths=(machine_path,),
    )
    result = OperationResult(
        operation=OperationName("audit.list"),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol("audit-query-read"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=AuditListData(AuditQuery(limit=10), (record,), None),
    )

    exit_class = emit_audit_result(
        result,
        AuditPresentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == 0
    assert captured.err == ""
    assert f"Record ID: {record.record_id}" in captured.out
    assert "Record operation: request.preview" in captured.out
    assert "Record resource scope: projects/123" in captured.out
    assert f"Record hash: {record.record_hash}" in captured.out
    assert "Fact source: manifest=[REDACTED]" in captured.out
    assert machine_path not in captured.out
    assert "\x1b" not in captured.out


def test_nonzero_human_audit_result_uses_stderr_and_preserves_failure_facts(
    capsys: object,
) -> None:
    """A missing audit record is a typed error result, not stdout prose."""
    result = OperationResult(
        operation=OperationName("audit.inspect"),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol("audit-record-read"), reached=False),
        outcome=Outcome(
            StableSymbol("audit-record-not-found"),
            ExitClass.REJECTED_PRECONDITION,
        ),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=AuditInspectData(
            "audit-00000000000000000001",
            None,
            reason="audit-record-not-found",
        ),
    )

    exit_class = emit_audit_result(
        result,
        AuditPresentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == ExitClass.REJECTED_PRECONDITION
    assert captured.out == ""
    assert "Operation: audit.inspect" in captured.err
    assert "Outcome: audit-record-not-found (exit 3)" in captured.err
    assert "Boundary: audit-record-read (not reached)" in captured.err
    assert "Complete: true" in captured.err
    assert "Record ID: audit-00000000000000000001" in captured.err
    assert "Reason: audit-record-not-found" in captured.err


def test_human_audit_verification_preserves_the_exact_chain_failure(
    capsys: object,
) -> None:
    """Verification output keeps the typed first failure and its chain location."""
    result = OperationResult(
        operation=OperationName("audit.verify"),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol("audit-chain-valid"), reached=False),
        outcome=Outcome(
            StableSymbol("audit-chain-invalid"),
            ExitClass.REQUESTED_OUTCOME_UNMET,
        ),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=AuditVerifyData(
            None,
            None,
            AuditVerification(
                valid=False,
                verified_from=None,
                verified_through=None,
                failure=AuditVerificationFailure(
                    AuditFailureCode.RECORD_HASH_MISMATCH,
                    segment=2,
                    sequence=5,
                    record_id="audit-00000000000000000005",
                ),
            ),
        ),
    )

    exit_class = emit_audit_result(
        result,
        AuditPresentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == ExitClass.REQUESTED_OUTCOME_UNMET
    assert captured.out == ""
    assert "Chain valid: false" in captured.err
    assert "Failure code: record-hash-mismatch" in captured.err
    assert "Failure segment: 2" in captured.err
    assert "Failure sequence: 5" in captured.err
    assert "Failure record ID: audit-00000000000000000005" in captured.err
