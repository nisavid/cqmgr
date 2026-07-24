"""Explicit installation-trust bootstrap and fail-closed loading."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import override

import pytest

from cqmgr.application.operations.trust import (
    InstallationTrust,
    InstallationTrustLoader,
    InstallationTrustPhase,
    TrustInitializationOperations,
    TrustLoadError,
)
from cqmgr.application.ports.secrets import (
    SecretBackendKind,
    SecretPurpose,
    SecretStoreOutcome,
    SecretStoreProbe,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
)

INSTALLATION_ID = "installation-test"
REFERENCE = SecretStoreReference.generate(
    INSTALLATION_ID,
    SecretPurpose.PLAN_AUTHENTICATION,
)
KEY = SecretValue(b"k" * 32)
KEY_COMMITMENT = bytes.fromhex(
    "5e318f8cf9cbe249a30812b8ca132d691ded7a91991413558db5758575f5e01f"
)
RECOVERY_INSTALLATION_ID = "installation-recovery"
RECOVERY_REFERENCE = SecretStoreReference.generate(
    RECOVERY_INSTALLATION_ID,
    SecretPurpose.PLAN_AUTHENTICATION,
)
RECOVERY_KEY = SecretValue(b"r" * 32)


class _Repository:
    def __init__(self) -> None:
        self.value: InstallationTrust | None = None
        self.transitions: list[InstallationTrustPhase] = []
        self.restarts: list[tuple[InstallationTrust, InstallationTrust]] = []

    def load(self) -> InstallationTrust | None:
        return self.value

    def create(self, value: InstallationTrust) -> None:
        assert self.value is None
        self.value = value
        self.transitions.append(value.phase)

    def transition(
        self,
        expected: InstallationTrustPhase,
        replacement: InstallationTrust,
    ) -> None:
        assert self.value is not None
        assert self.value.phase is expected
        self.value = replacement
        self.transitions.append(replacement.phase)

    def restart_incomplete(
        self,
        expected: InstallationTrust,
        replacement: InstallationTrust,
    ) -> None:
        assert self.value == expected
        assert expected.phase is not InstallationTrustPhase.ACTIVE
        assert replacement.phase is InstallationTrustPhase.PREPARED
        self.restarts.append((expected, replacement))
        self.value = replacement
        self.transitions.append(replacement.phase)


@dataclass
class _Store:
    outcome: SecretStoreOutcome = field(
        default_factory=lambda: SecretStoreOutcome(SecretStoreStatus.MISSING)
    )
    kind: SecretBackendKind = SecretBackendKind.MACOS_KEYCHAIN
    create_status: SecretStoreStatus = SecretStoreStatus.CREATED
    verified_secret: SecretValue | None = None

    def __post_init__(self) -> None:
        self.create_calls = 0
        self.get_calls = 0
        self.outcomes: dict[SecretStoreReference, SecretStoreOutcome] = {}

    def probe(self) -> SecretStoreProbe:
        return SecretStoreProbe(
            self.kind,
            "keyring.backends.macOS.Keyring",
        )

    def get(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        self.get_calls += 1
        if reference in self.outcomes:
            return self.outcomes[reference]
        if reference == REFERENCE:
            return self.outcome
        return SecretStoreOutcome(SecretStoreStatus.MISSING)

    def create(
        self,
        reference: SecretStoreReference,
        secret: SecretValue,
    ) -> SecretStoreOutcome:
        self.create_calls += 1
        if self.create_status is SecretStoreStatus.CREATED:
            available = SecretStoreOutcome.available(self.verified_secret or secret)
            self.outcomes[reference] = available
            if reference == REFERENCE:
                self.outcome = available
        return SecretStoreOutcome(self.create_status)

    def delete(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        del reference
        message = "trust bootstrap never deletes an installation key"
        raise AssertionError(message)


class _WorkflowLock:
    def __init__(self) -> None:
        self.active = False
        self.entries = 0

    def __enter__(self) -> object:
        assert not self.active
        self.active = True
        self.entries += 1
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        assert self.active
        self.active = False


def _material() -> tuple[str, SecretStoreReference, SecretValue]:
    return INSTALLATION_ID, REFERENCE, KEY


def _recovery_material() -> tuple[str, SecretStoreReference, SecretValue]:
    return RECOVERY_INSTALLATION_ID, RECOVERY_REFERENCE, RECOVERY_KEY


def test_trust_init_is_explicit_create_once_and_activates_after_verification() -> None:
    """Fresh explicit initialization writes once, verifies, and activates."""
    repository = _Repository()
    store = _Store()

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert result.initialized
    assert result.trust is not None
    assert result.trust.phase is InstallationTrustPhase.ACTIVE
    assert repository.transitions == [
        InstallationTrustPhase.PREPARED,
        InstallationTrustPhase.CREATE_INTENT,
        InstallationTrustPhase.ACTIVE,
    ]
    assert store.create_calls == 1
    assert store.get_calls == 1


def test_trust_init_serializes_the_complete_bootstrap_workflow() -> None:
    """The workflow lock covers capability probing through key verification."""
    lock = _WorkflowLock()

    class _CheckedStore(_Store):
        @override
        def probe(self) -> SecretStoreProbe:
            assert lock.active
            return super().probe()

        @override
        def get(self, reference: SecretStoreReference) -> SecretStoreOutcome:
            assert lock.active
            return super().get(reference)

        @override
        def create(
            self,
            reference: SecretStoreReference,
            secret: SecretValue,
        ) -> SecretStoreOutcome:
            assert lock.active
            return super().create(reference, secret)

    result = TrustInitializationOperations(
        _Repository(),
        _CheckedStore(),
        material=_material,
        workflow_lock=lock,
    ).initialize()

    assert result.initialized
    assert lock.entries == 1
    assert not lock.active


def test_active_trust_is_never_recreated_when_key_is_lost() -> None:
    """A missing active key is a terminal fail-closed state."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.ACTIVE,
    )
    store = _Store()

    with pytest.raises(TrustLoadError, match="missing"):
        InstallationTrustLoader(repository, store).load()

    assert store.get_calls == 1
    assert store.create_calls == 0


