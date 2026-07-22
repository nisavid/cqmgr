"""Canonical real-filesystem append-only audit journal."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Never

from cqmgr.adapters.persistence.locking import InterprocessFileLock
from cqmgr.domain.audit import (
    AUDIT_GENESIS_HASH,
    AUDIT_RECORD_SCHEMA,
    AuditFact,
    AuditFailureCode,
    AuditQuery,
    AuditQueryPage,
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

if TYPE_CHECKING:
    from collections.abc import Callable
    from os import PathLike

_SEGMENT_NAME: Final = "audit-{segment:08d}.jsonl"
_MANIFEST_NAME: Final = "manifest.json"
_MIN_RECORDS_PER_SEGMENT: Final = 2


class FilesystemAuditJournal:
    """Append-only JSON-lines journal with a canonical SHA-256 chain."""

    def __init__(
        self,
        root: str | PathLike[str],
        *,
        max_records_per_segment: int = 1000,
        failure_hook: Callable[[str], None] | None = None,
    ) -> None:
        """Open one installation-local journal and recover a stale manifest."""
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_records_per_segment = max_records_per_segment
        self._failure_hook = failure_hook or (lambda _stage: None)
        self._lock = InterprocessFileLock(self._root / ".journal.lock")
        if max_records_per_segment < _MIN_RECORDS_PER_SEGMENT:
            msg = "audit segments must allow at least two records"
            raise ValueError(msg)
        with self._lock:
            if not (self._root / _MANIFEST_NAME).exists() and not tuple(
                self._root.glob("audit-*.jsonl")
            ):
                self._write_empty_manifest()

    def append(
        self,
        draft: AuditRecordDraft,
        *,
        sensitive_values: tuple[str, ...] = (),
        machine_paths: tuple[str, ...] = (),
    ) -> AuditRecord:
        """Append and fsync one canonical record before returning it."""
        with self._lock:
            records = self._recover_and_read()
            safe_draft = self._scrub(draft, sensitive_values, machine_paths)
            segment = records[-1].segment if records else 1
            segment_count = sum(record.segment == segment for record in records)
            if records and segment_count >= self._max_records_per_segment:
                segment += 1
                checkpoint = self._build_record(
                    AuditRecordDraft(
                        kind=AuditRecordKind.ROTATION_CHECKPOINT,
                        operation=OperationName("audit.rotate"),
                        resource_scope=None,
                        occurred_at=safe_draft.occurred_at,
                        facts=(
                            AuditFact(
                                StableSymbol("previous-segment"),
                                RedactedText(str(segment - 1)),
                            ),
                        ),
                    ),
                    sequence=len(records) + 1,
                    segment=segment,
                    previous_hash=records[-1].record_hash,
                )
                self._append_record(checkpoint)
                records = (*records, checkpoint)
            record = self._build_record(
                safe_draft,
                sequence=len(records) + 1,
                segment=segment,
                previous_hash=records[-1].record_hash
                if records
                else AUDIT_GENESIS_HASH,
            )
            self._append_record(record)
            return record

    def query(self, query: AuditQuery) -> AuditQueryPage:
        """Read one filter-bound page in ascending chain order."""
        with self._lock:
            records = self._read_all(check_manifest=True)
            start = self._decode_cursor(query) if query.cursor is not None else 0
            filtered = tuple(
                record for record in records if self._matches(record, query)
            )
            page = filtered[start : start + query.limit]
            next_offset = start + len(page)
            cursor = (
                self._encode_cursor(query, next_offset)
                if next_offset < len(filtered)
                else None
            )
            return AuditQueryPage(records=page, next_cursor=cursor)

    def inspect(self, record_id: str) -> AuditRecord | None:
        """Read one exact record identity without exposing storage paths."""
        with self._lock:
            return next(
                (
                    record
                    for record in self._read_all(check_manifest=True)
                    if record.record_id == record_id
                ),
                None,
            )

    def verify(
        self, *, from_record_id: str | None = None, through_record_id: str | None = None
    ) -> AuditVerification:
        """Verify exact sequence and hash continuity for a retained range."""
        try:
            with self._lock:
                records = self._read_all(check_manifest=True)
        except AuditIntegrityError as error:
            return AuditVerification(
                valid=False,
                verified_from=None,
                verified_through=None,
                failure=error.failure,
            )
        selected = self._select_range(records, from_record_id, through_record_id)
        if isinstance(selected, AuditVerificationFailure):
            return AuditVerification(
                valid=False,
                verified_from=None,
                verified_through=None,
                failure=selected,
            )
        return AuditVerification(
            valid=True,
            verified_from=selected[0].record_id if selected else None,
            verified_through=selected[-1].record_id if selected else None,
        )

    def _segment_path(self, segment: int) -> Path:
        return self._root / _SEGMENT_NAME.format(segment=segment)

    def _append_record(self, record: AuditRecord) -> None:
        path = self._segment_path(record.segment)
        with path.open("ab") as stream:
            stream.write(self._encode(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._failure_hook("after-record-fsync")
        self._write_manifest(record)
        self._failure_hook("after-manifest-fsync")

    @staticmethod
    def _scrub(
        draft: AuditRecordDraft,
        sensitive_values: tuple[str, ...],
        machine_paths: tuple[str, ...],
    ) -> AuditRecordDraft:
        def scrub(value: RedactedText) -> RedactedText:
            return RedactedText(
                value.value,
                sensitive_values=sensitive_values,
                machine_paths=machine_paths,
            )

        return replace(
            draft,
            correlation_id=(
                scrub(draft.correlation_id) if draft.correlation_id else None
            ),
            facts=tuple(
                AuditFact(fact.name, scrub(fact.value)) for fact in draft.facts
            ),
        )

    @classmethod
    def _build_record(
        cls,
        draft: AuditRecordDraft,
        *,
        sequence: int,
        segment: int,
        previous_hash: str,
    ) -> AuditRecord:
        record_id = f"audit-{sequence:020d}"
        provisional = AuditRecord(
            record_id=record_id,
            sequence=sequence,
            segment=segment,
            draft=draft,
            previous_hash=previous_hash,
            record_hash="",
        )
        digest = hashlib.sha256(cls._canonical_mapping(provisional)).hexdigest()
        return replace(provisional, record_hash=f"sha256:{digest}")

    @classmethod
    def _encode(cls, record: AuditRecord) -> bytes:
        mapping = cls._mapping(record)
        return json.dumps(
            mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

    @classmethod
    def _canonical_mapping(cls, record: AuditRecord) -> bytes:
        mapping = cls._mapping(record)
        mapping.pop("record_hash")
        return json.dumps(
            mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

    @staticmethod
    def _mapping(record: AuditRecord) -> dict[str, Any]:
        draft = record.draft
        return {
            "schema": record.schema,
            "record_id": record.record_id,
            "sequence": record.sequence,
            "segment": record.segment,
            "kind": draft.kind.value,
            "operation": draft.operation.value,
            "resource_scope": (
                {
                    "type": draft.resource_scope.kind.value,
                    "name": draft.resource_scope.canonical_name,
                }
                if draft.resource_scope is not None
                else None
            ),
            "occurred_at": draft.occurred_at.isoformat().replace("+00:00", "Z"),
            "outcome": draft.outcome.value if draft.outcome else None,
            "correlation_id": (
                draft.correlation_id.value if draft.correlation_id else None
            ),
            "diagnostic_codes": [code.value for code in draft.diagnostic_codes],
            "facts": [
                {"name": fact.name.value, "value": fact.value.value}
                for fact in draft.facts
            ],
            "previous_hash": record.previous_hash,
            "record_hash": record.record_hash,
        }

    @classmethod
    def _decode(cls, raw: bytes) -> AuditRecord:
        data = json.loads(raw)
        if data.get("schema") != AUDIT_RECORD_SCHEMA:
            raise _UnsupportedAuditSchemaError
        scope_data = data["resource_scope"]
        scope = (
            ResourceScope(
                ResourceScopeKind(scope_data["type"]),
                scope_data["name"],
            )
            if scope_data is not None
            else None
        )
        occurred_at = datetime.fromisoformat(data["occurred_at"])
        draft = AuditRecordDraft(
            kind=AuditRecordKind(data["kind"]),
            operation=OperationName(data["operation"]),
            resource_scope=scope,
            occurred_at=occurred_at,
            outcome=StableSymbol(data["outcome"]) if data["outcome"] else None,
            correlation_id=(
                RedactedText(data["correlation_id"])
                if data["correlation_id"] is not None
                else None
            ),
            diagnostic_codes=tuple(
                DiagnosticCode(code) for code in data["diagnostic_codes"]
            ),
            facts=tuple(
                AuditFact(StableSymbol(fact["name"]), RedactedText(fact["value"]))
                for fact in data["facts"]
            ),
        )
        return AuditRecord(
            record_id=data["record_id"],
            sequence=data["sequence"],
            segment=data["segment"],
            draft=draft,
            previous_hash=data["previous_hash"],
            record_hash=data["record_hash"],
        )

    def _read_all(self, *, check_manifest: bool = False) -> tuple[AuditRecord, ...]:
        records: list[AuditRecord] = []
        paths = sorted(self._root.glob("audit-*.jsonl"))
        expected_hash = AUDIT_GENESIS_HASH
        expected_sequence = 1
        for expected_segment, path in enumerate(paths, start=1):
            actual_segment = int(path.stem.removeprefix("audit-"))
            if actual_segment != expected_segment:
                self._fail(
                    AuditFailureCode.MISSING_SEGMENT, expected_segment, None, None
                )
            decoded = self._read_segment(
                path,
                actual_segment=actual_segment,
                expected_sequence=expected_sequence,
            )
            for record in decoded:
                self._verify_record(
                    record,
                    actual_segment=actual_segment,
                    expected_sequence=expected_sequence,
                    expected_hash=expected_hash,
                )
                records.append(record)
                expected_hash = record.record_hash
                expected_sequence += 1
        if check_manifest:
            self._verify_manifest(tuple(records))
        return tuple(records)

    def _read_segment(
        self,
        path: Path,
        *,
        actual_segment: int,
        expected_sequence: int,
    ) -> tuple[AuditRecord, ...]:
        content = path.read_bytes()
        if content and not content.endswith(b"\n"):
            self._fail(
                AuditFailureCode.MALFORMED_RECORD,
                actual_segment,
                expected_sequence + content.count(b"\n"),
                None,
            )
        records: list[AuditRecord] = []
        for index, raw in enumerate(content.splitlines()):
            try:
                records.append(self._decode(raw))
            except _UnsupportedAuditSchemaError:
                self._fail(
                    AuditFailureCode.UNSUPPORTED_SCHEMA,
                    actual_segment,
                    expected_sequence + index,
                    None,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._fail(
                    AuditFailureCode.MALFORMED_RECORD,
                    actual_segment,
                    expected_sequence + index,
                    None,
                )
        return tuple(records)

    def _verify_record(
        self,
        record: AuditRecord,
        *,
        actual_segment: int,
        expected_sequence: int,
        expected_hash: str,
    ) -> None:
        if record.segment != actual_segment or record.sequence != expected_sequence:
            self._fail(
                AuditFailureCode.SEQUENCE_GAP,
                actual_segment,
                record.sequence,
                record.record_id,
            )
        if record.previous_hash != expected_hash:
            self._fail(
                AuditFailureCode.PREVIOUS_HASH_MISMATCH,
                actual_segment,
                record.sequence,
                record.record_id,
            )
        expected_record = self._build_record(
            record.draft,
            sequence=record.sequence,
            segment=record.segment,
            previous_hash=record.previous_hash,
        )
        if record.record_hash != expected_record.record_hash:
            self._fail(
                AuditFailureCode.RECORD_HASH_MISMATCH,
                actual_segment,
                record.sequence,
                record.record_id,
            )

    def _recover_and_read(self) -> tuple[AuditRecord, ...]:
        records = self._read_all()
        manifest = self._read_manifest()
        if not records:
            if manifest is not None and manifest.get("last_sequence") != 0:
                self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)
            return ()
        last = records[-1]
        if manifest is None:
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)
        if manifest.get("last_sequence", -1) < last.sequence:
            self._write_manifest(last)
            return records
        self._verify_manifest(records)
        return records

    def _read_manifest(self) -> dict[str, Any] | None:
        path = self._root / _MANIFEST_NAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError):
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)
        return data

    def _verify_manifest(self, records: tuple[AuditRecord, ...]) -> None:
        manifest = self._read_manifest()
        if manifest is None:
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)
        expected = (
            {
                "schema": "cqmgr.audit-manifest/v1",
                "last_sequence": records[-1].sequence,
                "last_segment": records[-1].segment,
                "last_hash": records[-1].record_hash,
            }
            if records
            else {
                "schema": "cqmgr.audit-manifest/v1",
                "last_sequence": 0,
                "last_segment": 0,
                "last_hash": AUDIT_GENESIS_HASH,
            }
        )
        if manifest != expected:
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)

    @staticmethod
    def _fail(
        code: AuditFailureCode,
        segment: int,
        sequence: int | None,
        record_id: str | None,
    ) -> Never:
        raise AuditIntegrityError(
            AuditVerificationFailure(code, segment, sequence, record_id)
        )

    @staticmethod
    def _matches(record: AuditRecord, query: AuditQuery) -> bool:
        return (
            (not query.operations or record.draft.operation in query.operations)
            and (
                not query.outcomes
                or (
                    record.draft.outcome is not None
                    and record.draft.outcome in query.outcomes
                )
            )
            and (query.since is None or record.draft.occurred_at >= query.since)
            and (query.until is None or record.draft.occurred_at <= query.until)
        )

    @classmethod
    def _query_binding(cls, query: AuditQuery) -> dict[str, Any]:
        return {
            "operations": [value.value for value in query.operations],
            "outcomes": [value.value for value in query.outcomes],
            "since": query.since.isoformat() if query.since else None,
            "until": query.until.isoformat() if query.until else None,
        }

    @classmethod
    def _encode_cursor(cls, query: AuditQuery, offset: int) -> str:
        payload = {"binding": cls._query_binding(query), "offset": offset}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        return base64.urlsafe_b64encode(raw + b"." + digest.encode()).decode()

    @classmethod
    def _decode_cursor(cls, query: AuditQuery) -> int:
        try:
            decoded = base64.urlsafe_b64decode(query.cursor or "")
            raw, supplied_digest = decoded.rsplit(b".", maxsplit=1)
            payload = json.loads(raw)
            offset = cls._validate_cursor_payload(
                query,
                payload=payload,
                raw=raw,
                supplied_digest=supplied_digest,
            )
        except (
            ValueError,
            KeyError,
            json.JSONDecodeError,
            _InvalidCursorError,
        ) as error:
            msg = "audit cursor is invalid or does not match the query"
            raise ValueError(msg) from error
        return offset

    @classmethod
    def _validate_cursor_payload(
        cls,
        query: AuditQuery,
        *,
        payload: dict[str, Any],
        raw: bytes,
        supplied_digest: bytes,
    ) -> int:
        offset = payload["offset"]
        invalid = (
            hashlib.sha256(raw).hexdigest().encode() != supplied_digest
            or payload["binding"] != cls._query_binding(query)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        )
        if invalid:
            raise _InvalidCursorError
        return offset

    @staticmethod
    def _select_range(
        records: tuple[AuditRecord, ...],
        from_record_id: str | None,
        through_record_id: str | None,
    ) -> tuple[AuditRecord, ...] | AuditVerificationFailure:
        positions = {record.record_id: index for index, record in enumerate(records)}
        for identity in (from_record_id, through_record_id):
            if identity is not None and identity not in positions:
                return AuditVerificationFailure(
                    AuditFailureCode.RECORD_NOT_FOUND, 0, None, identity
                )
        start = positions[from_record_id] if from_record_id is not None else 0
        if through_record_id is not None and positions[through_record_id] < start:
            return AuditVerificationFailure(
                AuditFailureCode.SEQUENCE_GAP, 0, None, through_record_id
            )
        end = (
            positions[through_record_id] + 1
            if through_record_id is not None
            else len(records)
        )
        return records[start:end]

    def _write_manifest(self, record: AuditRecord) -> None:
        self._write_manifest_data(
            {
                "schema": "cqmgr.audit-manifest/v1",
                "last_sequence": record.sequence,
                "last_segment": record.segment,
                "last_hash": record.record_hash,
            }
        )

    def _write_empty_manifest(self) -> None:
        self._write_manifest_data(
            {
                "schema": "cqmgr.audit-manifest/v1",
                "last_sequence": 0,
                "last_segment": 0,
                "last_hash": AUDIT_GENESIS_HASH,
            }
        )

    def _write_manifest_data(self, data: dict[str, Any]) -> None:
        temporary = self._root / f".{_MANIFEST_NAME}.tmp"
        with temporary.open("wb") as stream:
            stream.write(
                json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self._root / _MANIFEST_NAME)
        _fsync_directory(self._root)


class AuditIntegrityError(Exception):
    """Retained audit bytes failed exact continuity verification."""

    def __init__(self, failure: AuditVerificationFailure) -> None:
        """Retain the first exact continuity failure without a storage path."""
        super().__init__(failure.code.value)
        self.failure = failure


class _UnsupportedAuditSchemaError(Exception):
    """A retained record has a newer or unknown schema."""


class _InvalidCursorError(Exception):
    """A cursor failed integrity or query-binding validation."""


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows matrix
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
