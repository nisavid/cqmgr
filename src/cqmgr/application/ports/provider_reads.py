"""Separate typed ports for read-only Cloud Quotas and Monitoring evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.domain.identity import ADCIdentityEvidence
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.domain.quotas import (
        EffectiveQuotaEvidence,
        ProviderRead,
        QuotaPreferenceEvidence,
        UsageObservation,
    )


@dataclass(frozen=True, slots=True)
class ProviderReadContext:
    """Explicit project, ADC transport identity, deadline, and cancellation."""

    project: CanonicalProject
    identity: ADCIdentityEvidence
    deadline: float
    cancellation: CancellationToken

    def __post_init__(self) -> None:
        """Reject ambient or unbounded read coordination inputs."""
        if not isinstance(self.project, CanonicalProject):
            msg = "provider read context requires canonical project evidence"
            raise TypeError(msg)
        if not isinstance(self.identity, ADCIdentityEvidence):
            msg = "provider read context requires ADC identity evidence"
            raise TypeError(msg)
        if (
            isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not math.isfinite(self.deadline)
        ):
            msg = "provider read deadline must be finite monotonic seconds"
            raise ValueError(msg)
        if not isinstance(self.cancellation, CancellationToken):
            msg = "provider read cancellation must use CancellationToken"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class EffectiveQuotaReadRequest:
    """Read all effective slices for one explicit service and project."""

    context: ProviderReadContext
    service: str


@dataclass(frozen=True, slots=True)
class QuotaPreferenceReadRequest:
    """Read existing quota preferences for one explicit project."""

    context: ProviderReadContext


@dataclass(frozen=True, slots=True)
class UsageReadRequest:
    """Read allocation usage for one explicit interval and provider filter."""

    context: ProviderReadContext
    filter: str
    interval_start: datetime
    interval_end: datetime

    def __post_init__(self) -> None:
        """Require one bounded UTC interval and a non-empty exact filter."""
        if not isinstance(self.filter, str) or not self.filter:
            msg = "Monitoring filter must be non-empty"
            raise ValueError(msg)
        require_utc(self.interval_start, "interval_start")
        require_utc(self.interval_end, "interval_end")
        if self.interval_start >= self.interval_end:
            msg = "Monitoring interval must have start before end"
            raise ValueError(msg)


class EffectiveQuotaReader(Protocol):
    """Read normalized effective QuotaInfo slices only."""

    async def read(
        self, request: EffectiveQuotaReadRequest
    ) -> ProviderRead[EffectiveQuotaEvidence]:
        """Return bounded effective-quota evidence."""
        ...


class QuotaPreferenceReader(Protocol):
    """Read normalized existing QuotaPreference resources only."""

    async def read(
        self, request: QuotaPreferenceReadRequest
    ) -> ProviderRead[QuotaPreferenceEvidence]:
        """Return bounded preference evidence."""
        ...


class UsageReader(Protocol):
    """Read normalized Monitoring usage observations only."""

    async def read(self, request: UsageReadRequest) -> ProviderRead[UsageObservation]:
        """Return bounded usage evidence with explicit point intervals."""
        ...
