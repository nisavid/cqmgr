"""Validated local configuration and resource-scope resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

CONFIG_SCHEMA = "cqmgr.config/v1"
SELECTION_STATE_SCHEMA = "cqmgr.selection-state/v1"

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_QUOTA_CONTACT_KEYRING_PREFIX = "cqmgr:quota-contact:"


class ConfigurationError(ValueError):
    """Base class for invalid local configuration intent."""


class UnknownProfileError(ConfigurationError, LookupError):
    """An explicitly named profile does not exist."""


class ProfileResourceScopeError(ConfigurationError, LookupError):
    """An explicitly named profile has no resource scope."""


class ResourceScopeUnavailableError(ConfigurationError, LookupError):
    """No explicit or selected resource scope is available."""


class UnsupportedResourceScopeError(ConfigurationError):
    """A canonical resource scope is schema-valid but unsupported in V1."""


class ScopeResolutionSource(StrEnum):
    """The explicit source that supplied a resolved project resource scope."""

    EXPLICIT_INPUT = "explicit-input"
    NAMED_PROFILE = "named-profile"
    DIRECT_SELECTION = "direct-selection"
    SELECTED_PROFILE = "selected-profile"


class InterfaceSettingKey(StrEnum):
    """Validated public keys changed by local config operations."""

    NO_COLOR = "interface.no-color"
    VIM_NAVIGATION = "interface.vim-navigation"
    NERD_FONT = "interface.nerd-font"


@dataclass(frozen=True, slots=True)
class InterfaceSettings:
    """Local presentation defaults that never encode operation intent."""

    no_color: bool = False
    vim_navigation: bool = False
    nerd_font: bool = False

    def __post_init__(self) -> None:
        """Require explicit booleans for every interface default."""
        if any(
            not isinstance(value, bool)
            for value in (self.no_color, self.vim_navigation, self.nerd_font)
        ):
            msg = "interface settings must be booleans"
            raise TypeError(msg)

    def get(self, key: InterfaceSettingKey) -> bool:
        """Read one public interface setting."""
        if not isinstance(key, InterfaceSettingKey):
            msg = "interface setting key must be an InterfaceSettingKey"
            raise TypeError(msg)
        if key is InterfaceSettingKey.NO_COLOR:
            return self.no_color
        if key is InterfaceSettingKey.VIM_NAVIGATION:
            return self.vim_navigation
        return self.nerd_font

    def replace(
        self,
        key: InterfaceSettingKey,
        *,
        value: bool,
    ) -> InterfaceSettings:
        """Return settings with one validated public key changed."""
        if not isinstance(key, InterfaceSettingKey):
            msg = "interface setting key must be an InterfaceSettingKey"
            raise TypeError(msg)
        if not isinstance(value, bool):
            msg = "interface setting value must be bool"
            raise TypeError(msg)
        values = {
            "no_color": self.no_color,
            "vim_navigation": self.vim_navigation,
            "nerd_font": self.nerd_font,
        }
        field_name = {
            InterfaceSettingKey.NO_COLOR: "no_color",
            InterfaceSettingKey.VIM_NAVIGATION: "vim_navigation",
            InterfaceSettingKey.NERD_FONT: "nerd_font",
        }[key]
        values[field_name] = value
        return InterfaceSettings(**values)


@dataclass(frozen=True, slots=True)
class QuotaContactKeyringReference:
    """Closed native-keyring item identity for one profile's quota contact."""

    profile_name: str

    def __post_init__(self) -> None:
        """Restrict references to the same safe grammar as profile identity."""
        if (
            not isinstance(self.profile_name, str)
            or _PROFILE_NAME.fullmatch(self.profile_name) is None
        ):
            msg = "quota-contact keyring reference profile name is invalid"
            raise ValueError(msg)

    @classmethod
    def parse(cls, value: str) -> QuotaContactKeyringReference:
        """Parse only the complete cqmgr-owned native-keyring reference grammar."""
        if not isinstance(value, str):
            msg = "quota-contact keyring reference must be a string"
            raise TypeError(msg)
        if not value.startswith(_QUOTA_CONTACT_KEYRING_PREFIX):
            msg = "quota-contact keyring reference must use the cqmgr keyring grammar"
            raise ValueError(msg)
        profile_name = value.removeprefix(_QUOTA_CONTACT_KEYRING_PREFIX)
        try:
            reference = cls(profile_name)
        except ValueError as error:
            msg = "quota-contact keyring reference must use the cqmgr keyring grammar"
            raise ValueError(msg) from error
        if reference.canonical_name != value:
            msg = "quota-contact keyring reference must use the cqmgr keyring grammar"
            raise ValueError(msg)
        return reference

    @property
    def backend(self) -> str:
        """Name the required native OS-keyring backend class."""
        return "os-keyring"

    @property
    def service(self) -> str:
        """Return the fixed keyring service name."""
        return "cqmgr"

    @property
    def account(self) -> str:
        """Return the non-secret keyring account identity."""
        return f"quota-contact:{self.profile_name}"

    @property
    def canonical_name(self) -> str:
        """Return the validated persisted reference representation."""
        return f"{_QUOTA_CONTACT_KEYRING_PREFIX}{self.profile_name}"


