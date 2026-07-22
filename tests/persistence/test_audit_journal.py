"""Real-filesystem contracts for the append-only audit journal."""

import json
import multiprocessing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cqmgr.adapters.persistence.audit as audit_adapter
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


def _record_payload(path: Path, index: int = 0) -> dict[str, object]:
    return json.loads(path.read_bytes().splitlines()[index])


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


def test_verification_rejects_record_identity_tampering(tmp_path: Path) -> None:
    """A stored record identity is derived from and bound to its exact sequence."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft())
    _rewrite_record(
        tmp_path / "audit-00000001.jsonl",
        0,
        record_id="audit-00000000000000000999",
    )

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.RECORD_ID_MISMATCH
    assert result.failure.sequence == 1


@pytest.mark.parametrize(
    "variant",
    ["unknown-field", "whitespace", "key-order", "unicode-escape", "timestamp"],
)
def test_verification_rejects_noncanonical_record_bytes(
    tmp_path: Path,
    variant: str,
) -> None:
    """Semantically similar JSON is invalid unless every retained byte is canonical."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(
        _draft(facts=(AuditFact(StableSymbol("label"), RedactedText("café")),))
    )
    path = tmp_path / "audit-00000001.jsonl"
    payload = _record_payload(path)
    if variant == "unknown-field":
        payload["extra"] = "not-allowed"
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    elif variant == "whitespace":
        raw = json.dumps(payload, sort_keys=True)
    elif variant == "key-order":
        reversed_payload = {name: payload[name] for name in reversed(payload)}
        raw = json.dumps(
            reversed_payload,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        )
    elif variant == "unicode-escape":
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        payload["occurred_at"] = "2026-07-21T00:00:00+00:00"
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    path.write_text(raw + "\n")

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.NONCANONICAL_RECORD


