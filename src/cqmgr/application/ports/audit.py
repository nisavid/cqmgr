"""Application port for durable local audit evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cqmgr.domain.audit import (
        AuditQuery,
        AuditQueryPage,
        AuditRecord,
        AuditRecordDraft,
        AuditVerification,
    )


class AuditJournal(Protocol):
    """Append, query, inspect, and verify local audit evidence."""

    def append(
        self,
        draft: AuditRecordDraft,
        *,
        sensitive_values: tuple[str, ...] = (),
        machine_paths: tuple[str, ...] = (),
        deduplicate: bool = False,
    ) -> AuditRecord:
        """Durably append one explicitly scrubbed record."""
        ...

    def query(self, query: AuditQuery) -> AuditQueryPage:
        """Read one complete bounded query page."""
        ...

    def inspect(self, record_id: str) -> AuditRecord | None:
        """Read one exact retained record."""
        ...

    def verify(
        self, *, from_record_id: str | None = None, through_record_id: str | None = None
    ) -> AuditVerification:
        """Verify the requested retained chain and rotation checkpoints."""
        ...
