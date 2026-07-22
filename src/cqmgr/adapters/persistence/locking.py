"""Portable advisory locks for local cqmgr processes."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cqmgr.application.ports.coordination import (
    CancellationToken,
    CoordinationDeadlineExceededError,
)

if TYPE_CHECKING:
    from io import BufferedRandom
    from os import PathLike
    from types import TracebackType
    from typing import Self


class InterprocessFileLock:
    """One crash-releasing exclusive advisory file lock."""

    def __init__(
        self, path: str | PathLike[str], *, poll_seconds: float = 0.01
    ) -> None:
        """Bind a lock identity without acquiring it."""
        if poll_seconds <= 0:
            msg = "lock polling interval must be positive seconds"
            raise ValueError(msg)
        self._path = Path(path)
        self._poll_seconds = poll_seconds
        self._stream: BufferedRandom | None = None

    def acquire(
        self,
        *,
        deadline: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """Acquire before the monotonic deadline or fail without side effects."""
        if self._stream is not None:
            msg = "interprocess lock is already held by this instance"
            raise RuntimeError(msg)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b")
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            while True:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                try:
                    _try_lock(stream)
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise CoordinationDeadlineExceededError from None
                    time.sleep(self._poll_seconds)
                else:
                    self._stream = stream
                    return
        except BaseException:
            stream.close()
            raise

    def release(self) -> None:
        """Release the advisory lock and its process-local file descriptor."""
        stream = self._stream
        if stream is None:
            return
        try:
            _unlock(stream)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> Self:
        """Acquire without a deadline for a short local transaction."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release after the protected transaction."""
        self.release()


if os.name == "nt":
    import msvcrt

    def _try_lock(stream: BufferedRandom) -> None:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError from error

    def _unlock(stream: BufferedRandom) -> None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(stream: BufferedRandom) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(stream: BufferedRandom) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
