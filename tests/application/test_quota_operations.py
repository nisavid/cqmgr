"""Surface-neutral quota browse, inspection, and resolution contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.application.operations.quotas import (
    QuotaBrowseRequest,
    QuotaInspectRequest,
    QuotaOperations,
    QuotaResolveRequest,
    WorkloadResolutionOperations,
)
from cqmgr.application.ports.catalog_reads import CatalogRead
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.application.ports.provider_reads import (
    EffectiveQuotaReadRequest,
    ProviderReadContext,
)
from cqmgr.application.ports.quota_snapshots import (
    QuotaCursorQueryMismatchError,
    QuotaSnapshotOperationalError,
    ResolvedQuotaQueryCursor,
    UnknownQuotaCursorError,
)
from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogGroupId,
    CatalogLocationCoverage,
    CatalogMetadata,
    CatalogPredicates,
    ComputeAcceleratorType,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
    TpuAcceleratorConfig,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.identity import ADCIdentityEvidence, CredentialKind
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.quota_queries import (
    OpaqueQueryCursor,
    ProviderSourceCoverageState,
    QuotaQuery,
    QuotaQueryFilters,
    QuotaQueryItem,
    QuotaQuerySnapshot,
    QuotaSort,
    QuotaSortField,
)
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    MonitoringPoint,
    MonitoringValue,
    MonitoringValueKind,
    NormalizedDimensions,
    ProviderRead,
    ProviderReadCoverage,
    QuotaContainerType,
    QuotaIncreaseEligibility,
    QuotaIneligibilityReason,
    QuotaPreferenceEvidence,
    QuotaPreferenceOrigin,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
    UsageObservation,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import ExitClass, StableSymbol
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    Reconciliation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cqmgr.application.ports.catalog_reads import (
        TpuAcceleratorTypeReadRequest,
        TpuRuntimeVersionReadRequest,
    )
    from cqmgr.application.ports.provider_reads import (
        EffectiveQuotaReader,
        QuotaPreferenceReader,
        QuotaPreferenceReadRequest,
        UsageReader,
    )
    from cqmgr.domain.accelerator_overlay import SemanticAcceleratorOverlay

NOW = datetime(2026, 7, 22, 9, tzinfo=UTC)
PROJECT = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
UNIT = QuotaUnit("1")
OTHER_UNIT = QuotaUnit("{cores}")
ACCELERATOR = AcceleratorId("nvidia-h100")


class FixedClock:
    """Deterministic application clock."""

    def now(self) -> datetime:
        """Return the fixed test time."""
        return NOW


class ScriptedReader[ValueT]:
    """One scripted provider port with a visible call ledger."""

    def __init__(self, reads: Sequence[ProviderRead[ValueT]]) -> None:
        """Retain the scripted responses and an empty call ledger."""
        self.reads = list(reads)
        self.calls: list[object] = []

    async def read(self, request: object) -> ProviderRead[ValueT]:
        """Return the next scripted read without provider access."""
        self.calls.append(request)
        return self.reads.pop(0)


class ScriptedCatalogReader[ValueT]:
    """One scripted catalog port with a visible call ledger."""

    def __init__(self, reads: Sequence[CatalogRead[ValueT]]) -> None:
        """Retain scripted catalog responses and an empty call ledger."""
        self.reads = list(reads)
        self.calls: list[object] = []

    async def read(self, request: object) -> CatalogRead[ValueT]:
        """Return the next scripted catalog read without provider access."""
        self.calls.append(request)
        return self.reads.pop(0)


class MemorySnapshots:
    """Installation-local immutable snapshot fake."""

    def __init__(self) -> None:
        """Start with no retained snapshots."""
        self.snapshots: dict[str, QuotaQuerySnapshot] = {}

    def save(self, snapshot: QuotaQuerySnapshot) -> None:
        """Retain one snapshot by identity."""
        self.snapshots[snapshot.metadata.snapshot_id] = snapshot

    def load(
        self,
        snapshot_id: str,
        *,
        now: datetime,
        expected_query: QuotaQuery | None = None,
    ) -> QuotaQuerySnapshot:
        """Load and validate one retained snapshot."""
        snapshot = self.snapshots[snapshot_id]
        if snapshot.metadata.expires_at <= now:
            msg = "expired"
            raise UnknownQuotaCursorError(msg)
        if expected_query is not None and snapshot.metadata.query != expected_query:
            msg = "mismatch"
            raise QuotaCursorQueryMismatchError(msg)
        return snapshot


class MemoryCursors:
    """Opaque cursor fake bound to the in-memory snapshot repository."""

    def __init__(self, snapshots: MemorySnapshots) -> None:
        """Bind cursor handles to the in-memory snapshot repository."""
        self.snapshots = snapshots
        self.cursors: dict[str, tuple[str, int]] = {}
        self.resolve_error: Exception | None = None
        self.issue_error: Exception | None = None

    def issue(
        self,
        snapshot_id: str,
        offset: int,
        *,
        now: datetime,
    ) -> OpaqueQueryCursor:
        """Issue one opaque handle without exposing snapshot state."""
        del now
        if self.issue_error is not None:
            raise self.issue_error
        value = f"opaque-{len(self.cursors) + 1}"
        self.cursors[value] = (snapshot_id, offset)
        return OpaqueQueryCursor(value, snapshot_id, offset)

    def resolve(
        self,
        cursor: str,
        *,
        now: datetime,
        expected_query: QuotaQuery | None = None,
    ) -> ResolvedQuotaQueryCursor:
        """Resolve a handle or raise the configured cursor rejection."""
        if self.resolve_error is not None:
            raise self.resolve_error
        try:
            snapshot_id, offset = self.cursors[cursor]
        except KeyError as error:
            msg = "unknown"
            raise UnknownQuotaCursorError(msg) from error
        snapshot = self.snapshots.load(
            snapshot_id,
            now=now,
            expected_query=expected_query,
        )
        return ResolvedQuotaQueryCursor(snapshot, offset)


class ScriptedOverlay:
    """Minimal semantic overlay fake preserving independent product facts."""

    metadata = CatalogMetadata(
        "cqmgr.accelerator-catalog/v1",
        "2026-07-22",
        f"sha256:{'0' * 64}",
    )

    def classify(
        self,
        evidence: EffectiveQuotaEvidence,
        *,
        freshly_validated_mutable: bool,
    ) -> QuotaQueryItem:
        """Classify one known quota and leave every other slice generic."""
        known = evidence.identity.quota_id.startswith("known-")
        dimensions = dict(evidence.identity.dimensions.items)
        return QuotaQueryItem(
            identity=evidence.identity,
            display_name=evidence.quota_display_name,
            accelerator_id=ACCELERATOR if known else None,
            location=dimensions.get("region"),
            quota_pool="standard" if known else None,
            predicates=CatalogPredicates(
                discovered=True,
                cataloged=known,
                guided=known,
                mutable=freshly_validated_mutable,
            ),
            effective_value=evidence.effective_value,
        )

    def constraint_set(
        self,
        accelerator_id: AcceleratorId,
        anchor: EffectiveQuotaEvidence,
        evidences: tuple[EffectiveQuotaEvidence, ...],
    ) -> AcceleratorConstraintSet | None:
        """Return same-location exact references anchored to one primary slice."""
        if accelerator_id != ACCELERATOR:
            return None
        if anchor.identity.quota_id == "known-needs-companion" and not any(
            item.identity.quota_id == "known-companion" for item in evidences
        ):
            return None
        anchor_regions = dict(anchor.identity.dimensions.items)
        return AcceleratorConstraintSet(
            accelerator_id,
            tuple(
                ConstraintReference(item.identity)
                for item in evidences
                if dict(item.identity.dimensions.items).get("region")
                == anchor_regions.get("region")
            ),
        )

    def constraint_sets(
        self,
        evidence: EffectiveQuotaEvidence,
        evidences: tuple[EffectiveQuotaEvidence, ...],
    ) -> tuple[AcceleratorConstraintSet, ...]:
        """Return the fake's one anchored relationship when it is known."""
        if not evidence.identity.quota_id.startswith("known-"):
            return ()
        constraint_set = self.constraint_set(ACCELERATOR, evidence, evidences)
        return () if constraint_set is None else (constraint_set,)


