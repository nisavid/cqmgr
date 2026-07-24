"""Surface-neutral append-only audit facts and verification results."""

from __future__ import annotations

import re
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
_AUDIT_RECORD_ID_PATTERN = re.compile(r"audit-[0-9]{20}\Z")
_AUDIT_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AuditRecordKind(StrEnum):
    """Closed V1 kinds of durable audit evidence."""

    PREVIEW_EVIDENCE = "preview-evidence"
    APPLY_INTENT = "apply-intent"
    APPLY_RESULT = "apply-result"
    WATCH_OBSERVATION = "watch-observation"
    ROTATION_CHECKPOINT = "rotation-checkpoint"
    CRITICAL_UNKNOWN = "critical-unknown"


class AuditFactName(StrEnum):
    """Closed V1 fact classifications with explicit retention semantics."""

    PREFERENCE = "preference"
    PREFERENCE_IDENTITY = "preference-identity"
    EXACT_SLICE = "exact-slice"
    TARGET = "target"
    ETAG = "etag"
    ACTION = "action"
    DISPOSITION = "disposition"
    PLAN_DIGEST = "plan-digest"
    PLAN_SUBJECT = "plan-subject"
    PLAN_CHILD = "plan-child"
    TARGET_STRATEGY = "target-strategy"
    PREVIOUS_SEGMENT = "previous-segment"
    PROVIDER_BODY = "provider-body"
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class AuditFact:
    """One named, explicitly scrubbed audit fact."""

    name: AuditFactName
    value: RedactedText

    def __post_init__(self) -> None:
        """Require stable names and explicitly safe text."""
        if not isinstance(self.name, AuditFactName):
            msg = "audit fact name must be an AuditFactName"
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

    def __post_init__(self) -> None:
        """Require canonical runtime identities and chain metadata."""
        if not isinstance(self.record_id, str):
            msg = "audit record identity must be a string"
            raise TypeError(msg)
        if _AUDIT_RECORD_ID_PATTERN.fullmatch(self.record_id) is None:
            msg = "audit record identity has an invalid format"
            raise ValueError(msg)
        _require_positive_integer(self.sequence, "audit record sequence")
        _require_positive_integer(self.segment, "audit record segment")
        if not isinstance(self.draft, AuditRecordDraft):
            msg = "audit record draft must be an AuditRecordDraft"
            raise TypeError(msg)
        _require_audit_hash(self.previous_hash, "audit previous hash")
        _require_audit_hash(self.record_hash, "audit record hash")


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
    INVALID_ROTATION_CHECKPOINT = "invalid-rotation-checkpoint"
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

    def __post_init__(self) -> None:
        """Require one typed failure code and exact retained-chain location."""
        if not isinstance(self.code, AuditFailureCode):
            msg = "audit verification failure code must be an AuditFailureCode"
            raise TypeError(msg)
        if isinstance(self.segment, bool) or not isinstance(self.segment, int):
            msg = "audit verification failure segment must be an integer"
            raise TypeError(msg)
        if self.segment < 0:
            msg = "audit verification failure segment must be non-negative"
            raise ValueError(msg)
        if self.sequence is not None:
            _require_positive_integer(
                self.sequence,
                "audit verification failure sequence",
            )
        if self.record_id is not None and not isinstance(self.record_id, str):
            msg = "audit verification failure record identity must be a string or None"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class AuditVerification:
    """A complete verification result for one requested retained range."""

    valid: bool
    verified_from: str | None
    verified_through: str | None
    failure: AuditVerificationFailure | None = None

    def __post_init__(self) -> None:
        """Keep valid ranges and failures mutually exclusive."""
        if not isinstance(self.valid, bool):
            msg = "audit verification valid must be a boolean"
            raise TypeError(msg)
        if self.verified_from is not None and not isinstance(self.verified_from, str):
            msg = "audit verification verified from must be a string or None"
            raise TypeError(msg)
        if self.verified_through is not None and not isinstance(
            self.verified_through, str
        ):
            msg = "audit verification verified through must be a string or None"
            raise TypeError(msg)
        if self.failure is not None and not isinstance(
            self.failure, AuditVerificationFailure
        ):
            msg = (
                "audit verification failure must be an AuditVerificationFailure or None"
            )
            raise TypeError(msg)
        if self.valid == (self.failure is not None):
            msg = "valid audit verification must have no failure"
            raise ValueError(msg)


def _require_positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an integer"
        raise TypeError(msg)
    if value < 1:
        msg = f"{name} must be positive"
        raise ValueError(msg)


def _require_audit_hash(value: object, name: str) -> None:
    if not isinstance(value, str):
        msg = f"{name} must be a string"
        raise TypeError(msg)
    if _AUDIT_HASH_PATTERN.fullmatch(value) is None:
        msg = f"{name} has an invalid format"
        raise ValueError(msg)
