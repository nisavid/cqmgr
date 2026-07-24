"""Typed read-only refresh boundaries for Apply preflight."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.domain.plans import (
        ContactBinding,
        EvidenceBinding,
        PlanPrincipal,
        QuotaPlan,
    )
    from cqmgr.domain.quotas import (
        ConstraintReference,
        EffectiveQuotaSliceIdentity,
        QuotaQuantity,
    )
    from cqmgr.domain.scopes import ResourceScope


@dataclass(frozen=True, slots=True)
class RefreshedApplyChild:
    """Fresh mutation-gating evidence for one exact planned child."""

    child_id: str
    slice_identity: EffectiveQuotaSliceIdentity
    effective: QuotaQuantity
    usage: QuotaQuantity | None
    preference_name: str | None
    preference_etag: str | None
    evidence: tuple[EvidenceBinding, ...]
    fresh: bool = True
    complete: bool = True
    ambiguous: bool = False
    mutable: bool = True
    ongoing_rollout: bool = False


@dataclass(frozen=True, slots=True)
class ApplyRevalidation:
    """Fresh identity, contact, constraints, and every ordered child."""

    resource_scope: ResourceScope
    principal: PlanPrincipal
    contact_binding: ContactBinding
    contact_value: str = field(repr=False)
    constraints: tuple[ConstraintReference, ...]
    children: tuple[RefreshedApplyChild, ...]

    def __post_init__(self) -> None:
        """Keep the secret contact ephemeral and the refresh structurally complete."""
        if not isinstance(self.contact_value, str) or not self.contact_value:
            msg = "revalidated contact value must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.constraints, tuple) or not isinstance(
            self.children, tuple
        ):
            msg = "revalidated constraints and children must be tuples"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ApplyContactRefresh:
    """Current contact binding plus an ephemeral provider contact value."""

    binding: ContactBinding
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject incomplete current contact resolution."""
        if not isinstance(self.value, str) or not self.value:
            msg = "refreshed contact value must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ApplyEvidenceRefresh:
    """One complete current scope, constraint, and ordered-child refresh."""

    resource_scope: ResourceScope
    constraints: tuple[ConstraintReference, ...]
    children: tuple[RefreshedApplyChild, ...]


class ApplyRevalidator(Protocol):
    """Resolve every mutation-gating fact in one complete pass."""

    async def refresh(self, plan: QuotaPlan, now: datetime) -> ApplyRevalidation:
        """Return fresh identity, contact, constraint, and child evidence."""


class ApplyPrincipalRefresher(Protocol):
    """Re-resolve the current stable acting principal."""

    async def refresh_principal(self, plan: QuotaPlan, now: datetime) -> PlanPrincipal:
        """Return current principal and complete impersonation chain."""


class ApplyContactRefresher(Protocol):
    """Re-resolve a bound contact source without persisting its value."""

    async def refresh_contact(
        self, binding: ContactBinding, now: datetime
    ) -> ApplyContactRefresh:
        """Return the current non-secret binding and ephemeral contact value."""


class ApplyEvidenceRefresher(Protocol):
    """Refresh the complete constraint set and every ordered child."""

    async def refresh_evidence(
        self, plan: QuotaPlan, now: datetime
    ) -> ApplyEvidenceRefresh:
        """Return one complete current evidence pass."""
