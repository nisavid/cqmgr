"""Provider-scoped read-only application facade contracts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Timer
from typing import TYPE_CHECKING, override

import pytest

from cqmgr.adapters.google.identity import (
    ADCCredentialSnapshot,
    GoogleADCIdentityProvider,
)
from cqmgr.adapters.persistence.coordination import SharedBudgetCoordinator
from cqmgr.application.configuration import ConfigSnapshot, Profile, SelectionState
from cqmgr.application.operations.quotas import (
    QuotaBrowseData,
    QuotaInspectData,
)
from cqmgr.application.operations.read_only import (
    IncompleteQuotaInspectData,
    QuotaInspectSelector,
    ReadOnlyFailureData,
    ReadOnlyOperations,
    ReadOnlyQuotaQuery,
    ReadOnlyScopeInput,
)
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    BudgetLimit,
    BudgetRequest,
    BudgetScope,
    CancellationToken,
)
from cqmgr.domain.accelerator_overlay import (
    CandidateLocations,
    ComputeInstanceRequirement,
    ProvisioningModel,
)
from cqmgr.domain.catalog import CatalogPredicates
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.identity import (
    ADCIdentityEvidence,
    ADCQuotaProject,
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
    ProviderIdentityEvidence,
)
from cqmgr.domain.projects import CanonicalProject, ProjectReference, ProjectResolution
from cqmgr.domain.quota_queries import (
    V1_PROVIDER_SERVICES,
    ProviderSourceCoverage,
    QuotaQueryItem,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaScope,
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

_ADC_RETURN_GUARD_SECONDS = 0.2

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class MemoryRepository[SnapshotT]:
    """Expose one validated snapshot through the repository port."""

    def __init__(self, snapshot: SnapshotT) -> None:
        """Retain the current snapshot and observable read count."""
        self.snapshot = snapshot
        self.read_count = 0

    async def read(self) -> SnapshotT:
        """Return the current snapshot."""
        self.read_count += 1
        return self.snapshot

    async def update(self, transform: Callable[[SnapshotT], SnapshotT]) -> SnapshotT:
        """Apply a repository-port-compatible transform."""
        self.snapshot = transform(self.snapshot)
        return self.snapshot


class BlockingRepository[SnapshotT](MemoryRepository[SnapshotT]):
    """Expose a local read that only the invocation deadline can stop."""

    @override
    async def read(self) -> SnapshotT:
        """Block until cancelled by bounded setup."""
        self.read_count += 1
        await asyncio.Event().wait()
        return self.snapshot


class FixedClock:
    """Supply one deterministic result time."""

    def now(self) -> datetime:
        """Return one UTC timestamp."""
        return datetime(2026, 7, 23, 12, tzinfo=UTC)


class RecordingProjectResolver:
    """Return one scripted canonical project."""

    def __init__(
        self,
        resolution: ProjectResolution,
        events: list[str] | None = None,
    ) -> None:
        """Retain the scripted resolution."""
        self.resolution = resolution
        self.references: list[ProjectReference] = []
        self.events = events

    async def resolve(self, reference: ProjectReference) -> ProjectResolution:
        """Record and resolve one explicit project."""
        if self.events is not None:
            self.events.append("project")
        self.references.append(reference)
        return self.resolution


class RecordingIdentityProvider:
    """Return one scripted safe ADC identity."""

    def __init__(
        self,
        identity: ADCIdentityEvidence,
        events: list[str] | None = None,
    ) -> None:
        """Retain the scripted identity."""
        self.identity = identity
        self.quota_projects: list[object] = []
        self.events = events

    async def resolve(
        self,
        *,
        adc_quota_project: object = None,
        timeout_seconds: float = 10.0,
    ) -> ADCIdentityEvidence:
        """Record the separated transport quota project."""
        del timeout_seconds
        if self.events is not None:
            self.events.append("identity")
        self.quota_projects.append(adc_quota_project)
        return self.identity


class BlockingADCRuntime:
    """Model sync ADC discovery that ignores cancellation until released."""

    def __init__(self, started: Event, release: Event, completed: Event) -> None:
        """Retain observable worker lifecycle signals."""
        self.started = started
        self.release = release
        self.completed = completed

    def load(
        self,
        *,
        scopes: object,
        quota_project_id: str | None,
        timeout_seconds: float = 10.0,
    ) -> ADCCredentialSnapshot:
        """Block discovery independently from the asyncio caller."""
        del scopes, quota_project_id, timeout_seconds
        self.started.set()
        self.release.wait()
        self.completed.set()
        return ADCCredentialSnapshot(CredentialKind.UNKNOWN, object())

    def refresh(
        self,
        snapshot: ADCCredentialSnapshot,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Reject an unexpected post-deadline refresh."""
        del snapshot, timeout_seconds
        msg = "abandoned discovery must not continue identity setup"
        raise AssertionError(msg)

    def fetch_user_info(
        self,
        snapshot: ADCCredentialSnapshot,
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, object]:
        """Reject an unrelated UserInfo request."""
        del snapshot, timeout_seconds
        msg = "unexpected UserInfo request"
        raise AssertionError(msg)


