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
_MAXIMUM_CONTACT_SOURCE_IDENTITY_LENGTH = 256
_PROFILE_CONTACT_SOURCE_IDENTITY = re.compile(
    r"cqmgr:quota-contact:v1:"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}:"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}:"
    r"item-[A-Za-z0-9_-]{32}\Z"
)
_DIRECT_USER_CONTACT_SOURCE_IDENTITY = re.compile(
    r"principal://[A-Za-z0-9][A-Za-z0-9._~-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*\Z"
)
_PROTECTED_INPUT_CONTACT_SOURCE_IDENTITY = re.compile(
    r"input:hmac-sha256:[0-9a-f]{64}\Z"
)
_CONTACT_SOURCE_IDENTITIES = {
    "direct-user": _DIRECT_USER_CONTACT_SOURCE_IDENTITY,
    "named-profile": _PROFILE_CONTACT_SOURCE_IDENTITY,
    "per-operation-input": _PROTECTED_INPUT_CONTACT_SOURCE_IDENTITY,
    "selected-profile": _PROFILE_CONTACT_SOURCE_IDENTITY,
}


class PlanLedgerState(StrEnum):
    """Durable single-use state of one locally issued plan."""

    AVAILABLE = "available"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    CONSUMED = "consumed"
    QUARANTINED = "quarantined"
    INVALIDATED = "invalidated"


class PlanKind(StrEnum):
    """Closed V1 quota request plan subject kinds."""

    SINGLE = "single"
    BUNDLE = "bundle"


class TargetStrategy(StrEnum):
    """Explicit rule used to derive absolute quota targets."""

    MINIMUM = "minimum"
    PRESERVE_HEADROOM = "preserve-headroom"
    MANUAL = "manual"


