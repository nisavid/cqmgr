"""Surface-neutral append-only audit facts and verification results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.diagnostics import DiagnosticCode
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import OperationName, StableSymbol
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from datetime import datetime

AUDIT_RECORD_SCHEMA = "cqmgr.audit-record/v1"
AUDIT_GENESIS_HASH = "sha256:" + ("0" * 64)
MAX_AUDIT_QUERY_LIMIT = 1000


class AuditRecordKind(StrEnum):
    """Closed V1 kinds of durable audit evidence."""

    PREVIEW_EVIDENCE = "preview-evidence"
    APPLY_INTENT = "apply-intent"
    APPLY_RESULT = "apply-result"
    WATCH_OBSERVATION = "watch-observation"
    ROTATION_CHECKPOINT = "rotation-checkpoint"
    CRITICAL_UNKNOWN = "critical-unknown"


@dataclass(frozen=True, slots=True)
class AuditFact:
    """One named, explicitly scrubbed audit fact."""

    name: StableSymbol
    value: RedactedText

    def __post_init__(self) -> None:
        """Require stable names and explicitly safe text."""
        if not isinstance(self.name, StableSymbol):
            msg = "audit fact name must be a StableSymbol"
            raise TypeError(msg)
        if not isinstance(self.value, RedactedText):
            msg = "audit fact value must be RedactedText"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class AuditRecordDraft:
    """Safe operation evidence before journal chain metadata is assigned."""

    kind: AuditRecordKind
    operation: OperationName
    resource_scope: ResourceScope | None
    occurred_at: datetime
    outcome: StableSymbol | None = None
    correlation_id: RedactedText | None = None
    diagnostic_codes: tuple[DiagnosticCode, ...] = ()
    facts: tuple[AuditFact, ...] = ()

    def __post_init__(self) -> None:
        """Reject raw text and malformed audit values at the domain boundary."""
        if not isinstance(self.kind, AuditRecordKind):
            msg = "audit record kind must be an AuditRecordKind"
            raise TypeError(msg)
        if not isinstance(self.operation, OperationName):
            msg = "audit operation must be an OperationName"
            raise TypeError(msg)
        if self.resource_scope is not None and not isinstance(
            self.resource_scope, ResourceScope
        ):
            msg = "audit resource scope must be a ResourceScope or None"
            raise TypeError(msg)
        require_utc(self.occurred_at, "occurred_at")
        if self.outcome is not None and not isinstance(self.outcome, StableSymbol):
            msg = "audit outcome must be a StableSymbol or None"
            raise TypeError(msg)
        if self.correlation_id is not None and not isinstance(
            self.correlation_id, RedactedText
        ):
            msg = "audit correlation identity must be RedactedText or None"
            raise TypeError(msg)
        if not isinstance(self.diagnostic_codes, tuple) or any(
            not isinstance(code, DiagnosticCode) for code in self.diagnostic_codes
        ):
            msg = "audit diagnostic codes must be a tuple of DiagnosticCode values"
            raise TypeError(msg)
        if not isinstance(self.facts, tuple) or any(
            not isinstance(fact, AuditFact) for fact in self.facts
        ):
            msg = "audit facts must be a tuple of AuditFact values"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One canonical record in the retained append-only hash chain."""

    record_id: str
    sequence: int
    segment: int
    draft: AuditRecordDraft
    previous_hash: str
    record_hash: str
    schema: str = field(default=AUDIT_RECORD_SCHEMA, init=False)


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """One bounded local audit query."""

    operations: tuple[OperationName, ...] = ()
    outcomes: tuple[StableSymbol, ...] = ()
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 100
    cursor: str | None = None

    def __post_init__(self) -> None:
        """Validate the bounded query without guessing caller intent."""
        if not isinstance(self.operations, tuple) or any(
            not isinstance(value, OperationName) for value in self.operations
        ):
            msg = "audit operations must be a tuple of OperationName values"
            raise TypeError(msg)
        if not isinstance(self.outcomes, tuple) or any(
            not isinstance(value, StableSymbol) for value in self.outcomes
        ):
            msg = "audit outcomes must be a tuple of StableSymbol values"
            raise TypeError(msg)
        for name, value in (("since", self.since), ("until", self.until)):
            if value is not None:
                require_utc(value, name)
        if (
            self.since is not None
            and self.until is not None
            and self.until < self.since
        ):
            msg = "audit until cannot precede since"
            raise ValueError(msg)
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            msg = "audit query limit must be an integer"
            raise TypeError(msg)
        if not 1 <= self.limit <= MAX_AUDIT_QUERY_LIMIT:
            msg = "audit query limit must be from 1 through 1000"
            raise ValueError(msg)
        if self.cursor is not None and not isinstance(self.cursor, str):
            msg = "audit query cursor must be a string or None"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class AuditQueryPage:
    """One complete bounded page of retained audit records."""

    records: tuple[AuditRecord, ...]
    next_cursor: str | None


class AuditFailureCode(StrEnum):
    """Exact first-failure classifications for retained audit continuity."""

    MALFORMED_RECORD = "malformed-record"
    UNSUPPORTED_SCHEMA = "unsupported-schema"
    SEQUENCE_GAP = "sequence-gap"
    PREVIOUS_HASH_MISMATCH = "previous-hash-mismatch"
    RECORD_HASH_MISMATCH = "record-hash-mismatch"
    RECORD_ID_MISMATCH = "record-id-mismatch"
    NONCANONICAL_RECORD = "noncanonical-record"
    INVALID_SEGMENT_NAME = "invalid-segment-name"
    MISSING_SEGMENT = "missing-segment"
    MANIFEST_MISMATCH = "manifest-mismatch"
    RECORD_NOT_FOUND = "record-not-found"


@dataclass(frozen=True, slots=True)
class AuditVerificationFailure:
    """The exact first retained-chain failure and affected location."""

    code: AuditFailureCode
    segment: int
    sequence: int | None
    record_id: str | None


@dataclass(frozen=True, slots=True)
class AuditVerification:
    """A complete verification result for one requested retained range."""

    valid: bool
    verified_from: str | None
    verified_through: str | None
    failure: AuditVerificationFailure | None = None

    def __post_init__(self) -> None:
        """Keep valid ranges and failures mutually exclusive."""
        if self.valid == (self.failure is not None):
            msg = "valid audit verification must have no failure"
            raise ValueError(msg)
