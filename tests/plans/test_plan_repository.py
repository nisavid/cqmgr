"""Authenticated local and exported plan repository contracts."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_IMODE
from typing import cast, override
from unittest.mock import patch

import pytest
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule

import cqmgr.adapters.persistence.plans as plan_persistence
from cqmgr.adapters.persistence import secrets as secret_adapter
from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock
from cqmgr.adapters.persistence.plans import LocalPlanRepository
from cqmgr.adapters.persistence.secrets import NativeSecretStore
from cqmgr.adapters.serialization.plans import PlanCodec
from cqmgr.application.ports.plans import EncodedPlan, PlanLease, PlanRepositoryStatus
from cqmgr.application.ports.secrets import (
    SecretBackendKind,
    SecretPurpose,
    SecretStoreOutcome,
    SecretStoreProbe,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
)
from cqmgr.domain.plan_consumption import PlanLedgerRecord
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


class _MemoryConsumptionStore:
    """Process-local fake for the immutable native consumption-marker seam."""

    def __init__(self) -> None:
        self.values: dict[SecretStoreReference, SecretValue] = {}

    def probe(self) -> SecretStoreProbe:
        return SecretStoreProbe(SecretBackendKind.MACOS_KEYCHAIN, "test-memory")

    def get_consumption_marker(
        self, reference: SecretStoreReference
    ) -> SecretStoreOutcome:
        value = self.values.get(reference)
        if value is None:
            return SecretStoreOutcome(SecretStoreStatus.MISSING)
        return SecretStoreOutcome.available(value)

    def create_consumption_marker(
        self,
        reference: SecretStoreReference,
        secret: SecretValue,
    ) -> SecretStoreOutcome:
        if reference in self.values:
            return SecretStoreOutcome(SecretStoreStatus.CONFLICT)
        self.values[reference] = secret
        return SecretStoreOutcome(SecretStoreStatus.CREATED)

    def delete(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        if reference.purpose is SecretPurpose.PLAN_CONSUMPTION:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        if self.values.pop(reference, None) is None:
            return SecretStoreOutcome(SecretStoreStatus.MISSING)
        return SecretStoreOutcome(SecretStoreStatus.DELETED)


class _MemoryNativeKeyring:
    """Concrete keyring seam for shared-lock composition coverage."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


_CONSUMPTION_STORES: dict[Path, _MemoryConsumptionStore] = {}


def _repository(
    root: Path,
    *,
    lock: NativePlanInterprocessLock | None = None,
) -> LocalPlanRepository:
    resolved = root.resolve()
    store = _CONSUMPTION_STORES.setdefault(resolved, _MemoryConsumptionStore())
    return LocalPlanRepository(root, store, lock=lock)


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
            "hmac-sha256:" + ("c" * 64),
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


def _lease_worker(
    root: str,
    digest: str,
    key: bytes,
    queue: multiprocessing.Queue[str],
) -> None:
    repository = _repository(Path(root))
    outcome = repository.acquire_lease(
        digest,
        SecretValue(key),
        NOW,
        lease_duration=timedelta(seconds=2),
    )
    queue.put(outcome.status.value)


def _export_worker(
    root: str,
    plan_bytes: bytes,
    digest: str,
    destination: str,
    queue: multiprocessing.Queue[str],
) -> None:
    repository = _repository(Path(root))
    outcome = repository.export(EncodedPlan(plan_bytes, digest), Path(destination))
    queue.put(outcome.status.value)


def _crash_holding_lock(path: str) -> None:
    with NativePlanInterprocessLock(Path(path)):
        os._exit(0)


def _attempt_fake_provider_write(
    repository: LocalPlanRepository,
    digest: str,
    key: SecretValue,
    writes: list[str],
) -> PlanRepositoryStatus:
    leased = repository.acquire_lease(digest, key, NOW)
    if leased.lease is None:
        return leased.status
    dispatched = repository.mark_dispatched(leased.lease, key, NOW)
    if dispatched.status is PlanRepositoryStatus.DISPATCHED:
        writes.append(digest)
    return dispatched.status


