"""Typed read-only ports for provider-neutral accelerator catalog evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.catalog import CatalogLocationCoverage
from cqmgr.domain.quotas import ProviderRead

if TYPE_CHECKING:
    from cqmgr.domain.catalog import (
        ComputeMachineType,
        TpuAcceleratorType,
        TpuLocation,
        TpuRuntimeVersion,
    )


@dataclass(frozen=True, slots=True)
class CatalogRead[ReadT]:
    """Aggregate pages plus explicit per-source and per-location coverage."""

    read: ProviderRead[ReadT]
    location_coverage: tuple[CatalogLocationCoverage, ...]

    def __post_init__(self) -> None:
        """Require provider evidence and immutable location coverage."""
        if not isinstance(self.read, ProviderRead):
            msg = "catalog read must contain ProviderRead evidence"
            raise TypeError(msg)
        if not isinstance(self.location_coverage, tuple) or any(
            not isinstance(item, CatalogLocationCoverage)
            for item in self.location_coverage
        ):
            msg = "catalog location_coverage must use CatalogLocationCoverage"
            raise TypeError(msg)

    @property
    def values(self) -> tuple[ReadT, ...]:
        """Return normalized values without hiding their coverage."""
        return self.read.values

    @property
    def complete(self) -> bool:
        """Whether aggregate pages and every required location are complete."""
        return self.read.complete and all(
            item.complete for item in self.location_coverage
        )


@dataclass(frozen=True, slots=True)
class ComputeMachineTypeReadRequest:
    """Read project-visible Compute machine types across returned scopes."""

    context: ProviderReadContext

    def __post_init__(self) -> None:
        """Require explicit bounded provider context."""
        if not isinstance(self.context, ProviderReadContext):
            msg = "Compute catalog request requires ProviderReadContext"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class TpuLocationReadRequest:
    """Read all Cloud TPU service locations for one explicit project."""

    context: ProviderReadContext

    def __post_init__(self) -> None:
        """Require explicit bounded provider context."""
        if not isinstance(self.context, ProviderReadContext):
            msg = "TPU location request requires ProviderReadContext"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class TpuAcceleratorTypeReadRequest:
    """Read accelerator types for one explicit Cloud TPU zone."""

    context: ProviderReadContext
    zone: str

    def __post_init__(self) -> None:
        """Require explicit context and one canonical zone."""
        _require_context_and_zone(self.context, self.zone, "TPU accelerator")


@dataclass(frozen=True, slots=True)
class TpuRuntimeVersionReadRequest:
    """Read runtime versions for one explicit Cloud TPU zone."""

    context: ProviderReadContext
    zone: str

    def __post_init__(self) -> None:
        """Require explicit context and one canonical zone."""
        _require_context_and_zone(self.context, self.zone, "TPU runtime")


def _require_context_and_zone(
    context: object,
    zone: object,
    request_name: str,
) -> None:
    if not isinstance(context, ProviderReadContext):
        msg = f"{request_name} request requires ProviderReadContext"
        raise TypeError(msg)
    if (
        not isinstance(zone, str)
        or not zone
        or not zone.isascii()
        or zone != zone.lower()
        or "/" in zone
        or zone.startswith("-")
        or zone.endswith("-")
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in zone
        )
    ):
        msg = f"{request_name} zone must be a lowercase canonical location ID"
        raise ValueError(msg)


class ComputeMachineTypeReader(Protocol):
    """Read normalized Compute machine-type catalog evidence."""

    async def read(
        self,
        request: ComputeMachineTypeReadRequest,
    ) -> CatalogRead[ComputeMachineType]:
        """Return normalized machine types with scoped coverage."""
        ...


class TpuLocationReader(Protocol):
    """Read normalized Cloud TPU location evidence."""

    async def read(self, request: TpuLocationReadRequest) -> CatalogRead[TpuLocation]:
        """Return normalized TPU locations with source coverage."""
        ...


class TpuAcceleratorTypeReader(Protocol):
    """Read normalized Cloud TPU accelerator evidence for one zone."""

    async def read(
        self,
        request: TpuAcceleratorTypeReadRequest,
    ) -> CatalogRead[TpuAcceleratorType]:
        """Return normalized accelerator types for one zone."""
        ...


class TpuRuntimeVersionReader(Protocol):
    """Read normalized Cloud TPU runtime evidence for one zone."""

    async def read(
        self,
        request: TpuRuntimeVersionReadRequest,
    ) -> CatalogRead[TpuRuntimeVersion]:
        """Return normalized runtime versions for one zone."""
        ...
