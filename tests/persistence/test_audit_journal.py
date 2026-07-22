"""Real-filesystem contracts for the append-only audit journal."""

import json
import multiprocessing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cqmgr.adapters.persistence.audit import AuditIntegrityError, FilesystemAuditJournal
from cqmgr.domain.audit import (
    AUDIT_GENESIS_HASH,
    AuditFact,
    AuditFailureCode,
    AuditQuery,
    AuditRecordDraft,
    AuditRecordKind,
)
from cqmgr.domain.redaction import REDACTION_MARKER, RedactedText
from cqmgr.domain.results import OperationName, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

_OCCURRED_AT = datetime(2026, 7, 21, tzinfo=UTC)
_SECOND_RECORD = 2


class _InjectedCrashError(Exception):
    """A test-only failure immediately after a durable append."""


def _rewrite_record(path: Path, index: int, **changes: object) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    payload = json.loads(lines[index])
    payload.update(changes)
    lines[index] = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    path.write_bytes(b"".join(lines))


def _draft(
    *,
    operation: str = "request.preview",
    outcome: str = "plan-created",
    correlation_id: str | None = None,
    facts: tuple[AuditFact, ...] = (),
) -> AuditRecordDraft:
    return AuditRecordDraft(
        kind=AuditRecordKind.PREVIEW_EVIDENCE,
        operation=OperationName(operation),
        resource_scope=ResourceScope(ResourceScopeKind.PROJECT, "projects/123"),
        occurred_at=_OCCURRED_AT,
        outcome=StableSymbol(outcome),
        correlation_id=RedactedText(correlation_id) if correlation_id else None,
        facts=facts,
    )


def _append_in_process(root: str, number: int) -> None:
    journal = FilesystemAuditJournal(root)
    journal.append(_draft(correlation_id=f"worker-{number}"))


def test_append_query_and_verify_one_canonical_record(tmp_path: Path) -> None:
    """A durable append is queryable and starts one valid hash chain."""
    journal = FilesystemAuditJournal(tmp_path)
    draft = _draft()

    appended = journal.append(draft)
    page = journal.query(AuditQuery(limit=10))
    verification = journal.verify()

    assert page.records == (appended,)
    assert page.next_cursor is None
    assert appended.sequence == 1
    assert appended.previous_hash == "sha256:" + ("0" * 64)
    assert appended.record_hash.startswith("sha256:")
    assert verification.valid
    assert verification.verified_from == appended.record_id
    assert verification.verified_through == appended.record_id
    assert journal.inspect(appended.record_id) == appended
    assert journal.inspect("audit-missing") is None


def test_empty_journal_verifies_as_an_empty_retained_range(tmp_path: Path) -> None:
    """No retained records is a valid complete range with no invented identity."""
    result = FilesystemAuditJournal(tmp_path).verify()

    assert result.valid
    assert result.verified_from is None
    assert result.verified_through is None


def test_segment_size_must_leave_room_for_a_rotation_checkpoint(tmp_path: Path) -> None:
    """A segment policy cannot rotate on every operation record."""
    with pytest.raises(ValueError, match="at least two"):
        FilesystemAuditJournal(tmp_path, max_records_per_segment=1)


def test_rotation_checkpoint_continues_the_hash_chain(tmp_path: Path) -> None:
    """Rotation adds an explicit checkpoint before the next operation record."""
    journal = FilesystemAuditJournal(tmp_path, max_records_per_segment=2)
    first = journal.append(_draft(correlation_id="one"))
    second = journal.append(_draft(correlation_id="two"))
    third = journal.append(_draft(correlation_id="three"))

    records = journal.query(AuditQuery(limit=10)).records

    assert [record.draft.kind for record in records] == [
        AuditRecordKind.PREVIEW_EVIDENCE,
        AuditRecordKind.PREVIEW_EVIDENCE,
        AuditRecordKind.ROTATION_CHECKPOINT,
        AuditRecordKind.PREVIEW_EVIDENCE,
    ]
    assert records[_SECOND_RECORD].segment == _SECOND_RECORD
    assert records[2].previous_hash == second.record_hash
    assert third.previous_hash == records[2].record_hash
    assert first.segment == 1
    assert journal.verify().valid


