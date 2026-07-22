"""Typed application boundary for authenticated single-use plan storage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from cqmgr.domain.time import require_utc

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cqmgr.application.ports.secrets import SecretValue
    from cqmgr.domain.plans import PlanLedgerState
    from cqmgr.domain.results import StableSymbol


class PlanRepositoryStatus(StrEnum):
    """Closed result statuses from local and exported plan operations."""

    STORED = "stored"
    EXPORTED = "exported"
    AVAILABLE = "available"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    CONSUMED = "consumed"
    QUARANTINED = "quarantined"
    MISSING = "missing"
    CONFLICT = "conflict"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EncodedPlan:
    """Portable canonical bytes and their local digest handle."""

    bytes: bytes = field(repr=False)
    digest: str


@dataclass(frozen=True, slots=True)
class PlanLease:
    """Exclusive bounded authority to dispatch one local plan."""

    digest: str
    token: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        """Require a non-empty opaque lease and UTC expiry."""
        if (
            not isinstance(self.digest, str)
            or _SHA256_DIGEST.fullmatch(self.digest) is None
        ):
            msg = "lease digest must use sha256"
            raise ValueError(msg)
        if not isinstance(self.token, str) or not self.token:
            msg = "lease token must be non-empty"
            raise ValueError(msg)
        require_utc(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class PlanRepositoryOutcome:
    """Non-throwing local plan repository result."""

    status: PlanRepositoryStatus
    plan_bytes: bytes | None = field(default=None, repr=False)
    state: PlanLedgerState | None = None
    lease: PlanLease | None = field(default=None, repr=False)
    reason: StableSymbol | None = None
    authenticated: bool | None = None


class PlanRepository(Protocol):
    """Content-addressed authenticated plan and single-use ledger port."""

    def store(
        self, plan: EncodedPlan, authentication_key: SecretValue
    ) -> PlanRepositoryOutcome:
        """Store verified canonical bytes by digest."""

    def load(self, digest: str, now: datetime) -> PlanRepositoryOutcome:
        """Load one local plan and its recovered ledger state."""

    def export(self, plan: EncodedPlan, path: Path) -> PlanRepositoryOutcome:
        """Atomically export exact portable bytes."""

    def read_export(self, path: Path) -> PlanRepositoryOutcome:
        """Read and validate an explicit plan file for review."""

    def acquire_lease(
        self,
        digest: str,
        now: datetime,
        *,
        lease_duration: timedelta = timedelta(minutes=1),
    ) -> PlanRepositoryOutcome:
        """Acquire one bounded exclusive pre-dispatch lease."""

    def mark_dispatched(self, lease: PlanLease, now: datetime) -> PlanRepositoryOutcome:
        """Durably consume the plan immediately before dispatch."""

    def complete(self, lease: PlanLease, now: datetime) -> PlanRepositoryOutcome:
        """Record a durable terminal post-dispatch outcome."""

    def quarantine(
        self, lease: PlanLease, reason: StableSymbol, now: datetime
    ) -> PlanRepositoryOutcome:
        """Quarantine an interrupted or ambiguous dispatch."""
