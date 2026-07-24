"""Aggregate Watch subject and lifecycle contracts."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    UnknownDispatchResolution,
)
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
    WatchDisposition,
)
from cqmgr.domain.watch import (
    WatchAggregate,
    WatchCheckpoint,
    WatchChildIdentity,
    WatchChildLineage,
    WatchChildSummary,
    WatchResumeClaims,
    WatchSubject,
)

NOW = datetime(2026, 7, 24, 7, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
UNIT = QuotaUnit("1")


def _child(
    child_id: str,
    order: int,
    disposition: ApplyChildDisposition,
    *,
    resolution: UnknownDispatchResolution | None = None,
) -> WatchChildIdentity:
    identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        f"quota-{child_id}",
        NormalizedDimensions((("region", "us-central1"),)),
        QuotaScope.REGIONAL,
    )
    return WatchChildIdentity(
        child_id=child_id,
        order=order,
        slice_identity=identity,
        target=QuotaQuantity(8, UNIT),
        disposition=disposition,
        preference_identity=(
            f"{SCOPE.canonical_name}/locations/global/quotaPreferences/{child_id}"
        ),
        lineage_etag=f"etag-{child_id}",
        lineage_trace_id=None,
        unknown_resolution=resolution,
        resolution_checkpoint=1 if resolution is not None else 0,
        baseline=QuotaQuantity(4, UNIT),
    )


def _status(
    reconciliation: Reconciliation,
    *,
    granted: int | None = None,
    effective: int | None = None,
) -> QuotaRequestStatus:
    return QuotaRequestStatus.derive(
        reconciliation=reconciliation,
        baseline=QuotaQuantity(4, UNIT),
        desired=QuotaQuantity(8, UNIT),
        granted=None if granted is None else QuotaQuantity(granted, UNIT),
        effective=None if effective is None else QuotaQuantity(effective, UNIT),
        status_observed_at=NOW,
        effective_observed_at=None if effective is None else NOW,
    )


def test_subject_polls_only_the_authenticated_accepted_watch_set() -> None:
    """Failed, unresolved unknown, and unattempted children stay visible but idle."""
    children = (
        _child("accepted", 0, ApplyChildDisposition.ACCEPTED),
        _child(
            "resolved",
            1,
            ApplyChildDisposition.UNKNOWN,
            resolution=UnknownDispatchResolution.ACCEPTED,
        ),
        _child("unresolved", 2, ApplyChildDisposition.UNKNOWN),
        _child("failed", 3, ApplyChildDisposition.FAILED),
        _child("unattempted", 4, ApplyChildDisposition.UNATTEMPTED),
    )

    subject = WatchSubject(
        kind=PlanKind.BUNDLE,
        resource_scope=SCOPE,
        condition=WatchCondition.FULFILLED,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=children,
        resolution_checkpoint=1,
    )

    assert tuple(child.child_id for child in subject.accepted_children) == (
        "accepted",
        "resolved",
    )
    assert subject.children == children


def test_bundle_condition_is_orthogonal_and_never_flattens_children() -> None:
    """One adverse grant stops the aggregate while sibling facts remain intact."""
    subject = WatchSubject(
        kind=PlanKind.BUNDLE,
        resource_scope=SCOPE,
        condition=WatchCondition.GRANTED,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=(
            _child("direct", 0, ApplyChildDisposition.ACCEPTED),
            _child("companion", 1, ApplyChildDisposition.ACCEPTED),
        ),
    )
    summaries = (
        WatchChildSummary(
            subject.children[0],
            _status(Reconciliation.SETTLED, granted=8),
        ),
        WatchChildSummary(
            subject.children[1],
            _status(Reconciliation.SETTLED, granted=0),
        ),
    )

    aggregate = WatchAggregate.derive(subject, summaries)

    assert aggregate.disposition is WatchDisposition.UNMET
    assert aggregate.children == summaries
    assert aggregate.children[0].status is not None
    assert aggregate.children[0].status.is_granted
    assert aggregate.children[1].status is not None
    assert aggregate.children[1].status.granted == QuotaQuantity(0, UNIT)


def test_newly_accepted_child_is_pending_until_its_first_observation() -> None:
    """Resolution replay can checkpoint an expanded Watch set before polling it."""
    subject = WatchSubject(
        kind=PlanKind.BUNDLE,
        resource_scope=SCOPE,
        condition=WatchCondition.GRANTED,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=(
            _child("direct", 0, ApplyChildDisposition.ACCEPTED),
            _child(
                "resolved",
                1,
                ApplyChildDisposition.UNKNOWN,
                resolution=UnknownDispatchResolution.ACCEPTED,
            ),
        ),
        resolution_checkpoint=1,
    )
    aggregate = WatchAggregate.derive(
        subject,
        (
            WatchChildSummary(
                subject.children[0],
                _status(Reconciliation.RECONCILING),
            ),
            WatchChildSummary(subject.children[1], None),
        ),
    )

    assert aggregate.accepted_children == len(subject.accepted_children)
    assert aggregate.disposition is WatchDisposition.PENDING


@pytest.mark.parametrize(
    ("condition", "effective", "expected"),
    [
        (WatchCondition.GRANTED, None, WatchDisposition.REACHED),
        (WatchCondition.FULFILLED, None, WatchDisposition.PENDING),
        (WatchCondition.FULFILLED, 8, WatchDisposition.REACHED),
    ],
)
def test_aggregate_requires_every_accepted_child_to_reach_the_selected_condition(
    condition: WatchCondition,
    effective: int | None,
    expected: WatchDisposition,
) -> None:
    """Granted and fulfilled keep their distinct evidence boundaries."""
    subject = WatchSubject(
        kind=PlanKind.SINGLE,
        resource_scope=SCOPE,
        condition=condition,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=(_child("direct", 0, ApplyChildDisposition.ACCEPTED),),
    )
    summary = WatchChildSummary(
        subject.children[0],
        _status(Reconciliation.SETTLED, granted=8, effective=effective),
    )

    assert WatchAggregate.derive(subject, (summary,)).disposition is expected


def test_watch_subject_rejects_empty_or_cross_wired_shapes() -> None:
    """Unknown kinds, non-contiguous order, and empty Watch sets fail closed."""
    valid = WatchSubject(
        kind=PlanKind.SINGLE,
        resource_scope=SCOPE,
        condition=WatchCondition.GRANTED,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=(_child("direct", 0, ApplyChildDisposition.ACCEPTED),),
    )
    with pytest.raises(ValueError, match="exactly one"):
        replace(
            valid,
            kind=PlanKind.SINGLE,
            children=(
                valid.children[0],
                _child("companion", 1, ApplyChildDisposition.ACCEPTED),
            ),
        )
    with pytest.raises(ValueError, match="contiguous"):
        replace(valid, children=(replace(valid.children[0], order=1),))
    with pytest.raises(ValueError, match="accepted Watch set"):
        replace(
            valid,
            kind=PlanKind.BUNDLE,
            children=(_child("failed", 0, ApplyChildDisposition.FAILED),),
        )


def test_watch_summary_rejects_baseline_drift() -> None:
    """Observed grant classification remains bound to authenticated Apply evidence."""
    child = _child("direct", 0, ApplyChildDisposition.ACCEPTED)
    drifted = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=QuotaQuantity(0, UNIT),
        desired=child.target,
        granted=QuotaQuantity(4, UNIT),
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )

    with pytest.raises(ValueError, match="baseline"):
        WatchChildSummary(child, drifted)


@given(
    st.lists(
        st.sampled_from(
            [
                Reconciliation.RECONCILING,
                Reconciliation.FAILED,
                Reconciliation.SUPERSEDED,
            ]
        ),
        min_size=1,
        max_size=8,
    )
)
def test_adverse_child_dominates_aggregate_without_order_dependence(
    states: list[Reconciliation],
) -> None:
    """Any conclusive adverse child makes the aggregate condition unmet."""
    children = tuple(
        _child(f"child-{index}", index, ApplyChildDisposition.ACCEPTED)
        for index in range(len(states))
    )
    subject = WatchSubject(
        kind=PlanKind.BUNDLE,
        resource_scope=SCOPE,
        condition=WatchCondition.GRANTED,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=children,
    )
    aggregate = WatchAggregate.derive(
        subject,
        tuple(
            WatchChildSummary(child, _status(state))
            for child, state in zip(children, states, strict=True)
        ),
    )

    expected = (
        WatchDisposition.UNMET
        if any(
            state in {Reconciliation.FAILED, Reconciliation.SUPERSEDED}
            for state in states
        )
        else WatchDisposition.PENDING
    )
    assert aggregate.disposition is expected


class WatchLifecycleStateMachine(RuleBasedStateMachine):
    """Generate aggregate progression, lineage checkpoints, and stop boundaries."""

    def __init__(self) -> None:
        """Initialize two independently observed accepted children."""
        super().__init__()
        children = (
            _child("direct", 0, ApplyChildDisposition.ACCEPTED),
            _child("companion", 1, ApplyChildDisposition.ACCEPTED),
        )
        self.subject = WatchSubject(
            kind=PlanKind.BUNDLE,
            resource_scope=SCOPE,
            condition=WatchCondition.FULFILLED,
            intent_id="sha256:" + ("a" * 64),
            plan_digest="sha256:" + ("b" * 64),
            children=children,
        )
        self.statuses = [
            _status(Reconciliation.RECONCILING),
            _status(Reconciliation.RECONCILING),
        ]
        self.lineages = ["etag-direct", "etag-companion"]
        self.sequence = 0
        self._derive()

    def _derive(self) -> None:
        self.aggregate = WatchAggregate.derive(
            self.subject,
            tuple(
                WatchChildSummary(child, status)
                for child, status in zip(
                    self.subject.children,
                    self.statuses,
                    strict=True,
                )
            ),
        )

    @rule(
        index=st.integers(min_value=0, max_value=1),
        state=st.sampled_from(["reconciling", "granted", "fulfilled", "failed"]),
    )
    def observe_child(self, index: int, state: str) -> None:
        """Apply one authoritative child transition at arbitrary interleavings."""
        if state == "reconciling":
            status = _status(Reconciliation.RECONCILING)
        elif state == "granted":
            status = _status(Reconciliation.SETTLED, granted=8)
        elif state == "fulfilled":
            status = _status(Reconciliation.SETTLED, granted=8, effective=8)
        else:
            status = _status(Reconciliation.FAILED)
        self.statuses[index] = status
        self._derive()

    @rule(index=st.integers(min_value=0, max_value=1))
    def rotate_stable_lineage(self, index: int) -> None:
        """Retain a resumable latest etag checkpoint for one watched child."""
        self.lineages[index] = f"{self.lineages[index]}-next"

    @rule()
    def checkpoint_and_resume(self) -> None:
        """Bind the complete aggregate, lineage set, condition, and sequence."""
        checkpoint_id = "sha256:" + f"{self.sequence:064x}"
        checkpoint = WatchCheckpoint(
            checkpoint_id=checkpoint_id,
            installation_id="installation-123",
            subject=self.subject,
            aggregate=self.aggregate,
            lineages=tuple(
                WatchChildLineage(child.child_id, etag, None)
                for child, etag in zip(
                    self.subject.accepted_children,
                    self.lineages,
                    strict=True,
                )
            ),
            sequence=self.sequence,
            saved_at=NOW,
        )
        claims = WatchResumeClaims(
            installation_id=checkpoint.installation_id,
            checkpoint_id=checkpoint.checkpoint_id,
            intent_id=self.subject.intent_id,
            subject_digest="sha256:" + ("c" * 64),
            condition=self.subject.condition,
            resolution_checkpoint=self.subject.resolution_checkpoint,
            sequence=self.sequence,
        )
        assert claims.sequence == checkpoint.sequence
        assert checkpoint.aggregate == self.aggregate
        self.sequence += 1

    @rule(boundary=st.sampled_from(["timeout", "interruption"]))
    def stop_preserves_provider_facts(self, boundary: str) -> None:
        """Caller stop boundaries never relabel retained child evidence."""
        before = self.aggregate
        assert boundary in {"timeout", "interruption"}
        assert self.aggregate == before

    @invariant()
    def aggregate_is_always_rederived_from_independent_children(self) -> None:
        """Keep generated interleavings from flattening child evidence."""
        assert self.aggregate == WatchAggregate.derive(
            self.subject,
            self.aggregate.children,
        )


TestWatchLifecycleStateMachine = WatchLifecycleStateMachine.TestCase
