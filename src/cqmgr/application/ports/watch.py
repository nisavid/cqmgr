"""Typed read-only lifecycle observation and durable Watch-control ports."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.domain.diagnostics import Diagnostic
from cqmgr.domain.quotas import QuotaQuantity
from cqmgr.domain.status import QuotaRequestStatus
from cqmgr.domain.time import require_utc
from cqmgr.domain.watch import (
    WatchCheckpoint,
    WatchChildIdentity,
    WatchResumeClaims,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.application.ports.secrets import SecretValue


class WatchObservationTransientError(Exception):
    """One documented retryable provider observation failure."""

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        """Retain optional provider retry guidance without provider details."""
        super().__init__("transient Watch observation failure")
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not math.isfinite(retry_after_seconds)
            or retry_after_seconds < 0
        ):
            msg = "Watch transient retry-after must be finite non-negative seconds"
            raise ValueError(msg)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class WatchObservationRequest:
    """One exact accepted child read under the caller's shared deadline."""

    child: WatchChildIdentity
    deadline: float
    cancellation: CancellationToken

    def __post_init__(self) -> None:
        """Reject unbounded, mutable, or non-watchable read inputs."""
        if not isinstance(self.child, WatchChildIdentity) or not self.child.watchable:
            msg = "Watch observation requires one accepted child"
            raise ValueError(msg)
        if (
            isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not math.isfinite(self.deadline)
        ):
            msg = "Watch observation deadline must be finite"
            raise ValueError(msg)
        if not isinstance(self.cancellation, CancellationToken):
            msg = "Watch observation requires a CancellationToken"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class WatchObservation:
    """One normalized preference plus effective-quota observation."""

    status: QuotaRequestStatus
    preference_target: QuotaQuantity
    etag: str | None
    trace_id: str | None
    observed_at: datetime
    diagnostics: tuple[Diagnostic, ...] = ()
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        """Keep provider lifecycle, target, lineage, and retry evidence explicit."""
        if not isinstance(self.status, QuotaRequestStatus):
            msg = "Watch observation status must be a QuotaRequestStatus"
            raise TypeError(msg)
        if not isinstance(self.preference_target, QuotaQuantity):
            msg = "Watch preference target must be a QuotaQuantity"
            raise TypeError(msg)
        for name, value in (("etag", self.etag), ("trace_id", self.trace_id)):
            if value is not None and (not isinstance(value, str) or not value):
                msg = f"Watch observation {name} must be None or non-empty"
                raise ValueError(msg)
        if self.etag is None and self.trace_id is None:
            msg = "Watch observation requires an etag or stable trace ID"
            raise ValueError(msg)
        require_utc(self.observed_at, "observed_at")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            msg = "Watch observation diagnostics must be typed"
            raise TypeError(msg)
        if self.retry_after_seconds is not None and (
            isinstance(self.retry_after_seconds, bool)
            or not isinstance(self.retry_after_seconds, (int, float))
            or not math.isfinite(self.retry_after_seconds)
            or self.retry_after_seconds < 0
        ):
            msg = "Watch retry-after must be finite non-negative seconds"
            raise ValueError(msg)


class WatchObservationReader(Protocol):
    """Read one exact preference and effective quota without mutation capability."""

    async def observe(self, request: WatchObservationRequest) -> WatchObservation:
        """Return normalized lifecycle evidence for one accepted child."""
        ...


class WatchCheckpointRepositoryStatus(StrEnum):
    """Closed outcomes for authenticated durable Watch checkpoints."""

    STORED = "stored"
    AVAILABLE = "available"
    MISSING = "missing"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WatchCheckpointRepositoryOutcome:
    """One checkpoint persistence result."""

    status: WatchCheckpointRepositoryStatus
    checkpoint: WatchCheckpoint | None = None


class WatchCheckpointRepository(Protocol):
    """Persist authenticated immutable Watch observation checkpoints."""

    def save(
        self,
        checkpoint: WatchCheckpoint,
        authentication_key: SecretValue,
    ) -> WatchCheckpointRepositoryOutcome:
        """Create one immutable checkpoint."""
        ...

    def load(
        self,
        checkpoint_id: str,
        authentication_key: SecretValue,
    ) -> WatchCheckpointRepositoryOutcome:
        """Load and authenticate one exact checkpoint."""
        ...


class WatchResumeCodec(Protocol):
    """Authenticate opaque V1 Watch resume controls."""

    def encode(self, claims: WatchResumeClaims, key: SecretValue) -> str:
        """Return one authenticated non-secret opaque token."""
        ...

    def decode(self, token: str, key: SecretValue) -> WatchResumeClaims:
        """Authenticate and decode one exact supported token."""
        ...


class WatchClock(Protocol):
    """Supply wall, monotonic, and virtualizable sleep boundaries."""

    def now(self) -> datetime:
        """Return aware UTC wall time."""
        ...

    def monotonic(self) -> float:
        """Return monotonic seconds."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait without exceeding the caller-provided duration."""
        ...


class WatchStreamIdSource(Protocol):
    """Generate non-secret opaque per-run identities."""

    def __call__(self) -> str:
        """Return one nonempty stream identity."""
        ...
