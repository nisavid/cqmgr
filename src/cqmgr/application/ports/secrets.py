"""Typed application boundary for native secret storage."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, Self

SECRET_SERVICE_NAMESPACE = "io.nisavid.cqmgr"  # noqa: S105
_MAXIMUM_OPAQUE_IDENTIFIER_LENGTH = 128
_GENERATED_ITEM_ID = re.compile(r"item-[A-Za-z0-9_-]{32}\Z")


class SecretBackendKind(StrEnum):
    """Closed native-store identity and support classifications."""

    MACOS_KEYCHAIN = "macos-keychain"
    WINDOWS_CREDENTIAL_LOCKER = "windows-credential-locker"
    SECRET_SERVICE = "secret-service"  # noqa: S105
    KWALLET = "kwallet"
    MISSING = "missing"
    NULL = "null"
    PLAINTEXT = "plaintext"
    FILE_BACKED = "file-backed"
    THIRD_PARTY = "third-party"
    UNKNOWN = "unknown"


MUTATION_CAPABLE_BACKENDS = frozenset(
    {
        SecretBackendKind.MACOS_KEYCHAIN,
        SecretBackendKind.WINDOWS_CREDENTIAL_LOCKER,
        SecretBackendKind.SECRET_SERVICE,
    }
)


class SecretPurpose(StrEnum):
    """Allowlisted cqmgr secret purposes."""

    PLAN_AUTHENTICATION = "plan-authentication"
    QUOTA_CONTACT = "quota-contact"


class SecretStoreStatus(StrEnum):
    """Typed outcomes from exact-reference secret operations."""

    AVAILABLE = "available"
    CREATED = "created"
    DELETED = "deleted"
    MISSING = "missing"
    LOCKED_OR_CANCELLED = "locked-or-cancelled"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SecretStoreReference:
    """Non-secret stable identity for one cqmgr native keyring item."""

    installation_id: str
    purpose: SecretPurpose
    item_id: str

    @classmethod
    def generate(cls, installation_id: str, purpose: SecretPurpose) -> Self:
        """Create one collision-resistant immutable cqmgr-owned item reference."""
        return cls(
            installation_id=installation_id,
            purpose=purpose,
            item_id=f"item-{secrets.token_urlsafe(24)}",
        )

    def __post_init__(self) -> None:
        """Restrict item components to bounded opaque identifiers."""
        if not isinstance(self.purpose, SecretPurpose):
            msg = "secret purpose must be a SecretPurpose"
            raise TypeError(msg)
        for name, value in (
            ("installation_id", self.installation_id),
            ("item_id", self.item_id),
        ):
            if not _is_opaque_identifier(value):
                msg = f"{name} must be a bounded opaque identifier"
                raise ValueError(msg)
        if _GENERATED_ITEM_ID.fullmatch(self.item_id) is None:
            msg = "item_id must be a generated immutable reference"
            raise ValueError(msg)

    @property
    def service(self) -> str:
        """Return the deterministic cqmgr service namespace."""
        return f"{SECRET_SERVICE_NAMESPACE}/{self.installation_id}/{self.purpose.value}"

    @property
    def username(self) -> str:
        """Return the opaque backend item identity."""
        return self.item_id


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """Secret bytes whose representation never reveals their value."""

    _value: bytes

    def __post_init__(self) -> None:
        """Require non-empty bytes and retain an immutable copy."""
        if not isinstance(self._value, bytes):
            msg = "secret value must be bytes"
            raise TypeError(msg)
        if not self._value:
            msg = "secret value must not be empty"
            raise ValueError(msg)
        object.__setattr__(self, "_value", bytes(self._value))

    def reveal(self) -> bytes:
        """Return a copy for the narrow adapter or cryptographic boundary."""
        return bytes(self._value)

    def __repr__(self) -> str:
        """Return a stable redacted representation."""
        return "SecretValue(<redacted>)"


@dataclass(frozen=True, slots=True)
class SecretStoreProbe:
    """Backend identity and whether Preview and Apply may use it."""

    kind: SecretBackendKind
    backend_identity: str

    def __post_init__(self) -> None:
        """Require a known kind and a non-empty safe identity."""
        if not isinstance(self.kind, SecretBackendKind):
            msg = "backend kind must be a SecretBackendKind"
            raise TypeError(msg)
        if not isinstance(self.backend_identity, str) or not self.backend_identity:
            msg = "backend_identity must be a non-empty string"
            raise ValueError(msg)

    @property
    def mutation_capable(self) -> bool:
        """Return whether the backend is allowlisted for Preview and Apply."""
        return self.kind in MUTATION_CAPABLE_BACKENDS


@dataclass(frozen=True, slots=True)
class SecretStoreOutcome:
    """Closed result that reveals a value only for successful reads."""

    status: SecretStoreStatus
    secret: SecretValue | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Keep success and non-secret outcomes unambiguous."""
        if not isinstance(self.status, SecretStoreStatus):
            msg = "secret-store status must be a SecretStoreStatus"
            raise TypeError(msg)
        if self.status is SecretStoreStatus.AVAILABLE:
            if not isinstance(self.secret, SecretValue):
                msg = "available outcome must contain a SecretValue"
                raise ValueError(msg)
        elif self.secret is not None:
            msg = "only an available outcome may contain a secret"
            raise ValueError(msg)

    @classmethod
    def available(cls, secret: SecretValue) -> Self:
        """Build a successful read outcome."""
        return cls(SecretStoreStatus.AVAILABLE, secret)


class SecretStore(Protocol):
    """Narrow exact-reference native secret-store port."""

    def probe(self) -> SecretStoreProbe:
        """Return backend identity and capability without exposing a secret."""
        ...

    def get(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        """Read one exact reference."""
        ...

    def create(
        self, reference: SecretStoreReference, secret: SecretValue
    ) -> SecretStoreOutcome:
        """Create one exact reference without replacing an existing item."""
        ...

    def delete(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        """Delete one exact reference."""
        ...


def _is_opaque_identifier(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAXIMUM_OPAQUE_IDENTIFIER_LENGTH
        or not value.isascii()
    ):
        return False
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    return all(character in allowed for character in value)
