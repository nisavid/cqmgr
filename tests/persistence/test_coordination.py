"""State, property, and subprocess contracts for local coordination."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import time
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cqmgr.adapters.persistence.coordination import (
    DeterministicJitter,
    SharedBudgetCoordinator,
    SharedReadCoalescer,
)
from cqmgr.adapters.persistence.locking import InterprocessFileLock
from cqmgr.application.ports.coordination import (
    BudgetLimit,
    BudgetRequest,
    BudgetScope,
    CancellationToken,
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
)
from cqmgr.domain.redaction import REDACTION_MARKER, RedactedText

_DEADLINE_ASSERTION_BOUND = 0.5
_REFILLED_AT = 110.0


class _UnexpectedSleepError(Exception):
    """A virtual-clock test reached an unexpected wait path."""


class _LeaderFailureError(Exception):
    """A test-only coalesced-read leader failure."""


def _limits(
    capacity: int = 1, period_seconds: float = 60.0
) -> dict[BudgetScope, BudgetLimit]:
    return {
        scope: BudgetLimit(capacity=capacity, period_seconds=period_seconds)
        for scope in BudgetScope
    }


def _request(suffix: str = "one") -> BudgetRequest:
    return BudgetRequest(
        provider="cloudquotas.googleapis.com",
        project=f"projects/{suffix}",
        adc_quota_project=f"projects/billing-{suffix}",
    )


def _budget_worker(
    root: str, start: multiprocessing.synchronize.Event, queue: object
) -> None:
    start.wait(timeout=10)
    coordinator = SharedBudgetCoordinator(root, _limits(capacity=2))

    async def attempt() -> str:
        try:
            await coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 0.25,
                cancellation=CancellationToken(),
            )
        except CoordinationDeadlineExceededError:
            return "deadline"
        return "granted"

    queue.put(asyncio.run(attempt()))  # type: ignore[attr-defined]


def _coalesce_worker(
    root: str,
    marker: str,
    start: multiprocessing.synchronize.Event,
    queue: object,
) -> None:
    start.wait(timeout=10)
    coalescer = SharedReadCoalescer(root, result_ttl_seconds=1.0)

    async def work() -> RedactedText:
        _record_work(Path(marker))
        await asyncio.sleep(0.2)
        return RedactedText("normalized-result")

    async def run() -> str:
        result = await coalescer.run(
            "same-read",
            work,
            deadline=time.monotonic() + 5,
            cancellation=CancellationToken(),
        )
        return result.value

    queue.put(asyncio.run(run()))  # type: ignore[attr-defined]


def _record_work(path: Path) -> None:
    existing = path.read_text() if path.exists() else ""
    path.write_text(existing + "x")


def _hold_lock(path: str, ready: object) -> None:
    lock = InterprocessFileLock(path)
    lock.acquire()
    ready.put("locked")  # type: ignore[attr-defined]
    time.sleep(30)


@given(
    delay=st.floats(min_value=0, max_value=3600, allow_nan=False, allow_infinity=False),
    attempt=st.integers(min_value=0, max_value=1000),
    identity=st.text(min_size=1, max_size=40),
)
def test_deterministic_jitter_is_stable_and_bounded(
    delay: float,
    attempt: int,
    identity: str,
) -> None:
    """Injected jitter never exceeds its caller-owned delay or changes per key."""
    jitter = DeterministicJitter("installation-seed")

    first = jitter.apply(delay, attempt=attempt, identity=identity)
    second = jitter.apply(delay, attempt=attempt, identity=identity)

    assert first == second
    assert delay / 2 <= first <= delay


@pytest.mark.parametrize(
    ("delay", "attempt", "identity", "message"),
    [
        (-1, 0, "read", "delay"),
        (True, 0, "read", "delay"),
        (1, -1, "read", "attempt"),
        (1, True, "read", "attempt"),
        (1, 0, "", "identity"),
    ],
)
def test_deterministic_jitter_rejects_invalid_scheduling_inputs(
    delay: object,
    attempt: object,
    identity: object,
    message: str,
) -> None:
    """Jitter never guesses negative, boolean, or absent scheduling values."""
    jitter = DeterministicJitter("installation-seed")
    with pytest.raises(ValueError, match=message):
        jitter.apply(
            delay,  # type: ignore[arg-type]
            attempt=attempt,  # type: ignore[arg-type]
            identity=identity,  # type: ignore[arg-type]
        )


def test_deterministic_jitter_requires_a_nonempty_seed() -> None:
    """A caller must provide the stable seam that desynchronizes schedules."""
    with pytest.raises(ValueError, match="seed"):
        DeterministicJitter("")


def test_cancelled_budget_request_makes_zero_durable_charge(tmp_path: Path) -> None:
    """Cancellation before dispatch leaves every shared budget untouched."""
    token = CancellationToken()
    token.cancel()
    coordinator = SharedBudgetCoordinator(tmp_path, _limits(period_seconds=1))

    with pytest.raises(CoordinationCancelledError):
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 1,
                cancellation=token,
            )
        )

    assert not (tmp_path / "budgets.json").exists()


def test_budget_wait_preserves_deadline_and_conservative_charge(tmp_path: Path) -> None:
    """An exhausted window cannot borrow from the future or overrun a deadline."""
    coordinator = SharedBudgetCoordinator(tmp_path, _limits())
    token = CancellationToken()
    asyncio.run(
        coordinator.acquire(
            _request(),
            deadline=time.monotonic() + 1,
            cancellation=token,
        )
    )

    started = time.monotonic()
    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=started + 0.05,
                cancellation=CancellationToken(),
            )
        )

    assert time.monotonic() - started < _DEADLINE_ASSERTION_BOUND
    state = (tmp_path / "budgets.json").read_text()
    assert "cloudquotas.googleapis.com" not in state
    assert "projects/one" not in state


def test_budget_wait_observes_cancellation_promptly(tmp_path: Path) -> None:
    """Cancellation interrupts a budget wait without charging another request."""
    coordinator = SharedBudgetCoordinator(tmp_path, _limits(period_seconds=1))
    asyncio.run(
        coordinator.acquire(
            _request(),
            deadline=time.monotonic() + 1,
            cancellation=CancellationToken(),
        )
    )

    async def wait_then_cancel() -> None:
        token = CancellationToken()
        waiting = asyncio.create_task(
            coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 2,
                cancellation=token,
            )
        )
        await asyncio.sleep(0.02)
        token.cancel()
        with pytest.raises(CoordinationCancelledError):
            await asyncio.wait_for(waiting, timeout=0.5)

    asyncio.run(wait_then_cancel())
    state = json.loads((tmp_path / "budgets.json").read_text())
    assert {entry["used"] for entry in state["entries"].values()} == {1}


def test_budget_state_corruption_fails_closed(tmp_path: Path) -> None:
    """Unreadable or newer shared accounting state never resets usage to zero."""
    coordinator = SharedBudgetCoordinator(tmp_path, _limits())
    (tmp_path / "budgets.json").write_bytes(b"not-json")
    with pytest.raises(RuntimeError, match="unreadable"):
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
    (tmp_path / "budgets.json").write_text(
        json.dumps({"schema": "cqmgr.local-budget-state/v2", "entries": {}})
    )
    with pytest.raises(RuntimeError, match="unsupported schema"):
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )


def test_budget_configuration_and_request_types_fail_closed(tmp_path: Path) -> None:
    """Every budget axis and a typed conservative charge are mandatory."""
    with pytest.raises(ValueError, match="every"):
        SharedBudgetCoordinator(tmp_path, {BudgetScope.PROVIDER: BudgetLimit(1, 1)})
    coordinator = SharedBudgetCoordinator(tmp_path, _limits())
    with pytest.raises(TypeError, match="BudgetRequest"):
        asyncio.run(
            coordinator.acquire(  # type: ignore[arg-type]
                "request",  # type: ignore[arg-type]
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            coordinator.acquire(
                BudgetRequest("provider", "project", "billing", units=2),
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )


def test_clock_rollback_does_not_refill_a_budget_window(tmp_path: Path) -> None:
    """Wall-clock rollback extends conservative accounting instead of adding tokens."""
    now = 100.0

    def clock() -> float:
        return now

    coordinator = SharedBudgetCoordinator(
        tmp_path,
        _limits(period_seconds=10),
        wall_clock=clock,
        monotonic=time.monotonic,
    )
    asyncio.run(
        coordinator.acquire(
            _request(),
            deadline=time.monotonic() + 1,
            cancellation=CancellationToken(),
        )
    )
    now = 90.0

    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 0.05,
                cancellation=CancellationToken(),
            )
        )


def test_budget_refills_only_after_a_complete_window(tmp_path: Path) -> None:
    """A virtual clock makes the local fixed-window policy deterministic."""
    now = 100.0

    def clock() -> float:
        return now

    async def no_sleep(_seconds: float) -> None:
        raise _UnexpectedSleepError

    coordinator = SharedBudgetCoordinator(
        tmp_path,
        _limits(period_seconds=10),
        wall_clock=clock,
        monotonic=clock,
        sleep=no_sleep,
    )
    asyncio.run(
        coordinator.acquire(_request(), deadline=101, cancellation=CancellationToken())
    )
    now = _REFILLED_AT

    grant = asyncio.run(
        coordinator.acquire(_request(), deadline=111, cancellation=CancellationToken())
    )

    assert grant.charged_at == _REFILLED_AT


def test_concurrent_processes_share_one_conservative_budget(tmp_path: Path) -> None:
    """Separate processes cannot each spend a private copy of one local budget."""
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(target=_budget_worker, args=(str(tmp_path), start, queue))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)

    assert sorted(results) == ["deadline", "deadline", "granted", "granted"]
    assert [process.exitcode for process in processes] == [0] * 4


def test_equivalent_process_reads_are_coalesced_once(tmp_path: Path) -> None:
    """Concurrent processes share one normalized result without duplicate work."""
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    marker = tmp_path / "work-count"
    processes = [
        context.Process(
            target=_coalesce_worker,
            args=(str(tmp_path / "coalescing"), str(marker), start, queue),
        )
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)

    assert results == ["normalized-result"] * 3
    assert marker.read_text() == "x"
    assert [process.exitcode for process in processes] == [0] * 3


def test_coalesced_read_failure_releases_the_identity_for_retry(tmp_path: Path) -> None:
    """A failed leader leaves no false result and a later caller can lead."""
    coalescer = SharedReadCoalescer(tmp_path)

    async def fail() -> RedactedText:
        raise _LeaderFailureError

    async def succeed() -> RedactedText:
        return RedactedText("safe")

    with pytest.raises(_LeaderFailureError):
        asyncio.run(
            coalescer.run(
                "read",
                fail,
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
    result = asyncio.run(
        coalescer.run(
            "read",
            succeed,
            deadline=time.monotonic() + 1,
            cancellation=CancellationToken(),
        )
    )

    assert result.value == "safe"


def test_coalesced_read_rejects_raw_results_and_corrupt_state(tmp_path: Path) -> None:
    """Only safe normalized results and valid local coordination state are reusable."""
    coalescer = SharedReadCoalescer(tmp_path)

    async def raw() -> object:
        return "raw"

    with pytest.raises(TypeError, match="RedactedText"):
        asyncio.run(
            coalescer.run(  # type: ignore[arg-type]
                "raw-read",
                raw,  # type: ignore[arg-type]
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
    state = next(tmp_path.glob("*.json"), None)
    assert state is None
    digest = hashlib.sha256(b"broken-read").hexdigest()
    (tmp_path / f"{digest}.json").write_bytes(b"not-json")
    with pytest.raises(RuntimeError, match="unreadable"):
        asyncio.run(
            coalescer.run(
                "broken-read",
                _safe_read,
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )


def test_coalesced_read_honors_cancelled_and_elapsed_callers(tmp_path: Path) -> None:
    """Coalescing never widens the caller-owned cancellation or deadline."""
    coalescer = SharedReadCoalescer(tmp_path)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CoordinationCancelledError):
        asyncio.run(
            coalescer.run(
                "cancelled",
                _safe_read,
                deadline=time.monotonic() + 1,
                cancellation=token,
            )
        )
    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            coalescer.run(
                "elapsed",
                _safe_read,
                deadline=time.monotonic() - 1,
                cancellation=CancellationToken(),
            )
        )


async def _safe_read() -> RedactedText:
    await asyncio.sleep(0)
    return RedactedText("safe")


def test_coalesced_state_retains_only_explicitly_scrubbed_text(tmp_path: Path) -> None:
    """Shared read state excludes known contact values and machine-local paths."""
    quota_contact = "person@example.test"
    machine_path = "/Users/example/private/adc.json"
    coalescer = SharedReadCoalescer(tmp_path)

    async def work() -> RedactedText:
        return RedactedText(
            f"{quota_contact}:{machine_path}",
            sensitive_values=(quota_contact,),
            machine_paths=(machine_path,),
        )

    result = asyncio.run(
        coalescer.run(
            "safe-read",
            work,
            deadline=time.monotonic() + 1,
            cancellation=CancellationToken(),
        )
    )
    persisted = b"".join(path.read_bytes() for path in tmp_path.glob("*.json"))

    assert result.value == f"{REDACTION_MARKER}:{REDACTION_MARKER}"
    assert quota_contact.encode() not in persisted
    assert machine_path.encode() not in persisted


def test_process_death_releases_interprocess_lock(tmp_path: Path) -> None:
    """OS lock ownership recovers automatically after a process crashes."""
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    path = tmp_path / "coordination.lock"
    process = context.Process(target=_hold_lock, args=(str(path), ready))
    process.start()
    assert ready.get(timeout=10) == "locked"
    process.terminate()
    process.join(timeout=10)

    lock = InterprocessFileLock(path)
    lock.acquire(deadline=time.monotonic() + 1)
    lock.release()

    assert process.exitcode is not None


def test_lock_validates_polling_and_instance_ownership(tmp_path: Path) -> None:
    """A lock instance cannot recursively overwrite its held file descriptor."""
    with pytest.raises(ValueError, match="polling"):
        InterprocessFileLock(tmp_path / "invalid.lock", poll_seconds=0)
    lock = InterprocessFileLock(tmp_path / "valid.lock")
    lock.release()
    lock.acquire()
    with pytest.raises(RuntimeError, match="already held"):
        lock.acquire()
    lock.release()
