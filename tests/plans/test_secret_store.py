"""Allowlisted native secret-store port and adapter contracts."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, cast, override
from unittest.mock import patch

import keyring
import pytest
from keyring.errors import InitError, KeyringError, KeyringLocked

from cqmgr.adapters.persistence import secrets as secret_adapter
from cqmgr.adapters.persistence.native_plan_lock import (
    NativePlanInterprocessLock,
)
from cqmgr.adapters.persistence.secrets import NativeSecretStore
from cqmgr.application.ports.secrets import (
    SecretBackendKind,
    SecretPurpose,
    SecretStoreOutcome,
    SecretStoreProbe,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
)

if TYPE_CHECKING:
    from pathlib import Path

GENERATED_REFERENCE_COUNT = 32
MAXIMUM_REFERENCE_LENGTH = 128


def test_runtime_keyring_backend_is_classified_without_mutation(tmp_path: Path) -> None:
    """The installed platform backend is probed through its real concrete class."""
    backend = keyring.get_keyring()
    probe = NativeSecretStore(
        backend,
        NativePlanInterprocessLock(tmp_path / "runtime-keyring.lock"),
    ).probe()
    identity = (type(backend).__module__, type(backend).__name__)
    supported = {
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.SecretService", "Keyring"),
    }

    assert probe.backend_identity == ".".join(identity)
    assert probe.mutation_capable is (identity in supported)


class _FakeKeyring:
    values: dict[tuple[str, str], str]
    error: Exception | None
    calls: list[str]

    def __init__(self) -> None:
        self.values = {}
        self.error = None
        self.calls = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append("get")
        if self.error is not None:
            raise self.error
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append("set")
        if self.error is not None:
            raise self.error
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append("delete")
        if self.error is not None:
            raise self.error
        del self.values[(service, username)]


def _backend(module: str, name: str = "Keyring") -> _FakeKeyring:
    backend_type = type(name, (_FakeKeyring,), {"__module__": module})
    return backend_type()


def _reference() -> SecretStoreReference:
    return SecretStoreReference(
        installation_id="installation-123",
        purpose=SecretPurpose.PLAN_AUTHENTICATION,
        item_id="item-" + ("a" * 32),
    )


def _trusted_store(
    backend: _FakeKeyring,
    lock: NativePlanInterprocessLock,
    kind: SecretBackendKind = SecretBackendKind.MACOS_KEYCHAIN,
) -> NativeSecretStore:
    with patch.object(
        secret_adapter,
        "_trusted_native_backend_types",
        return_value={type(backend): kind},
    ):
        return NativeSecretStore(backend, lock)


@pytest.mark.parametrize(
    ("module", "name", "kind"),
    [
        ("keyring.backends.macOS", "Keyring", SecretBackendKind.MACOS_KEYCHAIN),
        (
            "keyring.backends.Windows",
            "WinVaultKeyring",
            SecretBackendKind.WINDOWS_CREDENTIAL_LOCKER,
        ),
        (
            "keyring.backends.SecretService",
            "Keyring",
            SecretBackendKind.SECRET_SERVICE,
        ),
    ],
)
def test_supported_native_backends_are_exactly_allowlisted(
    tmp_path: Path,
    module: str,
    name: str,
    kind: SecretBackendKind,
) -> None:
    """Only the three accepted native backend identities permit mutation."""
    backend = _backend(module, name)
    store = _trusted_store(
        backend,
        NativePlanInterprocessLock(tmp_path / "keyring.lock"),
        kind,
    )

    probe = store.probe()
    assert probe.kind is kind
    assert probe.mutation_capable


@pytest.mark.parametrize(
    ("module", "name", "kind"),
    [
        ("keyring.backends.kwallet", "DBusKeyring", SecretBackendKind.KWALLET),
        ("keyring.backends.null", "Keyring", SecretBackendKind.NULL),
        ("keyrings.alt.file", "PlaintextKeyring", SecretBackendKind.PLAINTEXT),
        ("keyrings.alt.file", "EncryptedKeyring", SecretBackendKind.FILE_BACKED),
        ("third_party.vault", "Keyring", SecretBackendKind.THIRD_PARTY),
        ("mystery", "Store", SecretBackendKind.UNKNOWN),
    ],
)
def test_blocked_backends_never_receive_secret_operations(
    tmp_path: Path,
    module: str,
    name: str,
    kind: SecretBackendKind,
) -> None:
    """Canary, plaintext, file, third-party, null and unknown stores fail closed."""
    backend = _backend(module, name)
    store = NativeSecretStore(
        backend, NativePlanInterprocessLock(tmp_path / "keyring.lock")
    )

    assert store.probe().kind is kind
    assert not store.probe().mutation_capable
    assert store.get(_reference()).status is SecretStoreStatus.UNSUPPORTED
    assert (
        store.create(_reference(), SecretValue(b"a" * 32)).status
        is SecretStoreStatus.UNSUPPORTED
    )
    delete_outcome = store.delete(_reference())
    assert delete_outcome.status is SecretStoreStatus.UNSUPPORTED
    assert backend.calls == []


def test_create_is_once_verified_and_never_replaces_an_existing_secret(
    tmp_path: Path,
) -> None:
    """The native API's replacement behavior is hidden behind create-once."""
    backend = _backend("keyring.backends.macOS")
    store = _trusted_store(
        backend, NativePlanInterprocessLock(tmp_path / "keyring.lock")
    )
    reference = _reference()
    secret = SecretValue(b"a" * 32)

    assert store.get(reference).status is SecretStoreStatus.MISSING
    assert store.create(reference, secret).status is SecretStoreStatus.CREATED
    loaded = store.get(reference)
    assert loaded.status is SecretStoreStatus.AVAILABLE
    assert loaded.secret is not None
    assert loaded.secret.reveal() == b"a" * 32
    assert "a" * 32 not in repr(loaded.secret)

    assert (
        store.create(reference, SecretValue(b"b" * 32)).status
        is SecretStoreStatus.CONFLICT
    )
    assert store.get(reference).secret == secret
    assert backend.calls.count("set") == 1
    deleted = store.delete(reference)
    missing = store.delete(reference)
    assert deleted.status is SecretStoreStatus.DELETED
    assert missing.status is SecretStoreStatus.MISSING


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (KeyringLocked("sensitive-detail-1"), SecretStoreStatus.LOCKED_OR_CANCELLED),
        (InitError("sensitive-detail-2"), SecretStoreStatus.UNAVAILABLE),
        (RuntimeError("sensitive-detail-3"), SecretStoreStatus.FAILED),
    ],
)
def test_backend_failures_are_typed_and_never_expose_exception_text(
    tmp_path: Path,
    error: Exception,
    status: SecretStoreStatus,
) -> None:
    """Operational native-store failures cross the port as closed outcomes."""
    backend = _backend("keyring.backends.SecretService")
    backend.error = error
    outcome = _trusted_store(
        backend, NativePlanInterprocessLock(tmp_path / "keyring.lock")
    ).get(_reference())

    assert outcome.status is status
    assert outcome.secret is None
    assert str(error) not in repr(outcome)


