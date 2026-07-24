"""Protected quota-contact selection and exact source re-resolution."""

from __future__ import annotations

import hmac
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

from cqmgr.application.ports.apply import ApplyContactRefresh
from cqmgr.application.ports.secrets import (
    SecretStoreStatus,
    SecretValue,
)
from cqmgr.domain.identity import (
    CredentialKind,
    PrincipalVerification,
    VerifiedDirectUserEmail,
)
from cqmgr.domain.plans import ContactBinding
from cqmgr.domain.results import StableSymbol

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.application.configuration import (
        ConfigSnapshot,
        Profile,
        QuotaContactKeyringReference,
        SelectionState,
    )
    from cqmgr.application.operations.trust import LoadedInstallationTrust
    from cqmgr.application.ports.secrets import SecretStoreOutcome
    from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject
    from cqmgr.domain.plans import PlanPrincipal


class QuotaContactStore(Protocol):
    """Read one profile-bound native-keyring quota contact."""

    def get_quota_contact(
        self,
        reference: QuotaContactKeyringReference,
    ) -> SecretStoreOutcome:
        """Return one exact protected value or a typed failure."""


class ContactConfigurationSource(Protocol):
    """Read validated profile metadata without mutation capability."""

    async def read(self) -> ConfigSnapshot:
        """Return the current validated configuration."""


class ContactSelectionSource(Protocol):
    """Read current selected-profile state without mutation capability."""

    async def read(self) -> SelectionState:
        """Return the current validated selection."""


class ContactIdentitySource(Protocol):
    """Resolve verified direct-user evidence without switching identity."""

    async def resolve(
        self,
        *,
        adc_quota_project: ADCQuotaProject | None = None,
        timeout_seconds: float = 10.0,
    ) -> ADCIdentityEvidence:
        """Return current typed ADC identity evidence."""


