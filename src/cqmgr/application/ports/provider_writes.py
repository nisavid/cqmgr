"""Narrow quota-preference mutation and uncertainty-reconciliation ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity, QuotaQuantity
from cqmgr.domain.results import StableSymbol


class QuotaPreferenceWriteAction(StrEnum):
    """Exact provider operation selected from freshly revalidated evidence."""

    CREATE = "create"
    AMEND = "amend"


@dataclass(frozen=True, slots=True)
class QuotaPreferenceWrite:
    """One exact-slice provider write with no retry or validate-only control."""

    child_id: str
    slice_identity: EffectiveQuotaSliceIdentity
    target: QuotaQuantity
    preference_identity: str
    action: QuotaPreferenceWriteAction
    current_etag: str | None
    contact_value: str = field(repr=False)
    acknowledgements: tuple[StableSymbol, ...] = ()

    def __post_init__(self) -> None:
        """Reject raw, incomplete, or alternate mutation controls."""
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "write child_id must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.slice_identity, EffectiveQuotaSliceIdentity):
            msg = "write slice_identity must be exact"
            raise TypeError(msg)
        if not isinstance(self.target, QuotaQuantity):
            msg = "write target must be a QuotaQuantity"
            raise TypeError(msg)
        if (
            not isinstance(self.preference_identity, str)
            or not self.preference_identity
        ):
            msg = "write preference_identity must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.action, QuotaPreferenceWriteAction):
            msg = "write action must be a QuotaPreferenceWriteAction"
            raise TypeError(msg)
        if self.current_etag is not None and (
            not isinstance(self.current_etag, str) or not self.current_etag
        ):
            msg = "write current etag must be None or non-empty"
            raise ValueError(msg)
        if not isinstance(self.contact_value, str) or not self.contact_value:
            msg = "write contact value must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.acknowledgements, tuple) or any(
            not isinstance(item, StableSymbol) for item in self.acknowledgements
        ):
            msg = "write acknowledgements must be StableSymbol values"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class QuotaPreferenceWriteResult:
    """One conclusive provider dispatch classification."""

    accepted: bool
    outcome: StableSymbol

    def __post_init__(self) -> None:
        """Require one conclusive typed provider result."""
        if not isinstance(self.accepted, bool):
            msg = "write result accepted must be bool"
            raise TypeError(msg)
        if not isinstance(self.outcome, StableSymbol):
            msg = "write result outcome must be a StableSymbol"
            raise TypeError(msg)


class UnknownWriteResolution(StrEnum):
    """Conclusive read-after-unknown proof at the bound identity."""

    ACCEPTED = "accepted"
    FAILED = "failed"
    UNRESOLVED = "unresolved"


class QuotaPreferenceWriter(Protocol):
    """Dispatch each already-durable exact-slice intent at most once."""

    async def dispatch(
        self, request: QuotaPreferenceWrite
    ) -> QuotaPreferenceWriteResult:
        """Create or amend once without generic retry."""
        ...


class QuotaPreferenceUnknownResolver(Protocol):
    """Read one deterministic identity after transport uncertainty."""

    async def resolve_unknown(
        self, request: QuotaPreferenceWrite
    ) -> UnknownWriteResolution:
        """Classify bound intent acceptance without issuing another write."""
        ...