@pytest.mark.parametrize("payload", [[], "text", 7, None])
def test_verification_returns_typed_failure_for_nonobject_json(
    tmp_path: Path,
    payload: object,
) -> None:
    """A valid JSON value that is not a record never escapes as a Python error."""
    journal = FilesystemAuditJournal(tmp_path)
    (tmp_path / "audit-00000001.jsonl").write_text(json.dumps(payload) + "\n")

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.MALFORMED_RECORD


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("kind", True, AuditFailureCode.MALFORMED_RECORD),
        ("sequence", True, AuditFailureCode.MALFORMED_RECORD),
        ("segment", True, AuditFailureCode.MALFORMED_RECORD),
        ("sequence", 0, AuditFailureCode.MALFORMED_RECORD),
        ("segment", 0, AuditFailureCode.MALFORMED_RECORD),
        ("previous_hash", "invalid", AuditFailureCode.MALFORMED_RECORD),
        ("record_hash", "invalid", AuditFailureCode.MALFORMED_RECORD),
        ("resource_scope", [], AuditFailureCode.NONCANONICAL_RECORD),
        (
            "resource_scope",
            {"type": "project", "name": "projects/123", "extra": "field"},
            AuditFailureCode.NONCANONICAL_RECORD,
        ),
        (
            "resource_scope",
            {"type": "project", "name": True},
            AuditFailureCode.NONCANONICAL_RECORD,
        ),
        ("outcome", True, AuditFailureCode.MALFORMED_RECORD),
        ("correlation_id", True, AuditFailureCode.MALFORMED_RECORD),
        ("diagnostic_codes", {}, AuditFailureCode.MALFORMED_RECORD),
        ("diagnostic_codes", [True], AuditFailureCode.MALFORMED_RECORD),
        ("facts", {}, AuditFailureCode.MALFORMED_RECORD),
        ("facts", ["fact"], AuditFailureCode.NONCANONICAL_RECORD),
        (
            "facts",
            [{"name": "label"}],
            AuditFailureCode.NONCANONICAL_RECORD,
        ),
        (
            "facts",
            [{"name": "label", "value": True}],
            AuditFailureCode.NONCANONICAL_RECORD,
        ),
    ],
)
def test_verification_rejects_every_noncanonical_record_field_shape(
    tmp_path: Path,
    field: str,
    value: object,
    expected: AuditFailureCode,
) -> None:
    """Closed record fields reject wrong scalar, collection, and nested shapes."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft())
    path = tmp_path / "audit-00000001.jsonl"
    payload = _record_payload(path)
    payload[field] = value
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is expected


def test_verification_returns_typed_failure_for_invalid_segment_filename(
    tmp_path: Path,
) -> None:
    """Unexpected segment names are integrity failures rather than parse exceptions."""
    journal = FilesystemAuditJournal(tmp_path)
    (tmp_path / "audit-invalid.jsonl").write_text("{}\n")

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.INVALID_SEGMENT_NAME


def test_verification_reports_earliest_record_failure_before_later_truncation(
    tmp_path: Path,
) -> None:
    """Chronological verification does not let a later parse failure mask tampering."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft(correlation_id="first"))
    journal.append(_draft(correlation_id="second"))
    path = tmp_path / "audit-00000001.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    first = json.loads(lines[0])
    first["outcome"] = "altered"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    lines[1] = lines[1][:-5]
    path.write_bytes(b"".join(lines))

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.RECORD_HASH_MISMATCH
    assert result.failure.sequence == 1


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


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema": "cqmgr.audit-manifest/v2", "last_sequence": 0},
        {
            "schema": "cqmgr.audit-manifest/v1",
            "last_sequence": True,
            "last_segment": 0,
            "last_hash": AUDIT_GENESIS_HASH,
        },
        {
            "schema": "cqmgr.audit-manifest/v1",
            "last_sequence": 0,
            "last_segment": 0,
            "last_hash": AUDIT_GENESIS_HASH,
            "extra": "field",
        },
    ],
)
def test_verification_rejects_newer_or_noncanonical_manifest_shape(
    tmp_path: Path,
    manifest: dict[str, object],
) -> None:
    """Manifest schema and closed field types fail closed before recovery."""
    journal = FilesystemAuditJournal(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    result = journal.verify()

    assert result.failure is not None
    assert result.failure.code is AuditFailureCode.MANIFEST_MISMATCH


def test_append_recovery_requires_manifest_to_match_an_exact_chain_prefix(
    tmp_path: Path,
) -> None:
    """An arbitrary lower manifest is not mistaken for a post-fsync crash window."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft(correlation_id="first"))
    journal.append(_draft(correlation_id="second"))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["last_sequence"] = 1
    manifest["last_segment"] = 1
    manifest["last_hash"] = "sha256:" + ("9" * 64)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(AuditIntegrityError, match="manifest-mismatch"):
        journal.append(_draft(correlation_id="third"))


def test_append_recovery_rejects_newer_manifest_before_writing(tmp_path: Path) -> None:
    """A newer manifest schema cannot be downgraded by a subsequent append."""
    journal = FilesystemAuditJournal(tmp_path)
    journal.append(_draft())
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["schema"] = "cqmgr.audit-manifest/v2"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(AuditIntegrityError, match="manifest-mismatch"):
        journal.append(_draft())


def test_new_segment_directory_entry_is_synced_before_durability_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash hook observes a new segment only after its directory entry is durable."""
    stages: list[str] = []
    original_sync = audit_adapter._fsync_directory  # noqa: SLF001

    def record_directory_sync(path: Path) -> None:
        stages.append("directory-fsync")
        original_sync(path)

    def record_hook(stage: str) -> None:
        stages.append(stage)

    monkeypatch.setattr(audit_adapter, "_fsync_directory", record_directory_sync)
    journal = FilesystemAuditJournal(tmp_path, failure_hook=record_hook)
    stages.clear()

    journal.append(_draft())

    assert stages[:2] == ["directory-fsync", "after-record-fsync"]


def test_rotation_checkpoint_crash_recovers_new_segment_without_lost_intent(
    tmp_path: Path,
) -> None:
    """A crash after a durable rotation checkpoint resumes in the new segment."""
    journal = FilesystemAuditJournal(tmp_path, max_records_per_segment=2)
    journal.append(_draft(correlation_id="one"))
    journal.append(_draft(correlation_id="two"))
    crashed = False

    def fail_after_checkpoint(stage: str) -> None:
        nonlocal crashed
        if stage == "after-record-fsync" and not crashed:
            crashed = True
            raise _InjectedCrashError

    crashing = FilesystemAuditJournal(
        tmp_path,
        max_records_per_segment=2,
        failure_hook=fail_after_checkpoint,
    )
    with pytest.raises(_InjectedCrashError):
        crashing.append(_draft(correlation_id="three"))

    recovered = FilesystemAuditJournal(tmp_path, max_records_per_segment=2)
    record = recovered.append(_draft(correlation_id="three"))
    records = recovered.query(AuditQuery(limit=10)).records

    assert record.segment == _SECOND_RECORD
    assert [item.draft.kind for item in records].count(
        AuditRecordKind.ROTATION_CHECKPOINT
    ) == 1
    assert recovered.verify().valid


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
