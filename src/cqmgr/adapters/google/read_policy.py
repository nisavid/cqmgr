"""Bounded retry and shared-budget policy for read-only Google adapters."""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING

from google.api_core import exceptions as google_exceptions

from cqmgr.application.ports.coordination import (
    BudgetCommitUnknownError,
    BudgetCoordinator,
    BudgetRequest,
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
    JitterSource,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.redaction import RedactedText

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cqmgr.application.ports.provider_reads import ProviderReadContext


class ProviderCallResult[CallT]:
    """Internal adapter result that never exposes provider exceptions."""

    def __init__(self, value: CallT | None, diagnostic: Diagnostic | None) -> None:
        """Retain exactly one normalized value or diagnostic."""
        self.value = value
        self.diagnostic = diagnostic


class GoogleReadPolicy:
    """Charge and dispatch each page attempt within one caller deadline."""

    def __init__(  # noqa: PLR0913
        self,
        budget: BudgetCoordinator,
        jitter: JitterSource,
        *,
        timeout_seconds: float = 20.0,
        maximum_attempts: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Bind finite transport and retry policy."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            msg = "Google read timeout_seconds must be positive and finite"
            raise ValueError(msg)
        if (
            isinstance(maximum_attempts, bool)
            or not isinstance(maximum_attempts, int)
            or maximum_attempts < 1
        ):
            msg = "Google read maximum_attempts must be positive"
            raise ValueError(msg)
        self._budget = budget
        self._jitter = jitter
        self._timeout_seconds = float(timeout_seconds)
        self._maximum_attempts = maximum_attempts
        self._monotonic = monotonic
        self._sleep = sleep

    async def call[CallT](  # noqa: PLR0911 - each typed gate returns immediately
        self,
        context: ProviderReadContext,
        *,
        provider: str,
        phase: str,
        identity: str,
        dispatch: Callable[[float], Awaitable[CallT]],
    ) -> ProviderCallResult[CallT]:
        """Execute one page with every attempt budgeted and deadline-clamped."""
        if not context.identity.read_capability:
            return ProviderCallResult(
                None,
                _diagnostic(
                    phase,
                    provider,
                    "provider-credentials-unavailable",
                    "Application Default Credentials are unavailable for this read.",
                    RetryDisposition.AFTER_REFRESH,
                ),
            )
        for attempt in range(self._maximum_attempts):
            remaining = context.deadline - self._monotonic()
            if remaining <= 0:
                return ProviderCallResult(
                    None,
                    _diagnostic(
                        phase,
                        provider,
                        "provider-read-deadline-exceeded",
                        "The provider read exceeded its caller-controlled deadline.",
                        RetryDisposition.AFTER_REFRESH,
                    ),
                )
            transport_identity = context.identity.transport_budget_identity
            try:
                await self._budget.acquire(
                    BudgetRequest(
                        provider=provider,
                        project=context.project.resource_scope.canonical_name,
                        adc_quota_project=(
                            transport_identity.value
                            if transport_identity is not None
                            else None
                        ),
                    ),
                    deadline=context.deadline,
                    cancellation=context.cancellation,
                )
            except (
                BudgetCommitUnknownError,
                CoordinationCancelledError,
                CoordinationDeadlineExceededError,
            ) as error:
                return ProviderCallResult(
                    None,
                    _budget_failure(phase, provider, error),
                )
            try:
                context.cancellation.raise_if_cancelled()
            except CoordinationCancelledError:
                return ProviderCallResult(
                    None,
                    _diagnostic(
                        phase,
                        provider,
                        "provider-read-cancelled",
                        "The provider read was cancelled before dispatch.",
                        RetryDisposition.AFTER_REFRESH,
                    ),
                )
            remaining = context.deadline - self._monotonic()
            if remaining <= 0:
                return ProviderCallResult(
                    None,
                    _diagnostic(
                        phase,
                        provider,
                        "provider-read-deadline-exceeded",
                        "The provider read exceeded its caller-controlled deadline.",
                        RetryDisposition.AFTER_REFRESH,
                    ),
                )
            try:
                return ProviderCallResult(
                    await dispatch(min(self._timeout_seconds, remaining)),
                    None,
                )
            except Exception as error:  # noqa: BLE001 - provider text is discarded
                retryable = _is_transient(error)
                if not retryable or attempt + 1 >= self._maximum_attempts:
                    return ProviderCallResult(
                        None,
                        _provider_failure(phase, provider, error),
                    )
                delay = self._jitter.apply(
                    min(float(2**attempt), self._timeout_seconds),
                    attempt=attempt,
                    identity=identity,
                )
                remaining = context.deadline - self._monotonic()
                if delay >= remaining:
                    return ProviderCallResult(
                        None,
                        _diagnostic(
                            phase,
                            provider,
                            "provider-read-deadline-exceeded",
                            "The provider read cannot retry within its deadline.",
                            RetryDisposition.AFTER_REFRESH,
                        ),
                    )
                await self._sleep(delay)
        msg = "unreachable retry loop"
        raise RuntimeError(msg)


def _is_transient(error: Exception) -> bool:
    return isinstance(
        error,
        (
            google_exceptions.DeadlineExceeded,
            google_exceptions.InternalServerError,
            google_exceptions.ResourceExhausted,
            google_exceptions.ServiceUnavailable,
            google_exceptions.TooManyRequests,
        ),
    )


def _budget_failure(
    phase: str,
    provider: str,
    error: BudgetCommitUnknownError
    | CoordinationCancelledError
    | CoordinationDeadlineExceededError,
) -> Diagnostic:
    if isinstance(error, CoordinationCancelledError):
        return _diagnostic(
            phase,
            provider,
            "provider-read-cancelled",
            "The provider read was cancelled before dispatch.",
            RetryDisposition.AFTER_REFRESH,
        )
    if isinstance(error, CoordinationDeadlineExceededError):
        return _diagnostic(
            phase,
            provider,
            "provider-read-deadline-exceeded",
            "The provider read exceeded its caller-controlled deadline.",
            RetryDisposition.AFTER_REFRESH,
        )
    return _diagnostic(
        phase,
        provider,
        "provider-read-budget-unavailable",
        "The shared read budget could not safely authorize this call.",
        RetryDisposition.AFTER_REFRESH,
    )


def _provider_failure(phase: str, provider: str, error: Exception) -> Diagnostic:
    if isinstance(
        error,
        (google_exceptions.PermissionDenied, google_exceptions.Unauthenticated),
    ):
        return _diagnostic(
            phase,
            provider,
            "provider-read-authorization-failed",
            "Grant the required read-only provider permission, then retry.",
            RetryDisposition.NEVER,
        )
    if isinstance(error, google_exceptions.InvalidArgument):
        return _diagnostic(
            phase,
            provider,
            "provider-read-invalid-request",
            "The provider rejected the normalized read request.",
            RetryDisposition.NEVER,
        )
    if isinstance(error, google_exceptions.NotFound):
        return _diagnostic(
            phase,
            provider,
            "provider-read-not-found",
            "The requested provider resource was not found.",
            RetryDisposition.NEVER,
        )
    if _is_transient(error):
        return _diagnostic(
            phase,
            provider,
            "provider-read-transient-failure",
            "The provider read exhausted its bounded transient retries.",
            RetryDisposition.AFTER_BACKOFF,
        )
    return _diagnostic(
        phase,
        provider,
        "provider-read-failed",
        "The provider read failed without trustworthy complete evidence.",
        RetryDisposition.UNKNOWN,
    )


def schema_diagnostic(phase: str, provider: str) -> Diagnostic:
    """Return static safe evidence for malformed or skewed provider schema."""
    return _diagnostic(
        phase,
        provider,
        "provider-schema-invalid",
        "The provider returned unsupported or malformed read evidence.",
        RetryDisposition.AFTER_UPGRADE,
    )


def page_cap_diagnostic(phase: str, provider: str) -> Diagnostic:
    """Return static safe evidence for a caller policy page cap."""
    return _diagnostic(
        phase,
        provider,
        "provider-page-cap-reached",
        "The bounded provider page cap left required pages unread.",
        RetryDisposition.AFTER_REFRESH,
    )


def _diagnostic(
    phase: str,
    provider: str,
    code: str,
    message: str,
    retry: RetryDisposition,
) -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode(code),
        severity=Severity.ERROR,
        phase=DiagnosticPhase(phase),
        source=DiagnosticSource(provider),
        retry=retry,
        message=RedactedText(message),
    )
