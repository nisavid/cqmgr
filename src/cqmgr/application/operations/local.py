"""Offline configuration, profile, and resource-scope operations."""

from __future__ import annotations

import asyncio
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
    ConfigurationRepositoryOperationalError,
    UnsupportedConfigurationSchemaError,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import (
    Completeness,
    EvidenceGap,
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
    identity_evidence: str = "deferred-offline"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileOperationData:
    """One profile result or a stable ordered profile listing."""

    profile: Profile | None
    profiles: tuple[Profile, ...]
    selected_profile: str | None
    resolution_source: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigOperationData:
    """One validated interface configuration setting."""

    key: str
    value: bool


@dataclass(frozen=True, slots=True)
class RepositoryFailureData:
    """Safe local repository failure detail."""

    guidance: str


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

    async def _read_local_state(self) -> tuple[ConfigSnapshot, SelectionState]:
        """Read independent configuration and selection snapshots concurrently."""
        configuration, selection = await asyncio.gather(
            self._configuration.read(),
            self._selection.read(),
        )
        return configuration, selection

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
        completeness: Completeness | None = None,
        diagnostics: tuple[Diagnostic, ...] = (),
    ) -> OperationResult[DataT]:
        started_at = self._clock.now()
        finished_at = self._clock.now()
        return OperationResult(
            operation=OperationName(operation),
            resource_scope=resource_scope,
            boundary=OperationBoundary(StableSymbol(boundary), reached),
            outcome=Outcome(StableSymbol(outcome), exit_class),
            completeness=completeness or Completeness.complete(),
            started_at=started_at,
            finished_at=finished_at,
            data=data,
            diagnostics=diagnostics,
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

    async def repository_failure(
        self,
        operation: str,
        error: ConfigurationRepositoryError,
    ) -> OperationResult[RepositoryFailureData]:
        """Classify unsupported newer state separately from corrupt local state."""
        unsupported = isinstance(error, UnsupportedConfigurationSchemaError)
        operational = isinstance(error, ConfigurationRepositoryOperationalError)
        outcome = (
            "unsupported-configuration-schema"
            if unsupported
            else "local-state-unavailable"
            if operational
            else "invalid-local-state"
        )
        guidance = (
            "Upgrade cqmgr to a version that supports this local-state schema, "
            "then retry."
            if unsupported
            else (
                "Check cqmgr local-state file permissions and storage availability, "
                "then retry."
                if operational
                else "Repair or restore the cqmgr local-state file, then retry."
            )
        )
        diagnostic = Diagnostic(
            code=DiagnosticCode(outcome),
            severity=Severity.ERROR,
            phase=DiagnosticPhase("local-state-read"),
            source=DiagnosticSource("local-state"),
            retry=(
                RetryDisposition.AFTER_UPGRADE
                if unsupported
                else RetryDisposition.AFTER_REFRESH
            ),
            message=RedactedText(guidance),
        )
        return self._result(
            operation=operation,
            resource_scope=None,
            boundary="local-state-valid",
            reached=False,
            outcome=outcome,
            exit_class=(
                ExitClass.REJECTED_PRECONDITION
                if unsupported
                else ExitClass.OPERATIONAL_FAILURE
            ),
            data=RepositoryFailureData(guidance=guidance),
            completeness=Completeness.unavailable(
                EvidenceGap(StableSymbol("local-state"), StableSymbol(outcome))
            ),
            diagnostics=(diagnostic,),
        )

    async def scope_show(self) -> OperationResult[ScopeOperationData]:
        """Inspect the resolved project and exact local resolution source."""
        configuration, selection = await self._read_local_state()
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

    async def scope_select(
        self,
        resource_scope: ResourceScope,
    ) -> OperationResult[ScopeOperationData]:
        """Select one explicit V1 project without ambient inference."""
        configuration, selection = await self._read_local_state()
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
        updated = await self._selection.update(
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

    async def scope_selection_usage_failure(
        self,
        reason: str,
    ) -> OperationResult[ScopeOperationData]:
        """Return one typed usage result after the leaf invocation was decoded."""
        return self._result(
            operation="scope.select",
            resource_scope=None,
            boundary="resource-scope-valid",
            reached=False,
            outcome="invalid-resource-scope",
            exit_class=ExitClass.USAGE,
            data=self._scope_data(SelectionState(), None, reason=reason),
        )

    async def scope_clear(self) -> OperationResult[ScopeOperationData]:
        """Clear only direct scope state and reveal selected-profile scope."""
        configuration = await self._configuration.read()
        updated = await self._selection.update(
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

    async def profile_list(self) -> OperationResult[ProfileOperationData]:
        """List validated profiles in stable name order."""
        configuration, selection = await self._read_local_state()
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

    async def profile_get(self, name: str) -> OperationResult[ProfileOperationData]:
        """Inspect one explicitly named validated profile."""
        configuration, selection = await self._read_local_state()
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

    async def profile_select(self, name: str) -> OperationResult[ProfileOperationData]:
        """Select one existing profile without changing direct scope state."""
        configuration, selection = await self._read_local_state()
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
        updated = await self._selection.update(
            lambda state: SelectionState(
                selected_profile=name,
                direct_resource_scope=state.direct_resource_scope,
            )
        )
        try:
            effective_resolution = resolve_resource_scope(configuration, updated)
        except ConfigurationError:
            effective_resolution = None
        return self._result(
            operation="profile.select",
            resource_scope=(
                effective_resolution.resource_scope
                if effective_resolution is not None
                else None
            ),
            boundary="local-selection-updated",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=ProfileOperationData(
                profile=profile,
                profiles=(),
                selected_profile=updated.selected_profile,
                resolution_source=(
                    effective_resolution.source.value
                    if effective_resolution is not None
                    else None
                ),
            ),
        )

    async def config_get(
        self,
        key: InterfaceSettingKey,
    ) -> OperationResult[ConfigOperationData]:
        """Inspect one validated interface configuration key."""
        value = (await self._configuration.read()).interface.get(key)
        return self._result(
            operation="config.get",
            resource_scope=None,
            boundary="local-configuration-read",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            data=ConfigOperationData(key.value, value),
        )

    async def config_set(
        self,
        key: InterfaceSettingKey,
        *,
        value: bool,
    ) -> OperationResult[ConfigOperationData]:
        """Atomically change one validated interface setting."""
        updated = await self._configuration.update(
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
