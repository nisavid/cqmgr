"""Read-only production refreshers for the protected Apply preflight."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cqmgr.application.operations.lifecycle_requests import (
    LifecyclePreparationError,
    bind_protected_contact,
)
from cqmgr.application.ports.apply import (
    ApplyContactRefresh,
    ApplyEvidenceRefresh,
    RefreshedApplyChild,
)
from cqmgr.domain.plans import PlanPrincipal
from cqmgr.domain.quotas import ConstraintReference

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from cqmgr.application.operations.read_only import ReadOnlyOperations
    from cqmgr.application.ports.identity import IdentityProvider
    from cqmgr.application.ports.secrets import SecretValue
    from cqmgr.domain.plans import ContactBinding, QuotaPlan


class ApplyRefreshError(RuntimeError):
    """Current mutation-gating evidence is unavailable or inconsistent."""


class CurrentApplyPrincipalRefresher:
    """Resolve the current stable ADC principal without switching identity."""

    def __init__(
        self,
        identity: IdentityProvider,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Bind one read-only identity provider and finite refresh timeout."""
        if timeout_seconds <= 0:
            message = "Apply identity timeout must be positive"
            raise ValueError(message)
        self._identity = identity
        self._timeout_seconds = timeout_seconds

    async def refresh_principal(
        self,
        plan: QuotaPlan,
        now: datetime,
    ) -> PlanPrincipal:
        """Return current verified identity; comparison remains Apply-owned."""
        del plan, now
        evidence = await self._identity.resolve(
            timeout_seconds=self._timeout_seconds,
        )
        principal = evidence.stable_principal
        if not evidence.preview_apply_capability or principal is None:
            message = "Apply principal is not stably verified"
            raise ApplyRefreshError(message)
        return PlanPrincipal(
            principal.value,
            tuple(item.value for item in evidence.impersonation_chain),
        )


class EphemeralApplyContactRefresher:
    """Rebind protected per-operation contacts retained only for this runtime."""

    def __init__(self) -> None:
        """Start without any implicitly available contact value."""
        self._values: dict[ContactBinding, SecretValue] = {}

    def register(
        self,
        binding: ContactBinding,
        value: SecretValue,
        authentication_key: SecretValue,
    ) -> None:
        """Retain one value only after its keyed binding exactly matches the Plan."""
        if bind_protected_contact(value, authentication_key) != binding:
            message = "Apply quota contact does not match the reviewed Plan"
            raise ApplyRefreshError(message)
        self._values[binding] = value

    async def refresh_contact(
        self,
        binding: ContactBinding,
        now: datetime,
    ) -> ApplyContactRefresh:
        """Return an exact value without falling through to another source."""
        del now
        value = self._values.get(binding)
        if value is None:
            message = "Apply quota contact source is unavailable"
            raise ApplyRefreshError(message)
        try:
            decoded = value.reveal().decode("utf-8")
        except UnicodeDecodeError as error:
            message = "Apply quota contact is not valid UTF-8"
            raise ApplyRefreshError(message) from error
        return ApplyContactRefresh(binding, decoded)


class ReadOnlyApplyEvidenceRefresher:
    """Refresh every exact planned child through the read-only facade."""

    def __init__(
        self,
        read_only: ReadOnlyOperations,
        *,
        deadline: Callable[[], float],
    ) -> None:
        """Bind a read-only facade and fresh caller-controlled deadline source."""
        self._read_only = read_only
        self._deadline = deadline

    async def refresh_evidence(
        self,
        plan: QuotaPlan,
        now: datetime,
    ) -> ApplyEvidenceRefresh:
        """Return complete current child facts in unchanged Plan order."""
        del now
        from cqmgr.application.operations.quotas import (  # noqa: PLC0415
            QuotaInspectData,
        )
        from cqmgr.application.operations.read_only import (  # noqa: PLC0415
            QuotaInspectSelector,
            ReadOnlyScopeInput,
        )

        children: list[RefreshedApplyChild] = []
        scope_input = ReadOnlyScopeInput(
            explicit_resource_scope=plan.resource_scope,
        )
        deadline = self._deadline()
        for child in plan.children:
            identity = child.slice_identity
            location = _identity_location(
                identity.dimensions.items, identity.quota_scope
            )
            result = await self._read_only.inspect(
                QuotaInspectSelector(
                    identity.service,
                    identity.quota_id,
                    location,
                    identity.dimensions,
                ),
                deadline=deadline,
                scope_input=scope_input,
            )
            data = result.data
            if (
                not result.succeeded
                or not isinstance(data, QuotaInspectData)
                or data.identity != identity
                or data.evidence is None
                or data.item is None
                or data.item.effective_value is None
                or data.item.evidence_observed_at is None
            ):
                outcome = result.outcome.code.value
                message = f"Apply child evidence unavailable: {outcome}"
                raise ApplyRefreshError(message)
            preference = data.preference
            children.append(
                RefreshedApplyChild(
                    child_id=child.child_id,
                    slice_identity=identity,
                    effective=data.item.effective_value,
                    usage=data.item.usage_value,
                    preference_name=(
                        None if preference is None else preference.provider_name
                    ),
                    preference_etag=None if preference is None else preference.etag,
                    evidence=(),
                    fresh=True,
                    complete=True,
                    ambiguous=False,
                    mutable=data.item.predicates.mutable,
                    ongoing_rollout=data.evidence.ongoing_rollout,
                )
            )
        constraints = tuple(
            ConstraintReference(child.slice_identity) for child in plan.children
        )
        if not children or any(
            child.slice_identity.resource_scope != plan.resource_scope
            for child in children
        ):
            message = "Apply evidence does not cover the exact Plan scope"
            raise ApplyRefreshError(message)
        return ApplyEvidenceRefresh(
            plan.resource_scope,
            constraints,
            tuple(children),
        )


def _identity_location(
    dimensions: tuple[tuple[str, str], ...],
    quota_scope: object,
) -> str:
    """Select only an exact provider location already bound by the Plan."""
    from cqmgr.domain.quotas import QuotaScope  # noqa: PLC0415

    values = dict(dimensions)
    if quota_scope is QuotaScope.ZONAL and "zone" in values:
        return values["zone"]
    if quota_scope is QuotaScope.REGIONAL and "region" in values:
        return values["region"]
    if "location" in values:
        return values["location"]
    if quota_scope is QuotaScope.GLOBAL:
        return "global"
    message = "Apply cannot derive an exact location from the planned slice"
    raise LifecyclePreparationError(message)
