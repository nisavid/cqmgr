"""Import-safe invocation classification and composition root."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cqmgr.application.operations.local import LocalOperations

_LOCAL_GROUPS = frozenset(
    ("scope", "sco", "profile", "pro", "config", "con", "audit", "aud")
)
_PROVIDER_GROUPS = frozenset(
    (
        "quota",
        "quo",
        "obtainability",
        "obt",
        "request",
        "req",
        "plan",
        "pla",
    )
)


class InvocationKind(StrEnum):
    """Startup dependency class selected before optional runtime imports."""

    HELP = "help"
    LOCAL = "local"
    TUI = "tui"
    PROVIDER = "provider"
    INVALID = "invalid"


def classify_invocation(
    arguments: Sequence[str],
    *,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> InvocationKind:
    """Classify raw argv without importing Textual, ADC, providers, or keyring."""
    if any(argument == "--help" for argument in arguments) or (
        arguments and arguments[0] == "--version"
    ):
        return InvocationKind.HELP
    if not arguments:
        return (
            InvocationKind.TUI
            if stdin_is_tty and stdout_is_tty
            else InvocationKind.HELP
        )
    command = arguments[0]
    if command == "tui":
        return InvocationKind.TUI
    if command in _LOCAL_GROUPS:
        return InvocationKind.LOCAL
    if command in _PROVIDER_GROUPS:
        return InvocationKind.PROVIDER
    return InvocationKind.INVALID


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Explicit platform-native paths for config and independent selection."""

    configuration: Path
    selection_state: Path


def _platform_paths(environment: Mapping[str, str]) -> RuntimePaths:
    home = Path.home()
    if sys.platform == "win32":
        config_home = Path(environment.get("APPDATA", home / "AppData/Roaming"))
        state_home = Path(environment.get("LOCALAPPDATA", home / "AppData/Local"))
    elif sys.platform == "darwin":
        config_home = home / "Library/Application Support"
        state_home = config_home
    else:
        config_home = Path(environment.get("XDG_CONFIG_HOME", home / ".config"))
        state_home = Path(environment.get("XDG_STATE_HOME", home / ".local/state"))
    return RuntimePaths(
        configuration=config_home / "cqmgr/config.toml",
        selection_state=state_home / "cqmgr/selection.toml",
    )


def runtime_paths(environment: Mapping[str, str] | None = None) -> RuntimePaths:
    """Resolve only cqmgr path overrides and platform-native defaults."""
    source = os.environ if environment is None else environment
    defaults = _platform_paths(source)
    return RuntimePaths(
        configuration=Path(
            source.get("CQMGR_CONFIG_PATH", str(defaults.configuration))
        ).expanduser(),
        selection_state=Path(
            source.get("CQMGR_SELECTION_STATE_PATH", str(defaults.selection_state))
        ).expanduser(),
    )


def build_local_operations(
    environment: Mapping[str, str] | None = None,
) -> LocalOperations:
    """Compose the complete local-only application without optional integrations."""
    from cqmgr.adapters.clock import SystemClock  # noqa: PLC0415
    from cqmgr.adapters.persistence.configuration import (  # noqa: PLC0415
        TomlConfigRepository,
        TomlSelectionStateRepository,
    )
    from cqmgr.application.operations.local import LocalOperations  # noqa: PLC0415

    paths = runtime_paths(environment)
    return LocalOperations(
        TomlConfigRepository(paths.configuration),
        TomlSelectionStateRepository(paths.selection_state),
        SystemClock(),
    )