class PlanIncapability(StrEnum):
    """Stable reasons trustworthy plan contents cannot be Applied."""

    EXPIRED = "expired"
    FOREIGN_OR_UNAUTHENTICATED = "foreign-or-unauthenticated"
    INSTALLATION_MISMATCH = "installation-mismatch"
    UNACKNOWLEDGED = "unacknowledged"
    LEASED = "leased"
    CONSUMED = "consumed"
    QUARANTINED = "quarantined"
    INVALIDATED = "invalidated"
    LOCAL_AUTHORITY_UNAVAILABLE = "local-authority-unavailable"


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
        identity_pattern = _CONTACT_SOURCE_IDENTITIES.get(self.source.value)
        if identity_pattern is None:
            msg = "contact source is unsupported"
            raise ValueError(msg)
        if (
            not isinstance(self.source_identity, str)
            or len(self.source_identity) > _MAXIMUM_CONTACT_SOURCE_IDENTITY_LENGTH
            or identity_pattern.fullmatch(self.source_identity) is None
        ):
            msg = "contact source_identity must match its bounded non-secret source"
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
class QuotaRequestPlanChild:
    """One ordered independently mutable child bound into a bundle plan."""

    child_id: str
    slice_identity: EffectiveQuotaSliceIdentity
    target: QuotaQuantity
    effective: QuotaQuantity
    usage: QuotaQuantity | None
    workload: QuotaQuantity | None
    prior_desired: QuotaQuantity | None
    granted: QuotaQuantity | None
    preference_name: str | None
    preference_etag: str | None
    target_strategy: TargetStrategy
    target_derivation: StableSymbol
    direct_accelerator_rank: int
    scope_breadth_rank: int
    warnings: tuple[StableSymbol, ...]
    required_acknowledgements: tuple[StableSymbol, ...]
    acknowledgements: tuple[StableSymbol, ...]
    evidence: tuple[EvidenceBinding, ...]

    def __post_init__(self) -> None:  # noqa: C901
        """Reject child bindings that cannot be reviewed or ordered exactly."""
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "plan child_id must be a non-empty string"
            raise ValueError(msg)
        if not isinstance(self.slice_identity, EffectiveQuotaSliceIdentity):
            msg = "plan child requires exact slice identity"
            raise TypeError(msg)
        quantities = (
            self.target,
            self.effective,
            self.usage,
            self.workload,
            self.prior_desired,
            self.granted,
        )
        if any(
            value is not None and not isinstance(value, QuotaQuantity)
            for value in quantities
        ):
            msg = "plan child quantities must be QuotaQuantity values"
            raise TypeError(msg)
        units = {value.unit for value in quantities if value is not None}
        if len(units) != 1:
            msg = "plan child quantities must use one native unit"
            raise ValueError(msg)
        _require_optional_nonempty(self.preference_name, "preference_name")
        _require_optional_nonempty(self.preference_etag, "preference_etag")
        if not isinstance(self.target_strategy, TargetStrategy):
            msg = "plan child target_strategy must be a TargetStrategy"
            raise TypeError(msg)
        if not isinstance(self.target_derivation, StableSymbol):
            msg = "plan child target_derivation must be a StableSymbol"
            raise TypeError(msg)
        if self.direct_accelerator_rank not in {0, 1}:
            msg = "plan child direct accelerator rank is unsupported"
            raise ValueError(msg)
        if self.scope_breadth_rank not in {0, 1, 2, 3}:
            msg = "plan child scope breadth rank is unsupported"
            raise ValueError(msg)
        _require_tuple_of(self.warnings, StableSymbol, "warnings")
        _require_tuple_of(
            self.required_acknowledgements,
            StableSymbol,
            "required_acknowledgements",
        )
        _require_tuple_of(self.acknowledgements, StableSymbol, "acknowledgements")
        _require_tuple_of(self.evidence, EvidenceBinding, "evidence")
        if not set(self.acknowledgements).issubset(self.required_acknowledgements):
            msg = "acknowledgements must be required by the plan child"
            raise ValueError(msg)
        if len({item.name for item in self.evidence}) != len(self.evidence):
            msg = "plan child evidence names must be unique"
            raise ValueError(msg)

    @property
    def order_key(self) -> tuple[object, ...]:
        """Return the exact accelerator-first deterministic comparator."""
        identity = self.slice_identity
        return (
            self.direct_accelerator_rank,
            self.scope_breadth_rank,
            identity.resource_scope.canonical_name,
            identity.service,
            identity.quota_id,
            identity.dimensions.items,
            identity.quota_scope.value,
        )

    @property
    def unresolved_acknowledgements(self) -> tuple[StableSymbol, ...]:
        """Return required child acknowledgements absent from Preview input."""
        acknowledged = frozenset(self.acknowledgements)
        return tuple(
            item for item in self.required_acknowledgements if item not in acknowledged
        )


