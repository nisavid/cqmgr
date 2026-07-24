"""Provider-scoped read-only orchestration over local scope and ADC evidence."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from cqmgr.application.configuration import (
    ConfigSnapshot,
    ConfigurationError,
    ProfileResourceScopeError,
    ResourceScopeUnavailableError,
    SelectionState,
    UnknownProfileError,
    UnsupportedResourceScopeError,
    resolve_resource_scope,
)
from cqmgr.application.operations.obtainability import (
    ObtainabilityCompareRequest,
    PreparedObtainabilityComparison,
    candidates_from_resolved_workload,
    prepare_obtainability_comparison,
    product_coverage_from_resolved_workload,
)
from cqmgr.application.operations.quotas import (
    MAX_BROWSE_LIMIT,
    QuotaBrowseRequest,
    QuotaInspectRequest,
    QuotaResolveRequest,
)
from cqmgr.application.ports.configuration import (
    ConfigurationRepositoryError,
    ConfigurationRepositoryOperationalError,
    UnsupportedConfigurationSchemaError,
)
from cqmgr.application.ports.coordination import (
    BudgetCommitUnknownError,
    BudgetRequest,
    CancellationToken,
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
    CoordinationUnavailableError,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.accelerator_overlay import (
    CandidateLocations,
    ComputeInstanceRequirement,
    ProvisioningModel,
    ResolvedWorkloadRequirement,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.identity import ADCQuotaProject, ProviderIdentityEvidence
from cqmgr.domain.obtainability import ObtainabilityComparison
from cqmgr.domain.projects import ProjectReference
from cqmgr.domain.quota_queries import (
    ProviderSourceCoverage,
    QuotaQuery,
    QuotaQueryFilters,
    QuotaQueryItem,
    QuotaSort,
)
from cqmgr.domain.quotas import NormalizedDimensions
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
from cqmgr.domain.scopes import ResourceScope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cqmgr.application.operations.obtainability import ObtainabilityOperations
    from cqmgr.application.operations.quotas import (
        QuotaBrowseData,
        QuotaInspectData,
        QuotaOperations,
        WorkloadResolutionOperations,
    )
    from cqmgr.application.ports.clock import Clock
    from cqmgr.application.ports.configuration import (
        ConfigRepository,
        SelectionStateRepository,
    )
    from cqmgr.application.ports.coordination import BudgetCoordinator
    from cqmgr.application.ports.identity import IdentityProvider, ProjectResolver
    from cqmgr.domain.accelerator_overlay import (
        CloudTpuSliceRequirement,
    )
    from cqmgr.domain.obtainability import (
        DistributionShape,
        ObtainabilityCandidate,
        SpotMachineConfiguration,
    )
    from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity


@dataclass(frozen=True, slots=True)
class ReadOnlyScopeInput:
    """Explicit resource-scope and profile inputs above local selection state."""

    explicit_resource_scope: ResourceScope | None = None
    explicit_profile: str | None = None

    def __post_init__(self) -> None:
        """Reject untyped or empty explicit selection inputs."""
        if self.explicit_resource_scope is not None and not isinstance(
            self.explicit_resource_scope, ResourceScope
        ):
            msg = "explicit_resource_scope must be a ResourceScope or None"
            raise TypeError(msg)
        if self.explicit_profile is not None and (
            not isinstance(self.explicit_profile, str) or not self.explicit_profile
        ):
            msg = "explicit_profile must be non-empty text or None"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ReadOnlyQuotaQuery:
    """A typed quota query whose resource scope is resolved by the facade."""

    filters: QuotaQueryFilters = field(default_factory=QuotaQueryFilters)
    sort: tuple[QuotaSort, ...] = ()

    def __post_init__(self) -> None:
        """Require the existing typed filter and sort contracts."""
        if not isinstance(self.filters, QuotaQueryFilters):
            msg = "read-only query filters must use QuotaQueryFilters"
            raise TypeError(msg)
        if not isinstance(self.sort, tuple) or any(
            not isinstance(item, QuotaSort) for item in self.sort
        ):
            msg = "read-only query sort must contain QuotaSort values"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class QuotaInspectSelector:
    """Exact public selector without a caller-invented quota scope."""

    service: str
    quota_id: str
    location: str
    dimensions: NormalizedDimensions = field(default_factory=NormalizedDimensions)

    def __post_init__(self) -> None:
        """Validate only caller-owned selector fields."""
        if not isinstance(self.quota_id, str) or not self.quota_id:
            msg = "quota inspect selector requires a quota ID"
            raise ValueError(msg)
        if not isinstance(self.dimensions, NormalizedDimensions):
            msg = "quota inspect dimensions must use NormalizedDimensions"
            raise TypeError(msg)
        # Reuse the public query grammar for canonical service and location values.
        filters = QuotaQueryFilters(
            services=(self.service,),
            locations=(self.location,),
        )
        object.__setattr__(self, "service", filters.services[0])


@dataclass(frozen=True, slots=True)
class ReadOnlyFailureData:
    """One stable provider-setup or selector failure reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class IncompleteQuotaInspectData:
    """Usable selector matches retained from one incomplete inventory read."""

    selector: QuotaInspectSelector
    matching_items: tuple[QuotaQueryItem, ...]
    source_coverage: tuple[ProviderSourceCoverage, ...]
    reason: str

    def __post_init__(self) -> None:
        """Keep only typed evidence that actually matches the public selector."""
        if not isinstance(self.selector, QuotaInspectSelector):
            msg = "incomplete inspect selector must use QuotaInspectSelector"
            raise TypeError(msg)
        if not isinstance(self.matching_items, tuple) or any(
            not isinstance(item, QuotaQueryItem)
            or not _matches_selector(item, self.selector)
            for item in self.matching_items
        ):
            msg = "incomplete inspect matching_items must match the selector"
            raise TypeError(msg)
        if not isinstance(self.source_coverage, tuple) or any(
            not isinstance(item, ProviderSourceCoverage)
            for item in self.source_coverage
        ):
            msg = "incomplete inspect source_coverage must be provider coverage"
            raise TypeError(msg)
        if not isinstance(self.reason, str) or not self.reason:
            msg = "incomplete inspect reason must be non-empty"
            raise ValueError(msg)


