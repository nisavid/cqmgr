"""Protected quota-contact source selection and exact Apply re-resolution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from cqmgr.application.configuration import (
    ConfigSnapshot,
    Profile,
    QuotaContactKeyringReference,
    SelectionState,
)
from cqmgr.application.operations.contacts import (
    ContactResolutionError,
    ProtectedContactResolver,
    bind_protected_contact,
)
from cqmgr.application.operations.trust import LoadedInstallationTrust
from cqmgr.application.ports.secrets import (
    SecretStoreOutcome,
    SecretStoreStatus,
    SecretValue,
)
from cqmgr.domain.identity import (
    ADCIdentityEvidence,
    ADCQuotaProject,
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
    VerifiedDirectUserEmail,
)
from cqmgr.domain.plans import PlanPrincipal

KEY = SecretValue(b"k" * 32)
TRUST = LoadedInstallationTrust(
    "installation-test",
    KEY,
    keyring_mutation_capable=True,
)
PRINCIPAL = PlanPrincipal("principal://accounts/123")
EXPLICIT = SecretValue(b"explicit@example.com")
PROFILE_CONTACT = SecretValue(b"profile@example.com")
NOW = datetime(2026, 7, 24, 16, tzinfo=UTC)
IDENTITY_TIMEOUT_SECONDS = 10.0
PREVIEW_AND_APPLY_IDENTITY_CALLS = 2


class _Config:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self.snapshot = snapshot
        self.reads = 0

    async def read(self) -> ConfigSnapshot:
        self.reads += 1
        return self.snapshot


class _Selection:
    def __init__(self, state: SelectionState) -> None:
        self.state = state
        self.reads = 0

    async def read(self) -> SelectionState:
        self.reads += 1
        return self.state


class _Contacts:
    def __init__(self, outcome: SecretStoreOutcome) -> None:
        self.outcome = outcome
        self.references: list[QuotaContactKeyringReference] = []

    def get_quota_contact(
        self,
        reference: QuotaContactKeyringReference,
    ) -> SecretStoreOutcome:
        self.references.append(reference)
        return self.outcome


class _Identity:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(
        self,
        *,
        adc_quota_project: ADCQuotaProject | None = None,
        timeout_seconds: float = IDENTITY_TIMEOUT_SECONDS,
    ) -> ADCIdentityEvidence:
        assert adc_quota_project is None
        assert timeout_seconds == IDENTITY_TIMEOUT_SECONDS
        self.calls += 1
        principal = PrincipalIdentity(PRINCIPAL.stable_identity)
        return ADCIdentityEvidence(
            credential_kind=CredentialKind.DIRECT_USER,
            acting_principal=principal,
            stable_principal=principal,
            verification=PrincipalVerification.VERIFIED,
            direct_user_email=VerifiedDirectUserEmail("direct@example.com"),
        )


def _resolver(
    *,
    config: ConfigSnapshot | None = None,
    selection: SelectionState | None = None,
    outcome: SecretStoreOutcome | None = None,
) -> tuple[ProtectedContactResolver, _Config, _Selection, _Contacts, _Identity]:
    configuration = _Config(config or ConfigSnapshot())
    selected = _Selection(selection or SelectionState())
    contacts = _Contacts(outcome or SecretStoreOutcome(SecretStoreStatus.MISSING))
    identity = _Identity()
    return (
        ProtectedContactResolver(configuration, selected, contacts, identity),
        configuration,
        selected,
        contacts,
        identity,
    )


def test_preview_contact_precedence_is_explicit_named_selected_then_direct() -> None:
    """Each earlier selected source wins without consulting later sources."""
    named_ref = QuotaContactKeyringReference("named")
    selected_ref = QuotaContactKeyringReference("selected")
    config = ConfigSnapshot(
        profiles=(
            Profile("named", quota_contact_keyring_reference=named_ref),
            Profile("selected", quota_contact_keyring_reference=selected_ref),
        )
    )
    resolver, configuration, selection, contacts, identity = _resolver(
        config=config,
        selection=SelectionState(selected_profile="selected"),
        outcome=SecretStoreOutcome.available(PROFILE_CONTACT),
    )

    explicit = asyncio.run(
        resolver.resolve_preview(
            explicit_value=EXPLICIT,
            explicit_profile="named",
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    assert explicit.binding.source.value == "per-operation-input"
    assert configuration.reads == selection.reads == identity.calls == 0

    named = asyncio.run(
        resolver.resolve_preview(
            explicit_value=None,
            explicit_profile="named",
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    assert named.binding.source.value == "named-profile"
    assert named.binding.source_identity == "profile:named"
    assert contacts.references[-1] == named_ref
    assert selection.reads == identity.calls == 0

    selected = asyncio.run(
        resolver.resolve_preview(
            explicit_value=None,
            explicit_profile=None,
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    assert selected.binding.source.value == "selected-profile"
    assert selected.binding.source_identity == "profile:selected"
    assert contacts.references[-1] == selected_ref
    assert identity.calls == 0

    direct_resolver, _, _, _, direct_identity = _resolver()
    direct = asyncio.run(
        direct_resolver.resolve_preview(
            explicit_value=None,
            explicit_profile=None,
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    assert direct.binding.source.value == "direct-user"
    assert direct.binding.source_identity == PRINCIPAL.stable_identity
    assert direct_identity.calls == 1
    assert "@" not in repr(direct)
    direct_apply = asyncio.run(
        direct_resolver.prepare_apply(
            direct.binding,
            explicit_value=None,
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    assert direct_apply.binding == direct.binding
    assert direct_identity.calls == PREVIEW_AND_APPLY_IDENTITY_CALLS


def test_selected_profile_failure_never_falls_through_to_direct_user() -> None:
    """An unavailable selected source is a terminal contact resolution failure."""
    reference = QuotaContactKeyringReference("selected")
    resolver, _, _, contacts, identity = _resolver(
        config=ConfigSnapshot(
            profiles=(Profile("selected", quota_contact_keyring_reference=reference),)
        ),
        selection=SelectionState(selected_profile="selected"),
    )

    with pytest.raises(ContactResolutionError, match="missing"):
        asyncio.run(
            resolver.resolve_preview(
                explicit_value=None,
                explicit_profile=None,
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )

    assert contacts.references == [reference]
    assert identity.calls == 0


def test_apply_re_resolves_only_the_exact_plan_bound_source() -> None:
    """Apply cannot substitute an available later source for its Plan binding."""
    reference = QuotaContactKeyringReference("selected")
    resolver, _, _, contacts, identity = _resolver(
        config=ConfigSnapshot(
            profiles=(Profile("selected", quota_contact_keyring_reference=reference),)
        ),
        selection=SelectionState(selected_profile="selected"),
        outcome=SecretStoreOutcome.available(PROFILE_CONTACT),
    )
    preview = asyncio.run(
        resolver.resolve_preview(
            explicit_value=None,
            explicit_profile=None,
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )

    applied = asyncio.run(
        resolver.prepare_apply(
            preview.binding,
            explicit_value=None,
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )

    assert applied.binding == preview.binding
    assert applied.value.reveal() == PROFILE_CONTACT.reveal()
    assert identity.calls == 0

    contacts.outcome = SecretStoreOutcome(SecretStoreStatus.LOCKED_OR_CANCELLED)
    with pytest.raises(ContactResolutionError, match="locked-or-cancelled"):
        asyncio.run(resolver.refresh_contact(preview.binding, NOW))
    assert identity.calls == 0


def test_contact_resolver_rejects_nonpositive_identity_timeout() -> None:
    """Identity lookup must always retain a finite positive timeout."""
    _, configuration, selection, contacts, identity = _resolver()

    with pytest.raises(ValueError, match="timeout must be positive"):
        ProtectedContactResolver(
            configuration,
            selection,
            contacts,
            identity,
            identity_timeout_seconds=0,
        )


@pytest.mark.parametrize(
    "value",
    [
        SecretValue(b"not-an-email"),
        SecretValue(b"\xff"),
    ],
)
def test_explicit_contact_must_be_one_utf8_email(value: SecretValue) -> None:
    """Protected input is validated before any lower-precedence source is read."""
    resolver, configuration, selection, _, identity = _resolver()

    with pytest.raises(ContactResolutionError, match="valid UTF-8 email"):
        asyncio.run(
            resolver.resolve_preview(
                explicit_value=value,
                explicit_profile=None,
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )

    assert configuration.reads == selection.reads == identity.calls == 0


def test_apply_requires_prepared_context_and_bound_explicit_value() -> None:
    """Apply refresh cannot invent either an invocation or protected input."""
    resolver, _, _, _, _ = _resolver()
    binding = bind_protected_contact(EXPLICIT, KEY)

    with pytest.raises(ContactResolutionError, match="source is unavailable"):
        asyncio.run(resolver.refresh_contact(binding, NOW))
    with pytest.raises(ContactResolutionError, match="Plan-bound per-operation"):
        asyncio.run(
            resolver.prepare_apply(
                binding,
                explicit_value=None,
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )


def test_selected_profile_must_remain_selected_at_apply() -> None:
    """A Plan bound to selected-profile cannot silently follow selection drift."""
    reference = QuotaContactKeyringReference("selected")
    resolver, _, selection, _, _ = _resolver(
        config=ConfigSnapshot(
            profiles=(Profile("selected", quota_contact_keyring_reference=reference),)
        ),
        selection=SelectionState(selected_profile="selected"),
        outcome=SecretStoreOutcome.available(PROFILE_CONTACT),
    )
    preview = asyncio.run(
        resolver.resolve_preview(
            explicit_value=None,
            explicit_profile=None,
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    selection.state = SelectionState()

    with pytest.raises(ContactResolutionError, match="profile changed"):
        asyncio.run(
            resolver.prepare_apply(
                preview.binding,
                explicit_value=None,
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )


def test_named_profile_errors_are_terminal() -> None:
    """Missing profile metadata never falls through to selected or direct-user."""
    resolver, _, selection, _, identity = _resolver()

    with pytest.raises(ContactResolutionError, match="is unavailable"):
        asyncio.run(
            resolver.resolve_preview(
                explicit_value=None,
                explicit_profile="missing",
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )

    no_reference, _, _, _, _ = _resolver(
        config=ConfigSnapshot(profiles=(Profile("named"),))
    )
    with pytest.raises(ContactResolutionError, match="no keyring reference"):
        asyncio.run(
            no_reference.resolve_preview(
                explicit_value=None,
                explicit_profile="named",
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )
    assert selection.reads == identity.calls == 0


def test_profile_value_and_direct_principal_must_match_plan_binding() -> None:
    """Apply rejects changed profile values and a different direct-user principal."""
    reference = QuotaContactKeyringReference("named")
    resolver, _, _, contacts, _ = _resolver(
        config=ConfigSnapshot(
            profiles=(Profile("named", quota_contact_keyring_reference=reference),)
        ),
        outcome=SecretStoreOutcome.available(PROFILE_CONTACT),
    )
    profile = asyncio.run(
        resolver.resolve_preview(
            explicit_value=None,
            explicit_profile="named",
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    contacts.outcome = SecretStoreOutcome.available(SecretValue(b"changed@example.com"))
    with pytest.raises(ContactResolutionError, match="does not match"):
        asyncio.run(
            resolver.prepare_apply(
                profile.binding,
                explicit_value=None,
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )

    direct, _, _, _, _ = _resolver()
    direct_preview = asyncio.run(
        direct.resolve_preview(
            explicit_value=None,
            explicit_profile=None,
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    with pytest.raises(ContactResolutionError, match="principal changed"):
        asyncio.run(
            direct.prepare_apply(
                direct_preview.binding,
                explicit_value=None,
                principal=PlanPrincipal("principal://accounts/other"),
                trust=TRUST,
            )
        )
