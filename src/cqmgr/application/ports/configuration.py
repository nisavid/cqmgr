"""Ports for validated configuration and mutable selection state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from cqmgr.application.configuration import ConfigSnapshot, SelectionState


class ConfigurationRepositoryError(Exception):
    """A local configuration repository cannot provide trustworthy state."""


class UnsupportedConfigurationSchemaError(ConfigurationRepositoryError):
    """Stored state uses a newer schema that cannot be interpreted safely."""


class ConfigurationRepositoryOperationalError(ConfigurationRepositoryError):
    """Filesystem or locking failure prevented trustworthy local-state access."""


class ConfigRepository(Protocol):
    """Read and atomically update operator-owned configuration."""

    async def read(self) -> ConfigSnapshot:
        """Read one validated, migrated snapshot."""
        raise NotImplementedError

    async def update(self, transform: ConfigTransform) -> ConfigSnapshot:
        """Atomically apply one read-modify-write transformation."""
        raise NotImplementedError


class SelectionStateRepository(Protocol):
    """Read and atomically update independent mutable selection state."""

    async def read(self) -> SelectionState:
        """Read one validated, migrated state snapshot."""
        raise NotImplementedError

    async def update(self, transform: SelectionTransform) -> SelectionState:
        """Atomically apply one read-modify-write transformation."""
        raise NotImplementedError


type ConfigTransform = Callable[[ConfigSnapshot], ConfigSnapshot]
type SelectionTransform = Callable[[SelectionState], SelectionState]