@dataclass
class OperationFixture:
    """Quota operation and its observable test doubles."""

    operations: QuotaOperations
    effective: ScriptedReader[EffectiveQuotaEvidence]
    preferences: ScriptedReader[QuotaPreferenceEvidence]
    usage: ScriptedReader[UsageObservation]
    snapshots: MemorySnapshots
    cursors: MemoryCursors


def _context() -> ProviderReadContext:
    return ProviderReadContext(
        project=CanonicalProject(PROJECT, "public-schema-project", "Public Project"),
        identity=ADCIdentityEvidence.principal_unverified(
            credential_kind=CredentialKind.UNKNOWN,
            adc_quota_project=None,
        ),
        deadline=100.0,
        cancellation=CancellationToken(),
    )


def _effective_service(request: object) -> str:
    assert isinstance(request, EffectiveQuotaReadRequest)
    return request.service


def _identity(
    quota_id: str,
    *,
    unit: QuotaUnit = UNIT,
    region: str = "us-central1",
    service: str = "compute.googleapis.com",
) -> EffectiveQuotaSliceIdentity:
    del unit
    return EffectiveQuotaSliceIdentity(
        PROJECT,
        service,
        quota_id,
        NormalizedDimensions((("region", region),)),
        QuotaScope.REGIONAL,
    )


def _evidence(  # noqa: PLR0913
    quota_id: str,
    *,
    value: int = 8,
    unit: QuotaUnit = UNIT,
    eligible: bool = True,
    fixed: bool = False,
    service: str = "compute.googleapis.com",
) -> EffectiveQuotaEvidence:
    return EffectiveQuotaEvidence(
        identity=_identity(quota_id, unit=unit, service=service),
        effective_value=QuotaQuantity(value, unit),
        metric=f"{service}/{quota_id}",
        declared_dimensions=("region",),
        applicable_locations=("us-central1",),
        eligibility=QuotaIncreaseEligibility(
            eligible,
            ProviderSymbol("OTHER", QuotaIneligibilityReason),
        ),
        fixed=fixed,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
        quota_display_name=quota_id,
    )


def _preference(identity: EffectiveQuotaSliceIdentity) -> QuotaPreferenceEvidence:
    return QuotaPreferenceEvidence(
        provider_name="projects/123456789/locations/global/quotaPreferences/public",
        identity=identity,
        preferred_value=8,
        granted_value=8,
        etag="public-etag",
        reconciling=False,
        state_detail=None,
        trace_id="public-trace",
        create_time=NOW - timedelta(minutes=2),
        update_time=NOW - timedelta(minutes=1),
        request_origin=ProviderSymbol("CLOUD_CONSOLE", QuotaPreferenceOrigin),
    )


def _usage(evidence: EffectiveQuotaEvidence, value: int = 3) -> UsageObservation:
    return UsageObservation(
        resource_scope=PROJECT,
        metric_type="serviceruntime.googleapis.com/quota/allocation/usage",
        metric_labels=NormalizedDimensions((("quota_metric", evidence.metric),)),
        resource_type="consumer_quota",
        resource_labels=NormalizedDimensions(
            (
                ("location", evidence.applicable_locations[0]),
                ("project_id", "public-schema-project"),
                ("service", evidence.identity.service),
            )
        ),
        points=(
            MonitoringPoint(
                interval_start=NOW - timedelta(minutes=5),
                interval_end=NOW,
                value=MonitoringValue(MonitoringValueKind.INT64, value),
            ),
        ),
        unit=evidence.effective_value.unit.symbol,
    )


def _complete[ValueT](*values: ValueT) -> ProviderRead[ValueT]:
    return ProviderRead(tuple(values), ProviderReadCoverage(1, 1), NOW)


def _incomplete[ValueT](*values: ValueT) -> ProviderRead[ValueT]:
    diagnostic = Diagnostic(
        DiagnosticCode("provider-page-cap-reached"),
        Severity.WARNING,
        DiagnosticPhase("effective-quota-read"),
        DiagnosticSource("cloud-quotas"),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("The bounded provider read retained partial evidence."),
    )
    return ProviderRead(
        tuple(values),
        ProviderReadCoverage(1, 1, page_cap_reached=True),
        NOW,
        (diagnostic,),
    )


