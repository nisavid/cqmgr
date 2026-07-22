"""Fail-closed invariants for quota-request status derivation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from cqmgr.domain.quotas import QuotaQuantity, QuotaUnit
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
    WatchDisposition,
)

UNIT = QuotaUnit("1")
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def quantity(value: int) -> QuotaQuantity:
    """Build one dimensionless quota quantity."""
    return QuotaQuantity(value, UNIT)


def test_non_settled_reconciliation_does_not_derive_settled_facts() -> None:
    """Grant and effective facts remain unproven until settlement."""
    status = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.RECONCILING,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(8),
        effective=quantity(8),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )

    assert status.grant_satisfaction is GrantSatisfaction.UNKNOWN
    assert status.effective_confirmation is EffectiveConfirmation.UNOBSERVED
    assert not status.is_granted
    assert not status.is_fulfilled


def test_settled_without_grant_evidence_keeps_watch_pending() -> None:
    """Settlement alone cannot refute a Watch condition."""
    status = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )

    assert status.watch(WatchCondition.GRANTED) is WatchDisposition.PENDING
    assert status.watch(WatchCondition.FULFILLED) is WatchDisposition.PENDING


def test_settled_known_non_target_grant_is_unmet_without_baseline() -> None:
    """A known adverse grant conclusively refutes both Watch conditions."""
    status = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=None,
        desired=quantity(8),
        granted=quantity(7),
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )

    assert status.grant_satisfaction is GrantSatisfaction.UNKNOWN
    assert status.watch(WatchCondition.GRANTED) is WatchDisposition.UNMET
    assert status.watch(WatchCondition.FULFILLED) is WatchDisposition.UNMET


def test_direct_construction_rejects_axes_inconsistent_with_evidence() -> None:
    """Callers cannot assert stronger axes than the source facts prove."""
    with pytest.raises(ValueError, match="exactly match"):
        QuotaRequestStatus(
            reconciliation=Reconciliation.SETTLED,
            grant_satisfaction=GrantSatisfaction.UNKNOWN,
            effective_confirmation=EffectiveConfirmation.UNOBSERVED,
            baseline=quantity(4),
            desired=quantity(8),
            granted=quantity(8),
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )
    with pytest.raises(ValueError, match="exactly match"):
        QuotaRequestStatus(
            reconciliation=Reconciliation.RECONCILING,
            grant_satisfaction=GrantSatisfaction.FULL,
            effective_confirmation=EffectiveConfirmation.CONFIRMED,
            baseline=quantity(4),
            desired=quantity(8),
            granted=quantity(8),
            effective=quantity(8),
            status_observed_at=NOW,
            effective_observed_at=NOW,
        )


def test_status_rejects_raw_strings_for_closed_types() -> None:
    """Serialized strings must be parsed before entering the typed domain."""
    with pytest.raises(TypeError, match="reconciliation"):
        QuotaRequestStatus.derive(
            reconciliation=cast("Reconciliation", "settled"),
            baseline=None,
            desired=quantity(8),
            granted=None,
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )
    with pytest.raises(TypeError, match="grant_satisfaction"):
        QuotaRequestStatus(
            reconciliation=Reconciliation.SETTLED,
            grant_satisfaction=cast("GrantSatisfaction", "full"),
            effective_confirmation=EffectiveConfirmation.UNOBSERVED,
            baseline=quantity(4),
            desired=quantity(8),
            granted=quantity(8),
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )
    status = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=quantity(4),
        desired=quantity(8),
        granted=quantity(8),
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )
    with pytest.raises(TypeError, match="reconciliation"):
        replace(status, reconciliation=cast("Reconciliation", "settled"))
    with pytest.raises(TypeError, match="effective_confirmation"):
        replace(
            status,
            effective_confirmation=cast("EffectiveConfirmation", "unobserved"),
        )
    with pytest.raises(TypeError, match="condition"):
        status.watch(cast("WatchCondition", "granted"))


def test_status_rejects_invalid_quantities_units_and_times() -> None:
    """All direct and derived status evidence uses canonical domain types."""
    with pytest.raises(TypeError, match="desired"):
        QuotaRequestStatus.derive(
            reconciliation=Reconciliation.SETTLED,
            baseline=None,
            desired=cast("QuotaQuantity", 8),
            granted=None,
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )
    with pytest.raises(TypeError, match="status_observed_at"):
        QuotaRequestStatus.derive(
            reconciliation=Reconciliation.SETTLED,
            baseline=None,
            desired=quantity(8),
            granted=None,
            effective=None,
            status_observed_at=cast("datetime", "2026-07-21T00:00:00Z"),
            effective_observed_at=None,
        )
    with pytest.raises(ValueError, match="one explicit unit"):
        QuotaRequestStatus(
            reconciliation=Reconciliation.SETTLED,
            grant_satisfaction=GrantSatisfaction.FULL,
            effective_confirmation=EffectiveConfirmation.UNOBSERVED,
            baseline=quantity(4),
            desired=quantity(8),
            granted=QuotaQuantity(8, QuotaUnit("count")),
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )


def test_effective_value_requires_its_observation_time() -> None:
    """Effective quota evidence is unusable without its source timestamp."""
    with pytest.raises(ValueError, match="both be present or both be absent"):
        QuotaRequestStatus.derive(
            reconciliation=Reconciliation.SETTLED,
            baseline=quantity(4),
            desired=quantity(8),
            granted=quantity(8),
            effective=quantity(8),
            status_observed_at=NOW,
            effective_observed_at=None,
        )


def test_effective_observation_time_requires_its_value() -> None:
    """An effective timestamp cannot claim an observation without a value."""
    with pytest.raises(ValueError, match="both be present or both be absent"):
        QuotaRequestStatus.derive(
            reconciliation=Reconciliation.SETTLED,
            baseline=quantity(4),
            desired=quantity(8),
            granted=quantity(8),
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=NOW,
        )


def test_unknown_provider_symbol_is_preserved_and_projected_unknown() -> None:
    """Future provider states remain visible without opening the product enum."""
    provider_state = ProviderSymbol("FUTURE_STATE", Reconciliation)

    status = QuotaRequestStatus.derive(
        reconciliation=provider_state,
        baseline=None,
        desired=quantity(8),
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )

    assert status.reconciliation is Reconciliation.UNKNOWN
    assert status.provider_reconciliation is provider_state


def test_status_rejects_invalid_provider_projections() -> None:
    """Provider evidence must be typed and agree with its product projection."""
    unknown = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.UNKNOWN,
        baseline=None,
        desired=quantity(8),
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )
    with pytest.raises(TypeError, match="provider_reconciliation"):
        replace(
            unknown,
            provider_reconciliation=cast(
                "ProviderSymbol[Reconciliation]", "FUTURE_STATE"
            ),
        )

    wrong_projection = ProviderSymbol("full", GrantSatisfaction)
    with pytest.raises(TypeError, match="Reconciliation enum type"):
        replace(
            unknown,
            provider_reconciliation=cast(
                "ProviderSymbol[Reconciliation]", wrong_projection
            ),
        )
    with pytest.raises(TypeError, match="Reconciliation enum type"):
        QuotaRequestStatus.derive(
            reconciliation=cast("ProviderSymbol[Reconciliation]", wrong_projection),
            baseline=None,
            desired=quantity(8),
            granted=None,
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )

    wrong_unknown_domain = ProviderSymbol("FUTURE_STATE", GrantSatisfaction)
    with pytest.raises(TypeError, match="Reconciliation enum type"):
        replace(
            unknown,
            provider_reconciliation=cast(
                "ProviderSymbol[Reconciliation]", wrong_unknown_domain
            ),
        )
    with pytest.raises(TypeError, match="Reconciliation enum type"):
        QuotaRequestStatus.derive(
            reconciliation=cast("ProviderSymbol[Reconciliation]", wrong_unknown_domain),
            baseline=None,
            desired=quantity(8),
            granted=None,
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )

    known_settled = ProviderSymbol("settled", Reconciliation)
    with pytest.raises(ValueError, match="match provider_reconciliation"):
        replace(unknown, provider_reconciliation=known_settled)
