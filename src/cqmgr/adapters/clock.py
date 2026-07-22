"""System clock adapter."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Supply aware UTC system time to application operations."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)
