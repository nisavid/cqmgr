"""Exact profile contact identity and invocation-scoped Apply retention."""

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
from cqmgr.domain.plans import PlanPrincipal

NOW = datetime(2026, 7, 24, 16, tzinfo=UTC)
KEY = SecretValue(b"k" * 32)
CONTACT = SecretValue(b"operator@example.com")
PRINCIPAL = PlanPrincipal("principal://accounts/123")
TRUST = LoadedInstallationTrust(
    "installation-a",
    KEY,
    keyring_mutation_capable=True,
)


class _Configuration:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self.snapshot = snapshot

    async def read(self) -> ConfigSnapshot:
        return self.snapshot


class _Selection:
    async def read(self) -> SelectionState:
        return SelectionState()


class _Contacts:
    def __init__(self) -> None:
        self.values: dict[QuotaContactKeyringReference, SecretValue] = {}

    def get_quota_contact(
        self,
        reference: QuotaContactKeyringReference,
    ) -> SecretStoreOutcome:
        value = self.values.get(reference)
        if value is None:
            return SecretStoreOutcome(SecretStoreStatus.MISSING)
        return SecretStoreOutcome.available(value)


class _UnusedIdentity:
    async def resolve(self, **_: object) -> object:
        raise AssertionError


def _resolver(
    configuration: _Configuration,
    contacts: _Contacts,
) -> ProtectedContactResolver:
    return ProtectedContactResolver(
        configuration,
        _Selection(),
        contacts,
        _UnusedIdentity(),  # type: ignore[arg-type]
    )


def test_profile_contact_reference_is_random_and_installation_scoped() -> None:
    """Each profile item is opaque and cannot collide across installations."""
    first = QuotaContactKeyringReference.generate("primary", "installation-a")
    second = QuotaContactKeyringReference.generate("primary", "installation-a")
    other_installation = QuotaContactKeyringReference.generate(
        "primary",
        "installation-b",
    )

    assert first.item_id != second.item_id
    assert first.account == first.item_id
    assert first.service != other_installation.service
    assert first.canonical_name != other_installation.canonical_name
    assert QuotaContactKeyringReference.parse(first.canonical_name) == first


def test_apply_rejects_same_profile_and_value_from_a_different_exact_item() -> None:
    """A reviewed profile item cannot be replaced by another item with equal bytes."""
    reviewed = QuotaContactKeyringReference(
        "primary",
        "installation-a",
        "item-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    replacement = QuotaContactKeyringReference(
        "primary",
        "installation-a",
        "item-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    )
    configuration = _Configuration(
        ConfigSnapshot(
            profiles=(Profile("primary", quota_contact_keyring_reference=reviewed),)
        )
    )
    contacts = _Contacts()
    contacts.values[reviewed] = CONTACT
    contacts.values[replacement] = CONTACT
    resolver = _resolver(configuration, contacts)

    preview = asyncio.run(
        resolver.resolve_preview(
            explicit_value=None,
            explicit_profile="primary",
            principal=PRINCIPAL,
            trust=TRUST,
        )
    )
    assert preview.binding.source_identity == reviewed.canonical_name

    configuration.snapshot = ConfigSnapshot(
        profiles=(Profile("primary", quota_contact_keyring_reference=replacement),)
    )
    with pytest.raises(ContactResolutionError, match="reviewed Plan"):
        asyncio.run(
            resolver.prepare_apply(
                preview.binding,
                explicit_value=None,
                principal=PRINCIPAL,
                trust=TRUST,
            )
        )


def test_apply_contexts_are_concurrent_safe_and_consumed_after_refresh() -> None:
    """Equal concurrent Apply invocations each get one non-retained context."""
    resolver = _resolver(_Configuration(ConfigSnapshot()), _Contacts())
    binding = bind_protected_contact(CONTACT, KEY)

    async def exercise() -> None:
        await asyncio.gather(
            resolver.prepare_apply(
                binding,
                explicit_value=CONTACT,
                principal=PRINCIPAL,
                trust=TRUST,
            ),
            resolver.prepare_apply(
                binding,
                explicit_value=CONTACT,
                principal=PRINCIPAL,
                trust=TRUST,
            ),
        )
        refreshed = await asyncio.gather(
            resolver.refresh_contact(binding, NOW),
            resolver.refresh_contact(binding, NOW),
        )
        assert all(value.value == "operator@example.com" for value in refreshed)
        with pytest.raises(ContactResolutionError, match="source is unavailable"):
            await resolver.refresh_contact(binding, NOW)

    asyncio.run(exercise())
