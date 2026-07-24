"""Resource-scope configuration and precedence contracts."""

from __future__ import annotations

from typing import cast

import pytest

from cqmgr.application.configuration import (
    ConfigSnapshot,
    InterfaceSettingKey,
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


def test_keyring_reference_parser_rejects_malformed_complete_values() -> None:
    """Typed prefixes still require exact profile, installation, and item grammar."""
    with pytest.raises(TypeError, match="must be a string"):
        QuotaContactKeyringReference.parse(cast("str", None))
    with pytest.raises(ValueError, match="keyring grammar"):
        QuotaContactKeyringReference.parse("cqmgr:quota-contact:v1:primary")
    with pytest.raises(ValueError, match="keyring grammar"):
        QuotaContactKeyringReference.parse(
            "cqmgr:quota-contact:v1:bad profile:installation-test:item-short"
        )


def test_configuration_value_objects_reject_malformed_runtime_values() -> None:
    """Invalid local configuration is rejected at its owning typed boundary."""
    with pytest.raises(TypeError, match="must be booleans"):
        InterfaceSettings(no_color=cast("bool", 1))
    with pytest.raises(TypeError, match="value must be bool"):
        InterfaceSettings().replace(
            InterfaceSettingKey.NO_COLOR,
            value=cast("bool", 1),
        )
    with pytest.raises(ValueError, match="profile name"):
        Profile(name="")
    with pytest.raises(TypeError, match="resource_scope"):
        Profile(name="primary", resource_scope=cast("ResourceScope", "projects/1"))
    with pytest.raises(TypeError, match="adc_quota_project"):
        Profile(name="primary", adc_quota_project=cast("ResourceScope", "projects/1"))
    with pytest.raises(ValueError, match="canonical project"):
        Profile(
            name="primary",
            adc_quota_project=ResourceScope(
                ResourceScopeKind.FOLDER,
                "folders/1",
            ),
        )
    with pytest.raises(TypeError, match="profile interface"):
        Profile(name="primary", interface=cast("InterfaceSettings", object()))

    profile = Profile(name="primary")
    with pytest.raises(ValueError, match="configuration schema"):
        ConfigSnapshot(schema="cqmgr.config/other")
    with pytest.raises(TypeError, match="tuple of Profile"):
        ConfigSnapshot(profiles=cast("tuple[Profile, ...]", [profile]))
    with pytest.raises(ValueError, match="must be unique"):
        ConfigSnapshot(profiles=(profile, profile))
    with pytest.raises(TypeError, match="configuration interface"):
        ConfigSnapshot(interface=cast("InterfaceSettings", object()))

    with pytest.raises(ValueError, match="selection schema"):
        SelectionState(schema="cqmgr.selection-state/other")
    with pytest.raises(ValueError, match="selected profile"):
        SelectionState(selected_profile="")
    with pytest.raises(TypeError, match="direct resource scope"):
        SelectionState(direct_resource_scope=cast("ResourceScope", "projects/1"))