class ContactResolutionError(RuntimeError):
    """The selected quota-contact source cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ResolvedContact:
    """One exact non-secret binding plus its ephemeral protected value."""

    binding: ContactBinding
    value: SecretValue = field(repr=False)


class LifecycleContactResolver(Protocol):
    """Resolve Preview contact intent through the canonical source order."""

    async def resolve_preview(
        self,
        *,
        explicit_value: SecretValue | None,
        explicit_profile: str | None,
        principal: PlanPrincipal,
        trust: LoadedInstallationTrust,
    ) -> ResolvedContact:
        """Return one exact protected source binding and ephemeral value."""


@dataclass(frozen=True, slots=True)
class _ApplyContactContext:
    explicit_value: SecretValue | None
    principal: PlanPrincipal
    trust: LoadedInstallationTrust


class ProtectedContactResolver:
    """Select Preview contact sources and re-resolve only the bound Apply source."""

    def __init__(
        self,
        configuration: ContactConfigurationSource,
        selection: ContactSelectionSource,
        contacts: QuotaContactStore,
        identity: ContactIdentitySource,
        *,
        identity_timeout_seconds: float = 10.0,
    ) -> None:
        """Bind local source metadata, native secrets, and verified identity."""
        if identity_timeout_seconds <= 0:
            message = "contact identity timeout must be positive"
            raise ValueError(message)
        self._configuration = configuration
        self._selection = selection
        self._contacts = contacts
        self._identity = identity
        self._identity_timeout_seconds = identity_timeout_seconds
        self._apply_contexts: dict[
            ContactBinding,
            deque[_ApplyContactContext],
        ] = {}

    async def resolve_preview(
        self,
        *,
        explicit_value: SecretValue | None,
        explicit_profile: str | None,
        principal: PlanPrincipal,
        trust: LoadedInstallationTrust,
    ) -> ResolvedContact:
        """Choose exactly one source in canonical precedence order."""
        if explicit_value is not None:
            _contact_text(explicit_value)
            return _resolved(
                explicit_value,
                trust.authentication_key,
                source="per-operation-input",
                source_identity=None,
            )
        if explicit_profile is not None:
            return await self._resolve_profile(
                explicit_profile,
                source="named-profile",
                trust=trust,
            )
        selection = await self._selection.read()
        if selection.selected_profile is not None:
            return await self._resolve_profile(
                selection.selected_profile,
                source="selected-profile",
                trust=trust,
            )
        return await self._resolve_direct_user(principal, trust)

    async def prepare_apply(
        self,
        binding: ContactBinding,
        *,
        explicit_value: SecretValue | None,
        principal: PlanPrincipal,
        trust: LoadedInstallationTrust,
    ) -> ResolvedContact:
        """Resolve and retain only the inputs needed to refresh the bound source."""
        resolved = await self._resolve_bound(
            binding,
            explicit_value=explicit_value,
            principal=principal,
            trust=trust,
        )
        contexts = self._apply_contexts.setdefault(binding, deque())
        contexts.append(
            _ApplyContactContext(
                explicit_value,
                principal,
                trust,
            )
        )
        return resolved

    async def refresh_contact(
        self,
        binding: ContactBinding,
        now: datetime,
    ) -> ApplyContactRefresh:
        """Freshly resolve the exact Plan-bound source without fallback."""
        del now
        contexts = self._apply_contexts.get(binding)
        if contexts is None:
            message = "Apply quota contact source is unavailable"
            raise ContactResolutionError(message)
        context = contexts.popleft()
        if not contexts:
            del self._apply_contexts[binding]
        resolved = await self._resolve_bound(
            binding,
            explicit_value=context.explicit_value,
            principal=context.principal,
            trust=context.trust,
        )
        return ApplyContactRefresh(
            resolved.binding,
            _contact_text(resolved.value),
        )

    async def _resolve_bound(
        self,
        binding: ContactBinding,
        *,
        explicit_value: SecretValue | None,
        principal: PlanPrincipal,
        trust: LoadedInstallationTrust,
    ) -> ResolvedContact:
        source = binding.source.value
        if source == "per-operation-input":
            if explicit_value is None:
                message = "Apply requires the Plan-bound per-operation contact"
                raise ContactResolutionError(message)
            _contact_text(explicit_value)
            resolved = _resolved(
                explicit_value,
                trust.authentication_key,
                source=source,
                source_identity=None,
            )
        elif source in {"named-profile", "selected-profile"}:
            from cqmgr.application.configuration import (  # noqa: PLC0415
                QuotaContactKeyringReference,
            )

            try:
                bound_reference = QuotaContactKeyringReference.parse(
                    binding.source_identity
                )
            except (TypeError, ValueError) as error:
                message = "Apply quota-contact profile reference is invalid"
                raise ContactResolutionError(message) from error
            profile_name = bound_reference.profile_name
            if source == "selected-profile":
                selection = await self._selection.read()
                if selection.selected_profile != profile_name:
                    message = "Apply selected quota-contact profile changed"
                    raise ContactResolutionError(message)
            resolved = await self._resolve_profile(
                profile_name,
                source=source,
                trust=trust,
            )
        elif source == "direct-user":
            if binding.source_identity != principal.stable_identity:
                message = "Apply direct-user contact principal changed"
                raise ContactResolutionError(message)
            resolved = await self._resolve_direct_user(principal, trust)
        else:  # ContactBinding rejects this, but retain a fail-closed port boundary.
            message = "Apply quota-contact source is unsupported"
            raise ContactResolutionError(message)
        if resolved.binding != binding:
            message = "Apply quota contact does not match the reviewed Plan"
            raise ContactResolutionError(message)
        return resolved

    async def _resolve_profile(
        self,
        profile_name: str,
        *,
        source: str,
        trust: LoadedInstallationTrust,
    ) -> ResolvedContact:
        configuration = await self._configuration.read()
        profile = _profile(configuration, profile_name)
        reference = profile.quota_contact_keyring_reference
        if reference is None:
            message = f"quota-contact profile {profile_name!r} has no keyring reference"
            raise ContactResolutionError(message)
        outcome = self._contacts.get_quota_contact(reference)
        if outcome.status is not SecretStoreStatus.AVAILABLE or outcome.secret is None:
            message = (
                f"quota-contact profile {profile_name!r} is {outcome.status.value}"
            )
            raise ContactResolutionError(message)
        _contact_text(outcome.secret)
        return _resolved(
            outcome.secret,
            trust.authentication_key,
            source=source,
            source_identity=reference.canonical_name,
        )

    async def _resolve_direct_user(
        self,
        principal: PlanPrincipal,
        trust: LoadedInstallationTrust,
    ) -> ResolvedContact:
        evidence = await self._identity.resolve(
            timeout_seconds=self._identity_timeout_seconds,
        )
        if (
            evidence.credential_kind is not CredentialKind.DIRECT_USER
            or evidence.verification is not PrincipalVerification.VERIFIED
            or evidence.stable_principal is None
            or evidence.stable_principal.value != principal.stable_identity
            or evidence.direct_user_email is None
        ):
            message = "verified direct-user quota contact is unavailable"
            raise ContactResolutionError(message)
        value = SecretValue(evidence.direct_user_email.value.encode("utf-8"))
        _contact_text(value)
        return _resolved(
            value,
            trust.authentication_key,
            source="direct-user",
            source_identity=principal.stable_identity,
        )


def bind_protected_contact(
    value: SecretValue,
    authentication_key: SecretValue,
) -> ContactBinding:
    """Bind a protected per-operation contact without retaining its value."""
    return _resolved(
        value,
        authentication_key,
        source="per-operation-input",
        source_identity=None,
    ).binding


def _resolved(
    value: SecretValue,
    authentication_key: SecretValue,
    *,
    source: str,
    source_identity: str | None,
) -> ResolvedContact:
    contact = value.reveal()
    key = authentication_key.reveal()
    if source_identity is None:
        source_digest = hmac.new(
            key,
            b"cqmgr-contact-source/v1:" + contact,
            sha256,
        ).hexdigest()
        source_identity = f"input:hmac-sha256:{source_digest}"
    value_digest = hmac.new(
        key,
        b"cqmgr-contact-value/v1:" + contact,
        sha256,
    ).hexdigest()
    return ResolvedContact(
        ContactBinding(
            StableSymbol(source),
            source_identity,
            f"hmac-sha256:{value_digest}",
        ),
        value,
    )


def _contact_text(value: SecretValue) -> str:
    try:
        decoded = value.reveal().decode("utf-8")
        VerifiedDirectUserEmail(decoded)
    except (UnicodeDecodeError, ValueError) as error:
        message = "quota contact must be one valid UTF-8 email"
        raise ContactResolutionError(message) from error
    return decoded


def _profile(configuration: ConfigSnapshot, name: str) -> Profile:
    try:
        return configuration.profile(name)
    except LookupError as error:
        message = f"quota-contact profile {name!r} is unavailable"
        raise ContactResolutionError(message) from error