def test_repeat_init_never_restarts_active_trust_with_missing_key() -> None:
    """Explicit init cannot turn a lost active authority into fresh trust."""
    repository = _Repository()
    active = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.ACTIVE,
    )
    repository.value = active
    store = _Store()

    def forbidden_material() -> tuple[str, SecretStoreReference, SecretValue]:
        message = "active authority must never be regenerated"
        raise AssertionError(message)

    result = TrustInitializationOperations(
        repository,
        store,
        material=forbidden_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert not result.initialized
    assert result.reason == "trust-unavailable"
    assert repository.value == active
    assert repository.restarts == []
    assert store.create_calls == 0


def test_loader_rejects_missing_incomplete_and_short_trust() -> None:
    """Every non-active or malformed local authority fails before creation."""
    missing = _Repository()
    with pytest.raises(TrustLoadError, match="run `cqmgr trust init`"):
        InstallationTrustLoader(missing, _Store()).load()

    incomplete = _Repository()
    incomplete.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.PREPARED,
    )
    with pytest.raises(TrustLoadError, match="incomplete"):
        InstallationTrustLoader(incomplete, _Store()).load()

    short = _Repository()
    short.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.ACTIVE,
    )
    with pytest.raises(TrustLoadError, match="inconsistent"):
        InstallationTrustLoader(
            short,
            _Store(SecretStoreOutcome.available(SecretValue(b"short"))),
        ).load()


def test_preview_loader_accepts_only_active_consistent_native_trust() -> None:
    """Lifecycle loading returns validated authority without creation."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.ACTIVE,
    )
    store = _Store(SecretStoreOutcome.available(KEY))

    loaded = InstallationTrustLoader(repository, store).load()

    assert loaded.installation_id == INSTALLATION_ID
    assert loaded.authentication_key.reveal() == KEY.reveal()
    assert loaded.keyring_mutation_capable
    assert store.create_calls == 0


def test_existing_create_intent_with_missing_key_restarts_explicit_init() -> None:
    """An explicit retry abandons a keyless candidate and creates fresh trust."""
    repository = _Repository()
    interrupted = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.CREATE_INTENT,
    )
    repository.value = interrupted
    store = _Store()

    result = TrustInitializationOperations(
        repository,
        store,
        material=_recovery_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert result.initialized
    assert result.reason == "incomplete-trust-restarted"
    assert len(repository.restarts) == 1
    assert repository.restarts[0][0] == interrupted
    assert repository.restarts[0][1].installation_id == RECOVERY_INSTALLATION_ID
    assert repository.value is not None
    assert repository.value.installation_id == RECOVERY_INSTALLATION_ID
    assert repository.value.phase is InstallationTrustPhase.ACTIVE
    assert store.create_calls == 1


def test_existing_prepared_trust_with_missing_key_restarts_explicit_init() -> None:
    """A keyless prepared candidate is not permanent installation authority."""
    repository = _Repository()
    interrupted = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.PREPARED,
    )
    repository.value = interrupted
    store = _Store()

    result = TrustInitializationOperations(
        repository,
        store,
        material=_recovery_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert result.initialized
    assert result.reason == "incomplete-trust-restarted"
    assert len(repository.restarts) == 1
    assert repository.restarts[0][0] == interrupted
    assert repository.restarts[0][1].installation_id == RECOVERY_INSTALLATION_ID
    assert repository.value is not None
    assert repository.value.installation_id == RECOVERY_INSTALLATION_ID
    assert repository.value.phase is InstallationTrustPhase.ACTIVE
    assert store.create_calls == 1


@pytest.mark.parametrize(
    "phase",
    [
        InstallationTrustPhase.PREPARED,
        InstallationTrustPhase.CREATE_INTENT,
    ],
)
@pytest.mark.parametrize(
    "status",
    [
        SecretStoreStatus.LOCKED_OR_CANCELLED,
        SecretStoreStatus.UNAVAILABLE,
        SecretStoreStatus.FAILED,
        SecretStoreStatus.CONFLICT,
    ],
)
def test_incomplete_trust_recovery_requires_exact_missing_key_evidence(
    phase: InstallationTrustPhase,
    status: SecretStoreStatus,
) -> None:
    """Ambiguous keyring evidence never replaces an incomplete candidate."""
    repository = _Repository()
    retained = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        phase,
    )
    repository.value = retained
    store = _Store(SecretStoreOutcome(status))

    def forbidden_material() -> tuple[str, SecretStoreReference, SecretValue]:
        message = "ambiguous evidence must not generate recovery material"
        raise AssertionError(message)

    result = TrustInitializationOperations(
        repository,
        store,
        material=forbidden_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert not result.initialized
    assert repository.value == retained
    assert repository.restarts == []
    assert store.create_calls == 0


def test_prepared_trust_with_available_key_remains_fail_closed() -> None:
    """A key without durable create intent cannot become active authority."""
    repository = _Repository()
    retained = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.PREPARED,
    )
    repository.value = retained
    store = _Store(SecretStoreOutcome.available(KEY))

    result = TrustInitializationOperations(
        repository,
        store,
        material=_recovery_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert not result.initialized
    assert result.reason == "trust-prepare-interrupted"
    assert repository.value == retained
    assert repository.restarts == []
    assert store.create_calls == 0


def test_existing_create_intent_with_available_key_finishes_activation() -> None:
    """A post-create crash can activate only the already-available exact key."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.CREATE_INTENT,
    )
    store = _Store(SecretStoreOutcome.available(KEY))

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert result.initialized
    assert repository.value is not None
    assert repository.value.phase is InstallationTrustPhase.ACTIVE
    assert store.create_calls == 0


