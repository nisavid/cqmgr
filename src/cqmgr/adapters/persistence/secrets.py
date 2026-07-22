"""Serialized adapter for allowlisted in-process native keyring backends."""

from __future__ import annotations

import base64
import hmac
from typing import TYPE_CHECKING, Protocol

from keyring.errors import InitError, KeyringError, KeyringLocked

from cqmgr.application.ports.secrets import (
    SecretBackendKind,
    SecretStoreOutcome,
    SecretStoreProbe,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
)

if TYPE_CHECKING:
    from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock

_VALUE_PREFIX = "cqmgr-secret/v1:"


class _KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class NativeSecretStore:
    """Create-once native-store operations with cqmgr process serialization."""

    def __init__(
        self, backend: _KeyringBackend, lock: NativePlanInterprocessLock
    ) -> None:
        """Classify an injected in-process backend without accessing it."""
        self._backend = backend
        self._lock = lock
        backend_type = type(backend)
        self._backend_identity = f"{backend_type.__module__}.{backend_type.__name__}"
        self._kind = _classify_backend(backend_type.__module__, backend_type.__name__)

    def probe(self) -> SecretStoreProbe:
        """Return the allowlist classification without opening the keyring."""
        return SecretStoreProbe(self._kind, self._backend_identity)

    def get(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        """Read one exact item under the shared cqmgr keyring lock."""
        if not self.probe().mutation_capable:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        with self._lock:
            return self._get_unlocked(reference)

    def create(  # noqa: PLR0911
        self, reference: SecretStoreReference, secret: SecretValue
    ) -> SecretStoreOutcome:
        """Create and verify one item without replacing an existing value."""
        if not self.probe().mutation_capable:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        encoded = _encode(secret)
        with self._lock:
            existing = self._get_raw(reference)
            if isinstance(existing, SecretStoreOutcome):
                if existing.status is not SecretStoreStatus.MISSING:
                    return existing
            else:
                return SecretStoreOutcome(SecretStoreStatus.CONFLICT)
            try:
                self._backend.set_password(
                    reference.service, reference.username, encoded
                )
            except Exception as error:  # noqa: BLE001
                return _failure(error)
            verified = self._get_raw(reference)
            if isinstance(verified, SecretStoreOutcome):
                return verified
            if not hmac.compare_digest(verified, encoded):
                return SecretStoreOutcome(SecretStoreStatus.CONFLICT)
            return SecretStoreOutcome(SecretStoreStatus.CREATED)

    def delete(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        """Delete one existing exact item without exposing its value."""
        if not self.probe().mutation_capable:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        with self._lock:
            existing = self._get_raw(reference)
            if isinstance(existing, SecretStoreOutcome):
                return existing
            try:
                self._backend.delete_password(reference.service, reference.username)
            except Exception as error:  # noqa: BLE001
                return _failure(error)
            return SecretStoreOutcome(SecretStoreStatus.DELETED)

    def _get_unlocked(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        raw = self._get_raw(reference)
        if isinstance(raw, SecretStoreOutcome):
            return raw
        try:
            secret = _decode(raw)
        except ValueError:
            return SecretStoreOutcome(SecretStoreStatus.FAILED)
        return SecretStoreOutcome.available(secret)

    def _get_raw(self, reference: SecretStoreReference) -> str | SecretStoreOutcome:
        try:
            value = self._backend.get_password(reference.service, reference.username)
        except Exception as error:  # noqa: BLE001
            return _failure(error)
        if value is None:
            return SecretStoreOutcome(SecretStoreStatus.MISSING)
        if not isinstance(value, str):
            return SecretStoreOutcome(SecretStoreStatus.FAILED)
        return value


def _failure(error: Exception) -> SecretStoreOutcome:
    if isinstance(error, KeyringLocked):
        status = SecretStoreStatus.LOCKED_OR_CANCELLED
    elif isinstance(error, InitError):
        status = SecretStoreStatus.UNAVAILABLE
    elif isinstance(error, KeyringError):
        status = SecretStoreStatus.FAILED
    else:
        status = SecretStoreStatus.FAILED
    return SecretStoreOutcome(status)


def _encode(secret: SecretValue) -> str:
    payload = base64.urlsafe_b64encode(secret.reveal()).decode("ascii")
    return f"{_VALUE_PREFIX}{payload}"


def _decode(value: str) -> SecretValue:
    if not value.startswith(_VALUE_PREFIX):
        msg = "unsupported keyring value schema"
        raise ValueError(msg)
    try:
        payload = value.removeprefix(_VALUE_PREFIX).encode("ascii")
        decoded = base64.b64decode(payload, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        msg = "invalid encoded keyring value"
        raise ValueError(msg) from error
    return SecretValue(decoded)


def _classify_backend(  # noqa: PLR0911
    module: str, name: str
) -> SecretBackendKind:
    identity = (module, name)
    if identity == ("keyring.backends.macOS", "Keyring"):
        return SecretBackendKind.MACOS_KEYCHAIN
    if identity == ("keyring.backends.Windows", "WinVaultKeyring"):
        return SecretBackendKind.WINDOWS_CREDENTIAL_LOCKER
    if identity == ("keyring.backends.SecretService", "Keyring"):
        return SecretBackendKind.SECRET_SERVICE
    if module == "keyring.backends.kwallet":
        return SecretBackendKind.KWALLET
    if module == "keyring.backends.fail":
        return SecretBackendKind.MISSING
    if module == "keyring.backends.null":
        return SecretBackendKind.NULL
    if name == "PlaintextKeyring":
        return SecretBackendKind.PLAINTEXT
    if name == "EncryptedKeyring" or "file" in module.lower():
        return SecretBackendKind.FILE_BACKED
    if name.casefold().endswith("keyring"):
        return SecretBackendKind.THIRD_PARTY
    return SecretBackendKind.UNKNOWN
