"""Framework-free quota request plan values and applicability rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    QuotaQuantity,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.schemas import QUOTA_REQUEST_PLAN_SCHEMA
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from datetime import datetime

PLAN_SCHEMA = QUOTA_REQUEST_PLAN_SCHEMA
PLAN_LIFETIME = timedelta(minutes=15)
_LOWER_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PlanLedgerState(StrEnum):
    """Durable single-use state of one locally issued plan."""

    AVAILABLE = "available"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    CONSUMED = "consumed"
    QUARANTINED = "quarantined"


class PlanIncapability(StrEnum):
    """Stable reasons trustworthy plan contents cannot be Applied."""

    EXPIRED = "expired"
    FOREIGN_OR_UNAUTHENTICATED = "foreign-or-unauthenticated"
    INSTALLATION_MISMATCH = "installation-mismatch"
    UNACKNOWLEDGED = "unacknowledged"
    LEASED = "leased"
    CONSUMED = "consumed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class PlanPrincipal:
    """Stable acting principal and complete impersonation chain."""

    stable_identity: str
    impersonation_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Require explicit, non-empty identity values."""
        if not isinstance(self.stable_identity, str) or not self.stable_identity:
            msg = "principal stable_identity must be a non-empty string"
            raise ValueError(msg)
        if not isinstance(self.impersonation_chain, tuple) or any(
            not isinstance(identity, str) or not identity
            for identity in self.impersonation_chain
        ):
            msg = "impersonation_chain must contain non-empty strings"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ContactBinding:
    """Non-secret binding to the exact quota-contact source and keyed value."""

    source: StableSymbol
    source_identity: str
    value_digest: str

    def __post_init__(self) -> None:
        """Reject raw or ambiguous contact bindings."""
        if not isinstance(self.source, StableSymbol):
            msg = "contact source must be a StableSymbol"
            raise TypeError(msg)
        if not isinstance(self.source_identity, str) or not self.source_identity:
            msg = "contact source_identity must be a non-empty string"
            raise ValueError(msg)
        if not _is_exact_digest(self.value_digest, "hmac-sha256"):
            msg = "contact value_digest must be an exact lowercase hmac-sha256 digest"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    """Digest of one freshly validated mutation-gating observation."""

    name: StableSymbol
    value_digest: str
    observed_at: datetime

    def __post_init__(self) -> None:
        """Require named, timestamped, content-addressed evidence."""
        if not isinstance(self.name, StableSymbol):
            msg = "evidence name must be a StableSymbol"
            raise TypeError(msg)
        if not _is_exact_digest(self.value_digest, "sha256"):
            msg = "evidence value_digest must be an exact lowercase sha256 digest"
            raise ValueError(msg)
        require_utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class QuotaRequestPlan:
    """Canonical single-slice authorization produced by Preview."""

    resource_scope: ResourceScope
    slice_identity: EffectiveQuotaSliceIdentity
    target: QuotaQuantity
    effective: QuotaQuantity
    effective_observed_at: datetime
    preference_name: str | None
    preference_etag: str | None
    principal: PlanPrincipal
    contact_binding: ContactBinding
    warnings: tuple[StableSymbol, ...]
    required_acknowledgements: tuple[StableSymbol, ...]
    acknowledgements: tuple[StableSymbol, ...]
    constraints: tuple[ConstraintReference, ...]
    evidence: tuple[EvidenceBinding, ...]
    installation_id: str
    issued_at: datetime
    expires_at: datetime
    schema: str = field(default=PLAN_SCHEMA, init=False)

    def __post_init__(self) -> None:  # noqa: C901
        """Enforce exact identity, evidence, and fixed-lifetime bindings."""
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "resource_scope must be a ResourceScope"
            raise TypeError(msg)
        if not isinstance(self.slice_identity, EffectiveQuotaSliceIdentity):
            msg = "slice_identity must be an EffectiveQuotaSliceIdentity"
            raise TypeError(msg)
        if self.slice_identity.resource_scope != self.resource_scope:
            msg = "slice identity must use the plan resource scope"
            raise ValueError(msg)
        if not isinstance(self.target, QuotaQuantity) or not isinstance(
            self.effective, QuotaQuantity
        ):
            msg = "target and effective must be QuotaQuantity values"
            raise TypeError(msg)
        if self.target.unit != self.effective.unit:
            msg = "target and effective quantities must use the same unit"
            raise ValueError(msg)
        require_utc(self.effective_observed_at, "effective_observed_at")
        _require_optional_nonempty(self.preference_name, "preference_name")
        _require_optional_nonempty(self.preference_etag, "preference_etag")
        if not isinstance(self.principal, PlanPrincipal):
            msg = "principal must be a PlanPrincipal"
            raise TypeError(msg)
        if not isinstance(self.contact_binding, ContactBinding):
            msg = "contact_binding must be a ContactBinding"
            raise TypeError(msg)
        _require_tuple_of(self.warnings, StableSymbol, "warnings")
        _require_tuple_of(
            self.required_acknowledgements,
            StableSymbol,
            "required_acknowledgements",
        )
        _require_tuple_of(self.acknowledgements, StableSymbol, "acknowledgements")
        _require_tuple_of(self.constraints, ConstraintReference, "constraints")
        _require_tuple_of(self.evidence, EvidenceBinding, "evidence")
        if len({item.name for item in self.evidence}) != len(self.evidence):
            msg = "evidence names must be unique"
            raise ValueError(msg)
        if not set(self.acknowledgements).issubset(self.required_acknowledgements):
            msg = "acknowledgements must be required by the plan"
            raise ValueError(msg)
        if not isinstance(self.installation_id, str) or not self.installation_id:
            msg = "installation_id must be a non-empty string"
            raise ValueError(msg)
        require_utc(self.issued_at, "issued_at")
        require_utc(self.expires_at, "expires_at")
        if self.expires_at - self.issued_at != PLAN_LIFETIME:
            msg = "quota request plans must expire exactly 15 minutes after issuance"
            raise ValueError(msg)

    def is_expired(self, now: datetime) -> bool:
        """Return whether applicability has ended at the supplied UTC time."""
        require_utc(now, "now")
        return now >= self.expires_at

    @property
    def unresolved_acknowledgements(self) -> tuple[StableSymbol, ...]:
        """Return required acknowledgement codes absent from Preview input."""
        acknowledged = frozenset(self.acknowledgements)
        return tuple(
            item for item in self.required_acknowledgements if item not in acknowledged
        )