def test_local_store_is_content_addressed_and_export_is_atomic_owner_only(
    tmp_path: Path,
) -> None:
    """Local and explicit exported copies preserve exact authenticated bytes."""
    encoded = _encoded()
    repository = _repository(tmp_path / "repository")

    assert repository.store(encoded, PLAN_KEY).status is PlanRepositoryStatus.STORED
    loaded = repository.load(encoded.digest, PLAN_KEY, NOW)
    assert loaded.status is PlanRepositoryStatus.AVAILABLE
    assert loaded.plan_bytes == encoded.bytes
    assert loaded.state is PlanLedgerState.AVAILABLE
    assert loaded.authenticated is True

    exported = tmp_path / "review" / "request.plan"
    assert repository.export(encoded, exported).status is PlanRepositoryStatus.EXPORTED
    assert repository.read_export(exported).plan_bytes == encoded.bytes
    assert S_IMODE(exported.stat().st_mode) == PRIVATE_FILE_MODE
    assert S_IMODE((tmp_path / "repository").stat().st_mode) == PRIVATE_DIRECTORY_MODE


def test_repository_and_native_marker_store_compose_under_one_shared_lock(
    tmp_path: Path,
) -> None:
    """Repository-owned locking encloses marker I/O without recursive acquire."""
    root = tmp_path / "repository"
    lock = NativePlanInterprocessLock(root / ".plan-repository.lock")
    backend = _MemoryNativeKeyring()
    with patch.object(
        secret_adapter,
        "_trusted_native_backend_types",
        return_value={type(backend): SecretBackendKind.MACOS_KEYCHAIN},
    ):
        marker_store = NativeSecretStore(backend, lock)
    repository = LocalPlanRepository(root, marker_store, lock=lock)

    assert repository.store(_encoded(), PLAN_KEY).status is PlanRepositoryStatus.STORED