def _fixture(
    effective_reads: Sequence[ProviderRead[EffectiveQuotaEvidence]],
    *,
    preferences: ProviderRead[QuotaPreferenceEvidence] | None = None,
    usage: ProviderRead[UsageObservation] | None = None,
    overlay: object | None = None,
) -> OperationFixture:
    effective = ScriptedReader(effective_reads)
    preference_reader = ScriptedReader((preferences or _complete(),))
    usage_reader = ScriptedReader(
        tuple((usage or _complete()) for _ in effective_reads)
    )
    snapshots = MemorySnapshots()
    cursors = MemoryCursors(snapshots)
    operations = QuotaOperations(
        cast("EffectiveQuotaReader", effective),
        cast("QuotaPreferenceReader", preference_reader),
        cast("UsageReader", usage_reader),
        cast(
            "SemanticAcceleratorOverlay",
            ScriptedOverlay() if overlay is None else overlay,
        ),
        snapshots,
        cursors,
        FixedClock(),
        snapshot_id_factory=lambda: f"snapshot-{len(snapshots.snapshots) + 1}",
    )
    return OperationFixture(
        operations,
        effective,
        preference_reader,
        usage_reader,
        snapshots,
        cursors,
    )


def _query(*sorts: QuotaSort) -> QuotaQuery:
    return QuotaQuery(
        PROJECT,
        filters=QuotaQueryFilters(services=("compute",)),
        sort=sorts,
    )


def _gpu_requirement() -> ComputeInstanceRequirement:
    return ComputeInstanceRequirement(
        machine_type="a3-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )


def _gpu_quota_evidence() -> tuple[EffectiveQuotaEvidence, EffectiveQuotaEvidence]:
    regional = replace(
        _evidence("GPUS-PER-GPU-FAMILY-per-project-region"),
        identity=EffectiveQuotaSliceIdentity(
            PROJECT,
            "compute.googleapis.com",
            "GPUS-PER-GPU-FAMILY-per-project-region",
            NormalizedDimensions(
                (("gpu_family", "NVIDIA_H100"), ("region", "us-central1"))
            ),
            QuotaScope.REGIONAL,
        ),
        applicable_locations=("us-central1",),
        declared_dimensions=("gpu_family", "region"),
    )
    global_ = replace(
        _evidence("GPUS-ALL-REGIONS-per-project"),
        identity=EffectiveQuotaSliceIdentity(
            PROJECT,
            "compute.googleapis.com",
            "GPUS-ALL-REGIONS-per-project",
            NormalizedDimensions(()),
            QuotaScope.GLOBAL,
        ),
        applicable_locations=("global",),
        declared_dimensions=(),
    )
    return regional, global_


def _catalog_diagnostic() -> Diagnostic:
    return Diagnostic(
        DiagnosticCode("unrelated-location-failed"),
        Severity.WARNING,
        DiagnosticPhase("catalog-read"),
        DiagnosticSource("compute-machine-types"),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("An unrelated location could not be read."),
    )


def _gpu_catalog_read() -> CatalogRead[ComputeMachineType]:
    machine = ComputeMachineType(
        name="a3-highgpu-8g",
        zone="us-central1-a",
        guest_accelerators=(AcceleratorAttachment("nvidia-h100-80gb", 8),),
        lifecycle=None,
    )
    diagnostic = _catalog_diagnostic()
    return CatalogRead(
        _complete(machine),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-east1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.FAILED,
                (diagnostic,),
            ),
        ),
    )


def _compute_accelerator_catalog_read(
    accelerator_type: str = "nvidia-h100-80gb",
    zone: str = "us-central1-a",
) -> CatalogRead[ComputeAcceleratorType]:
    return CatalogRead(
        _complete(ComputeAcceleratorType(accelerator_type, zone, None)),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                zone,
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
    )


def _legacy_tpu_requirement() -> CloudTpuSliceRequirement:
    return CloudTpuSliceRequirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-b",)),
    )


def _compute_tpu_requirement() -> ComputeInstanceRequirement:
    return ComputeInstanceRequirement(
        machine_type="ct6e-standard-4t",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-b",)),
    )


def _compute_tpu_quota_evidence() -> EffectiveQuotaEvidence:
    return replace(
        _evidence("provider-discovered-ct6e-id"),
        identity=EffectiveQuotaSliceIdentity(
            PROJECT,
            "compute.googleapis.com",
            "provider-discovered-ct6e-id",
            NormalizedDimensions((("region", "us-central1"), ("tpu_family", "CT6E"))),
            QuotaScope.REGIONAL,
        ),
        applicable_locations=("us-central1",),
        declared_dimensions=("region", "tpu_family"),
        quota_display_name="TPUs per TPU family",
    )


def _compute_tpu_catalog_read() -> CatalogRead[ComputeMachineType]:
    return CatalogRead(
        _complete(
            ComputeMachineType(
                "ct6e-standard-4t",
                "us-central1-b",
                (AcceleratorAttachment("tpu-v6e", 4),),
                None,
            )
        ),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-b",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
    )


def _legacy_tpu_quota_evidence(
    zone: str = "us-central1-b",
) -> EffectiveQuotaEvidence:
    return replace(
        _evidence("provider-discovered-v6e-id", unit=QuotaUnit("core")),
        identity=EffectiveQuotaSliceIdentity(
            PROJECT,
            "tpu.googleapis.com",
            "provider-discovered-v6e-id",
            NormalizedDimensions((("zone", zone),)),
            QuotaScope.ZONAL,
        ),
        metric="tpu.googleapis.com/provider-discovered-v6e-id",
        declared_dimensions=("zone",),
        applicable_locations=(zone,),
        quota_display_name="TPU v6e cores per project per zone",
    )


def _coverage(
    source: CatalogEvidenceSource,
    zone: str = "us-central1-b",
) -> CatalogLocationCoverage:
    return CatalogLocationCoverage(
        source,
        zone,
        LocationCoverageExpectation.REQUESTED,
        LocationCoverageState.SUCCESS,
    )


def _legacy_tpu_catalog_reads(
    zone: str = "us-central1-b",
) -> tuple[
    CatalogRead[TpuLocation],
    CatalogRead[TpuAcceleratorType],
    CatalogRead[TpuRuntimeVersion],
]:
    location = TpuLocation(
        f"projects/123456789/locations/{zone}",
        zone,
    )
    accelerator = TpuAcceleratorType(
        f"projects/123456789/locations/{zone}/acceleratorTypes/v6e-8",
        zone,
        "v6e-8",
        (TpuAcceleratorConfig("V6E", "2x4"),),
    )
    runtime = TpuRuntimeVersion(
        f"projects/123456789/locations/{zone}/runtimeVersions/tpu-vm-base",
        zone,
        "tpu-vm-base",
    )
    return (
        CatalogRead(
            _complete(location),
            (_coverage(CatalogEvidenceSource.TPU_LOCATIONS, zone),),
        ),
        CatalogRead(
            _complete(accelerator),
            (_coverage(CatalogEvidenceSource.TPU_ACCELERATOR_TYPES, zone),),
        ),
        CatalogRead(
            _complete(runtime),
            (_coverage(CatalogEvidenceSource.TPU_RUNTIME_VERSIONS, zone),),
        ),
    )