def test_query_cursor_is_bounded_and_bound_to_filters(tmp_path: Path) -> None:
    """Continuation cannot silently resume a different audit query."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft(operation="request.preview", outcome="plan-created"))
    journal.append(_draft(operation="plan.apply", outcome="submitted"))
    journal.append(_draft(operation="request.preview", outcome="plan-created"))
    query = AuditQuery(operations=(OperationName("request.preview"),), limit=1)

    first_page = journal.query(query)
    second_page = journal.query(replace(query, cursor=first_page.next_cursor))

    assert len(first_page.records) == 1
    assert len(second_page.records) == 1
    assert second_page.next_cursor is None
    with pytest.raises(ValueError, match="does not match"):
        journal.query(
            AuditQuery(
                operations=(OperationName("plan.apply"),),
                limit=1,
                cursor=first_page.next_cursor,
            )
        )

    tampered = (first_page.next_cursor or "")[:-2] + "AA"
    with pytest.raises(ValueError, match="invalid"):
        journal.query(replace(query, cursor=tampered))


def test_query_filters_outcomes_and_closed_time_range(tmp_path: Path) -> None:
    """Outcome and observation-time filters combine with AND semantics."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft(outcome="plan-created"))
    journal.append(
        replace(
            _draft(outcome="submitted"),
            occurred_at=_OCCURRED_AT.replace(hour=1),
        )
    )

    page = journal.query(
        AuditQuery(
            outcomes=(StableSymbol("submitted"),),
            since=_OCCURRED_AT.replace(minute=30),
            until=_OCCURRED_AT.replace(hour=2),
            limit=10,
        )
    )

    assert len(page.records) == 1
    assert page.records[0].draft.outcome == StableSymbol("submitted")


def test_append_scrubs_explicit_secrets_and_machine_paths(tmp_path: Path) -> None:
    """Neither the canonical record nor persisted bytes retain excluded text."""
    quota_contact = "person@example.test"
    machine_path = "/Users/example/private/adc.json"
    journal = FilesystemAuditJournal(tmp_path)

    record = journal.append(
        _draft(
            correlation_id=f"contact={quota_contact}",
            facts=(
                AuditFact(
                    StableSymbol("source"),
                    RedactedText(f"credential-path={machine_path}"),
                ),
            ),
        ),
        sensitive_values=(quota_contact,),
        machine_paths=(machine_path,),
    )
    persisted = b"".join(path.read_bytes() for path in tmp_path.glob("*.jsonl"))

    assert quota_contact.encode() not in persisted
    assert machine_path.encode() not in persisted
    assert record.draft.correlation_id is not None
    assert REDACTION_MARKER in record.draft.correlation_id.value
    assert REDACTION_MARKER in record.draft.facts[0].value.value


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("tamper", AuditFailureCode.RECORD_HASH_MISMATCH),
        ("truncate", AuditFailureCode.MALFORMED_RECORD),
        ("reorder", AuditFailureCode.SEQUENCE_GAP),
    ],
)
def test_verification_reports_the_exact_first_failure(
    tmp_path: Path,
    mutation: str,
    expected: AuditFailureCode,
) -> None:
    """Tampering, truncation, and reordering remain distinguishable."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft(correlation_id="first"))
    journal.append(_draft(correlation_id="second"))
    segment = tmp_path / "audit-00000001.jsonl"
    lines = segment.read_bytes().splitlines(keepends=True)
    if mutation == "tamper":
        _rewrite_record(segment, 0, outcome="different")
        lines = segment.read_bytes().splitlines(keepends=True)
    elif mutation == "truncate":
        lines[-1] = lines[-1][:-4]
    else:
        lines.reverse()
    segment.write_bytes(b"".join(lines))

    result = journal.verify()

    assert not result.valid
    assert result.failure is not None
    assert result.failure.code is expected
    assert result.failure.sequence == (1 if mutation == "tamper" else 2)


def test_verification_distinguishes_previous_hash_tampering(tmp_path: Path) -> None:
    """A changed chain link fails before the record's content hash is considered."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft(correlation_id="first"))
    journal.append(_draft(correlation_id="second"))
    segment = tmp_path / "audit-00000001.jsonl"
    _rewrite_record(segment, 1, previous_hash=AUDIT_GENESIS_HASH)

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.PREVIOUS_HASH_MISMATCH
    assert result.failure.sequence == _SECOND_RECORD


def test_verification_reports_unsupported_record_schema(tmp_path: Path) -> None:
    """A newer audit record schema is rejected without guessing its meaning."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft())
    _rewrite_record(
        tmp_path / "audit-00000001.jsonl",
        0,
        schema="cqmgr.audit-record/v2",
    )

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.UNSUPPORTED_SCHEMA


def test_verification_reports_malformed_complete_line(tmp_path: Path) -> None:
    """A newline-terminated non-record is still an exact malformed-record failure."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft())
    (tmp_path / "audit-00000001.jsonl").write_bytes(b"not-json\n")

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.MALFORMED_RECORD


