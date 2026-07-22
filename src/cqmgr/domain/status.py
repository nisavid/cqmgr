"""Orthogonal quota-request status and derived conditions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.quotas import QuotaQuantity
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from datetime import datetime


class Reconciliation(StrEnum):
    """Provider reconciliation state projected into product semantics."""

    SUBMITTED = "submitted"
    RECONCILING = "reconciling"
    SETTLED = "settled"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class GrantSatisfaction(StrEnum):
    """How much of the requested absolute change was granted."""

    UNKNOWN = "unknown"
    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"


class EffectiveConfirmation(StrEnum):
    """Whether fresh effective quota confirms the settled grant."""

    UNOBSERVED = "unobserved"
    STALE = "stale"
    MISMATCH = "mismatch"
    CONFIRMED = "confirmed"


class Headline(StrEnum):
    """Concise human status derived without replacing the axes."""

    SUBMITTED = "submitted"
    RECONCILING = "reconciling"
    REQUEST_SETTLED = "request-settled"
    GRANTED = "granted"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"
    FULFILLED = "fulfilled"


class WatchCondition(StrEnum):
    """The explicit lifecycle condition selected by a Watch."""

    GRANTED = "granted"
    FULFILLED = "fulfilled"


class WatchDisposition(StrEnum):
    """Whether current evidence reaches, refutes, or may yet reach a condition."""

    PENDING = "pending"
    REACHED = "reached"
    UNMET = "unmet"


def _same_unit(*quantities: QuotaQuantity | None) -> bool:
    units = {quantity.unit for quantity in quantities if quantity is not None}
    return len(units) <= 1


def _require_quantity(value: object, field_name: str, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if not isinstance(value, QuotaQuantity):
        msg = f"{field_name} must be a QuotaQuantity"
        raise TypeError(msg)


def _validate_evidence(  # noqa: PLR0913
    *,
    baseline: QuotaQuantity | None,
    desired: QuotaQuantity,
    granted: QuotaQuantity | None,
    effective: QuotaQuantity | None,
    status_observed_at: datetime,
    effective_observed_at: datetime | None,
) -> None:
    _require_quantity(baseline, "baseline")
    _require_quantity(desired, "desired", optional=False)
    _require_quantity(granted, "granted")
    _require_quantity(effective, "effective")
    require_utc(status_observed_at, "status_observed_at")
    if effective is not None and effective_observed_at is None:
        msg = (
            "effective and effective_observed_at must both be present or both be absent"
        )
        raise ValueError(msg)
    if effective is None and effective_observed_at is not None:
        msg = (
            "effective and effective_observed_at must both be present or both be absent"
        )
        raise ValueError(msg)
    if effective_observed_at is not None:
        require_utc(effective_observed_at, "effective_observed_at")
    if not _same_unit(baseline, desired, granted, effective):
        msg = "status quantities must use one explicit unit"
        raise ValueError(msg)


def derive_grant_satisfaction(
    baseline: QuotaQuantity | None,
    desired: QuotaQuantity,
    granted: QuotaQuantity | None,
) -> GrantSatisfaction:
    """Classify an absolute grant relative to the pre-request baseline."""
    if granted is None or not _same_unit(baseline, desired, granted):
        return GrantSatisfaction.UNKNOWN
    if granted.value == desired.value:
        return GrantSatisfaction.FULL
    if baseline is None:
        return GrantSatisfaction.UNKNOWN
    if granted.value == baseline.value:
        return GrantSatisfaction.NONE
    lower, upper = sorted((baseline.value, desired.value))
    if lower < granted.value < upper:
        return GrantSatisfaction.PARTIAL
    return GrantSatisfaction.UNKNOWN


def _derive_effective_confirmation(
    granted: QuotaQuantity | None,
    effective: QuotaQuantity | None,
    status_observed_at: datetime,
    effective_observed_at: datetime | None,
) -> EffectiveConfirmation:
    if effective is None or effective_observed_at is None or granted is None:
        return EffectiveConfirmation.UNOBSERVED
    if effective_observed_at < status_observed_at:
        return EffectiveConfirmation.STALE
    if not _same_unit(granted, effective) or effective.value != granted.value:
        return EffectiveConfirmation.MISMATCH
    return EffectiveConfirmation.CONFIRMED


def _derive_axes(  # noqa: PLR0913
    reconciliation: Reconciliation,
    baseline: QuotaQuantity | None,
    desired: QuotaQuantity,
    granted: QuotaQuantity | None,
    effective: QuotaQuantity | None,
    status_observed_at: datetime,
    effective_observed_at: datetime | None,
) -> tuple[GrantSatisfaction, EffectiveConfirmation]:
    if reconciliation is not Reconciliation.SETTLED:
        return GrantSatisfaction.UNKNOWN, EffectiveConfirmation.UNOBSERVED
    return (
        derive_grant_satisfaction(baseline, desired, granted),
        _derive_effective_confirmation(
            granted, effective, status_observed_at, effective_observed_at
        ),
    )


@dataclass(frozen=True, slots=True)
class QuotaRequestStatus:
    """Separate request axes with all known absolute quantities."""

    reconciliation: Reconciliation
    grant_satisfaction: GrantSatisfaction
    effective_confirmation: EffectiveConfirmation
    baseline: QuotaQuantity | None
    desired: QuotaQuantity
    granted: QuotaQuantity | None
    effective: QuotaQuantity | None
    status_observed_at: datetime
    effective_observed_at: datetime | None
    provider_reconciliation: ProviderSymbol[Reconciliation] | None = None

    def __post_init__(self) -> None:
        """Reject invalid types and axes not exactly derived from the evidence."""
        if not isinstance(self.reconciliation, Reconciliation):
            msg = "reconciliation must be a Reconciliation"
            raise TypeError(msg)
        if not isinstance(self.grant_satisfaction, GrantSatisfaction):
            msg = "grant_satisfaction must be a GrantSatisfaction"
            raise TypeError(msg)
        if not isinstance(self.effective_confirmation, EffectiveConfirmation):
            msg = "effective_confirmation must be an EffectiveConfirmation"
            raise TypeError(msg)
        _validate_evidence(
            baseline=self.baseline,
            desired=self.desired,
            granted=self.granted,
            effective=self.effective,
            status_observed_at=self.status_observed_at,
            effective_observed_at=self.effective_observed_at,
        )
        if self.provider_reconciliation is not None:
            if not isinstance(self.provider_reconciliation, ProviderSymbol):
                msg = "provider_reconciliation must be a ProviderSymbol"
                raise TypeError(msg)
            if self.provider_reconciliation.enum_type is not Reconciliation:
                msg = "provider_reconciliation must use the Reconciliation enum type"
                raise TypeError(msg)
            provider_known = self.provider_reconciliation.known
            if self.reconciliation is not (provider_known or Reconciliation.UNKNOWN):
                msg = "reconciliation must match provider_reconciliation"
                raise ValueError(msg)
        expected_axes = _derive_axes(
            self.reconciliation,
            self.baseline,
            self.desired,
            self.granted,
            self.effective,
            self.status_observed_at,
            self.effective_observed_at,
        )
        if (
            self.grant_satisfaction,
            self.effective_confirmation,
        ) != expected_axes:
            msg = "status axes must exactly match the derived evidence"
            raise ValueError(msg)

    @classmethod
    def derive(  # noqa: PLR0913
        cls,
        *,
        reconciliation: Reconciliation | ProviderSymbol[Reconciliation],
        baseline: QuotaQuantity | None,
        desired: QuotaQuantity,
        granted: QuotaQuantity | None,
        effective: QuotaQuantity | None,
        status_observed_at: datetime,
        effective_observed_at: datetime | None,
    ) -> QuotaRequestStatus:
        """Derive status axes from authoritative quantities and source times."""
        _validate_evidence(
            baseline=baseline,
            desired=desired,
            granted=granted,
            effective=effective,
            status_observed_at=status_observed_at,
            effective_observed_at=effective_observed_at,
        )
        if isinstance(reconciliation, ProviderSymbol):
            if reconciliation.enum_type is not Reconciliation:
                msg = "reconciliation ProviderSymbol must use Reconciliation enum type"
                raise TypeError(msg)
            provider_known = reconciliation.known
            reconciliation_state = provider_known or Reconciliation.UNKNOWN
            provider_reconciliation = reconciliation
        elif isinstance(reconciliation, Reconciliation):
            reconciliation_state = reconciliation
            provider_reconciliation = None
        else:
            msg = "reconciliation must be a Reconciliation or ProviderSymbol"
            raise TypeError(msg)
        grant_satisfaction, effective_confirmation = _derive_axes(
            reconciliation_state,
            baseline,
            desired,
            granted,
            effective,
            status_observed_at,
            effective_observed_at,
        )
        return cls(
            reconciliation=reconciliation_state,
            grant_satisfaction=grant_satisfaction,
            effective_confirmation=effective_confirmation,
            baseline=baseline,
            desired=desired,
            granted=granted,
            effective=effective,
            status_observed_at=status_observed_at,
            effective_observed_at=effective_observed_at,
            provider_reconciliation=provider_reconciliation,
        )

    @property
    def is_granted(self) -> bool:
        """Whether reconciliation settled at the full absolute target."""
        return (
            self.reconciliation is Reconciliation.SETTLED
            and self.grant_satisfaction is GrantSatisfaction.FULL
        )

    @property
    def is_fulfilled(self) -> bool:
        """Whether a full grant is freshly effective at the same value."""
        return (
            self.is_granted
            and self.effective_confirmation is EffectiveConfirmation.CONFIRMED
            and self.granted == self.desired == self.effective
        )

    @property
    def headline(self) -> Headline:
        """Derive the concise presentation headline."""
        if self.reconciliation is Reconciliation.FAILED:
            return Headline.FAILED
        if self.reconciliation is Reconciliation.SUPERSEDED:
            return Headline.SUPERSEDED
        if self.is_fulfilled:
            return Headline.FULFILLED
        if self.is_granted:
            return Headline.GRANTED
        if self.reconciliation is Reconciliation.SETTLED:
            return Headline.REQUEST_SETTLED
        return Headline(self.reconciliation.value)

    def watch(self, condition: WatchCondition) -> WatchDisposition:
        """Classify the selected Watch condition from current evidence."""
        if not isinstance(condition, WatchCondition):
            msg = "condition must be a WatchCondition"
            raise TypeError(msg)
        if condition is WatchCondition.GRANTED and self.is_granted:
            return WatchDisposition.REACHED
        if condition is WatchCondition.FULFILLED and self.is_fulfilled:
            return WatchDisposition.REACHED
        if self.reconciliation in {
            Reconciliation.FAILED,
            Reconciliation.SUPERSEDED,
        }:
            return WatchDisposition.UNMET
        if (
            self.reconciliation is Reconciliation.SETTLED
            and self.granted is not None
            and self.granted.value != self.desired.value
        ):
            return WatchDisposition.UNMET
        return WatchDisposition.PENDING