def test_resolve_gpu_retains_unrelated_catalog_failures() -> None:
    """Selected-location success is independent of unrelated catalog failures."""
    regional, global_ = (
        replace(evidence, effective_value=QuotaQuantity(64, UNIT))
        for evidence in _gpu_quota_evidence()
    )
    effective = ScriptedReader((_complete(regional, global_),))
    usage = ScriptedReader((_complete(_usage(regional, 55), _usage(global_, 60)),))
    compute = ScriptedCatalogReader((_gpu_catalog_read(),))
    compute_accelerators = ScriptedCatalogReader((_compute_accelerator_catalog_read(),))
    tpu_locations = ScriptedCatalogReader(())
    tpu_accelerators = ScriptedCatalogReader(())
    tpu_runtimes = ScriptedCatalogReader(())
    operations = WorkloadResolutionOperations(
        effective,
        usage,
        compute_accelerators,
        compute,
        tpu_locations,
        tpu_accelerators,
        tpu_runtimes,
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _gpu_requirement()))
    )

    assert result.succeeded
    assert result.data is not None
    location = result.data.locations[0]
    assert location.owning_service == "compute.googleapis.com"
    assert tuple(item.required for item in location.constraint_requirements) == (
        QuotaQuantity(8, UNIT),
        QuotaQuantity(8, UNIT),
    )
    assert tuple(item.source_quantity for item in location.constraint_requirements) == (
        8,
        8,
    )
    assert tuple(item.identity for item in location.assessments) == (
        global_.identity,
        regional.identity,
    )
    assert tuple(item.permits for item in location.assessments) == (False, True)
    assert location.permits is False
    assert result.diagnostics == (_catalog_diagnostic(),)
    assert len(compute_accelerators.calls) == 1
    assert len(compute.calls) == 1
    assert tpu_locations.calls == []
    assert tpu_accelerators.calls == []
    assert tpu_runtimes.calls == []


def test_resolve_gpu_reports_oversized_native_quantity_without_raising() -> None:
    """An oversized workload is a structured rejected precondition."""
    regional, global_ = _gpu_quota_evidence()
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(regional, global_),)),
        ScriptedReader((_complete(_usage(regional, 0), _usage(global_, 0)),)),
        ScriptedCatalogReader((_compute_accelerator_catalog_read(),)),
        ScriptedCatalogReader((_gpu_catalog_read(),)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )
    requirement = replace(_gpu_requirement(), instance_count=2**63)

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.outcome.code == StableSymbol("unsupported-conversion")
    assert result.completeness.is_complete
    assert result.data is not None
    location = result.data.locations[0]
    assert location.failure_reason is not None
    assert location.failure_reason.value == "unsupported-conversion"
    assert location.constraint_requirements == ()


@pytest.mark.parametrize(
    "usage_case", ["missing", "ambiguous", "incompatible", "negative"]
)
def test_resolve_stops_on_untrustworthy_constraint_usage(usage_case: str) -> None:
    """Every exact limiting slice requires one compatible authoritative series."""
    regional, global_ = _gpu_quota_evidence()
    regional_usage = _usage(regional, 0)
    global_usage = _usage(global_, 0)
    observations = {
        "missing": (regional_usage,),
        "ambiguous": (regional_usage, global_usage, global_usage),
        "incompatible": (
            regional_usage,
            replace(global_usage, unit=OTHER_UNIT.symbol),
        ),
        "negative": (regional_usage, _usage(global_, -1)),
    }[usage_case]
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(regional, global_),)),
        ScriptedReader((_complete(*observations),)),
        ScriptedCatalogReader((_compute_accelerator_catalog_read(),)),
        ScriptedCatalogReader((_gpu_catalog_read(),)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _gpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("constraint-usage-incomplete")
    assert result.data is None
    assert result.completeness.has_partial_data


def test_resolve_stops_on_incomplete_usage_read() -> None:
    """A partial Monitoring read cannot support quota sufficiency."""
    regional, global_ = _gpu_quota_evidence()
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(regional, global_),)),
        ScriptedReader((_incomplete(_usage(regional), _usage(global_)),)),
        ScriptedCatalogReader((_compute_accelerator_catalog_read(),)),
        ScriptedCatalogReader((_gpu_catalog_read(),)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _gpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("quota-usage-read-incomplete")
    assert result.data is None


def test_resolve_legacy_tpu_uses_only_selected_zone_tpu_catalogs() -> None:
    """Legacy Cloud TPU resolution reads its three exact-zone catalog sources."""
    location_read, accelerator_read, runtime_read = _legacy_tpu_catalog_reads()
    effective = ScriptedReader((_complete(_legacy_tpu_quota_evidence()),))
    compute = ScriptedCatalogReader(())
    tpu_locations = ScriptedCatalogReader((location_read,))
    tpu_accelerators = ScriptedCatalogReader((accelerator_read,))
    tpu_runtimes = ScriptedCatalogReader((runtime_read,))
    operations = WorkloadResolutionOperations(
        effective,
        ScriptedReader((_complete(_usage(_legacy_tpu_quota_evidence(), value=0)),)),
        ScriptedCatalogReader(()),
        compute,
        tpu_locations,
        tpu_accelerators,
        tpu_runtimes,
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _legacy_tpu_requirement()))
    )

    assert result.succeeded
    assert result.data is not None
    location = result.data.locations[0]
    assert location.owning_service == "tpu.googleapis.com"
    assert tuple(item.required for item in location.constraint_requirements) == (
        QuotaQuantity(8, QuotaUnit("core")),
    )
    assert tuple(item.source_quantity for item in location.constraint_requirements) == (
        8,
    )
    assert compute.calls == []
    assert len(tpu_locations.calls) == 1
    assert len(tpu_accelerators.calls) == 1
    assert len(tpu_runtimes.calls) == 1


