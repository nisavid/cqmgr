"""Application port for authenticated durable Apply records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.application.ports.secrets import SecretValue
    from cqmgr.domain.apply_records import (
        ApplyRecord,
        UnknownDispatchResolution,
        UnknownResolutionEvidence,
    )


class ApplyRecordRepositoryStatus(StrEnum):
    """Closed outcomes for local Apply-record persistence."""

    STORED = "stored"
    AVAILABLE = "available"
    MISSING = "missing"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ApplyRecordRepositoryOutcome:
    """One persistence result with trustworthy data only on success."""

    status: ApplyRecordRepositoryStatus
    record: ApplyRecord | None = None
    resolutions: tuple[UnknownResolutionEvidence, ...] = ()


class ApplyRecordRepository(Protocol):
    """Crash-safe authenticated store for Apply progress and reconciliation."""

    def create(
        self, record: ApplyRecord, authentication_key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        """Create one immutable intent identity at revision zero."""
        ...

    def load(
        self, intent_id: str, authentication_key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        """Load and authenticate one exact Apply record."""
        ...

    def save(
        self, record: ApplyRecord, authentication_key: SecretValue
    ) -> ApplyRecordRepositoryOutcome:
        """Commit exactly the next monotonic revision."""
        ...

    def append_unknown_resolution(  # noqa: PLR0913
        self,
        intent_id: str,
        child_id: str,
        resolution: UnknownDispatchResolution,
        recorded_at: datetime,
        authentication_key: SecretValue,
        *,
        lineage_etag: str | None = None,
        lineage_trace_id: str | None = None,
    ) -> ApplyRecordRepositoryOutcome:
        """Append one authenticated single-assignment resolution."""
        ...

    def load_unknown_resolutions(
        self,
        intent_id: str,
        authentication_key: SecretValue,
    ) -> ApplyRecordRepositoryOutcome:
        """Load the append-only resolution checkpoint independently."""
        ...

    def find_superseding_record(
        self,
        selected_intent_id: str,
        preference_identities: frozenset[str],
        authentication_key: SecretValue,
    ) -> ApplyRecordRepositoryOutcome:
        """Find the earliest later local dispatch for an exact preference set."""
        ...
