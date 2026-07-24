"""Authenticated immutable persistence for durable Watch checkpoints."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import stat
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING

from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock
from cqmgr.adapters.persistence.windows_acl import restrict_windows_acl
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.application.ports.watch import (
    WatchCheckpointRepositoryOutcome,
    WatchCheckpointRepositoryStatus,
)
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
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
    WatchDisposition,
)
from cqmgr.domain.watch import (
    WatchAggregate,
    WatchCheckpoint,
    WatchChildIdentity,
    WatchChildLineage,
    WatchChildSummary,
    WatchSubject,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_SCHEMA = "cqmgr.watch-checkpoint/v1"
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_MINIMUM_KEY_BYTES = 32
_PAIR_SIZE = 2


class LocalWatchCheckpointRepository:
    """Create and authenticate immutable observation checkpoints."""

    def __init__(
        self,
        root: Path,
        *,
        lock: NativePlanInterprocessLock | None = None,
    ) -> None:
        """Create one owner-private store under an installation-local lock."""
        self._root = root / "watch-checkpoints"
        self._root.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
        self._root.chmod(_PRIVATE_DIRECTORY_MODE)
        restrict_windows_acl(self._root)
        self._lock = lock or NativePlanInterprocessLock(
            root / ".watch-checkpoint-repository.lock"
        )

    def save(
        self,
        checkpoint: WatchCheckpoint,
        authentication_key: SecretValue,
    ) -> WatchCheckpointRepositoryOutcome:
        """Create one immutable authenticated checkpoint idempotently."""
        try:
            key = _key_bytes(authentication_key)
            path = self._path(checkpoint.checkpoint_id)
        except (TypeError, ValueError):
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.FAILED
            )
        return self._with_lock(lambda: self._save_locked(path, checkpoint, key))

    def _save_locked(
        self,
        path: Path,
        checkpoint: WatchCheckpoint,
        key: bytes,
    ) -> WatchCheckpointRepositoryOutcome:
        if path.exists():
            try:
                existing = self._read(path, key)
            except OSError:
                return WatchCheckpointRepositoryOutcome(
                    WatchCheckpointRepositoryStatus.FAILED
                )
            if existing == checkpoint:
                return WatchCheckpointRepositoryOutcome(
                    WatchCheckpointRepositoryStatus.STORED,
                    checkpoint,
                )
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.CONFLICT
            )
        try:
            _publish(path, _encode(checkpoint, key))
        except OSError:
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.FAILED
            )
        return WatchCheckpointRepositoryOutcome(
            WatchCheckpointRepositoryStatus.STORED,
            checkpoint,
        )

    def load(
        self,
        checkpoint_id: str,
        authentication_key: SecretValue,
    ) -> WatchCheckpointRepositoryOutcome:
        """Load and authenticate one exact immutable checkpoint."""
        try:
            key = _key_bytes(authentication_key)
            path = self._path(checkpoint_id)
        except (TypeError, ValueError):
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.FAILED
            )
        return self._with_lock(lambda: self._load_locked(path, key))

    def _load_locked(
        self,
        path: Path,
        key: bytes,
    ) -> WatchCheckpointRepositoryOutcome:
        if not path.exists():
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.MISSING
            )
        try:
            checkpoint = self._read(path, key)
        except OSError:
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.FAILED
            )
        if checkpoint is None:
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.CONFLICT
            )
        return WatchCheckpointRepositoryOutcome(
            WatchCheckpointRepositoryStatus.AVAILABLE,
            checkpoint,
        )

    def _read(self, path: Path, key: bytes) -> WatchCheckpoint | None:
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
        try:
            _require_keys(raw, {"schema", "checkpoint", "authentication"})
            mapping = _mapping(raw["checkpoint"])
            if raw["schema"] != _SCHEMA:
                return None
            supplied = _string(raw["authentication"])
            if not hmac.compare_digest(_authentication(mapping, key), supplied):
                return None
            return _decode_checkpoint(mapping)
        except (KeyError, TypeError, ValueError):
            return None

    def _path(self, checkpoint_id: str) -> Path:
        match = _DIGEST.fullmatch(checkpoint_id)
        if match is None:
            msg = "Watch checkpoint identity must be canonical sha256"
            raise ValueError(msg)
        return self._root / f"{match.group(1)}.json"

    def _with_lock(
        self,
        operation: Callable[[], WatchCheckpointRepositoryOutcome],
    ) -> WatchCheckpointRepositoryOutcome:
        try:
            with self._lock:
                return operation()
        except Exception:  # noqa: BLE001
            return WatchCheckpointRepositoryOutcome(
                WatchCheckpointRepositoryStatus.FAILED
            )


def _encode(checkpoint: WatchCheckpoint, key: bytes) -> bytes:
    mapping = _checkpoint_mapping(checkpoint)
    envelope = {
        "schema": _SCHEMA,
        "checkpoint": mapping,
        "authentication": _authentication(mapping, key),
    }
    return (
        json.dumps(envelope, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _checkpoint_mapping(checkpoint: WatchCheckpoint) -> dict[str, object]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "installation_id": checkpoint.installation_id,
        "subject": _subject_mapping(checkpoint.subject),
        "aggregate": {
            "disposition": checkpoint.aggregate.disposition.value,
            "children": [
                {
                    "child_id": summary.child.child_id,
                    "status": (
                        None
                        if summary.status is None
                        else _status_mapping(summary.status)
                    ),
                }
                for summary in checkpoint.aggregate.children
            ],
        },
        "lineages": [
            {
                "child_id": lineage.child_id,
                "etag": lineage.etag,
                "trace_id": lineage.trace_id,
            }
            for lineage in checkpoint.lineages
        ],
        "sequence": checkpoint.sequence,
        "saved_at": _format_time(checkpoint.saved_at),
    }


def _subject_mapping(subject: WatchSubject) -> dict[str, object]:
    return {
        "kind": subject.kind.value,
        "resource_scope": {
            "kind": subject.resource_scope.kind.value,
            "canonical_name": subject.resource_scope.canonical_name,
        },
        "condition": subject.condition.value,
        "intent_id": subject.intent_id,
        "plan_digest": subject.plan_digest,
        "resolution_checkpoint": subject.resolution_checkpoint,
        "children": [
            {
                "child_id": child.child_id,
                "order": child.order,
                "slice_identity": {
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
                "baseline": _quantity_mapping(child.baseline),
                "disposition": child.disposition.value,
                "preference_identity": child.preference_identity,
                "lineage_etag": child.lineage_etag,
                "lineage_trace_id": child.lineage_trace_id,
                "unknown_resolution": (
                    None
                    if child.unknown_resolution is None
                    else child.unknown_resolution.value
                ),
                "resolution_checkpoint": child.resolution_checkpoint,
            }
            for child in subject.children
        ],
    }


def _status_mapping(status: QuotaRequestStatus) -> dict[str, object]:
    return {
        "reconciliation": status.reconciliation.value,
        "provider_reconciliation": (
            None
            if status.provider_reconciliation is None
            else status.provider_reconciliation.raw
        ),
        "baseline": _quantity_mapping(status.baseline),
        "desired": _quantity_mapping(status.desired),
        "granted": _quantity_mapping(status.granted),
        "effective": _quantity_mapping(status.effective),
        "status_observed_at": _format_time(status.status_observed_at),
        "effective_observed_at": (
            None
            if status.effective_observed_at is None
            else _format_time(status.effective_observed_at)
        ),
    }


def _quantity_mapping(quantity: QuotaQuantity | None) -> dict[str, object] | None:
    if quantity is None:
        return None
    return {"value": quantity.value, "unit": quantity.unit.symbol}


def _decode_checkpoint(raw: dict[str, object]) -> WatchCheckpoint:
    _require_keys(
        raw,
        {
            "checkpoint_id",
            "installation_id",
            "subject",
            "aggregate",
            "lineages",
            "sequence",
            "saved_at",
        },
    )
    subject = _decode_subject(_mapping(raw["subject"]))
    aggregate_raw = _mapping(raw["aggregate"])
    _require_keys(aggregate_raw, {"disposition", "children"})
    summaries_raw = _list(aggregate_raw["children"])
    if len(summaries_raw) != len(subject.children):
        msg = "Watch checkpoint summaries must match subject length"
        raise ValueError(msg)
    summaries: list[WatchChildSummary] = []
    for child, value in zip(subject.children, summaries_raw, strict=True):
        summary_raw = _mapping(value)
        _require_keys(summary_raw, {"child_id", "status"})
        if _string(summary_raw["child_id"]) != child.child_id:
            msg = "Watch checkpoint summary child order changed"
            raise ValueError(msg)
        summaries.append(
            WatchChildSummary(
                child,
                None
                if summary_raw["status"] is None
                else _decode_status(_mapping(summary_raw["status"])),
            )
        )
    aggregate = WatchAggregate.derive(subject, tuple(summaries))
    if aggregate.disposition is not WatchDisposition(
        _string(aggregate_raw["disposition"])
    ):
        msg = "Watch checkpoint aggregate disposition is not derived"
        raise ValueError(msg)
    lineages = tuple(
        _decode_lineage(_mapping(value)) for value in _list(raw["lineages"])
    )
    return WatchCheckpoint(
        checkpoint_id=_string(raw["checkpoint_id"]),
        installation_id=_string(raw["installation_id"]),
        subject=subject,
        aggregate=aggregate,
        lineages=lineages,
        sequence=_integer(raw["sequence"]),
        saved_at=_time(raw["saved_at"]),
    )


def _decode_subject(raw: dict[str, object]) -> WatchSubject:
    _require_keys(
        raw,
        {
            "kind",
            "resource_scope",
            "condition",
            "intent_id",
            "plan_digest",
            "resolution_checkpoint",
            "children",
        },
    )
    scope_raw = _mapping(raw["resource_scope"])
    _require_keys(scope_raw, {"kind", "canonical_name"})
    scope = ResourceScope(
        ResourceScopeKind(_string(scope_raw["kind"])),
        _string(scope_raw["canonical_name"]),
    )
    children = tuple(
        _decode_child(_mapping(value), scope) for value in _list(raw["children"])
    )
    return WatchSubject(
        kind=PlanKind(_string(raw["kind"])),
        resource_scope=scope,
        condition=WatchCondition(_string(raw["condition"])),
        intent_id=_string(raw["intent_id"]),
        plan_digest=_string(raw["plan_digest"]),
        children=children,
        resolution_checkpoint=_integer(raw["resolution_checkpoint"]),
    )


def _decode_child(
    raw: dict[str, object],
    scope: ResourceScope,
) -> WatchChildIdentity:
    _require_keys(
        raw,
        {
            "child_id",
            "order",
            "slice_identity",
            "target",
            "baseline",
            "disposition",
            "preference_identity",
            "lineage_etag",
            "lineage_trace_id",
            "unknown_resolution",
            "resolution_checkpoint",
        },
    )
    slice_raw = _mapping(raw["slice_identity"])
    _require_keys(
        slice_raw,
        {"service", "quota_id", "dimensions", "quota_scope"},
    )
    pairs = _list(slice_raw["dimensions"])
    dimensions: list[tuple[str, str]] = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != _PAIR_SIZE:
            msg = "Watch checkpoint dimensions must be key-value pairs"
            raise ValueError(msg)
        dimensions.append((_string(pair[0]), _string(pair[1])))
    target_raw = _mapping(raw["target"])
    _require_keys(target_raw, {"value", "unit"})
    baseline_raw = raw["baseline"]
    return WatchChildIdentity(
        child_id=_string(raw["child_id"]),
        order=_integer(raw["order"]),
        slice_identity=EffectiveQuotaSliceIdentity(
            scope,
            _string(slice_raw["service"]),
            _string(slice_raw["quota_id"]),
            NormalizedDimensions(dimensions),
            QuotaScope(_string(slice_raw["quota_scope"])),
        ),
        target=QuotaQuantity(
            _integer(target_raw["value"]),
            QuotaUnit(_string(target_raw["unit"])),
        ),
        baseline=_optional_quantity(baseline_raw),
        disposition=ApplyChildDisposition(_string(raw["disposition"])),
        preference_identity=_string(raw["preference_identity"]),
        lineage_etag=_optional_string(raw["lineage_etag"]),
        lineage_trace_id=_optional_string(raw["lineage_trace_id"]),
        unknown_resolution=(
            None
            if raw["unknown_resolution"] is None
            else UnknownDispatchResolution(_string(raw["unknown_resolution"]))
        ),
        resolution_checkpoint=_integer(raw["resolution_checkpoint"]),
    )


def _decode_status(raw: dict[str, object]) -> QuotaRequestStatus:
    _require_keys(
        raw,
        {
            "reconciliation",
            "provider_reconciliation",
            "baseline",
            "desired",
            "granted",
            "effective",
            "status_observed_at",
            "effective_observed_at",
        },
    )
    reconciliation_raw = raw["provider_reconciliation"]
    reconciliation: Reconciliation | ProviderSymbol[Reconciliation]
    if reconciliation_raw is None:
        reconciliation = Reconciliation(_string(raw["reconciliation"]))
    else:
        reconciliation = ProviderSymbol(_string(reconciliation_raw), Reconciliation)
    return QuotaRequestStatus.derive(
        reconciliation=reconciliation,
        baseline=_optional_quantity(raw["baseline"]),
        desired=_quantity(raw["desired"]),
        granted=_optional_quantity(raw["granted"]),
        effective=_optional_quantity(raw["effective"]),
        status_observed_at=_time(raw["status_observed_at"]),
        effective_observed_at=(
            None
            if raw["effective_observed_at"] is None
            else _time(raw["effective_observed_at"])
        ),
    )


def _decode_lineage(raw: dict[str, object]) -> WatchChildLineage:
    _require_keys(raw, {"child_id", "etag", "trace_id"})
    return WatchChildLineage(
        _string(raw["child_id"]),
        _optional_string(raw["etag"]),
        _optional_string(raw["trace_id"]),
    )


def _quantity(value: object) -> QuotaQuantity:
    raw = _mapping(value)
    _require_keys(raw, {"value", "unit"})
    return QuotaQuantity(
        _integer(raw["value"]),
        QuotaUnit(_string(raw["unit"])),
    )


def _optional_quantity(value: object) -> QuotaQuantity | None:
    return None if value is None else _quantity(value)


def _authentication(mapping: dict[str, object], key: bytes) -> str:
    payload = json.dumps(
        mapping, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return f"hmac-sha256:{hmac.digest(key, payload, 'sha256').hex()}"


def _key_bytes(value: SecretValue) -> bytes:
    if not isinstance(value, SecretValue):
        msg = "authentication_key must be a SecretValue"
        raise TypeError(msg)
    key = value.reveal()
    if len(key) < _MINIMUM_KEY_BYTES:
        msg = "authentication key must contain at least 32 bytes"
        raise ValueError(msg)
    return key


def _publish(path: Path, data: bytes) -> None:
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
        msg = "Watch checkpoint time must use canonical UTC"
        raise ValueError(msg)
    return datetime.fromisoformat(f"{text[:-1]}+00:00")


def _require_keys(value: object, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        msg = "Watch checkpoint object fields do not match the schema"
        raise ValueError(msg)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        msg = "Watch checkpoint field must be an object"
        raise TypeError(msg)
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        msg = "Watch checkpoint field must be a list"
        raise TypeError(msg)
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        msg = "Watch checkpoint field must be a string"
        raise TypeError(msg)
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "Watch checkpoint field must be an integer"
        raise TypeError(msg)
    return value