def test_resolve_cloud_tpu_all_compatible_reads_every_discovered_zone() -> None:
    """All-compatible fans out both TPU child catalogs for each discovered zone."""
    zones = ("us-central1-b", "us-east1-d")
    first_reads = _legacy_tpu_catalog_reads(zones[0])
    second_reads = _legacy_tpu_catalog_reads(zones[1])
    location_read = CatalogRead(
        _complete(first_reads[0].values[0], second_reads[0].values[0]),
        (
            *first_reads[0].location_coverage,
            *second_reads[0].location_coverage,
        ),
    )
    quotas = tuple(_legacy_tpu_quota_evidence(zone) for zone in zones)
    tpu_accelerators = ScriptedCatalogReader((first_reads[1], second_reads[1]))
    tpu_runtimes = ScriptedCatalogReader((first_reads[2], second_reads[2]))
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(*quotas),)),
        ScriptedReader((_complete(*(_usage(quota, 0) for quota in quotas)),)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader((location_read,)),
        tpu_accelerators,
        tpu_runtimes,
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )
    requirement = replace(
        _legacy_tpu_requirement(),
        locations=AllCompatibleLocations(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.succeeded
    assert result.data is not None
    assert result.data.all_compatible_locations_exhaustive is True
    assert tuple(location.location for location in result.data.locations) == zones
    assert all(location.permits is True for location in result.data.locations)
    assert (
        tuple(
            cast("TpuAcceleratorTypeReadRequest", call).zone
            for call in tpu_accelerators.calls
        )
        == zones
    )
    assert (
        tuple(
            cast("TpuRuntimeVersionReadRequest", call).zone
            for call in tpu_runtimes.calls
        )
        == zones
    )


def test_resolve_compute_tpu_uses_compute_catalog_and_owned_quota() -> None:
    """A GKE TPU workload resolves through Compute-owned catalog and quota."""
    effective = ScriptedReader((_complete(_compute_tpu_quota_evidence()),))
    compute = ScriptedCatalogReader((_compute_tpu_catalog_read(),))
    tpu_locations = ScriptedCatalogReader(())
    tpu_accelerators = ScriptedCatalogReader(())
    tpu_runtimes = ScriptedCatalogReader(())
    operations = WorkloadResolutionOperations(
        effective,
        ScriptedReader((_complete(_usage(_compute_tpu_quota_evidence(), value=0)),)),
        ScriptedCatalogReader(
            (_compute_accelerator_catalog_read("tpu-v6e", "us-central1-b"),)
        ),
        compute,
        tpu_locations,
        tpu_accelerators,
        tpu_runtimes,
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _compute_tpu_requirement()))
    )

    assert result.succeeded
    assert result.data is not None
    location = result.data.locations[0]
    assert location.owning_service == "compute.googleapis.com"
    assert tuple(item.required for item in location.constraint_requirements) == (
        QuotaQuantity(4, UNIT),
    )
    assert tuple(item.source_quantity for item in location.constraint_requirements) == (
        4,
    )
    assert len(compute.calls) == 1
    assert tpu_locations.calls == []
    assert tpu_accelerators.calls == []
    assert tpu_runtimes.calls == []


def test_resolve_stops_on_incomplete_required_quota_read() -> None:
    """Partial effective-quota evidence cannot produce a resolved requirement."""
    effective = ScriptedReader((_incomplete(*_gpu_quota_evidence()),))
    operations = WorkloadResolutionOperations(
        effective,
        ScriptedReader((_complete(),)),
        ScriptedCatalogReader((_compute_accelerator_catalog_read(),)),
        ScriptedCatalogReader((_gpu_catalog_read(),)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _gpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("effective-quota-read-incomplete")
    assert result.data is None
    assert not result.completeness.is_complete
    assert result.completeness.has_partial_data


def test_resolve_stops_on_failed_selected_location_catalog_evidence() -> None:
    """A failed selected catalog location is incomplete, never unsupported."""
    diagnostic = _catalog_diagnostic()
    failed_selected = CatalogRead(
        _complete(),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.FAILED,
                (diagnostic,),
            ),
        ),
    )
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(*_gpu_quota_evidence()),)),
        ScriptedReader((_complete(),)),
        ScriptedCatalogReader((_compute_accelerator_catalog_read(),)),
        ScriptedCatalogReader((failed_selected,)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _gpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("missing-location-evidence")
    assert result.data is not None
    failure_reason = result.data.locations[0].failure_reason
    assert failure_reason is not None
    assert failure_reason.value == "missing-location-evidence"
    assert result.completeness.has_partial_data
    assert result.diagnostics == (diagnostic,)


def test_resolve_rejects_ineligible_exact_quota_slice() -> None:
    """Resolution stops when an exact required quota slice is ineligible."""
    regional, global_ = _gpu_quota_evidence()
    regional = replace(
        regional,
        eligibility=QuotaIncreaseEligibility(
            eligible=False,
            reason=ProviderSymbol("NO_REQUESTS_ALLOWED", QuotaIneligibilityReason),
        ),
    )
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(regional, global_),)),
        ScriptedReader((_complete(),)),
        ScriptedCatalogReader((_compute_accelerator_catalog_read(),)),
        ScriptedCatalogReader((_gpu_catalog_read(),)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _gpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.outcome.code == StableSymbol("ineligible")
    assert result.data is not None
    failure_reason = result.data.locations[0].failure_reason
    assert failure_reason is not None
    assert failure_reason.value == "ineligible"
    assert result.completeness.is_complete


def test_resolve_rejects_ambiguous_exact_quota_slice() -> None:
    """Duplicate exact quota evidence cannot produce workload guidance."""
    regional, global_ = _gpu_quota_evidence()
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(regional, regional, global_),)),
        ScriptedReader((_complete(),)),
        ScriptedCatalogReader((_compute_accelerator_catalog_read(),)),
        ScriptedCatalogReader((_gpu_catalog_read(),)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _gpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.outcome.code == StableSymbol("ambiguous")
    assert result.data is not None
    failure_reason = result.data.locations[0].failure_reason
    assert failure_reason is not None
    assert failure_reason.value == "ambiguous"


def test_browse_sorts_snapshots_and_resumes_without_provider_calls() -> None:
    """Cursor continuation reads the immutable local snapshot only."""
    generic = _evidence("generic-mutable")
    guided = _evidence("known-guided", eligible=False)
    fixture = _fixture((_complete(guided, generic),))

    first = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), _query(), limit=1))
    )
    calls = (
        len(fixture.effective.calls),
        len(fixture.preferences.calls),
        len(fixture.usage.calls),
    )
    second = asyncio.run(
        fixture.operations.browse(
            QuotaBrowseRequest(cursor=first.data.next_cursor, limit=1)
        )
    )

    assert first.succeeded
    assert second.succeeded
    assert first.data.ordered
    assert first.data.total == len((generic, guided))
    assert first.data.items[0].identity.quota_id == "generic-mutable"
    assert first.data.items[0].predicates == CatalogPredicates(
        discovered=True,
        cataloged=False,
        guided=False,
        mutable=True,
    )
    assert second.data.items[0].predicates == CatalogPredicates(
        discovered=True,
        cataloged=True,
        guided=True,
        mutable=False,
    )
    assert second.data.constraint_sets == (second.data.items[0].constraint_set,)
    assert calls == (
        len(fixture.effective.calls),
        len(fixture.preferences.calls),
        len(fixture.usage.calls),
    )


