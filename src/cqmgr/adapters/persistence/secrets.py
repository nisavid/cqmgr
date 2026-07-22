"""Serialized adapter for allowlisted in-process native keyring backends."""

from __future__ import annotations

import base64
import hmac
from importlib import import_module
from types import MappingProxyType
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
    from collections.abc import Mapping

    from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock

_VALUE_PREFIX = "cqmgr-secret/v1:"
_NATIVE_BACKEND_IDENTITIES = {
    ("keyring.backends.macOS", "Keyring"): SecretBackendKind.MACOS_KEYCHAIN,
    (
        "keyring.backends.Windows",
        "WinVaultKeyring",
    ): SecretBackendKind.WINDOWS_CREDENTIAL_LOCKER,
    ("keyring.backends.SecretService", "Keyring"): SecretBackendKind.SECRET_SERVICE,
}


class _KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None:
        """Read one credential value."""

    def set_password(self, service: str, username: str, password: str) -> None:
        """Set one credential value."""

    def delete_password(self, service: str, username: str) -> None:
        """Delete one credential value."""


class NativeSecretStore:
    """Create-once native-store operations with cqmgr process serialization."""

    def __init__(
        self,
        backend: _KeyringBackend,
        lock: NativePlanInterprocessLock,
    ) -> None:
        """Classify an injected backend against trusted concrete class identities."""
        self._backend = backend
        self._lock = lock
        backend_type = type(backend)
        self._backend_identity = f"{backend_type.__module__}.{backend_type.__name__}"
        trusted = _trusted_native_backend_types()
        self._kind = trusted.get(backend_type) or _classify_blocked_backend(
            backend_type.__module__, backend_type.__name__
        )

    def probe(self) -> SecretStoreProbe:
        """Return the allowlist classification without opening the keyring."""
        return SecretStoreProbe(self._kind, self._backend_identity)

    def get(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        """Read one exact item under the shared cqmgr keyring lock."""
        if not self.probe().mutation_capable:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        try:
            with self._lock:
                return self._get_unlocked(reference)
        except Exception as error:  # noqa: BLE001
            return _failure(error)

    def create(  # noqa: PLR0911
        self, reference: SecretStoreReference, secret: SecretValue
    ) -> SecretStoreOutcome:
        """Create and verify one item without replacing an existing value."""
        if not self.probe().mutation_capable:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        encoded = _encode(secret)
        try:
            with self._lock:
                existing = self._get_raw(reference)
                if isinstance(existing, SecretStoreOutcome):
                    if existing.status is not SecretStoreStatus.MISSING:
                        return existing
                else:
                    return SecretStoreOutcome(SecretStoreStatus.CONFLICT)
                self._backend.set_password(
                    reference.service, reference.username, encoded
                )
                verified = self._get_raw(reference)
                if isinstance(verified, SecretStoreOutcome):
                    return verified
                if not hmac.compare_digest(verified, encoded):
                    return SecretStoreOutcome(SecretStoreStatus.CONFLICT)
                return SecretStoreOutcome(SecretStoreStatus.CREATED)
        except Exception as error:  # noqa: BLE001
            return _failure(error)

    def delete(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        """Delete one existing exact item without exposing its value."""
        if not self.probe().mutation_capable:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        try:
            with self._lock:
                existing = self._get_raw(reference)
                if isinstance(existing, SecretStoreOutcome):
                    return existing
                self._backend.delete_password(reference.service, reference.username)
                return SecretStoreOutcome(SecretStoreStatus.DELETED)
        except Exception as error:  # noqa: BLE001
            return _failure(error)

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


def _trusted_native_backend_types() -> Mapping[type[object], SecretBackendKind]:
    trusted: dict[type[object], SecretBackendKind] = {}
    for (module_name, class_name), kind in _NATIVE_BACKEND_IDENTITIES.items():
        try:
            module = import_module(module_name)
        except (ImportError, OSError):
            continue
        backend_type = getattr(module, class_name, None)
        if (
            isinstance(backend_type, type)
            and backend_type.__module__ == module_name
            and getattr(module, class_name) is backend_type
        ):
            trusted[backend_type] = kind
    return MappingProxyType(trusted)


def _classify_blocked_backend(  # noqa: PLR0911
    module: str, name: str
) -> SecretBackendKind:
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
    if (
        name.casefold().endswith("keyring")
        or (module, name) in _NATIVE_BACKEND_IDENTITIES
    ):
        return SecretBackendKind.THIRD_PARTY
    return SecretBackendKind.UNKNOWN