@dataclass(frozen=True, slots=True)
class Profile:
    """One declarative local profile without credentials or operation intent."""

    name: str
    resource_scope: ResourceScope | None = None
    adc_quota_project: ResourceScope | None = None
    quota_contact_keyring_reference: QuotaContactKeyringReference | None = None
    interface: InterfaceSettings = InterfaceSettings()

    def __post_init__(self) -> None:
        """Validate profile identity and safe declarative references."""
        if not isinstance(self.name, str) or _PROFILE_NAME.fullmatch(self.name) is None:
            msg = (
                "profile name must use 1-64 ASCII letters, digits, dot, dash, "
                "or underscore"
            )
            raise ValueError(msg)
        if self.resource_scope is not None and not isinstance(
            self.resource_scope, ResourceScope
        ):
            msg = "profile resource_scope must be a ResourceScope or None"
            raise TypeError(msg)
        if self.adc_quota_project is not None:
            if not isinstance(self.adc_quota_project, ResourceScope):
                msg = "profile adc_quota_project must be a ResourceScope or None"
                raise TypeError(msg)
            if self.adc_quota_project.kind is not ResourceScopeKind.PROJECT:
                msg = "ADC quota project must be a canonical project resource scope"
                raise ValueError(msg)
        reference = self.quota_contact_keyring_reference
        if reference is not None:
            if not isinstance(reference, QuotaContactKeyringReference):
                msg = (
                    "quota-contact keyring reference must be a "
                    "QuotaContactKeyringReference"
                )
                raise TypeError(msg)
            if reference.profile_name != self.name:
                msg = "quota-contact keyring reference must match the profile name"
                raise ValueError(msg)
        if not isinstance(self.interface, InterfaceSettings):
            msg = "profile interface must be InterfaceSettings"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """One validated current configuration snapshot."""

    profiles: tuple[Profile, ...] = ()
    interface: InterfaceSettings = InterfaceSettings()
    schema: str = CONFIG_SCHEMA

    def __post_init__(self) -> None:
        """Reject malformed snapshots and duplicate profile identities."""
        if self.schema != CONFIG_SCHEMA:
            msg = f"configuration schema must be {CONFIG_SCHEMA!r}"
            raise ValueError(msg)
        if not isinstance(self.profiles, tuple) or any(
            not isinstance(profile, Profile) for profile in self.profiles
        ):
            msg = "profiles must be a tuple of Profile values"
            raise TypeError(msg)
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            msg = "profile names must be unique"
            raise ValueError(msg)
        if not isinstance(self.interface, InterfaceSettings):
            msg = "configuration interface must be InterfaceSettings"
            raise TypeError(msg)

    def profile(self, name: str) -> Profile:
        """Return an explicitly named profile or preserve the lookup failure."""
        for profile in self.profiles:
            if profile.name == name:
                return profile
        msg = f"unknown profile {name!r}"
        raise UnknownProfileError(msg)


@dataclass(frozen=True, slots=True)
class SelectionState:
    """Atomic mutable selected-profile and direct-scope state."""

    selected_profile: str | None = None
    direct_resource_scope: ResourceScope | None = None
    schema: str = SELECTION_STATE_SCHEMA

    def __post_init__(self) -> None:
        """Validate the independently versioned mutable state."""
        if self.schema != SELECTION_STATE_SCHEMA:
            msg = f"selection schema must be {SELECTION_STATE_SCHEMA!r}"
            raise ValueError(msg)
        if self.selected_profile is not None and (
            not isinstance(self.selected_profile, str)
            or _PROFILE_NAME.fullmatch(self.selected_profile) is None
        ):
            msg = "selected profile name is invalid"
            raise ValueError(msg)
        if self.direct_resource_scope is not None and not isinstance(
            self.direct_resource_scope, ResourceScope
        ):
            msg = "direct resource scope must be a ResourceScope or None"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ScopeResolution:
    """One canonical V1 project and the explicit source that supplied it."""

    resource_scope: ResourceScope
    source: ScopeResolutionSource


def _require_project(resource_scope: ResourceScope) -> ResourceScope:
    if resource_scope.kind is not ResourceScopeKind.PROJECT:
        msg = (
            f"{resource_scope.kind.value} resource scopes are reserved but unsupported "
            "in V1"
        )
        raise UnsupportedResourceScopeError(msg)
    return resource_scope


def parse_resource_scope_name(value: str) -> ResourceScope:
    """Parse one full canonical resource name without guessing its kind."""
    if not isinstance(value, str):
        msg = "resource scope input must be a string"
        raise TypeError(msg)
    for kind in ResourceScopeKind:
        if value.startswith(f"{kind.value}s/"):
            return ResourceScope(kind, value)
    msg = (
        "resource scope must be a canonical projects/, folders/, or organizations/ name"
    )
    raise ValueError(msg)


def resolve_resource_scope(
    configuration: ConfigSnapshot,
    selection: SelectionState,
    *,
    explicit_resource_scope: ResourceScope | None = None,
    explicit_profile: str | None = None,
) -> ScopeResolution:
    """Resolve one V1 project without ambient project or credential inference."""
    if explicit_resource_scope is not None:
        return ScopeResolution(
            _require_project(explicit_resource_scope),
            ScopeResolutionSource.EXPLICIT_INPUT,
        )
    if explicit_profile is not None:
        profile = configuration.profile(explicit_profile)
        if profile.resource_scope is None:
            msg = f"profile {explicit_profile!r} has no resource scope"
            raise ProfileResourceScopeError(msg)
        return ScopeResolution(
            _require_project(profile.resource_scope),
            ScopeResolutionSource.NAMED_PROFILE,
        )
    if selection.direct_resource_scope is not None:
        return ScopeResolution(
            _require_project(selection.direct_resource_scope),
            ScopeResolutionSource.DIRECT_SELECTION,
        )
    if selection.selected_profile is not None:
        profile = configuration.profile(selection.selected_profile)
        if profile.resource_scope is None:
            msg = f"profile {profile.name!r} has no resource scope"
            raise ProfileResourceScopeError(msg)
        return ScopeResolution(
            _require_project(profile.resource_scope),
            ScopeResolutionSource.SELECTED_PROFILE,
        )
    msg = "no explicit or selected resource scope is available"
    raise ResourceScopeUnavailableError(msg)
