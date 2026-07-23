"""Independent read-only ports for current and historical Spot advice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cqmgr.application.ports.provider_reads import ProviderReadContext
    from cqmgr.domain.obtainability import (
        CapacityAdvice,
        CapacityHistory,
        ObtainabilityCandidate,
    )
    from cqmgr.domain.quotas import ProviderRead


@dataclass(frozen=True, slots=True)
class CapacityAdviceReadRequest:
    """Read current advice for one exact immutable candidate."""

    context: ProviderReadContext
    candidate: ObtainabilityCandidate


@dataclass(frozen=True, slots=True)
class CapacityHistoryReadRequest:
    """Read one regional or zonal history surface for an exact machine request."""

    context: ProviderReadContext
    candidate: ObtainabilityCandidate
    location: str
    include_price: bool


class CapacityAdviceReader(Protocol):
    """Independently replaceable current-capacity advice port."""

    async def read(
        self,
        request: CapacityAdviceReadRequest,
    ) -> ProviderRead[CapacityAdvice]:
        """Return normalized current advice without provider DTOs."""
        ...


class CapacityHistoryReader(Protocol):
    """Independently replaceable Spot history port."""

    async def read(
        self,
        request: CapacityHistoryReadRequest,
    ) -> ProviderRead[CapacityHistory]:
        """Return normalized history without provider DTOs."""
        ...