def test_existing_create_intent_rejects_replaced_key() -> None:
    """Interrupted initialization cannot bless a different key at the same ref."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.CREATE_INTENT,
    )
    store = _Store(SecretStoreOutcome.available(SecretValue(b"x" * 32)))

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert not result.initialized
    assert result.reason == "trust-create-interrupted"
    assert repository.value.phase is InstallationTrustPhase.CREATE_INTENT
    assert store.create_calls == 0


def test_active_trust_rejects_replaced_key() -> None:
    """A same-length replacement cannot become active installation authority."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.ACTIVE,
    )
    store = _Store(SecretStoreOutcome.available(SecretValue(b"x" * 32)))

    with pytest.raises(TrustLoadError, match="inconsistent"):
        InstallationTrustLoader(repository, store).load()

    assert store.create_calls == 0


def test_active_trust_repeat_init_reports_existing_without_creation() -> None:
    """A healthy active installation remains create-once."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.ACTIVE,
    )
    store = _Store(SecretStoreOutcome.available(KEY))

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert not result.initialized
    assert result.reason == "already-initialized"
    assert store.create_calls == 0


@pytest.mark.parametrize(
    "kind",
    [
        SecretBackendKind.MISSING,
        SecretBackendKind.FILE_BACKED,
    ],
)
def test_unsupported_backend_blocks_init_and_loading(
    kind: SecretBackendKind,
) -> None:
    """Blocked backends never receive installation trust material."""
    repository = _Repository()
    store = _Store(kind=kind)

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
        workflow_lock=nullcontext(),
    ).initialize()

    assert not result.initialized
    assert result.reason == f"unsupported-secret-backend:{kind.value}"
    assert repository.value is None
    assert store.create_calls == 0

    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        KEY_COMMITMENT,
        InstallationTrustPhase.ACTIVE,
    )
    with pytest.raises(TrustLoadError, match="unsupported"):
        InstallationTrustLoader(repository, store).load()


def test_invalid_or_unverified_generated_material_never_activates() -> None:
    """Short material and read-after-create mismatch remain non-authoritative."""
    short_repository = _Repository()
    short = TrustInitializationOperations(
        short_repository,
        _Store(),
        material=lambda: (
            INSTALLATION_ID,
            REFERENCE,
            SecretValue(b"short"),
        ),
        workflow_lock=nullcontext(),
    ).initialize()
    assert not short.initialized
    assert short.reason == "invalid-trust-material"
    assert short_repository.value is None

    failed_repository = _Repository()
    failed = TrustInitializationOperations(
        failed_repository,
        _Store(create_status=SecretStoreStatus.FAILED),
        material=_material,
        workflow_lock=nullcontext(),
    ).initialize()
    assert not failed.initialized
    assert failed.reason == "trust-key-create-failed"

    mismatch_repository = _Repository()
    mismatch = TrustInitializationOperations(
        mismatch_repository,
        _Store(verified_secret=SecretValue(b"x" * 32)),
        material=_material,
        workflow_lock=nullcontext(),
    ).initialize()
    assert not mismatch.initialized
    assert mismatch.reason == "trust-key-verification-failed"
