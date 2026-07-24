"""Surface-neutral request composition, Preview, and plan review operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cqmgr.application.ports.plans import PlanRepositoryStatus
from cqmgr.domain.audit import (
    AuditFact,
    AuditFactName,
    AuditRecordDraft,
    AuditRecordKind,
)
from cqmgr.domain.plans import (
    PLAN_LIFETIME,
    ContactBinding,
    EvidenceBinding,
    PlanKind,
    PlanLedgerState,
    PlanPrincipal,
    PlanReview,
    QuotaPlan,
    QuotaRequestBundlePlan,
    QuotaRequestPlan,
    QuotaRequestPlanChild,
    TargetStrategy,
    review_plan,
)
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    QuotaQuantity,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import (
    Completeness,
    EvidenceGap,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cqmgr.application.ports.audit import AuditJournal
    from cqmgr.application.ports.plans import EncodedPlan, PlanCodec, PlanRepository
    from cqmgr.application.ports.secrets import SecretValue
    from cqmgr.domain.scopes import ResourceScope


_ACKNOWLEDGEMENT_CODES = frozenset(
    {
        "decrease-below-usage",
        "decrease-over-ten-percent",
        "unlimited-transition",
    }
)


@dataclass(frozen=True, slots=True)
class ComposeChild:
    """Fresh preflight evidence for one independently mutable exact slice."""

    child_id: str
    slice_identity: EffectiveQuotaSliceIdentity
    effective: QuotaQuantity
    usage: QuotaQuantity | None
    workload: QuotaQuantity | None
    direct_accelerator_rank: int
    scope_breadth_rank: int
    manual_target: QuotaQuantity | None = None
    preferred: QuotaQuantity | None = None
    granted: QuotaQuantity | None = None
    preference_settled: bool = False
    fresh: bool = True
    complete: bool = True
    ambiguous: bool = False
    mutable: bool = True
    ongoing_rollout: bool = False
    observed_at: datetime | None = None
    preference_name: str | None = None
    preference_etag: str | None = None
    warnings: tuple[str, ...] = ()
    evidence: tuple[EvidenceBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class ComposeRequest:
    """One single-slice or selected-location workload composition request."""

    kind: PlanKind
    strategy: TargetStrategy
    resource_scope: ResourceScope
    children: tuple[ComposeChild, ...]
    selected_location: str | None = None
    identity_verified: bool = True
    contact_verified: bool = True
    keyring_mutation_capable: bool = True
    expert: bool = False
    acknowledgements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComposedChild:
    """One absolute target and its independently reviewable disposition."""

    child_id: str
    slice_identity: EffectiveQuotaSliceIdentity
    target: QuotaQuantity
    effective: QuotaQuantity
    usage: QuotaQuantity | None
    no_op: bool
    direct_accelerator_rank: int
    scope_breadth_rank: int
    required_acknowledgements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Composition:
    """Complete composition outcome before authenticated plan issuance."""

    request: ComposeRequest
    reached: bool
    children: tuple[ComposedChild, ...] = ()
    incapability_reasons: tuple[str, ...] = ()

    @property
    def dispatch_children(self) -> tuple[ComposedChild, ...]:
        """Return only non-no-op children in the bound deterministic order."""
        return tuple(child for child in self.children if not child.no_op)


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    """Complete local inputs required to issue one authenticated plan."""

    composition: ComposeRequest
    principal: PlanPrincipal
    contact_binding: ContactBinding
    installation_id: str
    authentication_key: SecretValue
    normalized_workload: str
    now: datetime
    plan_out: Path | None = None


@dataclass(frozen=True, slots=True)
class PreviewData:
    """Reviewable Preview evidence and optional Apply-capable plan handle."""

    composition: Composition
    plan: QuotaPlan | None
    plan_digest: str | None
    audit_record_id: str | None
    apply_capability: bool


@dataclass(frozen=True, slots=True)
class PlanReviewRequest:
    """One local digest handle or explicit portable plan file to inspect."""

    digest: str | None
    path: Path | None
    authentication_key: SecretValue | None
    local_installation_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class PlanReviewData:
    """Trustworthy decoded plan review, absent only for invalid bytes."""

    review: PlanReview | None


class RequestPlanOperations:
    """Compose and issue audited, portable, locally authenticated plans."""

    def __init__(
        self,
        repository: PlanRepository | None = None,
        audit: AuditJournal | None = None,
        codec: PlanCodec | None = None,
    ) -> None:
        """Bind local durable storage ports; composition itself remains pure."""
        self._repository = repository
        self._audit = audit
        self._codec = codec

    @staticmethod
    def compose(request: ComposeRequest) -> Composition:
        """Compose absolute targets after a complete all-child preflight."""
        reasons = _preflight_reasons(request)
        if reasons:
            return Composition(request, reached=False, incapability_reasons=reasons)
        children = tuple(
            sorted(
                (_compose_child(child, request.strategy) for child in request.children),
                key=lambda child: (
                    child.direct_accelerator_rank,
                    child.scope_breadth_rank,
                    _slice_key(child.slice_identity),
                ),
            )
        )
        return Composition(request, reached=True, children=children)

    def preview(  # noqa: C901, PLR0911, PLR0912
        self, request: PreviewRequest
    ) -> OperationResult[PreviewData]:
        """Audit all-child preflight and issue or verify a no-op plan."""
        composition = self.compose(request.composition)
        if not composition.reached:
            return _preview_result(
                request,
                composition,
                reached=False,
                outcome="preview-rejected",
                exit_class=(
                    ExitClass.USAGE
                    if _has_usage_error(composition)
                    else ExitClass.REJECTED_PRECONDITION
                ),
            )
        if any(child.observed_at is None for child in request.composition.children):
            failed = Composition(
                request.composition,
                reached=False,
                incapability_reasons=("missing-observation-time",),
            )
            return _preview_result(
                request,
                failed,
                reached=False,
                outcome="preview-rejected",
                exit_class=ExitClass.INCOMPLETE_EVIDENCE,
            )
        if self._audit is None:
            return _preview_result(
                request,
                composition,
                reached=False,
                outcome="audit-unavailable",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
            )
        plan: QuotaPlan | None = None
        encoded: EncodedPlan | None = None
        if composition.dispatch_children:
            if self._repository is None:
                return _preview_result(
                    request,
                    composition,
                    reached=False,
                    outcome="plan-repository-unavailable",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                )
            if self._codec is None:
                return _preview_result(
                    request,
                    composition,
                    reached=False,
                    outcome="plan-codec-unavailable",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                )
            try:
                plan = _build_plan(request, composition)
                encoded = self._codec.encode(
                    plan,
                    request.authentication_key.reveal(),
                )
            except (TypeError, ValueError):
                return _preview_result(
                    request,
                    composition,
                    reached=False,
                    outcome="plan-encoding-failed",
                    exit_class=ExitClass.REJECTED_PRECONDITION,
                )
        try:
            audit_record = self._audit.append(
                AuditRecordDraft(
                    kind=AuditRecordKind.PREVIEW_EVIDENCE,
                    operation=OperationName("request.preview"),
                    resource_scope=request.composition.resource_scope,
                    occurred_at=request.now,
                    outcome=StableSymbol(
                        "verified-no-op"
                        if not composition.dispatch_children
                        else "plan-prepared"
                    ),
                    facts=_preview_audit_facts(
                        request,
                        composition,
                        plan_digest=encoded.digest if encoded is not None else None,
                    ),
                )
            )
        except (OSError, RuntimeError, ValueError):
            return _preview_result(
                request,
                composition,
                reached=False,
                outcome="preview-audit-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
            )
        if not composition.dispatch_children:
            return _preview_result(
                request,
                composition,
                reached=True,
                outcome="verified-no-op",
                audit_record_id=audit_record.record_id,
            )
        if self._repository is None or plan is None or encoded is None:
            return _preview_result(
                request,
                composition,
                reached=False,
                outcome="plan-preparation-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                audit_record_id=audit_record.record_id,
            )
        stored = self._repository.store(encoded, request.authentication_key)
        if stored.status is not PlanRepositoryStatus.STORED:
            return _preview_result(
                request,
                composition,
                reached=False,
                outcome="plan-store-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                plan=plan,
                plan_digest=encoded.digest,
                audit_record_id=audit_record.record_id,
            )
        if request.plan_out is not None:
            exported = self._repository.export(encoded, request.plan_out)
            if exported.status is not PlanRepositoryStatus.EXPORTED:
                return _preview_result(
                    request,
                    composition,
                    reached=False,
                    outcome="plan-export-failed",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                    plan=plan,
                    plan_digest=encoded.digest,
                    audit_record_id=audit_record.record_id,
                )
        return _preview_result(
            request,
            composition,
            reached=True,
            outcome="plan-issued",
            plan=plan,
            plan_digest=encoded.digest,
            audit_record_id=audit_record.record_id,
        )

    def review(  # noqa: PLR0911
        self, request: PlanReviewRequest
    ) -> OperationResult[PlanReviewData]:
        """Verify canonical bytes and report applicability without applying."""
        if self._repository is None:
            return _plan_review_result(
                request,
                reached=False,
                outcome="plan-repository-unavailable",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
            )
        if (request.digest is None) == (request.path is None):
            return _plan_review_result(
                request,
                reached=False,
                outcome="plan-selector-invalid",
                exit_class=ExitClass.USAGE,
            )
        if request.digest is not None:
            if request.authentication_key is None:
                return _plan_review_result(
                    request,
                    reached=False,
                    outcome="plan-authentication-key-missing",
                    exit_class=ExitClass.AUTHORIZATION,
                )
            loaded = self._repository.load(
                request.digest,
                request.authentication_key,
                request.now,
            )
        else:
            if request.path is None:
                return _plan_review_result(
                    request,
                    reached=False,
                    outcome="plan-selector-invalid",
                    exit_class=ExitClass.USAGE,
                )
            loaded = self._repository.read_export(request.path)
        if loaded.plan_bytes is None:
            return _plan_review_result(
                request,
                reached=False,
                outcome="plan-not-readable",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
            )
        try:
            if self._codec is None:
                return _plan_review_result(
                    request,
                    reached=False,
                    outcome="plan-codec-unavailable",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                )
            decoded = self._codec.decode(loaded.plan_bytes)
        except (TypeError, ValueError):
            return _plan_review_result(
                request,
                reached=False,
                outcome="plan-integrity-invalid",
                exit_class=ExitClass.STALE_OR_CONFLICTING,
            )
        if request.digest is not None and decoded.digest != request.digest:
            return _plan_review_result(
                request,
                reached=False,
                outcome="plan-digest-mismatch",
                exit_class=ExitClass.STALE_OR_CONFLICTING,
            )
        authenticated = (
            loaded.authenticated
            if loaded.authenticated is not None
            else (
                request.authentication_key is not None
                and decoded.authenticate(request.authentication_key.reveal())
            )
        )
        state = loaded.state or PlanLedgerState.AVAILABLE
        review = review_plan(
            decoded.plan,
            digest=decoded.digest,
            authenticated=authenticated,
            local_installation_id=request.local_installation_id,
            state=state,
            now=request.now,
        )
        return _plan_review_result(
            request,
            reached=True,
            outcome="plan-reviewed",
            review=review,
        )


def _preflight_reasons(  # noqa: C901, PLR0912
    request: ComposeRequest,
) -> tuple[str, ...]:
    reasons = list(_request_usage_reasons(request))
    if not request.identity_verified:
        reasons.append("identity-unverified")
    if not request.contact_verified:
        reasons.append("contact-unverified")
    if not request.keyring_mutation_capable:
        reasons.append("keyring-incapable")
    if not isinstance(request.strategy, TargetStrategy):
        reasons.append("unsupported-target-strategy")
    if not request.children:
        reasons.append("empty-child-set")
    if request.kind is PlanKind.SINGLE and len(request.children) != 1:
        reasons.append("inconsistent-single-child-set")
    if (
        request.kind is PlanKind.SINGLE
        and request.strategy is not TargetStrategy.MANUAL
    ):
        reasons.append("single-requires-manual-strategy")
    if request.kind is PlanKind.BUNDLE and not request.selected_location:
        reasons.append("bundle-location-required")
    seen_ids: set[str] = set()
    for child in request.children:
        if child.child_id in seen_ids:
            reasons.append("duplicate-child")
        seen_ids.add(child.child_id)
        if child.slice_identity.resource_scope != request.resource_scope:
            reasons.append("resource-scope-mismatch")
        if child.direct_accelerator_rank not in {0, 1}:
            reasons.append("unsupported-direct-accelerator-rank")
        if child.scope_breadth_rank not in {0, 1, 2, 3}:
            reasons.append("unsupported-scope-breadth-rank")
        if not child.fresh:
            reasons.append("stale-evidence")
        if not child.complete:
            reasons.append("incomplete-evidence")
        if child.ambiguous:
            reasons.append("ambiguous-evidence")
        if not child.mutable:
            reasons.append("unsupported-slice")
        if child.ongoing_rollout:
            reasons.append("ongoing-rollout")
        target, target_reason = _try_target(child, request.strategy)
        if target_reason is not None:
            reasons.append(target_reason)
            continue
        if target is None:
            reasons.append("target-unavailable")
            continue
        required = _required_acknowledgements(child, target, request.strategy)
        if required and not request.expert:
            reasons.append("expert-path-required")
        reasons.extend(
            f"missing-acknowledgement:{acknowledgement}"
            for acknowledgement in required
            if acknowledgement not in request.acknowledgements
        )
    return tuple(dict.fromkeys(reasons))


def _request_usage_reasons(request: ComposeRequest) -> tuple[str, ...]:
    """Return malformed-input reasons before semantic preflight."""
    reasons: list[str] = []
    if not isinstance(request.kind, PlanKind):
        reasons.append("unsupported-plan-kind")
    reasons.extend(
        f"unknown-acknowledgement:{acknowledgement}"
        for acknowledgement in request.acknowledgements
        if acknowledgement not in _ACKNOWLEDGEMENT_CODES
    )
    return tuple(reasons)


def _has_usage_error(composition: Composition) -> bool:
    """Return whether composition rejected malformed operator input."""
    return any(
        reason == "unsupported-plan-kind"
        or reason.startswith("unknown-acknowledgement:")
        for reason in composition.incapability_reasons
    )


def _compose_child(child: ComposeChild, strategy: TargetStrategy) -> ComposedChild:
    target, reason = _try_target(child, strategy)
    if target is None:
        raise ValueError(reason)
    return ComposedChild(
        child_id=child.child_id,
        slice_identity=child.slice_identity,
        target=target,
        effective=child.effective,
        usage=child.usage,
        no_op=_is_no_op(child, target, strategy),
        direct_accelerator_rank=child.direct_accelerator_rank,
        scope_breadth_rank=child.scope_breadth_rank,
        required_acknowledgements=_required_acknowledgements(
            child,
            target,
            strategy,
        ),
    )


def _try_target(  # noqa: C901, PLR0911
    child: ComposeChild, strategy: TargetStrategy
) -> tuple[QuotaQuantity | None, str | None]:
    try:
        if strategy is TargetStrategy.MANUAL:
            if child.manual_target is None:
                return None, "manual-target-required"
            target = child.manual_target
        elif strategy is TargetStrategy.PRESERVE_HEADROOM:
            if child.workload is None:
                return None, "workload-requirement-missing"
            target = _add(child.effective, child.workload)
        elif strategy is TargetStrategy.MINIMUM:
            if child.usage is None:
                return None, "usage-evidence-missing"
            if child.workload is None:
                return None, "workload-requirement-missing"
            target = _add(child.usage, child.workload)
            if child.preferred is not None and child.preferred.value < target.value:
                return None, "lower-conflicting-intent"
            if child.preferred is not None and child.preferred.value >= target.value:
                _same_unit(target, child.preferred)
                target = child.preferred
        else:
            return None, "unsupported-target-strategy"
        _same_unit(target, child.effective)
        if target.value < child.effective.value and child.usage is None:
            return None, "usage-evidence-missing"
    except ValueError:
        return None, "unit-conversion-ambiguous"
    return target, None


def _required_acknowledgements(
    child: ComposeChild,
    target: QuotaQuantity,
    strategy: TargetStrategy,
) -> tuple[str, ...]:
    if _is_no_op(child, target, strategy):
        return ()
    required: list[str] = []
    if (
        child.usage is not None
        and target.value != -1
        and child.usage.value != -1
        and target.value < child.usage.value
    ):
        required.append("decrease-below-usage")
    if (
        target.value != -1
        and child.effective.value > 0
        and target.value < child.effective.value
        and (child.effective.value - target.value) * 100 > child.effective.value * 10
    ):
        required.append("decrease-over-ten-percent")
    if (target.value == -1) != (child.effective.value == -1):
        required.append("unlimited-transition")
    return tuple(required)


def _is_no_op(
    child: ComposeChild,
    target: QuotaQuantity,
    strategy: TargetStrategy,
) -> bool:
    """Return whether composition proves this child needs no provider change."""
    settled_no_op = (
        child.preference_settled
        and child.preferred == target
        and child.granted == target
    )
    permits_workload = (
        strategy is TargetStrategy.MINIMUM
        and child.usage is not None
        and child.workload is not None
        and child.effective.value >= child.usage.value + child.workload.value
        and (
            child.preferred is None
            or child.preferred.value >= child.usage.value + child.workload.value
        )
    )
    return settled_no_op or permits_workload


def _add(left: QuotaQuantity, right: QuotaQuantity) -> QuotaQuantity:
    _same_unit(left, right)
    return QuotaQuantity(left.value + right.value, left.unit)


def _same_unit(left: QuotaQuantity, right: QuotaQuantity) -> None:
    if left.unit != right.unit:
        msg = "composition quantities must use one native unit per child"
        raise ValueError(msg)


def _slice_key(value: EffectiveQuotaSliceIdentity) -> tuple[object, ...]:
    return (
        value.resource_scope.canonical_name,
        value.service,
        value.quota_id,
        value.dimensions.items,
        value.quota_scope.value,
    )


def _build_plan(request: PreviewRequest, composition: Composition) -> QuotaPlan:
    inputs = {child.child_id: child for child in request.composition.children}
    constraints = tuple(
        ConstraintReference(child.slice_identity) for child in composition.children
    )
    if request.composition.kind is PlanKind.SINGLE:
        child = composition.dispatch_children[0]
        source = inputs[child.child_id]
        if source.observed_at is None:
            msg = "Preview requires an exact effective observation time"
            raise ValueError(msg)
        return QuotaRequestPlan(
            resource_scope=request.composition.resource_scope,
            slice_identity=child.slice_identity,
            target=child.target,
            effective=child.effective,
            effective_observed_at=source.observed_at,
            preference_name=source.preference_name,
            preference_etag=source.preference_etag,
            principal=request.principal,
            contact_binding=request.contact_binding,
            warnings=tuple(StableSymbol(item) for item in source.warnings),
            required_acknowledgements=tuple(
                StableSymbol(item) for item in child.required_acknowledgements
            ),
            acknowledgements=tuple(
                StableSymbol(item)
                for item in request.composition.acknowledgements
                if item in child.required_acknowledgements
            ),
            constraints=constraints,
            evidence=source.evidence,
            installation_id=request.installation_id,
            issued_at=request.now,
            expires_at=request.now + PLAN_LIFETIME,
        )
    plan_children = tuple(
        _build_bundle_child(child, inputs[child.child_id], request.composition)
        for child in composition.dispatch_children
    )
    return QuotaRequestBundlePlan(
        resource_scope=request.composition.resource_scope,
        kind=PlanKind.BUNDLE,
        selected_location=request.composition.selected_location or "",
        target_strategy=request.composition.strategy,
        normalized_workload=request.normalized_workload,
        children=plan_children,
        constraints=constraints,
        principal=request.principal,
        contact_binding=request.contact_binding,
        installation_id=request.installation_id,
        issued_at=request.now,
        expires_at=request.now + PLAN_LIFETIME,
    )


def _preview_audit_facts(
    request: PreviewRequest,
    composition: Composition,
    *,
    plan_digest: str | None,
) -> tuple[AuditFact, ...]:
    """Retain safe complete Preview intent without quota-contact material."""
    facts = [
        AuditFact(
            AuditFactName.PLAN_SUBJECT,
            RedactedText(request.composition.kind.value),
        ),
        AuditFact(
            AuditFactName.TARGET_STRATEGY,
            RedactedText(request.composition.strategy.value),
        ),
    ]
    if plan_digest is not None:
        facts.append(
            AuditFact(
                AuditFactName.PLAN_DIGEST,
                RedactedText(plan_digest),
            )
        )
    facts.extend(
        AuditFact(
            AuditFactName.PLAN_CHILD,
            RedactedText(_audit_child_value(child)),
        )
        for child in composition.children
    )
    return tuple(facts)


def _audit_child_value(child: ComposedChild) -> str:
    """Return one deterministic safe child identity, target, and disposition."""
    identity = child.slice_identity
    dimensions = ",".join(f"{key}={value}" for key, value in identity.dimensions.items)
    disposition = "no-op" if child.no_op else "applicable"
    return "|".join(
        (
            child.child_id,
            identity.resource_scope.canonical_name,
            identity.service,
            identity.quota_id,
            dimensions,
            identity.quota_scope.value,
            f"{child.target.base10}:{child.target.unit.symbol}",
            disposition,
        )
    )


def _build_bundle_child(
    child: ComposedChild,
    source: ComposeChild,
    request: ComposeRequest,
) -> QuotaRequestPlanChild:
    derivations = {
        TargetStrategy.MINIMUM: "usage-plus-workload",
        TargetStrategy.PRESERVE_HEADROOM: "effective-plus-workload",
        TargetStrategy.MANUAL: "manual-absolute",
    }
    return QuotaRequestPlanChild(
        child_id=child.child_id,
        slice_identity=child.slice_identity,
        target=child.target,
        effective=child.effective,
        usage=child.usage,
        workload=source.workload,
        prior_desired=source.preferred,
        granted=source.granted,
        preference_name=source.preference_name,
        preference_etag=source.preference_etag,
        target_strategy=request.strategy,
        target_derivation=StableSymbol(derivations[request.strategy]),
        direct_accelerator_rank=child.direct_accelerator_rank,
        scope_breadth_rank=child.scope_breadth_rank,
        warnings=tuple(StableSymbol(item) for item in source.warnings),
        required_acknowledgements=tuple(
            StableSymbol(item) for item in child.required_acknowledgements
        ),
        acknowledgements=tuple(
            StableSymbol(item)
            for item in request.acknowledgements
            if item in child.required_acknowledgements
        ),
        evidence=source.evidence,
    )


def _preview_result(  # noqa: PLR0913
    request: PreviewRequest,
    composition: Composition,
    *,
    reached: bool,
    outcome: str,
    exit_class: ExitClass = ExitClass.SUCCESS,
    plan: QuotaPlan | None = None,
    plan_digest: str | None = None,
    audit_record_id: str | None = None,
) -> OperationResult[PreviewData]:
    completeness = (
        Completeness.incomplete(
            EvidenceGap(
                StableSymbol("preview-evidence"),
                StableSymbol("required-evidence-missing"),
            )
        )
        if exit_class is ExitClass.INCOMPLETE_EVIDENCE
        else Completeness.complete()
    )
    return OperationResult(
        operation=OperationName("request.preview"),
        resource_scope=request.composition.resource_scope,
        boundary=OperationBoundary(StableSymbol("plan-previewed"), reached),
        outcome=Outcome(StableSymbol(outcome), exit_class),
        completeness=completeness,
        started_at=request.now,
        finished_at=request.now,
        data=PreviewData(
            composition=composition,
            plan=plan,
            plan_digest=plan_digest,
            audit_record_id=audit_record_id,
            apply_capability=reached and plan is not None,
        ),
    )


def _plan_review_result(
    request: PlanReviewRequest,
    *,
    reached: bool,
    outcome: str,
    exit_class: ExitClass = ExitClass.SUCCESS,
    review: PlanReview | None = None,
) -> OperationResult[PlanReviewData]:
    return OperationResult(
        operation=OperationName("plan.review"),
        resource_scope=review.plan.resource_scope if review is not None else None,
        boundary=OperationBoundary(StableSymbol("plan-reviewed"), reached),
        outcome=Outcome(StableSymbol(outcome), exit_class),
        completeness=Completeness.complete(),
        started_at=request.now,
        finished_at=request.now,
        data=PlanReviewData(review=review),
    )
