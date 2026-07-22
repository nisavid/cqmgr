"""Private atomic filesystem storage for quota-query snapshots and cursors."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cqmgr.adapters.persistence.windows_acl import restrict_windows_acl
from cqmgr.adapters.serialization.quota_snapshots import (
    decode_cursor_binding,
    decode_snapshot_record,
    encode_cursor_binding,
    encode_snapshot_record,
)
from cqmgr.application.ports.quota_snapshots import (
    ExpiredQuotaCursorError,
    ExpiredQuotaSnapshotError,
    MalformedQuotaCursorError,
    QuotaCursorQueryMismatchError,
    QuotaSnapshotConflictError,
    QuotaSnapshotNotFoundError,
    QuotaSnapshotOperationalError,
    QuotaSnapshotStoredDataError,
    ResolvedQuotaQueryCursor,
    UnknownQuotaCursorError,
)
from cqmgr.domain.quota_queries import OpaqueQueryCursor, QuotaQuery, QuotaQuerySnapshot

if TYPE_CHECKING:
    from collections.abc import Callable

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_OPAQUE_CURSOR = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_TOKEN_ATTEMPTS = 8


class FilesystemQuotaQuerySnapshots:
    """Persist normalized snapshots and random local cursor bindings."""

    def __init__(
        self,
        root: Path,
        *,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        """Bind one explicit installation-local private directory."""
        if not isinstance(root, Path):
            msg = "quota snapshot root must be a Path"
            raise TypeError(msg)
        self._root = root
        self._snapshots = root / "snapshots"
        self._cursors = root / "cursors"
        self._token_factory = token_factory

    def save(self, snapshot: QuotaQuerySnapshot) -> None:
        """Atomically persist one canonical complete or incomplete snapshot."""
        if not isinstance(snapshot, QuotaQuerySnapshot):
            msg = "quota snapshot repository requires QuotaQuerySnapshot"
            raise TypeError(msg)
        self._prepare()
        path = self._snapshot_path(snapshot.metadata.snapshot_id)
        data = encode_snapshot_record(snapshot)
        try:
            _publish_exclusive(path, data)
        except FileExistsError:
            try:
                existing = _read_repository_file(
                    self._root,
                    self._snapshots.name,
                    path.name,
                )
            except QuotaSnapshotOperationalError:
                raise
            except OSError as error:
                raise _operational_error(error) from error
            if existing != data:
                msg = "quota query snapshot ID conflicts with retained evidence"
                raise QuotaSnapshotConflictError(msg) from None
        except OSError as error:
            raise _operational_error(error) from error

    def load(
        self,
        snapshot_id: str,
        *,
        now: datetime,
        expected_query: QuotaQuery | None = None,
    ) -> QuotaQuerySnapshot:
        """Load one unexpired exact-query-bound snapshot."""
        _require_snapshot_id(snapshot_id)
        _require_utc_now(now)
        path = self._snapshot_path(snapshot_id)
        try:
            data = _read_repository_file(
                self._root,
                self._snapshots.name,
                path.name,
            )
        except QuotaSnapshotOperationalError:
            raise
        except FileNotFoundError as error:
            msg = "quota query snapshot is unknown"
            raise QuotaSnapshotNotFoundError(msg) from error
        except OSError as error:
            raise _operational_error(error) from error
        snapshot = decode_snapshot_record(data)
        if snapshot.metadata.snapshot_id != snapshot_id:
            msg = "stored quota snapshot identity does not match its key"
            raise QuotaSnapshotStoredDataError(msg)
        if now >= snapshot.metadata.expires_at:
            msg = "quota query snapshot has expired"
            raise ExpiredQuotaSnapshotError(msg)
        if expected_query is not None and snapshot.metadata.query != expected_query:
            msg = "explicit quota query does not match retained snapshot"
            raise QuotaCursorQueryMismatchError(msg)
        return snapshot

    def issue(
        self,
        snapshot_id: str,
        offset: int,
        *,
        now: datetime,
    ) -> OpaqueQueryCursor:
        """Issue one opaque random handle bound only in private local storage."""
        _require_utc_now(now)
        snapshot = self.load(snapshot_id, now=now)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or offset > len(snapshot.items)
        ):
            msg = "cursor offset is outside the retained snapshot"
            raise MalformedQuotaCursorError(msg)
        self._prepare()
        for _ in range(_TOKEN_ATTEMPTS):
            token = self._token_factory()
            if not isinstance(token, str) or _OPAQUE_CURSOR.fullmatch(token) is None:
                msg = "cursor token source returned an invalid handle"
                raise MalformedQuotaCursorError(msg)
            path = self._cursor_path(token)
            try:
                _publish_exclusive(path, encode_cursor_binding(snapshot_id, offset))
            except FileExistsError:
                continue
            except OSError as error:
                raise _operational_error(error) from error
            return OpaqueQueryCursor(token, snapshot_id, offset)
        msg = "could not allocate a unique local quota cursor"
        raise QuotaSnapshotOperationalError(msg)

    def resolve(
        self,
        cursor: str,
        *,
        now: datetime,
        expected_query: QuotaQuery | None = None,
    ) -> ResolvedQuotaQueryCursor:
        """Resolve one typed, unexpired, exact-query-bound continuation."""
        _require_utc_now(now)
        if not isinstance(cursor, str) or _OPAQUE_CURSOR.fullmatch(cursor) is None:
            msg = "quota query cursor is malformed"
            raise MalformedQuotaCursorError(msg)
        try:
            path = self._cursor_path(cursor)
            data = _read_repository_file(
                self._root,
                self._cursors.name,
                path.name,
            )
        except QuotaSnapshotOperationalError:
            raise
        except FileNotFoundError as error:
            msg = "quota query cursor is unknown"
            raise UnknownQuotaCursorError(msg) from error
        except OSError as error:
            raise _operational_error(error) from error
        snapshot_id, offset = decode_cursor_binding(data)
        try:
            snapshot = self.load(
                snapshot_id,
                now=now,
                expected_query=expected_query,
            )
        except ExpiredQuotaSnapshotError as error:
            msg = "quota query cursor has expired"
            raise ExpiredQuotaCursorError(msg) from error
        except QuotaSnapshotNotFoundError as error:
            msg = "quota query cursor is unknown"
            raise UnknownQuotaCursorError(msg) from error
        if offset > len(snapshot.items):
            msg = "stored quota cursor offset exceeds its snapshot"
            raise QuotaSnapshotStoredDataError(msg)
        return ResolvedQuotaQueryCursor(snapshot, offset)

    def _prepare(self) -> None:
        for directory in (self._root, self._snapshots, self._cursors):
            _ensure_private_directory(directory)

    def _snapshot_path(self, snapshot_id: str) -> Path:
        return self._snapshots / f"{_digest(snapshot_id)}.json"

    def _cursor_path(self, cursor: str) -> Path:
        return self._cursors / f"{_digest(cursor)}.json"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _require_snapshot_id(snapshot_id: object) -> None:
    if not isinstance(snapshot_id, str) or not snapshot_id:
        msg = "snapshot_id must be non-empty"
        raise ValueError(msg)


def _require_utc_now(now: object) -> None:
    if not isinstance(now, datetime):
        msg = "now must be a datetime"
        raise TypeError(msg)
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        msg = "now must be UTC"
        raise ValueError(msg)


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        msg = "quota snapshot directory must not be a symlink"
        raise QuotaSnapshotOperationalError(msg)
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    try:
        path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
        for directory in reversed(missing):
            directory.chmod(_PRIVATE_DIRECTORY_MODE)
            restrict_windows_acl(directory)
        path.chmod(_PRIVATE_DIRECTORY_MODE)
        restrict_windows_acl(path)
    except OSError as error:
        raise _operational_error(error) from error


def _publish_exclusive(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        _PRIVATE_FILE_MODE,
    )
    linked = False
    try:
        temporary.chmod(_PRIVATE_FILE_MODE)
        restrict_windows_acl(temporary)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        restrict_windows_acl(path)
        temporary.unlink()
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        if linked:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        raise


def _read_trusted_file(path: Path) -> bytes:
    if path.is_symlink():
        msg = "quota snapshot state file must not be a symlink"
        raise QuotaSnapshotOperationalError(msg)
    if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        msg = "quota snapshot state must be a regular file"
        raise QuotaSnapshotOperationalError(msg)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            msg = "quota snapshot state must be a regular file"
            raise QuotaSnapshotOperationalError(msg)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_repository_file(root: Path, directory: str, filename: str) -> bytes:
    """Read through no-follow directory descriptors on supported platforms."""
    directory_paths = (root, root / directory)
    for path in directory_paths:
        if path.is_symlink():
            msg = "quota snapshot directory must not be a symlink"
            raise QuotaSnapshotOperationalError(msg)
        if path.exists():
            restrict_windows_acl(path)
    state_path = root / directory / filename
    if state_path.is_symlink():
        msg = "quota snapshot state file must not be a symlink"
        raise QuotaSnapshotOperationalError(msg)
    if state_path.exists() and stat.S_ISREG(
        state_path.stat(follow_symlinks=False).st_mode
    ):
        restrict_windows_acl(state_path)

    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):  # pragma: win32 cover
        return _read_trusted_file(state_path)

    root_descriptor = _open_trusted_directory(root)
    try:
        directory_descriptor = _open_trusted_directory(
            directory,
            directory_descriptor=root_descriptor,
        )
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                return _read_regular_file_at(
                    filename,
                    flags,
                    directory_descriptor=directory_descriptor,
                )
            except OSError as error:
                if error.errno == errno.ELOOP:
                    msg = "quota snapshot state file must not be a symlink"
                    raise QuotaSnapshotOperationalError(msg) from error
                raise
        finally:
            os.close(directory_descriptor)
    finally:
        os.close(root_descriptor)


def _open_trusted_directory(
    path: Path | str,
    *,
    directory_descriptor: int | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, dir_fd=directory_descriptor)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        msg = "quota snapshot state directory must be a directory"
        raise QuotaSnapshotOperationalError(msg)
    return descriptor


def _read_regular_file_at(
    path: Path | str,
    flags: int,
    *,
    directory_descriptor: int | None = None,
) -> bytes:
    descriptor = os.open(path, flags, dir_fd=directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            msg = "quota snapshot state must be a regular file"
            raise QuotaSnapshotOperationalError(msg)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: win32 cover
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _operational_error(error: OSError) -> QuotaSnapshotOperationalError:
    return QuotaSnapshotOperationalError(
        f"quota snapshot filesystem operation failed: {type(error).__name__}"
    )
