"""Clock port for deterministic application operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class Clock(Protocol):
    """Supply current UTC time without binding operations to the system clock."""

    def now(self) -> datetime:
        """Return the current aware UTC time."""
        raise NotImplementedError
