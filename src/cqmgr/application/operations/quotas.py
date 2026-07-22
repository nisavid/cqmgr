"""Quota browse, exact-slice inspection, and workload-resolution operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from cqmgr.application.ports.catalog_reads import (
    ComputeMachineTypeReadRequest,
    TpuAcceleratorTypeReadRequest,
    TpuLocationReadRequest,
    TpuRuntimeVersionReadRequest,
)
from cqmgr.application.ports.provider_reads import (
    EffectiveQuotaReadRequest,
    ProviderReadContext,
    QuotaPreferenceReadRequest,
    UsageReadRequest,
)
from cqmgr.application.ports.quota_snapshots import (
    QuotaCursorError,
    QuotaSnapshotRepositoryError,
)
from cqmgr.domain.accelerator_overlay import (
    GpuWorkloadRequirement,
    QuotaConstraintAssessment,
    ResolutionFailureReason,
    ResolvedQuotaRequirement,
    TpuWorkloadRequirement,
    WorkloadCatalogEvidence,
    WorkloadResolutionError,
)
from cqmgr.domain.catalog import (
    AcceleratorConstraintSet,
    CatalogEvidenceSource,
    ManagementPlane,
)
from cqmgr.domain.quota_queries import (
    QUOTA_QUERY_EVIDENCE_CONTRACT,
    IncompatibleSortUnitsError,
    QuerySnapshotMetadata,
    QuotaQuery,
    QuotaQueryItem,
    QuotaQuerySnapshot,
    QuotaSortField,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    MonitoringValueKind,
    ProviderRead,
    QuotaPreferenceEvidence,
    QuotaQuantity,
    QuotaScope,
    UsageObservation,
)
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
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    QuotaRequestStatus,
    Reconciliation,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from cqmgr.application.ports.catalog_reads import (
        ComputeMachineTypeReader,
        TpuAcceleratorTypeReader,
        TpuLocationReader,
        TpuRuntimeVersionReader,
    )
    from cqmgr.application.ports.clock import Clock
    from cqmgr.application.ports.provider_reads import (
        EffectiveQuotaReader,
        QuotaPreferenceReader,
        UsageReader,
    )
    from cqmgr.application.ports.quota_snapshots import (
        QuotaQueryCursorCodec,
        QuotaQuerySnapshotRepository,
    )
    from cqmgr.domain.accelerator_overlay import SemanticAcceleratorOverlay
    from cqmgr.domain.catalog import CatalogLocationCoverage
    from cqmgr.domain.diagnostics import Diagnostic


MAX_BROWSE_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class QuotaResolveRequest:
    """Resolve one typed workload against one explicit project context."""

    context: ProviderReadContext
    requirement: GpuWorkloadRequirement | TpuWorkloadRequirement

    def __post_init__(self) -> None:
        """Require explicit provider context and a typed workload shape."""
        if not isinstance(self.context, ProviderReadContext):
            msg = "quota resolution requires ProviderReadContext"
            raise TypeError(msg)
        if not isinstance(
            self.requirement, (GpuWorkloadRequirement, TpuWorkloadRequirement)
        ):
            msg = "quota resolution requires a typed GPU or TPU workload"
            raise TypeError(msg)


class WorkloadResolutionOperations:
    """Resolve workload shapes from live quota and compatibility evidence."""

    def __init__(  # noqa: PLR0913
        self,
        effective: EffectiveQuotaReader,
        usage: UsageReader,
        compute_machine_types: ComputeMachineTypeReader,
        tpu_locations: TpuLocationReader,
        tpu_accelerator_types: TpuAcceleratorTypeReader,
        tpu_runtime_versions: TpuRuntimeVersionReader,
        overlay: SemanticAcceleratorOverlay,
        clock: Clock,
        *,
        usage_window: timedelta = timedelta(hours=1),
    ) -> None:
        """Inject every provider-neutral evidence boundary and the semantic overlay."""
        self._effective = effective
        self._usage = usage
        self._compute_machine_types = compute_machine_types
        self._tpu_locations = tpu_locations
        self._tpu_accelerator_types = tpu_accelerator_types
        self._tpu_runtime_versions = tpu_runtime_versions
        self._overlay = overlay
        self._clock = clock
        self._usage_window = usage_window

    async def resolve(  # noqa: PLR0911
        self,
        request: QuotaResolveRequest,
    ) -> OperationResult[ResolvedQuotaRequirement | None]:
        """Resolve one exact workload without making a capacity claim."""
        started_at = self._clock.now()
        effective_read, usage_read, catalog, diagnostics = await self._read_evidence(
            request, started_at
        )
        if not effective_read.complete:
            gaps = tuple(
                EvidenceGap(
                    StableSymbol(source),
                    StableSymbol("effective-quota-read-incomplete"),
                )
                for source in sorted(
                    {
                        diagnostic.source.value
                        for diagnostic in effective_read.diagnostics
                    }
                    or {"effective-quota"}
                )
            )
            has_partial_data = bool(effective_read.values)
            return self._result(
                request,
                started_at,
                reached=False,
                outcome="effective-quota-read-incomplete",
                exit_class=(
                    ExitClass.INCOMPLETE_EVIDENCE
                    if has_partial_data
                    else ExitClass.OPERATIONAL_FAILURE
                ),
                completeness=(
                    Completeness.incomplete(*gaps)
                    if has_partial_data
                    else Completeness.unavailable(*gaps)
                ),
                data=None,
                diagnostics=diagnostics,
            )
        if not usage_read.complete:
            gaps = (
                EvidenceGap(
                    StableSymbol("cloud-monitoring"),
                    StableSymbol("quota-usage-read-incomplete"),
                ),
            )
            return self._result(
                request,
                started_at,
                reached=False,
                outcome="quota-usage-read-incomplete",
                exit_class=ExitClass.INCOMPLETE_EVIDENCE,
                completeness=Completeness.incomplete(*gaps),
                data=None,
                diagnostics=diagnostics,
            )
        catalog_gaps = _required_catalog_gaps(request.requirement, catalog)
        if catalog_gaps:
            has_partial_data = bool(effective_read.values)
            return self._result(
                request,
                started_at,
                reached=False,
                outcome=ResolutionFailureReason.MISSING_LOCATION_EVIDENCE.value,
                exit_class=(
                    ExitClass.INCOMPLETE_EVIDENCE
                    if has_partial_data
                    else ExitClass.OPERATIONAL_FAILURE
                ),
                completeness=(
                    Completeness.incomplete(*catalog_gaps)
                    if has_partial_data
                    else Completeness.unavailable(*catalog_gaps)
                ),
                data=None,
                diagnostics=diagnostics,
            )
        try:
            resolved = self._overlay.resolve(
                request.requirement,
                effective_read.values,
                catalog,
            )
        except WorkloadResolutionError as error:
            if error.reason is ResolutionFailureReason.MISSING_LOCATION_EVIDENCE:
                gaps = tuple(
                    EvidenceGap(
                        StableSymbol(source),
                        StableSymbol(error.reason.value),
                    )
                    for source in sorted(
                        {diagnostic.source.value for diagnostic in diagnostics}
                        or {"accelerator-catalog"}
                    )
                )
                has_partial_data = bool(effective_read.values)
                return self._result(
                    request,
                    started_at,
                    reached=False,
                    outcome=error.reason.value,
                    exit_class=(
                        ExitClass.INCOMPLETE_EVIDENCE
                        if has_partial_data
                        else ExitClass.OPERATIONAL_FAILURE
                    ),
                    completeness=(
                        Completeness.incomplete(*gaps)
                        if has_partial_data
                        else Completeness.unavailable(*gaps)
                    ),
                    data=None,
                    diagnostics=diagnostics,
                )
            return self._result(
                request,
                started_at,
                reached=False,
                outcome=error.reason.value,
                exit_class=ExitClass.REJECTED_PRECONDITION,
                completeness=Completeness.complete(),
                data=None,
                diagnostics=diagnostics,
            )
        try:
            assessed = replace(
                resolved,
                assessments=_quota_constraint_assessments(
                    resolved,
                    effective_read.values,
                    usage_read.values,
                ),
            )
        except _AmbiguousEvidenceError:
            gap = EvidenceGap(
                StableSymbol("cloud-monitoring"),
                StableSymbol("constraint-usage-incomplete"),
            )
            return self._result(
                request,
                started_at,
                reached=False,
                outcome="constraint-usage-incomplete",
                exit_class=ExitClass.INCOMPLETE_EVIDENCE,
                completeness=Completeness.incomplete(gap),
                data=None,
                diagnostics=diagnostics,
            )
        return self._result(
            request,
            started_at,
            reached=True,
            outcome="requirement-resolved",
            exit_class=ExitClass.SUCCESS,
            completeness=Completeness.complete(),
            data=assessed,
            diagnostics=diagnostics,
        )

    async def _read_evidence(
        self,
        request: QuotaResolveRequest,
        observed_at: datetime,
    ) -> tuple[
        ProviderRead[EffectiveQuotaEvidence],
        ProviderRead[UsageObservation],
        WorkloadCatalogEvidence,
        tuple[Diagnostic, ...],
    ]:
        requirement = request.requirement
        uses_compute = isinstance(requirement, GpuWorkloadRequirement) or (
            isinstance(requirement, TpuWorkloadRequirement)
            and requirement.management_plane is ManagementPlane.COMPUTE
        )
        if uses_compute:
            return await self._read_compute_evidence(request.context, observed_at)
        return await self._read_legacy_tpu_evidence(
            request.context,
            requirement.zone,
            observed_at,
        )

    async def _read_compute_evidence(
        self,
        context: ProviderReadContext,
        observed_at: datetime,
    ) -> tuple[
        ProviderRead[EffectiveQuotaEvidence],
        ProviderRead[UsageObservation],
        WorkloadCatalogEvidence,
        tuple[Diagnostic, ...],
    ]:
        effective_read, usage_read, machine_read = await asyncio.gather(
            self._effective.read(
                EffectiveQuotaReadRequest(context, "compute.googleapis.com")
            ),
            self._usage.read(
                UsageReadRequest(
                    context,
                    "compute.googleapis.com",
                    observed_at - self._usage_window,
                    observed_at,
                )
            ),
            self._compute_machine_types.read(ComputeMachineTypeReadRequest(context)),
        )
        diagnostics = _resolution_diagnostics(
            (*effective_read.diagnostics, *usage_read.diagnostics),
            machine_read.read.diagnostics,
            machine_read.location_coverage,
        )
        return (
            effective_read,
            usage_read,
            WorkloadCatalogEvidence(
                compute_machine_types=machine_read.values,
                tpu_locations=(),
                tpu_accelerator_types=(),
                tpu_runtime_versions=(),
                coverage=machine_read.location_coverage,
            ),
            diagnostics,
        )

    async def _read_legacy_tpu_evidence(
        self,
        context: ProviderReadContext,
        zone: str,
        observed_at: datetime,
    ) -> tuple[
        ProviderRead[EffectiveQuotaEvidence],
        ProviderRead[UsageObservation],
        WorkloadCatalogEvidence,
        tuple[Diagnostic, ...],
    ]:
        (
            effective_read,
            usage_read,
            location_read,
            accelerator_read,
            runtime_read,
        ) = await asyncio.gather(
            self._effective.read(
                EffectiveQuotaReadRequest(context, "tpu.googleapis.com")
            ),
            self._usage.read(
                UsageReadRequest(
                    context,
                    "tpu.googleapis.com",
                    observed_at - self._usage_window,
                    observed_at,
                )
            ),
            self._tpu_locations.read(TpuLocationReadRequest(context)),
            self._tpu_accelerator_types.read(
                TpuAcceleratorTypeReadRequest(context, zone)
            ),
            self._tpu_runtime_versions.read(
                TpuRuntimeVersionReadRequest(context, zone)
            ),
        )
        coverage = (
            *location_read.location_coverage,
            *accelerator_read.location_coverage,
            *runtime_read.location_coverage,
        )
        diagnostics = _resolution_diagnostics(
            (*effective_read.diagnostics, *usage_read.diagnostics),
            (
                *location_read.read.diagnostics,
                *accelerator_read.read.diagnostics,
                *runtime_read.read.diagnostics,
            ),
            coverage,
        )
        return (
            effective_read,
            usage_read,
            WorkloadCatalogEvidence(
                compute_machine_types=(),
                tpu_locations=location_read.values,
                tpu_accelerator_types=accelerator_read.values,
                tpu_runtime_versions=runtime_read.values,
                coverage=coverage,
            ),
            diagnostics,
        )

    def _result[DataT](  # noqa: PLR0913
        self,
        request: QuotaResolveRequest,
        started_at: datetime,
        *,
        reached: bool,
        outcome: str,
        exit_class: ExitClass,
        completeness: Completeness,
        data: DataT,
        diagnostics: tuple[Diagnostic, ...],
    ) -> OperationResult[DataT]:
        return OperationResult(
            operation=OperationName("quota.resolve"),
            resource_scope=request.context.project.resource_scope,
            boundary=OperationBoundary(
                StableSymbol("workload-requirement-resolved"), reached
            ),
            outcome=Outcome(StableSymbol(outcome), exit_class),
            completeness=completeness,
            started_at=started_at,
            finished_at=self._clock.now(),
            data=data,
            diagnostics=diagnostics,
        )


@dataclass(frozen=True, slots=True)
class QuotaBrowseRequest:
    """One initial or cursor-resumed bounded logical quota query."""

    context: ProviderReadContext | None = None
    query: QuotaQuery | None = None
    cursor: str | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        """Require context, at least one query identity, and a bounded limit."""
        if self.context is not None and not isinstance(
            self.context, ProviderReadContext
        ):
            msg = "quota browse context must be ProviderReadContext or None"
            raise TypeError(msg)
        if self.query is not None and not isinstance(self.query, QuotaQuery):
            msg = "quota browse query must be QuotaQuery or None"
            raise TypeError(msg)
        if self.cursor is not None and (
            not isinstance(self.cursor, str) or not self.cursor
        ):
            msg = "quota browse cursor must be non-empty text or None"
            raise ValueError(msg)
        if self.query is None and self.cursor is None:
            msg = "quota browse requires a query or cursor"
            raise ValueError(msg)
        if self.cursor is None and self.context is None:
            msg = "an initial quota browse requires ProviderReadContext"
            raise ValueError(msg)
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            msg = "quota browse limit must be an integer"
            raise TypeError(msg)
        if not 1 <= self.limit <= MAX_BROWSE_LIMIT:
            msg = "quota browse limit must be from 1 through 1000"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class QuotaBrowseData:
    """One logical page and its honest collection-level claims."""

    query: QuotaQuery | None
    items: tuple[QuotaQueryItem, ...]
    constraint_sets: tuple[AcceleratorConstraintSet, ...]
    ordered: bool
    total: int | None
    next_cursor: str | None
    snapshot_id: str | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class QuotaInspectRequest:
    """Inspect one complete exact effective quota slice identity."""

    context: ProviderReadContext
    identity: EffectiveQuotaSliceIdentity

    def __post_init__(self) -> None:
        """Require explicit provider context and exact identity."""
        if not isinstance(self.context, ProviderReadContext):
            msg = "quota inspect requires ProviderReadContext"
            raise TypeError(msg)
        if not isinstance(self.identity, EffectiveQuotaSliceIdentity):
            msg = "quota inspect requires EffectiveQuotaSliceIdentity"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class QuotaInspectData:
    """Exact live evidence and every safely joined related fact."""

    identity: EffectiveQuotaSliceIdentity
    evidence: EffectiveQuotaEvidence | None
    item: QuotaQueryItem | None
    preference: QuotaPreferenceEvidence | None
    usage: UsageObservation | None
    status: QuotaRequestStatus | None
    constraint_set: AcceleratorConstraintSet | None
    reason: str | None = None
    constraint_sets: tuple[AcceleratorConstraintSet, ...] = ()

    def __post_init__(self) -> None:
        """Normalize singular compatibility only for one unambiguous set."""
        constraint_sets = self.constraint_sets
        if not isinstance(constraint_sets, tuple) or any(
            not isinstance(item, AcceleratorConstraintSet) for item in constraint_sets
        ):
            msg = "inspect constraint_sets must contain AcceleratorConstraintSet"
            raise TypeError(msg)
        if self.constraint_set is not None:
            if constraint_sets and constraint_sets != (self.constraint_set,):
                msg = "inspect singular constraint_set is ambiguous"
                raise ValueError(msg)
            constraint_sets = (self.constraint_set,)
        constraint_sets = tuple(dict.fromkeys(constraint_sets))
        object.__setattr__(self, "constraint_sets", constraint_sets)
        object.__setattr__(
            self,
            "constraint_set",
            constraint_sets[0] if len(constraint_sets) == 1 else None,
        )


class _AmbiguousEvidenceError(ValueError):
    """Provider observations cannot be joined to one exact slice safely."""


class _InapplicableSortError(ValueError):
    """A requested public sort has no value in the collected result."""


class QuotaOperations:
    """Browse and inspect quota evidence through shared provider-neutral ports."""

    def __init__(  # noqa: PLR0913
        self,
        effective: EffectiveQuotaReader,
        preferences: QuotaPreferenceReader,
        usage: UsageReader,
        overlay: SemanticAcceleratorOverlay,
        snapshots: QuotaQuerySnapshotRepository,
        cursors: QuotaQueryCursorCodec,
        clock: Clock,
        *,
        snapshot_id_factory: Callable[[], str] = lambda: uuid4().hex,
        snapshot_ttl: timedelta = timedelta(minutes=15),
        usage_window: timedelta = timedelta(hours=1),
    ) -> None:
        """Inject provider, semantic, local snapshot, cursor, and time boundaries."""
        if snapshot_ttl <= timedelta(0) or usage_window <= timedelta(0):
            msg = "quota snapshot TTL and usage window must be positive"
            raise ValueError(msg)
        self._effective = effective
        self._preferences = preferences
        self._usage = usage
        self._overlay = overlay
        self._snapshots = snapshots
        self._cursors = cursors
        self._clock = clock
        self._snapshot_id_factory = snapshot_id_factory
        self._snapshot_ttl = snapshot_ttl
        self._usage_window = usage_window

    async def browse(  # noqa: PLR0911
        self,
        request: QuotaBrowseRequest,
    ) -> OperationResult[QuotaBrowseData]:
        """Read, filter, snapshot, and page one bounded logical quota query."""
        started_at = self._clock.now()
        if request.cursor is not None:
            return self._resume_browse(request, started_at)
        query = request.query
        if query is None:
            return self._browse_rejection(request, started_at, "query-required")
        context = request.context
        if context is None:
            return self._browse_rejection(request, started_at, "context-required")
        if query.resource_scope != context.project.resource_scope:
            return self._browse_rejection(
                request, started_at, "resource-scope-mismatch"
            )

        reads = await self._read_query_sources(context, query)
        effective_reads, preference_read, usage_reads = reads
        complete = all(
            read.complete for read in (*effective_reads, preference_read, *usage_reads)
        )
        diagnostics = tuple(
            diagnostic
            for read in (*effective_reads, preference_read, *usage_reads)
            for diagnostic in read.diagnostics
        )
        evidences = tuple(
            evidence for read in effective_reads for evidence in read.values
        )
        if _has_duplicate_effective_identities(evidences):
            return self._duplicate_browse_result(
                request,
                query,
                started_at,
                diagnostics,
                complete=complete,
                has_partial_data=bool(evidences),
            )
        preferences = preference_read.values
        usages = tuple(value for read in usage_reads for value in read.values)
        service_complete = {
            service: read.complete
            for service, read in zip(query.services, effective_reads, strict=True)
        }
        try:
            items = tuple(
                self._join_item(
                    evidence,
                    evidences=evidences,
                    preferences=preferences,
                    usages=usages,
                    effective_observed_at=_read_observed_at(
                        evidence.identity.service,
                        query.services,
                        effective_reads,
                    ),
                    freshly_validated=service_complete[evidence.identity.service],
                    strict_usage=_query_requires_usage(query),
                )
                for evidence in evidences
            )
        except (TypeError, ValueError):
            return self._browse_rejection(request, started_at, "ambiguous-evidence")

        filtered = tuple(item for item in items if query.filters.matches(item))
        constraint_sets = _constraint_sets(filtered)
        if not complete:
            data = QuotaBrowseData(
                query=query,
                items=filtered,
                constraint_sets=constraint_sets,
                ordered=False,
                total=None,
                next_cursor=None,
                snapshot_id=None,
                reason="incomplete-provider-evidence",
            )
            return self._incomplete_result(
                "quota.list",
                query.resource_scope,
                "logical-page-read",
                data,
                diagnostics,
                started_at=started_at,
                has_partial_data=bool(evidences),
            )

        snapshot_id = self._snapshot_id_factory()
        metadata = QuerySnapshotMetadata(
            snapshot_id=snapshot_id,
            query=query,
            catalog=self._overlay.metadata,
            evidence_contract=QUOTA_QUERY_EVIDENCE_CONTRACT,
            observed_at=started_at,
            expires_at=started_at + self._snapshot_ttl,
            complete=True,
        )
        snapshot = QuotaQuerySnapshot(metadata, filtered)
        try:
            _validate_applicable_sorts(filtered, query)
            ordered = snapshot.sorted_items()
        except (IncompatibleSortUnitsError, _InapplicableSortError):
            return self._browse_rejection(request, started_at, "inapplicable-sort")
        snapshot = QuotaQuerySnapshot(metadata, ordered)
        try:
            self._snapshots.save(snapshot)
        except QuotaSnapshotRepositoryError:
            return self._browse_operational_failure(
                request,
                started_at,
                "snapshot-store-failed",
                diagnostics,
            )
        return self._browse_page(snapshot, request.limit, 0, started_at)

    async def inspect(  # noqa: PLR0911
        self,
        request: QuotaInspectRequest,
    ) -> OperationResult[QuotaInspectData]:
        """Read one exact slice and join only unambiguous authoritative evidence."""
        started_at = self._clock.now()
        if request.identity.resource_scope != request.context.project.resource_scope:
            return self._inspect_rejection(
                request, started_at, "resource-scope-mismatch"
            )
        end = started_at
        effective_read, preference_read, usage_read = await asyncio.gather(
            self._effective.read(
                EffectiveQuotaReadRequest(request.context, request.identity.service)
            ),
            self._preferences.read(QuotaPreferenceReadRequest(request.context)),
            self._usage.read(
                UsageReadRequest(
                    request.context,
                    request.identity.service,
                    end - self._usage_window,
                    end,
                )
            ),
        )
        reads = (effective_read, preference_read, usage_read)
        complete = all(read.complete for read in reads)
        diagnostics = tuple(
            diagnostic for read in reads for diagnostic in read.diagnostics
        )
        matches = tuple(
            evidence
            for evidence in effective_read.values
            if evidence.identity == request.identity
        )
        if len(matches) > 1:
            reason = "duplicate-effective-slice"
            data = QuotaInspectData(
                request.identity, None, None, None, None, None, None, reason
            )
            if not complete:
                return self._incomplete_result(
                    "quota.inspect",
                    request.identity.resource_scope,
                    "exact-slice-inspected",
                    data,
                    diagnostics,
                    started_at=started_at,
                    has_partial_data=False,
                    gap_sources=("effective-quota",),
                    gap_reason=reason,
                )
            return self._result(
                operation="quota.inspect",
                resource_scope=request.identity.resource_scope,
                boundary="exact-slice-inspected",
                reached=False,
                outcome=reason,
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                completeness=Completeness.complete(),
                data=data,
                started_at=started_at,
                diagnostics=diagnostics,
            )
        if not matches:
            reason = "exact-slice-not-found"
            if not complete:
                data = QuotaInspectData(
                    request.identity, None, None, None, None, None, None, reason
                )
                return self._incomplete_result(
                    "quota.inspect",
                    request.identity.resource_scope,
                    "exact-slice-inspected",
                    data,
                    diagnostics,
                    started_at=started_at,
                    has_partial_data=False,
                )
            return self._inspect_rejection(request, started_at, reason)
        evidence = matches[0]
        try:
            preference = _one_exact_preference(evidence, preference_read.values)
            usage = _one_exact_usage(evidence, usage_read.values)
            item, status = self._joined_classification(
                evidence,
                evidences=effective_read.values,
                preferences=preference_read.values,
                usages=usage_read.values,
                effective_observed_at=effective_read.observed_at,
                freshly_validated=effective_read.complete,
            )
        except (TypeError, ValueError):
            return self._inspect_rejection(
                request, started_at, "ambiguous-related-evidence"
            )
        constraint_sets = item.constraint_sets
        data = QuotaInspectData(
            identity=request.identity,
            evidence=evidence,
            item=item,
            preference=preference,
            usage=usage,
            status=status,
            constraint_set=item.constraint_set,
            constraint_sets=constraint_sets,
        )
        if item.predicates.guided and not constraint_sets:
            data = replace(data, reason="constraint-set-incomplete")
            return self._incomplete_result(
                "quota.inspect",
                request.identity.resource_scope,
                "exact-slice-inspected",
                data,
                diagnostics,
                started_at=started_at,
                has_partial_data=True,
                gap_sources=("accelerator-catalog",),
                gap_reason="required-constraint-missing",
            )
        if not complete:
            return self._incomplete_result(
                "quota.inspect",
                request.identity.resource_scope,
                "exact-slice-inspected",
                data,
                diagnostics,
                started_at=started_at,
                has_partial_data=True,
            )
        return self._result(
            operation="quota.inspect",
            resource_scope=request.identity.resource_scope,
            boundary="exact-slice-inspected",
            reached=True,
            outcome="slice-inspected",
            exit_class=ExitClass.SUCCESS,
            completeness=Completeness.complete(),
            data=data,
            started_at=started_at,
            diagnostics=diagnostics,
        )

    async def _read_query_sources(
        self,
        context: ProviderReadContext,
        query: QuotaQuery,
    ) -> tuple[
        tuple[ProviderRead[EffectiveQuotaEvidence], ...],
        ProviderRead[QuotaPreferenceEvidence],
        tuple[ProviderRead[UsageObservation], ...],
    ]:
        end = self._clock.now()
        effective_tasks = tuple(
            asyncio.create_task(
                self._effective.read(EffectiveQuotaReadRequest(context, service))
            )
            for service in query.services
        )
        preference_task = asyncio.create_task(
            self._preferences.read(QuotaPreferenceReadRequest(context))
        )
        usage_tasks = tuple(
            asyncio.create_task(
                self._usage.read(
                    UsageReadRequest(
                        context,
                        service,
                        end - self._usage_window,
                        end,
                    )
                )
            )
            for service in query.services
        )
        await asyncio.gather(*effective_tasks, preference_task, *usage_tasks)
        return (
            tuple(task.result() for task in effective_tasks),
            preference_task.result(),
            tuple(task.result() for task in usage_tasks),
        )

    def _join_item(  # noqa: PLR0913
        self,
        evidence: EffectiveQuotaEvidence,
        *,
        evidences: tuple[EffectiveQuotaEvidence, ...],
        preferences: tuple[QuotaPreferenceEvidence, ...],
        usages: tuple[UsageObservation, ...],
        effective_observed_at: datetime,
        freshly_validated: bool,
        strict_usage: bool,
    ) -> QuotaQueryItem:
        item, _ = self._joined_classification(
            evidence,
            evidences=evidences,
            preferences=preferences,
            usages=usages,
            effective_observed_at=effective_observed_at,
            freshly_validated=freshly_validated,
            strict_usage=strict_usage,
        )
        return item

    def _joined_classification(  # noqa: PLR0913
        self,
        evidence: EffectiveQuotaEvidence,
        *,
        evidences: tuple[EffectiveQuotaEvidence, ...],
        preferences: tuple[QuotaPreferenceEvidence, ...],
        usages: tuple[UsageObservation, ...],
        effective_observed_at: datetime,
        freshly_validated: bool,
        strict_usage: bool = True,
    ) -> tuple[QuotaQueryItem, QuotaRequestStatus | None]:
        mutable = (
            freshly_validated and evidence.eligibility.eligible and not evidence.fixed
        )
        item = self._overlay.classify(
            evidence,
            freshly_validated_mutable=mutable,
        )
        constraint_sets = self._overlay.constraint_sets(evidence, evidences)
        preference = _one_exact_preference(evidence, preferences)
        usage_value = _joined_usage_value(evidence, usages, strict=strict_usage)
        status = _status(evidence, preference, effective_observed_at)
        return (
            replace(
                item,
                usage_value=usage_value,
                desired_value=None if status is None else status.desired,
                granted_value=None if status is None else status.granted,
                reconciliation=(
                    Reconciliation.UNKNOWN if status is None else status.reconciliation
                ),
                grant_satisfaction=(
                    GrantSatisfaction.UNKNOWN
                    if status is None
                    else status.grant_satisfaction
                ),
                effective_confirmation=(
                    EffectiveConfirmation.UNOBSERVED
                    if status is None
                    else status.effective_confirmation
                ),
                evidence_observed_at=effective_observed_at,
                constraint_sets=constraint_sets,
                constraint_set=None,
            ),
            status,
        )

    def _resume_browse(
        self,
        request: QuotaBrowseRequest,
        started_at: datetime,
    ) -> OperationResult[QuotaBrowseData]:
        try:
            resolved = self._cursors.resolve(
                request.cursor or "",
                now=started_at,
                expected_query=request.query,
            )
        except (QuotaCursorError, QuotaSnapshotRepositoryError):
            return self._browse_rejection(request, started_at, "cursor-rejected")
        snapshot = resolved.snapshot
        if (
            request.context is not None
            and snapshot.metadata.query.resource_scope
            != request.context.project.resource_scope
        ) or (not snapshot.metadata.complete or resolved.offset > len(snapshot.items)):
            return self._browse_rejection(request, started_at, "cursor-rejected")
        return self._browse_page(snapshot, request.limit, resolved.offset, started_at)

    def _browse_page(
        self,
        snapshot: QuotaQuerySnapshot,
        limit: int,
        offset: int,
        started_at: datetime,
    ) -> OperationResult[QuotaBrowseData]:
        end = min(offset + limit, len(snapshot.items))
        items = snapshot.items[offset:end]
        try:
            next_cursor = (
                self._cursors.issue(
                    snapshot.metadata.snapshot_id,
                    end,
                    now=started_at,
                ).value
                if end < len(snapshot.items)
                else None
            )
        except QuotaSnapshotRepositoryError:
            data = QuotaBrowseData(
                query=snapshot.metadata.query,
                items=items,
                constraint_sets=_constraint_sets(items),
                ordered=True,
                total=len(snapshot.items),
                next_cursor=None,
                snapshot_id=snapshot.metadata.snapshot_id,
                reason="cursor-issue-failed",
            )
            return self._result(
                operation="quota.list",
                resource_scope=snapshot.metadata.query.resource_scope,
                boundary="logical-page-read",
                reached=False,
                outcome="cursor-issue-failed",
                exit_class=ExitClass.OPERATIONAL_FAILURE,
                completeness=Completeness.complete(),
                data=data,
                started_at=started_at,
            )
        data = QuotaBrowseData(
            query=snapshot.metadata.query,
            items=items,
            constraint_sets=_constraint_sets(items),
            ordered=True,
            total=len(snapshot.items),
            next_cursor=next_cursor,
            snapshot_id=snapshot.metadata.snapshot_id,
        )
        return self._result(
            operation="quota.list",
            resource_scope=snapshot.metadata.query.resource_scope,
            boundary="logical-page-read",
            reached=True,
            outcome="page-read",
            exit_class=ExitClass.SUCCESS,
            completeness=Completeness.complete(),
            data=data,
            started_at=started_at,
        )

    def _browse_rejection(
        self,
        request: QuotaBrowseRequest,
        started_at: datetime,
        reason: str,
    ) -> OperationResult[QuotaBrowseData]:
        data = QuotaBrowseData(
            query=request.query,
            items=(),
            constraint_sets=(),
            ordered=False,
            total=None,
            next_cursor=None,
            snapshot_id=None,
            reason=reason,
        )
        return self._result(
            operation="quota.list",
            resource_scope=(
                request.query.resource_scope
                if request.query is not None
                else (
                    None
                    if request.context is None
                    else request.context.project.resource_scope
                )
            ),
            boundary="logical-page-read",
            reached=False,
            outcome=reason,
            exit_class=ExitClass.REJECTED_PRECONDITION,
            completeness=Completeness.complete(),
            data=data,
            started_at=started_at,
        )

    def _browse_operational_failure(
        self,
        request: QuotaBrowseRequest,
        started_at: datetime,
        reason: str,
        diagnostics: tuple[Diagnostic, ...],
    ) -> OperationResult[QuotaBrowseData]:
        data = QuotaBrowseData(
            query=request.query,
            items=(),
            constraint_sets=(),
            ordered=False,
            total=None,
            next_cursor=None,
            snapshot_id=None,
            reason=reason,
        )
        return self._result(
            operation="quota.list",
            resource_scope=(
                request.query.resource_scope
                if request.query is not None
                else (
                    None
                    if request.context is None
                    else request.context.project.resource_scope
                )
            ),
            boundary="logical-page-read",
            reached=False,
            outcome=reason,
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            completeness=Completeness.complete(),
            data=data,
            started_at=started_at,
            diagnostics=diagnostics,
        )

    def _duplicate_browse_result(  # noqa: PLR0913
        self,
        request: QuotaBrowseRequest,
        query: QuotaQuery,
        started_at: datetime,
        diagnostics: tuple[Diagnostic, ...],
        *,
        complete: bool,
        has_partial_data: bool,
    ) -> OperationResult[QuotaBrowseData]:
        reason = "duplicate-effective-slice"
        if complete:
            return self._browse_operational_failure(
                request,
                started_at,
                reason,
                diagnostics,
            )
        data = QuotaBrowseData(
            query=query,
            items=(),
            constraint_sets=(),
            ordered=False,
            total=None,
            next_cursor=None,
            snapshot_id=None,
            reason=reason,
        )
        return self._incomplete_result(
            "quota.list",
            query.resource_scope,
            "logical-page-read",
            data,
            diagnostics,
            started_at=started_at,
            has_partial_data=has_partial_data,
            gap_sources=("effective-quota",),
            gap_reason=reason,
        )

    def _inspect_rejection(
        self,
        request: QuotaInspectRequest,
        started_at: datetime,
        reason: str,
    ) -> OperationResult[QuotaInspectData]:
        data = QuotaInspectData(
            request.identity, None, None, None, None, None, None, reason
        )
        return self._result(
            operation="quota.inspect",
            resource_scope=request.identity.resource_scope,
            boundary="exact-slice-inspected",
            reached=False,
            outcome=reason,
            exit_class=ExitClass.REJECTED_PRECONDITION,
            completeness=Completeness.complete(),
            data=data,
            started_at=started_at,
        )

    def _incomplete_result[DataT](  # noqa: PLR0913
        self,
        operation: str,
        resource_scope: ResourceScope | None,
        boundary: str,
        data: DataT,
        diagnostics: tuple[Diagnostic, ...],
        *,
        started_at: datetime,
        has_partial_data: bool,
        gap_sources: tuple[str, ...] = (),
        gap_reason: str = "provider-read-incomplete",
    ) -> OperationResult[DataT]:
        gaps = tuple(
            EvidenceGap(StableSymbol(source), StableSymbol(gap_reason))
            for source in sorted(
                set(gap_sources)
                or {diagnostic.source.value for diagnostic in diagnostics}
                or {"provider"}
            )
        )
        completeness = (
            Completeness.incomplete(*gaps)
            if has_partial_data
            else Completeness.unavailable(*gaps)
        )
        return self._result(
            operation=operation,
            resource_scope=resource_scope,
            boundary=boundary,
            reached=False,
            outcome="incomplete-evidence",
            exit_class=(
                ExitClass.INCOMPLETE_EVIDENCE
                if has_partial_data
                else ExitClass.OPERATIONAL_FAILURE
            ),
            completeness=completeness,
            data=data,
            started_at=started_at,
            diagnostics=diagnostics,
        )

    def _result[DataT](  # noqa: PLR0913
        self,
        *,
        operation: str,
        resource_scope: ResourceScope | None,
        boundary: str,
        reached: bool,
        outcome: str,
        exit_class: ExitClass,
        completeness: Completeness,
        data: DataT,
        started_at: datetime,
        diagnostics: tuple[Diagnostic, ...] = (),
    ) -> OperationResult[DataT]:
        if resource_scope is not None and not isinstance(resource_scope, ResourceScope):
            msg = "quota operation result requires ResourceScope or None"
            raise TypeError(msg)
        return OperationResult(
            operation=OperationName(operation),
            resource_scope=resource_scope,
            boundary=OperationBoundary(StableSymbol(boundary), reached),
            outcome=Outcome(StableSymbol(outcome), exit_class),
            completeness=completeness,
            started_at=started_at,
            finished_at=self._clock.now(),
            data=data,
            diagnostics=diagnostics,
        )


def _one_exact_preference(
    evidence: EffectiveQuotaEvidence,
    preferences: tuple[QuotaPreferenceEvidence, ...],
) -> QuotaPreferenceEvidence | None:
    matches = tuple(item for item in preferences if item.identity == evidence.identity)
    if len(matches) > 1:
        msg = "more than one provider preference matches the exact quota slice"
        raise _AmbiguousEvidenceError(msg)
    return matches[0] if matches else None


def _one_exact_usage(
    evidence: EffectiveQuotaEvidence,
    usages: tuple[UsageObservation, ...],
) -> UsageObservation | None:
    locations = _exact_usage_locations(evidence)
    matches = []
    for usage in usages:
        metric_labels = dict(usage.metric_labels.items)
        resource_labels = dict(usage.resource_labels.items)
        if (
            usage.resource_scope == evidence.identity.resource_scope
            and metric_labels.get("quota_metric") == evidence.metric
            and resource_labels.get("service") == evidence.identity.service
            and resource_labels.get("location") in locations
        ):
            matches.append(usage)
    if len(matches) > 1:
        msg = "more than one usage series matches the exact quota slice"
        raise _AmbiguousEvidenceError(msg)
    return matches[0] if matches else None


def _exact_usage_locations(evidence: EffectiveQuotaEvidence) -> set[str]:
    dimensions = dict(evidence.identity.dimensions.items)
    scope_dimension = {
        QuotaScope.REGIONAL: "region",
        QuotaScope.ZONAL: "zone",
    }.get(evidence.identity.quota_scope)
    if scope_dimension is not None:
        location = dimensions.get(scope_dimension)
        if location is None:
            msg = "quota scope requires an exact location dimension"
            raise _AmbiguousEvidenceError(msg)
        return {location}

    dimension_locations = tuple(
        value
        for key, value in dimensions.items()
        if key in {"location", "region", "zone"}
    )
    if evidence.identity.quota_scope is QuotaScope.GLOBAL:
        if dimension_locations:
            msg = "global quota slice cannot carry a location dimension"
            raise _AmbiguousEvidenceError(msg)
    elif len(dimension_locations) > 1:
        msg = "unknown quota scope has ambiguous location dimensions"
        raise _AmbiguousEvidenceError(msg)
    elif dimension_locations:
        return {dimension_locations[0]}
    return set(evidence.applicable_locations)


def _usage_quantity(
    evidence: EffectiveQuotaEvidence,
    usage: UsageObservation | None,
) -> QuotaQuantity | None:
    if usage is None:
        return None
    if usage.unit != evidence.effective_value.unit.symbol or not usage.points:
        msg = "usage unit or points cannot be joined to the exact quota slice"
        raise _AmbiguousEvidenceError(msg)
    point = max(usage.points, key=lambda value: value.interval_end)
    if point.value.kind is not MonitoringValueKind.INT64:
        msg = "quota usage requires an authoritative INT64 point"
        raise _AmbiguousEvidenceError(msg)
    if point.value.value < 0:  # type: ignore[operator]
        msg = "quota usage requires a non-negative authoritative point"
        raise _AmbiguousEvidenceError(msg)
    return QuotaQuantity(point.value.value, evidence.effective_value.unit)  # type: ignore[arg-type]


def _joined_usage_value(
    evidence: EffectiveQuotaEvidence,
    usages: tuple[UsageObservation, ...],
    *,
    strict: bool,
) -> QuotaQuantity | None:
    """Join usage strictly when requested, otherwise omit untrustworthy evidence."""
    try:
        return _usage_quantity(evidence, _one_exact_usage(evidence, usages))
    except _AmbiguousEvidenceError:
        if strict:
            raise
        return None


def _quota_constraint_assessments(
    resolved: ResolvedQuotaRequirement,
    evidences: tuple[EffectiveQuotaEvidence, ...],
    usages: tuple[UsageObservation, ...],
) -> tuple[QuotaConstraintAssessment, ...]:
    """Assess every exact limiting slice from authoritative native-unit usage."""
    assessments = []
    for reference in resolved.constraint_set.references:
        matches = tuple(
            evidence
            for evidence in evidences
            if evidence.identity == reference.slice_identity
        )
        if len(matches) != 1:
            msg = "constraint assessment requires one exact effective quota slice"
            raise _AmbiguousEvidenceError(msg)
        evidence = matches[0]
        usage = _usage_quantity(evidence, _one_exact_usage(evidence, usages))
        if usage is None:
            msg = "constraint assessment requires exact authoritative usage"
            raise _AmbiguousEvidenceError(msg)
        effective = evidence.effective_value
        required = resolved.required_amount
        assessments.append(
            QuotaConstraintAssessment(
                reference.slice_identity,
                effective,
                usage,
                required,
                usage.value + required.value <= effective.value,
            )
        )
    return tuple(assessments)


def _status(
    evidence: EffectiveQuotaEvidence,
    preference: QuotaPreferenceEvidence | None,
    effective_observed_at: datetime,
) -> QuotaRequestStatus | None:
    if preference is None or preference.update_time is None:
        return None
    desired = QuotaQuantity(preference.preferred_value, evidence.effective_value.unit)
    granted = (
        None
        if preference.granted_value is None
        else QuotaQuantity(preference.granted_value, evidence.effective_value.unit)
    )
    return QuotaRequestStatus.derive(
        reconciliation=(
            Reconciliation.RECONCILING
            if preference.reconciling
            else Reconciliation.SETTLED
        ),
        baseline=None,
        desired=desired,
        granted=granted,
        effective=evidence.effective_value,
        status_observed_at=preference.update_time,
        effective_observed_at=effective_observed_at,
    )


def _read_observed_at(
    service: str,
    services: tuple[str, ...],
    reads: tuple[ProviderRead[EffectiveQuotaEvidence], ...],
) -> datetime:
    return reads[services.index(service)].observed_at


def _validate_applicable_sorts(
    items: tuple[QuotaQueryItem, ...],
    query: QuotaQuery,
) -> None:
    if not items:
        return
    always_applicable = {
        QuotaSortField.QUOTA_ID,
        QuotaSortField.SERVICE,
        QuotaSortField.QUOTA_SCOPE,
    }
    for sort in query.sort:
        if sort.field in always_applicable:
            continue
        if not any(_sort_value(item, sort.field) is not None for item in items):
            msg = f"sort field {sort.field.value} is inapplicable to the result"
            raise _InapplicableSortError(msg)


def _sort_value(item: QuotaQueryItem, field: QuotaSortField) -> object | None:
    values = {
        QuotaSortField.DISPLAY_NAME: item.display_name,
        QuotaSortField.ACCELERATOR: item.accelerator_id,
        QuotaSortField.LOCATION: item.location,
        QuotaSortField.QUOTA_POOL: item.quota_pool,
        QuotaSortField.EFFECTIVE: item.effective_value,
        QuotaSortField.USAGE: item.usage_value,
        QuotaSortField.DESIRED: item.desired_value,
        QuotaSortField.GRANTED: item.granted_value,
        QuotaSortField.RECONCILIATION: item.reconciliation,
        QuotaSortField.GRANT_SATISFACTION: item.grant_satisfaction,
        QuotaSortField.EFFECTIVE_CONFIRMATION: item.effective_confirmation,
        QuotaSortField.EVIDENCE_AGE: item.evidence_observed_at,
    }
    return values.get(field)


def _query_requires_usage(query: QuotaQuery) -> bool:
    """Whether the query's requested output semantics depend on usage evidence."""
    return any(sort.field is QuotaSortField.USAGE for sort in query.sort)


