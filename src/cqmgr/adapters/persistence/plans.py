"""Crash-safe content-addressed local quota request plan repository."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock
from cqmgr.adapters.serialization.plans import PlanCodec, PlanDecodeError
from cqmgr.application.ports.plans import (
    EncodedPlan,
    PlanLease,
    PlanRepositoryOutcome,
    PlanRepositoryStatus,
)
from cqmgr.domain.plan_consumption import (
    PlanLedgerDecision,
    PlanLedgerRecord,
)
from cqmgr.domain.plans import PlanLedgerState
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cqmgr.application.ports.secrets import SecretValue

_STATE_SCHEMA = "cqmgr.plan-state/v1"
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700


_LedgerRecord = PlanLedgerRecord


class LocalPlanRepository:
    """Filesystem repository with one serialized durable consumption ledger."""

    def __init__(
        self,
        root: Path,
        *,
        owner_alive: Callable[[int], bool] | None = None,
    ) -> None:
        """Create private storage directories without reading plan contents."""
        self._root = root
        self._plans = root / "plans"
        self._states = root / "state"
        self._owner_alive = owner_alive or _owner_alive
        for directory in (self._root, self._plans, self._states):
            directory.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
            directory.chmod(_PRIVATE_DIRECTORY_MODE)
        self._lock = NativePlanInterprocessLock(root / ".plan-repository.lock")

    def store(  # noqa: PLR0911
        self, plan: EncodedPlan, authentication_key: SecretValue
    ) -> PlanRepositoryOutcome:
        """Store exact canonical bytes by their verified digest."""
        try:
            decoded = PlanCodec.decode(plan.bytes)
            digest_hex = _digest_hex(plan.digest)
        except (PlanDecodeError, TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        if decoded.digest != plan.digest:
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        if not decoded.authenticate(authentication_key.reveal()):
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        with self._lock:
            plan_path = self._plans / f"{digest_hex}.plan"
            state_path = self._states / f"{digest_hex}.json"
            if plan_path.exists():
                try:
                    if plan_path.read_bytes() != plan.bytes:
                        return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
                except OSError:
                    return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            else:
                try:
                    _atomic_write(plan_path, plan.bytes, _PRIVATE_FILE_MODE)
                except OSError:
                    return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            if not state_path.exists():
                try:
                    self._write_record(digest_hex, _LedgerRecord.available())
                except OSError:
                    return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            return PlanRepositoryOutcome(
                PlanRepositoryStatus.STORED,
                state=PlanLedgerState.AVAILABLE,
            )

    def load(self, digest: str, now: datetime) -> PlanRepositoryOutcome:
        """Load trustworthy local bytes and recover an abandoned state window."""
        require_utc(now, "now")
        try:
            digest_hex = _digest_hex(digest)
        except ValueError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        with self._lock:
            plan_path = self._plans / f"{digest_hex}.plan"
            if not plan_path.is_file():
                return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
            try:
                if stat.S_IMODE(plan_path.stat().st_mode) != _PRIVATE_FILE_MODE:
                    return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
                plan_bytes = plan_path.read_bytes()
                decoded = PlanCodec.decode(plan_bytes)
                if decoded.digest != digest:
                    return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
                record = self._recover_record(digest_hex, now)
            except (OSError, PlanDecodeError, ValueError):
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            return _outcome_for_record(record, plan_bytes=plan_bytes)

    def export(  # noqa: PLR0911
        self, plan: EncodedPlan, path: Path
    ) -> PlanRepositoryOutcome:
        """Atomically write exact portable bytes to one explicit private path."""
        try:
            decoded = PlanCodec.decode(plan.bytes)
        except (PlanDecodeError, TypeError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        if decoded.digest != plan.digest:
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        path.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
        if path.exists():
            try:
                if path.is_file() and path.read_bytes() == plan.bytes:
                    path.chmod(_PRIVATE_FILE_MODE)
                    return PlanRepositoryOutcome(PlanRepositoryStatus.EXPORTED)
            except OSError:
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        try:
            _atomic_write(path, plan.bytes, _PRIVATE_FILE_MODE)
        except OSError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        return PlanRepositoryOutcome(PlanRepositoryStatus.EXPORTED)

    def read_export(self, path: Path) -> PlanRepositoryOutcome:
        """Read and validate an explicit exported plan for safe review."""
        try:
            if not path.is_file():
                return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
            plan_bytes = path.read_bytes()
            PlanCodec.decode(plan_bytes)
        except (OSError, PlanDecodeError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.EXPORTED, plan_bytes=plan_bytes
        )

    def acquire_lease(  # noqa: PLR0911
        self,
        digest: str,
        now: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=1),
    ) -> PlanRepositoryOutcome:
        """Acquire the one exclusive pre-dispatch lease for an available plan."""
        require_utc(now, "now")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta():
            msg = "lease_duration must be a positive timedelta"
            raise ValueError(msg)
        try:
            digest_hex = _digest_hex(digest)
        except ValueError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        with self._lock:
            if not (self._plans / f"{digest_hex}.plan").is_file():
                return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
            try:
                record = self._recover_record(digest_hex, now)
            except (OSError, ValueError):
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            lease = PlanLease(
                digest=digest,
                token=secrets.token_urlsafe(24),
                expires_at=now + lease_duration,
            )
            transition = record.acquire(
                token=lease.token,
                expires_at=lease.expires_at,
                owner_pid=os.getpid(),
            )
            if transition.decision is PlanLedgerDecision.CONFLICT:
                if record.state in {
                    PlanLedgerState.LEASED,
                    PlanLedgerState.DISPATCHED,
                }:
                    return PlanRepositoryOutcome(
                        PlanRepositoryStatus.CONFLICT,
                        state=record.state,
                        reason=record.reason,
                    )
                return _unavailable_for_record(record)
            try:
                self._write_record(digest_hex, transition.record)
            except OSError:
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            return PlanRepositoryOutcome(
                PlanRepositoryStatus.LEASED,
                state=PlanLedgerState.LEASED,
                lease=lease,
            )

    def mark_dispatched(self, lease: PlanLease, now: datetime) -> PlanRepositoryOutcome:
        """Durably consume a valid lease immediately before provider dispatch."""
        require_utc(now, "now")
        return self._transition_with_lease(
            lease,
            now,
            required=PlanLedgerState.LEASED,
            target=PlanLedgerState.DISPATCHED,
            status=PlanRepositoryStatus.DISPATCHED,
        )

    def complete(self, lease: PlanLease, now: datetime) -> PlanRepositoryOutcome:
        """Record that the dispatched plan has one durable terminal outcome."""
        require_utc(now, "now")
        return self._transition_with_lease(
            lease,
            now,
            required=PlanLedgerState.DISPATCHED,
            target=PlanLedgerState.CONSUMED,
            status=PlanRepositoryStatus.CONSUMED,
        )

    def quarantine(
        self, lease: PlanLease, reason: StableSymbol, now: datetime
    ) -> PlanRepositoryOutcome:
        """Make an interrupted or ambiguous dispatch permanently inapplicable."""
        require_utc(now, "now")
        if not isinstance(reason, StableSymbol):
            msg = "quarantine reason must be a StableSymbol"
            raise TypeError(msg)
        try:
            digest_hex = _digest_hex(lease.digest)
        except ValueError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        with self._lock:
            try:
                record = self._read_record(digest_hex)
            except (OSError, ValueError):
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            transition = record.quarantine(token=lease.token, reason=reason)
            if transition.decision is PlanLedgerDecision.CONFLICT:
                return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
            try:
                self._write_record(digest_hex, transition.record)
            except OSError:
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            return _outcome_for_record(transition.record)

    def _transition_with_lease(  # noqa: PLR0911
        self,
        lease: PlanLease,
        now: datetime,
        *,
        required: PlanLedgerState,
        target: PlanLedgerState,
        status: PlanRepositoryStatus,
    ) -> PlanRepositoryOutcome:
        try:
            digest_hex = _digest_hex(lease.digest)
        except ValueError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        with self._lock:
            try:
                record = self._read_record(digest_hex)
            except (OSError, ValueError):
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            transition = (
                record.dispatch(token=lease.token, now=now)
                if required is PlanLedgerState.LEASED
                else record.complete(token=lease.token)
            )
            if transition.decision in {
                PlanLedgerDecision.ACCEPTED,
                PlanLedgerDecision.EXPIRED,
            }:
                try:
                    self._write_record(digest_hex, transition.record)
                except OSError:
                    return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            if transition.decision is PlanLedgerDecision.EXPIRED:
                return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
            if transition.decision in {
                PlanLedgerDecision.ACCEPTED,
                PlanLedgerDecision.IDEMPOTENT,
            }:
                return PlanRepositoryOutcome(status, state=target)
            if transition.decision is PlanLedgerDecision.CONFLICT:
                return PlanRepositoryOutcome(
                    PlanRepositoryStatus.CONFLICT,
                    state=record.state,
                    reason=record.reason,
                )
            return _unavailable_for_record(record)

    def _recover_record(self, digest_hex: str, now: datetime) -> _LedgerRecord:
        state_path = self._states / f"{digest_hex}.json"
        if not state_path.exists():
            record = _LedgerRecord.available()
            self._write_record(digest_hex, record)
            return record
        record = self._read_record(digest_hex)
        transition = record.recover(now, self._owner_alive)
        if transition.decision in {
            PlanLedgerDecision.ACCEPTED,
            PlanLedgerDecision.EXPIRED,
        }:
            self._write_record(digest_hex, transition.record)
        return transition.record

    def _read_record(self, digest_hex: str) -> _LedgerRecord:
        raw = json.loads((self._states / f"{digest_hex}.json").read_text())
        expected = {
            "lease_expires_at",
            "lease_token",
            "owner_pid",
            "reason",
            "schema",
            "state",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            msg = "plan ledger record has unsupported fields"
            raise ValueError(msg)
        if raw["schema"] != _STATE_SCHEMA:
            msg = "plan ledger record has unsupported schema"
            raise ValueError(msg)
        token = raw["lease_token"]
        owner_pid = raw["owner_pid"]
        if token is not None and not isinstance(token, str):
            msg = "plan ledger lease token must be a string"
            raise ValueError(msg)
        if owner_pid is not None and (
            isinstance(owner_pid, bool) or not isinstance(owner_pid, int)
        ):
            msg = "plan ledger owner pid must be an integer"
            raise ValueError(msg)
        return _LedgerRecord(
            state=PlanLedgerState(raw["state"]),
            lease_token=token,
            lease_expires_at=_parse_optional_time(raw["lease_expires_at"]),
            owner_pid=owner_pid,
            reason=(StableSymbol(raw["reason"]) if raw["reason"] is not None else None),
        )

    def _write_record(self, digest_hex: str, record: _LedgerRecord) -> None:
        raw = {
            "lease_expires_at": (
                _format_time(record.lease_expires_at)
                if record.lease_expires_at is not None
                else None
            ),
            "lease_token": record.lease_token,
            "owner_pid": record.owner_pid,
            "reason": record.reason.value if record.reason is not None else None,
            "schema": _STATE_SCHEMA,
            "state": record.state.value,
        }
        data = (
            json.dumps(raw, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        _atomic_write(self._states / f"{digest_hex}.json", data, _PRIVATE_FILE_MODE)


def _digest_hex(digest: str) -> str:
    if not isinstance(digest, str):
        msg = "plan digest must be a string"
        raise TypeError(msg)
    match = _DIGEST.fullmatch(digest)
    if match is None:
        msg = "plan digest must be canonical sha256"
        raise ValueError(msg)
    return match.group(1)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(mode)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


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


def _parse_optional_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        msg = "plan ledger time must be canonical UTC"
        raise ValueError(msg)
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    if parsed.tzinfo != UTC:
        msg = "plan ledger time must be UTC"
        raise ValueError(msg)
    return parsed


def _owner_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _status_for_state(state: PlanLedgerState) -> PlanRepositoryStatus:
    return PlanRepositoryStatus(state.value)


def _outcome_for_record(
    record: _LedgerRecord, *, plan_bytes: bytes | None = None
) -> PlanRepositoryOutcome:
    return PlanRepositoryOutcome(
        _status_for_state(record.state),
        plan_bytes=plan_bytes,
        state=record.state,
        reason=record.reason,
    )


def _unavailable_for_record(record: _LedgerRecord) -> PlanRepositoryOutcome:
    if record.state is PlanLedgerState.AVAILABLE:
        status = PlanRepositoryStatus.CONFLICT
    else:
        status = _status_for_state(record.state)
    return PlanRepositoryOutcome(status, state=record.state, reason=record.reason)