def test_first_use_durably_publishes_each_created_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh nested repository entries are flushed from parent to leaf."""
    flushed: list[Path] = []
    monkeypatch.setattr(plan_persistence, "_fsync_directory", flushed.append)
    root = tmp_path / "fresh" / "nested" / "repository"

    _repository(root)

    assert flushed == [
        tmp_path,
        tmp_path / "fresh",
        tmp_path / "fresh" / "nested",
        root,
        root,
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_repository_replaces_every_explicit_plan_reader(tmp_path: Path) -> None:
    """Plan and ledger paths retain only the active Windows account ACL."""
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    executable = rf"{system_root}\System32\icacls.exe"
    subprocess.run(  # noqa: S603
        [
            executable,
            str(tmp_path),
            "/grant",
            "*S-1-1-0:(OI)(CI)R",
            "*S-1-5-32-544:(OI)(CI)R",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)

    for path in (
        tmp_path,
        tmp_path / "plans",
        tmp_path / "state",
        _plan_path(tmp_path, encoded.digest),
        _state_path(tmp_path, encoded.digest),
    ):
        completed = subprocess.run(  # noqa: S603
            [executable, str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        acl = completed.stdout.casefold()
        assert "everyone:" not in acl
        assert "s-1-1-0:" not in acl
        assert "builtin\\users:" not in acl
        assert "builtin\\administrators:" not in acl
        assert "s-1-5-32-544:" not in acl


def test_lease_dispatch_terminal_consumption_is_single_use(tmp_path: Path) -> None:
    """A plan is durably consumed before dispatch and can never be leased again."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)

    leased = repository.acquire_lease(encoded.digest, PLAN_KEY, NOW)
    assert leased.status is PlanRepositoryStatus.LEASED
    assert leased.lease is not None
    assert (
        repository.acquire_lease(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    assert (
        repository.mark_dispatched(leased.lease, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.DISPATCHED
    )
    assert (
        repository.complete(leased.lease, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONSUMED
    )
    consumed = repository.load(encoded.digest, PLAN_KEY, NOW)
    assert consumed.status is PlanRepositoryStatus.CONSUMED
    assert consumed.plan_bytes == encoded.bytes
    assert (
        repository.acquire_lease(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONSUMED
    )


def test_stale_pre_dispatch_lease_recovers_but_dispatch_crash_quarantines(
    tmp_path: Path,
) -> None:
    """Recovery distinguishes safe pre-dispatch abandonment from ambiguity."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    first = repository.acquire_lease(
        encoded.digest, PLAN_KEY, NOW, lease_duration=timedelta(seconds=1)
    )
    assert first.lease is not None

    recovered = repository.acquire_lease(
        encoded.digest,
        PLAN_KEY,
        NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=1),
    )
    assert recovered.status is PlanRepositoryStatus.LEASED
    assert recovered.lease is not None
    repository.mark_dispatched(recovered.lease, PLAN_KEY, NOW + timedelta(seconds=2))

    restarted = _repository(tmp_path)
    assert (
        restarted.load(
            encoded.digest,
            PLAN_KEY,
            NOW + timedelta(seconds=2, milliseconds=500),
        ).status
        is PlanRepositoryStatus.DISPATCHED
    )
    assert (
        restarted.mark_dispatched(
            recovered.lease, PLAN_KEY, NOW + timedelta(seconds=3)
        ).status
        is PlanRepositoryStatus.QUARANTINED
    )
    loaded = restarted.load(encoded.digest, PLAN_KEY, NOW + timedelta(seconds=3))
    assert loaded.status is PlanRepositoryStatus.QUARANTINED
    assert loaded.state is PlanLedgerState.QUARANTINED
    assert loaded.reason == StableSymbol("ambiguous-dispatch")


def test_dispatch_deadline_quarantines_when_plan_bytes_are_missing(
    tmp_path: Path,
) -> None:
    """Durable dispatch evidence reaches quarantine without readable plan bytes."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    leased = repository.acquire_lease(
        encoded.digest, PLAN_KEY, NOW, lease_duration=timedelta(seconds=1)
    )
    assert leased.lease is not None
    assert (
        repository.mark_dispatched(leased.lease, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.DISPATCHED
    )
    _plan_path(tmp_path, encoded.digest).unlink()

    deadline = NOW + timedelta(seconds=1)
    recovered = repository.mark_dispatched(leased.lease, PLAN_KEY, deadline)

    assert recovered.status is PlanRepositoryStatus.QUARANTINED
    assert recovered.state is PlanLedgerState.QUARANTINED
    assert recovered.reason == StableSymbol("ambiguous-dispatch")
    loaded = repository.load(encoded.digest, PLAN_KEY, deadline)
    assert loaded.status is PlanRepositoryStatus.QUARANTINED
    assert loaded.state is PlanLedgerState.QUARANTINED
    assert loaded.reason == StableSymbol("ambiguous-dispatch")


def test_load_recovers_dispatch_deadline_before_decoding_corrupt_plan(
    tmp_path: Path,
) -> None:
    """Load preserves durable dispatch recovery when plan bytes are corrupt."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    leased = repository.acquire_lease(
        encoded.digest, PLAN_KEY, NOW, lease_duration=timedelta(seconds=1)
    )
    assert leased.lease is not None
    repository.mark_dispatched(leased.lease, PLAN_KEY, NOW)
    _plan_path(tmp_path, encoded.digest).write_bytes(b"corrupt")

    loaded = repository.load(encoded.digest, PLAN_KEY, NOW + timedelta(seconds=1))

    assert loaded.status is PlanRepositoryStatus.QUARANTINED
    assert loaded.state is PlanLedgerState.QUARANTINED
    assert loaded.reason == StableSymbol("ambiguous-dispatch")


def test_concurrent_processes_obtain_at_most_one_lease_and_call_no_provider(
    tmp_path: Path,
) -> None:
    """The local ledger serializes separate cqmgr processes before any provider."""
    encoded = _encoded()
    _repository(tmp_path).store(encoded, PLAN_KEY)
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[str] = context.Queue()
    processes = [
        context.Process(
            target=_lease_worker,
            args=(str(tmp_path), encoded.digest, KEY, queue),
        )
        for _ in range(CONTENDING_PROCESS_COUNT)
    ]
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


def test_foreign_key_is_rejected_across_processes_before_provider_dispatch(
    tmp_path: Path,
) -> None:
    """A second installation cannot turn a copied digest into Apply authority."""
    encoded = _encoded()
    _repository(tmp_path).store(encoded, PLAN_KEY)
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[str] = context.Queue()
    process = context.Process(
        target=_lease_worker,
        args=(str(tmp_path), encoded.digest, b"f" * 32, queue),
    )

    process.start()
    process.join(timeout=10)

    assert process.exitcode == 0
    assert queue.get(timeout=1) == PlanRepositoryStatus.CONFLICT.value
    assert (
        _repository(tmp_path)
        .acquire_lease(
            encoded.digest,
            PLAN_KEY,
            NOW,
        )
        .status
        is PlanRepositoryStatus.LEASED
    )


def test_pre_dispatch_authentication_failure_proves_zero_fake_provider_writes(
    tmp_path: Path,
) -> None:
    """The Apply-shaped guard reaches a fake provider only after durable dispatch."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    writes: list[str] = []

    rejected = _attempt_fake_provider_write(
        repository,
        encoded.digest,
        SecretValue(b"f" * 32),
        writes,
    )
    dispatched = _attempt_fake_provider_write(
        repository,
        encoded.digest,
        PLAN_KEY,
        writes,
    )

    assert rejected is PlanRepositoryStatus.CONFLICT
    assert dispatched is PlanRepositoryStatus.DISPATCHED
    assert writes == [encoded.digest]


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


def test_lock_timeout_is_typed_at_every_plan_repository_operation(
    tmp_path: Path,
) -> None:
    """OS-lock contention cannot escape any plan repository result boundary."""
    root = tmp_path / "repository"
    lock_path = root / "repository.lock"
    repository = _repository(
        root,
        lock=NativePlanInterprocessLock(
            lock_path,
            timeout_seconds=0.01,
            poll_seconds=0.001,
        ),
    )
    encoded = _encoded()
    setup = _repository(root)
    setup.store(encoded, PLAN_KEY)
    leased = setup.acquire_lease(encoded.digest, PLAN_KEY, NOW)
    assert leased.lease is not None
    with NativePlanInterprocessLock(lock_path):
        outcomes = (
            repository.store(encoded, PLAN_KEY),
            repository.load(encoded.digest, PLAN_KEY, NOW),
            repository.export(encoded, tmp_path / "export.plan"),
            repository.read_export(tmp_path / "export.plan"),
            repository.acquire_lease(encoded.digest, PLAN_KEY, NOW),
            repository.mark_dispatched(leased.lease, PLAN_KEY, NOW),
            repository.complete(leased.lease, PLAN_KEY, NOW),
            repository.quarantine(
                leased.lease, StableSymbol("lock-failure"), PLAN_KEY, NOW
            ),
        )

    assert {outcome.status for outcome in outcomes} == {PlanRepositoryStatus.FAILED}


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
    repository = _repository(tmp_path / "repository")
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
    repository = _repository(tmp_path)
    assert (
        repository.load(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.MISSING
    )
    assert (
        repository.load("not-a-digest", PLAN_KEY, NOW).status
        is PlanRepositoryStatus.MISSING
    )
    repository.store(encoded, PLAN_KEY)

    plan_path = _plan_path(tmp_path, encoded.digest)
    plan_path.chmod(0o644)
    assert (
        repository.load(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.FAILED
    )
    plan_path.chmod(PRIVATE_FILE_MODE)

    leased = repository.acquire_lease(encoded.digest, PLAN_KEY, NOW)
    assert leased.lease is not None
    repository.mark_dispatched(leased.lease, PLAN_KEY, NOW)
    repository.complete(leased.lease, PLAN_KEY, NOW)

    state_path = _state_path(tmp_path, encoded.digest)
    state_path.unlink()
    assert (
        repository.load(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.QUARANTINED
    )
    assert (
        repository.acquire_lease(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.QUARANTINED
    )
    state = json.loads(state_path.read_text())
    state["schema"] = "cqmgr.plan-state/v2"
    state_path.write_text(json.dumps(state))
    assert (
        repository.load(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.FAILED
    )


def test_repeat_store_preserves_and_reports_terminal_state(tmp_path: Path) -> None:
    """Idempotent storage cannot misreport or reset a consumed plan."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    leased = repository.acquire_lease(encoded.digest, PLAN_KEY, NOW)
    assert leased.lease is not None
    repository.mark_dispatched(leased.lease, PLAN_KEY, NOW)
    repository.complete(leased.lease, PLAN_KEY, NOW)

    repeated = repository.store(encoded, PLAN_KEY)

    assert repeated.status is PlanRepositoryStatus.CONSUMED
    assert repeated.state is PlanLedgerState.CONSUMED


def test_foreign_authentication_key_cannot_issue_apply_authority(
    tmp_path: Path,
) -> None:
    """A digest-valid local plan remains inapplicable to another installation."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)

    foreign = repository.acquire_lease(
        encoded.digest,
        SecretValue(b"f" * 32),
        NOW,
    )

    assert foreign.status is PlanRepositoryStatus.CONFLICT
    assert foreign.lease is None
    assert repository.acquire_lease(encoded.digest, PLAN_KEY, NOW).status is (
        PlanRepositoryStatus.LEASED
    )


def test_authentication_field_tampering_blocks_load_and_lease(tmp_path: Path) -> None:
    """A digest-valid envelope cannot acquire authority with a forged HMAC."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    envelope = json.loads(encoded.bytes)
    envelope["authentication"] = "hmac-sha256:" + ("0" * 64)
    plan_path = _plan_path(tmp_path, encoded.digest)
    plan_path.write_text(
        json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"
    )
    plan_path.chmod(PRIVATE_FILE_MODE)

    loaded = repository.load(encoded.digest, PLAN_KEY, NOW)
    leased = repository.acquire_lease(encoded.digest, PLAN_KEY, NOW)

    assert loaded.status is PlanRepositoryStatus.CONFLICT
    assert loaded.authenticated is False
    assert leased.status is PlanRepositoryStatus.CONFLICT
    assert leased.authenticated is False
    assert leased.lease is None


def test_valid_shape_ledger_rollback_cannot_resurrect_a_consumed_plan(
    tmp_path: Path,
) -> None:
    """Replaying an unsigned available record fails closed after consumption."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    leased = repository.acquire_lease(encoded.digest, PLAN_KEY, NOW)
    assert leased.lease is not None
    repository.mark_dispatched(leased.lease, PLAN_KEY, NOW)
    repository.complete(leased.lease, PLAN_KEY, NOW)
    _state_path(tmp_path, encoded.digest).write_text(
        json.dumps(
            {
                "lease_expires_at": None,
                "lease_token": None,
                "reason": None,
                "schema": "cqmgr.plan-state/v1",
                "state": "available",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )

    assert repository.acquire_lease(encoded.digest, PLAN_KEY, NOW).status is (
        PlanRepositoryStatus.FAILED
    )


def test_authentic_ledger_replay_cannot_authorize_a_second_provider_write(
    tmp_path: Path,
) -> None:
    """The immutable native marker closes rollback outside the filesystem."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    state_path = _state_path(tmp_path, encoded.digest)
    authentic_available_state = state_path.read_bytes()
    writes: list[str] = []

    first = _attempt_fake_provider_write(
        repository,
        encoded.digest,
        PLAN_KEY,
        writes,
    )
    marker_store = _CONSUMPTION_STORES[tmp_path.resolve()]
    marker_reference = next(iter(marker_store.values))
    assert marker_store.delete(marker_reference).status is SecretStoreStatus.UNSUPPORTED
    state_path.write_bytes(authentic_available_state)
    replay = _attempt_fake_provider_write(
        repository,
        encoded.digest,
        PLAN_KEY,
        writes,
    )

    assert first is PlanRepositoryStatus.DISPATCHED
    assert replay is PlanRepositoryStatus.QUARANTINED
    assert writes == [encoded.digest]


def test_plan_expiry_caps_lease_and_is_revalidated_at_dispatch(tmp_path: Path) -> None:
    """An expired plan cannot acquire or spend dispatch authority."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)

    leased = repository.acquire_lease(
        encoded.digest,
        PLAN_KEY,
        NOW + timedelta(minutes=14),
        lease_duration=timedelta(minutes=10),
    )
    assert leased.status is PlanRepositoryStatus.LEASED
    assert leased.lease is not None
    assert leased.lease.expires_at == NOW + PLAN_LIFETIME
    assert (
        repository.mark_dispatched(leased.lease, PLAN_KEY, NOW + PLAN_LIFETIME).status
        is PlanRepositoryStatus.EXPIRED
    )
    assert (
        repository.acquire_lease(encoded.digest, PLAN_KEY, NOW + PLAN_LIFETIME).status
        is PlanRepositoryStatus.EXPIRED
    )


def test_concurrent_different_exports_never_overwrite(tmp_path: Path) -> None:
    """Only one complete plan can claim an explicit export destination."""
    first = _encoded()
    decoded = PlanCodec.decode(first.bytes)
    second = PlanCodec.encode(
        replace(decoded.plan, target=QuotaQuantity(9, QuotaUnit("1"))), KEY
    )
    destination = tmp_path / "request.plan"
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[str] = context.Queue()
    processes = [
        context.Process(
            target=_export_worker,
            args=(
                str(tmp_path / f"repository-{index}"),
                plan.bytes,
                plan.digest,
                str(destination),
                queue,
            ),
        )
        for index, plan in enumerate((first, second))
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    statuses = [queue.get(timeout=1) for _ in processes]
    assert statuses.count(PlanRepositoryStatus.EXPORTED.value) == 1
    assert statuses.count(PlanRepositoryStatus.CONFLICT.value) == 1
    assert destination.read_bytes() in {first.bytes, second.bytes}


def test_export_acl_failure_leaves_no_published_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed privacy boundary cannot leave review bytes at the final path."""
    encoded = _encoded()
    repository = _repository(tmp_path / "repository")
    destination = tmp_path / "review" / "request.plan"

    def fail_final_path(path: Path) -> None:
        if path == destination:
            message = "injected ACL failure"
            raise OSError(message)

    monkeypatch.setattr(plan_persistence, "_restrict_windows_acl", fail_final_path)

    assert repository.export(encoded, destination).status is PlanRepositoryStatus.FAILED
    assert not destination.exists()


def test_lease_validation_conflicts_expiry_and_quarantine_are_durable(
    tmp_path: Path,
) -> None:
    """Wrong, stale and ambiguous lease transitions remain provider-free."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    with pytest.raises(ValueError, match="positive"):
        repository.acquire_lease(
            encoded.digest, PLAN_KEY, NOW, lease_duration=timedelta()
        )
    assert repository.load(encoded.digest, SecretValue(b"short"), NOW).status is (
        PlanRepositoryStatus.FAILED
    )
    assert (
        repository.acquire_lease(
            encoded.digest,
            SecretValue(b"short"),
            NOW,
        ).status
        is PlanRepositoryStatus.FAILED
    )
    assert (
        repository.acquire_lease("bad", PLAN_KEY, NOW).status
        is PlanRepositoryStatus.MISSING
    )

    leased = repository.acquire_lease(
        encoded.digest, PLAN_KEY, NOW, lease_duration=timedelta(seconds=1)
    )
    assert leased.lease is not None
    wrong = replace(leased.lease, token="wrong-token")  # noqa: S106
    assert (
        repository.mark_dispatched(wrong, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    assert (
        repository.complete(wrong, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    assert (
        repository.quarantine(wrong, StableSymbol("ambiguous"), PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    with pytest.raises(TypeError, match="StableSymbol"):
        repository.quarantine(
            leased.lease,
            cast("StableSymbol", "ambiguous"),
            PLAN_KEY,
            NOW,
        )
    assert (
        repository.mark_dispatched(
            leased.lease, PLAN_KEY, NOW + timedelta(seconds=1)
        ).status
        is PlanRepositoryStatus.EXPIRED
    )

    second = repository.acquire_lease(
        encoded.digest, PLAN_KEY, NOW + timedelta(seconds=2)
    )
    assert second.lease is not None
    assert (
        repository.quarantine(
            second.lease,
            StableSymbol("operator-interrupted"),
            PLAN_KEY,
            NOW,
        ).status
        is PlanRepositoryStatus.QUARANTINED
    )
    assert (
        repository.acquire_lease(encoded.digest, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.QUARANTINED
    )


def test_dispatch_and_completion_are_idempotent_for_the_exact_lease(
    tmp_path: Path,
) -> None:
    """A retry after a local response loss cannot create a second dispatch."""
    encoded = _encoded()
    repository = _repository(tmp_path)
    repository.store(encoded, PLAN_KEY)
    leased = repository.acquire_lease(encoded.digest, PLAN_KEY, NOW)
    assert leased.lease is not None
    assert (
        repository.mark_dispatched(leased.lease, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.DISPATCHED
    )
    wrong = replace(leased.lease, token="wrong-token")  # noqa: S106
    assert (
        repository.complete(wrong, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    assert (
        repository.mark_dispatched(leased.lease, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONFLICT
    )
    assert (
        repository.complete(leased.lease, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONSUMED
    )
    assert (
        repository.complete(leased.lease, PLAN_KEY, NOW).status
        is PlanRepositoryStatus.CONSUMED
    )


def test_lease_value_rejects_malformed_identity() -> None:
    """Opaque lease authority cannot be reconstructed from malformed values."""
    with pytest.raises(ValueError, match="sha256"):
        PlanLease("bad", "token", NOW)
    with pytest.raises(ValueError, match="token"):
        PlanLease("sha256:" + ("d" * 64), "", NOW)


@pytest.mark.parametrize(
    ("state", "token", "expires_at", "reason", "error", "message"),
    [
        (
            cast("PlanLedgerState", "available"),
            None,
            None,
            None,
            TypeError,
            "state",
        ),
        (PlanLedgerState.AVAILABLE, cast("str", 7), None, None, ValueError, "token"),
        (PlanLedgerState.AVAILABLE, "", None, None, ValueError, "token"),
        (
            PlanLedgerState.AVAILABLE,
            None,
            None,
            cast("StableSymbol", "reason"),
            TypeError,
            "reason",
        ),
        (PlanLedgerState.AVAILABLE, "token", None, None, ValueError, "available"),
        (PlanLedgerState.LEASED, None, NOW, None, ValueError, "leased"),
        (PlanLedgerState.LEASED, "token", None, None, ValueError, "leased"),
        (
            PlanLedgerState.LEASED,
            "token",
            NOW,
            StableSymbol("reason"),
            ValueError,
            "leased",
        ),
        (PlanLedgerState.CONSUMED, None, None, None, ValueError, "consumed"),
        (PlanLedgerState.CONSUMED, "token", NOW, None, ValueError, "consumed"),
        (
            PlanLedgerState.CONSUMED,
            "token",
            None,
            StableSymbol("reason"),
            ValueError,
            "consumed",
        ),
        (
            PlanLedgerState.QUARANTINED,
            None,
            NOW,
            StableSymbol("reason"),
            ValueError,
            "quarantined",
        ),
        (PlanLedgerState.QUARANTINED, None, None, None, ValueError, "quarantined"),
    ],
)
def test_ledger_record_rejects_ambiguous_state_shapes(  # noqa: PLR0913
    state: PlanLedgerState,
    token: str | None,
    expires_at: datetime | None,
    reason: StableSymbol | None,
    error: type[Exception],
    message: str,
) -> None:
    """Every persisted state has one fail-closed domain representation."""
    with pytest.raises(error, match=message):
        PlanLedgerRecord(state, token, expires_at, reason)


class PlanRepositoryStateMachine(RuleBasedStateMachine):
    """Generated single-use transitions over the public repository seam."""

    def __init__(self) -> None:
        """Create one isolated real-filesystem repository model."""
        super().__init__()
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = _repository(Path(self.temporary.name))
        self.encoded = _encoded()
        self.repository.store(self.encoded, PLAN_KEY)
        self.model = PlanLedgerState.AVAILABLE
        self.lease: PlanLease | None = None
        self.now = NOW

    @precondition(
        lambda self: (
            self.model is PlanLedgerState.AVAILABLE and self.now < NOW + PLAN_LIFETIME
        )
    )
    @rule()
    def lease_available_plan(self) -> None:
        """Acquire the only lease from available state."""
        outcome = self.repository.acquire_lease(self.encoded.digest, PLAN_KEY, self.now)
        assert outcome.status is PlanRepositoryStatus.LEASED
        assert outcome.lease is not None
        self.lease = outcome.lease
        self.model = PlanLedgerState.LEASED

    @precondition(
        lambda self: (
            self.model is PlanLedgerState.AVAILABLE and self.now >= NOW + PLAN_LIFETIME
        )
    )
    @rule()
    def expired_plan_stays_unleased(self) -> None:
        """Once plan evidence expires, generated transitions cannot revive it."""
        outcome = self.repository.acquire_lease(self.encoded.digest, PLAN_KEY, self.now)
        assert outcome.status is PlanRepositoryStatus.EXPIRED

    @precondition(lambda self: self.model is PlanLedgerState.LEASED)
    @rule()
    def abandon_lease(self) -> None:
        """Recover an expired lease without consuming the plan."""
        assert self.lease is not None
        self.now = self.lease.expires_at
        outcome = self.repository.load(self.encoded.digest, PLAN_KEY, self.now)
        assert outcome.state is PlanLedgerState.AVAILABLE
        self.model = PlanLedgerState.AVAILABLE
        self.lease = None

    @precondition(lambda self: self.model is PlanLedgerState.LEASED)
    @rule()
    def dispatch(self) -> None:
        """Durably consume the plan before the simulated provider boundary."""
        assert self.lease is not None
        outcome = self.repository.mark_dispatched(self.lease, PLAN_KEY, self.now)
        assert outcome.state is PlanLedgerState.DISPATCHED
        self.model = PlanLedgerState.DISPATCHED

    @precondition(lambda self: self.model is PlanLedgerState.DISPATCHED)
    @rule()
    def finish(self) -> None:
        """Record one terminal result for a dispatched plan."""
        assert self.lease is not None
        outcome = self.repository.complete(self.lease, PLAN_KEY, self.now)
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
            self.lease,
            StableSymbol("state-machine-ambiguity"),
            PLAN_KEY,
            self.now,
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
        outcome = self.repository.acquire_lease(self.encoded.digest, PLAN_KEY, self.now)
        assert outcome.state is self.model

    @override
    def teardown(self) -> None:
        """Remove the generated isolated repository."""
        self.temporary.cleanup()


TestPlanRepositoryStateMachine = PlanRepositoryStateMachine.TestCase
