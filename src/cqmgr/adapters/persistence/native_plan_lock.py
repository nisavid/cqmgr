"""Cross-platform OS-backed locking for native secrets and plan state."""

from __future__ import annotations

import errno
import math
import os
import sys
import time
from threading import Condition, get_ident
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
        invalid_timeout = (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        )
        invalid_poll = (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(poll_seconds)
            or poll_seconds <= 0
        )
        if invalid_timeout or invalid_poll:
            msg = "lock timeout must be non-negative and poll interval positive"
            raise ValueError(msg)
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._handle: IO[bytes] | None = None
        self._thread_condition = Condition()
        self._owner_thread_id: int | None = None

    @property
    def path(self) -> Path:
        """Return the stable local lock path."""
        return self._path

    @property
    def owned_by_current_thread(self) -> bool:
        """Return whether the caller already holds this exact lock instance."""
        with self._thread_condition:
            return self._owner_thread_id == get_ident() and self._handle is not None

    def __enter__(self) -> Self:
        """Acquire the lock within its bounded timeout."""
        deadline = time.monotonic() + self._timeout_seconds
        self._reserve_thread_ownership(deadline)
        handle: IO[bytes] | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                handle = os.fdopen(descriptor, "r+b", buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
            while True:
                try:
                    _try_lock(handle)
                    break
                except OSError as error:
                    if not _is_contention(error):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        msg = "timed out acquiring interprocess lock"
                        raise InterprocessLockTimeoutError(msg) from error
                    time.sleep(min(self._poll_seconds, remaining))
        except BaseException:
            if handle is not None:
                handle.close()
            self._release_thread_ownership()
            raise
        self._handle = handle
        return self

    def _reserve_thread_ownership(self, deadline: float) -> None:
        thread_id = get_ident()
        with self._thread_condition:
            if self._owner_thread_id == thread_id:
                msg = "interprocess lock is not reentrant"
                raise RuntimeError(msg)
            while self._owner_thread_id is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    msg = "timed out acquiring interprocess lock"
                    raise InterprocessLockTimeoutError(msg)
                self._thread_condition.wait(timeout=remaining)
            self._owner_thread_id = thread_id

    def __exit__(self, *_error: object) -> None:
        """Release the operating-system lock and close its handle."""
        with self._thread_condition:
            if self._owner_thread_id is None:
                return
            if self._owner_thread_id != get_ident():
                msg = "interprocess lock is owned by another thread"
                raise RuntimeError(msg)
            handle = self._handle
        if handle is None:  # pragma: no cover - owner enters and exits synchronously
            msg = "interprocess lock ownership is incomplete"
            raise RuntimeError(msg)
        try:
            _unlock(handle)
        finally:
            handle.close()
            self._handle = None
            self._release_thread_ownership()

    def _release_thread_ownership(self) -> None:
        with self._thread_condition:
            self._owner_thread_id = None
            self._thread_condition.notify_all()


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


def _is_contention(error: OSError) -> bool:
    return isinstance(error, BlockingIOError) or error.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
    }