def test_bare_browse_federates_both_v1_providers_with_bound_coverage() -> None:
    """A bare query reads Compute and TPU and binds both into its cursor snapshot."""
    compute = _evidence("compute-quota")
    tpu = _evidence("tpu-quota", service="tpu.googleapis.com")
    fixture = _fixture((_complete(compute), _complete(tpu)))

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), QuotaQuery(PROJECT)))
    )

    assert result.succeeded
    assert tuple(
        _effective_service(request) for request in fixture.effective.calls
    ) == (
        "compute.googleapis.com",
        "tpu.googleapis.com",
    )
    assert tuple(item.identity.service for item in result.data.items) == (
        "compute.googleapis.com",
        "tpu.googleapis.com",
    )
    assert tuple(item.state for item in result.data.source_coverage) == (
        ProviderSourceCoverageState.COMPLETE,
        ProviderSourceCoverageState.COMPLETE,
    )
    snapshot = fixture.snapshots.snapshots[result.data.snapshot_id or ""]
    assert snapshot.metadata.source_coverage == result.data.source_coverage


def test_preference_schema_failure_degrades_only_its_provider_coverage() -> None:
    """Assignable preference failures retain unrelated provider completeness."""
    diagnostic = Diagnostic(
        DiagnosticCode("provider-schema-invalid"),
        Severity.ERROR,
        DiagnosticPhase("quota-preference-read"),
        DiagnosticSource("cloud-quotas"),
        RetryDisposition.AFTER_UPGRADE,
        RedactedText("The provider returned malformed preference evidence."),
    )
    preferences = ProviderRead[QuotaPreferenceEvidence](
        (),
        ProviderReadCoverage(2, 2),
        NOW,
        (diagnostic,),
        ("tpu.googleapis.com",),
    )
    fixture = _fixture(
        (
            _complete(_evidence("compute-quota")),
            _complete(_evidence("tpu-quota", service="tpu.googleapis.com")),
        ),
        preferences=preferences,
    )

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), QuotaQuery(PROJECT)))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert tuple(item.state for item in result.data.source_coverage) == (
        ProviderSourceCoverageState.COMPLETE,
        ProviderSourceCoverageState.INCOMPLETE,
    )
    assert tuple(item.pages_attempted for item in result.data.source_coverage) == (4, 4)
    assert result.data.source_coverage[0].diagnostic_codes == ()
    assert result.data.source_coverage[1].diagnostic_codes == (
        DiagnosticCode("provider-schema-invalid"),
    )
    preference_request = cast(
        "QuotaPreferenceReadRequest", fixture.preferences.calls[0]
    )
    assert preference_request.services == (
        "compute.googleapis.com",
        "tpu.googleapis.com",
    )


def test_service_filter_prunes_reads_rows_and_marks_other_provider_unqueried() -> None:
    """Input shorthand selects only Compute without treating TPU as a failure."""
    fixture = _fixture((_complete(_evidence("compute-quota")),))
    query = QuotaQuery(
        PROJECT,
        filters=QuotaQueryFilters(services=("compute",)),
    )

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), query))
    )

    assert result.succeeded
    assert [_effective_service(request) for request in fixture.effective.calls] == [
        "compute.googleapis.com"
    ]
    assert tuple(item.identity.service for item in result.data.items) == (
        "compute.googleapis.com",
    )
    assert tuple(item.state for item in result.data.source_coverage) == (
        ProviderSourceCoverageState.COMPLETE,
        ProviderSourceCoverageState.INTENTIONALLY_UNQUERIED,
    )


def test_catalog_group_filter_prunes_nonmember_rows() -> None:
    """Provider pruning and displayed-row filtering use the same group facet."""
    fixture = _fixture(
        (_complete(_evidence("known-guided", service="tpu.googleapis.com")),)
    )
    query = QuotaQuery(
        PROJECT,
        filters=QuotaQueryFilters(catalog_groups=(CatalogGroupId.CLOUD_TPU_LEGACY,)),
    )

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), query))
    )

    assert result.succeeded
    assert result.data.items == ()
    assert [_effective_service(request) for request in fixture.effective.calls] == [
        "tpu.googleapis.com"
    ]


def test_initial_page_retains_snapshot_when_cursor_issue_fails() -> None:
    """A local cursor-write failure preserves the collected snapshot context."""
    fixture = _fixture((_complete(_evidence("first"), _evidence("second")),))
    fixture.cursors.issue_error = QuotaSnapshotOperationalError("cursor store failed")

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), _query(), limit=1))
    )

    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert result.data.query == _query()
    assert result.data.snapshot_id == "snapshot-1"
    assert result.data.items[0].identity.quota_id == "first"
    assert result.data.next_cursor is None
    assert result.data.reason == "cursor-issue-failed"


def test_resumed_page_retains_snapshot_when_cursor_issue_fails() -> None:
    """A resumed local page retains its bound snapshot when reissue fails."""
    fixture = _fixture(
        (
            _complete(
                _evidence("first"),
                _evidence("second"),
                _evidence("third"),
            ),
        )
    )
    first = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), _query(), limit=1))
    )
    message = "cursor store failed"
    fixture.cursors.issue_error = QuotaSnapshotOperationalError(message)

    result = asyncio.run(
        fixture.operations.browse(
            QuotaBrowseRequest(cursor=first.data.next_cursor, limit=1)
        )
    )

    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert result.data.query == _query()
    assert result.data.snapshot_id == first.data.snapshot_id
    assert result.data.items[0].identity.quota_id == "second"
    assert result.data.next_cursor is None
    assert result.data.reason == "cursor-issue-failed"


