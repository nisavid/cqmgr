"""Pure single-use quota request plan ledger transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.plans import PlanLedgerState
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
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
    reason: StableSymbol | None = None

    def __post_init__(self) -> None:
        """Require one unambiguous durable shape for each ledger state."""
        if not isinstance(self.state, PlanLedgerState):
            msg = "plan ledger state must be a PlanLedgerState"
            raise TypeError(msg)
        if self.lease_token is not None and (
            not isinstance(self.lease_token, str) or not self.lease_token
        ):
            msg = "plan ledger lease token must be non-empty"
            raise ValueError(msg)
        if self.lease_expires_at is not None:
            require_utc(self.lease_expires_at, "lease_expires_at")
        if self.reason is not None and not isinstance(self.reason, StableSymbol):
            msg = "plan ledger reason must be a StableSymbol"
            raise TypeError(msg)
        if self.state is PlanLedgerState.AVAILABLE and any(
            value is not None
            for value in (self.lease_token, self.lease_expires_at, self.reason)
        ):
            msg = "available plan ledger state cannot retain lease metadata"
            raise ValueError(msg)
        if self.state in {PlanLedgerState.LEASED, PlanLedgerState.DISPATCHED} and (
            self.lease_token is None
            or self.lease_expires_at is None
            or self.reason is not None
        ):
            msg = "leased and dispatched states require exact lease metadata"
            raise ValueError(msg)
        if self.state is PlanLedgerState.CONSUMED and (
            self.lease_token is None
            or self.lease_expires_at is not None
            or self.reason is not None
        ):
            msg = "consumed state requires only its exact lease token"
            raise ValueError(msg)
        if self.state is PlanLedgerState.QUARANTINED and (
            self.lease_expires_at is not None or self.reason is None
        ):
            msg = "quarantined state requires a reason and no live deadline"
            raise ValueError(msg)

    @classmethod
    def available(cls) -> PlanLedgerRecord:
        """Build the initial reusable state."""
        return cls(PlanLedgerState.AVAILABLE)

    @classmethod
    def quarantined(cls, reason: StableSymbol) -> PlanLedgerRecord:
        """Build a terminal fail-closed state without dispatch authority."""
        return cls(PlanLedgerState.QUARANTINED, reason=reason)

    def recover(self, now: datetime) -> PlanLedgerTransition:
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
            and self.lease_expires_at is not None
            and now >= self.lease_expires_at
        ):
            return PlanLedgerTransition(
                PlanLedgerDecision.ACCEPTED,
                PlanLedgerRecord(
                    PlanLedgerState.QUARANTINED,
                    lease_token=self.lease_token,
                    reason=StableSymbol("ambiguous-dispatch"),
                ),
            )
        return PlanLedgerTransition(PlanLedgerDecision.IDEMPOTENT, self)

    def acquire(
        self,
        *,
        token: str,
        expires_at: datetime,
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
                lease_expires_at=self.lease_expires_at,
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
                reason=reason,
            ),
        )