def test_secret_port_values_are_non_secret_bounded_and_fail_closed() -> None:
    """References, probes, values and outcomes cannot encode ambiguous states."""
    reference = _reference()
    assert reference.service == (
        "io.nisavid.cqmgr/installation-123/plan-authentication"
    )
    assert reference.username == "item-" + ("a" * 32)
    generated = {
        SecretStoreReference.generate(
            "installation-123", SecretPurpose.PLAN_AUTHENTICATION
        ).item_id
        for _ in range(GENERATED_REFERENCE_COUNT)
    }
    assert len(generated) == GENERATED_REFERENCE_COUNT
    assert all(
        item.startswith("item-") and len(item) <= MAXIMUM_REFERENCE_LENGTH
        for item in generated
    )
    with pytest.raises(TypeError, match="purpose"):
        SecretStoreReference("installation", cast("SecretPurpose", "purpose"), "item")
    with pytest.raises(ValueError, match="installation_id"):
        SecretStoreReference("bad/value", SecretPurpose.QUOTA_CONTACT, "item")
    with pytest.raises(ValueError, match="item_id"):
        SecretStoreReference("installation", SecretPurpose.QUOTA_CONTACT, "")
    with pytest.raises(ValueError, match="generated immutable"):
        SecretStoreReference(
            "installation", SecretPurpose.QUOTA_CONTACT, "deterministic-item"
        )
    with pytest.raises(TypeError, match="bytes"):
        SecretValue(cast("bytes", "secret"))
    with pytest.raises(ValueError, match="empty"):
        SecretValue(b"")
    with pytest.raises(TypeError, match="backend kind"):
        SecretStoreProbe(cast("SecretBackendKind", "unknown"), "backend")
    with pytest.raises(ValueError, match="backend_identity"):
        SecretStoreProbe(SecretBackendKind.UNKNOWN, "")
    with pytest.raises(TypeError, match="status"):
        SecretStoreOutcome(cast("SecretStoreStatus", "failed"))
    with pytest.raises(ValueError, match="must contain"):
        SecretStoreOutcome(SecretStoreStatus.AVAILABLE)
    with pytest.raises(ValueError, match="only an available"):
        SecretStoreOutcome(SecretStoreStatus.FAILED, SecretValue(b"secret"))


def test_corrupt_or_non_string_native_values_fail_without_exposure(
    tmp_path: Path,
) -> None:
    """Unknown value schemas and corrupt backend types never cross the port."""
    backend = _backend("keyring.backends.macOS")
    reference = _reference()
    backend.values[(reference.service, reference.username)] = "raw-plaintext"
    store = _trusted_store(
        backend, NativePlanInterprocessLock(tmp_path / "keyring.lock")
    )
    assert store.get(reference).status is SecretStoreStatus.FAILED
    backend.values[(reference.service, reference.username)] = "cqmgr-secret/v1:å"
    assert store.get(reference).status is SecretStoreStatus.FAILED
    backend.values[(reference.service, reference.username)] = cast("str", 7)
    assert store.get(reference).status is SecretStoreStatus.FAILED