@pytest.mark.parametrize(
    "error",
    [UnknownQuotaCursorError("unknown"), QuotaCursorQueryMismatchError("mismatch")],
)
def test_bad_cursor_rejects_before_provider_access(error: Exception) -> None:
    """Unknown and mismatched opaque cursors cannot trigger provider reads."""
    fixture = _fixture((_complete(_evidence("generic")),))
    fixture.cursors.resolve_error = error

    result = asyncio.run(
        fixture.operations.browse(
            QuotaBrowseRequest(_context(), _query(), cursor="opaque-invalid")
        )
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert fixture.effective.calls == []
    assert fixture.preferences.calls == []
    assert fixture.usage.calls == []


def test_scope_less_bad_cursor_rejects_without_provider_access() -> None:
    """A cursor-only rejection needs neither ADC context nor a resource scope."""
    fixture = _fixture((_complete(_evidence("generic")),))
    fixture.cursors.resolve_error = UnknownQuotaCursorError("unknown")

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(cursor="opaque-invalid"))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.resource_scope is None
    assert fixture.effective.calls == []
    assert fixture.preferences.calls == []
    assert fixture.usage.calls == []


@pytest.mark.parametrize(
    ("sort", "evidences"),
    [
        (QuotaSort(QuotaSortField.USAGE), (_evidence("generic"),)),
        (
            QuotaSort(QuotaSortField.EFFECTIVE),
            (_evidence("first"), _evidence("second", unit=OTHER_UNIT)),
        ),
    ],
)
def test_browse_rejects_inapplicable_or_mixed_unit_sort(
    sort: QuotaSort,
    evidences: tuple[EffectiveQuotaEvidence, ...],
) -> None:
    """A requested sort must be meaningful and unit-coherent."""
    fixture = _fixture((_complete(*evidences),))

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), _query(sort)))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert fixture.snapshots.snapshots == {}


def test_complete_browse_rejects_duplicate_effective_slice_as_provider_failure() -> (
    None
):
    """Duplicate exact provider evidence is an integrity failure, not user input."""
    evidence = _evidence("duplicate")
    fixture = _fixture((_complete(evidence, evidence),))

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), _query()))
    )

    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert result.outcome.code == StableSymbol("duplicate-effective-slice")
    assert result.data.reason == "duplicate-effective-slice"
    assert fixture.snapshots.snapshots == {}


def test_incomplete_browse_retains_filtered_items_without_order_or_cursor() -> None:
    """Usable partial evidence stays visible without global claims."""
    fixture = _fixture((_incomplete(_evidence("generic-mutable")),))

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), _query()))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert [item.identity.quota_id for item in result.data.items] == ["generic-mutable"]
    assert not result.data.ordered
    assert result.data.total is None
    assert result.data.next_cursor is None
    assert result.data.snapshot_id is None
    assert tuple(item.state for item in result.data.source_coverage) == (
        ProviderSourceCoverageState.INCOMPLETE,
        ProviderSourceCoverageState.INTENTIONALLY_UNQUERIED,
    )
    assert tuple(
        code.value for code in result.data.source_coverage[0].diagnostic_codes
    ) == ("provider-page-cap-reached",)
    assert fixture.snapshots.snapshots == {}


def test_incomplete_browse_with_no_filtered_rows_uses_incomplete_exit() -> None:
    """An empty filtered view cannot turn an incomplete scan into unavailability."""
    query = QuotaQuery(
        PROJECT,
        filters=QuotaQueryFilters(services=("compute",), cataloged=True),
    )
    fixture = _fixture((_incomplete(_evidence("generic")),))

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), query))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.data.items == ()
    assert not result.data.ordered
    assert result.data.total is None
    assert result.data.next_cursor is None


def test_empty_result_allows_requested_optional_sort() -> None:
    """An empty complete collection is valid for every supported sort field."""
    fixture = _fixture((_complete(),))

    result = asyncio.run(
        fixture.operations.browse(
            QuotaBrowseRequest(
                _context(),
                _query(QuotaSort(QuotaSortField.USAGE)),
            )
        )
    )

    assert result.succeeded
    assert result.data.items == ()
    assert result.data.total == 0


def test_browse_retains_quota_when_optional_usage_is_incompatible() -> None:
    """Bad optional usage evidence does not erase authoritative quota evidence."""
    evidence = _evidence("generic")
    incompatible_usage = replace(_usage(evidence), unit=OTHER_UNIT.symbol)
    fixture = _fixture(
        (_complete(evidence),),
        usage=_complete(incompatible_usage),
    )

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), _query()))
    )

    assert result.succeeded
    assert tuple(item.identity for item in result.data.items) == (evidence.identity,)
    assert result.data.items[0].usage_value is None


def test_inspect_joins_only_exact_authoritative_evidence() -> None:
    """Inspect returns the exact slice, status, usage, and independent constraints."""
    selected = _evidence("known-guided")
    companion = _evidence("known-global")
    unrelated_region = _evidence("known-other-region")
    unrelated_region = replace(
        unrelated_region,
        identity=_identity("known-other-region", region="us-east1"),
        applicable_locations=("us-east1",),
    )
    fixture = _fixture(
        (_complete(selected, companion, unrelated_region),),
        preferences=_complete(_preference(selected.identity)),
        usage=_complete(_usage(selected)),
    )

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), selected.identity))
    )

    assert result.succeeded
    assert result.data.evidence is selected
    assert result.data.item is not None
    assert result.data.item.usage_value == QuotaQuantity(3, UNIT)
    assert result.data.item.desired_value == QuotaQuantity(8, UNIT)
    assert result.data.item.grant_satisfaction is GrantSatisfaction.FULL
    assert result.data.item.effective_confirmation is EffectiveConfirmation.CONFIRMED
    assert result.data.item.reconciliation is Reconciliation.SETTLED
    assert result.data.item.predicates.mutable
    assert result.data.constraint_set is not None
    references = {
        reference.slice_identity for reference in result.data.constraint_set.references
    }
    assert references == {
        selected.identity,
        companion.identity,
    }
    assert unrelated_region.identity not in references