def _has_duplicate_effective_identities(
    evidences: tuple[EffectiveQuotaEvidence, ...],
) -> bool:
    """Whether provider evidence repeats any exact effective quota identity."""
    identities = tuple(evidence.identity for evidence in evidences)
    return len(set(identities)) != len(identities)


def _constraint_sets(
    items: tuple[QuotaQueryItem, ...],
) -> tuple[AcceleratorConstraintSet, ...]:
    """Retain each distinct anchored constraint set represented on a page."""
    return tuple(
        dict.fromkeys(
            constraint_set for item in items for constraint_set in item.constraint_sets
        )
    )


def _resolution_diagnostics(
    quota_diagnostics: tuple[Diagnostic, ...],
    catalog_diagnostics: tuple[Diagnostic, ...],
    coverage: tuple[CatalogLocationCoverage, ...],
) -> tuple[Diagnostic, ...]:
    """Combine source and location diagnostics without duplicating evidence."""
    return tuple(
        dict.fromkeys(
            (
                *quota_diagnostics,
                *catalog_diagnostics,
                *(diagnostic for item in coverage for diagnostic in item.diagnostics),
            )
        )
    )


def _required_catalog_gaps(
    requirement: GpuWorkloadRequirement | TpuWorkloadRequirement,
    catalog: WorkloadCatalogEvidence,
) -> tuple[EvidenceGap, ...]:
    """Identify only missing selected-location catalog evidence as blocking."""
    if isinstance(requirement, GpuWorkloadRequirement) or (
        requirement.management_plane is ManagementPlane.COMPUTE
    ):
        required = ((CatalogEvidenceSource.COMPUTE_MACHINE_TYPES, requirement.zone),)
    else:
        required = tuple(
            (source, requirement.zone)
            for source in (
                CatalogEvidenceSource.TPU_LOCATIONS,
                CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
                CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
            )
        )
    return tuple(
        EvidenceGap(
            StableSymbol(source.value),
            StableSymbol(ResolutionFailureReason.MISSING_LOCATION_EVIDENCE.value),
        )
        for source, location in required
        if len(
            records := tuple(
                coverage
                for coverage in catalog.coverage
                if coverage.source is source and coverage.location == location
            )
        )
        != 1
        or not records[0].complete
    )
