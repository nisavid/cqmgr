"""Surface-neutral operation results and Watch events."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from cqmgr.domain.diagnostics import Diagnostic
from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity, QuotaQuantity
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.schemas import OPERATION_RESULT_SCHEMA, WATCH_EVENT_SCHEMA
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.status import QuotaRequestStatus, WatchCondition, WatchDisposition
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from datetime import datetime

_KEBAB_SYMBOL_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_OPERATION_NAME_PATTERN = re.compile(
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*)*\Z"
)
_WATCH_OPERATION_NAME = "request.watch"


@dataclass(frozen=True, slots=True)
class StableSymbol:
    """An open, stable lowercase kebab-case enum value."""

    value: str

    def __post_init__(self) -> None:
        """Reject values outside the stable enum grammar."""
        if (
            not isinstance(self.value, str)
            or _KEBAB_SYMBOL_PATTERN.fullmatch(self.value) is None
        ):
            msg = f"invalid stable symbol: {self.value!r}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class OperationName:
    """A stable dotted domain-operation name with kebab-case segments."""

    value: str

    def __post_init__(self) -> None:
        """Reject interface spelling and malformed operation names."""
        if (
            not isinstance(self.value, str)
            or _OPERATION_NAME_PATTERN.fullmatch(self.value) is None
        ):
            msg = f"invalid operation name: {self.value!r}"
            raise ValueError(msg)


class ExitClass(IntEnum):
    """Global operation-independent process classes."""

    SUCCESS = 0
    USAGE = 2
    REJECTED_PRECONDITION = 3
    AUTHORIZATION = 4
    STALE_OR_CONFLICTING = 5
    INCOMPLETE_EVIDENCE = 6
    REQUESTED_OUTCOME_UNMET = 7
    TIMEOUT = 8
    OPERATIONAL_FAILURE = 9
    INTERRUPTED = 130


@dataclass(frozen=True, slots=True)
class OperationBoundary:
    """The condition an operation promises before reporting success."""

    condition: StableSymbol
    reached: bool

    def __post_init__(self) -> None:
        """Require a stable condition and an explicit boolean observation."""
        if not isinstance(self.condition, StableSymbol):
            msg = "boundary condition must be a StableSymbol"
            raise TypeError(msg)
        if not isinstance(self.reached, bool):
            msg = "boundary reached must be bool"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class Outcome:
    """A precise symbolic outcome paired with its closed global exit class."""

    code: StableSymbol
    exit_class: ExitClass

    def __post_init__(self) -> None:
        """Fail closed on malformed codes or unknown numeric exit classes."""
        if not isinstance(self.code, StableSymbol):
            msg = "outcome code must be a StableSymbol"
            raise TypeError(msg)
        if not isinstance(self.exit_class, ExitClass):
            msg = "outcome exit_class must be an ExitClass"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class EvidenceGap:
    """One required source, page, refresh, or local read that is missing."""

    source: StableSymbol
    reason: StableSymbol

    def __post_init__(self) -> None:
        """Require stable source and reason values."""
        if not isinstance(self.source, StableSymbol) or not isinstance(
            self.reason, StableSymbol
        ):
            msg = "evidence gap source and reason must be StableSymbol values"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class Completeness:
    """Required-observation coverage and whether incomplete data is usable."""

    is_complete: bool
    gaps: tuple[EvidenceGap, ...] = ()
    has_partial_data: bool = False

    def __post_init__(self) -> None:
        """Keep complete, partial, and unavailable evidence distinguishable."""
        if not isinstance(self.is_complete, bool) or not isinstance(
            self.has_partial_data, bool
        ):
            msg = "completeness flags must be bool"
            raise TypeError(msg)
        if not isinstance(self.gaps, tuple) or any(
            not isinstance(gap, EvidenceGap) for gap in self.gaps
        ):
            msg = "completeness gaps must be a tuple of EvidenceGap values"
            raise TypeError(msg)
        if self.is_complete and self.gaps:
            msg = "complete evidence cannot contain a gap"
            raise ValueError(msg)
        if self.is_complete and self.has_partial_data:
            msg = "complete evidence cannot be classified as partial data"
            raise ValueError(msg)
        if not self.is_complete and not self.gaps:
            msg = "incomplete evidence must identify at least one gap"
            raise ValueError(msg)

    @classmethod
    def complete(cls) -> Completeness:
        """Build complete required evidence."""
        return cls(is_complete=True)

    @classmethod
    def incomplete(cls, *gaps: EvidenceGap) -> Completeness:
        """Build incomplete evidence that preserves usable partial data."""
        return cls(is_complete=False, gaps=tuple(gaps), has_partial_data=True)

    @classmethod
    def unavailable(cls, *gaps: EvidenceGap) -> Completeness:
        """Build incomplete evidence with no usable partial observation."""
        return cls(is_complete=False, gaps=tuple(gaps), has_partial_data=False)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Safe authoritative source, time, coverage, status, and identity evidence."""

    source: StableSymbol
    observed_at: datetime
    coverage: StableSymbol
    interval_started_at: datetime | None = None
    interval_finished_at: datetime | None = None
    lifecycle_or_preview_status: RedactedText | None = None
    request_identity: RedactedText | None = None

    def __post_init__(self) -> None:
        """Require safe values and coherent UTC source observation times."""
        if not isinstance(self.source, StableSymbol) or not isinstance(
            self.coverage, StableSymbol
        ):
            msg = "provenance source and coverage must be StableSymbol values"
            raise TypeError(msg)
        require_utc(self.observed_at, "observed_at")
        for field_name, value in (
            ("interval_started_at", self.interval_started_at),
            ("interval_finished_at", self.interval_finished_at),
        ):
            if value is not None:
                require_utc(value, field_name)
        if (
            self.interval_started_at is not None
            and self.interval_finished_at is not None
            and self.interval_finished_at < self.interval_started_at
        ):
            msg = "interval_finished_at cannot precede interval_started_at"
            raise ValueError(msg)
        safe_text = (
            self.lifecycle_or_preview_status,
            self.request_identity,
        )
        if any(
            value is not None and not isinstance(value, RedactedText)
            for value in safe_text
        ):
            msg = "provenance status and request identity must use RedactedText"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class OperationResult[DataT]:
    """One versioned, surface-neutral domain operation result."""

    operation: OperationName
    resource_scope: ResourceScope | None
    boundary: OperationBoundary
    outcome: Outcome
    completeness: Completeness
    started_at: datetime
    finished_at: datetime
    data: DataT
    diagnostics: tuple[Diagnostic, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    schema: str = field(default=OPERATION_RESULT_SCHEMA, init=False)

    def _validate_types(self) -> None:
        """Reject values outside the typed result boundary."""
        if not isinstance(self.operation, OperationName):
            msg = "operation must be an OperationName"
            raise TypeError(msg)
        if self.resource_scope is not None and not isinstance(
            self.resource_scope, ResourceScope
        ):
            msg = "resource_scope must be a ResourceScope or None"
            raise TypeError(msg)
        if not isinstance(self.boundary, OperationBoundary):
            msg = "boundary must be an OperationBoundary"
            raise TypeError(msg)
        if not isinstance(self.outcome, Outcome):
            msg = "outcome must be an Outcome"
            raise TypeError(msg)
        if not isinstance(self.completeness, Completeness):
            msg = "completeness must be a Completeness"
            raise TypeError(msg)
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            msg = "diagnostics must be a tuple of Diagnostic values"
            raise TypeError(msg)
        if not isinstance(self.provenance, tuple) or any(
            not isinstance(item, Provenance) for item in self.provenance
        ):
            msg = "provenance must be a tuple of Provenance values"
            raise TypeError(msg)

    def __post_init__(self) -> None:
        """Enforce types, timestamps, boundary, completeness, and exit invariants."""
        self._validate_types()
        require_utc(self.started_at, "started_at")
        require_utc(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            msg = "finished_at cannot precede started_at"
            raise ValueError(msg)
        if self.boundary.reached and not self.completeness.is_complete:
            msg = "the operation boundary cannot be reached with incomplete evidence"
            raise ValueError(msg)

        succeeded = self.outcome.exit_class is ExitClass.SUCCESS
        if succeeded != self.boundary.reached:
            msg = "exit class 0 must exactly match the reached success boundary"
            raise ValueError(msg)
        partial_evidence = (
            not self.completeness.is_complete and self.completeness.has_partial_data
        )
        uses_incomplete_exit = self.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
        if partial_evidence != uses_incomplete_exit:
            msg = "exit class 6 must exactly identify usable incomplete evidence"
            raise ValueError(msg)

    @property
    def succeeded(self) -> bool:
        """Whether the declared boundary was reached with complete evidence."""
        return self.outcome.exit_class is ExitClass.SUCCESS


@dataclass(frozen=True, slots=True)
class ProviderPreferenceIdentity:
    """One canonical provider preference bound to an exact quota slice."""

    canonical_name: str
    slice_identity: EffectiveQuotaSliceIdentity

    def __post_init__(self) -> None:
        """Require a non-empty provider name and exact slice identity."""
        if not isinstance(self.canonical_name, str) or not self.canonical_name:
            msg = "provider preference canonical_name must be a non-empty string"
            raise ValueError(msg)
        if not isinstance(self.slice_identity, EffectiveQuotaSliceIdentity):
            msg = "provider preference slice_identity must be exact"
            raise TypeError(msg)
        expected_prefix = (
            f"{self.slice_identity.resource_scope.canonical_name}/locations/global/"
            "quotaPreferences/"
        )
        preference_id = self.canonical_name.removeprefix(expected_prefix)
        if (
            not self.canonical_name.startswith(expected_prefix)
            or not preference_id
            or "/" in preference_id
        ):
            msg = (
                "provider preference canonical_name must bind the exact resource scope"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WatchRequestIdentity:
    """The complete immutable request identity repeated by every Watch event."""

    resource_scope: ResourceScope
    condition: WatchCondition
    intent_id: str
    target: QuotaQuantity
    provider_preference: ProviderPreferenceIdentity

    def __post_init__(self) -> None:
        """Bind the watched intent to one resource, target, and provider preference."""
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "Watch resource_scope must be a ResourceScope"
            raise TypeError(msg)
        if not isinstance(self.condition, WatchCondition):
            msg = "Watch condition must be a WatchCondition"
            raise TypeError(msg)
        if not isinstance(self.intent_id, str) or not self.intent_id:
            msg = "Watch intent_id must be a non-empty string"
            raise ValueError(msg)
        if not isinstance(self.target, QuotaQuantity):
            msg = "Watch target must be a QuotaQuantity"
            raise TypeError(msg)
        if not isinstance(self.provider_preference, ProviderPreferenceIdentity):
            msg = "Watch provider_preference must be a ProviderPreferenceIdentity"
            raise TypeError(msg)
        if (
            self.provider_preference.slice_identity.resource_scope
            != self.resource_scope
        ):
            msg = "Watch provider preference must use the request resource scope"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WatchEvent[DataT]:
    """One ordered material Watch observation or terminal result."""

    stream_id: str
    sequence: int
    event: StableSymbol
    resume: str
    observed_at: datetime
    request: WatchRequestIdentity
    status: QuotaRequestStatus
    result: OperationResult[DataT] | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    schema: str = field(default=WATCH_EVENT_SCHEMA, init=False)

    def _validate_types(self) -> None:
        """Reject values outside the typed Watch-event boundary."""
        if not isinstance(self.stream_id, str) or not self.stream_id:
            msg = "stream_id must be a non-empty string"
            raise ValueError(msg)
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            msg = "sequence must be a non-negative integer"
            raise ValueError(msg)
        if not isinstance(self.event, StableSymbol):
            msg = "event must be a StableSymbol"
            raise TypeError(msg)
        if not isinstance(self.resume, str) or not self.resume:
            msg = "resume must be a non-empty string"
            raise ValueError(msg)
        require_utc(self.observed_at, "observed_at")
        if not isinstance(self.request, WatchRequestIdentity):
            msg = "request must be a WatchRequestIdentity"
            raise TypeError(msg)
        if not isinstance(self.status, QuotaRequestStatus):
            msg = "status must be a QuotaRequestStatus"
            raise TypeError(msg)
        if self.status.desired != self.request.target:
            msg = "Watch status desired value must match the request target"
            raise ValueError(msg)
        if self.result is not None and not isinstance(self.result, OperationResult):
            msg = "result must be an OperationResult or None"
            raise TypeError(msg)
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            msg = "diagnostics must be a tuple of Diagnostic values"
            raise TypeError(msg)

    def __post_init__(self) -> None:
        """Enforce typed identity, status, stream controls, and terminal shape."""
        self._validate_types()
        is_terminal = self.event.value == "terminal"
        if is_terminal != (self.result is not None):
            msg = "exactly the terminal Watch event carries an operation result"
            raise ValueError(msg)
        if self.result is not None and (
            self.result.operation.value != _WATCH_OPERATION_NAME
            or self.result.resource_scope != self.request.resource_scope
            or self.result.boundary.condition.value != self.request.condition.value
        ):
            msg = (
                "terminal Watch result must match the request operation, resource "
                "scope, and condition"
            )
            raise ValueError(msg)
        if self.result is not None:
            disposition = self.status.watch(self.request.condition)
            reached = disposition is WatchDisposition.REACHED
            unmet = disposition is WatchDisposition.UNMET
            succeeded = self.result.outcome.exit_class is ExitClass.SUCCESS
            requested_outcome_unmet = (
                self.result.outcome.exit_class is ExitClass.REQUESTED_OUTCOME_UNMET
            )
            if reached != succeeded or unmet != requested_outcome_unmet:
                msg = (
                    "terminal Watch outcome must match the attached status disposition"
                )
                raise ValueError(msg)