@dataclass(frozen=True, slots=True)
class QuotaRequestBundlePlan:
    """Canonical ordered workload-bundle authorization produced by Preview."""

    resource_scope: ResourceScope
    kind: PlanKind
    selected_location: str
    target_strategy: TargetStrategy
    normalized_workload: str
    children: tuple[QuotaRequestPlanChild, ...]
    constraints: tuple[ConstraintReference, ...]
    principal: PlanPrincipal
    contact_binding: ContactBinding
    installation_id: str
    issued_at: datetime
    expires_at: datetime
    no_op_children: tuple[QuotaRequestPlanChild, ...] = ()
    schema: str = field(default=PLAN_SCHEMA, init=False)

    def __post_init__(self) -> None:  # noqa: C901, PLR0912
        """Require one complete, canonically ordered bundle subject."""
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "resource_scope must be a ResourceScope"
            raise TypeError(msg)
        if self.kind is not PlanKind.BUNDLE:
            msg = "workload request plan kind must be bundle"
            raise ValueError(msg)
        if not isinstance(self.selected_location, str) or not self.selected_location:
            msg = "bundle selected_location must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.target_strategy, TargetStrategy):
            msg = "bundle target_strategy must be a TargetStrategy"
            raise TypeError(msg)
        if (
            not isinstance(self.normalized_workload, str)
            or not self.normalized_workload
        ):
            msg = "bundle normalized_workload must be non-empty"
            raise ValueError(msg)
        if (
            not isinstance(self.children, tuple)
            or not self.children
            or any(
                not isinstance(child, QuotaRequestPlanChild) for child in self.children
            )
        ):
            msg = "bundle children must be a non-empty tuple of plan children"
            raise ValueError(msg)
        if len({child.child_id for child in self.children}) != len(self.children):
            msg = "bundle plan child IDs must be unique"
            raise ValueError(msg)
        if (
            tuple(sorted(self.children, key=lambda child: child.order_key))
            != self.children
        ):
            msg = "bundle plan children must use deterministic accelerator-first order"
            raise ValueError(msg)
        if any(
            child.slice_identity.resource_scope != self.resource_scope
            for child in self.children
        ):
            msg = "bundle child resource scope must match the plan"
            raise ValueError(msg)
        _require_bundle_no_op_children(self)
        _require_tuple_of(self.constraints, ConstraintReference, "constraints")
        _require_plan_constraints(self.resource_scope, self.constraints)
        constraint_identities = {
            constraint.slice_identity for constraint in self.constraints
        }
        if any(
            child.slice_identity not in constraint_identities
            for child in (*self.children, *self.no_op_children)
        ):
            msg = "bundle composition children must belong to the constraint set"
            raise ValueError(msg)
        if not isinstance(self.principal, PlanPrincipal):
            msg = "principal must be a PlanPrincipal"
            raise TypeError(msg)
        if not isinstance(self.contact_binding, ContactBinding):
            msg = "contact_binding must be a ContactBinding"
            raise TypeError(msg)
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
        """Return unresolved acknowledgements across every ordered child."""
        return tuple(
            acknowledgement
            for child in self.children
            for acknowledgement in child.unresolved_acknowledgements
        )


def _require_bundle_no_op_children(plan: QuotaRequestBundlePlan) -> None:
    """Require verified no-op facts to be exact and composition-bound."""
    _require_tuple_of(
        plan.no_op_children,
        QuotaRequestPlanChild,
        "no_op_children",
    )
    composition = (*plan.children, *plan.no_op_children)
    if len({child.child_id for child in composition}) != len(composition):
        msg = "bundle plan child IDs must be unique across composition"
        raise ValueError(msg)
    if (
        tuple(sorted(plan.no_op_children, key=lambda child: child.order_key))
        != plan.no_op_children
    ):
        msg = "bundle no-op children must use deterministic accelerator-first order"
        raise ValueError(msg)
    if any(
        child.slice_identity.resource_scope != plan.resource_scope
        for child in plan.no_op_children
    ):
        msg = "bundle no-op child resource scope must match the plan"
        raise ValueError(msg)
    if any(child.required_acknowledgements for child in plan.no_op_children):
        msg = "bundle no-op children cannot require acknowledgements"
        raise ValueError(msg)


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
    target_strategy: TargetStrategy = TargetStrategy.MANUAL
    target_derivation: StableSymbol = field(
        default_factory=lambda: StableSymbol("manual-absolute")
    )
    child_id: str = "single"
    usage: QuotaQuantity | None = None
    workload: QuotaQuantity | None = None
    prior_desired: QuotaQuantity | None = None
    granted: QuotaQuantity | None = None
    direct_accelerator_rank: int = 0
    scope_breadth_rank: int = 0
    kind: PlanKind = field(default=PlanKind.SINGLE, init=False)
    schema: str = field(default=PLAN_SCHEMA, init=False)

    def __post_init__(self) -> None:  # noqa: C901, PLR0912, PLR0915
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
        _require_plan_constraints(self.resource_scope, self.constraints)
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
        if self.target_strategy is not TargetStrategy.MANUAL:
            msg = "single-slice plans must use the manual target strategy"
            raise ValueError(msg)
        if not isinstance(self.target_derivation, StableSymbol):
            msg = "target_derivation must be a StableSymbol"
            raise TypeError(msg)
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "single plan child_id must be non-empty"
            raise ValueError(msg)
        for value in (
            self.usage,
            self.workload,
            self.prior_desired,
            self.granted,
        ):
            if value is not None and (
                not isinstance(value, QuotaQuantity) or value.unit != self.target.unit
            ):
                msg = "single plan child quantities must use the target unit"
                raise ValueError(msg)
        if self.direct_accelerator_rank not in {0, 1}:
            msg = "single plan direct accelerator rank is unsupported"
            raise ValueError(msg)
        if self.scope_breadth_rank not in {0, 1, 2, 3}:
            msg = "single plan scope breadth rank is unsupported"
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

    @property
    def children(self) -> tuple[QuotaRequestPlanChild, ...]:
        """Expose the single mutation through the common ordered-child shape."""
        return (
            QuotaRequestPlanChild(
                child_id=self.child_id,
                slice_identity=self.slice_identity,
                target=self.target,
                effective=self.effective,
                usage=self.usage,
                workload=self.workload,
                prior_desired=self.prior_desired,
                granted=self.granted,
                preference_name=self.preference_name,
                preference_etag=self.preference_etag,
                target_strategy=self.target_strategy,
                target_derivation=self.target_derivation,
                direct_accelerator_rank=self.direct_accelerator_rank,
                scope_breadth_rank=self.scope_breadth_rank,
                warnings=self.warnings,
                required_acknowledgements=self.required_acknowledgements,
                acknowledgements=self.acknowledgements,
                evidence=self.evidence,
            ),
        )