class RecordingBudget:
    """Record project-setup request charges."""

    def __init__(self, events: list[str]) -> None:
        """Retain a shared call-order ledger."""
        self.events = events
        self.requests: list[BudgetRequest] = []

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Record one conservative charge before provider dispatch."""
        del deadline, cancellation
        self.events.append("budget")
        self.requests.append(request)
        return BudgetGrant(0.0, request)


class RecordingQuotaOperations:
    """Record facade requests at the existing operation boundary."""

    def __init__(
        self,
        browse_results: list[OperationResult[QuotaBrowseData]],
        inspect_result: OperationResult[QuotaInspectData] | None = None,
    ) -> None:
        """Retain scripted delegate results."""
        self.browse_results = browse_results
        self.inspect_result = inspect_result
        self.browse_requests: list[object] = []
        self.inspect_requests: list[object] = []

    async def browse(self, request: object) -> OperationResult[QuotaBrowseData]:
        """Record and return one browse result."""
        self.browse_requests.append(request)
        return self.browse_results.pop(0)

    async def inspect(self, request: object) -> OperationResult[QuotaInspectData]:
        """Record and return the exact inspect result."""
        self.inspect_requests.append(request)
        assert self.inspect_result is not None
        return self.inspect_result


class UnusedWorkloadOperations:
    """Fail if an unrelated workload operation is dispatched."""

    async def resolve(self, request: object) -> OperationResult[None]:
        """Reject unexpected workload dispatch."""
        raise AssertionError(request)


class RecordingWorkloadOperations:
    """Record one typed workload-resolution request."""

    def __init__(self, scripted: OperationResult[None]) -> None:
        """Retain the scripted operation result."""
        self.scripted = scripted
        self.requests: list[object] = []

    async def resolve(self, request: object) -> OperationResult[None]:
        """Record and return one workload result."""
        self.requests.append(request)
        return self.scripted


def scope(identifier: str) -> ResourceScope:
    """Build one canonical project resource scope."""
    return ResourceScope(ResourceScopeKind.PROJECT, f"projects/{identifier}")


def canonical_project(identifier: str = "123456789") -> CanonicalProject:
    """Build canonical Resource Manager evidence."""
    return CanonicalProject(scope(identifier), "fixture-project", None)


def identity(*, available: bool = True) -> ADCIdentityEvidence:
    """Build safe available or unavailable ADC evidence."""
    if available:
        return ADCIdentityEvidence(
            credential_kind=CredentialKind.SERVICE_ACCOUNT,
            acting_principal=None,
            stable_principal=None,
            verification=PrincipalVerification.UNVERIFIED,
        )
    return ADCIdentityEvidence.unavailable(
        credential_kind=CredentialKind.UNKNOWN,
        code="adc-unavailable",
        guidance="Configure Application Default Credentials, then retry.",
    )


def result[DataT](
    operation: str,
    data: DataT,
    *,
    resource_scope: ResourceScope | None = None,
) -> OperationResult[DataT]:
    """Build a canonical successful operation result."""
    now = FixedClock().now()
    return OperationResult(
        operation=OperationName(operation),
        resource_scope=resource_scope,
        boundary=OperationBoundary(
            condition=StableSymbol("completed"),
            reached=True,
        ),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=now,
        finished_at=now,
        data=data,
    )


def browse_result(
    items: tuple[QuotaQueryItem, ...] = (),
) -> OperationResult[QuotaBrowseData]:
    """Build one scripted quota browse page."""
    return result(
        "quota.list",
        QuotaBrowseData(
            query=None,
            items=items,
            constraint_sets=(),
            ordered=True,
            total=len(items),
            next_cursor=None,
            snapshot_id="snapshot",
        ),
        resource_scope=scope("123456789"),
    )


def service(  # noqa: PLR0913 - explicit fixture dependencies stay independent
    *,
    configuration: ConfigSnapshot | None = None,
    selection: SelectionState | None = None,
    project_resolution: ProjectResolution | None = None,
    adc_identity: ADCIdentityEvidence | None = None,
    quotas: RecordingQuotaOperations | None = None,
    workloads: RecordingWorkloadOperations | None = None,
) -> tuple[
    ReadOnlyOperations,
    MemoryRepository[ConfigSnapshot],
    MemoryRepository[SelectionState],
    RecordingProjectResolver,
    RecordingIdentityProvider,
    RecordingQuotaOperations,
    RecordingWorkloadOperations | None,
]:
    """Compose the facade with observable application-port doubles."""
    project = canonical_project()
    resolver = RecordingProjectResolver(
        project_resolution or ProjectResolution(ProjectReference("123456789"), project)
    )
    identity_provider = RecordingIdentityProvider(adc_identity or identity())
    quota_operations = quotas or RecordingQuotaOperations([browse_result()])
    config_repository = MemoryRepository(configuration or ConfigSnapshot())
    selection_repository = MemoryRepository(selection or SelectionState())
    facade = ReadOnlyOperations(
        config_repository,
        selection_repository,
        resolver,
        identity_provider,
        quota_operations,  # type: ignore[arg-type]
        workloads or UnusedWorkloadOperations(),  # type: ignore[arg-type]
        FixedClock(),
        monotonic=lambda: 0.0,
    )
    return (
        facade,
        config_repository,
        selection_repository,
        resolver,
        identity_provider,
        quota_operations,
        workloads,
    )


def test_browse_resolves_selected_project_and_builds_bounded_provider_context() -> None:
    """A selected scope is canonicalized before one typed quota browse."""
    facade, _, _, resolver, identity_provider, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789"))
    )
    cancellation = CancellationToken()
    deadline = 42.5

    returned = asyncio.run(
        facade.browse(
            ReadOnlyQuotaQuery(),
            deadline=deadline,
            cancellation=cancellation,
        )
    )

    assert returned.outcome.exit_class is ExitClass.SUCCESS
    assert resolver.references == [ProjectReference("projects/123456789")]
    assert identity_provider.quota_projects == [None]
    request = quotas.browse_requests[0]
    assert request.context.project == canonical_project()  # type: ignore[attr-defined]
    assert request.context.deadline == deadline  # type: ignore[attr-defined]
    assert request.context.cancellation is cancellation  # type: ignore[attr-defined]
    assert request.query.resource_scope == scope("123456789")  # type: ignore[attr-defined]


def test_close_without_owned_provider_clients_is_idempotent() -> None:
    """Application-only facades need no special lifecycle adapter."""
    facade, *_ = service()

    asyncio.run(facade.aclose())
    asyncio.run(facade.aclose())


def test_provider_result_retains_only_sanitized_acting_principal_evidence() -> None:
    """A provider result exposes principal proof without ADC transport state."""
    principal = PrincipalIdentity(
        "serviceAccount:quota-reader@example.iam.gserviceaccount.com"
    )
    adc = ADCIdentityEvidence(
        credential_kind=CredentialKind.SERVICE_ACCOUNT,
        acting_principal=principal,
        stable_principal=principal,
        verification=PrincipalVerification.VERIFIED,
        adc_quota_project=ADCQuotaProject("billing-project"),
    )
    facade, _, _, _, _, _, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789")),
        adc_identity=adc,
    )

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=5.0))

    assert returned.identity_evidence == ProviderIdentityEvidence(
        credential_kind=CredentialKind.SERVICE_ACCOUNT,
        verification=PrincipalVerification.VERIFIED,
        acting_principal=principal,
    )
    assert "billing-project" not in repr(returned.identity_evidence)


def test_identity_is_resolved_before_resource_manager_canonicalization() -> None:
    """One configured ADC context signs Resource Manager and later provider reads."""
    events: list[str] = []
    project = canonical_project()
    resolver = RecordingProjectResolver(
        ProjectResolution(ProjectReference("123456789"), project),
        events,
    )
    identity_provider = RecordingIdentityProvider(identity(), events)
    quotas = RecordingQuotaOperations([browse_result()])
    facade = ReadOnlyOperations(
        MemoryRepository(ConfigSnapshot()),
        MemoryRepository(SelectionState(direct_resource_scope=scope("123456789"))),
        resolver,
        identity_provider,
        quotas,  # type: ignore[arg-type]
        UnusedWorkloadOperations(),  # type: ignore[arg-type]
        FixedClock(),
        monotonic=lambda: 0.0,
    )

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=42.5))

    assert returned.outcome.exit_class is ExitClass.SUCCESS
    assert events == ["identity", "project"]


def test_resource_manager_read_is_charged_before_dispatch() -> None:
    """Project canonicalization shares the invocation request budget."""
    events: list[str] = []
    project = canonical_project()
    resolver = RecordingProjectResolver(
        ProjectResolution(ProjectReference("123456789"), project),
        events,
    )
    identity_provider = RecordingIdentityProvider(identity(), events)
    budget = RecordingBudget(events)
    quotas = RecordingQuotaOperations([browse_result()])
    facade = ReadOnlyOperations(
        MemoryRepository(ConfigSnapshot()),
        MemoryRepository(SelectionState(direct_resource_scope=scope("123456789"))),
        resolver,
        identity_provider,
        quotas,  # type: ignore[arg-type]
        UnusedWorkloadOperations(),  # type: ignore[arg-type]
        FixedClock(),
        monotonic=lambda: 0.0,
        budget=budget,
    )

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=42.5))

    assert returned.outcome.exit_class is ExitClass.SUCCESS
    assert events == ["identity", "budget", "project"]
    assert budget.requests == [
        BudgetRequest("resource-manager", "projects/123456789", None)
    ]


def test_corrupt_budget_state_blocks_resource_manager_dispatch(
    tmp_path: Path,
) -> None:
    """Malformed shared accounting becomes a typed setup failure before RM."""
    (tmp_path / "budgets.json").write_bytes(b"not-json")
    budget = SharedBudgetCoordinator(
        tmp_path,
        {scope: BudgetLimit(capacity=1, period_seconds=60.0) for scope in BudgetScope},
    )
    resolver = RecordingProjectResolver(
        ProjectResolution(ProjectReference("123456789"), canonical_project())
    )
    quotas = RecordingQuotaOperations([browse_result()])
    facade = ReadOnlyOperations(
        MemoryRepository(ConfigSnapshot()),
        MemoryRepository(SelectionState(direct_resource_scope=scope("123456789"))),
        resolver,
        RecordingIdentityProvider(identity()),
        quotas,  # type: ignore[arg-type]
        UnusedWorkloadOperations(),  # type: ignore[arg-type]
        FixedClock(),
        budget=budget,
    )

    returned = asyncio.run(
        facade.browse(
            ReadOnlyQuotaQuery(),
            deadline=time.monotonic() + 1.0,
        )
    )

    assert returned.outcome.code.value == "provider-read-budget-unavailable"
    assert returned.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert resolver.references == []
    assert quotas.browse_requests == []


def test_expired_deadline_stops_before_identity_and_resource_manager() -> None:
    """Caller-controlled setup deadline prevents every external setup call."""
    facade, config, selected, resolver, identity_provider, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789"))
    )

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=-1.0))

    assert returned.outcome.code == StableSymbol("operation-deadline-exceeded")
    assert returned.outcome.exit_class is ExitClass.TIMEOUT
    assert config.read_count == selected.read_count == 0
    assert identity_provider.quota_projects == []
    assert resolver.references == []
    assert quotas.browse_requests == []


def test_cancelled_setup_stops_before_identity_and_resource_manager() -> None:
    """Caller cancellation prevents every external setup call."""
    facade, config, selected, resolver, identity_provider, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789"))
    )
    cancellation = CancellationToken()
    cancellation.cancel()

    returned = asyncio.run(
        facade.browse(
            ReadOnlyQuotaQuery(),
            deadline=1e20,
            cancellation=cancellation,
        )
    )

    assert returned.outcome.code == StableSymbol("operation-interrupted")
    assert returned.outcome.exit_class is ExitClass.INTERRUPTED
    assert config.read_count == selected.read_count == 0
    assert identity_provider.quota_projects == []
    assert resolver.references == []
    assert quotas.browse_requests == []


def test_deadline_expires_while_local_configuration_is_loading() -> None:
    """The invocation deadline bounds local setup as well as provider setup."""
    configuration = BlockingRepository(ConfigSnapshot())
    selection = MemoryRepository(
        SelectionState(direct_resource_scope=scope("123456789"))
    )
    resolver = RecordingProjectResolver(
        ProjectResolution(ProjectReference("123456789"), canonical_project())
    )
    identity_provider = RecordingIdentityProvider(identity())
    quotas = RecordingQuotaOperations([browse_result()])
    facade = ReadOnlyOperations(
        configuration,
        selection,
        resolver,
        identity_provider,
        quotas,  # type: ignore[arg-type]
        UnusedWorkloadOperations(),  # type: ignore[arg-type]
        FixedClock(),
    )

    returned = asyncio.run(
        asyncio.wait_for(
            facade.browse(
                ReadOnlyQuotaQuery(),
                deadline=time.monotonic() + 0.01,
            ),
            timeout=0.5,
        )
    )

    assert returned.outcome.exit_class is ExitClass.TIMEOUT
    assert configuration.read_count == 1
    assert selection.read_count == 1
    assert resolver.references == []
    assert identity_provider.quota_projects == []


def test_deadline_abandons_hung_adc_discovery_without_executor_shutdown_wait() -> None:
    """A timed-out invocation returns while its uncooperative sync load is fenced."""
    started = Event()
    release = Event()
    completed = Event()
    release_timer = Timer(0.5, release.set)
    resolver = RecordingProjectResolver(
        ProjectResolution(ProjectReference("123456789"), canonical_project())
    )
    quotas = RecordingQuotaOperations([browse_result()])
    facade = ReadOnlyOperations(
        MemoryRepository(ConfigSnapshot()),
        MemoryRepository(SelectionState(direct_resource_scope=scope("123456789"))),
        resolver,
        GoogleADCIdentityProvider(BlockingADCRuntime(started, release, completed)),
        quotas,  # type: ignore[arg-type]
        UnusedWorkloadOperations(),  # type: ignore[arg-type]
        FixedClock(),
    )
    release_timer.start()
    before = time.monotonic()
    try:
        returned = asyncio.run(
            facade.browse(
                ReadOnlyQuotaQuery(),
                deadline=time.monotonic() + 0.02,
            )
        )
        elapsed = time.monotonic() - before
    finally:
        release.set()
        release_timer.cancel()
        release_timer.join()

    assert started.is_set()
    assert elapsed < _ADC_RETURN_GUARD_SECONDS
    assert completed.wait(timeout=0.5)
    assert returned.outcome.exit_class is ExitClass.TIMEOUT
    assert resolver.references == []
    assert quotas.browse_requests == []


def test_cursor_browse_resumes_without_local_state_project_or_adc_resolution() -> None:
    """A product cursor resumes its bound snapshot without provider setup."""
    facade, config, selected, resolver, identity_provider, quotas, _ = service()

    returned = asyncio.run(facade.browse(cursor="opaque", limit=7, deadline=9.0))

    assert returned.outcome.exit_class is ExitClass.SUCCESS
    assert config.read_count == selected.read_count == 0
    assert resolver.references == []
    assert identity_provider.quota_projects == []
    request = quotas.browse_requests[0]
    assert request.cursor == "opaque"  # type: ignore[attr-defined]
    assert request.context is None  # type: ignore[attr-defined]
    assert request.query is None  # type: ignore[attr-defined]


def test_missing_scope_is_a_rejected_precondition_before_provider_setup() -> None:
    """Missing explicit and selected scope state is a typed operation failure."""
    facade, _, _, resolver, identity_provider, quotas, _ = service()

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=5.0))

    assert returned.operation == OperationName("quota.list")
    assert returned.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert returned.outcome.code == StableSymbol("resource-scope-unavailable")
    assert resolver.references == []
    assert identity_provider.quota_projects == []
    assert quotas.browse_requests == []


def test_project_resolution_failure_preserves_only_safe_diagnostics() -> None:
    """Canonicalization follows configured ADC and retains normalized guidance."""
    diagnostic = Diagnostic(
        DiagnosticCode("project-resolution-failed"),
        Severity.ERROR,
        DiagnosticPhase("project-resolution"),
        DiagnosticSource("resource-manager"),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("Check Resource Manager access, then retry."),
    )
    failed = ProjectResolution(
        ProjectReference("123456789"),
        None,
        (diagnostic,),
    )
    facade, _, _, _, identity_provider, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789")),
        project_resolution=failed,
    )

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=5.0))

    assert returned.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert returned.outcome.code == StableSymbol("project-resolution-failed")
    assert returned.diagnostics == (diagnostic,)
    assert identity_provider.quota_projects == [None]
    assert quotas.browse_requests == []


def test_unavailable_adc_stops_before_quota_operations_without_provider_text() -> None:
    """Unavailable ADC is authorization failure with static safe diagnostics."""
    facade, _, _, _, _, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789")),
        adc_identity=identity(available=False),
    )

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=5.0))

    assert returned.outcome.exit_class is ExitClass.AUTHORIZATION
    assert returned.outcome.code == StableSymbol("adc-unavailable")
    assert "Configure Application Default Credentials" in repr(returned)
    assert quotas.browse_requests == []


def test_inspect_derives_quota_scope_from_one_exact_provider_slice() -> None:
    """Selector inputs never invent the quota scope required by exact inspect."""
    dimensions = NormalizedDimensions((("region", "us-central1"),))
    exact_identity = EffectiveQuotaSliceIdentity(
        scope("123456789"),
        "compute.googleapis.com",
        "GPUS-PER-GPU-FAMILY-per-project-region",
        dimensions,
        QuotaScope.REGIONAL,
    )
    item = QuotaQueryItem(
        identity=exact_identity,
        display_name=None,
        accelerator_id=None,
        location="us-central1",
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=False,
            guided=False,
            mutable=False,
        ),
        effective_value=None,
    )
    inspect_data = QuotaInspectData(
        exact_identity,
        None,
        None,
        None,
        None,
        None,
        None,
        "fixture",
    )
    quotas = RecordingQuotaOperations(
        [browse_result((item,))],
        result(
            "quota.inspect",
            inspect_data,
            resource_scope=scope("123456789"),
        ),
    )
    facade, _, _, _, _, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789")),
        quotas=quotas,
    )

    returned = asyncio.run(
        facade.inspect(
            QuotaInspectSelector(
                service="compute.googleapis.com",
                quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
                location="us-central1",
                dimensions=dimensions,
            ),
            deadline=5.0,
            scope_input=ReadOnlyScopeInput(),
        )
    )

    assert returned.outcome.exit_class is ExitClass.SUCCESS
    browse_request = quotas.browse_requests[0]
    assert browse_request.query.services == ("compute.googleapis.com",)  # type: ignore[attr-defined]
    inspect_request = quotas.inspect_requests[0]
    assert inspect_request.identity == exact_identity  # type: ignore[attr-defined]
    assert inspect_request.identity.quota_scope is QuotaScope.REGIONAL  # type: ignore[attr-defined]


def test_incomplete_inspect_retains_matching_browse_evidence() -> None:
    """An incomplete exact lookup keeps its usable matching observation visible."""
    selector = QuotaInspectSelector(
        service="compute",
        quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
        location="us-central1",
        dimensions=NormalizedDimensions((("region", "us-central1"),)),
    )
    item = QuotaQueryItem(
        identity=EffectiveQuotaSliceIdentity(
            scope("123456789"),
            "compute.googleapis.com",
            selector.quota_id,
            selector.dimensions,
            QuotaScope.REGIONAL,
        ),
        display_name=None,
        accelerator_id=None,
        location=selector.location,
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=False,
            guided=False,
            mutable=False,
        ),
        effective_value=None,
    )
    coverage = (
        ProviderSourceCoverage.incomplete(
            V1_PROVIDER_SERVICES[0],
            pages_attempted=1,
            pages_completed=0,
            observed_at=FixedClock().now(),
        ),
        ProviderSourceCoverage.intentionally_unqueried(V1_PROVIDER_SERVICES[1]),
    )
    page = browse_result((item,))
    page = replace(
        page,
        boundary=replace(page.boundary, reached=False),
        outcome=Outcome(
            StableSymbol("incomplete-evidence"),
            ExitClass.INCOMPLETE_EVIDENCE,
        ),
        completeness=Completeness.incomplete(
            EvidenceGap(
                StableSymbol("cloud-quotas"),
                StableSymbol("provider-read-incomplete"),
            )
        ),
        data=replace(
            page.data,
            ordered=False,
            total=None,
            snapshot_id=None,
            reason="incomplete-provider-evidence",
            source_coverage=coverage,
        ),
    )
    quotas = RecordingQuotaOperations([page])
    facade, _, _, _, _, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789")),
        quotas=quotas,
    )

    returned = asyncio.run(facade.inspect(selector, deadline=5.0))

    assert returned.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert returned.completeness.has_partial_data
    assert returned.data == IncompleteQuotaInspectData(
        selector=selector,
        matching_items=(item,),
        source_coverage=coverage,
        reason="incomplete-provider-evidence",
    )
    assert quotas.inspect_requests == []


@pytest.mark.parametrize(
    ("page_outcome", "expected_outcome"),
    [
        (
            Outcome(
                StableSymbol("provider-read-failed"),
                ExitClass.OPERATIONAL_FAILURE,
            ),
            Outcome(
                StableSymbol("incomplete-evidence"),
                ExitClass.INCOMPLETE_EVIDENCE,
            ),
        ),
        (
            Outcome(
                StableSymbol("operation-deadline-exceeded"),
                ExitClass.TIMEOUT,
            ),
            Outcome(
                StableSymbol("operation-deadline-exceeded"),
                ExitClass.TIMEOUT,
            ),
        ),
    ],
)
def test_later_failed_page_retains_earlier_inspect_match_and_stop_exit(
    page_outcome: Outcome,
    expected_outcome: Outcome,
) -> None:
    """A later page failure cannot discard earlier evidence or stop semantics."""
    selector = QuotaInspectSelector(
        service="compute",
        quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
        location="us-central1",
        dimensions=NormalizedDimensions((("region", "us-central1"),)),
    )
    item = QuotaQueryItem(
        identity=EffectiveQuotaSliceIdentity(
            scope("123456789"),
            "compute.googleapis.com",
            selector.quota_id,
            selector.dimensions,
            QuotaScope.REGIONAL,
        ),
        display_name=None,
        accelerator_id=None,
        location=selector.location,
        quota_pool=None,
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=False,
            guided=False,
            mutable=False,
        ),
        effective_value=None,
    )
    coverage = (
        ProviderSourceCoverage.complete(
            V1_PROVIDER_SERVICES[0],
            pages_attempted=1,
            pages_completed=1,
            observed_at=FixedClock().now(),
        ),
        ProviderSourceCoverage.intentionally_unqueried(V1_PROVIDER_SERVICES[1]),
    )
    first = browse_result((item,))
    first = replace(
        first,
        data=replace(
            first.data,
            next_cursor="cursor-2",
            source_coverage=coverage,
        ),
    )
    second = browse_result()
    second = replace(
        second,
        boundary=replace(second.boundary, reached=False),
        outcome=page_outcome,
        completeness=Completeness.unavailable(
            EvidenceGap(
                StableSymbol("cloud-quotas"),
                StableSymbol("provider-read-failed"),
            )
        ),
        data=replace(
            second.data,
            ordered=False,
            total=None,
            snapshot_id=None,
            reason="provider-read-failed",
        ),
    )
    quotas = RecordingQuotaOperations([first, second])
    facade, _, _, _, _, quotas, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789")),
        quotas=quotas,
    )

    returned = asyncio.run(facade.inspect(selector, deadline=5.0))

    assert returned.outcome == expected_outcome
    assert returned.completeness == Completeness.incomplete(
        EvidenceGap(
            StableSymbol("cloud-quotas"),
            StableSymbol("provider-read-failed"),
        )
    )
    assert returned.data == IncompleteQuotaInspectData(
        selector=selector,
        matching_items=(item,),
        source_coverage=coverage,
        reason="provider-read-failed",
    )
    assert quotas.inspect_requests == []


def test_read_only_input_guards_reject_untyped_or_ambiguous_values() -> None:
    """Facade input DTOs reject values outside their exact typed contracts."""
    with pytest.raises(TypeError, match="explicit_resource_scope"):
        ReadOnlyScopeInput(explicit_resource_scope=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="explicit_profile"):
        ReadOnlyScopeInput(explicit_profile="")
    with pytest.raises(TypeError, match="query filters"):
        ReadOnlyQuotaQuery(filters=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="query sort"):
        ReadOnlyQuotaQuery(sort=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="quota ID"):
        QuotaInspectSelector("compute", "", "global")
    with pytest.raises(TypeError, match="dimensions"):
        QuotaInspectSelector(
            "compute",
            "quota-id",
            "global",
            dimensions=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "service_input",
    ["compute", "compute.googleapis.com"],
)
def test_inspect_normalizes_service_shorthand_before_exact_matching(
    service_input: str,
) -> None:
    """Shorthand and durable DNS service inputs select the same provider slice."""
    selector = QuotaInspectSelector(
        service=service_input,
        quota_id="GPUS-ALL-REGIONS-per-project",
        location="global",
    )

    assert selector.service == "compute.googleapis.com"


def test_usage_failures_are_typed_and_remain_offline() -> None:
    """Parser failures produce JSON-ready exit-2 results without setup reads."""
    facade, configuration, selection, resolver, identity_provider, quotas, _ = service()

    results = asyncio.run(_usage_failures(facade))

    assert [item.operation.value for item in results] == [
        "quota.list",
        "quota.inspect",
        "quota.resolve",
    ]
    assert all(item.outcome.exit_class is ExitClass.USAGE for item in results)
    assert [item.outcome.code.value for item in results] == [
        "invalid-quota-query",
        "invalid-quota-selector",
        "invalid-workload-requirement",
    ]
    assert all(
        item.data.reason == "select exactly one location mode" for item in results
    )
    assert configuration.read_count == selection.read_count == 0
    assert resolver.references == []
    assert identity_provider.quota_projects == []
    assert quotas.browse_requests == quotas.inspect_requests == []


async def _usage_failures(
    facade: ReadOnlyOperations,
) -> tuple[
    OperationResult[ReadOnlyFailureData],
    OperationResult[ReadOnlyFailureData],
    OperationResult[ReadOnlyFailureData],
]:
    """Collect every typed usage helper through its public async seam."""
    return (
        await facade.browse_usage_failure("select exactly one location mode"),
        await facade.inspect_usage_failure("select exactly one location mode"),
        await facade.resolve_usage_failure("select exactly one location mode"),
    )


def test_selected_profile_supplies_scope_and_separate_adc_quota_project() -> None:
    """Profile transport billing never replaces the selected resource scope."""
    facade, _, _, _, identity_provider, _, _ = service(
        configuration=ConfigSnapshot(
            profiles=(
                Profile(
                    name="primary",
                    resource_scope=scope("123456789"),
                    adc_quota_project=scope("987654321"),
                ),
            )
        ),
        selection=SelectionState(selected_profile="primary"),
    )

    returned = asyncio.run(facade.browse(ReadOnlyQuotaQuery(), deadline=5.0))

    assert returned.resource_scope == scope("123456789")
    assert identity_provider.quota_projects == [ADCQuotaProject("projects/987654321")]


def test_resolve_delegates_typed_requirement_with_the_same_bounded_context() -> None:
    """Workload resolution shares scope, ADC, deadline, and cancellation policy."""
    workload_result = result(
        "quota.resolve.compute-instance",
        None,
        resource_scope=scope("123456789"),
    )
    workloads = RecordingWorkloadOperations(workload_result)
    facade, _, _, _, _, _, _ = service(
        selection=SelectionState(direct_resource_scope=scope("123456789")),
        workloads=workloads,
    )
    requirement = ComputeInstanceRequirement(
        machine_type="a3-highgpu-8g",
        instance_count=2,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )
    deadline = 8.0

    returned = asyncio.run(facade.resolve(requirement, deadline=deadline))

    assert returned.operation == workload_result.operation
    assert returned.data is workload_result.data
    assert returned.identity_evidence == ProviderIdentityEvidence.from_adc(identity())
    request = workloads.requests[0]
    assert request.requirement is requirement  # type: ignore[attr-defined]
    assert request.context.deadline == deadline  # type: ignore[attr-defined]
