"""Resource-scope configuration and precedence contracts."""

from __future__ import annotations

from typing import cast

import pytest

from cqmgr.application.configuration import (
    ConfigSnapshot,
    InterfaceSettings,
    Profile,
    ProfileResourceScopeError,
    QuotaContactKeyringReference,
    ScopeResolutionSource,
    SelectionState,
    UnsupportedResourceScopeError,
    resolve_resource_scope,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind


def project(identifier: str) -> ResourceScope:
    """Build one canonical project resource scope."""
    return ResourceScope(ResourceScopeKind.PROJECT, f"projects/{identifier}")


def test_explicit_resource_scope_wins_every_other_source() -> None:
    """Explicit operation intent takes precedence over all saved local state."""
    configuration = ConfigSnapshot(
        profiles=(Profile(name="named", resource_scope=project("2")),)
    )
    selection = SelectionState(
        selected_profile="named",
        direct_resource_scope=project("3"),
    )

    resolved = resolve_resource_scope(
        configuration,
        selection,
        explicit_resource_scope=project("1"),
        explicit_profile="named",
    )

    assert resolved.resource_scope == project("1")
    assert resolved.source is ScopeResolutionSource.EXPLICIT_INPUT


def test_explicit_profile_without_scope_does_not_fall_through() -> None:
    """A named profile preserves caller intent even when it has no project."""
    configuration = ConfigSnapshot(
        profiles=(
            Profile(name="auth-only"),
            Profile(name="selected", resource_scope=project("4")),
        )
    )
    selection = SelectionState(
        selected_profile="selected",
        direct_resource_scope=project("3"),
    )

    with pytest.raises(ProfileResourceScopeError, match="has no resource scope"):
        resolve_resource_scope(
            configuration,
            selection,
            explicit_profile="auth-only",
        )


@pytest.mark.parametrize(
    ("selection", "expected_scope", "expected_source"),
    [
        (
            SelectionState(
                selected_profile="selected",
                direct_resource_scope=project("3"),
            ),
            project("3"),
            ScopeResolutionSource.DIRECT_SELECTION,
        ),
        (
            SelectionState(selected_profile="selected"),
            project("4"),
            ScopeResolutionSource.SELECTED_PROFILE,
        ),
    ],
)
def test_saved_selection_precedence(
    selection: SelectionState,
    expected_scope: ResourceScope,
    expected_source: ScopeResolutionSource,
) -> None:
    """A direct selection wins, otherwise the selected profile supplies scope."""
    configuration = ConfigSnapshot(
        profiles=(Profile(name="selected", resource_scope=project("4")),)
    )

    resolved = resolve_resource_scope(configuration, selection)

    assert resolved.resource_scope == expected_scope
    assert resolved.source is expected_source


@pytest.mark.parametrize(
    "unsupported_scope",
    [
        ResourceScope(ResourceScopeKind.FOLDER, "folders/12"),
        ResourceScope(ResourceScopeKind.ORGANIZATION, "organizations/13"),
    ],
)
def test_v1_rejects_non_project_scopes_without_inference(
    unsupported_scope: ResourceScope,
) -> None:
    """Schema-reserved folder and organization inputs fail before fallback."""
    with pytest.raises(UnsupportedResourceScopeError):
        resolve_resource_scope(
            ConfigSnapshot(),
            SelectionState(direct_resource_scope=project("3")),
            explicit_resource_scope=unsupported_scope,
        )


def test_interface_settings_reject_non_enum_keys() -> None:
    """Public config access fails closed instead of defaulting or leaking KeyError."""
    settings = InterfaceSettings()
    invalid = "interface.unknown"

    with pytest.raises(TypeError, match="InterfaceSettingKey"):
        settings.get(invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="InterfaceSettingKey"):
        settings.replace(invalid, value=True)  # type: ignore[arg-type]


def test_quota_contact_uses_a_closed_profile_bound_os_keyring_reference() -> None:
    """Profiles can retain only safe native-keyring item identity metadata."""
    reference = QuotaContactKeyringReference(
        "primary",
        "installation-test",
        "item-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )

    assert reference.canonical_name == (
        "cqmgr:quota-contact:v1:primary:installation-test:"
        "item-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    assert reference.service == ("io.nisavid.cqmgr/installation-test/quota-contact")
    assert reference.account == "item-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert (
        Profile(
            name="primary",
            quota_contact_keyring_reference=reference,
        ).quota_contact_keyring_reference
        == reference
    )

    with pytest.raises(TypeError, match="QuotaContactKeyringReference"):
        Profile(
            name="primary",
            quota_contact_keyring_reference=cast(
                "QuotaContactKeyringReference",
                "operator@example.com",
            ),
        )
    with pytest.raises(ValueError, match="profile name"):
        Profile(
            name="primary",
            quota_contact_keyring_reference=QuotaContactKeyringReference("secondary"),
        )


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "operator@example.com",
        "raw quota contact",
        "credential-json",
        "cqmgr:quota-contact:operator@example.com",
    ],
)
def test_keyring_reference_parser_rejects_raw_contact_and_credentials(
    unsafe_value: str,
) -> None:
    """Only the complete closed cqmgr native-keyring grammar is accepted."""
    with pytest.raises(ValueError, match="keyring reference"):
        QuotaContactKeyringReference.parse(unsafe_value)