def test_inspect_joins_dimensionless_preference_to_explicit_global_slice() -> None:
    """Preference scope uncertainty does not hide an otherwise exact global match."""
    selected = replace(
        _evidence("GPUS-ALL-REGIONS-per-project"),
        identity=EffectiveQuotaSliceIdentity(
            PROJECT,
            "compute.googleapis.com",
            "GPUS-ALL-REGIONS-per-project",
            NormalizedDimensions(),
            QuotaScope.GLOBAL,
        ),
        declared_dimensions=(),
        applicable_locations=("global",),
    )
    preference_identity = replace(
        selected.identity,
        quota_scope=QuotaScope.UNKNOWN,
    )
    fixture = _fixture(
        (_complete(selected),),
        preferences=_complete(_preference(preference_identity)),
        usage=_complete(_usage(selected)),
    )

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), selected.identity))
    )

    assert result.succeeded
    assert result.data.item is not None
    assert result.data.item.desired_value == QuotaQuantity(8, UNIT)
    assert result.data.item.granted_value == QuotaQuantity(8, UNIT)
    assert result.data.item.reconciliation is Reconciliation.SETTLED


def test_inspect_does_not_join_usage_from_another_applicable_location() -> None:
    """A dimensioned slice joins usage only at its exact location."""
    evidence = replace(
        _evidence("known-guided"),
        applicable_locations=("us-central1", "us-east1"),
    )
    east_usage = replace(
        _usage(evidence),
        resource_labels=NormalizedDimensions(
            (
                ("location", "us-east1"),
                ("project_id", "public-schema-project"),
                ("service", evidence.identity.service),
            )
        ),
    )
    fixture = _fixture(
        (_complete(evidence),),
        usage=_complete(east_usage),
    )

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), evidence.identity))
    )

    assert result.succeeded
    assert result.data.usage is None
    assert result.data.item is not None
    assert result.data.item.usage_value is None


def test_inspect_zonal_slice_does_not_join_parent_region_usage() -> None:
    """A zonal slice requires usage at its exact zone."""
    evidence = replace(
        _evidence("known-zonal"),
        identity=EffectiveQuotaSliceIdentity(
            PROJECT,
            "compute.googleapis.com",
            "known-zonal",
            NormalizedDimensions(
                (("region", "us-central1"), ("zone", "us-central1-a"))
            ),
            QuotaScope.ZONAL,
        ),
        applicable_locations=("us-central1", "us-central1-a"),
        declared_dimensions=("region", "zone"),
    )
    regional_usage = replace(
        _usage(evidence),
        resource_labels=NormalizedDimensions(
            (
                ("location", "us-central1"),
                ("project_id", "public-schema-project"),
                ("service", evidence.identity.service),
            )
        ),
    )
    fixture = _fixture(
        (_complete(evidence),),
        usage=_complete(regional_usage),
    )

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), evidence.identity))
    )

    assert result.succeeded
    assert result.data.usage is None
    assert result.data.item is not None
    assert result.data.item.usage_value is None


def test_inspect_global_companion_exposes_each_anchored_region_set() -> None:
    """Inspect preserves alternative regional sets sharing one global slice."""
    central, global_ = _gpu_quota_evidence()
    east = replace(
        central,
        identity=replace(
            central.identity,
            dimensions=NormalizedDimensions(
                (("gpu_family", "NVIDIA_H100"), ("region", "us-east1"))
            ),
        ),
        applicable_locations=("us-east1",),
    )
    fixture = _fixture(
        (_complete(east, global_, central),),
        overlay=MAINTAINED_ACCELERATOR_OVERLAY,
    )

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), global_.identity))
    )

    assert result.succeeded
    assert result.data.constraint_set is None
    assert tuple(
        tuple(reference.slice_identity for reference in constraint.references)
        for constraint in result.data.constraint_sets
    ) == (
        (global_.identity, central.identity),
        (global_.identity, east.identity),
    )


def test_browse_filters_shared_companion_through_related_accelerator_sets() -> None:
    """Catalog and accelerator facets retain a recognized shared companion."""
    regional, global_ = _gpu_quota_evidence()
    fixture = _fixture(
        (_complete(regional, global_),),
        overlay=MAINTAINED_ACCELERATOR_OVERLAY,
    )
    query = QuotaQuery(
        PROJECT,
        filters=QuotaQueryFilters(
            services=("compute",),
            accelerators=(ACCELERATOR,),
            cataloged=True,
            guided=True,
        ),
    )

    result = asyncio.run(
        fixture.operations.browse(QuotaBrowseRequest(_context(), query))
    )

    assert result.succeeded
    assert tuple(item.identity for item in result.data.items) == (
        global_.identity,
        regional.identity,
    )
    companion = result.data.items[0]
    assert companion.accelerator_id is None
    assert companion.predicates.cataloged
    assert companion.predicates.guided
    assert companion.constraint_sets


def test_inspect_mutability_uses_only_complete_exact_effective_read() -> None:
    """Unrelated preference incompleteness does not erase fresh mutability."""
    selected = _evidence("generic")
    fixture = _fixture(
        (_complete(selected),),
        preferences=_incomplete(),
    )

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), selected.identity))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.data.item is not None
    assert result.data.item.predicates.mutable


def test_inspect_fails_closed_on_ambiguous_exact_preference() -> None:
    """Duplicate exact provider preferences cannot be selected by guesswork."""
    selected = _evidence("known-guided")
    preference = _preference(selected.identity)
    fixture = _fixture(
        (_complete(selected),),
        preferences=_complete(
            preference,
            replace(preference, provider_name="duplicate"),
        ),
    )

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), selected.identity))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION


def test_complete_inspect_rejects_duplicate_effective_slice_as_provider_failure() -> (
    None
):
    """Inspect classifies duplicate exact provider slices as an integrity failure."""
    selected = _evidence("duplicate")
    fixture = _fixture((_complete(selected, selected),))

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), selected.identity))
    )

    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert result.outcome.code == StableSymbol("duplicate-effective-slice")
    assert result.data.reason == "duplicate-effective-slice"


def test_inspect_is_incomplete_when_guided_constraint_set_is_missing() -> None:
    """A guided exact slice is not inspect-complete without required companions."""
    selected = _evidence("known-needs-companion")
    fixture = _fixture((_complete(selected),))

    result = asyncio.run(
        fixture.operations.inspect(QuotaInspectRequest(_context(), selected.identity))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.data.evidence is selected
    assert result.data.item is not None
    assert result.data.item.constraint_set is None
    assert result.data.reason == "constraint-set-incomplete"