_DEFAULT_SCOPE_INPUT = ReadOnlyScopeInput()
_AMBIGUOUS_MATCH_COUNT = 2


class ReadOnlyOperations:
    """Resolve provider context once, then delegate to read-only operations."""

    def __init__(  # noqa: PLR0913
        self,
        configuration: ConfigRepository,
        selection: SelectionStateRepository,
        projects: ProjectResolver,
        identity: IdentityProvider,
        quotas: QuotaOperations,
        workloads: WorkloadResolutionOperations,
        clock: Clock,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        shutdown: Callable[[], Awaitable[None]] | None = None,
        budget: BudgetCoordinator | None = None,
        obtainability: ObtainabilityOperations | None = None,
    ) -> None:
        """Inject local state, provider setup, and existing read operations."""
        self._configuration = configuration
        self._selection = selection
        self._projects = projects
        self._identity = identity
        self._quotas = quotas
        self._workloads = workloads
        self._clock = clock
        self._monotonic = monotonic
        self._shutdown = shutdown
        self._budget = budget
        self._obtainability = obtainability

    async def aclose(self) -> None:
        """Release invocation-scoped provider clients once."""
        shutdown = self._shutdown
        self._shutdown = None
        if shutdown is not None:
            await shutdown()

    async def browse(  # noqa: PLR0913 - complete provider invocation contract
        self,
        query: ReadOnlyQuotaQuery | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaBrowseData | ReadOnlyFailureData]:
        """Run an initial provider query or resume a local bound snapshot."""
        if cursor is not None and query is None:
            return await self._quotas.browse(
                QuotaBrowseRequest(cursor=cursor, limit=limit)
            )
        if query is None:
            return self._failure(
                operation="quota.list",
                boundary="logical-page-read",
                outcome="query-required",
                exit_class=ExitClass.USAGE,
                resource_scope=None,
            )
        if cursor is not None:
            local = await self._local_scope(
                operation="quota.list",
                boundary="logical-page-read",
                scope_input=scope_input,
            )
            if isinstance(local, OperationResult):
                return local
            configuration, selection, resource_scope = local
            del configuration, selection
            return await self._quotas.browse(
                QuotaBrowseRequest(
                    query=QuotaQuery(resource_scope, query.filters, query.sort),
                    cursor=cursor,
                    limit=limit,
                )
            )
        prepared = await self._provider_context(
            operation="quota.list",
            boundary="logical-page-read",
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if isinstance(prepared, OperationResult):
            return prepared
        context, _ = prepared
        delegated = await self._quotas.browse(
            QuotaBrowseRequest(
                context=context,
                query=QuotaQuery(
                    context.project.resource_scope,
                    query.filters,
                    query.sort,
                ),
                limit=limit,
            )
        )
        return _with_provider_identity(delegated, context)

    async def compare_obtainability_all_compatible(  # noqa: PLR0913
        self,
        requirement: ComputeInstanceRequirement,
        *,
        machine: SpotMachineConfiguration,
        distribution_shape: DistributionShape,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison | ReadOnlyFailureData]:
        """Resolve all compatible locations, then compare the exact expansion."""
        prepared = await self._provider_context(
            operation="obtainability.compare",
            boundary="spot-advice-assessed",
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if isinstance(prepared, OperationResult):
            return prepared
        context, _ = prepared
        operations = self._obtainability
        if operations is None:
            return _with_provider_identity(
                self._failure(
                    operation="obtainability.compare",
                    boundary="spot-advice-assessed",
                    outcome="spot-advice-unavailable",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                    resource_scope=context.project.resource_scope,
                ),
                context,
            )
        resolved_result = await self._workloads.resolve(
            QuotaResolveRequest(context, requirement)
        )
        resolved = resolved_result.data
        if not isinstance(resolved, ResolvedWorkloadRequirement):
            return _with_provider_identity(
                self._failure(
                    operation="obtainability.compare",
                    boundary="spot-advice-assessed",
                    outcome="spot-advice-resolution-failed",
                    exit_class=resolved_result.outcome.exit_class,
                    resource_scope=context.project.resource_scope,
                    completeness=resolved_result.completeness,
                    diagnostics=resolved_result.diagnostics,
                    reason="workload resolution did not produce compatible evidence",
                ),
                context,
            )
        try:
            candidates = candidates_from_resolved_workload(
                resolved,
                machine=machine,
                distribution_shape=distribution_shape,
            )
        except ValueError:
            return _with_provider_identity(
                OperationResult(
                    operation=OperationName("obtainability.compare"),
                    resource_scope=context.project.resource_scope,
                    boundary=OperationBoundary(
                        StableSymbol("spot-advice-assessed"),
                        reached=False,
                    ),
                    outcome=Outcome(
                        StableSymbol("spot-advice-no-compatible-locations"),
                        ExitClass.REJECTED_PRECONDITION,
                    ),
                    completeness=resolved_result.completeness,
                    started_at=resolved_result.started_at,
                    finished_at=resolved_result.finished_at,
                    data=ObtainabilityComparison(
                        (),
                        catalog_coverage=product_coverage_from_resolved_workload(
                            resolved,
                            machine,
                        ),
                        resolver_provenance=resolved,
                    ),
                    diagnostics=resolved_result.diagnostics,
                    provenance=resolved_result.provenance,
                ),
                context,
            )
        prepared_comparison = prepare_obtainability_comparison(
            resolved,
            candidates,
        )
        delegated = await operations.compare(
            ObtainabilityCompareRequest(
                context,
                prepared_comparison.candidates,
                resolver_provenance=prepared_comparison.resolver_provenance,
            )
        )
        return _with_provider_identity(delegated, context)

    async def browse_usage_failure(
        self,
        reason: str,
    ) -> OperationResult[ReadOnlyFailureData]:
        """Return a surface-neutral quota-list parse or usage failure."""
        return self._usage_failure(
            "quota.list",
            "logical-page-read",
            "invalid-quota-query",
            reason,
        )

    async def inspect(
        self,
        selector: QuotaInspectSelector,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[
        IncompleteQuotaInspectData | QuotaInspectData | ReadOnlyFailureData
    ]:
        """Resolve a public selector to one provider-supplied exact slice identity."""
        prepared = await self._provider_context(
            operation="quota.inspect",
            boundary="exact-slice-inspected",
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if isinstance(prepared, OperationResult):
            return prepared
        context, identity_diagnostics = prepared
        query = QuotaQuery(
            context.project.resource_scope,
            QuotaQueryFilters(
                services=(selector.service,),
                locations=(selector.location,),
                text=selector.quota_id,
            ),
        )
        page = await self._quotas.browse(
            QuotaBrowseRequest(context=context, query=query, limit=MAX_BROWSE_LIMIT)
        )
        if page.outcome.exit_class is not ExitClass.SUCCESS:
            return self._inspect_from_browse_failure(
                page,
                selector=selector,
                identity_diagnostics=identity_diagnostics,
                identity_evidence=ProviderIdentityEvidence.from_adc(context.identity),
            )

        matching_items = [
            item for item in page.data.items if _matches_selector(item, selector)
        ]
        source_coverage = page.data.source_coverage
        seen_cursors: set[str] = set()
        while (
            page.data.next_cursor is not None
            and len(matching_items) < _AMBIGUOUS_MATCH_COUNT
        ):
            cursor = page.data.next_cursor
            if cursor in seen_cursors:
                return self._failure(
                    operation="quota.inspect",
                    boundary="exact-slice-inspected",
                    outcome="cursor-cycle",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                    resource_scope=context.project.resource_scope,
                    diagnostics=identity_diagnostics,
                    identity_evidence=ProviderIdentityEvidence.from_adc(
                        context.identity
                    ),
                )
            seen_cursors.add(cursor)
            page = await self._quotas.browse(
                QuotaBrowseRequest(cursor=cursor, limit=MAX_BROWSE_LIMIT)
            )
            if page.outcome.exit_class is not ExitClass.SUCCESS:
                return self._inspect_from_browse_failure(
                    page,
                    selector=selector,
                    identity_diagnostics=identity_diagnostics,
                    identity_evidence=ProviderIdentityEvidence.from_adc(
                        context.identity
                    ),
                    retained_items=tuple(matching_items),
                    retained_coverage=source_coverage,
                )
            matching_items.extend(
                item for item in page.data.items if _matches_selector(item, selector)
            )
            source_coverage = _merge_source_coverage(
                source_coverage,
                page.data.source_coverage,
            )

        if len(matching_items) != 1:
            return self._failure(
                operation="quota.inspect",
                boundary="exact-slice-inspected",
                outcome=(
                    "exact-slice-not-found"
                    if not matching_items
                    else "ambiguous-exact-slice-selector"
                ),
                exit_class=ExitClass.REJECTED_PRECONDITION,
                resource_scope=context.project.resource_scope,
                diagnostics=identity_diagnostics,
                identity_evidence=ProviderIdentityEvidence.from_adc(context.identity),
            )
        delegated = await self._quotas.inspect(
            QuotaInspectRequest(context, matching_items[0].identity)
        )
        return _with_provider_identity(delegated, context)

    async def inspect_usage_failure(
        self,
        reason: str,
    ) -> OperationResult[ReadOnlyFailureData]:
        """Return a surface-neutral quota-inspect parse or usage failure."""
        return self._usage_failure(
            "quota.inspect",
            "exact-slice-inspected",
            "invalid-quota-selector",
            reason,
        )

    async def resolve(
        self,
        requirement: ComputeInstanceRequirement | CloudTpuSliceRequirement,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ResolvedWorkloadRequirement | ReadOnlyFailureData | None]:
        """Resolve one typed workload against the canonical provider context."""
        prepared = await self._provider_context(
            operation="quota.resolve",
            boundary="workload-resolved",
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if isinstance(prepared, OperationResult):
            return prepared
        context, _ = prepared
        delegated = await self._workloads.resolve(
            QuotaResolveRequest(context, requirement)
        )
        return _with_provider_identity(delegated, context)

    async def resolve_usage_failure(
        self,
        reason: str,
    ) -> OperationResult[ReadOnlyFailureData]:
        """Return a surface-neutral workload-resolution parse or usage failure."""
        return self._usage_failure(
            "quota.resolve",
            "workload-resolved",
            "invalid-workload-requirement",
            reason,
        )

    async def compare_obtainability(
        self,
        candidates: tuple[ObtainabilityCandidate, ...],
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison | ReadOnlyFailureData]:
        """Resolve exact Spot candidates, then compare only eligible evidence."""
        prepared = await self._provider_context(
            operation="obtainability.compare",
            boundary="spot-advice-assessed",
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if isinstance(prepared, OperationResult):
            return prepared
        context, _ = prepared
        operations = self._obtainability
        if operations is None:
            return self._failure(
                operation="obtainability.compare",
                boundary="spot-advice-assessed",
                outcome="spot-advice-unavailable",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=context.project.resource_scope,
                identity_evidence=ProviderIdentityEvidence.from_adc(context.identity),
            )
        first = candidates[0]
        locations = tuple(
            dict.fromkeys(
                location
                for candidate in candidates
                for location in candidate.zones or (candidate.endpoint_region,)
            )
        )
        requirement = ComputeInstanceRequirement(
            first.machine.machine_type,
            first.vm_count,
            ProvisioningModel.SPOT,
            CandidateLocations(locations),
            attached_accelerator_type=(
                None
                if first.machine.gpu is None
                else first.machine.gpu.accelerator_type
            ),
            attached_accelerator_count=(
                None if first.machine.gpu is None else first.machine.gpu.count
            ),
        )
        resolved_result = await self._workloads.resolve(
            QuotaResolveRequest(context, requirement)
        )
        resolved = resolved_result.data
        if not isinstance(resolved, ResolvedWorkloadRequirement):
            return _with_provider_identity(
                self._failure(
                    operation="obtainability.compare",
                    boundary="spot-advice-assessed",
                    outcome="spot-advice-resolution-failed",
                    exit_class=resolved_result.outcome.exit_class,
                    resource_scope=context.project.resource_scope,
                    completeness=resolved_result.completeness,
                    diagnostics=resolved_result.diagnostics,
                    reason="workload resolution did not produce compatible evidence",
                ),
                context,
            )
        prepared_comparison = prepare_obtainability_comparison(
            resolved,
            candidates,
        )
        delegated = await operations.compare(
            ObtainabilityCompareRequest(
                context,
                prepared_comparison.candidates,
                resolver_provenance=prepared_comparison.resolver_provenance,
            )
        )
        return _with_provider_identity(delegated, context)

    async def compare_obtainability_prepared(
        self,
        prepared_comparison: PreparedObtainabilityComparison,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison | ReadOnlyFailureData]:
        """Compare one exact resolver-backed value without resolving it again."""
        if not isinstance(prepared_comparison, PreparedObtainabilityComparison):
            msg = "prepared obtainability comparison must be typed"
            raise TypeError(msg)
        prepared = await self._provider_context(
            operation="obtainability.compare",
            boundary="spot-advice-assessed",
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if isinstance(prepared, OperationResult):
            return prepared
        context, _ = prepared
        operations = self._obtainability
        if operations is None:
            return _with_provider_identity(
                self._failure(
                    operation="obtainability.compare",
                    boundary="spot-advice-assessed",
                    outcome="spot-advice-unavailable",
                    exit_class=ExitClass.OPERATIONAL_FAILURE,
                    resource_scope=context.project.resource_scope,
                ),
                context,
            )
        delegated = await operations.compare(
            ObtainabilityCompareRequest(
                context,
                prepared_comparison.candidates,
                resolver_provenance=prepared_comparison.resolver_provenance,
            )
        )
        return _with_provider_identity(delegated, context)

    async def compare_obtainability_usage_failure(
        self,
        reason: str,
    ) -> OperationResult[ReadOnlyFailureData]:
        """Return a surface-neutral obtainability parse or usage failure."""
        return self._usage_failure(
            "obtainability.compare",
            "spot-advice-assessed",
            "invalid-obtainability-request",
            reason,
        )

    async def _local_scope(
        self,
        *,
        operation: str,
        boundary: str,
        scope_input: ReadOnlyScopeInput,
    ) -> (
        tuple[ConfigSnapshot, SelectionState, ResourceScope]
        | OperationResult[ReadOnlyFailureData]
    ):
        try:
            configuration, selection = await asyncio.gather(
                self._configuration.read(),
                self._selection.read(),
            )
            resolution = resolve_resource_scope(
                configuration,
                selection,
                explicit_resource_scope=scope_input.explicit_resource_scope,
                explicit_profile=scope_input.explicit_profile,
            )
        except ConfigurationRepositoryError as error:
            return self._repository_failure(operation, boundary, error)
        except ConfigurationError as error:
            outcome = _configuration_outcome(error)
            return self._failure(
                operation=operation,
                boundary=boundary,
                outcome=outcome,
                exit_class=ExitClass.REJECTED_PRECONDITION,
                resource_scope=scope_input.explicit_resource_scope,
            )
        return configuration, selection, resolution.resource_scope

    async def _provider_context(  # noqa: C901, PLR0911 - typed setup gates return
        self,
        *,
        operation: str,
        boundary: str,
        deadline: float,
        cancellation: CancellationToken | None,
        scope_input: ReadOnlyScopeInput,
    ) -> (
        tuple[ProviderReadContext, tuple[Diagnostic, ...]]
        | OperationResult[ReadOnlyFailureData]
    ):
        setup_cancellation = cancellation or CancellationToken()
        try:
            local = await _await_provider_setup(
                lambda: self._local_scope(
                    operation=operation,
                    boundary=boundary,
                    scope_input=scope_input,
                ),
                deadline=deadline,
                cancellation=setup_cancellation,
                monotonic=self._monotonic,
            )
        except (
            CoordinationCancelledError,
            CoordinationDeadlineExceededError,
        ) as error:
            return self._setup_stopped(
                operation,
                boundary,
                scope_input.explicit_resource_scope,
                error,
            )
        if isinstance(local, OperationResult):
            return local
        configuration, selection, resource_scope = local
        try:
            adc_quota_project = _adc_quota_project(
                configuration,
                selection,
                scope_input,
            )
        except ConfigurationError as error:
            return self._failure(
                operation=operation,
                boundary=boundary,
                outcome=_configuration_outcome(error),
                exit_class=ExitClass.REJECTED_PRECONDITION,
                resource_scope=resource_scope,
            )
        try:
            identity = await _await_provider_setup(
                lambda: self._identity.resolve(
                    adc_quota_project=adc_quota_project,
                    timeout_seconds=_remaining_setup_timeout(
                        deadline,
                        self._monotonic,
                    ),
                ),
                deadline=deadline,
                cancellation=setup_cancellation,
                monotonic=self._monotonic,
            )
        except (
            CoordinationCancelledError,
            CoordinationDeadlineExceededError,
        ) as error:
            return self._setup_stopped(operation, boundary, resource_scope, error)
        if not identity.read_capability:
            outcome = identity.diagnostics[0].code.value
            return self._failure(
                operation=operation,
                boundary=boundary,
                outcome=outcome,
                exit_class=ExitClass.AUTHORIZATION,
                resource_scope=resource_scope,
                diagnostics=identity.diagnostics,
                unavailable_source="application-default-credentials",
                identity_evidence=ProviderIdentityEvidence.from_adc(identity),
            )
        reference = ProjectReference(resource_scope.canonical_name)
        try:
            if self._budget is not None:
                transport_identity = identity.transport_budget_identity
                await self._budget.acquire(
                    BudgetRequest(
                        provider="resource-manager",
                        project=resource_scope.canonical_name,
                        adc_quota_project=(
                            transport_identity.value
                            if transport_identity is not None
                            else None
                        ),
                    ),
                    deadline=deadline,
                    cancellation=setup_cancellation,
                )
            project_resolution = await _await_provider_setup(
                lambda: self._projects.resolve(reference),
                deadline=deadline,
                cancellation=setup_cancellation,
                monotonic=self._monotonic,
            )
        except (
            CoordinationCancelledError,
            CoordinationDeadlineExceededError,
        ) as error:
            return self._setup_stopped(
                operation,
                boundary,
                resource_scope,
                error,
                identity_evidence=ProviderIdentityEvidence.from_adc(identity),
            )
        except (BudgetCommitUnknownError, CoordinationUnavailableError):
            diagnostic = _diagnostic(
                "provider-read-budget-unavailable",
                "project-resolution",
                "resource-manager",
                "Local request-budget coordination is unavailable; retry later.",
            )
            return self._failure(
                operation=operation,
                boundary=boundary,
                outcome=diagnostic.code.value,
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                resource_scope=resource_scope,
                diagnostics=(*identity.diagnostics, diagnostic),
                unavailable_source="resource-manager",
                identity_evidence=ProviderIdentityEvidence.from_adc(identity),
            )
        if not project_resolution.succeeded:
            exit_class = _project_exit_class(project_resolution.diagnostics)
            outcome = project_resolution.diagnostics[0].code.value
            return self._failure(
                operation=operation,
                boundary=boundary,
                outcome=outcome,
                exit_class=exit_class,
                resource_scope=resource_scope,
                diagnostics=(*identity.diagnostics, *project_resolution.diagnostics),
                unavailable_source="resource-manager",
                identity_evidence=ProviderIdentityEvidence.from_adc(identity),
            )
        project = project_resolution.project
        if project is None:
            msg = "successful project resolution must contain canonical evidence"
            raise AssertionError(msg)
        context = ProviderReadContext(
            project=project,
            identity=identity,
            deadline=deadline,
            cancellation=setup_cancellation,
        )
        return context, identity.diagnostics

    def _setup_stopped(
        self,
        operation: str,
        boundary: str,
        resource_scope: ResourceScope | None,
        error: CoordinationCancelledError | CoordinationDeadlineExceededError,
        *,
        identity_evidence: ProviderIdentityEvidence | None = None,
    ) -> OperationResult[ReadOnlyFailureData]:
        interrupted = isinstance(error, CoordinationCancelledError)
        outcome = (
            "operation-interrupted" if interrupted else "operation-deadline-exceeded"
        )
        return self._failure(
            operation=operation,
            boundary=boundary,
            outcome=outcome,
            exit_class=ExitClass.INTERRUPTED if interrupted else ExitClass.TIMEOUT,
            resource_scope=resource_scope,
            unavailable_source="provider-setup",
            identity_evidence=identity_evidence,
        )

    def _repository_failure(
        self,
        operation: str,
        boundary: str,
        error: ConfigurationRepositoryError,
    ) -> OperationResult[ReadOnlyFailureData]:
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
            "Upgrade cqmgr to a compatible local-state schema, then retry."
            if unsupported
            else "Repair or restore cqmgr local state, then retry."
        )
        diagnostic = _diagnostic(
            outcome,
            "local-state-read",
            "local-state",
            guidance,
        )
        return self._failure(
            operation=operation,
            boundary=boundary,
            outcome=outcome,
            exit_class=(
                ExitClass.REJECTED_PRECONDITION
                if unsupported
                else ExitClass.OPERATIONAL_FAILURE
            ),
            resource_scope=None,
            diagnostics=(diagnostic,),
            unavailable_source="local-state",
        )

    def _usage_failure(
        self,
        operation: str,
        boundary: str,
        outcome: str,
        reason: str,
    ) -> OperationResult[ReadOnlyFailureData]:
        return self._failure(
            operation=operation,
            boundary=boundary,
            outcome=outcome,
            exit_class=ExitClass.USAGE,
            resource_scope=None,
            reason=reason,
        )

    def _inspect_from_browse_failure(  # noqa: PLR0913 - evidence stays explicit
        self,
        page: OperationResult[QuotaBrowseData],
        *,
        selector: QuotaInspectSelector,
        identity_diagnostics: tuple[Diagnostic, ...],
        identity_evidence: ProviderIdentityEvidence | None = None,
        retained_items: tuple[QuotaQueryItem, ...] = (),
        retained_coverage: tuple[ProviderSourceCoverage, ...] = (),
    ) -> OperationResult[IncompleteQuotaInspectData | ReadOnlyFailureData]:
        matching_items = _merge_matching_items(
            retained_items,
            tuple(
                item for item in page.data.items if _matches_selector(item, selector)
            ),
        )
        if page.completeness.has_partial_data or matching_items:
            outcome = (
                page.outcome
                if page.outcome.exit_class
                in (
                    ExitClass.INCOMPLETE_EVIDENCE,
                    ExitClass.TIMEOUT,
                    ExitClass.INTERRUPTED,
                )
                else Outcome(
                    StableSymbol("incomplete-evidence"),
                    ExitClass.INCOMPLETE_EVIDENCE,
                )
            )
            completeness = (
                page.completeness
                if page.completeness.has_partial_data
                else Completeness.incomplete(*page.completeness.gaps)
            )
            data = IncompleteQuotaInspectData(
                selector=selector,
                matching_items=matching_items,
                source_coverage=_merge_source_coverage(
                    retained_coverage,
                    page.data.source_coverage,
                ),
                reason=page.data.reason or page.outcome.code.value,
            )
            return OperationResult(
                operation=OperationName("quota.inspect"),
                resource_scope=page.resource_scope,
                boundary=OperationBoundary(
                    condition=StableSymbol("exact-slice-inspected"),
                    reached=False,
                ),
                outcome=outcome,
                completeness=completeness,
                started_at=page.started_at,
                finished_at=page.finished_at,
                data=data,
                diagnostics=(*identity_diagnostics, *page.diagnostics),
                provenance=page.provenance,
                identity_evidence=identity_evidence,
            )
        return self._failure(
            operation="quota.inspect",
            boundary="exact-slice-inspected",
            outcome=page.outcome.code.value,
            exit_class=page.outcome.exit_class,
            resource_scope=page.resource_scope,
            completeness=page.completeness,
            diagnostics=(*identity_diagnostics, *page.diagnostics),
            identity_evidence=identity_evidence,
        )

    def _failure(  # noqa: PLR0913 - canonical result fields stay explicit
        self,
        *,
        operation: str,
        boundary: str,
        outcome: str,
        exit_class: ExitClass,
        resource_scope: ResourceScope | None,
        completeness: Completeness | None = None,
        diagnostics: tuple[Diagnostic, ...] = (),
        unavailable_source: str | None = None,
        reason: str | None = None,
        identity_evidence: ProviderIdentityEvidence | None = None,
    ) -> OperationResult[ReadOnlyFailureData]:
        if completeness is None:
            completeness = (
                Completeness.complete()
                if unavailable_source is None
                else Completeness.unavailable(
                    EvidenceGap(
                        StableSymbol(unavailable_source),
                        StableSymbol(outcome),
                    )
                )
            )
        started_at = self._clock.now()
        return OperationResult(
            operation=OperationName(operation),
            resource_scope=resource_scope,
            boundary=OperationBoundary(
                condition=StableSymbol(boundary),
                reached=False,
            ),
            outcome=Outcome(StableSymbol(outcome), exit_class),
            completeness=completeness,
            started_at=started_at,
            finished_at=self._clock.now(),
            data=ReadOnlyFailureData(reason or outcome),
            diagnostics=diagnostics,
            identity_evidence=identity_evidence,
        )


def _configuration_outcome(error: ConfigurationError) -> str:
    if isinstance(error, UnknownProfileError):
        return "unknown-profile"
    if isinstance(error, ProfileResourceScopeError):
        return "profile-resource-scope-unavailable"
    if isinstance(error, UnsupportedResourceScopeError):
        return "unsupported-resource-scope"
    if isinstance(error, ResourceScopeUnavailableError):
        return "resource-scope-unavailable"
    return "invalid-resource-scope"


def _project_exit_class(diagnostics: tuple[Diagnostic, ...]) -> ExitClass:
    code = diagnostics[0].code.value
    if code == "project-authorization-failed":
        return ExitClass.AUTHORIZATION
    if code in {"project-not-found", "invalid-project-reference"}:
        return ExitClass.REJECTED_PRECONDITION
    return ExitClass.OPERATIONAL_FAILURE


def _adc_quota_project(
    configuration: ConfigSnapshot,
    selection: SelectionState,
    scope_input: ReadOnlyScopeInput,
) -> ADCQuotaProject | None:
    profile_name = scope_input.explicit_profile or selection.selected_profile
    if profile_name is None:
        return None
    profile = configuration.profile(profile_name)
    if profile.adc_quota_project is None:
        return None
    return ADCQuotaProject(profile.adc_quota_project.canonical_name)


def _matches_selector(
    item: QuotaQueryItem,
    selector: QuotaInspectSelector,
) -> bool:
    identity = item.identity
    return (
        identity.service == selector.service
        and identity.quota_id == selector.quota_id
        and identity.dimensions == selector.dimensions
        and item.location == selector.location
    )


def _merge_matching_items(
    retained: tuple[QuotaQueryItem, ...],
    current: tuple[QuotaQueryItem, ...],
) -> tuple[QuotaQueryItem, ...]:
    """Retain one observation for every exact slice seen before a page failure."""
    merged: dict[EffectiveQuotaSliceIdentity, QuotaQueryItem] = {
        item.identity: item for item in retained
    }
    merged.update({item.identity: item for item in current})
    return tuple(merged.values())


def _merge_source_coverage(
    retained: tuple[ProviderSourceCoverage, ...],
    current: tuple[ProviderSourceCoverage, ...],
) -> tuple[ProviderSourceCoverage, ...]:
    """Merge cumulative provider coverage while preserving service order."""
    merged = {coverage.service: coverage for coverage in retained}
    merged.update({coverage.service: coverage for coverage in current})
    return tuple(merged.values())


def _with_provider_identity[DataT](
    result: OperationResult[DataT],
    context: ProviderReadContext,
) -> OperationResult[DataT]:
    diagnostics = tuple(
        dict.fromkeys((*context.identity.diagnostics, *result.diagnostics))
    )
    return replace(
        result,
        diagnostics=diagnostics,
        identity_evidence=ProviderIdentityEvidence.from_adc(context.identity),
    )


def _diagnostic(
    code: str,
    phase: str,
    source: str,
    guidance: str,
) -> Diagnostic:
    return Diagnostic(
        DiagnosticCode(code),
        Severity.ERROR,
        DiagnosticPhase(phase),
        DiagnosticSource(source),
        RetryDisposition.AFTER_REFRESH,
        RedactedText(guidance),
    )


async def _await_provider_setup[ValueT](
    operation: Callable[[], Awaitable[ValueT]],
    *,
    deadline: float,
    cancellation: CancellationToken,
    monotonic: Callable[[], float],
) -> ValueT:
    """Await setup under the same caller-controlled stop contract as provider reads."""
    cancellation.raise_if_cancelled()
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise CoordinationDeadlineExceededError
    work_task = asyncio.ensure_future(operation())
    cancellation_task = asyncio.create_task(cancellation.wait())
    tasks = (work_task, cancellation_task)
    try:
        done, _ = await asyncio.wait(
            tasks,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            raise CoordinationCancelledError
        if work_task not in done:
            raise CoordinationDeadlineExceededError
        return await work_task
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _remaining_setup_timeout(
    deadline: float,
    monotonic: Callable[[], float],
) -> float:
    """Return positive setup time or preserve the global deadline exit."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise CoordinationDeadlineExceededError
    return remaining
