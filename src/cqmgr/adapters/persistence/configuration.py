"""Atomic TOML persistence for configuration and selection state."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

if os.name == "nt":  # pragma: no cover - imported on the Windows matrix
    import msvcrt
else:  # pragma: no cover - imported on POSIX matrices
    import fcntl

from cqmgr.application.configuration import (
    CONFIG_SCHEMA,
    SELECTION_STATE_SCHEMA,
    ConfigSnapshot,
    InterfaceSettings,
    Profile,
    QuotaContactKeyringReference,
    SelectionState,
)
from cqmgr.application.ports.configuration import (
    ConfigurationRepositoryError,
    UnsupportedConfigurationSchemaError,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

    from cqmgr.application.ports.configuration import (
        ConfigTransform,
        SelectionTransform,
    )

_CONFIG_SCHEMA_V0 = "cqmgr.config/v0"
_SELECTION_SCHEMA_V0 = "cqmgr.selection-state/v0"
_SCHEMA_VERSION = re.compile(r"cqmgr\.(config|selection-state)/v([0-9]+)\Z")


class StoredDataError(ConfigurationRepositoryError, ValueError):
    """Base class for unsafe or unsupported stored local data."""


class InvalidStoredDataError(StoredDataError):
    """Stored data is malformed or violates the closed schema."""


class UnsupportedStoredSchemaError(
    StoredDataError,
    UnsupportedConfigurationSchemaError,
):
    """Stored data uses a newer schema that this build cannot interpret."""


class StoredDataOperationalError(StoredDataError):
    """A filesystem or locking failure prevented trustworthy local state."""


def _operational_error(path: Path, error: OSError) -> StoredDataOperationalError:
    message = f"local state operation failed for {path.name}: {type(error).__name__}"
    return StoredDataOperationalError(message)


def _expect_keys(
    value: dict[str, object],
    allowed: set[str],
    location: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        msg = f"unknown {location} field(s): {', '.join(sorted(unknown))}"
        raise InvalidStoredDataError(msg)


def _table(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        msg = f"{location} must be a TOML table"
        raise InvalidStoredDataError(msg)
    return cast("dict[str, object]", value)


def _optional_string(value: object, location: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{location} must be a string"
        raise InvalidStoredDataError(msg)
    return value


def _interface(value: object, location: str) -> InterfaceSettings:
    table = {} if value is None else _table(value, location)
    _expect_keys(table, {"no_color", "vim_navigation", "nerd_font"}, location)
    for key, setting in table.items():
        if not isinstance(setting, bool):
            msg = f"{location}.{key} must be a boolean"
            raise InvalidStoredDataError(msg)
    return InterfaceSettings(
        no_color=cast("bool", table.get("no_color", False)),
        vim_navigation=cast("bool", table.get("vim_navigation", False)),
        nerd_font=cast("bool", table.get("nerd_font", False)),
    )


def _resource_scope(value: object, location: str) -> ResourceScope | None:
    raw = _optional_string(value, location)
    if raw is None:
        return None
    kinds = {
        "projects/": ResourceScopeKind.PROJECT,
        "folders/": ResourceScopeKind.FOLDER,
        "organizations/": ResourceScopeKind.ORGANIZATION,
    }
    for prefix, kind in kinds.items():
        if raw.startswith(prefix):
            try:
                return ResourceScope(kind, raw)
            except (TypeError, ValueError) as error:
                raise InvalidStoredDataError(str(error)) from error
    msg = f"{location} must be a canonical project, folder, or organization name"
    raise InvalidStoredDataError(msg)


def _schema(value: object, expected_kind: str, current: str) -> str:
    if not isinstance(value, str):
        msg = "schema must be a string"
        raise InvalidStoredDataError(msg)
    match = _SCHEMA_VERSION.fullmatch(value)
    if match is None or match.group(1) != expected_kind:
        msg = f"invalid {expected_kind} schema {value!r}"
        raise InvalidStoredDataError(msg)
    version = int(match.group(2))
    if version > 1:
        msg = f"unsupported newer {expected_kind} schema {value!r}"
        raise UnsupportedStoredSchemaError(msg)
    if value not in {current, current.removesuffix("v1") + "v0"}:
        msg = f"unsupported {expected_kind} schema {value!r}"
        raise InvalidStoredDataError(msg)
    return value


def _decode_config(document: dict[str, object]) -> ConfigSnapshot:
    _expect_keys(document, {"schema", "interface", "profiles"}, "configuration")
    schema = _schema(document.get("schema"), "config", CONFIG_SCHEMA)
    interface = _interface(document.get("interface"), "interface")
    profiles_table = _table(document.get("profiles", {}), "profiles")
    profiles: list[Profile] = []
    for name, raw_profile in profiles_table.items():
        profile = _table(raw_profile, f"profiles.{name}")
        allowed = {
            "adc_quota_project",
            "interface",
            "quota_contact_keyring_reference",
            "resource_scope",
        }
        if schema == _CONFIG_SCHEMA_V0:
            allowed.add("project")
        _expect_keys(profile, allowed, f"profiles.{name}")
        if "project" in profile and "resource_scope" in profile:
            msg = f"profiles.{name} cannot contain both project and resource_scope"
            raise InvalidStoredDataError(msg)
        scope_value = profile.get(
            "resource_scope",
            profile.get("project"),
        )
        try:
            profiles.append(
                Profile(
                    name=name,
                    resource_scope=_resource_scope(
                        scope_value,
                        f"profiles.{name}.resource_scope",
                    ),
                    adc_quota_project=_resource_scope(
                        profile.get("adc_quota_project"),
                        f"profiles.{name}.adc_quota_project",
                    ),
                    quota_contact_keyring_reference=(
                        QuotaContactKeyringReference.parse(reference)
                        if (
                            reference := _optional_string(
                                profile.get("quota_contact_keyring_reference"),
                                f"profiles.{name}.quota_contact_keyring_reference",
                            )
                        )
                        is not None
                        else None
                    ),
                    interface=_interface(
                        profile.get("interface"),
                        f"profiles.{name}.interface",
                    ),
                )
            )
        except (TypeError, ValueError) as error:
            raise InvalidStoredDataError(str(error)) from error
    try:
        return ConfigSnapshot(
            profiles=tuple(sorted(profiles, key=lambda profile: profile.name)),
            interface=interface,
        )
    except (TypeError, ValueError) as error:
        raise InvalidStoredDataError(str(error)) from error


def _decode_selection(document: dict[str, object]) -> SelectionState:
    _expect_keys(
        document,
        {"schema", "selected_profile", "direct_resource_scope", "direct_project"},
        "selection state",
    )
    schema = _schema(
        document.get("schema"),
        "selection-state",
        SELECTION_STATE_SCHEMA,
    )
    if schema == SELECTION_STATE_SCHEMA and "direct_project" in document:
        msg = "direct_project is only valid in selection-state/v0"
        raise InvalidStoredDataError(msg)
    if "direct_resource_scope" in document and "direct_project" in document:
        msg = "selection state cannot contain both direct scope fields"
        raise InvalidStoredDataError(msg)
    selected_profile = _optional_string(
        document.get("selected_profile"),
        "selected_profile",
    )
    try:
        return SelectionState(
            selected_profile=selected_profile,
            direct_resource_scope=_resource_scope(
                document.get(
                    "direct_resource_scope",
                    document.get("direct_project"),
                ),
                "direct_resource_scope",
            ),
        )
    except (TypeError, ValueError) as error:
        raise InvalidStoredDataError(str(error)) from error


def _load(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"cannot read valid TOML from {path.name}: {error}"
        raise InvalidStoredDataError(msg) from error
    return _table(document, "document")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_interface(settings: InterfaceSettings) -> list[str]:
    return [
        f"no_color = {str(settings.no_color).lower()}",
        f"vim_navigation = {str(settings.vim_navigation).lower()}",
        f"nerd_font = {str(settings.nerd_font).lower()}",
    ]


def _render_config(snapshot: ConfigSnapshot) -> str:
    lines = [f"schema = {_toml_string(CONFIG_SCHEMA)}", "", "[interface]"]
    lines.extend(_render_interface(snapshot.interface))
    for profile in sorted(snapshot.profiles, key=lambda item: item.name):
        profile_name = _toml_string(profile.name)
        lines.extend(("", f"[profiles.{profile_name}]"))
        if profile.resource_scope is not None:
            resource_scope = _toml_string(profile.resource_scope.canonical_name)
            lines.append(f"resource_scope = {resource_scope}")
        if profile.adc_quota_project is not None:
            lines.append(
                "adc_quota_project = "
                + _toml_string(profile.adc_quota_project.canonical_name)
            )
        if profile.quota_contact_keyring_reference is not None:
            lines.append(
                "quota_contact_keyring_reference = "
                + _toml_string(profile.quota_contact_keyring_reference.canonical_name)
            )
        lines.extend(("", f"[profiles.{profile_name}.interface]"))
        lines.extend(_render_interface(profile.interface))
    return "\n".join((*lines, ""))


def _render_selection(state: SelectionState) -> str:
    lines = [f"schema = {_toml_string(SELECTION_STATE_SCHEMA)}"]
    if state.selected_profile is not None:
        lines.append(f"selected_profile = {_toml_string(state.selected_profile)}")
    if state.direct_resource_scope is not None:
        lines.append(
            "direct_resource_scope = "
            + _toml_string(state.direct_resource_scope.canonical_name)
        )
    return "\n".join((*lines, ""))


def _lock_stream(stream: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows matrix
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_stream(stream: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows matrix
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        path.chmod(0o600)
        _lock_stream(stream)
        try:
            yield
        finally:
            _unlock_stream(stream)


def _cleanup_temporary_files(path: Path) -> None:
    for candidate in path.parent.glob(f".{path.name}.*.tmp"):
        candidate.unlink(missing_ok=True)


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        temporary.chmod(0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name != "nt":  # pragma: no cover - platform-specific durability
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class TomlConfigRepository:
    """Validate and atomically update operator-owned TOML configuration."""

    def __init__(self, path: Path) -> None:
        """Bind the operator-owned configuration path and sibling lock."""
        self._path = path
        self._lock_path = path.with_name(f".{path.name}.lock")

    def _read_unlocked(self) -> ConfigSnapshot:
        return (
            _decode_config(_load(self._path))
            if self._path.exists()
            else ConfigSnapshot()
        )

    def _read(self) -> ConfigSnapshot:
        try:
            with _exclusive_lock(self._lock_path):
                _cleanup_temporary_files(self._path)
                return self._read_unlocked()
        except OSError as error:
            raise _operational_error(self._path, error) from error

    async def read(self) -> ConfigSnapshot:
        """Read a validated snapshot without blocking the application loop."""
        return await asyncio.to_thread(self._read)

    def _update(self, transform: ConfigTransform) -> ConfigSnapshot:
        try:
            with _exclusive_lock(self._lock_path):
                _cleanup_temporary_files(self._path)
                updated = transform(self._read_unlocked())
                if not isinstance(updated, ConfigSnapshot):
                    msg = "configuration transform must return ConfigSnapshot"
                    raise TypeError(msg)
                _atomic_write(self._path, _render_config(updated))
                return updated
        except OSError as error:
            raise _operational_error(self._path, error) from error

    async def update(self, transform: ConfigTransform) -> ConfigSnapshot:
        """Serialize one atomic update without blocking the application loop."""
        return await asyncio.to_thread(self._update, transform)


class TomlSelectionStateRepository:
    """Validate and atomically update independent mutable selection state."""

    def __init__(self, path: Path) -> None:
        """Bind the independent selection-state path and sibling lock."""
        self._path = path
        self._lock_path = path.with_name(f".{path.name}.lock")

    def _read_unlocked(self) -> SelectionState:
        return (
            _decode_selection(_load(self._path))
            if self._path.exists()
            else SelectionState()
        )

    def _read(self) -> SelectionState:
        try:
            with _exclusive_lock(self._lock_path):
                _cleanup_temporary_files(self._path)
                return self._read_unlocked()
        except OSError as error:
            raise _operational_error(self._path, error) from error

    async def read(self) -> SelectionState:
        """Read validated state without blocking the application loop."""
        return await asyncio.to_thread(self._read)

    def _update(self, transform: SelectionTransform) -> SelectionState:
        try:
            with _exclusive_lock(self._lock_path):
                _cleanup_temporary_files(self._path)
                updated = transform(self._read_unlocked())
                if not isinstance(updated, SelectionState):
                    msg = "selection transform must return SelectionState"
                    raise TypeError(msg)
                _atomic_write(self._path, _render_selection(updated))
                return updated
        except OSError as error:
            raise _operational_error(self._path, error) from error

    async def update(self, transform: SelectionTransform) -> SelectionState:
        """Serialize one atomic update without blocking the application loop."""
        return await asyncio.to_thread(self._update, transform)
