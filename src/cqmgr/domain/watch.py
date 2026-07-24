"""Provider-neutral aggregate Watch subjects, checkpoints, and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    UnknownDispatchResolution,
)
from cqmgr.domain.diagnostics import Diagnostic
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity, QuotaQuantity
from cqmgr.domain.schemas import WATCH_EVENT_SCHEMA
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.status import (
    QuotaRequestStatus,
    WatchCondition,
    WatchDisposition,
)
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.domain.results import OperationResult


class WatchEventKind(StrEnum):
    """Closed material records in one Watch stream."""

    INITIAL = "initial"
    CHILD_STATUS_CHANGED = "child-status-changed"
    UNKNOWN_RESOLUTION_RECORDED = "unknown-resolution-recorded"
    ACCEPTED_WATCH_SET_CHANGED = "accepted-watch-set-changed"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class WatchChildIdentity:
    """One immutable ordered Apply child retained by a Watch subject."""

    child_id: str
    order: int
    slice_identity: EffectiveQuotaSliceIdentity
    target: QuotaQuantity
    disposition: ApplyChildDisposition
    preference_identity: str
    lineage_etag: str | None
    lineage_trace_id: str | None
    unknown_resolution: UnknownDispatchResolution | None = None
    resolution_checkpoint: int = 0
    baseline: QuotaQuantity | None = None

    def __post_init__(self) -> None:  # noqa: C901, PLR0912
        """Reject identities that cannot be tied to durable Apply evidence."""
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "Watch child_id must be non-empty"
            raise ValueError(msg)
        if (
            isinstance(self.order, bool)
            or not isinstance(self.order, int)
            or self.order < 0
        ):
            msg = "Watch child order must be a non-negative integer"
            raise ValueError(msg)
        if not isinstance(self.slice_identity, EffectiveQuotaSliceIdentity):
            msg = "Watch child slice_identity must be exact"
            raise TypeError(msg)
        if not isinstance(self.target, QuotaQuantity):
            msg = "Watch child target must be a QuotaQuantity"
            raise TypeError(msg)
        if self.baseline is not None and (
            not isinstance(self.baseline, QuotaQuantity)
            or self.baseline.unit != self.target.unit
        ):
            msg = "Watch child baseline must match the target unit"
            raise ValueError(msg)
        if not isinstance(self.disposition, ApplyChildDisposition):
            msg = "Watch child disposition must be an ApplyChildDisposition"
            raise TypeError(msg)
        if (
            not isinstance(self.preference_identity, str)
            or not self.preference_identity
        ):
            msg = "Watch child preference_identity must be non-empty"
            raise ValueError(msg)
        for name, value in (
            ("lineage_etag", self.lineage_etag),
            ("lineage_trace_id", self.lineage_trace_id),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                msg = f"{name} must be None or non-empty"
                raise ValueError(msg)
        if self.unknown_resolution is not None and (
            not isinstance(self.unknown_resolution, UnknownDispatchResolution)
            or self.disposition is not ApplyChildDisposition.UNKNOWN
        ):
            msg = "unknown resolution requires an unknown Apply child"
            raise ValueError(msg)
        if (
            isinstance(self.resolution_checkpoint, bool)
            or not isinstance(self.resolution_checkpoint, int)
            or self.resolution_checkpoint < 0
        ):
            msg = "resolution checkpoint must be a non-negative integer"
            raise ValueError(msg)
        if (self.unknown_resolution is None) != (self.resolution_checkpoint == 0):
            msg = "resolution checkpoint must match resolution evidence"
            raise ValueError(msg)
        if self.watchable and (
            self.lineage_etag is None and self.lineage_trace_id is None
        ):
            msg = "accepted Watch child requires provider lineage"
            raise ValueError(msg)

    @property
    def watchable(self) -> bool:
        """Whether authenticated Apply evidence admits this child to Watch."""
        return self.disposition is ApplyChildDisposition.ACCEPTED or (
            self.disposition is ApplyChildDisposition.UNKNOWN
            and self.unknown_resolution is UnknownDispatchResolution.ACCEPTED
        )


@dataclass(frozen=True, slots=True)
class WatchSubject:
    """Complete single or bundle intent selected from one durable Apply record."""

    kind: PlanKind
    resource_scope: ResourceScope
    condition: WatchCondition
    intent_id: str
    plan_digest: str
    children: tuple[WatchChildIdentity, ...]
    resolution_checkpoint: int = 0

    def __post_init__(self) -> None:  # noqa: C901
        """Bind exact ordered children and require a nonempty accepted Watch set."""
        if not isinstance(self.kind, PlanKind):
            msg = "Watch subject kind must be a PlanKind"
            raise TypeError(msg)
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "Watch subject resource_scope must be a ResourceScope"
            raise TypeError(msg)
        if not isinstance(self.condition, WatchCondition):
            msg = "Watch condition must be a WatchCondition"
            raise TypeError(msg)
        for name, value in (
            ("intent_id", self.intent_id),
            ("plan_digest", self.plan_digest),
        ):
            if not isinstance(value, str) or not value:
                msg = f"Watch subject {name} must be non-empty"
                raise ValueError(msg)
        if not isinstance(self.children, tuple) or not self.children:
            msg = "Watch subject requires ordered children"
            raise ValueError(msg)
        if self.kind is PlanKind.SINGLE and len(self.children) != 1:
            msg = "single Watch subject requires exactly one child"
            raise ValueError(msg)
        if tuple(child.order for child in self.children) != tuple(
            range(len(self.children))
        ):
            msg = "Watch child order must be contiguous"
            raise ValueError(msg)
        if len({child.child_id for child in self.children}) != len(self.children):
            msg = "Watch child identities must be unique"
            raise ValueError(msg)
        if any(
            child.slice_identity.resource_scope != self.resource_scope
            for child in self.children
        ):
            msg = "Watch children must use the subject resource scope"
            raise ValueError(msg)
        if (
            isinstance(self.resolution_checkpoint, bool)
            or not isinstance(self.resolution_checkpoint, int)
            or self.resolution_checkpoint < 0
        ):
            msg = "resolution checkpoint must be a non-negative integer"
            raise ValueError(msg)
        if not self.accepted_children:
            msg = "Watch subject requires a nonempty accepted Watch set"
            raise ValueError(msg)

    @property
    def accepted_children(self) -> tuple[WatchChildIdentity, ...]:
        """Return only children backed by accepted dispatch evidence."""
        return tuple(child for child in self.children if child.watchable)


@dataclass(frozen=True, slots=True)
class WatchChildSummary:
    """One subject child with its latest status when it is watchable."""

    child: WatchChildIdentity
    status: QuotaRequestStatus | None

    def __post_init__(self) -> None:
        """Keep non-watchable children visible without invented observations."""
        if not isinstance(self.child, WatchChildIdentity):
            msg = "Watch summary child must be a WatchChildIdentity"
            raise TypeError(msg)
        if self.status is not None and not isinstance(self.status, QuotaRequestStatus):
            msg = "Watch summary status must be QuotaRequestStatus or None"
            raise TypeError(msg)
        if not self.child.watchable and self.status is not None:
            msg = "non-watchable children cannot carry status"
            raise ValueError(msg)
        if self.status is not None and self.status.desired != self.child.target:
            msg = "Watch child status must retain its bound target"
            raise ValueError(msg)
        if self.status is not None and self.status.baseline != self.child.baseline:
            msg = "Watch child status must retain its bound baseline"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WatchAggregate:
    """Selected condition over ordered child summaries."""

    condition: WatchCondition
    disposition: WatchDisposition
    accepted_children: int
    children: tuple[WatchChildSummary, ...]

    @classmethod
    def derive(
        cls,
        subject: WatchSubject,
        children: tuple[WatchChildSummary, ...],
    ) -> WatchAggregate:
        """Derive the aggregate without combining quantities or axes."""
        if tuple(item.child for item in children) != subject.children:
            msg = "Watch aggregate summaries must match the complete subject"
            raise ValueError(msg)
        watched = tuple(item for item in children if item.child.watchable)
        dispositions = tuple(
            item.status.watch(subject.condition)
            for item in watched
            if item.status is not None
        )
        if any(item is WatchDisposition.UNMET for item in dispositions):
            disposition = WatchDisposition.UNMET
        elif len(dispositions) == len(watched) and all(
            item is WatchDisposition.REACHED for item in dispositions
        ):
            disposition = WatchDisposition.REACHED
        else:
            disposition = WatchDisposition.PENDING
        return cls(
            condition=subject.condition,
            disposition=disposition,
            accepted_children=len(watched),
            children=children,
        )


@dataclass(frozen=True, slots=True)
class WatchChildLineage:
    """Last continuously observed provider lineage for one watched child."""

    child_id: str
    etag: str | None
    trace_id: str | None

    def __post_init__(self) -> None:
        """Require one stable child identity and at least one lineage value."""
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "Watch lineage child_id must be non-empty"
            raise ValueError(msg)
        for name, value in (("etag", self.etag), ("trace_id", self.trace_id)):
            if value is not None and (not isinstance(value, str) or not value):
                msg = f"Watch lineage {name} must be None or non-empty"
                raise ValueError(msg)
        if self.etag is None and self.trace_id is None:
            msg = "Watch lineage requires an etag or stable trace ID"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WatchCheckpoint:
    """Durable authenticated observation checkpoint referenced by resume."""

    checkpoint_id: str
    installation_id: str
    subject: WatchSubject
    aggregate: WatchAggregate
    lineages: tuple[WatchChildLineage, ...]
    sequence: int
    saved_at: datetime

    def __post_init__(self) -> None:
        """Bind one durable aggregate and every accepted child's lineage."""
        for name, value in (
            ("checkpoint_id", self.checkpoint_id),
            ("installation_id", self.installation_id),
        ):
            if not isinstance(value, str) or not value:
                msg = f"Watch checkpoint {name} must be non-empty"
                raise ValueError(msg)
        if not isinstance(self.subject, WatchSubject) or not isinstance(
            self.aggregate, WatchAggregate
        ):
            msg = "Watch checkpoint subject and aggregate must be typed"
            raise TypeError(msg)
        if (
            tuple(item.child for item in self.aggregate.children)
            != self.subject.children
        ):
            msg = "Watch checkpoint aggregate must match its subject"
            raise ValueError(msg)
        if (
            not isinstance(self.lineages, tuple)
            or any(not isinstance(item, WatchChildLineage) for item in self.lineages)
            or tuple(item.child_id for item in self.lineages)
            != tuple(child.child_id for child in self.subject.accepted_children)
        ):
            msg = "Watch checkpoint lineages must match the accepted Watch set"
            raise ValueError(msg)
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            msg = "Watch checkpoint sequence must be non-negative"
            raise ValueError(msg)
        require_utc(self.saved_at, "saved_at")