class _MismatchingKeyring(_FakeKeyring):
    @override
    def set_password(self, service: str, username: str, password: str) -> None:
        """Simulate an observable non-cqmgr write racing after creation."""
        del password
        self.calls.append("set")
        self.values[(service, username)] = "cqmgr-secret/v1:bWlzbWF0Y2g="


def test_create_read_after_write_mismatch_and_keyring_error_are_conflicts(
    tmp_path: Path,
) -> None:
    """Create verification exposes observable external races as closed outcomes."""
    backend_type = type(
        "Keyring",
        (_MismatchingKeyring,),
        {"__module__": "keyring.backends.macOS"},
    )
    mismatch = _trusted_store(
        backend_type(),
        NativePlanInterprocessLock(tmp_path / "mismatch.lock"),
    )
    assert (
        mismatch.create(_reference(), SecretValue(b"a" * 32)).status
        is SecretStoreStatus.CONFLICT
    )

    backend = _backend("keyring.backends.macOS")
    backend.error = KeyringError("sensitive-detail")
    store = _trusted_store(
        backend, NativePlanInterprocessLock(tmp_path / "failed.lock")
    )
    assert (
        store.create(_reference(), SecretValue(b"a" * 32)).status
        is SecretStoreStatus.FAILED
    )
    delete_outcome = store.delete(_reference())
    assert delete_outcome.status is SecretStoreStatus.FAILED


def test_spoofed_native_module_and_name_are_not_allowlisted(tmp_path: Path) -> None:
    """Mutable Python class metadata cannot grant native-store capability."""
    backend = _backend("keyring.backends.macOS", "Keyring")

    probe = NativeSecretStore(
        backend, NativePlanInterprocessLock(tmp_path / "spoof.lock")
    ).probe()

    assert probe.kind is SecretBackendKind.THIRD_PARTY
    assert not probe.mutation_capable


def test_default_allowlist_is_bound_to_exported_concrete_classes() -> None:
    """Production trust entries are the exact classes exported by keyring."""
    trusted = secret_adapter._trusted_native_backend_types()  # noqa: SLF001

    assert trusted
    for backend_type, kind in trusted.items():
        module = import_module(backend_type.__module__)
        assert getattr(module, backend_type.__name__) is backend_type
        assert kind in {
            SecretBackendKind.MACOS_KEYCHAIN,
            SecretBackendKind.WINDOWS_CREDENTIAL_LOCKER,
            SecretBackendKind.SECRET_SERVICE,
        }


def test_lock_timeout_is_a_typed_secret_store_failure(tmp_path: Path) -> None:
    """OS-lock contention cannot escape the secret-store outcome boundary."""
    path = tmp_path / "keyring.lock"
    backend = _backend("keyring.backends.macOS", "Keyring")
    store = _trusted_store(
        backend,
        NativePlanInterprocessLock(
            path,
            timeout_seconds=0.01,
            poll_seconds=0.001,
        ),
    )

    with NativePlanInterprocessLock(path):
        outcomes = (
            store.get(_reference()),
            store.create(_reference(), SecretValue(b"a" * 32)),
            store.delete(_reference()),
        )

    assert {outcome.status for outcome in outcomes} == {SecretStoreStatus.FAILED}


def test_native_plan_lock_rejects_invalid_configuration_and_reentrancy(
    tmp_path: Path,
) -> None:
    """The serialization boundary is bounded and cannot deadlock itself."""
    with pytest.raises(ValueError, match="timeout"):
        NativePlanInterprocessLock(tmp_path / "lock", timeout_seconds=-1)
    lock = NativePlanInterprocessLock(tmp_path / "lock")
    with lock:
        assert lock.path == tmp_path / "lock"
        with pytest.raises(RuntimeError, match="not reentrant"):
            lock.__enter__()
    lock.__exit__()


@pytest.mark.parametrize(
    "timeout_seconds",
    [True, float("nan"), float("inf"), float("-inf")],
)
def test_native_plan_lock_rejects_nonfinite_or_boolean_timeout(
    tmp_path: Path,
    timeout_seconds: float,
) -> None:
    """A caller cannot turn bounded lock acquisition into an endless wait."""
    with pytest.raises(ValueError, match="timeout"):
        NativePlanInterprocessLock(
            tmp_path / "lock",
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize(
    "poll_seconds",
    [True, float("nan"), float("inf"), float("-inf")],
)
def test_native_plan_lock_rejects_nonfinite_or_boolean_poll_interval(
    tmp_path: Path,
    poll_seconds: float,
) -> None:
    """Lock polling always uses a finite positive scheduling interval."""
    with pytest.raises(ValueError, match="poll"):
        NativePlanInterprocessLock(
            tmp_path / "lock",
            poll_seconds=poll_seconds,
        )
