"""Read-only production Apply refresher contracts."""

# Runtime type expressions keep CodeQL's import analysis aligned with these tests.
# ruff: noqa: TC006

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cqmgr.application.operations.lifecycle_apply import (
    ApplyRefreshError,
    CurrentApplyPrincipalRefresher,
    EphemeralApplyContactRefresher,
    ReadOnlyApplyEvidenceRefresher,
)
from cqmgr.application.operations.lifecycle_requests import (
    LifecyclePreparationError,
    bind_protected_contact,
)
from cqmgr.application.operations.quotas import QuotaInspectData
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.catalog import CatalogPredicates
from cqmgr.domain.identity import (
    ADCIdentityEvidence,
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
)
from cqmgr.domain.plans import PlanPrincipal
from cqmgr.domain.quota_queries import QuotaQueryItem
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaContainerType,
    QuotaIncreaseEligibility,
    QuotaIneligibilityReason,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 24, 16, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
UNIT = QuotaUnit("1")
KEY = SecretValue(b"k" * 32)
DEADLINE = 500.0


class _Identity:
    async def resolve(self, **kwargs: object) -> ADCIdentityEvidence:
        assert kwargs == {"timeout_seconds": 10.0}
        principal = PrincipalIdentity(
            "serviceAccount:agent@example.iam.gserviceaccount.com"
        )
        return ADCIdentityEvidence(
            CredentialKind.SERVICE_ACCOUNT,
            principal,
            principal,
            verification=PrincipalVerification.VERIFIED,
        )


class _ReadOnly:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[object] = []

    async def inspect(self, selector: object, **kwargs: object) -> object:
        self.calls.append((selector, kwargs))
        return self.result


class _ReadOnlyByQuota:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results

    async def inspect(self, selector: object, **kwargs: object) -> object:
        del kwargs
        return self.results[selector.quota_id]  # type: ignore[attr-defined]


def _identity() -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        "GPU-DIRECT",
        NormalizedDimensions((("region", "us-central1"),)),
        QuotaScope.REGIONAL,
    )


def _inspect_result(identity: EffectiveQuotaSliceIdentity) -> object:
    effective = EffectiveQuotaEvidence(
        identity=identity,
        effective_value=QuotaQuantity(4, UNIT),
        metric="compute.googleapis.com/GPU-DIRECT",
        declared_dimensions=("region",),
        applicable_locations=("us-central1",),
        eligibility=QuotaIncreaseEligibility(
            eligible=True,
            reason=ProviderSymbol("OTHER", QuotaIneligibilityReason),
        ),
        fixed=False,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
    )
    item = QuotaQueryItem(
        identity=identity,
        display_name=None,
        accelerator_id=None,
        location="us-central1",
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=True,
            guided=True,
            mutable=True,
        ),
        effective_value=QuotaQuantity(4, UNIT),
        usage_value=QuotaQuantity(2, UNIT),
        evidence_observed_at=NOW,
    )
    return SimpleNamespace(
        succeeded=True,
        data=QuotaInspectData(identity, effective, item, None, None, None, None),
        outcome=SimpleNamespace(code=StableSymbol("exact-slice-inspected")),
    )


def test_current_principal_requires_verified_stable_adc() -> None:
    """Apply receives the current stable principal without identity switching."""
    refresher = CurrentApplyPrincipalRefresher(cast(Any, _Identity()))

    principal = asyncio.run(refresher.refresh_principal(cast(Any, object()), NOW))

    assert principal == PlanPrincipal(
        "serviceAccount:agent@example.iam.gserviceaccount.com"
    )


def test_current_principal_rejects_invalid_timeout_and_unverified_identity() -> None:
    """Apply never treats an invalid budget or unstable ADC identity as authority."""
    with pytest.raises(ValueError, match="positive"):
        CurrentApplyPrincipalRefresher(cast(Any, _Identity()), timeout_seconds=0)

    class _UnverifiedIdentity:
        async def resolve(self, **kwargs: object) -> ADCIdentityEvidence:
            assert kwargs == {"timeout_seconds": 10.0}
            return ADCIdentityEvidence(
                CredentialKind.UNKNOWN,
                None,
                None,
                verification=PrincipalVerification.UNVERIFIED,
            )

    refresher = CurrentApplyPrincipalRefresher(cast(Any, _UnverifiedIdentity()))
    with pytest.raises(ApplyRefreshError, match="stably verified"):
        asyncio.run(refresher.refresh_principal(cast(Any, object()), NOW))


def test_ephemeral_contact_requires_exact_plan_binding() -> None:
    """A re-entered contact is retained only after its keyed digest matches."""
    refresher = EphemeralApplyContactRefresher()
    contact = SecretValue(b"operator@example.com")
    binding = bind_protected_contact(contact, KEY)

    with pytest.raises(ApplyRefreshError, match="does not match"):
        refresher.register(
            binding,
            SecretValue(b"other@example.com"),
            KEY,
        )

    refresher.register(binding, contact, KEY)
    refreshed = asyncio.run(refresher.refresh_contact(binding, NOW))

    assert refreshed.binding == binding
    assert refreshed.value == "operator@example.com"
    assert "operator@example.com" not in repr(refresher)


