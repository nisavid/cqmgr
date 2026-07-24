"""Pure durable state for ordered non-atomic quota request Apply."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity, QuotaQuantity
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from datetime import datetime


class ApplyChildDisposition(StrEnum):
    """Immutable outcome of one ordered child dispatch."""

    ACCEPTED = "accepted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    UNATTEMPTED = "unattempted"


class UnknownDispatchResolution(StrEnum):
    """Append-only proof discovered after an unknown dispatch."""

    ACCEPTED = "accepted"
    FAILED = "failed"


class ApplyRecordState(StrEnum):
    """Aggregate durable progress of one consumed Apply intent."""

    IN_PROGRESS = "in-progress"
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CRITICAL_UNKNOWN = "critical-unknown"


@dataclass(frozen=True, slots=True)
class UnknownResolutionEvidence:
    """One append-only authenticated unknown-dispatch resolution."""

    intent_id: str
    child_id: str
    resolution: UnknownDispatchResolution
    recorded_at: datetime
    checkpoint: int = 1

    def __post_init__(self) -> None:
        """Require exact single-assignment journal evidence."""
        if not isinstance(self.intent_id, str) or not self.intent_id:
            msg = "resolution intent_id must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "resolution child_id must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.resolution, UnknownDispatchResolution):
            msg = "resolution must be an UnknownDispatchResolution"
            raise TypeError(msg)
        require_utc(self.recorded_at, "recorded_at")
        if self.checkpoint != 1:
            msg = "V1 unknown resolution checkpoint must be one"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ApplyChildRecord:
    """One ordered child, its provider intent, and immutable outcome."""

    child_id: str
    slice_identity: EffectiveQuotaSliceIdentity
    target: QuotaQuantity
    preference_identity: str
    etag: str | None
    preference_existed: bool = False
    dispatch_intent_at: datetime | None = None
    disposition: ApplyChildDisposition | None = None
    provider_outcome: StableSymbol | None = None
    outcome_recorded_at: datetime | None = None
    unknown_resolution: UnknownDispatchResolution | None = None
    resolution_recorded_at: datetime | None = None

    def __post_init__(self) -> None:  # noqa: C901, PLR0912
        """Reject impossible child histories."""
        if not isinstance(self.child_id, str) or not self.child_id:
            msg = "Apply child_id must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.slice_identity, EffectiveQuotaSliceIdentity):
            msg = "Apply child slice_identity must be exact"
            raise TypeError(msg)
        if not isinstance(self.target, QuotaQuantity):
            msg = "Apply child target must be a QuotaQuantity"
            raise TypeError(msg)
        if (
            not isinstance(self.preference_identity, str)
            or not self.preference_identity
        ):
            msg = "Apply child preference_identity must be non-empty"
            raise ValueError(msg)
        if self.etag is not None and (not isinstance(self.etag, str) or not self.etag):
            msg = "Apply child etag must be None or non-empty"
            raise ValueError(msg)
        if not isinstance(self.preference_existed, bool):
            msg = "Apply child preference_existed must be bool"
            raise TypeError(msg)
        for name, value in (
            ("dispatch_intent_at", self.dispatch_intent_at),
            ("outcome_recorded_at", self.outcome_recorded_at),
            ("resolution_recorded_at", self.resolution_recorded_at),
        ):
            if value is not None:
                require_utc(value, name)
        if self.disposition is not None and not isinstance(
            self.disposition, ApplyChildDisposition
        ):
            msg = "Apply child disposition must be an ApplyChildDisposition"
            raise TypeError(msg)
        if self.provider_outcome is not None and not isinstance(
            self.provider_outcome, StableSymbol
        ):
            msg = "Apply child provider_outcome must be a StableSymbol"
            raise TypeError(msg)
        if self.unknown_resolution is not None and not isinstance(
            self.unknown_resolution, UnknownDispatchResolution
        ):
            msg = "unknown resolution must be an UnknownDispatchResolution"
            raise TypeError(msg)
        if self.disposition is ApplyChildDisposition.UNATTEMPTED:
            if any(
                value is not None
                for value in (
                    self.dispatch_intent_at,
                    self.provider_outcome,
                    self.outcome_recorded_at,
                )
            ):
                msg = "unattempted child cannot retain dispatch evidence"
                raise ValueError(msg)
        elif self.disposition is not None and (
            self.dispatch_intent_at is None
            or self.provider_outcome is None
            or self.outcome_recorded_at is None
        ):
            msg = "terminal dispatched child requires intent and outcome evidence"
            raise ValueError(msg)
        if self.unknown_resolution is not None and (
            self.disposition is not ApplyChildDisposition.UNKNOWN
            or self.resolution_recorded_at is None
        ):
            msg = "unknown resolution requires an unknown child and timestamp"
            raise ValueError(msg)
        if self.resolution_recorded_at is not None and self.unknown_resolution is None:
            msg = "resolution timestamp requires unknown resolution evidence"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ApplyRecord:
    """Authenticated durable Apply record and ordered resume checkpoint."""

    intent_id: str
    plan_digest: str
    kind: PlanKind
    resource_scope: ResourceScope
    created_at: datetime
    children: tuple[ApplyChildRecord, ...]
    state: ApplyRecordState = ApplyRecordState.IN_PROGRESS
    finished_at: datetime | None = None
    revision: int = 0

    def __post_init__(self) -> None:  # noqa: C901
        """Require a complete, ordered, internally coherent Apply subject."""
        if not isinstance(self.intent_id, str) or not self.intent_id:
            msg = "Apply intent_id must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.plan_digest, str) or not self.plan_digest:
            msg = "Apply plan_digest must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.kind, PlanKind):
            msg = "Apply kind must be a PlanKind"
            raise TypeError(msg)
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "Apply resource_scope must be a ResourceScope"
            raise TypeError(msg)
        require_utc(self.created_at, "created_at")
        if not isinstance(self.children, tuple) or not self.children:
            msg = "Apply record requires ordered children"
            raise ValueError(msg)
        if any(
            not isinstance(child, ApplyChildRecord)
            or child.slice_identity.resource_scope != self.resource_scope
            for child in self.children
        ):
            msg = "Apply children must use the record resource scope"
            raise ValueError(msg)
        if len({child.child_id for child in self.children}) != len(self.children):
            msg = "Apply child identities must be unique"
            raise ValueError(msg)
        if not isinstance(self.state, ApplyRecordState):
            msg = "Apply state must be an ApplyRecordState"
            raise TypeError(msg)
        if self.finished_at is not None:
            require_utc(self.finished_at, "finished_at")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            msg = "Apply revision must be an integer"
            raise TypeError(msg)
        if self.revision < 0:
            msg = "Apply revision cannot be negative"
            raise ValueError(msg)
        if (self.state is ApplyRecordState.IN_PROGRESS) != (self.finished_at is None):
            msg = "only an in-progress Apply record may omit finished_at"
            raise ValueError(msg)

    def record_dispatch_intent(self, child_id: str, now: datetime) -> ApplyRecord:
        """Fsync-ready transition for the next undispatched child."""
        require_utc(now, "now")
        index = self._child_index(child_id)
        child = self.children[index]
        if self.state is not ApplyRecordState.IN_PROGRESS:
            msg = "terminal Apply child cannot be dispatched"
            raise ValueError(msg)
        if child.dispatch_intent_at is not None or child.disposition is not None:
            msg = "Apply child cannot be dispatched more than once"
            raise ValueError(msg)
        if any(
            prior.disposition is not ApplyChildDisposition.ACCEPTED
            for prior in self.children[:index]
        ):
            msg = "Apply child cannot be dispatched before prior acceptance"
            raise ValueError(msg)
        updated = replace(child, dispatch_intent_at=now)
        return self._replace_child(index, updated)

    def record_outcome(
        self,
        child_id: str,
        disposition: ApplyChildDisposition,
        provider_outcome: StableSymbol,
        now: datetime,
    ) -> ApplyRecord:
        """Record exactly one terminal result for a dispatched child."""
        require_utc(now, "now")
        if disposition not in {
            ApplyChildDisposition.ACCEPTED,
            ApplyChildDisposition.FAILED,
            ApplyChildDisposition.UNKNOWN,
        }:
            msg = "dispatched child outcome cannot be unattempted"
            raise ValueError(msg)
        index = self._child_index(child_id)
        child = self.children[index]
        if child.dispatch_intent_at is None or child.disposition is not None:
            msg = "Apply child outcome requires one unresolved dispatch intent"
            raise ValueError(msg)
        updated = replace(
            child,
            disposition=disposition,
            provider_outcome=provider_outcome,
            outcome_recorded_at=now,
        )
        return self._replace_child(index, updated)

    def recover_interrupted(self, now: datetime) -> ApplyRecord:
        """Convert intent-without-outcome to unknown and stop later children."""
        require_utc(now, "now")
        for child in self.children:
            if child.dispatch_intent_at is not None and child.disposition is None:
                recovered = self.record_outcome(
                    child.child_id,
                    ApplyChildDisposition.UNKNOWN,
                    StableSymbol("dispatch-interrupted"),
                    now,
                )
                return recovered.finalize(now)
        return self

    def finalize(self, now: datetime) -> ApplyRecord:
        """Close one Apply after acceptance or the first stopping outcome."""
        require_utc(now, "now")
        if self.state is not ApplyRecordState.IN_PROGRESS:
            return self
        dispositions = tuple(child.disposition for child in self.children)
        blocker = next(
            (
                item
                for item in dispositions
                if item in {ApplyChildDisposition.FAILED, ApplyChildDisposition.UNKNOWN}
            ),
            None,
        )
        if blocker is None and any(item is None for item in dispositions):
            msg = "Apply record cannot finish before every child is decided"
            raise ValueError(msg)
        children = self.children
        if blocker is not None:
            blocker_index = next(
                index
                for index, child in enumerate(children)
                if child.disposition is blocker
            )
            children = tuple(
                replace(child, disposition=ApplyChildDisposition.UNATTEMPTED)
                if index > blocker_index and child.disposition is None
                else child
                for index, child in enumerate(children)
            )
        state = {
            None: ApplyRecordState.ACCEPTED,
            ApplyChildDisposition.FAILED: ApplyRecordState.FAILED,
            ApplyChildDisposition.UNKNOWN: ApplyRecordState.UNKNOWN,
        }[blocker]
        return replace(
            self,
            children=children,
            state=state,
            finished_at=now,
            revision=self.revision + 1,
        )

    def mark_critical_unknown(self, now: datetime) -> ApplyRecord:
        """Close a post-dispatch persistence ambiguity without retry authority."""
        require_utc(now, "now")
        return replace(
            self,
            state=ApplyRecordState.CRITICAL_UNKNOWN,
            finished_at=now,
            revision=self.revision + 1,
        )

    def stop_unattempted(self, now: datetime) -> ApplyRecord:
        """Close a consumed Apply before any child may have been dispatched."""
        require_utc(now, "now")
        if any(child.dispatch_intent_at is not None for child in self.children):
            msg = "dispatched Apply cannot stop as wholly unattempted"
            raise ValueError(msg)
        return replace(
            self,
            children=tuple(
                replace(child, disposition=ApplyChildDisposition.UNATTEMPTED)
                for child in self.children
            ),
            state=ApplyRecordState.FAILED,
            finished_at=now,
            revision=self.revision + 1,
        )

    def resolve_unknown(
        self,
        child_id: str,
        resolution: UnknownDispatchResolution,
        now: datetime,
    ) -> ApplyRecord:
        """Append single-assignment read-after-unknown proof."""
        require_utc(now, "now")
        index = self._child_index(child_id)
        child = self.children[index]
        if child.disposition is not ApplyChildDisposition.UNKNOWN:
            msg = "only an unknown child can receive resolution evidence"
            raise ValueError(msg)
        if child.unknown_resolution is not None:
            if child.unknown_resolution is not resolution:
                msg = "conflicting unknown dispatch resolution"
                raise ValueError(msg)
            return self
        return self._replace_child(
            index,
            replace(
                child,
                unknown_resolution=resolution,
                resolution_recorded_at=now,
            ),
        )

    def _child_index(self, child_id: str) -> int:
        for index, child in enumerate(self.children):
            if child.child_id == child_id:
                return index
        msg = "unknown Apply child"
        raise ValueError(msg)

    def _replace_child(self, index: int, child: ApplyChildRecord) -> ApplyRecord:
        children = list(self.children)
        children[index] = child
        return replace(self, children=tuple(children), revision=self.revision + 1)
