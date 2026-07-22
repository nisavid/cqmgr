"""Offline configuration, profile, and resource-scope operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cqmgr.application.configuration import (
    ConfigSnapshot,
    ConfigurationError,
    InterfaceSettingKey,
    Profile,
    ScopeResolution,
    ScopeResolutionSource,
    SelectionState,
    UnsupportedResourceScopeError,
    resolve_resource_scope,
)
from cqmgr.application.ports.configuration import (
    ConfigurationRepositoryError,
    UnsupportedConfigurationSchemaError,
)
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from cqmgr.application.ports.clock import Clock
    from cqmgr.application.ports.configuration import (
        ConfigRepository,
        SelectionStateRepository,
    )


@dataclass(frozen=True, slots=True)
class ScopeOperationData:
    """Visible local selection facts and their resolution source."""

    resolution_source: str | None
    selected_profile: str | None
    direct_resource_scope: ResourceScope | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileOperationData:
    """One profile result or a stable ordered profile listing."""

    profile: Profile | None
    profiles: tuple[Profile, ...]
    selected_profile: str | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigOperationData:
    """One validated interface configuration setting."""

    key: str
    value: bool


@dataclass(frozen=True, slots=True)
class RepositoryFailureData:
    """Safe local repository failure detail."""

    reason: str


class LocalOperations:
    """Run local operations using only config, selection, and clock ports."""

    def __init__(
        self,
        configuration: ConfigRepository,
        selection: SelectionStateRepository,
        clock: Clock,
    ) -> None:
        """Inject the complete offline dependency set."""
        self._configuration = configuration
        self._selection = selection
        self._clock = clock

    def _result[DataT](  # noqa: PLR0913
        self,
        *,
        operation: str,
        resource_scope: ResourceScope | None,
        boundary: str,
        reached: bool,
        outcome: str,
        exit_class: ExitClass,
        data: DataT,
    ) -> OperationResult[DataT]:
        started_at = self._clock.now()
        finished_at = self._clock.now()
        return OperationResult(
            operation=OperationName(operation),
            resource_scope=resource_scope,
            boundary=OperationBoundary(StableSymbol(boundary), reached),
            outcome=Outcome(StableSymbol(outcome), exit_class),
            completeness=Completeness.complete(),
            started_at=started_at,
            finished_at=finished_at,
            data=data,
        )

    def _scope_data(
        self,
        selection: SelectionState,
        resolution: ScopeResolution | None,
        *,
        reason: str | None = None,
        source: ScopeResolutionSource | None = None,
    ) -> ScopeOperationData:
        return ScopeOperationData(
            resolution_source=(
                resolution.source.value
                if resolution is not None
                else source.value
                if source is not None
                else None
            ),
            selected_profile=selection.selected_profile,
            direct_resource_scope=selection.direct_resource_scope,
            reason=reason,
        )

    def repository_failure(
        self,
        operation: str,
        error: ConfigurationRepositoryError,
    ) -> OperationResult[RepositoryFailureData]:
        """Classify unsupported newer state separately from corrupt local state."""
        unsupported = isinstance(error, UnsupportedConfigurationSchemaError)
        return self._result(
            operation=operation,
            resource_scope=None,
            boundary="local-state-valid",
            reached=False,
            outcome=(
                "unsupported-configuration-schema"
                if unsupported
                else "invalid-local-state"
            ),
            exit_class=(
                ExitClass.REJECTED_PRECONDITION
                if unsupported
                else ExitClass.OPERATIONAL_FAILURE
            ),
            data=RepositoryFailureData(reason=str(error)),
        )

    def scope_show(self) -> OperationResult[ScopeOperationData]:
        """Inspect the resolved project and exact local resolution source."""
        configuration = self._configuration.read()
        selection = self._selection.read()
        try:
            resolution = resolve_resource_scope(configuration, selection)
        except ConfigurationError as error:
            return self._result(
                operation="scope.show",
                resource_scope=None,
                boundary="resource-scope-resolved",
                reached=False,
                outcome="resource-scope-unavailable",
                exit_class=ExitClass.REJECTED_PRECONDITION,
                data=self._scope_data(selection, None, reason=str(error)),
            )
        return self._result(
            operation="scope.show",
            resource_scope=resolution.resource_scope,
            boundary="resource-scope-resolved",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=self._scope_data(selection, resolution),
        )

    def scope_select(
        self,
        resource_scope: ResourceScope,
    ) -> OperationResult[ScopeOperationData]:
        """Select one explicit V1 project without ambient inference."""
        configuration = self._configuration.read()
        selection = self._selection.read()
        try:
            resolution = resolve_resource_scope(
                configuration,
                selection,
                explicit_resource_scope=resource_scope,
            )
        except UnsupportedResourceScopeError as error:
            return self._result(
                operation="scope.select",
                resource_scope=resource_scope,
                boundary="local-selection-updated",
                reached=False,
                outcome="unsupported-resource-scope",
                exit_class=ExitClass.REJECTED_PRECONDITION,
                data=self._scope_data(
                    selection,
                    None,
                    reason=str(error),
                    source=ScopeResolutionSource.EXPLICIT_INPUT,
                ),
            )
        updated = self._selection.update(
            lambda state: SelectionState(
                selected_profile=state.selected_profile,
                direct_resource_scope=resolution.resource_scope,
            )
        )
        selected_resolution = ScopeResolution(
            resolution.resource_scope,
            ScopeResolutionSource.DIRECT_SELECTION,
        )
        return self._result(
            operation="scope.select",
            resource_scope=resolution.resource_scope,
            boundary="local-selection-updated",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=self._scope_data(updated, selected_resolution),
        )

    def scope_clear(self) -> OperationResult[ScopeOperationData]:
        """Clear only direct scope state and reveal selected-profile scope."""
        configuration = self._configuration.read()
        updated = self._selection.update(
            lambda state: SelectionState(selected_profile=state.selected_profile)
        )
        try:
            resolution = resolve_resource_scope(configuration, updated)
        except ConfigurationError as error:
            return self._result(
                operation="scope.clear",
                resource_scope=None,
                boundary="local-selection-cleared",
                reached=True,
                outcome="succeeded",
                exit_class=ExitClass.SUCCESS,
                data=self._scope_data(updated, None, reason=str(error)),
            )
        return self._result(
            operation="scope.clear",
            resource_scope=resolution.resource_scope,
            boundary="local-selection-cleared",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=self._scope_data(updated, resolution),
        )

    def profile_list(self) -> OperationResult[ProfileOperationData]:
        """List validated profiles in stable name order."""
        configuration = self._configuration.read()
        selection = self._selection.read()
        data = ProfileOperationData(
            profile=None,
            profiles=tuple(sorted(configuration.profiles, key=lambda item: item.name)),
            selected_profile=selection.selected_profile,
        )
        return self._result(
            operation="profile.list",
            resource_scope=None,
            boundary="local-configuration-read",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=data,
        )

    def profile_get(self, name: str) -> OperationResult[ProfileOperationData]:
        """Inspect one explicitly named validated profile."""
        configuration = self._configuration.read()
        selection = self._selection.read()
        try:
            profile = configuration.profile(name)
        except ConfigurationError as error:
            return self._result(
                operation="profile.get",
                resource_scope=None,
                boundary="local-configuration-read",
                reached=False,
                outcome="unknown-profile",
                exit_class=ExitClass.REJECTED_PRECONDITION,
                data=ProfileOperationData(
                    profile=None,
                    profiles=(),
                    selected_profile=selection.selected_profile,
                    reason=str(error),
                ),
            )
        return self._result(
            operation="profile.get",
            resource_scope=profile.resource_scope,
            boundary="local-configuration-read",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=ProfileOperationData(
                profile=profile,
                profiles=(),
                selected_profile=selection.selected_profile,
            ),
        )

    def profile_select(self, name: str) -> OperationResult[ProfileOperationData]:
        """Select one existing profile without changing direct scope state."""
        configuration = self._configuration.read()
        selection = self._selection.read()
        try:
            profile = configuration.profile(name)
        except ConfigurationError as error:
            return self._result(
                operation="profile.select",
                resource_scope=None,
                boundary="local-selection-updated",
                reached=False,
                outcome="unknown-profile",
                exit_class=ExitClass.REJECTED_PRECONDITION,
                data=ProfileOperationData(
                    profile=None,
                    profiles=(),
                    selected_profile=selection.selected_profile,
                    reason=str(error),
                ),
            )
        if (
            profile.resource_scope is not None
            and profile.resource_scope.kind is not ResourceScopeKind.PROJECT
        ):
            reason = (
                f"{profile.resource_scope.kind.value} resource scopes are reserved "
                "but unsupported in V1"
            )
            return self._result(
                operation="profile.select",
                resource_scope=profile.resource_scope,
                boundary="local-selection-updated",
                reached=False,
                outcome="unsupported-resource-scope",
                exit_class=ExitClass.REJECTED_PRECONDITION,
                data=ProfileOperationData(
                    profile=profile,
                    profiles=(),
                    selected_profile=selection.selected_profile,
                    reason=reason,
                ),
            )
        updated = self._selection.update(
            lambda state: SelectionState(
                selected_profile=name,
                direct_resource_scope=state.direct_resource_scope,
            )
        )
        return self._result(
            operation="profile.select",
            resource_scope=profile.resource_scope,
            boundary="local-selection-updated",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=ProfileOperationData(
                profile=profile,
                profiles=(),
                selected_profile=updated.selected_profile,
            ),
        )

    def config_get(
        self,
        key: InterfaceSettingKey,
    ) -> OperationResult[ConfigOperationData]:
        """Inspect one validated interface configuration key."""
        value = self._configuration.read().interface.get(key)
        return self._result(
            operation="config.get",
            resource_scope=None,
            boundary="local-configuration-read",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=ConfigOperationData(key.value, value),
        )

    def config_set(
        self,
        key: InterfaceSettingKey,
        *,
        value: bool,
    ) -> OperationResult[ConfigOperationData]:
        """Atomically change one validated interface setting."""
        updated = self._configuration.update(
            lambda snapshot: ConfigSnapshot(
                profiles=snapshot.profiles,
                interface=snapshot.interface.replace(key, value=value),
            )
        )
        return self._result(
            operation="config.set",
            resource_scope=None,
            boundary="local-configuration-updated",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=ConfigOperationData(key.value, updated.interface.get(key)),
        )