def test_ephemeral_contact_rejects_missing_and_non_utf8_values() -> None:
    """A protected contact must be re-entered exactly and decode as UTF-8."""
    refresher = EphemeralApplyContactRefresher()
    contact = SecretValue(b"\xff")
    binding = bind_protected_contact(contact, KEY)

    with pytest.raises(ApplyRefreshError, match="unavailable"):
        asyncio.run(refresher.refresh_contact(binding, NOW))

    refresher.register(binding, contact, KEY)
    with pytest.raises(ApplyRefreshError, match="UTF-8"):
        asyncio.run(refresher.refresh_contact(binding, NOW))


def test_evidence_refresher_inspects_every_exact_planned_child() -> None:
    """Apply refreshes exact identity, values, mutability, and rollout read-only."""
    identity = _identity()
    read_only = _ReadOnly(_inspect_result(identity))
    plan = SimpleNamespace(
        resource_scope=SCOPE,
        constraints=(ConstraintReference(identity),),
        children=(
            SimpleNamespace(
                child_id="single",
                slice_identity=identity,
            ),
        ),
    )
    refresher = ReadOnlyApplyEvidenceRefresher(
        cast(Any, read_only),
        deadline=lambda: DEADLINE,
    )

    refreshed = asyncio.run(refresher.refresh_evidence(cast(Any, plan), NOW))

    assert refreshed.resource_scope == SCOPE
    assert refreshed.constraints[0].slice_identity == identity
    assert refreshed.children[0].effective == QuotaQuantity(4, UNIT)
    assert refreshed.children[0].usage == QuotaQuantity(2, UNIT)
    selector, kwargs = cast(tuple[Any, dict[str, object]], read_only.calls[0])
    assert selector.quota_id == identity.quota_id
    assert selector.location == "us-central1"
    assert kwargs["deadline"] == DEADLINE


def test_evidence_refresher_rejects_failed_and_scope_mismatched_reads() -> None:
    """Apply requires successful exact reads covering the reviewed Plan scope."""
    identity = _identity()
    child = SimpleNamespace(child_id="single", slice_identity=identity)
    failed = SimpleNamespace(
        succeeded=False,
        data=None,
        outcome=SimpleNamespace(code=StableSymbol("provider-unavailable")),
    )
    refresher = ReadOnlyApplyEvidenceRefresher(
        cast(Any, _ReadOnly(failed)),
        deadline=lambda: DEADLINE,
    )
    with pytest.raises(ApplyRefreshError, match="provider-unavailable"):
        asyncio.run(
            refresher.refresh_evidence(
                cast(
                    Any,
                    SimpleNamespace(
                        resource_scope=SCOPE,
                        constraints=(ConstraintReference(identity),),
                        children=(child,),
                    ),
                ),
                NOW,
            )
        )

    other_scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/456")
    refresher = ReadOnlyApplyEvidenceRefresher(
        cast(Any, _ReadOnly(_inspect_result(identity))),
        deadline=lambda: DEADLINE,
    )
    with pytest.raises(ApplyRefreshError, match="exact Plan scope"):
        asyncio.run(
            refresher.refresh_evidence(
                cast(
                    Any,
                    SimpleNamespace(
                        resource_scope=other_scope,
                        constraints=(ConstraintReference(identity),),
                        children=(child,),
                    ),
                ),
                NOW,
            )
        )


def test_evidence_refresher_rejects_slice_without_exact_location() -> None:
    """Apply never infers a regional location absent from the reviewed identity."""
    identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        "GPU-DIRECT",
        NormalizedDimensions(),
        QuotaScope.REGIONAL,
    )
    plan = SimpleNamespace(
        resource_scope=SCOPE,
        constraints=(ConstraintReference(identity),),
        children=(SimpleNamespace(child_id="single", slice_identity=identity),),
    )
    refresher = ReadOnlyApplyEvidenceRefresher(
        cast(Any, _ReadOnly(_inspect_result(identity))),
        deadline=lambda: DEADLINE,
    )

    with pytest.raises(LifecyclePreparationError, match="exact location"):
        asyncio.run(refresher.refresh_evidence(cast(Any, plan), NOW))


def test_evidence_refresher_includes_dispatch_and_verified_no_op_children() -> None:
    """Mixed bundles refresh every bound constraint, including prior no-ops."""
    direct = _identity()
    no_op = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        "GPU-COMPANION",
        direct.dimensions,
        QuotaScope.REGIONAL,
    )
    plan = SimpleNamespace(
        resource_scope=SCOPE,
        constraints=(ConstraintReference(direct), ConstraintReference(no_op)),
        children=(SimpleNamespace(child_id="direct", slice_identity=direct),),
        no_op_children=(SimpleNamespace(child_id="companion", slice_identity=no_op),),
    )
    refresher = ReadOnlyApplyEvidenceRefresher(
        cast(
            Any,
            _ReadOnlyByQuota(
                {
                    direct.quota_id: _inspect_result(direct),
                    no_op.quota_id: _inspect_result(no_op),
                }
            ),
        ),
        deadline=lambda: DEADLINE,
    )

    refreshed = asyncio.run(refresher.refresh_evidence(cast(Any, plan), NOW))

    assert refreshed.constraints == plan.constraints
    assert tuple(child.child_id for child in refreshed.children) == (
        "direct",
        "companion",
    )
    assert all(
        child.evidence[0].name == StableSymbol("quota-state")
        for child in refreshed.children
    )
