"""Pure single-use quota request plan ledger transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.plans import PlanLedgerState
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime


class PlanLedgerDecision(StrEnum):
    """Outcome of one deterministic consumption-ledger transition."""

    ACCEPTED = "accepted"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PlanLedgerTransition:
    """One decision and the state that must remain durable afterward."""

    decision: PlanLedgerDecision
    record: PlanLedgerRecord


@dataclass(frozen=True, slots=True)
class PlanLedgerRecord:
    """Pure durable state for one locally authenticated plan digest."""

    state: PlanLedgerState
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    owner_pid: int | None = None
    reason: StableSymbol | None = None

    @classmethod
    def available(cls) -> PlanLedgerRecord:
        """Build the initial reusable state."""
        return cls(PlanLedgerState.AVAILABLE)

    def recover(
        self, now: datetime, owner_alive: Callable[[int], bool]
    ) -> PlanLedgerTransition:
        """Release stale pre-dispatch work and quarantine ambiguous dispatch."""
        require_utc(now, "now")
        if (
            self.state is PlanLedgerState.LEASED
            and self.lease_expires_at is not None
            and now >= self.lease_expires_at
        ):
            return PlanLedgerTransition(PlanLedgerDecision.EXPIRED, self.available())
        if (
            self.state is PlanLedgerState.DISPATCHED
            and self.owner_pid is not None
            and not owner_alive(self.owner_pid)
        ):
            return PlanLedgerTransition(
                PlanLedgerDecision.ACCEPTED,
                PlanLedgerRecord(
                    PlanLedgerState.QUARANTINED,
                    lease_token=self.lease_token,
                    owner_pid=self.owner_pid,
                    reason=StableSymbol("ambiguous-dispatch"),
                ),
            )
        return PlanLedgerTransition(PlanLedgerDecision.IDEMPOTENT, self)

    def acquire(
        self,
        *,
        token: str,
        expires_at: datetime,
        owner_pid: int,
    ) -> PlanLedgerTransition:
        """Create one lease only from available state."""
        require_utc(expires_at, "expires_at")
        if self.state is not PlanLedgerState.AVAILABLE:
            return PlanLedgerTransition(PlanLedgerDecision.CONFLICT, self)
        return PlanLedgerTransition(
            PlanLedgerDecision.ACCEPTED,
            PlanLedgerRecord(
                PlanLedgerState.LEASED,
                lease_token=token,
                lease_expires_at=expires_at,
                owner_pid=owner_pid,
            ),
        )

    def dispatch(self, *, token: str, now: datetime) -> PlanLedgerTransition:
        """Durably consume the exact unexpired lease before provider dispatch."""
        require_utc(now, "now")
        if self.state is PlanLedgerState.DISPATCHED and self.lease_token == token:
            return PlanLedgerTransition(PlanLedgerDecision.IDEMPOTENT, self)
        if self.state is not PlanLedgerState.LEASED or self.lease_token != token:
            return PlanLedgerTransition(PlanLedgerDecision.CONFLICT, self)
        if self.lease_expires_at is None or now >= self.lease_expires_at:
            return PlanLedgerTransition(PlanLedgerDecision.EXPIRED, self.available())
        return PlanLedgerTransition(
            PlanLedgerDecision.ACCEPTED,
            PlanLedgerRecord(
                PlanLedgerState.DISPATCHED,
                lease_token=token,
                owner_pid=self.owner_pid,
            ),
        )

    def complete(self, *, token: str) -> PlanLedgerTransition:
        """Close the exact dispatched plan after its durable terminal result."""
        if self.state is PlanLedgerState.CONSUMED and self.lease_token == token:
            return PlanLedgerTransition(PlanLedgerDecision.IDEMPOTENT, self)
        if self.state is not PlanLedgerState.DISPATCHED or self.lease_token != token:
            return PlanLedgerTransition(PlanLedgerDecision.CONFLICT, self)
        return PlanLedgerTransition(
            PlanLedgerDecision.ACCEPTED,
            PlanLedgerRecord(
                PlanLedgerState.CONSUMED,
                lease_token=token,
                owner_pid=self.owner_pid,
            ),
        )

    def quarantine(self, *, token: str, reason: StableSymbol) -> PlanLedgerTransition:
        """Permanently block one leased or dispatched ambiguous plan."""
        if (
            self.state
            not in {
                PlanLedgerState.LEASED,
                PlanLedgerState.DISPATCHED,
            }
            or self.lease_token != token
        ):
            return PlanLedgerTransition(PlanLedgerDecision.CONFLICT, self)
        return PlanLedgerTransition(
            PlanLedgerDecision.ACCEPTED,
            PlanLedgerRecord(
                PlanLedgerState.QUARANTINED,
                lease_token=token,
                owner_pid=self.owner_pid,
                reason=reason,
            ),
        )
