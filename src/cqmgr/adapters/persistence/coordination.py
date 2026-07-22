"""Installation-local budgets, coalescing, and deterministic jitter."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import time
from contextlib import suppress
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
_BUDGET_KEY_PATTERN: Final = re.compile(
    r"(provider|project|adc-quota-project):[0-9a-f]{64}"
)
_BUDGET_STATE_FIELDS: Final = frozenset({"schema", "entries"})
_BUDGET_ENTRY_FIELDS: Final = frozenset({"window_started_at", "last_seen_at", "used"})


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
        self._lock_path = self._root / ".budgets.lock"

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
            lock = InterprocessFileLock(self._lock_path)
            await lock.acquire_async(
                deadline=actual_deadline,
                cancellation=cancellation,
            )
            try:
                cancellation.raise_if_cancelled()
                if self._monotonic() >= deadline:
                    raise CoordinationDeadlineExceededError
                state = self._read_state()
                now = self._wall_clock()
                entries, wait = self._prospective_entries(state, request, now)
                if wait is None:
                    self._write_state(entries)
                    cancellation.raise_if_cancelled()
                    if self._monotonic() >= deadline:
                        raise CoordinationDeadlineExceededError
                    return BudgetGrant(charged_at=now, request=request)
            finally:
                lock.release()
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
                msg = "budget request units exceed configured capacity"
                raise ValueError(msg)
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
        if not isinstance(state, dict):
            msg = "local budget state is malformed"
            raise TypeError(msg)
        if state.get("schema") != _STATE_SCHEMA:
            msg = "local budget state has an unsupported schema"
            raise RuntimeError(msg)
        if set(state) != _BUDGET_STATE_FIELDS or not isinstance(state["entries"], dict):
            msg = "local budget state is malformed"
            raise RuntimeError(msg)
        for key, entry in state["entries"].items():
            self._validate_budget_entry(key, entry)
        return state

    def _validate_budget_entry(self, key: object, entry: object) -> None:
        if not isinstance(key, str) or _BUDGET_KEY_PATTERN.fullmatch(key) is None:
            msg = "local budget state contains an invalid entry key"
            raise RuntimeError(msg)
        if not isinstance(entry, dict) or set(entry) != _BUDGET_ENTRY_FIELDS:
            msg = "local budget state contains a malformed entry"
            raise RuntimeError(msg)
        window_started = entry["window_started_at"]
        last_seen = entry["last_seen_at"]
        used = entry["used"]
        if not _is_finite_nonnegative_number(window_started) or not (
            _is_finite_nonnegative_number(last_seen)
        ):
            msg = "local budget state contains an invalid clock"
            raise RuntimeError(msg)
        if last_seen < window_started:
            msg = "local budget state contains a reversed clock"
            raise RuntimeError(msg)
        scope = BudgetScope(key.partition(":")[0])
        if (
            isinstance(used, bool)
            or not isinstance(used, int)
            or used < 0
            or used > self._limits[scope].capacity
        ):
            msg = "local budget state contains invalid usage"
            raise RuntimeError(msg)

    def _write_state(self, entries: dict[str, Any]) -> None:
        _atomic_write_json(
            self._root,
            "budgets.json",
            {"schema": _STATE_SCHEMA, "entries": entries},
        )


class SharedReadCoalescer:
    """Share one normalized safe read result with concurrent local callers."""

    def __init__(
        self,
        root: str | PathLike[str],
        *,
        result_ttl_seconds: float = 0.25,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Open an installation-local coordination directory."""
        if not _is_finite_nonnegative_number(result_ttl_seconds):
            msg = "coalesced result TTL must be non-negative seconds"
            raise ValueError(msg)
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._result_ttl = result_ttl_seconds
        self._wall_clock = wall_clock
        self._monotonic = monotonic

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
        cancellation.raise_if_cancelled()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise CoordinationDeadlineExceededError
        await lock.acquire_async(
            deadline=time.monotonic() + remaining,
            cancellation=cancellation,
        )
        try:
            cancellation.raise_if_cancelled()
            if self._monotonic() >= deadline:
                raise CoordinationDeadlineExceededError
            now = self._wall_clock()
            state = _read_optional_json(state_path)
            cached = _read_cached_result(state, now=now)
            if cached is not None:
                return cached
            owner = secrets.token_hex(16)
            _atomic_write_json(
                self._root,
                state_path.name,
                {
                    "schema": _COALESCE_SCHEMA,
                    "status": "in-flight",
                    "owner": owner,
                },
            )
            try:
                value = await _run_bounded_work(
                    work,
                    deadline=deadline,
                    cancellation=cancellation,
                    monotonic=self._monotonic,
                )
                _require_redacted_text(value)
                cancellation.raise_if_cancelled()
                _raise_deadline_if_elapsed(deadline, self._monotonic)
            except BaseException:
                state_path.unlink(missing_ok=True)
                raise
            _atomic_write_json(
                self._root,
                state_path.name,
                {
                    "schema": _COALESCE_SCHEMA,
                    "status": "done",
                    "owner": owner,
                    "expires_at": self._wall_clock() + self._result_ttl,
                    "value": base64.b64encode(value.value.encode()).decode(),
                },
            )
            return value
        finally:
            lock.release()


async def _run_bounded_work(
    work: Callable[[], Awaitable[RedactedText]],
    *,
    deadline: float,
    cancellation: CancellationToken,
    monotonic: Callable[[], float],
) -> RedactedText:
    task = asyncio.ensure_future(work())
    try:
        while not task.done():
            cancellation.raise_if_cancelled()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CoordinationDeadlineExceededError
            await asyncio.wait(
                {task},
                timeout=min(_CANCELLATION_POLL_SECONDS, remaining),
            )
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _read_cached_result(
    state: dict[str, Any] | None,
    *,
    now: float,
) -> RedactedText | None:
    if state is None:
        return None
    if state.get("schema") != _COALESCE_SCHEMA:
        msg = "local coalescing state has an unsupported schema"
        raise RuntimeError(msg)
    status = state.get("status")
    if status == "in-flight":
        if (
            set(state) != {"schema", "status", "owner"}
            or not isinstance(state.get("owner"), str)
            or not state["owner"]
        ):
            msg = "local coalescing state is malformed"
            raise RuntimeError(msg)
        return None
    if status != "done" or set(state) != {
        "schema",
        "status",
        "owner",
        "expires_at",
        "value",
    }:
        msg = "local coalescing state is malformed"
        raise RuntimeError(msg)
    expires_at = state["expires_at"]
    owner = state["owner"]
    value = state["value"]
    if (
        not _is_finite_nonnegative_number(expires_at)
        or not isinstance(owner, str)
        or not owner
        or not isinstance(value, str)
    ):
        msg = "local coalescing state is malformed"
        raise RuntimeError(msg)
    if expires_at < now:
        return None
    try:
        decoded = base64.b64decode(value, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as error:
        msg = "local coalescing state is malformed"
        raise RuntimeError(msg) from error
    return RedactedText(decoded)


def _is_finite_nonnegative_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _raise_deadline_if_elapsed(
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    if monotonic() >= deadline:
        raise CoordinationDeadlineExceededError


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