type QuotaPlan = QuotaRequestPlan | QuotaRequestBundlePlan


@dataclass(frozen=True, slots=True)
class PlanReview:
    """Trustworthy canonical contents and independent Apply capability."""

    plan: QuotaPlan
    digest: str
    authenticated: bool
    state: PlanLedgerState
    apply_capability: bool
    incapability_reasons: tuple[PlanIncapability, ...]


def review_plan(  # noqa: C901, PLR0912, PLR0913
    plan: QuotaPlan,
    *,
    digest: str,
    authenticated: bool,
    local_installation_id: str,
    state: PlanLedgerState,
    now: datetime,
    local_authority_available: bool = True,
) -> PlanReview:
    """Classify applicability without hiding safe digest-valid contents."""
    if not isinstance(plan, (QuotaRequestPlan, QuotaRequestBundlePlan)):
        msg = "plan must be a QuotaRequestPlan or QuotaRequestBundlePlan"
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
    if not isinstance(local_authority_available, bool):
        msg = "local_authority_available must be bool"
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
    if not local_authority_available:
        reasons.append(PlanIncapability.LOCAL_AUTHORITY_UNAVAILABLE)
    if state in {PlanLedgerState.LEASED, PlanLedgerState.DISPATCHED}:
        reasons.append(PlanIncapability.LEASED)
    elif state is PlanLedgerState.CONSUMED:
        reasons.append(PlanIncapability.CONSUMED)
    elif state is PlanLedgerState.QUARANTINED:
        reasons.append(PlanIncapability.QUARANTINED)
    elif state is PlanLedgerState.INVALIDATED:
        reasons.append(PlanIncapability.INVALIDATED)
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


def _require_plan_constraints(
    resource_scope: ResourceScope,
    constraints: tuple[ConstraintReference, ...],
) -> None:
    identities = tuple(item.slice_identity for item in constraints)
    if any(identity.resource_scope != resource_scope for identity in identities):
        msg = "constraint resource scope must match the plan resource scope"
        raise ValueError(msg)
    if len(set(identities)) != len(identities):
        msg = "plan constraints must be unique"
        raise ValueError(msg)


def _is_exact_digest(value: object, algorithm: str) -> bool:
    if not isinstance(value, str):
        return False
    prefix = f"{algorithm}:"
    return (
        value.startswith(prefix)
        and _LOWER_HEX_DIGEST.fullmatch(value.removeprefix(prefix)) is not None
    )
