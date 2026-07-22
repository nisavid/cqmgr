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
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.application.ports.quota_snapshots import (
    QuotaCursorQueryMismatchError,
    ResolvedQuotaQueryCursor,
    UnknownQuotaCursorError,
)
from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    GpuWorkloadRequirement,
    ProvisioningModel,
    TpuWorkloadRequirement,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogLocationCoverage,
    CatalogMetadata,
    CatalogPredicates,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
    ManagementPlane,
    TpuAcceleratorConfig,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
    WorkloadConsumer,
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
    QuotaQuery,
    QuotaQueryItem,
    QuotaQuerySnapshot,
    QuotaSort,
    QuotaSortField,
    ServiceSource,
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

    from cqmgr.application.ports.provider_reads import (
        EffectiveQuotaReader,
        QuotaPreferenceReader,
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

    def issue(
        self,
        snapshot_id: str,
        offset: int,
        *,
        now: datetime,
    ) -> OpaqueQueryCursor:
        """Issue one opaque handle without exposing snapshot state."""
        del now
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


def _identity(
    quota_id: str,
    *,
    unit: QuotaUnit = UNIT,
    region: str = "us-central1",
) -> EffectiveQuotaSliceIdentity:
    del unit
    return EffectiveQuotaSliceIdentity(
        PROJECT,
        "compute.googleapis.com",
        quota_id,
        NormalizedDimensions((("region", region),)),
        QuotaScope.REGIONAL,
    )


def _evidence(
    quota_id: str,
    *,
    value: int = 8,
    unit: QuotaUnit = UNIT,
    eligible: bool = True,
    fixed: bool = False,
) -> EffectiveQuotaEvidence:
    return EffectiveQuotaEvidence(
        identity=_identity(quota_id, unit=unit),
        effective_value=QuotaQuantity(value, unit),
        metric=f"compute.googleapis.com/{quota_id}",
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
                ("location", "us-central1"),
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
) -> OperationFixture:
    effective = ScriptedReader(effective_reads)
    preference_reader = ScriptedReader((preferences or _complete(),))
    usage_reader = ScriptedReader((usage or _complete(),))
    snapshots = MemorySnapshots()
    cursors = MemoryCursors(snapshots)
    operations = QuotaOperations(
        cast("EffectiveQuotaReader", effective),
        cast("QuotaPreferenceReader", preference_reader),
        cast("UsageReader", usage_reader),
        cast("SemanticAcceleratorOverlay", ScriptedOverlay()),
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
    return QuotaQuery(PROJECT, ServiceSource("compute.googleapis.com"), sort=sorts)


def _gpu_requirement() -> GpuWorkloadRequirement:
    return GpuWorkloadRequirement(
        accelerator_id=ACCELERATOR,
        workload_consumer=WorkloadConsumer.COMPUTE_ENGINE,
        accelerator_count=8,
        machine_type="a3-highgpu-8g",
        provisioning_model=ProvisioningModel.STANDARD,
        region="us-central1",
        zone="us-central1-a",
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


def _legacy_tpu_requirement() -> TpuWorkloadRequirement:
    return TpuWorkloadRequirement(
        management_plane=ManagementPlane.TPU,
        accelerator_id=AcceleratorId("tpu-v6e"),
        workload_consumer=WorkloadConsumer.CLOUD_TPU_API,
        accelerator_count=8,
        provisioning_model=ProvisioningModel.STANDARD,
        region=None,
        zone="us-central1-b",
        machine_type=None,
        topology="2x4",
        runtime_version="tpu-vm-base",
    )


def _compute_tpu_requirement() -> TpuWorkloadRequirement:
    return TpuWorkloadRequirement(
        management_plane=ManagementPlane.COMPUTE,
        accelerator_id=AcceleratorId("tpu-v6e"),
        workload_consumer=WorkloadConsumer.GKE,
        accelerator_count=4,
        provisioning_model=ProvisioningModel.STANDARD,
        region="us-central1",
        zone="us-central1-b",
        machine_type="ct6e-standard-4t",
        topology=None,
        runtime_version=None,
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


def _legacy_tpu_quota_evidence() -> EffectiveQuotaEvidence:
    return replace(
        _evidence("provider-discovered-v6e-id", unit=QuotaUnit("core")),
        identity=EffectiveQuotaSliceIdentity(
            PROJECT,
            "tpu.googleapis.com",
            "provider-discovered-v6e-id",
            NormalizedDimensions((("zone", "us-central1-b"),)),
            QuotaScope.ZONAL,
        ),
        metric="tpu.googleapis.com/provider-discovered-v6e-id",
        declared_dimensions=("zone",),
        applicable_locations=("us-central1-b",),
        quota_display_name="TPU v6e cores per project per zone",
    )


def _coverage(source: CatalogEvidenceSource) -> CatalogLocationCoverage:
    return CatalogLocationCoverage(
        source,
        "us-central1-b",
        LocationCoverageExpectation.REQUESTED,
        LocationCoverageState.SUCCESS,
    )


def _legacy_tpu_catalog_reads() -> tuple[
    CatalogRead[TpuLocation],
    CatalogRead[TpuAcceleratorType],
    CatalogRead[TpuRuntimeVersion],
]:
    location = TpuLocation(
        "projects/123456789/locations/us-central1-b",
        "us-central1-b",
    )
    accelerator = TpuAcceleratorType(
        "projects/123456789/locations/us-central1-b/acceleratorTypes/v6e-8",
        "us-central1-b",
        "v6e-8",
        (TpuAcceleratorConfig("V6E", "2x4"),),
    )
    runtime = TpuRuntimeVersion(
        "projects/123456789/locations/us-central1-b/runtimeVersions/tpu-vm-base",
        "us-central1-b",
        "tpu-vm-base",
    )
    return (
        CatalogRead(
            _complete(location),
            (_coverage(CatalogEvidenceSource.TPU_LOCATIONS),),
        ),
        CatalogRead(
            _complete(accelerator),
            (_coverage(CatalogEvidenceSource.TPU_ACCELERATOR_TYPES),),
        ),
        CatalogRead(
            _complete(runtime),
            (_coverage(CatalogEvidenceSource.TPU_RUNTIME_VERSIONS),),
        ),
    )


def test_resolve_gpu_retains_unrelated_catalog_failures() -> None:
    """Selected-location success is independent of unrelated catalog failures."""
    effective = ScriptedReader((_complete(*_gpu_quota_evidence()),))
    compute = ScriptedCatalogReader((_gpu_catalog_read(),))
    tpu_locations = ScriptedCatalogReader(())
    tpu_accelerators = ScriptedCatalogReader(())
    tpu_runtimes = ScriptedCatalogReader(())
    operations = WorkloadResolutionOperations(
        effective,
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
    assert result.data.owning_service == "compute.googleapis.com"
    assert result.data.required_amount == QuotaQuantity(8, UNIT)
    assert result.diagnostics == (_catalog_diagnostic(),)
    assert len(compute.calls) == 1
    assert tpu_locations.calls == []
    assert tpu_accelerators.calls == []
    assert tpu_runtimes.calls == []


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
    assert result.data.owning_service == "tpu.googleapis.com"
    assert result.data.required_amount == QuotaQuantity(8, QuotaUnit("core"))
    assert compute.calls == []
    assert len(tpu_locations.calls) == 1
    assert len(tpu_accelerators.calls) == 1
    assert len(tpu_runtimes.calls) == 1


def test_resolve_compute_tpu_uses_compute_catalog_and_owned_quota() -> None:
    """A GKE TPU workload resolves through Compute-owned catalog and quota."""
    effective = ScriptedReader((_complete(_compute_tpu_quota_evidence()),))
    compute = ScriptedCatalogReader((_compute_tpu_catalog_read(),))
    tpu_locations = ScriptedCatalogReader(())
    tpu_accelerators = ScriptedCatalogReader(())
    tpu_runtimes = ScriptedCatalogReader(())
    operations = WorkloadResolutionOperations(
        effective,
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
    assert result.data.owning_service == "compute.googleapis.com"
    assert result.data.required_amount == QuotaQuantity(4, UNIT)
    assert len(compute.calls) == 1
    assert tpu_locations.calls == []
    assert tpu_accelerators.calls == []
    assert tpu_runtimes.calls == []


def test_resolve_stops_on_incomplete_required_quota_read() -> None:
    """Partial effective-quota evidence cannot produce a resolved requirement."""
    effective = ScriptedReader((_incomplete(*_gpu_quota_evidence()),))
    operations = WorkloadResolutionOperations(
        effective,
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
    assert result.data is None
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
    assert result.data is None
    assert result.completeness.is_complete


def test_resolve_rejects_ambiguous_exact_quota_slice() -> None:
    """Duplicate exact quota evidence cannot produce workload guidance."""
    regional, global_ = _gpu_quota_evidence()
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(regional, regional, global_),)),
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
    assert result.data is None


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
    assert fixture.snapshots.snapshots == {}


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
