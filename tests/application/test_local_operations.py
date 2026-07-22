"""Offline local application-operation contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cqmgr.application.configuration import (
    ConfigSnapshot,
    InterfaceSettingKey,
    Profile,
    SelectionState,
)
from cqmgr.application.operations.local import LocalOperations
from cqmgr.application.ports.configuration import (
    ConfigurationRepositoryError,
    UnsupportedConfigurationSchemaError,
)
from cqmgr.domain.results import ExitClass
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import Callable


class MemoryRepository[SnapshotT]:
    """Scripted atomic repository at the public port seam."""

    def __init__(self, snapshot: SnapshotT) -> None:
        """Retain the initial snapshot and update count."""
        self.snapshot = snapshot
        self.update_count = 0

    def read(self) -> SnapshotT:
        """Return the current snapshot."""
        return self.snapshot

    def update(self, transform: Callable[[SnapshotT], SnapshotT]) -> SnapshotT:
        """Apply one transformation and retain the result."""
        self.snapshot = transform(self.snapshot)
        self.update_count += 1
        return self.snapshot


class FixedClock:
    """Deterministic application clock."""

    def now(self) -> datetime:
        """Return one UTC observation time."""
        return datetime(2026, 7, 21, 12, tzinfo=UTC)


def scope(kind: ResourceScopeKind, identifier: str) -> ResourceScope:
    """Build one canonical scope."""
    return ResourceScope(kind, f"{kind.value}s/{identifier}")


def operations(
    configuration: ConfigSnapshot | None = None,
    selection: SelectionState | None = None,
) -> tuple[
    LocalOperations,
    MemoryRepository[ConfigSnapshot],
    MemoryRepository[SelectionState],
]:
    """Build local operations with no provider, ADC, or keyring ports."""
    config_repository = MemoryRepository(configuration or ConfigSnapshot())
    selection_repository = MemoryRepository(selection or SelectionState())
    return (
        LocalOperations(config_repository, selection_repository, FixedClock()),
        config_repository,
        selection_repository,
    )


def test_scope_select_reports_canonical_project_and_direct_source() -> None:
    """A local project selection is visible in the surface-neutral result."""
    service, _, state = operations()

    result = service.scope_select(scope(ResourceScopeKind.PROJECT, "123"))

    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert result.resource_scope == scope(ResourceScopeKind.PROJECT, "123")
    assert result.data.resolution_source == "direct-selection"
    assert state.snapshot.direct_resource_scope == result.resource_scope


def test_scope_select_rejects_folder_before_updating_state() -> None:
    """A schema-reserved folder never triggers fallback or local mutation."""
    service, _, state = operations(
        selection=SelectionState(
            direct_resource_scope=scope(ResourceScopeKind.PROJECT, "123")
        )
    )

    result = service.scope_select(scope(ResourceScopeKind.FOLDER, "456"))

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.resource_scope == scope(ResourceScopeKind.FOLDER, "456")
    assert result.data.resolution_source == "explicit-input"
    assert state.update_count == 0


def test_scope_clear_reveals_selected_profile_project() -> None:
    """Clearing only direct state exposes the lower-precedence profile scope."""
    profile_project = scope(ResourceScopeKind.PROJECT, "789")
    service, _, state = operations(
        configuration=ConfigSnapshot(
            profiles=(Profile(name="primary", resource_scope=profile_project),)
        ),
        selection=SelectionState(
            selected_profile="primary",
            direct_resource_scope=scope(ResourceScopeKind.PROJECT, "123"),
        ),
    )

    result = service.scope_clear()

    assert result.resource_scope == profile_project
    assert result.data.resolution_source == "selected-profile"
    assert state.snapshot.direct_resource_scope is None


def test_unknown_profile_selection_is_rejected_without_state_write() -> None:
    """An explicit profile lookup miss is not reclassified as ambient fallback."""
    service, _, state = operations()

    result = service.profile_select("missing")

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert state.update_count == 0


def test_config_set_changes_only_validated_interface_key() -> None:
    """Configuration mutation preserves profiles and reports the exact setting."""
    configured_profile = Profile(name="primary")
    service, configuration, _ = operations(
        configuration=ConfigSnapshot(profiles=(configured_profile,))
    )

    result = service.config_set(InterfaceSettingKey.NERD_FONT, value=True)

    assert result.outcome.exit_class is ExitClass.SUCCESS
    assert result.data.key == "interface.nerd-font"
    assert result.data.value is True
    assert configuration.snapshot.profiles == (configured_profile,)
    assert configuration.snapshot.interface.nerd_font is True


def test_repository_failures_have_closed_exit_classification() -> None:
    """Newer schema is a precondition; corrupt local state is operational failure."""
    service, _, _ = operations()

    newer = service.repository_failure(
        "config.get",
        UnsupportedConfigurationSchemaError("cqmgr.config/v2"),
    )
    invalid = service.repository_failure(
        "scope.show",
        ConfigurationRepositoryError("invalid TOML"),
    )

    assert newer.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert newer.outcome.code.value == "unsupported-configuration-schema"
    assert invalid.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert invalid.outcome.code.value == "invalid-local-state"
