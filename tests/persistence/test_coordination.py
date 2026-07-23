"""State, property, and subprocess contracts for local coordination."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import math
import multiprocessing
import tempfile
import threading
import time
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import cqmgr.adapters.persistence.coordination as coordination_adapter
import cqmgr.adapters.persistence.locking as locking_adapter
from cqmgr.adapters.persistence.coordination import (
    DeterministicJitter,
    SharedBudgetCoordinator,
    SharedReadCoalescer,
)
from cqmgr.adapters.persistence.locking import InterprocessFileLock
from cqmgr.application.ports.coordination import (
    BudgetCommitUnknownError,
    BudgetLimit,
    BudgetRequest,
    BudgetScope,
    CancellationToken,
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
    CoordinationUnavailableError,
)
from cqmgr.domain.redaction import REDACTION_MARKER, RedactedText

_DEADLINE_ASSERTION_BOUND = 0.5
_EXPECTED_EVENT_LOOP_TICKS = 4
_EXPECTED_TWO_CALLS = 2
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
                deadline=time.monotonic() + 5,
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


def _hold_coalesced_leadership(root: str, ready: object) -> None:
    coalescer = SharedReadCoalescer(root)

    async def work() -> RedactedText:
        ready.put("leading")  # type: ignore[attr-defined]
        await asyncio.sleep(30)
        return RedactedText("never-published")

    asyncio.run(
        coalescer.run(
            "crashed-read",
            work,
            deadline=time.monotonic() + 60,
            cancellation=CancellationToken(),
        )
    )


def _hold_owned_sync_leadership(  # noqa: PLR0913 - explicit subprocess signals
    root: str,
    marker: str,
    started: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    caller_finished: multiprocessing.synchronize.Event,
    process_release: multiprocessing.synchronize.Event,
) -> None:
    coalescer = SharedReadCoalescer(root)

    def work() -> RedactedText:
        _record_work(Path(marker))
        started.set()
        release.wait(timeout=10)
        return RedactedText("sync-result")

    try:
        asyncio.run(
            coalescer.run_sync(
                "owned-sync-read",
                work,
                deadline=time.monotonic() + 5,
                cancellation=CancellationToken(),
            )
        )
    except CoordinationDeadlineExceededError:
        caller_finished.set()
    process_release.wait(timeout=10)


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
        (math.nan, 0, "read", "delay"),
        (math.inf, 0, "read", "delay"),
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


@pytest.mark.parametrize("control", ["cancellation", "deadline"])
def test_durable_budget_charge_completes_the_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    """A committed charge cannot be reported as a rejected acquisition."""
    token = CancellationToken()
    now = 100.0
    original_write = coordination_adapter._atomic_write_json  # noqa: SLF001

    def monotonic() -> float:
        return now

    def write_then_stop(root: Path, name: str, data: dict[str, object]) -> None:
        nonlocal now
        original_write(root, name, data)
        if control == "cancellation":
            token.cancel()
        else:
            now = 101.0

    monkeypatch.setattr(coordination_adapter, "_atomic_write_json", write_then_stop)
    grant = asyncio.run(
        SharedBudgetCoordinator(
            tmp_path,
            _limits(capacity=2),
            monotonic=monotonic,
        ).acquire(
            _request(),
            deadline=101,
            cancellation=token,
        )
    )

    assert grant.request == _request()
    state = json.loads((tmp_path / "budgets.json").read_text())
    assert {entry["used"] for entry in state["entries"].values()} == {1}


@pytest.mark.parametrize("control", ["cancellation", "deadline"])
def test_budget_controls_are_rechecked_immediately_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    """Cancellation or expiry during preparation prevents the durable charge."""
    token = CancellationToken()
    now = 100.0
    original_prepare = SharedBudgetCoordinator._prospective_entries  # noqa: SLF001

    def monotonic() -> float:
        return now

    def prepare_then_stop(
        coordinator: SharedBudgetCoordinator,
        state: dict[str, object],
        request: BudgetRequest,
        wall_time: float,
    ) -> tuple[dict[str, object], float | None]:
        nonlocal now
        prepared = original_prepare(coordinator, state, request, wall_time)
        if control == "cancellation":
            token.cancel()
        else:
            now = 101.0
        return prepared

    monkeypatch.setattr(
        SharedBudgetCoordinator,
        "_prospective_entries",
        prepare_then_stop,
    )
    coordinator = SharedBudgetCoordinator(
        tmp_path,
        _limits(capacity=2),
        monotonic=monotonic,
    )
    expected = (
        CoordinationCancelledError
        if control == "cancellation"
        else CoordinationDeadlineExceededError
    )

    with pytest.raises(expected):
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=101,
                cancellation=token,
            )
        )

    assert not (tmp_path / "budgets.json").exists()


def test_post_replace_sync_failure_reports_unknown_budget_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visible charge with unconfirmed durability cannot invite a blind retry."""
    request = _request()

    def fail_directory_sync(_root: Path) -> None:
        raise OSError(errno.EIO, "directory sync failed")

    monkeypatch.setattr(
        coordination_adapter,
        "_sync_directory",
        fail_directory_sync,
    )
    coordinator = SharedBudgetCoordinator(tmp_path, _limits(capacity=2))

    with pytest.raises(BudgetCommitUnknownError) as caught:
        asyncio.run(
            coordinator.acquire(
                request,
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )

    error = caught.value
    assert str(error) == "local budget charge durability is unknown"
    assert error.possible_grant.request == request
    assert error.possible_grant.charged_at > 0
    assert isinstance(error.__cause__, OSError)
    storage_error = error.__cause__.__cause__
    assert isinstance(storage_error, OSError)
    assert storage_error.errno == errno.EIO
    assert storage_error.strerror == "directory sync failed"
    state = json.loads((tmp_path / "budgets.json").read_text())
    assert {entry["used"] for entry in state["entries"].values()} == {1}


def test_pre_replace_sync_failure_remains_a_definite_storage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed temporary-file sync leaves no charge and permits a later retry."""
    (tmp_path / ".budgets.lock").write_bytes(b"0")
    failure = OSError(errno.EIO, "temporary sync failed")

    def fail_file_sync(_descriptor: int) -> None:
        raise failure

    monkeypatch.setattr(coordination_adapter.os, "fsync", fail_file_sync)
    coordinator = SharedBudgetCoordinator(tmp_path, _limits(capacity=2))

    with pytest.raises(OSError, match="temporary sync failed") as caught:
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )

    assert caught.value is failure
    assert not isinstance(caught.value, BudgetCommitUnknownError)
    assert not (tmp_path / "budgets.json").exists()


@pytest.mark.parametrize("deadline", [True, "later", math.nan, math.inf, -math.inf])
def test_public_coordinators_reject_invalid_deadlines_before_side_effects(
    tmp_path: Path,
    deadline: object,
) -> None:
    """Invalid deadline values cannot charge state, acquire locks, or begin work."""
    budget_root = tmp_path / "budgets"
    coalescing_root = tmp_path / "coalescing"
    work_calls = 0

    async def work() -> RedactedText:
        nonlocal work_calls
        work_calls += 1
        return RedactedText("unsafe")

    with pytest.raises((TypeError, ValueError), match="deadline"):
        asyncio.run(
            SharedBudgetCoordinator(budget_root, _limits()).acquire(
                _request(),
                deadline=deadline,  # type: ignore[arg-type]
                cancellation=CancellationToken(),
            )
        )
    with pytest.raises((TypeError, ValueError), match="deadline"):
        asyncio.run(
            SharedReadCoalescer(coalescing_root).run(
                "invalid-deadline",
                work,
                deadline=deadline,  # type: ignore[arg-type]
                cancellation=CancellationToken(),
            )
        )

    assert tuple(budget_root.iterdir()) == ()
    assert tuple(coalescing_root.iterdir()) == ()
    assert work_calls == 0


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
    with pytest.raises(CoordinationUnavailableError):
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
    with pytest.raises(CoordinationUnavailableError):
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


@pytest.mark.parametrize(
    "state",
    [
        {"schema": "cqmgr.local-budget-state/v1", "entries": [], "extra": 1},
        {
            "schema": "cqmgr.local-budget-state/v1",
            "entries": {"provider:not-a-digest": {}},
        },
        {
            "schema": "cqmgr.local-budget-state/v1",
            "entries": {
                "provider:" + ("0" * 64): {
                    "window_started_at": 1.0,
                    "last_seen_at": 1.0,
                    "used": True,
                }
            },
        },
        {
            "schema": "cqmgr.local-budget-state/v1",
            "entries": {
                "provider:" + ("0" * 64): {
                    "window_started_at": math.inf,
                    "last_seen_at": math.inf,
                    "used": 0,
                }
            },
        },
        {
            "schema": "cqmgr.local-budget-state/v1",
            "entries": {
                "provider:" + ("0" * 64): {
                    "window_started_at": 2.0,
                    "last_seen_at": 1.0,
                    "used": 0,
                }
            },
        },
        {
            "schema": "cqmgr.local-budget-state/v1",
            "entries": {
                "provider:" + ("0" * 64): {
                    "window_started_at": 1.0,
                    "last_seen_at": 1.0,
                    "used": 2,
                }
            },
        },
    ],
)
def test_every_corrupt_budget_entry_fails_closed(
    tmp_path: Path,
    state: dict[str, object],
) -> None:
    """Malformed keys, shapes, clocks, booleans, and overuse never reset budgets."""
    (tmp_path / "budgets.json").write_text(json.dumps(state))
    coordinator = SharedBudgetCoordinator(tmp_path, _limits())

    with pytest.raises(CoordinationUnavailableError):
        asyncio.run(
            coordinator.acquire(
                _request(),
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )


@given(
    capacity=st.integers(min_value=1, max_value=8),
    attempts=st.integers(min_value=1, max_value=12),
)
def test_budget_property_never_grants_more_than_capacity(
    capacity: int,
    attempts: int,
) -> None:
    """Any sequential acquisition trace conservatively caps durable grants."""
    with tempfile.TemporaryDirectory() as root:
        coordinator = SharedBudgetCoordinator(
            root,
            _limits(capacity=capacity, period_seconds=60),
        )
        grants = 0
        for _ in range(attempts):
            try:
                asyncio.run(
                    coordinator.acquire(
                        _request(),
                        deadline=time.monotonic() + 1,
                        cancellation=CancellationToken(),
                    )
                )
            except CoordinationDeadlineExceededError:
                continue
            grants += 1

        assert grants == min(capacity, attempts)
        with pytest.raises(ValueError, match="capacity"):
            asyncio.run(
                coordinator.acquire(
                    BudgetRequest(
                        "provider",
                        "project",
                        "billing",
                        units=capacity + 1,
                    ),
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


def test_coalesced_leader_work_is_bounded_by_caller_deadline(tmp_path: Path) -> None:
    """Leader work is cancelled and unpublished when its caller deadline expires."""
    coalescer = SharedReadCoalescer(tmp_path)

    async def slow() -> RedactedText:
        await asyncio.sleep(10)
        return RedactedText("late")

    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            coalescer.run(
                "slow-read",
                slow,
                deadline=time.monotonic() + 0.05,
                cancellation=CancellationToken(),
            )
        )

    assert list(tmp_path.glob("*.json")) == []


def test_coalesced_leader_work_is_bounded_by_cancellation(tmp_path: Path) -> None:
    """Cancelling a leader cancels its work and releases the identity for retry."""
    coalescer = SharedReadCoalescer(tmp_path)

    async def cancel_during_work() -> None:
        token = CancellationToken()

        async def slow() -> RedactedText:
            await asyncio.sleep(10)
            return RedactedText("late")

        task = asyncio.create_task(
            coalescer.run(
                "cancelled-leader",
                slow,
                deadline=time.monotonic() + 5,
                cancellation=token,
            )
        )
        await asyncio.sleep(0.02)
        token.cancel()
        with pytest.raises(CoordinationCancelledError):
            await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(cancel_during_work())
    assert list(tmp_path.glob("*.json")) == []


def test_coalescing_never_duplicates_work_when_a_waiter_deadline_expires(
    tmp_path: Path,
) -> None:
    """A waiting caller times out without becoming an overlapping successor leader."""
    first = SharedReadCoalescer(tmp_path)
    second = SharedReadCoalescer(tmp_path)
    calls = 0

    async def exercise() -> None:
        nonlocal calls
        started = asyncio.Event()

        async def leader_work() -> RedactedText:
            nonlocal calls
            calls += 1
            started.set()
            await asyncio.sleep(0.15)
            return RedactedText("leader")

        async def forbidden_duplicate() -> RedactedText:
            nonlocal calls
            calls += 1
            return RedactedText("duplicate")

        leader = asyncio.create_task(
            first.run(
                "one-read",
                leader_work,
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
        await started.wait()
        with pytest.raises(CoordinationDeadlineExceededError):
            await second.run(
                "one-read",
                forbidden_duplicate,
                deadline=time.monotonic() + 0.05,
                cancellation=CancellationToken(),
            )
        assert (await leader).value == "leader"

    asyncio.run(exercise())
    assert calls == 1


def test_coalescing_waiter_cancellation_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    """A follower remains cancellable while another leader holds the process lock."""
    first = SharedReadCoalescer(tmp_path)
    second = SharedReadCoalescer(tmp_path)

    async def exercise() -> int:
        started = asyncio.Event()
        release = asyncio.Event()

        async def leader_work() -> RedactedText:
            started.set()
            await release.wait()
            return RedactedText("leader")

        leader = asyncio.create_task(
            first.run(
                "shared-read",
                leader_work,
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
        await started.wait()
        token = CancellationToken()
        follower = asyncio.create_task(
            second.run(
                "shared-read",
                _safe_read,
                deadline=time.monotonic() + 1,
                cancellation=token,
            )
        )
        ticks = 0
        for _ in range(4):
            await asyncio.sleep(0.01)
            ticks += 1
        token.cancel()
        with pytest.raises(CoordinationCancelledError):
            await asyncio.wait_for(follower, timeout=0.5)
        release.set()
        await leader
        return ticks

    assert asyncio.run(exercise()) == _EXPECTED_EVENT_LOOP_TICKS


def test_windows_lock_error_mapping_propagates_fatal_os_errors() -> None:
    """Only actual Windows lock contention is normalized to BlockingIOError."""
    contention = OSError(errno.EACCES, "lock violation")
    fatal = OSError(errno.EIO, "device failure")

    with pytest.raises(BlockingIOError):
        locking_adapter._raise_windows_lock_error(contention)  # noqa: SLF001
    with pytest.raises(OSError, match="device failure") as caught:
        locking_adapter._raise_windows_lock_error(fatal)  # noqa: SLF001

    assert caught.value is fatal


def test_noncooperative_leader_timeout_returns_without_duplicate_work(
    tmp_path: Path,
) -> None:
    """A timed-out caller transfers ownership until stubborn read work really ends."""
    coalescer = SharedReadCoalescer(tmp_path)
    calls = 0

    async def exercise() -> None:
        nonlocal calls
        release = asyncio.Event()

        async def stubborn() -> RedactedText:
            nonlocal calls
            calls += 1
            await release.wait()
            return RedactedText("late")

        with pytest.raises(CoordinationDeadlineExceededError):
            await asyncio.wait_for(
                coalescer.run(
                    "stubborn-read",
                    stubborn,
                    deadline=time.monotonic() + 0.05,
                    cancellation=CancellationToken(),
                ),
                timeout=0.5,
            )
        with pytest.raises(CoordinationDeadlineExceededError):
            await coalescer.run(
                "stubborn-read",
                stubborn,
                deadline=time.monotonic() + 0.05,
                cancellation=CancellationToken(),
            )
        assert calls == 1
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(exercise())


def test_owned_sync_leader_retains_ownership_until_work_really_ends(
    tmp_path: Path,
) -> None:
    """Task cancellation cannot permit duplicate sync provider work in its thread."""
    coalescer = SharedReadCoalescer(tmp_path)
    release = threading.Event()
    started = threading.Event()
    calls = 0

    def sync_read() -> RedactedText:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=5)
        return RedactedText("thread-result")

    async def exercise() -> None:
        leader = asyncio.create_task(
            coalescer.run_sync(
                "threaded-read",
                sync_read,
                deadline=time.monotonic() + 5,
                cancellation=CancellationToken(),
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await leader

        with pytest.raises(CoordinationDeadlineExceededError):
            await coalescer.run_sync(
                "threaded-read",
                sync_read,
                deadline=time.monotonic() + 0.05,
                cancellation=CancellationToken(),
            )
        assert calls == 1
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(exercise())


def test_owned_sync_leadership_survives_leader_event_loop_shutdown(
    tmp_path: Path,
) -> None:
    """A successor process cannot duplicate a live worker after asyncio.run exits."""
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    release = context.Event()
    caller_finished = context.Event()
    process_release = context.Event()
    marker = tmp_path / "owned-sync-count"
    root = tmp_path / "owned-sync"
    leader = context.Process(
        target=_hold_owned_sync_leadership,
        args=(
            str(root),
            str(marker),
            started,
            release,
            caller_finished,
            process_release,
        ),
    )
    leader.start()
    assert started.wait(timeout=10)
    assert caller_finished.wait(timeout=10)
    follower = SharedReadCoalescer(root)

    def follower_work() -> RedactedText:
        _record_work(marker)
        return RedactedText("follower")

    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            follower.run_sync(
                "owned-sync-read",
                follower_work,
                deadline=time.monotonic() + 0.05,
                cancellation=CancellationToken(),
            )
        )
    assert marker.read_text() == "x"
    release.set()
    result = asyncio.run(
        follower.run_sync(
            "owned-sync-read",
            follower_work,
            deadline=time.monotonic() + 1,
            cancellation=CancellationToken(),
        )
    )
    process_release.set()
    leader.join(timeout=10)

    assert result.value == "follower"
    assert marker.read_text() == "xx"
    assert leader.exitcode == 0


@pytest.mark.parametrize("result_ttl", [0.0, 1.0])
def test_coalesced_cache_uses_strict_monotonic_age(
    tmp_path: Path,
    result_ttl: float,
) -> None:
    """Zero TTL and backward clock motion never reuse a stale shared result."""
    now = 100.0
    calls = 0

    def monotonic() -> float:
        return now

    coalescer = SharedReadCoalescer(
        tmp_path,
        result_ttl_seconds=result_ttl,
        monotonic=monotonic,
    )

    async def work() -> RedactedText:
        nonlocal calls
        calls += 1
        return RedactedText(f"result-{calls}")

    first = asyncio.run(
        coalescer.run(
            "monotonic-read",
            work,
            deadline=101,
            cancellation=CancellationToken(),
        )
    )
    if result_ttl > 0:
        now = 99.0
    second = asyncio.run(
        coalescer.run(
            "monotonic-read",
            work,
            deadline=101,
            cancellation=CancellationToken(),
        )
    )

    assert first.value == "result-1"
    assert second.value == "result-2"
    assert calls == _EXPECTED_TWO_CALLS


@pytest.mark.parametrize("control", ["cancellation", "deadline"])
def test_publication_rechecks_caller_controls_but_retains_safe_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    """A durable safe result remains reusable when its leader expires during publish."""
    token = CancellationToken()
    now = 100.0
    original_write = coordination_adapter._atomic_write_json  # noqa: SLF001

    def monotonic() -> float:
        return now

    def write_then_stop(root: Path, name: str, data: dict[str, object]) -> None:
        nonlocal now
        original_write(root, name, data)
        if data.get("status") == "done":
            if control == "cancellation":
                token.cancel()
            else:
                now = 101.0

    monkeypatch.setattr(coordination_adapter, "_atomic_write_json", write_then_stop)
    coalescer = SharedReadCoalescer(tmp_path, monotonic=monotonic)
    expected = (
        CoordinationCancelledError
        if control == "cancellation"
        else CoordinationDeadlineExceededError
    )

    with pytest.raises(expected):
        asyncio.run(
            coalescer.run(
                "published-read",
                _safe_read,
                deadline=101,
                cancellation=token,
            )
        )
    monkeypatch.setattr(coordination_adapter, "_atomic_write_json", original_write)
    now = 100.1
    result = asyncio.run(
        coalescer.run(
            "published-read",
            _safe_read,
            deadline=101,
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


def test_process_death_releases_coalesced_leadership_for_retry(tmp_path: Path) -> None:
    """A crashed leader cannot fence out or publish over its healthy successor."""
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    process = context.Process(
        target=_hold_coalesced_leadership,
        args=(str(tmp_path), ready),
    )
    process.start()
    assert ready.get(timeout=10) == "leading"
    process.terminate()
    process.join(timeout=10)

    result = asyncio.run(
        SharedReadCoalescer(tmp_path).run(
            "crashed-read",
            _safe_read,
            deadline=time.monotonic() + 1,
            cancellation=CancellationToken(),
        )
    )

    assert result.value == "safe"
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


def test_sync_lock_contention_clamps_poll_to_remaining_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synchronous polling never widens the caller's monotonic deadline."""
    path = tmp_path / "sync-deadline.lock"
    held = InterprocessFileLock(path)
    waiting = InterprocessFileLock(path, poll_seconds=0.2)
    held.acquire()
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(locking_adapter.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(locking_adapter.time, "sleep", sleep)

    with pytest.raises(CoordinationDeadlineExceededError):
        waiting.acquire(deadline=100.01)

    held.release()
    assert sleeps == pytest.approx([0.01])


def test_async_lock_checks_free_lock_deadline_and_cancellation_first(
    tmp_path: Path,
) -> None:
    """An available lock still cannot widen an elapsed or cancelled caller."""
    lock = InterprocessFileLock(tmp_path / "async.lock")
    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            lock.acquire_async(
                deadline=time.monotonic() - 1,
                cancellation=CancellationToken(),
            )
        )
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CoordinationCancelledError):
        asyncio.run(
            lock.acquire_async(
                deadline=time.monotonic() + 1,
                cancellation=token,
            )
        )
    lock.acquire()
    lock.release()


def test_async_lock_contention_does_not_block_event_loop(tmp_path: Path) -> None:
    """A contended file lock polls asynchronously until the holder releases."""
    path = tmp_path / "contended.lock"
    held = InterprocessFileLock(path)
    waiting = InterprocessFileLock(path)
    held.acquire()

    async def exercise() -> int:
        task = asyncio.create_task(
            waiting.acquire_async(
                deadline=time.monotonic() + 1,
                cancellation=CancellationToken(),
            )
        )
        ticks = 0
        for _ in range(4):
            await asyncio.sleep(0.01)
            ticks += 1
        held.release()
        await task
        waiting.release()
        return ticks

    assert asyncio.run(exercise()) == _EXPECTED_EVENT_LOOP_TICKS
