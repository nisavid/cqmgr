"""Public application contracts for local audit read operations."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING

from cqmgr.adapters.persistence.audit import FilesystemAuditJournal
from cqmgr.application.operations.audit import AuditOperations
from cqmgr.domain.audit import (
    AuditQuery,
    AuditRecordDraft,
    AuditRecordKind,
)
from cqmgr.domain.results import ExitClass, OperationName, StableSymbol

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path


class FixedClock:
    """Return one deterministic application observation time."""

    def now(self) -> datetime:
        """Return the clock observation used by operation envelopes."""
        return datetime(2026, 7, 23, 18, tzinfo=UTC)


def run[ResultT](awaitable: Coroutine[object, object, ResultT]) -> ResultT:
    """Run one async application operation at its public seam."""
    return asyncio.run(awaitable)


def append_record(journal: FilesystemAuditJournal) -> str:
    """Append one safe retained record through the real audit adapter."""
    record = journal.append(
        AuditRecordDraft(
            kind=AuditRecordKind.PREVIEW_EVIDENCE,
            operation=OperationName("request.preview"),
            resource_scope=None,
            occurred_at=datetime(2026, 7, 23, 17, tzinfo=UTC),
            outcome=StableSymbol("plan-created"),
        )
    )
    return record.record_id


def test_public_audit_operations_are_async_first() -> None:
    """Interfaces invoke the audit application boundary as coroutines."""
    assert iscoroutinefunction(AuditOperations.list)
    assert iscoroutinefunction(AuditOperations.inspect)
    assert iscoroutinefunction(AuditOperations.verify)


def test_list_returns_a_complete_versioned_audit_result(tmp_path: Path) -> None:
    """A bounded real-journal query returns typed audit records successfully."""
    journal = FilesystemAuditJournal(tmp_path)
    record_id = append_record(journal)
    operations = AuditOperations(journal, FixedClock())

    result = run(operations.list(AuditQuery(limit=10)))

    assert result.schema == "cqmgr.operation-result/v1"
    assert result.operation == OperationName("audit.list")
    assert result.boundary.condition == StableSymbol("audit-query-read")
    assert result.boundary.reached
    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert result.completeness.is_complete
    assert result.data.query == AuditQuery(limit=10)
    assert [record.record_id for record in result.data.records] == [record_id]
    assert result.data.next_cursor is None


def test_inspect_rejects_a_missing_record_without_reclassifying_it_as_usage(
    tmp_path: Path,
) -> None:
    """A well-formed absent audit identity is a rejected precondition."""
    operations = AuditOperations(FilesystemAuditJournal(tmp_path), FixedClock())

    result = run(operations.inspect("audit-00000000000000000001"))

    assert result.operation == OperationName("audit.inspect")
    assert result.boundary.condition == StableSymbol("audit-record-read")
    assert not result.boundary.reached
    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.outcome.code == StableSymbol("audit-record-not-found")
    assert result.completeness.is_complete
    assert result.data.record is None
    assert result.data.reason == "audit-record-not-found"


def test_verify_reports_an_invalid_chain_as_requested_outcome_unmet(
    tmp_path: Path,
) -> None:
    """A real retained-chain mismatch preserves typed failure evidence."""
    journal = FilesystemAuditJournal(tmp_path)
    append_record(journal)
    segment = tmp_path / "audit-00000001.jsonl"
    segment.write_bytes(segment.read_bytes().replace(b"plan-created", b"plan-rejected"))
    operations = AuditOperations(journal, FixedClock())

    result = run(operations.verify())

    assert result.operation == OperationName("audit.verify")
    assert result.boundary.condition == StableSymbol("audit-chain-valid")
    assert not result.boundary.reached
    assert result.outcome.exit_class is ExitClass.REQUESTED_OUTCOME_UNMET
    assert result.outcome.code == StableSymbol("audit-chain-invalid")
    assert result.completeness.is_complete
    verification = result.data.verification
    assert verification is not None
    assert verification.valid is False
    assert verification.failure is not None
    assert verification.failure.code.value == "record-hash-mismatch"


def test_usage_failures_remain_typed_operation_results(tmp_path: Path) -> None:
    """Decoded invalid query input returns usage without consulting the journal."""
    operations = AuditOperations(FilesystemAuditJournal(tmp_path), FixedClock())

    result = run(operations.list_usage_failure("limit must be a positive integer"))

    assert result.operation == OperationName("audit.list")
    assert result.outcome.exit_class is ExitClass.USAGE
    assert result.outcome.code == StableSymbol("invalid-audit-query")
    assert not result.boundary.reached
    assert result.data.reason == "limit must be a positive integer"


def test_list_rejects_an_invalid_cursor_as_usage(tmp_path: Path) -> None:
    """An opaque cursor that the journal cannot decode is invalid query input."""
    operations = AuditOperations(FilesystemAuditJournal(tmp_path), FixedClock())

    result = run(operations.list(AuditQuery(cursor="not-a-valid-cursor")))

    assert result.outcome.exit_class is ExitClass.USAGE
    assert result.outcome.code == StableSymbol("invalid-audit-query")
    assert result.data.reason == "audit query is invalid"


def test_journal_failure_returns_safe_operational_guidance(tmp_path: Path) -> None:
    """Unexpected local journal failures do not expose retained contents or paths."""
    journal = FilesystemAuditJournal(tmp_path)
    operations = AuditOperations(journal, FixedClock())
    shutil.rmtree(tmp_path)
    tmp_path.write_text("not an audit directory")

    result = run(operations.list(AuditQuery()))

    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert result.outcome.code == StableSymbol("audit-journal-unavailable")
    assert not result.completeness.is_complete
    assert result.data.reason == "audit-journal-unavailable"
    guidance = result.data.guidance
    assert guidance is not None
    assert "not an audit directory" not in guidance
    assert str(tmp_path) not in guidance
