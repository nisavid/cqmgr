"""Explicit installation-trust bootstrap and fail-closed loading."""

from __future__ import annotations

from dataclasses import dataclass, field

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


class _Repository:
    def __init__(self) -> None:
        self.value: InstallationTrust | None = None
        self.transitions: list[InstallationTrustPhase] = []

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

    def probe(self) -> SecretStoreProbe:
        return SecretStoreProbe(
            self.kind,
            "keyring.backends.macOS.Keyring",
        )

    def get(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        assert reference == REFERENCE
        self.get_calls += 1
        return self.outcome

    def create(
        self,
        reference: SecretStoreReference,
        secret: SecretValue,
    ) -> SecretStoreOutcome:
        assert reference == REFERENCE
        assert secret.reveal() == KEY.reveal()
        self.create_calls += 1
        if self.create_status is SecretStoreStatus.CREATED:
            self.outcome = SecretStoreOutcome.available(self.verified_secret or secret)
        return SecretStoreOutcome(self.create_status)

    def delete(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        del reference
        message = "trust bootstrap never deletes an installation key"
        raise AssertionError(message)


def _material() -> tuple[str, SecretStoreReference, SecretValue]:
    return INSTALLATION_ID, REFERENCE, KEY


def test_trust_init_is_explicit_create_once_and_activates_after_verification() -> None:
    """Fresh explicit initialization writes once, verifies, and activates."""
    repository = _Repository()
    store = _Store()

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
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


def test_active_trust_is_never_recreated_when_key_is_lost() -> None:
    """A missing active key is a terminal fail-closed state."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        InstallationTrustPhase.ACTIVE,
    )
    store = _Store()

    with pytest.raises(TrustLoadError, match="missing"):
        InstallationTrustLoader(repository, store).load()

    assert store.get_calls == 1
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
        InstallationTrustPhase.PREPARED,
    )
    with pytest.raises(TrustLoadError, match="incomplete"):
        InstallationTrustLoader(incomplete, _Store()).load()

    short = _Repository()
    short.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
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
        InstallationTrustPhase.ACTIVE,
    )
    store = _Store(SecretStoreOutcome.available(KEY))

    loaded = InstallationTrustLoader(repository, store).load()

    assert loaded.installation_id == INSTALLATION_ID
    assert loaded.authentication_key.reveal() == KEY.reveal()
    assert loaded.keyring_mutation_capable
    assert store.create_calls == 0


def test_existing_create_intent_with_missing_key_fails_without_recreation() -> None:
    """An interrupted write-intent never guesses that a missing key is fresh."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        InstallationTrustPhase.CREATE_INTENT,
    )
    store = _Store()

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
    ).initialize()

    assert not result.initialized
    assert result.reason == "trust-create-interrupted"
    assert store.create_calls == 0


def test_existing_create_intent_with_available_key_finishes_activation() -> None:
    """A post-create crash can activate only the already-available exact key."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        InstallationTrustPhase.CREATE_INTENT,
    )
    store = _Store(SecretStoreOutcome.available(KEY))

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
    ).initialize()

    assert result.initialized
    assert repository.value is not None
    assert repository.value.phase is InstallationTrustPhase.ACTIVE
    assert store.create_calls == 0


def test_active_trust_repeat_init_reports_existing_without_creation() -> None:
    """A healthy active installation remains create-once."""
    repository = _Repository()
    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
        InstallationTrustPhase.ACTIVE,
    )
    store = _Store(SecretStoreOutcome.available(KEY))

    result = TrustInitializationOperations(
        repository,
        store,
        material=_material,
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
    ).initialize()

    assert not result.initialized
    assert result.reason == f"unsupported-secret-backend:{kind.value}"
    assert repository.value is None
    assert store.create_calls == 0

    repository.value = InstallationTrust(
        INSTALLATION_ID,
        REFERENCE,
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
    ).initialize()
    assert not short.initialized
    assert short.reason == "invalid-trust-material"
    assert short_repository.value is None

    failed_repository = _Repository()
    failed = TrustInitializationOperations(
        failed_repository,
        _Store(create_status=SecretStoreStatus.FAILED),
        material=_material,
    ).initialize()
    assert not failed.initialized
    assert failed.reason == "trust-key-create-failed"

    mismatch_repository = _Repository()
    mismatch = TrustInitializationOperations(
        mismatch_repository,
        _Store(verified_secret=SecretValue(b"x" * 32)),
        material=_material,
    ).initialize()
    assert not mismatch.initialized
    assert mismatch.reason == "trust-key-verification-failed"
