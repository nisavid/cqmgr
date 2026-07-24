"""Explicit installation-trust initialization and fail-closed loading."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from cqmgr.application.ports.secrets import (
    SecretPurpose,
    SecretStoreStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from cqmgr.application.ports.secrets import (
        SecretStore,
        SecretStoreReference,
        SecretValue,
    )

_AUTHENTICATION_KEY_BYTES = 32
_AUTHENTICATION_KEY_COMMITMENT_BYTES = 32


class InstallationTrustPhase(StrEnum):
    """Durable bootstrap phase for one installation signing identity."""

    PREPARED = "prepared"
    CREATE_INTENT = "create-intent"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class InstallationTrust:
    """Non-secret installation identity and exact native-keyring reference."""

    installation_id: str
    authentication_key_reference: SecretStoreReference
    authentication_key_commitment: bytes
    phase: InstallationTrustPhase

    def __post_init__(self) -> None:
        """Keep the installation and secret reference inseparable."""
        from cqmgr.application.ports.secrets import (  # noqa: PLC0415
            SecretStoreReference,
        )

        if not isinstance(self.installation_id, str) or not self.installation_id:
            msg = "installation trust ID must be non-empty"
            raise ValueError(msg)
        if not isinstance(
            self.authentication_key_reference,
            SecretStoreReference,
        ):
            msg = "installation trust key reference must be exact"
            raise TypeError(msg)
        if self.authentication_key_reference.installation_id != self.installation_id:
            msg = "installation trust key reference must match the installation"
            raise ValueError(msg)
        if (
            self.authentication_key_reference.purpose
            is not SecretPurpose.PLAN_AUTHENTICATION
        ):
            msg = "installation trust key must authenticate plans"
            raise ValueError(msg)
        if (
            not isinstance(self.authentication_key_commitment, bytes)
            or len(self.authentication_key_commitment)
            != _AUTHENTICATION_KEY_COMMITMENT_BYTES
        ):
            msg = "installation trust key commitment must be a SHA-256 digest"
            raise ValueError(msg)
        if not isinstance(self.phase, InstallationTrustPhase):
            msg = "installation trust phase must be typed"
            raise TypeError(msg)


class InstallationTrustRepository(Protocol):
    """Create-once durable non-secret installation-trust state."""

    def load(self) -> InstallationTrust | None:
        """Return the exact retained state, or None only when never initialized."""

    def create(self, value: InstallationTrust) -> None:
        """Create the first prepared state without replacing an existing record."""

    def transition(
        self,
        expected: InstallationTrustPhase,
        replacement: InstallationTrust,
    ) -> None:
        """Atomically replace one exact expected phase."""

    def restart_incomplete(
        self,
        expected: InstallationTrust,
        replacement: InstallationTrust,
    ) -> None:
        """Atomically replace one exact incomplete bootstrap candidate."""


class TrustLoadError(RuntimeError):
    """Installation trust cannot be used without unsafe reconstruction."""


@dataclass(frozen=True, slots=True)
class LoadedInstallationTrust:
    """Validated active installation authority held only in process memory."""

    installation_id: str
    authentication_key: SecretValue
    keyring_mutation_capable: bool


@dataclass(frozen=True, slots=True)
class TrustInitializationResult:
    """Stable result of an explicit one-time trust initialization attempt."""

    initialized: bool
    trust: InstallationTrust | None = None
    reason: str | None = None


type TrustMaterial = tuple[str, SecretStoreReference, SecretValue]
type TrustMaterialGenerator = Callable[[], TrustMaterial]


def _authentication_key_commitment(key: SecretValue) -> bytes:
    """Return the non-secret commitment bound to one generated key."""
    return hashlib.sha256(key.reveal()).digest()


class InstallationTrustLoader:
    """Load active trust without any secret creation or recovery authority."""

    def __init__(
        self,
        repository: InstallationTrustRepository,
        store: SecretStore,
    ) -> None:
        """Bind the non-secret record to its exact native secret store."""
        self._repository = repository
        self._store = store

    def load(self) -> LoadedInstallationTrust:
        """Require one active, consistent, allowlisted native-keyring value."""
        trust = self._repository.load()
        if trust is None:
            message = "installation trust is missing; run `cqmgr trust init`"
            raise TrustLoadError(message)
        if trust.phase is not InstallationTrustPhase.ACTIVE:
            message = "installation trust initialization is incomplete"
            raise TrustLoadError(message)
        probe = self._store.probe()
        if not probe.mutation_capable:
            message = f"installation trust backend is unsupported: {probe.kind.value}"
            raise TrustLoadError(message)
        outcome = self._store.get(trust.authentication_key_reference)
        if outcome.status is not SecretStoreStatus.AVAILABLE or outcome.secret is None:
            message = (
                f"installation trust key is {outcome.status.value}; "
                "it will not be recreated"
            )
            raise TrustLoadError(message)
        if len(
            outcome.secret.reveal()
        ) != _AUTHENTICATION_KEY_BYTES or not hmac.compare_digest(
            _authentication_key_commitment(outcome.secret),
            trust.authentication_key_commitment,
        ):
            message = "installation trust key is inconsistent"
            raise TrustLoadError(message)
        return LoadedInstallationTrust(
            trust.installation_id,
            outcome.secret,
            keyring_mutation_capable=True,
        )


class TrustInitializationOperations:
    """Own the sole explicit create-once installation-key workflow."""

    def __init__(
        self,
        repository: InstallationTrustRepository,
        store: SecretStore,
        *,
        material: TrustMaterialGenerator,
        workflow_lock: AbstractContextManager[object],
    ) -> None:
        """Bind create-once persistence, storage, and random material generation."""
        self._repository = repository
        self._store = store
        self._material = material
        self._workflow_lock = workflow_lock

    def initialize(self) -> TrustInitializationResult:
        """Serialize and execute one explicit installation-trust attempt."""
        with self._workflow_lock:
            return self._initialize_locked()

    def _initialize_locked(self) -> TrustInitializationResult:
        """Initialize fresh trust or recover one exact incomplete candidate."""
        probe = self._store.probe()
        if not probe.mutation_capable:
            return TrustInitializationResult(
                initialized=False,
                reason=f"unsupported-secret-backend:{probe.kind.value}",
            )
        trust = self._repository.load()
        fresh_material: TrustMaterial | None = None
        if trust is None:
            prepared = self._prepare_material()
            if isinstance(prepared, TrustInitializationResult):
                return prepared
            trust, fresh_material = prepared
            self._repository.create(trust)
        if trust.phase is InstallationTrustPhase.ACTIVE:
            return self._load_active(trust)
        if trust.phase is InstallationTrustPhase.CREATE_INTENT:
            return self._resume_create_intent(trust)
        if fresh_material is None:
            return self._resume_prepared(trust)
        return self._activate_prepared(trust, fresh_material)

    def _prepare_material(
        self,
    ) -> tuple[InstallationTrust, TrustMaterial] | TrustInitializationResult:
        fresh_material = self._material()
        installation_id, reference, key = fresh_material
        if len(key.reveal()) != _AUTHENTICATION_KEY_BYTES:
            return TrustInitializationResult(
                initialized=False,
                reason="invalid-trust-material",
            )
        try:
            trust = InstallationTrust(
                installation_id,
                reference,
                _authentication_key_commitment(key),
                InstallationTrustPhase.PREPARED,
            )
        except (TypeError, ValueError):
            return TrustInitializationResult(
                initialized=False,
                reason="invalid-trust-material",
            )
        return trust, fresh_material

    def _load_active(
        self,
        trust: InstallationTrust,
    ) -> TrustInitializationResult:
        try:
            loaded = InstallationTrustLoader(
                self._repository,
                self._store,
            ).load()
        except TrustLoadError:
            return TrustInitializationResult(
                initialized=False,
                reason="trust-unavailable",
            )
        del loaded
        return TrustInitializationResult(
            initialized=False,
            trust=trust,
            reason="already-initialized",
        )

    def _resume_create_intent(
        self,
        trust: InstallationTrust,
    ) -> TrustInitializationResult:
        existing = self._store.get(trust.authentication_key_reference)
        if (
            existing.status is SecretStoreStatus.AVAILABLE
            and existing.secret is not None
            and len(existing.secret.reveal()) == _AUTHENTICATION_KEY_BYTES
            and hmac.compare_digest(
                _authentication_key_commitment(existing.secret),
                trust.authentication_key_commitment,
            )
        ):
            active = replace(trust, phase=InstallationTrustPhase.ACTIVE)
            self._repository.transition(
                InstallationTrustPhase.CREATE_INTENT,
                active,
            )
            return TrustInitializationResult(initialized=True, trust=active)
        if existing.status is SecretStoreStatus.MISSING:
            return self._restart_incomplete(trust)
        return TrustInitializationResult(
            initialized=False,
            trust=trust,
            reason="trust-create-interrupted",
        )

    def _resume_prepared(
        self,
        trust: InstallationTrust,
    ) -> TrustInitializationResult:
        existing = self._store.get(trust.authentication_key_reference)
        if existing.status is SecretStoreStatus.MISSING:
            return self._restart_incomplete(trust)
        return TrustInitializationResult(
            initialized=False,
            trust=trust,
            reason="trust-prepare-interrupted",
        )

    def _restart_incomplete(
        self,
        trust: InstallationTrust,
    ) -> TrustInitializationResult:
        prepared = self._prepare_material()
        if isinstance(prepared, TrustInitializationResult):
            return prepared
        replacement, fresh_material = prepared
        if (
            replacement.installation_id == trust.installation_id
            or replacement.authentication_key_reference
            == trust.authentication_key_reference
            or hmac.compare_digest(
                replacement.authentication_key_commitment,
                trust.authentication_key_commitment,
            )
        ):
            return TrustInitializationResult(
                initialized=False,
                trust=trust,
                reason="trust-recovery-material-reused",
            )
        self._repository.restart_incomplete(trust, replacement)
        result = self._activate_prepared(replacement, fresh_material)
        if result.initialized:
            return replace(result, reason="incomplete-trust-restarted")
        return result

    def _activate_prepared(
        self,
        trust: InstallationTrust,
        fresh_material: TrustMaterial,
    ) -> TrustInitializationResult:
        installation_id, reference, key = fresh_material
        if (
            installation_id != trust.installation_id
            or reference != trust.authentication_key_reference
            or not hmac.compare_digest(
                _authentication_key_commitment(key),
                trust.authentication_key_commitment,
            )
        ):
            return TrustInitializationResult(
                initialized=False,
                trust=trust,
                reason="trust-material-inconsistent",
            )
        intent = replace(trust, phase=InstallationTrustPhase.CREATE_INTENT)
        self._repository.transition(InstallationTrustPhase.PREPARED, intent)
        created = self._store.create(reference, key)
        if created.status is not SecretStoreStatus.CREATED:
            return TrustInitializationResult(
                initialized=False,
                trust=intent,
                reason=f"trust-key-create-{created.status.value}",
            )
        verified = self._store.get(reference)
        if (
            verified.status is not SecretStoreStatus.AVAILABLE
            or verified.secret is None
            or not hmac.compare_digest(verified.secret.reveal(), key.reveal())
        ):
            return TrustInitializationResult(
                initialized=False,
                trust=intent,
                reason="trust-key-verification-failed",
            )
        active = replace(intent, phase=InstallationTrustPhase.ACTIVE)
        self._repository.transition(InstallationTrustPhase.CREATE_INTENT, active)
        return TrustInitializationResult(initialized=True, trust=active)
