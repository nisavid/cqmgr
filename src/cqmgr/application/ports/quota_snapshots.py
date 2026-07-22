"""Application boundary for installation-local quota query snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cqmgr.domain.quota_queries import QuotaQuerySnapshot

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.domain.quota_queries import (
        OpaqueQueryCursor,
        QuotaQuery,
    )


class QuotaSnapshotRepositoryError(Exception):
    """A local quota snapshot cannot be trusted or accessed."""


class QuotaSnapshotStoredDataError(QuotaSnapshotRepositoryError, ValueError):
    """Stored quota snapshot or cursor state is malformed."""


class UnsupportedQuotaSnapshotSchemaError(QuotaSnapshotStoredDataError):
    """Stored quota snapshot state uses a newer unsupported schema."""


class QuotaSnapshotNotFoundError(QuotaSnapshotRepositoryError, LookupError):
    """The requested installation-local snapshot is unknown."""


class QuotaSnapshotConflictError(QuotaSnapshotRepositoryError, ValueError):
    """An immutable snapshot ID is already bound to different evidence."""


class ExpiredQuotaSnapshotError(QuotaSnapshotRepositoryError, ValueError):
    """The requested installation-local snapshot has expired."""


class QuotaCursorError(QuotaSnapshotRepositoryError, ValueError):
    """An opaque quota cursor cannot be resolved safely."""


class UnknownQuotaCursorError(QuotaCursorError, LookupError):
    """The cursor is well-shaped but unknown to this installation."""


class MalformedQuotaCursorError(QuotaCursorError):
    """The cursor is not a valid opaque installation-local handle."""


class ExpiredQuotaCursorError(QuotaCursorError):
    """The cursor's bound immutable snapshot has expired."""


class QuotaCursorQueryMismatchError(QuotaCursorError):
    """Explicit query options differ from the cursor-bound query."""


class QuotaSnapshotOperationalError(QuotaSnapshotRepositoryError, OSError):
    """A filesystem operation prevented trustworthy local persistence."""


@dataclass(frozen=True, slots=True)
class ResolvedQuotaQueryCursor:
    """A validated cursor resolution ready for local page consumption."""

    snapshot: QuotaQuerySnapshot
    offset: int

    def __post_init__(self) -> None:
        """Require a typed record and nonnegative logical offset."""
        if not isinstance(self.snapshot, QuotaQuerySnapshot):
            msg = "cursor resolution requires a quota query snapshot"
            raise TypeError(msg)
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or self.offset < 0
        ):
            msg = "cursor offset must be a non-negative integer"
            raise ValueError(msg)


class QuotaQuerySnapshotRepository(Protocol):
    """Persist and retrieve bounded immutable product snapshots."""

    def save(self, snapshot: QuotaQuerySnapshot) -> None:
        """Atomically persist canonical safe snapshot evidence."""
        ...

    def load(
        self,
        snapshot_id: str,
        *,
        now: datetime,
        expected_query: QuotaQuery | None = None,
    ) -> QuotaQuerySnapshot:
        """Load one unexpired snapshot with optional exact query binding."""
        ...


class QuotaQueryCursorCodec(Protocol):
    """Issue and resolve opaque installation-local snapshot continuations."""

    def issue(
        self,
        snapshot_id: str,
        offset: int,
        *,
        now: datetime,
    ) -> OpaqueQueryCursor:
        """Create a random handle bound internally to snapshot and offset."""
        ...

    def resolve(
        self,
        cursor: str,
        *,
        now: datetime,
        expected_query: QuotaQuery | None = None,
    ) -> ResolvedQuotaQueryCursor:
        """Resolve an unexpired cursor and optional exact query binding."""
        ...
