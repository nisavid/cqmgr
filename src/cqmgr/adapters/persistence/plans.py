"""Crash-safe content-addressed local quota request plan repository."""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
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
from cqmgr.application.ports.secrets import (
    ConsumptionMarkerStore,
    SecretPurpose,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
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

_STATE_SCHEMA = "cqmgr.plan-state/v1"
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
_AUTHENTICATION = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")
_MINIMUM_AUTHENTICATION_KEY_BYTES = 32
_CONSUMPTION_MARKER_SCHEMA = b"cqmgr.plan-consumption/v1:"
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700


_LedgerRecord = PlanLedgerRecord


class LocalPlanRepository:
    """Filesystem repository with one serialized durable consumption ledger."""

    def __init__(
        self,
        root: Path,
        consumption_store: ConsumptionMarkerStore,
        *,
        lock: NativePlanInterprocessLock | None = None,
    ) -> None:
        """Create private storage directories without reading plan contents."""
        self._root = root
        self._plans = root / "plans"
        self._states = root / "state"
        self._consumption_store = consumption_store
        for directory in (self._root, self._plans, self._states):
            _ensure_private_directory(directory)
        self._lock = lock or NativePlanInterprocessLock(root / ".plan-repository.lock")

    def store(
        self, plan: EncodedPlan, authentication_key: SecretValue
    ) -> PlanRepositoryOutcome:
        """Store exact canonical bytes by their verified digest."""
        try:
            key = _key_bytes(authentication_key)
            decoded = PlanCodec.decode(plan.bytes)
            digest_hex = _digest_hex(plan.digest)
            authenticated = decoded.authenticate(key)
        except (PlanDecodeError, TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        if decoded.digest != plan.digest:
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        if not authenticated:
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        return self._with_lock(
            lambda: self._store_locked(
                plan,
                digest_hex,
                decoded.plan.installation_id,
                key,
            )
        )

    def load(
        self,
        digest: str,
        authentication_key: SecretValue,
        now: datetime,
    ) -> PlanRepositoryOutcome:
        """Load trustworthy local bytes and recover an abandoned state window."""
        require_utc(now, "now")
        try:
            key = _key_bytes(authentication_key)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        try:
            digest_hex = _digest_hex(digest)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        return self._with_lock(lambda: self._load_locked(digest, digest_hex, key, now))

    def export(self, plan: EncodedPlan, path: Path) -> PlanRepositoryOutcome:
        """Atomically write exact portable bytes to one explicit private path."""
        try:
            decoded = PlanCodec.decode(plan.bytes)
        except (PlanDecodeError, TypeError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        if decoded.digest != plan.digest:
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        return self._with_lock(lambda: self._export_locked(plan, path))

    def read_export(self, path: Path) -> PlanRepositoryOutcome:
        """Read and validate an explicit exported plan for safe review."""
        return self._with_lock(lambda: self._read_export_locked(path))

    def acquire_lease(
        self,
        digest: str,
        authentication_key: SecretValue,
        now: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=1),
    ) -> PlanRepositoryOutcome:
        """Authenticate and exclusively lease one available local plan."""
        require_utc(now, "now")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta():
            msg = "lease_duration must be a positive timedelta"
            raise ValueError(msg)
        try:
            key = _key_bytes(authentication_key)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        try:
            digest_hex = _digest_hex(digest)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        return self._with_lock(
            lambda: self._acquire_lease_locked(
                digest,
                digest_hex,
                key,
                now,
                lease_duration,
            )
        )

    def _store_locked(  # noqa: PLR0911
        self,
        plan: EncodedPlan,
        digest_hex: str,
        installation_id: str,
        key: bytes,
    ) -> PlanRepositoryOutcome:
        plan_path = self._plans / f"{digest_hex}.plan"
        state_path = self._states / f"{digest_hex}.json"
        try:
            if plan_path.exists() and plan_path.read_bytes() != plan.bytes:
                return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
            if plan_path.exists() and not state_path.exists():
                record = _LedgerRecord.quarantined(
                    StableSymbol("missing-consumption-ledger")
                )
                self._write_record(digest_hex, record, key)
                return _outcome_for_record(record)
            if state_path.exists():
                record = self._read_record(digest_hex, key)
            else:
                record = _LedgerRecord.available()
                self._write_record(digest_hex, record, key)
            marker_status = self._read_consumption_marker(
                digest_hex,
                installation_id,
                key,
            )
            if marker_status is SecretStoreStatus.AVAILABLE:
                if record.state in {
                    PlanLedgerState.DISPATCHED,
                    PlanLedgerState.CONSUMED,
                    PlanLedgerState.QUARANTINED,
                }:
                    return _outcome_for_record(record, authenticated=True)
                return PlanRepositoryOutcome(
                    PlanRepositoryStatus.QUARANTINED,
                    state=PlanLedgerState.QUARANTINED,
                    reason=StableSymbol("consumption-marker-exists"),
                    authenticated=True,
                )
            if marker_status is not SecretStoreStatus.MISSING:
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
            if not plan_path.exists():
                _atomic_write(plan_path, plan.bytes, _PRIVATE_FILE_MODE)
        except (OSError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        if record.state is PlanLedgerState.AVAILABLE:
            return PlanRepositoryOutcome(
                PlanRepositoryStatus.STORED,
                state=PlanLedgerState.AVAILABLE,
            )
        return _outcome_for_record(record)

    def _load_locked(  # noqa: C901, PLR0911
        self, digest: str, digest_hex: str, key: bytes, now: datetime
    ) -> PlanRepositoryOutcome:
        plan_path = self._plans / f"{digest_hex}.plan"
        state_path = self._states / f"{digest_hex}.json"
        if not plan_path.is_file() and not state_path.is_file():
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        try:
            record = self._recover_record(digest_hex, key, now)
            if record.state in {
                PlanLedgerState.DISPATCHED,
                PlanLedgerState.CONSUMED,
                PlanLedgerState.QUARANTINED,
            }:
                try:
                    plan_bytes, decoded = self._read_local_plan(digest, digest_hex)
                except (OSError, PlanDecodeError, ValueError):
                    return _outcome_for_record(record)
                if not decoded.authenticate(key):
                    return PlanRepositoryOutcome(
                        PlanRepositoryStatus.CONFLICT,
                        state=record.state,
                        authenticated=False,
                    )
                return _outcome_for_record(
                    record,
                    plan_bytes=plan_bytes,
                    authenticated=True,
                )
            if not plan_path.is_file():
                return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
            plan_bytes, decoded = self._read_local_plan(digest, digest_hex)
            if not decoded.authenticate(key):
                return PlanRepositoryOutcome(
                    PlanRepositoryStatus.CONFLICT,
                    state=record.state,
                    authenticated=False,
                )
            record = self._recover_record(digest_hex, key, now)
            marker_status = self._read_consumption_marker(
                digest_hex,
                decoded.plan.installation_id,
                key,
            )
            if marker_status is SecretStoreStatus.AVAILABLE:
                if record.state in {
                    PlanLedgerState.DISPATCHED,
                    PlanLedgerState.CONSUMED,
                    PlanLedgerState.QUARANTINED,
                }:
                    return _outcome_for_record(record, authenticated=True)
                return PlanRepositoryOutcome(
                    PlanRepositoryStatus.QUARANTINED,
                    state=PlanLedgerState.QUARANTINED,
                    reason=StableSymbol("consumption-marker-exists"),
                    authenticated=True,
                )
            if marker_status is not SecretStoreStatus.MISSING:
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        except (OSError, PlanDecodeError, TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        return _outcome_for_record(
            record,
            plan_bytes=plan_bytes,
            authenticated=True,
        )

    def _export_locked(self, plan: EncodedPlan, path: Path) -> PlanRepositoryOutcome:
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
            if path.exists():
                if path.is_file() and path.read_bytes() == plan.bytes:
                    path.chmod(_PRIVATE_FILE_MODE)
                    return PlanRepositoryOutcome(PlanRepositoryStatus.EXPORTED)
                return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
            try:
                _atomic_publish_no_replace(path, plan.bytes, _PRIVATE_FILE_MODE)
            except FileExistsError:
                if path.is_file() and path.read_bytes() == plan.bytes:
                    return PlanRepositoryOutcome(PlanRepositoryStatus.EXPORTED)
                return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        except OSError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        return PlanRepositoryOutcome(PlanRepositoryStatus.EXPORTED)

    def _read_export_locked(self, path: Path) -> PlanRepositoryOutcome:
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

    def _acquire_lease_locked(  # noqa: C901, PLR0911
        self,
        digest: str,
        digest_hex: str,
        key: bytes,
        now: datetime,
        lease_duration: timedelta,
    ) -> PlanRepositoryOutcome:
        if not (self._plans / f"{digest_hex}.plan").is_file():
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        try:
            _plan_bytes, decoded = self._read_local_plan(digest, digest_hex)
            if not decoded.authenticate(key):
                return PlanRepositoryOutcome(
                    PlanRepositoryStatus.CONFLICT,
                    authenticated=False,
                )
            record = self._recover_record(digest_hex, key, now)
            marker_status = self._read_consumption_marker(
                digest_hex,
                decoded.plan.installation_id,
                key,
            )
            if marker_status is SecretStoreStatus.AVAILABLE:
                if record.state in {
                    PlanLedgerState.DISPATCHED,
                    PlanLedgerState.CONSUMED,
                    PlanLedgerState.QUARANTINED,
                }:
                    return _outcome_for_record(record, authenticated=True)
                return PlanRepositoryOutcome(
                    PlanRepositoryStatus.QUARANTINED,
                    state=PlanLedgerState.QUARANTINED,
                    reason=StableSymbol("consumption-marker-exists"),
                    authenticated=True,
                )
            if marker_status is not SecretStoreStatus.MISSING:
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        except (OSError, PlanDecodeError, TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        if decoded.plan.is_expired(now):
            return PlanRepositoryOutcome(
                PlanRepositoryStatus.EXPIRED,
                state=record.state,
                reason=StableSymbol("plan-expired"),
            )
        lease = PlanLease(
            digest=digest,
            token=secrets.token_urlsafe(24),
            expires_at=min(now + lease_duration, decoded.plan.expires_at),
        )
        transition = record.acquire(
            token=lease.token,
            expires_at=lease.expires_at,
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
            self._write_record(digest_hex, transition.record, key)
        except OSError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.LEASED,
            state=PlanLedgerState.LEASED,
            lease=lease,
            authenticated=True,
        )

    def mark_dispatched(
        self,
        lease: PlanLease,
        authentication_key: SecretValue,
        now: datetime,
    ) -> PlanRepositoryOutcome:
        """Durably consume a valid lease immediately before provider dispatch."""
        require_utc(now, "now")
        return self._transition_with_lease(
            lease,
            authentication_key,
            now,
            required=PlanLedgerState.LEASED,
            target=PlanLedgerState.DISPATCHED,
            status=PlanRepositoryStatus.DISPATCHED,
        )

    def complete(
        self,
        lease: PlanLease,
        authentication_key: SecretValue,
        now: datetime,
    ) -> PlanRepositoryOutcome:
        """Record that the dispatched plan has one durable terminal outcome."""
        require_utc(now, "now")
        return self._transition_with_lease(
            lease,
            authentication_key,
            now,
            required=PlanLedgerState.DISPATCHED,
            target=PlanLedgerState.CONSUMED,
            status=PlanRepositoryStatus.CONSUMED,
        )

    def quarantine(
        self,
        lease: PlanLease,
        reason: StableSymbol,
        authentication_key: SecretValue,
        now: datetime,
    ) -> PlanRepositoryOutcome:
        """Make an interrupted or ambiguous dispatch permanently inapplicable."""
        require_utc(now, "now")
        if not isinstance(reason, StableSymbol):
            msg = "quarantine reason must be a StableSymbol"
            raise TypeError(msg)
        try:
            key = _key_bytes(authentication_key)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        try:
            digest_hex = _digest_hex(lease.digest)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        return self._with_lock(
            lambda: self._quarantine_locked(digest_hex, key, lease, reason)
        )

    def _quarantine_locked(
        self,
        digest_hex: str,
        key: bytes,
        lease: PlanLease,
        reason: StableSymbol,
    ) -> PlanRepositoryOutcome:
        try:
            record = self._read_record(digest_hex, key)
        except (OSError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        transition = record.quarantine(token=lease.token, reason=reason)
        if transition.decision is PlanLedgerDecision.CONFLICT:
            return PlanRepositoryOutcome(PlanRepositoryStatus.CONFLICT)
        try:
            self._write_record(digest_hex, transition.record, key)
        except OSError:
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        return _outcome_for_record(transition.record)

    def _transition_with_lease(  # noqa: PLR0913
        self,
        lease: PlanLease,
        authentication_key: SecretValue,
        now: datetime,
        *,
        required: PlanLedgerState,
        target: PlanLedgerState,
        status: PlanRepositoryStatus,
    ) -> PlanRepositoryOutcome:
        try:
            key = _key_bytes(authentication_key)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        try:
            digest_hex = _digest_hex(lease.digest)
        except (TypeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.MISSING)
        return self._with_lock(
            lambda: self._transition_with_lease_locked(
                lease,
                digest_hex,
                key,
                now,
                required=required,
                target=target,
                status=status,
            )
        )

    def _transition_with_lease_locked(  # noqa: C901, PLR0911, PLR0912, PLR0913
        self,
        lease: PlanLease,
        digest_hex: str,
        key: bytes,
        now: datetime,
        *,
        required: PlanLedgerState,
        target: PlanLedgerState,
        status: PlanRepositoryStatus,
    ) -> PlanRepositoryOutcome:
        try:
            record = self._read_record(digest_hex, key)
            if required is PlanLedgerState.LEASED:
                recovered = record.recover(now)
                if recovered.record.state is PlanLedgerState.QUARANTINED:
                    self._write_record(digest_hex, recovered.record, key)
                    return _outcome_for_record(recovered.record)
                _plan_bytes, decoded = self._read_local_plan(lease.digest, digest_hex)
                if not decoded.authenticate(key):
                    return PlanRepositoryOutcome(
                        PlanRepositoryStatus.CONFLICT,
                        state=record.state,
                        authenticated=False,
                    )
                if (
                    record.state is PlanLedgerState.LEASED
                    and record.lease_token == lease.token
                    and decoded.plan.is_expired(now)
                ):
                    expired_record = _LedgerRecord.available()
                    self._write_record(digest_hex, expired_record, key)
                    return PlanRepositoryOutcome(
                        PlanRepositoryStatus.EXPIRED,
                        state=PlanLedgerState.AVAILABLE,
                        reason=StableSymbol("plan-expired"),
                    )
            else:
                _plan_bytes, decoded = self._read_local_plan(lease.digest, digest_hex)
                if not decoded.authenticate(key):
                    return PlanRepositoryOutcome(
                        PlanRepositoryStatus.CONFLICT,
                        state=record.state,
                        authenticated=False,
                    )
        except (OSError, PlanDecodeError, ValueError):
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        transition = (
            record.dispatch(token=lease.token, now=now)
            if required is PlanLedgerState.LEASED
            else record.complete(token=lease.token)
        )
        if (
            target is PlanLedgerState.DISPATCHED
            and transition.decision is PlanLedgerDecision.IDEMPOTENT
        ):
            return PlanRepositoryOutcome(
                PlanRepositoryStatus.CONFLICT,
                state=record.state,
                reason=StableSymbol("dispatch-already-recorded"),
                authenticated=True,
            )
        if transition.decision is PlanLedgerDecision.ACCEPTED:
            if target is PlanLedgerState.DISPATCHED:
                marker_status = self._create_consumption_marker(
                    digest_hex,
                    decoded.plan.installation_id,
                    key,
                )
                if marker_status is not SecretStoreStatus.CREATED:
                    outcome_status = (
                        PlanRepositoryStatus.CONFLICT
                        if marker_status is SecretStoreStatus.CONFLICT
                        else PlanRepositoryStatus.FAILED
                    )
                    return PlanRepositoryOutcome(
                        outcome_status,
                        state=record.state,
                        reason=StableSymbol("consumption-marker-conflict"),
                        authenticated=True,
                    )
            elif target is PlanLedgerState.CONSUMED:
                marker_status = self._read_consumption_marker(
                    digest_hex,
                    decoded.plan.installation_id,
                    key,
                )
                if marker_status is not SecretStoreStatus.AVAILABLE:
                    return PlanRepositoryOutcome(
                        PlanRepositoryStatus.FAILED,
                        state=record.state,
                        reason=StableSymbol("consumption-marker-unavailable"),
                    )
        if transition.decision in {
            PlanLedgerDecision.ACCEPTED,
            PlanLedgerDecision.EXPIRED,
        }:
            try:
                self._write_record(digest_hex, transition.record, key)
            except OSError:
                return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)
        if transition.decision is PlanLedgerDecision.EXPIRED:
            return PlanRepositoryOutcome(
                PlanRepositoryStatus.EXPIRED,
                state=transition.record.state,
                reason=StableSymbol("plan-expired"),
            )
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

    def _recover_record(
        self,
        digest_hex: str,
        key: bytes,
        now: datetime,
    ) -> _LedgerRecord:
        state_path = self._states / f"{digest_hex}.json"
        if not state_path.exists():
            record = _LedgerRecord.quarantined(
                StableSymbol("missing-consumption-ledger")
            )
            self._write_record(digest_hex, record, key)
            return record
        record = self._read_record(digest_hex, key)
        transition = record.recover(now)
        if transition.decision in {
            PlanLedgerDecision.ACCEPTED,
            PlanLedgerDecision.EXPIRED,
        }:
            self._write_record(digest_hex, transition.record, key)
        return transition.record

    def _read_record(self, digest_hex: str, key: bytes) -> _LedgerRecord:
        raw = json.loads((self._states / f"{digest_hex}.json").read_text())
        expected = {
            "authentication",
            "lease_expires_at",
            "lease_token",
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
        authentication = raw["authentication"]
        record_mapping = {name: raw[name] for name in expected - {"authentication"}}
        if (
            not isinstance(authentication, str)
            or _AUTHENTICATION.fullmatch(authentication) is None
            or not hmac.compare_digest(
                authentication,
                _record_authentication(digest_hex, record_mapping, key),
            )
        ):
            msg = "plan ledger record authentication failed"
            raise ValueError(msg)
        token = raw["lease_token"]
        if token is not None and not isinstance(token, str):
            msg = "plan ledger lease token must be a string"
            raise ValueError(msg)
        return _LedgerRecord(
            state=PlanLedgerState(raw["state"]),
            lease_token=token,
            lease_expires_at=_parse_optional_time(raw["lease_expires_at"]),
            reason=(StableSymbol(raw["reason"]) if raw["reason"] is not None else None),
        )

    def _write_record(self, digest_hex: str, record: _LedgerRecord, key: bytes) -> None:
        record_mapping: dict[str, object] = {
            "lease_expires_at": (
                _format_time(record.lease_expires_at)
                if record.lease_expires_at is not None
                else None
            ),
            "lease_token": record.lease_token,
            "reason": record.reason.value if record.reason is not None else None,
            "schema": _STATE_SCHEMA,
            "state": record.state.value,
        }
        raw = {
            **record_mapping,
            "authentication": _record_authentication(
                digest_hex,
                record_mapping,
                key,
            ),
        }
        data = (
            json.dumps(raw, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        _atomic_write(self._states / f"{digest_hex}.json", data, _PRIVATE_FILE_MODE)

    def _read_consumption_marker(
        self,
        digest_hex: str,
        installation_id: str,
        key: bytes,
    ) -> SecretStoreStatus:
        reference = _consumption_marker_reference(digest_hex, installation_id)
        expected = _consumption_marker_value(digest_hex, key)
        outcome = self._consumption_store.get_consumption_marker(reference)
        if outcome.status is not SecretStoreStatus.AVAILABLE:
            return outcome.status
        if outcome.secret is None or not hmac.compare_digest(
            outcome.secret.reveal(),
            expected.reveal(),
        ):
            return SecretStoreStatus.CONFLICT
        return SecretStoreStatus.AVAILABLE

    def _create_consumption_marker(
        self,
        digest_hex: str,
        installation_id: str,
        key: bytes,
    ) -> SecretStoreStatus:
        return self._consumption_store.create_consumption_marker(
            _consumption_marker_reference(digest_hex, installation_id),
            _consumption_marker_value(digest_hex, key),
        ).status

    def _read_local_plan(self, digest: str, digest_hex: str):  # noqa: ANN202
        plan_path = self._plans / f"{digest_hex}.plan"
        if (
            os.name != "nt"
            and stat.S_IMODE(plan_path.stat().st_mode) != _PRIVATE_FILE_MODE
        ):
            msg = "local plan permissions are not private"
            raise ValueError(msg)
        plan_bytes = plan_path.read_bytes()
        decoded = PlanCodec.decode(plan_bytes)
        if decoded.digest != digest:
            msg = "local plan digest does not match its address"
            raise ValueError(msg)
        return plan_bytes, decoded

    def _with_lock(
        self, operation: Callable[[], PlanRepositoryOutcome]
    ) -> PlanRepositoryOutcome:
        try:
            with self._lock:
                return operation()
        except Exception:  # noqa: BLE001
            return PlanRepositoryOutcome(PlanRepositoryStatus.FAILED)


def _digest_hex(digest: str) -> str:
    if not isinstance(digest, str):
        msg = "plan digest must be a string"
        raise TypeError(msg)
    match = _DIGEST.fullmatch(digest)
    if match is None:
        msg = "plan digest must be canonical sha256"
        raise ValueError(msg)
    return match.group(1)


def _key_bytes(authentication_key: SecretValue) -> bytes:
    if not isinstance(authentication_key, SecretValue):
        msg = "authentication_key must be a SecretValue"
        raise TypeError(msg)
    key = authentication_key.reveal()
    if len(key) < _MINIMUM_AUTHENTICATION_KEY_BYTES:
        msg = "plan authentication key must contain at least 32 bytes"
        raise ValueError(msg)
    return key


def _record_authentication(
    digest_hex: str,
    record_mapping: dict[str, object],
    key: bytes,
) -> str:
    bound_record = {
        "digest": f"sha256:{digest_hex}",
        "record": record_mapping,
    }
    data = json.dumps(
        bound_record,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"hmac-sha256:{hmac.digest(key, data, 'sha256').hex()}"


def _consumption_marker_reference(
    digest_hex: str,
    installation_id: str,
) -> SecretStoreReference:
    digest_identity = base64.urlsafe_b64encode(bytes.fromhex(digest_hex)[:24]).decode(
        "ascii"
    )
    return SecretStoreReference(
        installation_id=installation_id,
        purpose=SecretPurpose.PLAN_CONSUMPTION,
        item_id=f"item-{digest_identity}",
    )


def _consumption_marker_value(digest_hex: str, key: bytes) -> SecretValue:
    marker = hmac.digest(
        key,
        _CONSUMPTION_MARKER_SCHEMA + digest_hex.encode("ascii"),
        "sha256",
    )
    return SecretValue(marker)


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
    for directory in reversed(missing):
        directory.chmod(_PRIVATE_DIRECTORY_MODE)
        _restrict_windows_acl(directory)
        _fsync_directory(directory.parent)
    path.chmod(_PRIVATE_DIRECTORY_MODE)
    _restrict_windows_acl(path)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        temporary.chmod(mode)
        _restrict_windows_acl(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(mode)
        _restrict_windows_acl(path)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def _atomic_publish_no_replace(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    linked = False
    try:
        temporary.chmod(mode)
        _restrict_windows_acl(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        linked = True
        _restrict_windows_acl(path)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        if linked:
            with suppress(OSError):
                path.unlink(missing_ok=True)
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


def _restrict_windows_acl(path: Path) -> None:
    if os.name != "nt":
        return
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    executable = rf"{system_root}\System32\icacls.exe"
    username = os.environ.get("USERNAME")
    if not username:
        msg = "Windows account identity is unavailable"
        raise OSError(msg)
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{username}" if domain else username
    permission = "(OI)(CI)F" if path.is_dir() else "F"
    completed = subprocess.run(  # noqa: S603
        [
            executable,
            str(path),
            "/inheritance:r",
            "/remove:g",
            "*S-1-1-0",
            "*S-1-5-11",
            "*S-1-5-32-545",
            "/grant:r",
            f"{principal}:{permission}",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if completed.returncode != 0:
        msg = "Windows private ACL enforcement failed"
        raise OSError(msg)


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


def _status_for_state(state: PlanLedgerState) -> PlanRepositoryStatus:
    return PlanRepositoryStatus(state.value)


def _outcome_for_record(
    record: _LedgerRecord,
    *,
    plan_bytes: bytes | None = None,
    authenticated: bool | None = None,
) -> PlanRepositoryOutcome:
    return PlanRepositoryOutcome(
        _status_for_state(record.state),
        plan_bytes=plan_bytes,
        state=record.state,
        reason=record.reason,
        authenticated=authenticated,
    )


def _unavailable_for_record(record: _LedgerRecord) -> PlanRepositoryOutcome:
    if record.state is PlanLedgerState.AVAILABLE:
        status = PlanRepositoryStatus.CONFLICT
    else:
        status = _status_for_state(record.state)
    return PlanRepositoryOutcome(status, state=record.state, reason=record.reason)
