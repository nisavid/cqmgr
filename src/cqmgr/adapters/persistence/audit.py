"""Canonical real-filesystem append-only audit journal."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
from cqmgr.domain.redaction import REDACTION_MARKER, RedactedText
from cqmgr.domain.results import OperationName, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import Callable
    from os import PathLike

_SEGMENT_NAME: Final = "audit-{segment:08d}.jsonl"
_MANIFEST_NAME: Final = "manifest.json"
_MIN_RECORDS_PER_SEGMENT: Final = 2
_RECORD_FIELDS: Final = frozenset(
    {
        "schema",
        "record_id",
        "sequence",
        "segment",
        "kind",
        "operation",
        "resource_scope",
        "occurred_at",
        "outcome",
        "correlation_id",
        "diagnostic_codes",
        "facts",
        "previous_hash",
        "record_hash",
    }
)
_RESOURCE_SCOPE_FIELDS: Final = frozenset({"type", "name"})
_FACT_FIELDS: Final = frozenset({"name", "value"})
_MANIFEST_FIELDS: Final = frozenset(
    {"schema", "last_sequence", "last_segment", "last_hash"}
)
_SHA256_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEGMENT_PATTERN: Final = re.compile(r"audit-([0-9]{8})\.jsonl\Z")
_QUOTA_CONTACT_PATTERN: Final = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
_POSIX_MACHINE_PATH_PATTERN: Final = re.compile(r"(?<![\w:/])/(?!/)[^\s,;]+")
_WINDOWS_MACHINE_PATH_PATTERN: Final = re.compile(
    r"(?i)(?<!\w)(?:[A-Z]:[\\/]|\\\\)[^\s,;]+"
)
_RAW_PROVIDER_BODY_PATTERN: Final = re.compile(r"(?s)(?:\{.*\}|\[.*\])")
_RAW_PROVIDER_BODY_FACT_NAMES: Final = frozenset({"provider-body"})
_GOOGLE_ACCESS_TOKEN_PATTERN: Final = re.compile(r"(?<!\w)ya29\.[A-Za-z0-9._~-]+")


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
        if max_records_per_segment < _MIN_RECORDS_PER_SEGMENT:
            msg = "audit segments must allow at least two records"
            raise ValueError(msg)
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_records_per_segment = max_records_per_segment
        self._failure_hook = failure_hook or (lambda _stage: None)
        self._lock_path = self._root / ".journal.lock"
        with self._new_lock():
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
        with self._new_lock():
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
        with self._new_lock():
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
        with self._new_lock():
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
            with self._new_lock():
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

    def _new_lock(self) -> InterprocessFileLock:
        return InterprocessFileLock(self._lock_path)

    def _append_record(self, record: AuditRecord) -> None:
        path = self._segment_path(record.segment)
        is_new_segment = not path.exists()
        with path.open("ab") as stream:
            stream.write(self._encode(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if is_new_segment:
            _fsync_directory(self._root)
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
            explicit = RedactedText(
                value.value,
                sensitive_values=sensitive_values,
                machine_paths=machine_paths,
            )
            automatic = _QUOTA_CONTACT_PATTERN.sub(REDACTION_MARKER, explicit.value)
            automatic = _POSIX_MACHINE_PATH_PATTERN.sub(REDACTION_MARKER, automatic)
            automatic = _WINDOWS_MACHINE_PATH_PATTERN.sub(REDACTION_MARKER, automatic)
            automatic = _RAW_PROVIDER_BODY_PATTERN.sub(REDACTION_MARKER, automatic)
            automatic = _GOOGLE_ACCESS_TOKEN_PATTERN.sub(REDACTION_MARKER, automatic)
            return RedactedText(automatic)

        return replace(
            draft,
            correlation_id=(
                scrub(draft.correlation_id) if draft.correlation_id else None
            ),
            facts=tuple(
                AuditFact(
                    fact.name,
                    RedactedText(REDACTION_MARKER)
                    if fact.name.value in _RAW_PROVIDER_BODY_FACT_NAMES
                    else scrub(fact.value),
                )
                for fact in draft.facts
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
            record_hash=AUDIT_GENESIS_HASH,
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
        if not isinstance(data, dict):
            raise _MalformedAuditRecordError
        if data.get("schema") != AUDIT_RECORD_SCHEMA:
            raise _UnsupportedAuditSchemaError
        if set(data) != _RECORD_FIELDS:
            raise _NoncanonicalAuditRecordError
        cls._validate_record_field_shapes(data)
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
        record = AuditRecord(
            record_id=data["record_id"],
            sequence=data["sequence"],
            segment=data["segment"],
            draft=draft,
            previous_hash=data["previous_hash"],
            record_hash=data["record_hash"],
        )
        if raw != cls._encode(record):
            raise _NoncanonicalAuditRecordError
        return record

    @staticmethod
    def _validate_record_field_shapes(data: dict[str, Any]) -> None:
        required_strings = (
            "record_id",
            "kind",
            "operation",
            "occurred_at",
            "previous_hash",
            "record_hash",
        )
        if any(not isinstance(data[name], str) for name in required_strings):
            raise _MalformedAuditRecordError
        if type(data["sequence"]) is not int or type(data["segment"]) is not int:
            raise _MalformedAuditRecordError
        if data["sequence"] < 1 or data["segment"] < 1:
            raise _MalformedAuditRecordError
        if _SHA256_PATTERN.fullmatch(data["previous_hash"]) is None or (
            _SHA256_PATTERN.fullmatch(data["record_hash"]) is None
        ):
            raise _MalformedAuditRecordError
        FilesystemAuditJournal._validate_record_scope(data["resource_scope"])
        FilesystemAuditJournal._validate_record_collections(data)

    @staticmethod
    def _validate_record_scope(scope: object) -> None:
        if scope is not None and (
            not isinstance(scope, dict)
            or set(scope) != _RESOURCE_SCOPE_FIELDS
            or any(not isinstance(scope[name], str) for name in _RESOURCE_SCOPE_FIELDS)
        ):
            raise _NoncanonicalAuditRecordError

    @staticmethod
    def _validate_record_collections(data: dict[str, Any]) -> None:
        if data["outcome"] is not None and not isinstance(data["outcome"], str):
            raise _MalformedAuditRecordError
        if data["correlation_id"] is not None and not isinstance(
            data["correlation_id"], str
        ):
            raise _MalformedAuditRecordError
        diagnostic_codes = data["diagnostic_codes"]
        if not isinstance(diagnostic_codes, list) or any(
            not isinstance(code, str) for code in diagnostic_codes
        ):
            raise _MalformedAuditRecordError
        facts = data["facts"]
        if not isinstance(facts, list):
            raise _MalformedAuditRecordError
        if any(
            not isinstance(fact, dict)
            or set(fact) != _FACT_FIELDS
            or any(not isinstance(fact[name], str) for name in _FACT_FIELDS)
            for fact in facts
        ):
            raise _NoncanonicalAuditRecordError

    def _read_all(self, *, check_manifest: bool = False) -> tuple[AuditRecord, ...]:
        records: list[AuditRecord] = []
        paths = self._ordered_segment_paths()
        expected_hash = AUDIT_GENESIS_HASH
        expected_sequence = 1
        for expected_segment, (actual_segment, path) in enumerate(paths, start=1):
            if actual_segment != expected_segment:
                self._fail(
                    AuditFailureCode.MISSING_SEGMENT, expected_segment, None, None
                )
            for position, raw_line in enumerate(
                path.read_bytes().splitlines(keepends=True),
                start=1,
            ):
                if not raw_line.endswith(b"\n"):
                    self._fail(
                        AuditFailureCode.MALFORMED_RECORD,
                        actual_segment,
                        expected_sequence,
                        None,
                    )
                raw = raw_line[:-1]
                record = self._decode_record_or_fail(
                    raw,
                    actual_segment=actual_segment,
                    expected_sequence=expected_sequence,
                )
                self._verify_rotation_checkpoint(
                    record,
                    actual_segment=actual_segment,
                    position=position,
                )
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

    def _verify_rotation_checkpoint(
        self,
        record: AuditRecord,
        *,
        actual_segment: int,
        position: int,
    ) -> None:
        is_checkpoint = record.draft.kind is AuditRecordKind.ROTATION_CHECKPOINT
        checkpoint_required = actual_segment > 1 and position == 1
        if is_checkpoint != checkpoint_required:
            self._fail(
                AuditFailureCode.INVALID_ROTATION_CHECKPOINT,
                actual_segment,
                record.sequence,
                record.record_id,
            )
        if not is_checkpoint:
            return
        draft = record.draft
        valid = (
            draft.operation == OperationName("audit.rotate")
            and draft.resource_scope is None
            and draft.outcome is None
            and draft.correlation_id is None
            and not draft.diagnostic_codes
            and len(draft.facts) == 1
            and draft.facts[0].name == StableSymbol("previous-segment")
            and draft.facts[0].value.value == str(actual_segment - 1)
        )
        if not valid:
            self._fail(
                AuditFailureCode.INVALID_ROTATION_CHECKPOINT,
                actual_segment,
                record.sequence,
                record.record_id,
            )

    def _ordered_segment_paths(self) -> tuple[tuple[int, Path], ...]:
        parsed: list[tuple[int, Path]] = []
        for path in self._root.glob("audit-*.jsonl"):
            match = _SEGMENT_PATTERN.fullmatch(path.name)
            if match is None:
                self._fail(AuditFailureCode.INVALID_SEGMENT_NAME, 0, None, None)
            parsed.append((int(match.group(1)), path))
        return tuple(sorted(parsed))

    def _decode_record_or_fail(
        self,
        raw: bytes,
        *,
        actual_segment: int,
        expected_sequence: int,
    ) -> AuditRecord:
        try:
            return self._decode(raw)
        except _UnsupportedAuditSchemaError:
            self._fail(
                AuditFailureCode.UNSUPPORTED_SCHEMA,
                actual_segment,
                expected_sequence,
                None,
            )
        except _NoncanonicalAuditRecordError:
            self._fail(
                AuditFailureCode.NONCANONICAL_RECORD,
                actual_segment,
                expected_sequence,
                None,
            )
        except (
            _MalformedAuditRecordError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            self._fail(
                AuditFailureCode.MALFORMED_RECORD,
                actual_segment,
                expected_sequence,
                None,
            )

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
        expected_record_id = f"audit-{record.sequence:020d}"
        if record.record_id != expected_record_id:
            self._fail(
                AuditFailureCode.RECORD_ID_MISMATCH,
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
        expected_digest = hashlib.sha256(self._canonical_mapping(record)).hexdigest()
        if record.record_hash != f"sha256:{expected_digest}":
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
            self._require_empty_manifest(manifest)
            return ()
        last = records[-1]
        if manifest is None:
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)
        manifest_sequence = manifest["last_sequence"]
        if manifest_sequence < last.sequence:
            self._require_manifest_prefix(manifest, records)
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
        if (
            not isinstance(data, dict)
            or set(data) != _MANIFEST_FIELDS
            or data.get("schema") != "cqmgr.audit-manifest/v1"
            or type(data.get("last_sequence")) is not int
            or type(data.get("last_segment")) is not int
            or data["last_sequence"] < 0
            or data["last_segment"] < 0
            or not isinstance(data.get("last_hash"), str)
            or _SHA256_PATTERN.fullmatch(data["last_hash"]) is None
        ):
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)
        return data

    def _require_empty_manifest(self, manifest: dict[str, Any] | None) -> None:
        expected = {
            "schema": "cqmgr.audit-manifest/v1",
            "last_sequence": 0,
            "last_segment": 0,
            "last_hash": AUDIT_GENESIS_HASH,
        }
        if manifest != expected:
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)

    def _require_manifest_prefix(
        self,
        manifest: dict[str, Any],
        records: tuple[AuditRecord, ...],
    ) -> None:
        sequence = manifest["last_sequence"]
        if sequence == 0:
            self._require_empty_manifest(manifest)
            return
        prefix = records[sequence - 1]
        if (
            manifest["last_segment"] != prefix.segment
            or manifest["last_hash"] != prefix.record_hash
        ):
            self._fail(AuditFailureCode.MANIFEST_MISMATCH, 0, None, None)

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
            TypeError,
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


class _MalformedAuditRecordError(Exception):
    """A retained JSON value cannot form a typed audit record."""


class _NoncanonicalAuditRecordError(Exception):
    """A retained record has valid meaning but noncanonical bytes or fields."""


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows matrix
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
