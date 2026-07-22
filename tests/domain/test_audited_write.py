"""Mutation-safety proofs for the write-ahead application seam."""

from __future__ import annotations

import asyncio

import pytest

from cqmgr.application.operations.audited_write import (
    AuditedWriteCoordinator,
    CriticalUnknownDispatchError,
    CriticalUnknownOutcome,
    WriteSafetyHooks,
)
from cqmgr.domain.redaction import RedactedText


class _InjectedGuardError(Exception):
    """A test-only audit, storage, or lock failure before dispatch."""


class _InjectedTerminalAuditError(Exception):
    """A test-only terminal audit failure after dispatch."""


@pytest.mark.parametrize("failed_guard", ["audit", "storage", "lock"])
def test_every_pre_dispatch_failure_proves_zero_provider_writes(
    failed_guard: str,
) -> None:
    """Audit, storage, and lock failures all stop before the provider boundary."""
    provider_writes = 0

    def guard(name: str) -> None:
        if name == failed_guard:
            raise _InjectedGuardError(name)

    async def dispatch() -> str:
        nonlocal provider_writes
        provider_writes += 1
        return "written"

    with pytest.raises(_InjectedGuardError, match=failed_guard):
        asyncio.run(
            AuditedWriteCoordinator[str]().run(
                hooks=WriteSafetyHooks(
                    pre_dispatch=tuple(
                        lambda name=name: guard(name)
                        for name in ("audit", "storage", "lock")
                    ),
                    record_terminal=lambda _result: None,
                    record_critical_unknown=lambda _outcome: None,
                    quarantine=lambda _identity: None,
                ),
                dispatch=dispatch,
                reconciliation_identity=RedactedText("quotaPreferences/request-1"),
                quarantine_identity=RedactedText("plan-digest-1"),
            )
        )

    assert provider_writes == 0


def test_post_dispatch_audit_failure_records_critical_unknown_and_quarantine() -> None:
    """One dispatched write retains both deterministic recovery identities."""
    provider_writes = 0
    unknowns: list[CriticalUnknownOutcome] = []
    quarantined: list[RedactedText] = []

    async def dispatch() -> str:
        nonlocal provider_writes
        provider_writes += 1
        return "provider-accepted"

    def fail_terminal(_result: str) -> None:
        raise _InjectedTerminalAuditError

    with pytest.raises(CriticalUnknownDispatchError) as caught:
        asyncio.run(
            AuditedWriteCoordinator[str]().run(
                hooks=WriteSafetyHooks(
                    pre_dispatch=(lambda: None,),
                    record_terminal=fail_terminal,
                    record_critical_unknown=unknowns.append,
                    quarantine=quarantined.append,
                ),
                dispatch=dispatch,
                reconciliation_identity=RedactedText("quotaPreferences/request-1"),
                quarantine_identity=RedactedText("plan-digest-1"),
            )
        )

    assert provider_writes == 1
    assert unknowns == [caught.value.outcome]
    assert quarantined == [caught.value.outcome.quarantine_identity]
    assert caught.value.outcome.reconciliation_identity.value == (
        "quotaPreferences/request-1"
    )


@pytest.mark.parametrize("field", ["reconciliation_identity", "quarantine_identity"])
def test_critical_unknown_requires_nonempty_safe_recovery_identities(
    field: str,
) -> None:
    """An ambiguous outcome cannot exist without both recovery identities."""
    values = {
        "reconciliation_identity": RedactedText("quotaPreferences/request-1"),
        "quarantine_identity": RedactedText("plan-digest-1"),
    }
    values[field] = RedactedText("")

    with pytest.raises(ValueError, match="must not be empty"):
        CriticalUnknownOutcome(**values)