@dataclass(frozen=True, slots=True)
class WatchResumeClaims:
    """Authenticated non-secret controls carried by an opaque resume token."""

    installation_id: str
    checkpoint_id: str
    intent_id: str
    subject_digest: str
    condition: WatchCondition
    resolution_checkpoint: int
    sequence: int

    def __post_init__(self) -> None:
        """Reject incomplete token claims before repository access."""
        for name, value in (
            ("installation_id", self.installation_id),
            ("checkpoint_id", self.checkpoint_id),
            ("intent_id", self.intent_id),
            ("subject_digest", self.subject_digest),
        ):
            if not isinstance(value, str) or not value:
                msg = f"Watch resume {name} must be non-empty"
                raise ValueError(msg)
        if not isinstance(self.condition, WatchCondition):
            msg = "Watch resume condition must be a WatchCondition"
            raise TypeError(msg)
        for name, value in (
            ("resolution_checkpoint", self.resolution_checkpoint),
            ("sequence", self.sequence),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                msg = f"Watch resume {name} must be non-negative"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WatchResultData:
    """Terminal Watch evidence retained in the operation result."""

    subject: WatchSubject
    aggregate: WatchAggregate
    resume: str
    deadline: datetime
    elapsed_seconds: float
    last_material_observed_at: datetime

    def __post_init__(self) -> None:
        """Require the terminal result to retain one durable observation time."""
        require_utc(self.deadline, "deadline")
        require_utc(self.last_material_observed_at, "last_material_observed_at")


@dataclass(frozen=True, slots=True)
class WatchStreamEvent:
    """One initial, material, or terminal aggregate Watch record."""

    stream_id: str
    sequence: int
    event: WatchEventKind
    resume: str
    observed_at: datetime
    subject: WatchSubject
    aggregate: WatchAggregate
    child_id: str | None = None
    result: OperationResult[WatchResultData] | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    schema: str = field(default=WATCH_EVENT_SCHEMA, init=False)

    def __post_init__(self) -> None:  # noqa: C901
        """Require ordered, self-contained records and one terminal result."""
        if not isinstance(self.stream_id, str) or not self.stream_id:
            msg = "Watch stream_id must be non-empty"
            raise ValueError(msg)
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            msg = "Watch sequence must be a non-negative integer"
            raise ValueError(msg)
        if not isinstance(self.event, WatchEventKind):
            msg = "Watch event must be a WatchEventKind"
            raise TypeError(msg)
        if not isinstance(self.resume, str) or not self.resume:
            msg = "Watch resume token must be non-empty"
            raise ValueError(msg)
        require_utc(self.observed_at, "observed_at")
        if not isinstance(self.subject, WatchSubject) or not isinstance(
            self.aggregate, WatchAggregate
        ):
            msg = "Watch event subject and aggregate must be typed"
            raise TypeError(msg)
        if self.aggregate.children != tuple(
            WatchChildSummary(child, summary.status)
            for child, summary in zip(
                self.subject.children,
                self.aggregate.children,
                strict=True,
            )
        ):
            msg = "Watch aggregate must match its subject"
            raise ValueError(msg)
        is_child = self.event in {
            WatchEventKind.CHILD_STATUS_CHANGED,
            WatchEventKind.UNKNOWN_RESOLUTION_RECORDED,
            WatchEventKind.ACCEPTED_WATCH_SET_CHANGED,
        }
        if is_child != (self.child_id is not None):
            msg = "material child events must name exactly one child_id"
            raise ValueError(msg)
        if self.child_id is not None and self.child_id not in {
            child.child_id for child in self.subject.children
        }:
            msg = "Watch event child_id must belong to the subject"
            raise ValueError(msg)
        if (self.event is WatchEventKind.TERMINAL) != (self.result is not None):
            msg = "exactly the terminal Watch event carries a result"
            raise ValueError(msg)
        if self.result is not None and (
            self.result.operation.value != "request.watch"
            or self.result.resource_scope != self.subject.resource_scope
            or self.result.boundary.condition.value != self.subject.condition.value
            or self.result.data.subject != self.subject
            or self.result.data.aggregate != self.aggregate
            or self.result.data.resume != self.resume
        ):
            msg = "terminal Watch result must match its event"
            raise ValueError(msg)
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            msg = "Watch diagnostics must be a tuple of Diagnostic values"
            raise TypeError(msg)