@dataclass(frozen=True, slots=True)
class PlanReview:
    """Trustworthy canonical contents and independent Apply capability."""

    plan: QuotaRequestPlan
    digest: str
    authenticated: bool
    state: PlanLedgerState
    apply_capability: bool
    incapability_reasons: tuple[PlanIncapability, ...]


def review_plan(  # noqa: C901, PLR0913
    plan: QuotaRequestPlan,
    *,
    digest: str,
    authenticated: bool,
    local_installation_id: str,
    state: PlanLedgerState,
    now: datetime,
) -> PlanReview:
    """Classify applicability without hiding safe digest-valid contents."""
    if not isinstance(plan, QuotaRequestPlan):
        msg = "plan must be a QuotaRequestPlan"
        raise TypeError(msg)
    if not _is_exact_digest(digest, "sha256"):
        msg = "plan digest must be an exact lowercase sha256 digest"
        raise ValueError(msg)
    if not isinstance(authenticated, bool):
        msg = "authenticated must be bool"
        raise TypeError(msg)
    if not isinstance(local_installation_id, str) or not local_installation_id:
        msg = "local_installation_id must be a non-empty string"
        raise ValueError(msg)
    if not isinstance(state, PlanLedgerState):
        msg = "state must be a PlanLedgerState"
        raise TypeError(msg)
    require_utc(now, "now")

    reasons: list[PlanIncapability] = []
    if plan.is_expired(now):
        reasons.append(PlanIncapability.EXPIRED)
    if not authenticated:
        reasons.append(PlanIncapability.FOREIGN_OR_UNAUTHENTICATED)
    if plan.installation_id != local_installation_id:
        reasons.append(PlanIncapability.INSTALLATION_MISMATCH)
    if plan.unresolved_acknowledgements:
        reasons.append(PlanIncapability.UNACKNOWLEDGED)
    if state in {PlanLedgerState.LEASED, PlanLedgerState.DISPATCHED}:
        reasons.append(PlanIncapability.LEASED)
    elif state is PlanLedgerState.CONSUMED:
        reasons.append(PlanIncapability.CONSUMED)
    elif state is PlanLedgerState.QUARANTINED:
        reasons.append(PlanIncapability.QUARANTINED)
    return PlanReview(
        plan=plan,
        digest=digest,
        authenticated=authenticated,
        state=state,
        apply_capability=not reasons,
        incapability_reasons=tuple(reasons),
    )


def _require_optional_nonempty(value: object, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        msg = f"{name} must be None or a non-empty string"
        raise ValueError(msg)


def _require_tuple_of(value: object, item_type: type[object], name: str) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, item_type) for item in value
    ):
        msg = f"{name} must be a tuple of {item_type.__name__} values"
        raise TypeError(msg)


def _is_exact_digest(value: object, algorithm: str) -> bool:
    if not isinstance(value, str):
        return False
    prefix = f"{algorithm}:"
    return (
        value.startswith(prefix)
        and _LOWER_HEX_DIGEST.fullmatch(value.removeprefix(prefix)) is not None
    )
