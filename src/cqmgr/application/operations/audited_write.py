"""Write-ahead safety guard for future provider mutation operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cqmgr.domain.redaction import RedactedText

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True, slots=True)
class CriticalUnknownOutcome:
    """The identities required to quarantine and reconcile an ambiguous write."""

    reconciliation_identity: RedactedText
    quarantine_identity: RedactedText

    def __post_init__(self) -> None:
        """Require safe, non-empty identities for both recovery actions."""
        for name, value in (
            ("reconciliation", self.reconciliation_identity),
            ("quarantine", self.quarantine_identity),
        ):
            if not isinstance(value, RedactedText):
                msg = f"critical unknown {name} identity must be RedactedText"
                raise TypeError(msg)
            if not value.value:
                msg = f"critical unknown {name} identity must not be empty"
                raise ValueError(msg)


class CriticalUnknownDispatchError(Exception):
    """A provider write may have happened but its terminal record failed."""

    def __init__(
        self,
        outcome: CriticalUnknownOutcome,
        *,
        recovery_failures: tuple[Exception, ...] = (),
    ) -> None:
        """Retain the exact identities needed for deterministic recovery."""
        super().__init__("provider write has a critical unknown durable outcome")
        self.outcome = outcome
        self.recovery_failures = recovery_failures


@dataclass(frozen=True, slots=True)
class WriteSafetyHooks[Result]:
    """Pre-dispatch guards and post-dispatch durable recovery callbacks."""

    pre_dispatch: tuple[Callable[[], None], ...]
    record_terminal: Callable[[Result], None]
    record_critical_unknown: Callable[[CriticalUnknownOutcome], None]
    quarantine: Callable[[RedactedText], None]


class AuditedWriteCoordinator[Result]:
    """Dispatch exactly once after every pre-write guard has completed."""

    async def run(
        self,
        *,
        hooks: WriteSafetyHooks[Result],
        dispatch: Callable[[], Awaitable[Result]],
        reconciliation_identity: RedactedText,
        quarantine_identity: RedactedText,
    ) -> Result:
        """Prevent dispatch on guard failure and fail closed after ambiguous writes."""
        outcome = CriticalUnknownOutcome(
            reconciliation_identity=reconciliation_identity,
            quarantine_identity=quarantine_identity,
        )
        for guard in hooks.pre_dispatch:
            guard()
        result = await dispatch()
        try:
            hooks.record_terminal(result)
        except Exception as error:
            recovery_failures: list[Exception] = []
            for recovery in (
                lambda: hooks.quarantine(outcome.quarantine_identity),
                lambda: hooks.record_critical_unknown(outcome),
            ):
                try:
                    recovery()
                except Exception as recovery_error:  # noqa: BLE001 - retained evidence
                    recovery_failures.append(recovery_error)
            raise CriticalUnknownDispatchError(
                outcome,
                recovery_failures=tuple(recovery_failures),
            ) from error
        return result
