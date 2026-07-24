"""Authenticated atomic local persistence for durable Apply records."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock
from cqmgr.adapters.persistence.windows_acl import restrict_windows_acl
from cqmgr.application.ports.apply_records import (
    ApplyRecordRepositoryOutcome,
    ApplyRecordRepositoryStatus,
)
from cqmgr.application.ports.secrets import SecretValue
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

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_SCHEMA = "cqmgr.apply-record/v1"
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_MINIMUM_KEY_BYTES = 32
_DIMENSION_PAIR_SIZE = 2
_RECORD_FIELDS = frozenset(
    {
        "intent_id",
        "plan_digest",
        "kind",
        "resource_scope",
        "created_at",
        "state",
        "finished_at",
        "revision",
        "children",
    }
)
_RECORD_SEQUENCE_FIELD = frozenset({"creation_sequence"})
_LEGACY_CHILD_FIELDS = frozenset(
    {
        "child_id",
        "slice_identity",
        "target",
        "preference_identity",
        "etag",
        "preference_existed",
        "dispatch_intent_at",
        "disposition",
        "provider_outcome",
        "outcome_recorded_at",
        "unknown_resolution",
        "resolution_recorded_at",
    }
)
_CHILD_LINEAGE_FIELDS = frozenset({"accepted_etag", "accepted_trace_id"})
_CHILD_BASELINE_FIELD = frozenset({"baseline"})
_LEGACY_RESOLUTION_FIELDS = frozenset(
    {
        "intent_id",
        "child_id",
        "resolution",
        "recorded_at",
        "checkpoint",
    }
)
_RESOLUTION_LINEAGE_FIELDS = frozenset({"lineage_etag", "lineage_trace_id"})
_SLICE_FIELDS = frozenset(
    {
        "resource_scope",
        "service",
        "quota_id",
        "dimensions",
        "quota_scope",
    }
)
_SCOPE_FIELDS = frozenset({"kind", "canonical_name"})
_QUANTITY_FIELDS = frozenset({"value", "unit"})


class LocalApplyRecordRepository:
    """Serialize each authenticated Apply record under one native lock."""

    def __init__(
        self,
        root: Path,
        *,
        lock: NativePlanInterprocessLock | None = None,
    ) -> None:
        """Create the private store and its interprocess lock."""
        self._root = root / "apply-records"
        self._resolutions = root / "apply-resolutions"
        self._root.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
        self._resolutions.mkdir(
            parents=True,
            exist_ok=True,
            mode=_PRIVATE_DIRECTORY_MODE,
        )
        for directory in (self._root, self._resolutions):
            directory.chmod(_PRIVATE_DIRECTORY_MODE)
            restrict_windows_acl(directory)
        self._lock = lock or NativePlanInterprocessLock(
            root / ".apply-record-repository.lock"
        )

    def create(
        self, record: ApplyRecord, authentication_key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        """Create one revision-zero record without replacement."""
        if record.revision != 0:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        try:
            key = _key_bytes(authentication_key)
            path = self._path(record.intent_id)
        except (TypeError, ValueError):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return self._with_lock(lambda: self._create_locked(path, record, key))

    def _create_locked(  # noqa: PLR0911
        self, path: Path, record: ApplyRecord, key: bytes
    ) -> ApplyRecordRepositoryOutcome:
        if path.exists():
            existing = self._read(path, key)
            if (
                existing is not None
                and replace(existing, creation_sequence=None) == record
            ):
                return ApplyRecordRepositoryOutcome(
                    ApplyRecordRepositoryStatus.STORED,
                    existing,
                )
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        try:
            retained = tuple(
                self._read(candidate, key)
                for candidate in sorted(self._root.glob("*.json"))
            )
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        if any(item is None for item in retained):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        sequences = tuple(
            item.creation_sequence
            for item in retained
            if item is not None and item.creation_sequence is not None
        )
        if len(sequences) != len(set(sequences)):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        record = replace(record, creation_sequence=max(sequences, default=0) + 1)
        try:
            _publish(path, _encode(record, key), replace=False)
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.STORED, record)

    def load(
        self, intent_id: str, authentication_key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        """Load one authenticated record."""
        try:
            key = _key_bytes(authentication_key)
            path = self._path(intent_id)
        except (TypeError, ValueError):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return self._with_lock(lambda: self._load_locked(path, key))

    def _load_locked(self, path: Path, key: bytes) -> ApplyRecordRepositoryOutcome:
        if not path.exists():
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        try:
            record = self._read(path, key)
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        if record is None:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE,
            record,
        )

    def save(
        self, record: ApplyRecord, authentication_key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        """Commit only the next authenticated record revision."""
        try:
            key = _key_bytes(authentication_key)
            path = self._path(record.intent_id)
        except (TypeError, ValueError):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return self._with_lock(lambda: self._save_locked(path, record, key))

    def append_unknown_resolution(  # noqa: PLR0913
        self,
        intent_id: str,
        child_id: str,
        resolution: UnknownDispatchResolution,
        recorded_at: datetime,
        authentication_key: SecretValue,
        *,
        lineage_etag: str | None = None,
        lineage_trace_id: str | None = None,
    ) -> ApplyRecordRepositoryOutcome:
        """Append one child resolution without replacing Apply state."""
        try:
            key = _key_bytes(authentication_key)
            directory = self._resolution_directory(intent_id)
            path = directory / f"{hashlib.sha256(child_id.encode()).hexdigest()}.json"
            evidence = UnknownResolutionEvidence(
                intent_id,
                child_id,
                resolution,
                recorded_at,
                lineage_etag=lineage_etag,
                lineage_trace_id=lineage_trace_id,
            )
        except (TypeError, ValueError):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return self._with_lock(
            lambda: self._append_resolution_locked(path, evidence, key)
        )

    def _append_resolution_locked(
        self,
        path: Path,
        evidence: UnknownResolutionEvidence,
        key: bytes,
    ) -> ApplyRecordRepositoryOutcome:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=_PRIVATE_DIRECTORY_MODE,
        )
        path.parent.chmod(_PRIVATE_DIRECTORY_MODE)
        restrict_windows_acl(path.parent)
        if path.exists():
            existing = self._read_resolution(path, key)
            if existing is not None and replace(existing, checkpoint=1) == evidence:
                return ApplyRecordRepositoryOutcome(
                    ApplyRecordRepositoryStatus.STORED,
                    resolutions=(existing,),
                )
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        retained = self._load_resolutions_locked(path.parent, key)
        if retained.status is not ApplyRecordRepositoryStatus.AVAILABLE:
            return retained
        evidence = replace(
            evidence,
            checkpoint=max(
                (item.checkpoint for item in retained.resolutions),
                default=0,
            )
            + 1,
        )
        try:
            _publish(path, _encode_resolution(evidence, key), replace=False)
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.STORED,
            resolutions=(evidence,),
        )

    def load_unknown_resolutions(
        self,
        intent_id: str,
        authentication_key: SecretValue,
    ) -> ApplyRecordRepositoryOutcome:
        """Load independent append-only resolution evidence."""
        try:
            key = _key_bytes(authentication_key)
            directory = self._resolution_directory(intent_id)
        except (TypeError, ValueError):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return self._with_lock(lambda: self._load_resolutions_locked(directory, key))

    def find_superseding_record(
        self,
        selected_intent_id: str,
        preference_identities: frozenset[str],
        authentication_key: SecretValue,
    ) -> ApplyRecordRepositoryOutcome:
        """Find the earliest later authenticated Apply that may have dispatched."""
        if (
            not isinstance(preference_identities, frozenset)
            or not preference_identities
            or any(
                not isinstance(identity, str) or not identity
                for identity in preference_identities
            )
        ):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        try:
            key = _key_bytes(authentication_key)
            selected_path = self._path(selected_intent_id)
        except (TypeError, ValueError):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return self._with_lock(
            lambda: self._find_superseding_record_locked(
                selected_path,
                preference_identities,
                key,
            )
        )

    def _find_superseding_record_locked(  # noqa: PLR0911
        self,
        selected_path: Path,
        preference_identities: frozenset[str],
        key: bytes,
    ) -> ApplyRecordRepositoryOutcome:
        if not selected_path.exists():
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        try:
            records = tuple(
                self._read(path, key) for path in sorted(self._root.glob("*.json"))
            )
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        if any(record is None for record in records):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        authenticated = tuple(record for record in records if record is not None)
        selected = next(
            (
                record
                for record in authenticated
                if self._path(record.intent_id) == selected_path
            ),
            None,
        )
        if selected is None:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        sequences = tuple(
            record.creation_sequence
            for record in authenticated
            if record.creation_sequence is not None
        )
        if len(sequences) != len(set(sequences)):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        superseding = tuple(
            record
            for record in authenticated
            if record.intent_id != selected.intent_id
            and _created_after(record, selected)
            and any(
                child.preference_identity in preference_identities
                and child.dispatch_intent_at is not None
                and child.disposition is not ApplyChildDisposition.FAILED
                for child in record.children
            )
        )
        if not superseding:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        earliest = min(
            superseding,
            key=lambda record: (
                record.creation_sequence is None,
                record.creation_sequence or 0,
                record.intent_id,
            ),
        )
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE,
            earliest,
        )

    def _load_resolutions_locked(
        self,
        directory: Path,
        key: bytes,
    ) -> ApplyRecordRepositoryOutcome:
        if not directory.exists():
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.AVAILABLE)
        try:
            evidence = tuple(
                self._read_resolution(path, key)
                for path in sorted(directory.glob("*.json"))
            )
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        if any(item is None for item in evidence):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        ordered = tuple(
            sorted(
                (item for item in evidence if item is not None),
                key=lambda item: item.checkpoint,
            )
        )
        if tuple(item.checkpoint for item in ordered) != tuple(
            range(1, len(ordered) + 1)
        ):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        return ApplyRecordRepositoryOutcome(
            ApplyRecordRepositoryStatus.AVAILABLE,
            resolutions=ordered,
        )

    def _save_locked(
        self, path: Path, record: ApplyRecord, key: bytes
    ) -> ApplyRecordRepositoryOutcome:
        if not path.exists():
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.MISSING)
        try:
            existing = self._read(path, key)
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        if existing is None:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        if record.creation_sequence is None:
            record = replace(record, creation_sequence=existing.creation_sequence)
        if (
            existing.intent_id != record.intent_id
            or existing.plan_digest != record.plan_digest
            or existing.creation_sequence != record.creation_sequence
            or record.revision != existing.revision + 1
        ):
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.CONFLICT)
        try:
            _publish(path, _encode(record, key), replace=True)
        except OSError:
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)
        return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.STORED, record)

    def _path(self, intent_id: str) -> Path:
        match = _DIGEST.fullmatch(intent_id)
        if match is None:
            msg = "Apply intent identity must be canonical sha256"
            raise ValueError(msg)
        return self._root / f"{match.group(1)}.json"

    def _resolution_directory(self, intent_id: str) -> Path:
        match = _DIGEST.fullmatch(intent_id)
        if match is None:
            msg = "Apply intent identity must be canonical sha256"
            raise ValueError(msg)
        return self._resolutions / match.group(1)

    def _read(self, path: Path, key: bytes) -> ApplyRecord | None:
        if (
            path.is_symlink()
            or not path.is_file()
            or (
                os.name != "nt"
                and stat.S_IMODE(path.stat().st_mode) != _PRIVATE_FILE_MODE
            )
        ):
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {
            "schema",
            "record",
            "authentication",
        }:
            return None
        if raw["schema"] != _SCHEMA or not isinstance(raw["record"], dict):
            return None
        expected = _authentication(raw["record"], key)
        supplied = raw["authentication"]
        if not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
            return None
        try:
            return _decode_record(raw["record"])
        except (KeyError, TypeError, ValueError):
            return None

    def _read_resolution(
        self,
        path: Path,
        key: bytes,
    ) -> UnknownResolutionEvidence | None:
        if (
            path.is_symlink()
            or not path.is_file()
            or (
                os.name != "nt"
                and stat.S_IMODE(path.stat().st_mode) != _PRIVATE_FILE_MODE
            )
        ):
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {
                "schema",
                "resolution",
                "authentication",
            }:
                return None
            mapping = raw["resolution"]
            if raw["schema"] != _SCHEMA or not isinstance(mapping, dict):
                return None
            supplied = raw["authentication"]
            if not isinstance(supplied, str) or not hmac.compare_digest(
                _authentication(mapping, key),
                supplied,
            ):
                return None
            _require_resolution_fields(mapping)
            return UnknownResolutionEvidence(
                intent_id=_string(mapping["intent_id"]),
                child_id=_string(mapping["child_id"]),
                resolution=UnknownDispatchResolution(_string(mapping["resolution"])),
                recorded_at=_time(mapping["recorded_at"]),
                checkpoint=_integer(mapping["checkpoint"]),
                lineage_etag=_optional_string(mapping.get("lineage_etag")),
                lineage_trace_id=_optional_string(mapping.get("lineage_trace_id")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _with_lock(
        self, operation: Callable[[], ApplyRecordRepositoryOutcome]
    ) -> ApplyRecordRepositoryOutcome:
        try:
            with self._lock:
                return operation()
        except Exception:  # noqa: BLE001
            return ApplyRecordRepositoryOutcome(ApplyRecordRepositoryStatus.FAILED)


def _encode(record: ApplyRecord, key: bytes) -> bytes:
    mapping = _record_mapping(record)
    envelope = {
        "schema": _SCHEMA,
        "record": mapping,
        "authentication": _authentication(mapping, key),
    }
    return (
        json.dumps(envelope, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _created_after(candidate: ApplyRecord, selected: ApplyRecord) -> bool:
    """Compare authenticated repository order and fail closed for legacy peers."""
    if selected.creation_sequence is None:
        return True
    if candidate.creation_sequence is None:
        return False
    return candidate.creation_sequence > selected.creation_sequence


def _encode_resolution(
    evidence: UnknownResolutionEvidence,
    key: bytes,
) -> bytes:
    mapping: dict[str, object] = {
        "intent_id": evidence.intent_id,
        "child_id": evidence.child_id,
        "resolution": evidence.resolution.value,
        "recorded_at": _format_time(evidence.recorded_at),
        "checkpoint": evidence.checkpoint,
        "lineage_etag": evidence.lineage_etag,
        "lineage_trace_id": evidence.lineage_trace_id,
    }
    envelope = {
        "schema": _SCHEMA,
        "resolution": mapping,
        "authentication": _authentication(mapping, key),
    }
    return (
        json.dumps(envelope, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _record_mapping(record: ApplyRecord) -> dict[str, object]:
    return {
        "intent_id": record.intent_id,
        "plan_digest": record.plan_digest,
        "kind": record.kind.value,
        "resource_scope": {
            "kind": record.resource_scope.kind.value,
            "canonical_name": record.resource_scope.canonical_name,
        },
        "created_at": _format_time(record.created_at),
        "creation_sequence": record.creation_sequence,
        "state": record.state.value,
        "finished_at": (
            None if record.finished_at is None else _format_time(record.finished_at)
        ),
        "revision": record.revision,
        "children": [
            {
                "child_id": child.child_id,
                "slice_identity": {
                    "resource_scope": {
                        "kind": child.slice_identity.resource_scope.kind.value,
                        "canonical_name": (
                            child.slice_identity.resource_scope.canonical_name
                        ),
                    },
                    "service": child.slice_identity.service,
                    "quota_id": child.slice_identity.quota_id,
                    "dimensions": [
                        list(item) for item in child.slice_identity.dimensions.items
                    ],
                    "quota_scope": child.slice_identity.quota_scope.value,
                },
                "target": {
                    "value": child.target.value,
                    "unit": child.target.unit.symbol,
                },
                "baseline": (
                    None
                    if child.baseline is None
                    else {
                        "value": child.baseline.value,
                        "unit": child.baseline.unit.symbol,
                    }
                ),
                "preference_identity": child.preference_identity,
                "etag": child.etag,
                "preference_existed": child.preference_existed,
                "dispatch_intent_at": (
                    None
                    if child.dispatch_intent_at is None
                    else _format_time(child.dispatch_intent_at)
                ),
                "disposition": (
                    None if child.disposition is None else child.disposition.value
                ),
                "provider_outcome": (
                    None
                    if child.provider_outcome is None
                    else child.provider_outcome.value
                ),
                "outcome_recorded_at": (
                    None
                    if child.outcome_recorded_at is None
                    else _format_time(child.outcome_recorded_at)
                ),
                "unknown_resolution": (
                    None
                    if child.unknown_resolution is None
                    else child.unknown_resolution.value
                ),
                "resolution_recorded_at": (
                    None
                    if child.resolution_recorded_at is None
                    else _format_time(child.resolution_recorded_at)
                ),
                "accepted_etag": child.accepted_etag,
                "accepted_trace_id": child.accepted_trace_id,
            }
            for child in record.children
        ],
    }


def _decode_record(raw: dict[str, object]) -> ApplyRecord:
    if set(raw) not in {_RECORD_FIELDS, _RECORD_FIELDS | _RECORD_SEQUENCE_FIELD}:
        msg = "Apply record fields do not match V1"
        raise ValueError(msg)
    scope_raw = _mapping(raw["resource_scope"])
    _require_exact_fields(scope_raw, _SCOPE_FIELDS)
    children_raw = raw["children"]
    if not isinstance(children_raw, list):
        msg = "Apply children must be a list"
        raise TypeError(msg)
    return ApplyRecord(
        intent_id=_string(raw["intent_id"]),
        plan_digest=_string(raw["plan_digest"]),
        kind=PlanKind(_string(raw["kind"])),
        resource_scope=_scope(scope_raw),
        created_at=_time(raw["created_at"]),
        creation_sequence=(
            None
            if raw.get("creation_sequence") is None
            else _integer(raw["creation_sequence"])
        ),
        children=tuple(_decode_child(_mapping(child)) for child in children_raw),
        state=ApplyRecordState(_string(raw["state"])),
        finished_at=_optional_time(raw["finished_at"]),
        revision=_integer(raw["revision"]),
    )


def _decode_child(raw: dict[str, object]) -> ApplyChildRecord:
    if set(raw) not in {
        _LEGACY_CHILD_FIELDS,
        _LEGACY_CHILD_FIELDS | _CHILD_BASELINE_FIELD,
        _LEGACY_CHILD_FIELDS | _CHILD_LINEAGE_FIELDS,
        _LEGACY_CHILD_FIELDS | _CHILD_LINEAGE_FIELDS | _CHILD_BASELINE_FIELD,
    }:
        msg = "Apply child fields do not match V1"
        raise ValueError(msg)
    slice_raw = _mapping(raw["slice_identity"])
    _require_exact_fields(slice_raw, _SLICE_FIELDS)
    _require_exact_fields(_mapping(slice_raw["resource_scope"]), _SCOPE_FIELDS)
    target_raw = _mapping(raw["target"])
    _require_exact_fields(target_raw, _QUANTITY_FIELDS)
    baseline_raw = raw.get("baseline")
    if baseline_raw is not None:
        _require_exact_fields(_mapping(baseline_raw), _QUANTITY_FIELDS)
    dimensions = slice_raw["dimensions"]
    if not isinstance(dimensions, list):
        msg = "Apply dimensions must be a list"
        raise TypeError(msg)
    if any(
        not isinstance(item, list) or len(item) != _DIMENSION_PAIR_SIZE
        for item in dimensions
    ):
        msg = "Apply dimensions must contain exact key-value pairs"
        raise ValueError(msg)
    return ApplyChildRecord(
        child_id=_string(raw["child_id"]),
        slice_identity=EffectiveQuotaSliceIdentity(
            _scope(_mapping(slice_raw["resource_scope"])),
            _string(slice_raw["service"]),
            _string(slice_raw["quota_id"]),
            NormalizedDimensions(
                (_string(item[0]), _string(item[1])) for item in dimensions
            ),
            QuotaScope(_string(slice_raw["quota_scope"])),
        ),
        target=QuotaQuantity(
            _integer(target_raw["value"]),
            QuotaUnit(_string(target_raw["unit"])),
        ),
        baseline=(
            None
            if baseline_raw is None
            else QuotaQuantity(
                _integer(_mapping(baseline_raw)["value"]),
                QuotaUnit(_string(_mapping(baseline_raw)["unit"])),
            )
        ),
        preference_identity=_string(raw["preference_identity"]),
        etag=_optional_string(raw["etag"]),
        preference_existed=_boolean(raw["preference_existed"]),
        dispatch_intent_at=_optional_time(raw["dispatch_intent_at"]),
        disposition=(
            None
            if raw["disposition"] is None
            else ApplyChildDisposition(_string(raw["disposition"]))
        ),
        provider_outcome=(
            None
            if raw["provider_outcome"] is None
            else StableSymbol(_string(raw["provider_outcome"]))
        ),
        outcome_recorded_at=_optional_time(raw["outcome_recorded_at"]),
        unknown_resolution=(
            None
            if raw["unknown_resolution"] is None
            else UnknownDispatchResolution(_string(raw["unknown_resolution"]))
        ),
        resolution_recorded_at=_optional_time(raw["resolution_recorded_at"]),
        accepted_etag=_optional_string(raw.get("accepted_etag")),
        accepted_trace_id=_optional_string(raw.get("accepted_trace_id")),
    )


def _scope(raw: dict[str, object]) -> ResourceScope:
    _require_exact_fields(raw, _SCOPE_FIELDS)
    return ResourceScope(
        ResourceScopeKind(_string(raw["kind"])),
        _string(raw["canonical_name"]),
    )


def _authentication(mapping: dict[str, object], key: bytes) -> str:
    data = json.dumps(
        mapping, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return f"hmac-sha256:{hmac.digest(key, data, 'sha256').hex()}"


def _key_bytes(value: SecretValue) -> bytes:
    if not isinstance(value, SecretValue):
        msg = "authentication_key must be a SecretValue"
        raise TypeError(msg)
    key = value.reveal()
    if len(key) < _MINIMUM_KEY_BYTES:
        msg = "authentication key must contain at least 32 bytes"
        raise ValueError(msg)
    return key


def _publish(path: Path, data: bytes, *, replace: bool) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        _PRIVATE_FILE_MODE,
    )
    try:
        temporary.chmod(_PRIVATE_FILE_MODE)
        restrict_windows_acl(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            temporary.replace(path)
        else:
            os.link(temporary, path)
            temporary.unlink()
        restrict_windows_acl(path)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: win32 cover
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _time(value: object) -> datetime:
    text = _string(value)
    if not text.endswith("Z"):
        msg = "Apply time must use canonical UTC"
        raise ValueError(msg)
    return datetime.fromisoformat(f"{text[:-1]}+00:00")


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        msg = "Apply field must be an object"
        raise TypeError(msg)
    return value


def _require_exact_fields(
    mapping: dict[str, object],
    fields: frozenset[str],
) -> None:
    if set(mapping) != fields:
        msg = "Apply object fields do not match V1"
        raise ValueError(msg)


def _require_resolution_fields(mapping: dict[str, object]) -> None:
    if set(mapping) not in {
        _LEGACY_RESOLUTION_FIELDS,
        _LEGACY_RESOLUTION_FIELDS | _RESOLUTION_LINEAGE_FIELDS,
    }:
        msg = "resolution fields do not match V1"
        raise ValueError(msg)


def _string(value: object) -> str:
    if not isinstance(value, str):
        msg = "Apply field must be a string"
        raise TypeError(msg)
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "Apply field must be an integer"
        raise TypeError(msg)
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        msg = "Apply field must be a boolean"
        raise TypeError(msg)
    return value
