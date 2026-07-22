"""Installation-local budgets, coalescing, and deterministic jitter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from cqmgr.adapters.persistence.locking import InterprocessFileLock
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    BudgetLimit,
    BudgetRequest,
    BudgetScope,
    CancellationToken,
    CoordinationDeadlineExceededError,
)
from cqmgr.domain.redaction import RedactedText

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from os import PathLike

_STATE_SCHEMA: Final = "cqmgr.local-budget-state/v1"
_COALESCE_SCHEMA: Final = "cqmgr.coalesced-read/v1"
_CANCELLATION_POLL_SECONDS: Final = 0.05


class DeterministicJitter:
    """Stable bounded jitter with no global random state."""

    def __init__(self, seed: str) -> None:
        """Bind a non-secret local scheduling seed."""
        if not isinstance(seed, str) or not seed:
            msg = "jitter seed must be a non-empty string"
            raise ValueError(msg)
        self._seed = seed

    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        """Return a stable value in the inclusive half-to-full-delay interval."""
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
            msg = "jitter delay must be non-negative seconds"
            raise ValueError(msg)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            msg = "jitter attempt must be a non-negative integer"
            raise ValueError(msg)
        if not isinstance(identity, str) or not identity:
            msg = "jitter identity must be a non-empty string"
            raise ValueError(msg)
        material = f"{self._seed}\0{identity}\0{attempt}".encode()
        number = int.from_bytes(hashlib.sha256(material).digest()[:8])
        fraction = number / ((1 << 64) - 1)
        return float(delay) * (0.5 + (fraction / 2))


class SharedBudgetCoordinator:
    """Conservatively charge provider, project, and quota-project windows."""

    def __init__(
        self,
        root: str | PathLike[str],
        limits: Mapping[BudgetScope, BudgetLimit],
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Open one shared accounting ledger with injectable time seams."""
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._limits = dict(limits)
        required = set(BudgetScope)
        if set(self._limits) != required:
            msg = "local budgets must configure every provider and project axis"
            raise ValueError(msg)
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = InterprocessFileLock(self._root / ".budgets.lock")

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Atomically charge every axis without exceeding the caller deadline."""
        if not isinstance(request, BudgetRequest):
            msg = "budget request must be a BudgetRequest"
            raise TypeError(msg)
        while True:
            cancellation.raise_if_cancelled()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise CoordinationDeadlineExceededError
            actual_deadline = time.monotonic() + remaining
            self._lock.acquire(deadline=actual_deadline, cancellation=cancellation)
            try:
                state = self._read_state()
                now = self._wall_clock()
                entries, wait = self._prospective_entries(state, request, now)
                if wait is None:
                    self._write_state(entries)
                    return BudgetGrant(charged_at=now, request=request)
            finally:
                self._lock.release()
            remaining = deadline - self._monotonic()
            if wait is None or wait >= remaining:
                raise CoordinationDeadlineExceededError
            await self._sleep(
                min(max(wait, 0.001), remaining, _CANCELLATION_POLL_SECONDS)
            )

    def _prospective_entries(
        self,
        state: dict[str, Any],
        request: BudgetRequest,
        now: float,
    ) -> tuple[dict[str, Any], float | None]:
        entries = dict(state["entries"])
        axes = (
            (BudgetScope.PROVIDER, request.provider),
            (BudgetScope.PROJECT, request.project),
            (BudgetScope.ADC_QUOTA_PROJECT, request.adc_quota_project),
        )
        blocked_for: list[float] = []
        prepared: list[tuple[str, dict[str, Any]]] = []
        for scope, identity in axes:
            limit = self._limits[scope]
            if request.units > limit.capacity:
                raise CoordinationDeadlineExceededError
            key = self._budget_key(scope, identity)
            existing = entries.get(key)
            if existing is None:
                entry = {"window_started_at": now, "last_seen_at": now, "used": 0}
            else:
                entry = dict(existing)
                last_seen = float(entry["last_seen_at"])
                window_started = float(entry["window_started_at"])
                conservative_now = max(now, last_seen)
                if now >= last_seen and now - window_started >= limit.period_seconds:
                    entry = {
                        "window_started_at": now,
                        "last_seen_at": now,
                        "used": 0,
                    }
                else:
                    entry["last_seen_at"] = conservative_now
            if int(entry["used"]) + request.units > limit.capacity:
                reset_at = float(entry["window_started_at"]) + limit.period_seconds
                blocked_for.append(
                    max(
                        reset_at - now,
                        limit.period_seconds
                        if now < float(entry["last_seen_at"])
                        else 0.0,
                    )
                )
            prepared.append((key, entry))
        if blocked_for:
            return entries, max(blocked_for)
        for key, entry in prepared:
            entry["used"] = int(entry["used"]) + request.units
            entries[key] = entry
        return entries, None

    @staticmethod
    def _budget_key(scope: BudgetScope, identity: str) -> str:
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"{scope.value}:{digest}"

    def _read_state(self) -> dict[str, Any]:
        path = self._root / "budgets.json"
        if not path.exists():
            return {"schema": _STATE_SCHEMA, "entries": {}}
        try:
            state = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as error:
            msg = "local budget state is unreadable"
            raise RuntimeError(msg) from error
        if state.get("schema") != _STATE_SCHEMA or not isinstance(
            state.get("entries"), dict
        ):
            msg = "local budget state has an unsupported schema"
            raise RuntimeError(msg)
        return state

    def _write_state(self, entries: dict[str, Any]) -> None:
        _atomic_write_json(
            self._root,
            "budgets.json",
            {"schema": _STATE_SCHEMA, "entries": entries},
        )


class SharedReadCoalescer:
    """Share one normalized safe read result with concurrent local callers."""

    def __init__(  # noqa: PLR0913 - each injected clock is an explicit test seam
        self,
        root: str | PathLike[str],
        *,
        result_ttl_seconds: float = 0.25,
        leader_lease_seconds: float = 30.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Open an installation-local coordination directory."""
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._result_ttl = result_ttl_seconds
        self._leader_lease = leader_lease_seconds
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._sleep = sleep

    async def run(
        self,
        identity: str,
        work: Callable[[], Awaitable[RedactedText]],
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> RedactedText:
        """Elect one leader and return its safe result to equivalent waiters."""
        digest = hashlib.sha256(identity.encode()).hexdigest()
        lock = InterprocessFileLock(self._root / f".{digest}.lock")
        state_path = self._root / f"{digest}.json"
        while True:
            cancellation.raise_if_cancelled()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise CoordinationDeadlineExceededError
            lock.acquire(
                deadline=time.monotonic() + remaining, cancellation=cancellation
            )
            leader = False
            try:
                now = self._wall_clock()
                state = _read_optional_json(state_path)
                if (
                    state is not None
                    and state.get("schema") == _COALESCE_SCHEMA
                    and state.get("status") == "done"
                    and float(state["expires_at"]) >= now
                ):
                    value = base64.b64decode(state["value"]).decode()
                    return RedactedText(value)
                if (
                    state is None
                    or state.get("schema") != _COALESCE_SCHEMA
                    or state.get("status") != "in-flight"
                    or float(state.get("lease_expires_at", 0)) < now
                ):
                    _atomic_write_json(
                        self._root,
                        state_path.name,
                        {
                            "schema": _COALESCE_SCHEMA,
                            "status": "in-flight",
                            "lease_expires_at": now + self._leader_lease,
                        },
                    )
                    leader = True
            finally:
                lock.release()
            if leader:
                try:
                    value = await work()
                    _require_redacted_text(value)
                except BaseException:
                    lock.acquire()
                    try:
                        state_path.unlink(missing_ok=True)
                    finally:
                        lock.release()
                    raise
                lock.acquire()
                try:
                    _atomic_write_json(
                        self._root,
                        state_path.name,
                        {
                            "schema": _COALESCE_SCHEMA,
                            "status": "done",
                            "expires_at": self._wall_clock() + self._result_ttl,
                            "value": base64.b64encode(value.value.encode()).decode(),
                        },
                    )
                finally:
                    lock.release()
                return value
            await self._sleep(min(0.01, remaining))


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        msg = "local coalescing state is unreadable"
        raise RuntimeError(msg) from error
    if not isinstance(value, dict):
        msg = "local coalescing state is malformed"
        raise TypeError(msg)
    return value


def _atomic_write_json(root: Path, name: str, data: dict[str, Any]) -> None:
    temporary = root / f".{name}.{os.getpid()}.tmp"
    destination = root / name
    with temporary.open("wb") as stream:
        stream.write(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(destination)
    if os.name == "nt":
        return
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _require_redacted_text(value: object) -> None:
    if not isinstance(value, RedactedText):
        msg = "coalesced reads must return RedactedText"
        raise TypeError(msg)
