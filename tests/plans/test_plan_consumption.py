"""Secret-safe plan consumption ledger value contracts."""

from datetime import UTC, datetime

from cqmgr.domain.plan_consumption import (
    PlanLedgerDecision,
    PlanLedgerRecord,
    PlanLedgerTransition,
)
from cqmgr.domain.plans import PlanLedgerState
from cqmgr.domain.results import StableSymbol


def test_lease_authority_is_absent_from_record_and_transition_representations() -> None:
    """Debug representations never disclose authority to advance plan state."""
    token = "lease-authority-must-remain-private"  # noqa: S105
    record = PlanLedgerRecord(
        PlanLedgerState.LEASED,
        lease_token=token,
        lease_expires_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    transition = PlanLedgerTransition(PlanLedgerDecision.ACCEPTED, record)

    assert token not in repr(record)
    assert token not in repr(transition)


def test_quarantine_is_idempotent_for_the_exact_retained_lease() -> None:
    """Repeated containment cannot lose an already-durable quarantine."""
    token = "lease-authority"  # noqa: S105
    record = PlanLedgerRecord(
        PlanLedgerState.DISPATCHED,
        lease_token=token,
        lease_expires_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )

    first = record.quarantine(
        token=token,
        reason=StableSymbol("unknown-dispatch"),
    )
    replay = first.record.quarantine(
        token=token,
        reason=StableSymbol("critical-unknown"),
    )

    assert first.decision is PlanLedgerDecision.ACCEPTED
    assert replay.decision is PlanLedgerDecision.IDEMPOTENT
    assert replay.record is first.record
