"""Application contracts for installation-local coordination."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Lock
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cqmgr.domain.redaction import RedactedText


class CoordinationCancelledError(Exception):
    """The caller cancelled local coordination before work began."""


class CoordinationDeadlineExceededError(Exception):
    """The caller deadline cannot accommodate the requested local work."""


class CancellationToken:
    """One explicit thread-safe cancellation signal shared by application work."""

    def __init__(self) -> None:
        """Create a token in the active state."""
        self._event = Event()
        self._waiters_lock = Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = (
            set()
        )

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Request cooperative cancellation without implying provider reversal."""
        with self._waiters_lock:
            self._event.set()
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for loop, waiter in waiters:
            try:
                loop.call_soon_threadsafe(_complete_waiter, waiter)
            except RuntimeError:
                continue

    def raise_if_cancelled(self) -> None:
        """Stop before dispatch when cancellation has already been requested."""
        if self.cancelled:
            raise CoordinationCancelledError

    async def wait(self) -> None:
        """Wait asynchronously for cancellation from any thread."""
        if self.cancelled:
            return
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        entry = (loop, future)
        with self._waiters_lock:
            if self._event.is_set():
                return
            self._waiters.add(entry)
        try:
            await future
        finally:
            with self._waiters_lock:
                self._waiters.discard(entry)


def _complete_waiter(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


class BudgetScope(StrEnum):
    """Independent local request-budget axes."""

    PROVIDER = "provider"
    PROJECT = "project"
    ADC_QUOTA_PROJECT = "adc-quota-project"


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    """A conservative fixed-window request limit."""

    capacity: int
    period_seconds: float

    def __post_init__(self) -> None:
        """Require a non-zero bounded accounting window."""
        if (
            isinstance(self.capacity, bool)
            or not isinstance(self.capacity, int)
            or self.capacity < 1
        ):
            msg = "budget capacity must be a positive integer"
            raise ValueError(msg)
        if (
            isinstance(self.period_seconds, bool)
            or not isinstance(self.period_seconds, (int, float))
            or not math.isfinite(self.period_seconds)
            or self.period_seconds <= 0
        ):
            msg = "budget period must be positive seconds"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BudgetRequest:
    """One request charged atomically against all applicable local axes."""

    provider: str
    project: str
    adc_quota_project: str | None
    units: int = 1

    def __post_init__(self) -> None:
        """Reject absent identities and non-conservative request units."""
        if any(
            not isinstance(value, str) or not value
            for value in (self.provider, self.project)
        ) or (
            self.adc_quota_project is not None
            and (
                not isinstance(self.adc_quota_project, str)
                or not self.adc_quota_project
            )
        ):
            msg = "budget identities must be non-empty strings"
            raise ValueError(msg)
        if (
            isinstance(self.units, bool)
            or not isinstance(self.units, int)
            or self.units < 1
        ):
            msg = "budget units must be a positive integer"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BudgetGrant:
    """A durable conservative charge made before one provider dispatch."""

    charged_at: float
    request: BudgetRequest


class BudgetCommitUnknownError(Exception):
    """A visible budget charge has an unconfirmed durable commit."""

    def __init__(self, possible_grant: BudgetGrant) -> None:
        """Retain the possible charge details that forbid a blind retry."""
        super().__init__("local budget charge durability is unknown")
        self.possible_grant = possible_grant


class BudgetCoordinator(Protocol):
    """Coordinate request charges across local cqmgr processes."""

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Commit a charge or report typed ambiguity with possible grant details."""
        ...


class JitterSource(Protocol):
    """Supply bounded jitter through an injectable deterministic seam."""

    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        """Return a non-negative delay no larger than the supplied bound."""
        ...


class ReadCoalescer(Protocol):
    """Combine equivalent normalized safe reads across local processes."""

    async def run(
        self,
        identity: str,
        work: Callable[[], Awaitable[RedactedText]],
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> RedactedText:
        """Return the leader's safe result to concurrent equivalent callers."""
        ...

    async def run_sync(
        self,
        identity: str,
        work: Callable[[], RedactedText],
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> RedactedText:
        """Fence real worker completion when adapting a synchronous read."""
        ...
