"""Cross-platform OS-backed locking for native secrets and plan state."""

from __future__ import annotations

import os
import sys
import time
from typing import IO, TYPE_CHECKING, Self

if TYPE_CHECKING:
    from pathlib import Path

if sys.platform == "win32":  # pragma: win32 cover
    import msvcrt
else:  # pragma: win32 no cover
    import fcntl


class InterprocessLockTimeoutError(TimeoutError):
    """Raised when another live process retains a local lock."""


class NativePlanInterprocessLock:
    """An exclusive lock released automatically when its process exits."""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 5.0,
        poll_seconds: float = 0.01,
    ) -> None:
        """Configure a bounded lock acquisition."""
        if timeout_seconds < 0 or poll_seconds <= 0:
            msg = "lock timeout must be non-negative and poll interval positive"
            raise ValueError(msg)
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._handle: IO[bytes] | None = None

    @property
    def path(self) -> Path:
        """Return the stable local lock path."""
        return self._path

    def __enter__(self) -> Self:
        """Acquire the lock within its bounded timeout."""
        if self._handle is not None:
            msg = "interprocess lock is not reentrant"
            raise RuntimeError(msg)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                _try_lock(handle)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    handle.close()
                    msg = "timed out acquiring interprocess lock"
                    raise InterprocessLockTimeoutError(msg) from error
                time.sleep(self._poll_seconds)
        self._handle = handle
        return self

    def __exit__(self, *_error: object) -> None:
        """Release the operating-system lock and close its handle."""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()


def _try_lock(handle: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: win32 no cover
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: win32 no cover
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