def test_verification_reports_missing_first_segment(tmp_path: Path) -> None:
    """A retained chain cannot begin at a later rotation segment."""
    journal = FilesystemAuditJournal(tmp_path, max_records_per_segment=2)
    journal.append(_draft(correlation_id="one"))
    journal.append(_draft(correlation_id="two"))
    journal.append(_draft(correlation_id="three"))
    (tmp_path / "audit-00000001.jsonl").unlink()

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.MISSING_SEGMENT
    assert result.failure.segment == 1


def test_verification_detects_deleted_segment(tmp_path: Path) -> None:
    """Deleting retained records cannot turn a damaged chain into a valid prefix."""
    journal = FilesystemAuditJournal(tmp_path, max_records_per_segment=2)
    journal.append(_draft(correlation_id="one"))
    journal.append(_draft(correlation_id="two"))
    journal.append(_draft(correlation_id="three"))
    (tmp_path / "audit-00000002.jsonl").unlink()

    result = journal.verify()

    assert not result.valid
    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.MANIFEST_MISMATCH
    with pytest.raises(AuditIntegrityError, match="manifest-mismatch"):
        journal.append(_draft())


def test_verification_requires_manifest_for_retained_records(tmp_path: Path) -> None:
    """Report deleted durable head metadata instead of reconstructing it on read."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft())
    (tmp_path / "manifest.json").unlink()

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.MANIFEST_MISMATCH
    with pytest.raises(AuditIntegrityError, match="manifest-mismatch"):
        journal.append(_draft())


def test_verification_rejects_malformed_manifest(tmp_path: Path) -> None:
    """Unreadable durable head metadata is an exact integrity failure."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft())
    (tmp_path / "manifest.json").write_bytes(b"not-json")

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.MANIFEST_MISMATCH


def test_append_rejects_manifest_that_claims_deleted_records(tmp_path: Path) -> None:
    """A writer cannot silently restart sequence numbers after retained deletion."""
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "cqmgr.audit-manifest/v1",
                "last_sequence": 1,
                "last_segment": 1,
                "last_hash": "sha256:" + ("1" * 64),
            }
        )
    )
    journal = FilesystemAuditJournal(tmp_path)

    with pytest.raises(AuditIntegrityError, match="manifest-mismatch"):
        journal.append(_draft())


def test_empty_manifest_is_valid_only_for_an_empty_chain(tmp_path: Path) -> None:
    """A canonical empty manifest does not invent any retained audit evidence."""
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "cqmgr.audit-manifest/v1",
                "last_sequence": 0,
                "last_segment": 0,
                "last_hash": AUDIT_GENESIS_HASH,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    assert FilesystemAuditJournal(tmp_path).verify().valid


def test_verification_range_reports_missing_and_reverse_identities(
    tmp_path: Path,
) -> None:
    """Range verification identifies exact absent or reversed record bounds."""
    journal = FilesystemAuditJournal(tmp_path)
    first = journal.append(_draft(correlation_id="first"))
    second = journal.append(_draft(correlation_id="second"))

    missing = journal.verify(from_record_id="audit-missing")
    reverse = journal.verify(
        from_record_id=second.record_id,
        through_record_id=first.record_id,
    )
    selected = journal.verify(
        from_record_id=second.record_id,
        through_record_id=second.record_id,
    )

    assert missing.failure is not None
    assert missing.failure.code is AuditFailureCode.RECORD_NOT_FOUND
    assert reverse.failure is not None
    assert reverse.failure.code is AuditFailureCode.SEQUENCE_GAP
    assert selected.valid
    assert selected.verified_from == second.record_id


def test_crash_after_record_fsync_recovers_without_losing_the_record(
    tmp_path: Path,
) -> None:
    """A stale manifest is advanced only after the durable chain verifies."""
    crashed = False

    def fail_once(stage: str) -> None:
        nonlocal crashed
        if stage == "after-record-fsync" and not crashed:
            crashed = True
            raise _InjectedCrashError

    journal = FilesystemAuditJournal(tmp_path, failure_hook=fail_once)
    with pytest.raises(_InjectedCrashError):
        journal.append(_draft(correlation_id="durable-before-crash"))

    recovered = FilesystemAuditJournal(tmp_path)
    second = recovered.append(_draft(correlation_id="after-recovery"))

    assert second.sequence == _SECOND_RECORD
    assert recovered.verify().valid
    assert len(recovered.query(AuditQuery(limit=10)).records) == _SECOND_RECORD


def test_concurrent_processes_serialize_writers_without_lost_updates(
    tmp_path: Path,
) -> None:
    """Independent local processes append one total order under one lock."""
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_in_process, args=(str(tmp_path), number))
        for number in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert [process.exitcode for process in processes] == [0] * 6
    journal = FilesystemAuditJournal(tmp_path)
    records = journal.query(AuditQuery(limit=10)).records
    assert [record.sequence for record in records] == list(range(1, 7))
    assert journal.verify().valid
