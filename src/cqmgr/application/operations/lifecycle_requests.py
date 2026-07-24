"""Async protected request preparation shared by every lifecycle surface."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

from cqmgr.application.operations.plans import (
    ComposeChild,
    ComposeRequest,
    PreviewRequest,
)
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.plans import ContactBinding, PlanKind, PlanPrincipal, TargetStrategy
from cqmgr.domain.results import StableSymbol

_DIRECT_AND_COMPANION_COUNT = 2

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from cqmgr.application.operations.read_only import (
        QuotaInspectSelector,
        ReadOnlyScopeInput,
    )
    from cqmgr.application.operations.trust import LoadedInstallationTrust
    from cqmgr.domain.accelerator_overlay import (
        CloudTpuSliceRequirement,
        ComputeInstanceRequirement,
    )
    from cqmgr.domain.scopes import ResourceScope


@dataclass(frozen=True, slots=True)
class LifecycleCompositionIntent:
    """Surface-neutral public intent before provider and trust resolution."""

    scope_input: ReadOnlyScopeInput
    selector: QuotaInspectSelector | None
    workload: ComputeInstanceRequirement | CloudTpuSliceRequirement | None
    target_strategy: TargetStrategy
    targets: tuple[tuple[str | None, str], ...]
    acknowledgements: tuple[str, ...] = ()
    expert: bool = False
    quota_contact: SecretValue | None = field(default=None, repr=False)
    plan_out: Path | None = None

    def __post_init__(self) -> None:
        """Keep request shape and protected values explicit and disjoint."""
        from pathlib import Path  # noqa: PLC0415

        from cqmgr.application.operations.read_only import (  # noqa: PLC0415
            QuotaInspectSelector,
            ReadOnlyScopeInput,
        )
        from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
            CloudTpuSliceRequirement,
            ComputeInstanceRequirement,
        )

        if not isinstance(self.scope_input, ReadOnlyScopeInput):
            msg = "lifecycle scope input must be typed"
            raise TypeError(msg)
        exact = isinstance(self.selector, QuotaInspectSelector)
        workload = isinstance(
            self.workload,
            (ComputeInstanceRequirement, CloudTpuSliceRequirement),
        )
        if exact == workload:
            msg = "lifecycle intent requires exactly one exact slice or workload"
            raise ValueError(msg)
        if not isinstance(self.target_strategy, TargetStrategy):
            msg = "lifecycle target strategy must be typed"
            raise TypeError(msg)
        if not isinstance(self.targets, tuple):
            msg = "lifecycle targets must be a tuple"
            raise TypeError(msg)
        if not isinstance(self.acknowledgements, tuple) or any(
            not isinstance(item, str) or not item for item in self.acknowledgements
        ):
            msg = "lifecycle acknowledgements must be non-empty text"
            raise ValueError(msg)
        if not isinstance(self.expert, bool):
            msg = "lifecycle expert intent must be boolean"
            raise TypeError(msg)
        if self.quota_contact is not None and not isinstance(
            self.quota_contact,
            SecretValue,
        ):
            msg = "lifecycle quota contact must be protected"
            raise TypeError(msg)
        if self.plan_out is not None and not isinstance(self.plan_out, Path):
            msg = "lifecycle plan output must use Path"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class LifecycleCompositionEvidence:
    """Fresh provider evidence ready for pure composition and Preview binding."""

    kind: PlanKind
    resource_scope: ResourceScope
    children: tuple[ComposeChild, ...]
    selected_location: str | None
    principal: PlanPrincipal | None
    identity_verified: bool
    normalized_workload: str

    def __post_init__(self) -> None:
        """Reject incomplete or falsely verified protected evidence."""
        from cqmgr.application.operations.plans import ComposeChild  # noqa: PLC0415
        from cqmgr.domain.scopes import ResourceScope  # noqa: PLC0415

        if not isinstance(self.kind, PlanKind):
            msg = "lifecycle evidence kind must be typed"
            raise TypeError(msg)
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "lifecycle evidence scope must be typed"
            raise TypeError(msg)
        if not isinstance(self.children, tuple) or any(
            not isinstance(item, ComposeChild) for item in self.children
        ):
            msg = "lifecycle evidence children must be typed"
            raise TypeError(msg)
        if self.principal is not None and not isinstance(self.principal, PlanPrincipal):
            msg = "lifecycle evidence principal must be stable"
            raise TypeError(msg)
        if not isinstance(self.identity_verified, bool):
            msg = "lifecycle identity verification must be boolean"
            raise TypeError(msg)
        if self.identity_verified and self.principal is None:
            msg = "verified lifecycle evidence requires a stable principal"
            raise ValueError(msg)
        if (
            not isinstance(self.normalized_workload, str)
            or not self.normalized_workload
        ):
            msg = "normalized lifecycle workload must be non-empty"
            raise ValueError(msg)


class LifecycleCompositionReader(Protocol):
    """Resolve current provider evidence without exposing mutation capability."""

    async def read(
        self,
        intent: LifecycleCompositionIntent,
        *,
        deadline: float,
    ) -> LifecycleCompositionEvidence:
        """Return complete current composition evidence or fail closed."""


class InstallationTrustSource(Protocol):
    """Load already-active authority without initialization capability."""

    def load(self) -> LoadedInstallationTrust:
        """Return validated active trust or fail closed."""


class LifecyclePreparationError(RuntimeError):
    """Fresh provider evidence could not safely become a lifecycle request."""


class ReadOnlyLifecycleCompositionReader:
    """Convert the existing read-only facade into mutation-gating evidence."""

    def __init__(self, read_only: object) -> None:
        """Bind one read-only facade without accepting a provider write port."""
        self._read_only = read_only

    async def read(
        self,
        intent: LifecycleCompositionIntent,
        *,
        deadline: float,
    ) -> LifecycleCompositionEvidence:
        """Resolve an exact slice or workload using only typed read operations."""
        if intent.selector is not None:
            return await self._read_exact(intent, deadline=deadline)
        return await self._read_workload(intent, deadline=deadline)

    async def _read_exact(
        self,
        intent: LifecycleCompositionIntent,
        *,
        deadline: float,
    ) -> LifecycleCompositionEvidence:
        from cqmgr.application.operations.quotas import (  # noqa: PLC0415
            QuotaInspectData,
        )

        selector = intent.selector
        if selector is None:  # pragma: no cover - caller dispatches exact only
            message = "exact lifecycle preparation requires a selector"
            raise LifecyclePreparationError(message)
        result = await self._read_only.inspect(  # type: ignore[attr-defined]
            selector,
            deadline=deadline,
            scope_input=intent.scope_input,
        )
        data = result.data
        if (
            not result.succeeded
            or not isinstance(data, QuotaInspectData)
            or data.evidence is None
            or data.item is None
            or data.item.effective_value is None
            or data.item.evidence_observed_at is None
        ):
            message = (
                f"exact lifecycle evidence unavailable: {result.outcome.code.value}"
            )
            raise LifecyclePreparationError(message)
        if len(intent.targets) != 1 or intent.targets[0][0] is not None:
            message = "exact lifecycle preparation requires one unkeyed target"
            raise LifecyclePreparationError(message)
        try:
            manual_target = data.item.effective_value.__class__(
                int(intent.targets[0][1]),
                data.item.effective_value.unit,
            )
        except (TypeError, ValueError) as error:
            message = "exact lifecycle target must be a signed integer"
            raise LifecyclePreparationError(message) from error
        principal, verified = _plan_principal(result.identity_evidence)
        preference = data.preference
        unit = data.item.effective_value.unit
        child = ComposeChild(
            child_id="single",
            slice_identity=data.identity,
            effective=data.item.effective_value,
            usage=data.item.usage_value,
            workload=None,
            manual_target=manual_target,
            preferred=(
                None
                if preference is None
                else data.item.effective_value.__class__(
                    preference.preferred_value,
                    unit,
                )
            ),
            granted=(
                None
                if preference is None or preference.granted_value is None
                else data.item.effective_value.__class__(
                    preference.granted_value,
                    unit,
                )
            ),
            preference_settled=preference is not None and not preference.reconciling,
            direct_accelerator_rank=0,
            scope_breadth_rank=_scope_breadth_rank(data.identity.quota_scope),
            fresh=True,
            complete=True,
            ambiguous=False,
            mutable=data.item.predicates.mutable,
            ongoing_rollout=data.evidence.ongoing_rollout,
            observed_at=data.item.evidence_observed_at,
            preference_name=None if preference is None else preference.provider_name,
            preference_etag=None if preference is None else preference.etag,
        )
        return LifecycleCompositionEvidence(
            kind=PlanKind.SINGLE,
            resource_scope=data.identity.resource_scope,
            children=(child,),
            selected_location=None,
            principal=principal,
            identity_verified=verified,
            normalized_workload="exact-slice",
        )

    async def _read_workload(
        self,
        intent: LifecycleCompositionIntent,
        *,
        deadline: float,
    ) -> LifecycleCompositionEvidence:
        from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
            ResolvedWorkloadRequirement,
            WorkloadLocationDisposition,
        )

        workload = intent.workload
        if workload is None:  # pragma: no cover - caller dispatches workload only
            message = "workload lifecycle preparation requires a workload"
            raise LifecyclePreparationError(message)
        resolved_result = await self._read_only.resolve(  # type: ignore[attr-defined]
            workload,
            deadline=deadline,
            scope_input=intent.scope_input,
        )
        resolved = resolved_result.data
        if not resolved_result.succeeded or not isinstance(
            resolved,
            ResolvedWorkloadRequirement,
        ):
            message = (
                "workload lifecycle evidence unavailable: "
                f"{resolved_result.outcome.code.value}"
            )
            raise LifecyclePreparationError(message)
        compatible = tuple(
            location
            for location in resolved.locations
            if location.disposition is WorkloadLocationDisposition.COMPATIBLE
        )
        if len(compatible) != 1:
            message = (
                "workload lifecycle preparation requires exactly one "
                "compatible selected location"
            )
            raise LifecyclePreparationError(message)
        selected = compatible[0]
        if not selected.constraint_requirements or not selected.assessments:
            message = "workload lifecycle evidence lacks complete constraint assessment"
            raise LifecyclePreparationError(message)
        target_values = dict(intent.targets)
        child_ids = tuple(
            _workload_child_id(index, len(selected.constraint_requirements))
            for index in range(len(selected.constraint_requirements))
        )
        if intent.target_strategy is TargetStrategy.MANUAL and set(
            target_values
        ) != set(child_ids):
            message = "manual workload targets must name every selected child"
            raise LifecyclePreparationError(message)
        if intent.target_strategy is not TargetStrategy.MANUAL and target_values:
            message = "derived workload strategy cannot contain manual targets"
            raise LifecyclePreparationError(message)

        children: list[ComposeChild] = []
        principals: list[PlanPrincipal] = []
        for index, (child_id, requirement) in enumerate(
            zip(child_ids, selected.constraint_requirements, strict=True)
        ):
            selector = _selector_for_identity(
                requirement.identity,
                selected.location,
            )
            inspect_result = await self._read_only.inspect(  # type: ignore[attr-defined]
                selector,
                deadline=deadline,
                scope_input=intent.scope_input,
            )
            child, principal, verified = _child_from_inspect(
                inspect_result,
                child_id=child_id,
                direct_accelerator_rank=0 if index == 0 else 1,
                requirement=requirement,
                manual_target=target_values.get(child_id),
            )
            if not verified or principal is None:
                message = "workload lifecycle principal is unverified"
                raise LifecyclePreparationError(message)
            children.append(child)
            principals.append(principal)
        if len(set(principals)) != 1:
            message = "workload lifecycle principal changed across constraint reads"
            raise LifecyclePreparationError(message)
        return LifecycleCompositionEvidence(
            kind=PlanKind.BUNDLE,
            resource_scope=children[0].slice_identity.resource_scope,
            children=tuple(children),
            selected_location=selected.location,
            principal=principals[0],
            identity_verified=True,
            normalized_workload=(f"{type(workload).__name__}:{selected.location}"),
        )


@dataclass(frozen=True, slots=True)
class PreparedLifecycleRequests:
    """One shared composition and its optional protected Preview request."""

    composition: ComposeRequest
    preview: PreviewRequest | None


class LifecycleRequestOperations:
    """Prepare lifecycle requests under one async evidence and trust boundary."""

    def __init__(
        self,
        reader: LifecycleCompositionReader,
        trust: InstallationTrustSource,
        *,
        now: Callable[[], datetime],
    ) -> None:
        """Bind read-only evidence, active local authority, and an explicit clock."""
        self._reader = reader
        self._trust = trust
        self._now = now

    async def prepare(
        self,
        intent: LifecycleCompositionIntent,
        *,
        deadline: float,
    ) -> PreparedLifecycleRequests:
        """Resolve fresh evidence once and derive Compose plus optional Preview."""
        evidence = await self._reader.read(intent, deadline=deadline)
        composition = ComposeRequest(
            kind=evidence.kind,
            strategy=intent.target_strategy,
            resource_scope=evidence.resource_scope,
            children=evidence.children,
            selected_location=evidence.selected_location,
            expert=intent.expert,
            acknowledgements=intent.acknowledgements,
        )
        if intent.quota_contact is None or evidence.principal is None:
            return PreparedLifecycleRequests(composition, None)
        trust = self._trust.load()
        contact = _contact_binding(
            intent.quota_contact,
            trust.authentication_key,
        )
        preview = PreviewRequest(
            composition=composition,
            principal=evidence.principal,
            contact_binding=contact,
            installation_id=trust.installation_id,
            authentication_key=trust.authentication_key,
            identity_verified=evidence.identity_verified,
            contact_verified=True,
            keyring_mutation_capable=trust.keyring_mutation_capable,
            normalized_workload=evidence.normalized_workload,
            now=self._now(),
            plan_out=intent.plan_out,
        )
        return PreparedLifecycleRequests(composition, preview)


def _contact_binding(
    value: SecretValue,
    authentication_key: SecretValue,
) -> ContactBinding:
    """Bind a protected per-operation contact without retaining its value."""
    key = authentication_key.reveal()
    contact = value.reveal()
    source_digest = hmac.new(
        key,
        b"cqmgr-contact-source/v1:" + contact,
        sha256,
    ).hexdigest()
    value_digest = hmac.new(
        key,
        b"cqmgr-contact-value/v1:" + contact,
        sha256,
    ).hexdigest()
    return ContactBinding(
        StableSymbol("per-operation-input"),
        f"input:hmac-sha256:{source_digest}",
        f"hmac-sha256:{value_digest}",
    )


def _plan_principal(value: object) -> tuple[PlanPrincipal | None, bool]:
    """Translate sanitized provider identity into the stable plan binding."""
    from cqmgr.domain.identity import (  # noqa: PLC0415
        PrincipalVerification,
        ProviderIdentityEvidence,
    )

    if not isinstance(value, ProviderIdentityEvidence):
        return None, False
    principal = value.acting_principal
    if value.verification is not PrincipalVerification.VERIFIED or principal is None:
        return None, False
    return (
        PlanPrincipal(
            principal.value,
            tuple(item.value for item in value.impersonation_chain),
        ),
        True,
    )


def _scope_breadth_rank(value: object) -> int:
    """Map exact quota scope to the stable narrow-to-broad ordering rank."""
    from cqmgr.domain.quotas import QuotaScope  # noqa: PLC0415

    if not isinstance(value, QuotaScope):
        return 3
    return {
        QuotaScope.GLOBAL: 0,
        QuotaScope.REGIONAL: 1,
        QuotaScope.ZONAL: 2,
        QuotaScope.UNKNOWN: 3,
    }[value]


def _workload_child_id(index: int, count: int) -> str:
    """Name one selected direct or companion workload constraint."""
    if index == 0:
        return "direct"
    if count == _DIRECT_AND_COMPANION_COUNT:
        return "companion"
    return f"companion-{index}"


def _selector_for_identity(
    identity: object,
    location: str,
) -> QuotaInspectSelector:
    """Build a public exact selector from one resolver-owned identity."""
    from cqmgr.application.operations.read_only import (  # noqa: PLC0415
        QuotaInspectSelector,
    )
    from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity  # noqa: PLC0415

    if not isinstance(identity, EffectiveQuotaSliceIdentity):
        message = "workload constraint identity is not exact"
        raise LifecyclePreparationError(message)
    return QuotaInspectSelector(
        identity.service,
        identity.quota_id,
        location,
        identity.dimensions,
    )


def _child_from_inspect(
    result: object,
    *,
    child_id: str,
    direct_accelerator_rank: int,
    requirement: object,
    manual_target: str | None,
) -> tuple[ComposeChild, PlanPrincipal | None, bool]:
    """Convert one fresh constraint inspection without inferring write safety."""
    from cqmgr.application.operations.quotas import QuotaInspectData  # noqa: PLC0415
    from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
        QuotaConstraintRequirement,
    )
    from cqmgr.domain.quotas import QuotaQuantity  # noqa: PLC0415

    data = result.data  # type: ignore[attr-defined]
    if (
        not result.succeeded  # type: ignore[attr-defined]
        or not isinstance(data, QuotaInspectData)
        or not isinstance(requirement, QuotaConstraintRequirement)
        or data.identity != requirement.identity
        or data.evidence is None
        or data.item is None
        or data.item.effective_value is None
        or data.item.evidence_observed_at is None
    ):
        outcome = result.outcome.code.value  # type: ignore[attr-defined]
        message = f"workload constraint evidence unavailable: {outcome}"
        raise LifecyclePreparationError(message)
    target = None
    if manual_target is not None:
        try:
            target = QuotaQuantity(
                int(manual_target),
                data.item.effective_value.unit,
            )
        except (TypeError, ValueError) as error:
            message = "manual workload target must be a signed integer"
            raise LifecyclePreparationError(message) from error
    preference = data.preference
    unit = data.item.effective_value.unit
    principal, verified = _plan_principal(
        result.identity_evidence  # type: ignore[attr-defined]
    )
    return (
        ComposeChild(
            child_id=child_id,
            slice_identity=data.identity,
            effective=data.item.effective_value,
            usage=data.item.usage_value,
            workload=requirement.required,
            manual_target=target,
            preferred=(
                None
                if preference is None
                else QuotaQuantity(preference.preferred_value, unit)
            ),
            granted=(
                None
                if preference is None or preference.granted_value is None
                else QuotaQuantity(preference.granted_value, unit)
            ),
            preference_settled=preference is not None and not preference.reconciling,
            direct_accelerator_rank=direct_accelerator_rank,
            scope_breadth_rank=_scope_breadth_rank(data.identity.quota_scope),
            fresh=True,
            complete=True,
            ambiguous=False,
            mutable=data.item.predicates.mutable,
            ongoing_rollout=data.evidence.ongoing_rollout,
            observed_at=data.item.evidence_observed_at,
            preference_name=None if preference is None else preference.provider_name,
            preference_etag=None if preference is None else preference.etag,
        ),
        principal,
        verified,
    )
