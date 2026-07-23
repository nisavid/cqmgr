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
    """Read existing preferences for selected services in one explicit project."""

    context: ProviderReadContext
    services: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Bind logical partitions while retaining one project-global API read."""
        if not isinstance(self.services, tuple):
            msg = "preference services must be a tuple"
            raise TypeError(msg)
        if any(not _is_service(service) for service in self.services):
            msg = "preference services must be canonical lowercase DNS names"
            raise ValueError(msg)
        if len(set(self.services)) != len(self.services):
            msg = "preference services must be unique"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class UsageReadRequest:
    """Read allocation usage for one canonical service and explicit interval."""

    context: ProviderReadContext
    service: str
    interval_start: datetime
    interval_end: datetime

    def __post_init__(self) -> None:
        """Require a canonical service and one bounded UTC interval."""
        _require_service(self.service)
        require_utc(self.interval_start, "interval_start")
        require_utc(self.interval_end, "interval_end")
        if self.interval_start >= self.interval_end:
            msg = "Monitoring interval must have start before end"
            raise ValueError(msg)


def _require_service(service: object) -> None:
    if not _is_service(service):
        msg = "usage service must be a canonical lowercase DNS name"
        raise ValueError(msg)


def _is_service(service: object) -> bool:
    if (
        not isinstance(service, str)
        or not service.isascii()
        or service != service.lower()
    ):
        return False
    labels = service.split(".")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    minimum_labels = 2
    return len(labels) >= minimum_labels and not any(
        not label
        or label.startswith("-")
        or label.endswith("-")
        or any(character not in allowed for character in label)
        for label in labels
    )


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
