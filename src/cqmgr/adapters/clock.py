"""System clock adapter."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime


class SystemClock:
    """Supply aware UTC system time to application operations."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return process-local monotonic seconds for caller deadlines."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Suspend without blocking the async runtime."""
        await asyncio.sleep(max(0.0, seconds))
