"""Authenticated local and exported plan repository contracts."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_IMODE
from typing import cast, override

import pytest
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock
from cqmgr.adapters.persistence.plans import LocalPlanRepository
from cqmgr.adapters.serialization.plans import PlanCodec
from cqmgr.application.ports.plans import EncodedPlan, PlanLease, PlanRepositoryStatus
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.plans import (
    PLAN_LIFETIME,
    ContactBinding,
    PlanIncapability,
    PlanLedgerState,
    PlanPrincipal,
    QuotaRequestPlan,
    review_plan,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
KEY = b"k" * 32
PLAN_KEY = SecretValue(KEY)
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
CONTENDING_PROCESS_COUNT = 4


def _encoded():  # noqa: ANN202
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
    slice_identity = EffectiveQuotaSliceIdentity(
        resource_scope=scope,
        service="compute.googleapis.com",
        quota_id="GPUS-PER-PROJECT",
        dimensions=NormalizedDimensions(),
        quota_scope=QuotaScope.GLOBAL,
    )
    plan = QuotaRequestPlan(
        resource_scope=scope,
        slice_identity=slice_identity,
        target=QuotaQuantity(8, QuotaUnit("1")),
        effective=QuotaQuantity(4, QuotaUnit("1")),
        effective_observed_at=NOW,
        preference_name=None,
        preference_etag=None,
        principal=PlanPrincipal("principal://accounts/123"),
        contact_binding=ContactBinding(
            StableSymbol("direct-user"),
            "principal://accounts/123",
            "hmac-sha256:contact-binding",
        ),
        warnings=(),
        required_acknowledgements=(),
        acknowledgements=(),
        constraints=(),
        evidence=(),
        installation_id="installation-123",
        issued_at=NOW,
        expires_at=NOW + PLAN_LIFETIME,
    )
    return PlanCodec.encode(plan, KEY)


def _lease_worker(root: str, digest: str, queue: multiprocessing.Queue[str]) -> None:
    repository = LocalPlanRepository(Path(root))
    outcome = repository.acquire_lease(digest, NOW, lease_duration=timedelta(seconds=2))
    queue.put(outcome.status.value)


def _crash_holding_lock(path: str) -> None:
    with NativePlanInterprocessLock(Path(path)):
        os._exit(0)


def test_local_store_is_content_addressed_and_export_is_atomic_owner_only(
    tmp_path: Path,
) -> None:
    """Local and explicit exported copies preserve exact authenticated bytes."""
    encoded = _encoded()
    repository = LocalPlanRepository(tmp_path / "repository")

    assert repository.store(encoded, PLAN_KEY).status is PlanRepositoryStatus.STORED
    loaded = repository.load(encoded.digest, NOW)
    assert loaded.status is PlanRepositoryStatus.AVAILABLE
    assert loaded.plan_bytes == encoded.bytes
    assert loaded.state is PlanLedgerState.AVAILABLE

    exported = tmp_path / "review" / "request.plan"
    assert repository.export(encoded, exported).status is PlanRepositoryStatus.EXPORTED
    assert repository.read_export(exported).plan_bytes == encoded.bytes
    assert S_IMODE(exported.stat().st_mode) == PRIVATE_FILE_MODE
    assert S_IMODE((tmp_path / "repository").stat().st_mode) == PRIVATE_DIRECTORY_MODE


def test_lease_dispatch_terminal_consumption_is_single_use(tmp_path: Path) -> None:
    """A plan is durably consumed before dispatch and can never be leased again."""
    encoded = _encoded()
    repository = LocalPlanRepository(tmp_path)
    repository.store(encoded, PLAN_KEY)

    leased = repository.acquire_lease(encoded.digest, NOW)
    assert leased.status is PlanRepositoryStatus.LEASED
    assert leased.lease is not None
    assert (
        repository.acquire_lease(encoded.digest, NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    assert (
        repository.mark_dispatched(leased.lease, NOW).status
        is PlanRepositoryStatus.DISPATCHED
    )
    assert (
        repository.complete(leased.lease, NOW).status is PlanRepositoryStatus.CONSUMED
    )
    assert (
        repository.acquire_lease(encoded.digest, NOW).status
        is PlanRepositoryStatus.CONSUMED
    )


def test_stale_pre_dispatch_lease_recovers_but_dispatch_crash_quarantines(
    tmp_path: Path,
) -> None:
    """Recovery distinguishes safe pre-dispatch abandonment from ambiguity."""
    encoded = _encoded()
    repository = LocalPlanRepository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    first = repository.acquire_lease(
        encoded.digest, NOW, lease_duration=timedelta(seconds=1)
    )
    assert first.lease is not None

    recovered = repository.acquire_lease(encoded.digest, NOW + timedelta(seconds=2))
    assert recovered.status is PlanRepositoryStatus.LEASED
    assert recovered.lease is not None
    repository.mark_dispatched(recovered.lease, NOW + timedelta(seconds=2))

    restarted = LocalPlanRepository(tmp_path, owner_alive=lambda _pid: False)
    loaded = restarted.load(encoded.digest, NOW + timedelta(seconds=3))
    assert loaded.status is PlanRepositoryStatus.QUARANTINED
    assert loaded.state is PlanLedgerState.QUARANTINED
    assert loaded.reason == StableSymbol("ambiguous-dispatch")


def test_concurrent_processes_obtain_at_most_one_lease_and_call_no_provider(
    tmp_path: Path,
) -> None:
    """The local ledger serializes separate cqmgr processes before any provider."""
    encoded = _encoded()
    LocalPlanRepository(tmp_path).store(encoded, PLAN_KEY)
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[str] = context.Queue()
    processes = [
        context.Process(
            target=_lease_worker,
            args=(str(tmp_path), encoded.digest, queue),
        )
        for _ in range(CONTENDING_PROCESS_COUNT)
    ]
    provider_calls = 0

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    statuses = [queue.get(timeout=1) for _ in processes]

    assert statuses.count(PlanRepositoryStatus.LEASED.value) == 1
    assert statuses.count(PlanRepositoryStatus.CONFLICT.value) == (
        CONTENDING_PROCESS_COUNT - 1
    )
    assert provider_calls == 0


def test_process_crash_releases_os_lock_without_deleting_shared_state(
    tmp_path: Path,
) -> None:
    """An abandoned kernel lock is immediately recoverable by another process."""
    context = multiprocessing.get_context("spawn")
    lock_path = tmp_path / "state.lock"
    process = context.Process(target=_crash_holding_lock, args=(str(lock_path),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0

    with NativePlanInterprocessLock(lock_path, timeout_seconds=1):
        assert lock_path.exists()


def test_safe_review_separates_trustworthy_contents_from_apply_capability() -> None:
    """Foreign, expired, consumed and unacknowledged plans remain reviewable."""
    decoded = PlanCodec.decode(_encoded().bytes)
    applicable = review_plan(
        decoded.plan,
        digest=decoded.digest,
        authenticated=decoded.authenticate(KEY),
        local_installation_id="installation-123",
        state=PlanLedgerState.AVAILABLE,
        now=NOW,
    )
    assert applicable.apply_capability
    assert applicable.incapability_reasons == ()

    foreign = review_plan(
        decoded.plan,
        digest=decoded.digest,
        authenticated=False,
        local_installation_id="installation-foreign",
        state=PlanLedgerState.CONSUMED,
        now=NOW + PLAN_LIFETIME,
    )
    assert not foreign.apply_capability
    assert set(foreign.incapability_reasons) == {
        PlanIncapability.EXPIRED,
        PlanIncapability.FOREIGN_OR_UNAUTHENTICATED,
        PlanIncapability.INSTALLATION_MISMATCH,
        PlanIncapability.CONSUMED,
    }


def _state_path(root: Path, digest: str) -> Path:
    return root / "state" / f"{digest.removeprefix('sha256:')}.json"


def _plan_path(root: Path, digest: str) -> Path:
    return root / "plans" / f"{digest.removeprefix('sha256:')}.plan"


def test_repository_rejects_invalid_digest_bytes_and_conflicting_exports(
    tmp_path: Path,
) -> None:
    """Content addressing and explicit export paths never guess or overwrite."""
    encoded = _encoded()
    repository = LocalPlanRepository(tmp_path / "repository")
    assert (
        repository.store(EncodedPlan(b"not-json", encoded.digest), PLAN_KEY).status
        is PlanRepositoryStatus.FAILED
    )
    assert (
        repository.store(
            EncodedPlan(encoded.bytes, "sha256:" + ("0" * 64)), PLAN_KEY
        ).status
        is PlanRepositoryStatus.CONFLICT
    )
    assert repository.store(encoded, PLAN_KEY).status is PlanRepositoryStatus.STORED
    assert repository.store(encoded, PLAN_KEY).status is PlanRepositoryStatus.STORED
    assert (
        repository.store(encoded, SecretValue(b"f" * 32)).status
        is PlanRepositoryStatus.CONFLICT
    )

    exported = tmp_path / "export.plan"
    assert repository.export(encoded, exported).status is PlanRepositoryStatus.EXPORTED
    assert repository.export(encoded, exported).status is PlanRepositoryStatus.EXPORTED
    exported.write_bytes(b"different")
    assert repository.export(encoded, exported).status is PlanRepositoryStatus.CONFLICT
    assert repository.read_export(exported).status is PlanRepositoryStatus.FAILED
    assert (
        repository.read_export(tmp_path / "missing.plan").status
        is PlanRepositoryStatus.MISSING
    )


def test_repository_fail_closed_on_missing_unsafe_or_corrupt_local_state(
    tmp_path: Path,
) -> None:
    """Unsafe permissions and unsupported state never become Apply capability."""
    encoded = _encoded()
    repository = LocalPlanRepository(tmp_path)
    assert repository.load(encoded.digest, NOW).status is PlanRepositoryStatus.MISSING
    assert repository.load("not-a-digest", NOW).status is PlanRepositoryStatus.MISSING
    repository.store(encoded, PLAN_KEY)

    plan_path = _plan_path(tmp_path, encoded.digest)
    plan_path.chmod(0o644)
    assert repository.load(encoded.digest, NOW).status is PlanRepositoryStatus.FAILED
    plan_path.chmod(PRIVATE_FILE_MODE)

    state_path = _state_path(tmp_path, encoded.digest)
    state_path.unlink()
    assert repository.load(encoded.digest, NOW).status is PlanRepositoryStatus.AVAILABLE
    state = json.loads(state_path.read_text())
    state["schema"] = "cqmgr.plan-state/v2"
    state_path.write_text(json.dumps(state))
    assert repository.load(encoded.digest, NOW).status is PlanRepositoryStatus.FAILED


def test_lease_validation_conflicts_expiry_and_quarantine_are_durable(
    tmp_path: Path,
) -> None:
    """Wrong, stale and ambiguous lease transitions remain provider-free."""
    encoded = _encoded()
    repository = LocalPlanRepository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    with pytest.raises(ValueError, match="positive"):
        repository.acquire_lease(encoded.digest, NOW, lease_duration=timedelta())
    assert repository.acquire_lease("bad", NOW).status is PlanRepositoryStatus.MISSING

    leased = repository.acquire_lease(
        encoded.digest, NOW, lease_duration=timedelta(seconds=1)
    )
    assert leased.lease is not None
    wrong = replace(leased.lease, token="wrong-token")  # noqa: S106
    assert (
        repository.mark_dispatched(wrong, NOW).status is PlanRepositoryStatus.CONFLICT
    )
    assert repository.complete(wrong, NOW).status is PlanRepositoryStatus.CONFLICT
    assert (
        repository.quarantine(wrong, StableSymbol("ambiguous"), NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    with pytest.raises(TypeError, match="StableSymbol"):
        repository.quarantine(leased.lease, cast("StableSymbol", "ambiguous"), NOW)
    assert (
        repository.mark_dispatched(leased.lease, NOW + timedelta(seconds=1)).status
        is PlanRepositoryStatus.CONFLICT
    )

    second = repository.acquire_lease(encoded.digest, NOW + timedelta(seconds=2))
    assert second.lease is not None
    assert (
        repository.quarantine(
            second.lease, StableSymbol("operator-interrupted"), NOW
        ).status
        is PlanRepositoryStatus.QUARANTINED
    )
    assert (
        repository.acquire_lease(encoded.digest, NOW).status
        is PlanRepositoryStatus.QUARANTINED
    )


def test_dispatch_and_completion_are_idempotent_for_the_exact_lease(
    tmp_path: Path,
) -> None:
    """A retry after a local response loss cannot create a second dispatch."""
    encoded = _encoded()
    repository = LocalPlanRepository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    leased = repository.acquire_lease(encoded.digest, NOW)
    assert leased.lease is not None
    assert (
        repository.mark_dispatched(leased.lease, NOW).status
        is PlanRepositoryStatus.DISPATCHED
    )
    wrong = replace(leased.lease, token="wrong-token")  # noqa: S106
    assert repository.complete(wrong, NOW).status is PlanRepositoryStatus.CONFLICT
    assert (
        repository.mark_dispatched(leased.lease, NOW).status
        is PlanRepositoryStatus.DISPATCHED
    )
    assert (
        repository.complete(leased.lease, NOW).status is PlanRepositoryStatus.CONSUMED
    )
    assert (
        repository.complete(leased.lease, NOW).status is PlanRepositoryStatus.CONSUMED
    )


def test_lease_value_rejects_malformed_identity() -> None:
    """Opaque lease authority cannot be reconstructed from malformed values."""
    with pytest.raises(ValueError, match="sha256"):
        PlanLease("bad", "token", NOW)
    with pytest.raises(ValueError, match="token"):
        PlanLease("sha256:digest", "", NOW)


class PlanRepositoryStateMachine(RuleBasedStateMachine):
    """Generated single-use transitions over the public repository seam."""

    def __init__(self) -> None:
        """Create one isolated real-filesystem repository model."""
        super().__init__()
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = LocalPlanRepository(Path(self.temporary.name))
        self.encoded = _encoded()
        self.repository.store(self.encoded, PLAN_KEY)
        self.model = PlanLedgerState.AVAILABLE
        self.lease: PlanLease | None = None
        self.now = NOW
        self.provider_calls = 0

    @precondition(lambda self: self.model is PlanLedgerState.AVAILABLE)
    @rule()
    def lease_available_plan(self) -> None:
        """Acquire the only lease from available state."""
        outcome = self.repository.acquire_lease(self.encoded.digest, self.now)
        assert outcome.status is PlanRepositoryStatus.LEASED
        assert outcome.lease is not None
        self.lease = outcome.lease
        self.model = PlanLedgerState.LEASED

    @precondition(lambda self: self.model is PlanLedgerState.LEASED)
    @rule()
    def abandon_lease(self) -> None:
        """Recover an expired lease without consuming the plan."""
        assert self.lease is not None
        self.now = self.lease.expires_at
        outcome = self.repository.load(self.encoded.digest, self.now)
        assert outcome.state is PlanLedgerState.AVAILABLE
        self.model = PlanLedgerState.AVAILABLE
        self.lease = None

    @precondition(lambda self: self.model is PlanLedgerState.LEASED)
    @rule()
    def dispatch(self) -> None:
        """Durably consume the plan before the simulated provider boundary."""
        assert self.lease is not None
        outcome = self.repository.mark_dispatched(self.lease, self.now)
        assert outcome.state is PlanLedgerState.DISPATCHED
        self.model = PlanLedgerState.DISPATCHED

    @precondition(lambda self: self.model is PlanLedgerState.DISPATCHED)
    @rule()
    def finish(self) -> None:
        """Record one terminal result for a dispatched plan."""
        assert self.lease is not None
        outcome = self.repository.complete(self.lease, self.now)
        assert outcome.state is PlanLedgerState.CONSUMED
        self.model = PlanLedgerState.CONSUMED

    @precondition(
        lambda self: self.model in {PlanLedgerState.LEASED, PlanLedgerState.DISPATCHED}
    )
    @rule()
    def quarantine(self) -> None:
        """Make an ambiguous in-flight state permanently inapplicable."""
        assert self.lease is not None
        outcome = self.repository.quarantine(
            self.lease, StableSymbol("state-machine-ambiguity"), self.now
        )
        assert outcome.state is PlanLedgerState.QUARANTINED
        self.model = PlanLedgerState.QUARANTINED

    @precondition(
        lambda self: (
            self.model in {PlanLedgerState.CONSUMED, PlanLedgerState.QUARANTINED}
        )
    )
    @rule()
    def observe_terminal_state(self) -> None:
        """Prove a terminal plan cannot regain a lease."""
        outcome = self.repository.acquire_lease(self.encoded.digest, self.now)
        assert outcome.state is self.model

    @invariant()
    def provider_is_never_called(self) -> None:
        """Prove every generated repository transition remains provider-free."""
        assert self.provider_calls == 0

    @override
    def teardown(self) -> None:
        """Remove the generated isolated repository."""
        self.temporary.cleanup()


TestPlanRepositoryStateMachine = PlanRepositoryStateMachine.TestCase
