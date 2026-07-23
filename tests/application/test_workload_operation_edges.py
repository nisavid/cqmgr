"""Application-boundary edges for workload-first quota resolution."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.application.operations.quotas import (
    QuotaBrowseRequest,
    QuotaInspectData,
    QuotaInspectRequest,
    QuotaOperations,
    QuotaResolveRequest,
    WorkloadResolutionOperations,
)
from cqmgr.application.ports.catalog_reads import CatalogRead
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    AcceleratorConstraintSet,
    CatalogEvidenceSource,
    CatalogLocationCoverage,
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
from cqmgr.domain.quota_queries import QuotaQuery
from cqmgr.domain.quotas import (
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    ProviderRead,
    ProviderReadCoverage,
    QuotaScope,
    UsageObservation,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import ExitClass, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import Sequence

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

NOW = datetime(2026, 7, 22, 9, tzinfo=UTC)
PROJECT = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")


class FixedClock:
    """Return one deterministic operation time."""

    def now(self) -> datetime:
        """Return the fixed UTC instant."""
        return NOW


class ScriptedReader[ValueT]:
    """Return provider reads from a deterministic external-boundary script."""

    def __init__(self, reads: Sequence[ProviderRead[ValueT]]) -> None:
        """Retain provider reads in dispatch order."""
        self.reads = list(reads)

    async def read(self, request: object) -> ProviderRead[ValueT]:
        """Return the next provider observation."""
        del request
        return self.reads.pop(0)


class ScriptedCatalogReader[ValueT]:
    """Return catalog reads from a deterministic external-boundary script."""

    def __init__(self, reads: Sequence[CatalogRead[ValueT]]) -> None:
        """Retain catalog reads and an observable request ledger."""
        self.reads = list(reads)
        self.calls: list[object] = []

    async def read(self, request: object) -> CatalogRead[ValueT]:
        """Return the next catalog observation."""
        self.calls.append(request)
        return self.reads.pop(0)


class BlockingReader[ValueT]:
    """Expose cancellation cleanup for one indefinitely blocked read."""

    def __init__(self) -> None:
        """Start with an unentered and uncleaned read."""
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def read(self, request: object) -> ValueT:
        """Block until cancellation and expose the coroutine cleanup."""
        del request
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.cleaned.set()
        raise AssertionError


def _context() -> ProviderReadContext:
    """Return one explicit project read context."""
    return ProviderReadContext(
        project=CanonicalProject(PROJECT, "public-schema-project", "Public Project"),
        identity=ADCIdentityEvidence.principal_unverified(
            credential_kind=CredentialKind.UNKNOWN,
            adc_quota_project=None,
        ),
        deadline=100.0,
        cancellation=CancellationToken(),
    )


def _complete[ValueT](*values: ValueT) -> ProviderRead[ValueT]:
    """Return one complete provider read."""
    return ProviderRead(tuple(values), ProviderReadCoverage(1, 1), NOW)


def _incomplete[ValueT](*values: ValueT) -> ProviderRead[ValueT]:
    """Return a bounded incomplete provider read."""
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


def _catalog_diagnostic() -> Diagnostic:
    """Return one redacted incomplete-catalog diagnostic."""
    return Diagnostic(
        DiagnosticCode("catalog-location-not-scanned"),
        Severity.WARNING,
        DiagnosticPhase("catalog-read"),
        DiagnosticSource("tpu-locations"),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("One requested catalog location was not scanned."),
    )


def _gpu_requirement() -> ComputeInstanceRequirement:
    """Return one public Compute workload input."""
    return ComputeInstanceRequirement(
        machine_type="a3-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )


def _gpu_catalog_read() -> CatalogRead[ComputeMachineType]:
    """Return exact Compute machine evidence for the workload."""
    machine = ComputeMachineType(
        name="a3-highgpu-8g",
        zone="us-central1-a",
        guest_accelerators=(AcceleratorAttachment("nvidia-h100-80gb", 8),),
        lifecycle=None,
    )
    return CatalogRead(
        _complete(machine),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
    )


def _compute_accelerator_catalog_read() -> CatalogRead[ComputeAcceleratorType]:
    """Return exact Compute accelerator declaration evidence."""
    return CatalogRead(
        _complete(ComputeAcceleratorType("nvidia-h100-80gb", "us-central1-a", None)),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
    )


def _legacy_tpu_requirement() -> CloudTpuSliceRequirement:
    """Return one public Cloud TPU slice workload input."""
    return CloudTpuSliceRequirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-b",)),
    )


def _legacy_tpu_catalog_reads() -> tuple[
    CatalogRead[TpuLocation],
    CatalogRead[TpuAcceleratorType],
    CatalogRead[TpuRuntimeVersion],
]:
    """Return complete exact-zone TPU catalog evidence."""
    zone = "us-central1-b"
    location = TpuLocation(f"projects/123456789/locations/{zone}", zone)
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
            (
                CatalogLocationCoverage(
                    CatalogEvidenceSource.TPU_LOCATIONS,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
            ),
        ),
        CatalogRead(
            _complete(accelerator),
            (
                CatalogLocationCoverage(
                    CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
            ),
        ),
        CatalogRead(
            _complete(runtime),
            (
                CatalogLocationCoverage(
                    CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
                    zone,
                    LocationCoverageExpectation.REQUESTED,
                    LocationCoverageState.SUCCESS,
                ),
            ),
        ),
    )


def _identity() -> EffectiveQuotaSliceIdentity:
    """Return one exact public quota slice identity."""
    return EffectiveQuotaSliceIdentity(
        PROJECT,
        "compute.googleapis.com",
        "GPUS-ALL-REGIONS-per-project",
        NormalizedDimensions(()),
        QuotaScope.GLOBAL,
    )


def test_browse_request_rejects_invalid_public_inputs() -> None:
    """Browse inputs require typed identities, a query path, and a bounded limit."""
    context = _context()
    query = QuotaQuery(PROJECT)

    with pytest.raises(TypeError, match="context"):
        QuotaBrowseRequest(cast("ProviderReadContext", object()), query)
    with pytest.raises(TypeError, match="query"):
        QuotaBrowseRequest(context, cast("QuotaQuery", object()))
    with pytest.raises(ValueError, match="cursor"):
        QuotaBrowseRequest(cursor=cast("str", 1))
    with pytest.raises(ValueError, match="cursor"):
        QuotaBrowseRequest(cursor="")
    with pytest.raises(ValueError, match="query or cursor"):
        QuotaBrowseRequest()
    with pytest.raises(ValueError, match="requires ProviderReadContext"):
        QuotaBrowseRequest(query=query)
    with pytest.raises(TypeError, match="limit"):
        QuotaBrowseRequest(context, query, limit=True)
    with pytest.raises(TypeError, match="limit"):
        QuotaBrowseRequest(context, query, limit=cast("int", "1"))
    with pytest.raises(ValueError, match="1 through 1000"):
        QuotaBrowseRequest(context, query, limit=0)
    with pytest.raises(ValueError, match="1 through 1000"):
        QuotaBrowseRequest(context, query, limit=1001)


def test_inspect_request_rejects_untyped_public_inputs() -> None:
    """Inspection requires one explicit context and exact slice identity."""
    context = _context()
    identity = _identity()

    with pytest.raises(TypeError, match="ProviderReadContext"):
        QuotaInspectRequest(cast("ProviderReadContext", object()), identity)
    with pytest.raises(TypeError, match="EffectiveQuotaSliceIdentity"):
        QuotaInspectRequest(
            context,
            cast("EffectiveQuotaSliceIdentity", object()),
        )


@pytest.mark.parametrize(
    ("snapshot_ttl", "usage_window"),
    [
        (timedelta(0), timedelta(hours=1)),
        (timedelta(minutes=1), timedelta(0)),
    ],
)
def test_quota_operations_require_positive_evidence_windows(
    snapshot_ttl: timedelta,
    usage_window: timedelta,
) -> None:
    """Snapshot retention and Monitoring windows must both be positive."""
    with pytest.raises(ValueError, match="must be positive"):
        QuotaOperations(
            cast("EffectiveQuotaReader", object()),
            cast("QuotaPreferenceReader", object()),
            cast("UsageReader", object()),
            cast("SemanticAcceleratorOverlay", object()),
            cast("QuotaQuerySnapshotRepository", object()),
            cast("QuotaQueryCursorCodec", object()),
            FixedClock(),
            snapshot_ttl=snapshot_ttl,
            usage_window=usage_window,
        )


def test_inspect_data_rejects_untyped_constraint_sets() -> None:
    """Structured inspection cannot retain untyped compatibility evidence."""
    with pytest.raises(TypeError, match="constraint_sets"):
        QuotaInspectData(
            _identity(),
            None,
            None,
            None,
            None,
            None,
            None,
            constraint_sets=cast(
                "tuple[AcceleratorConstraintSet, ...]",
                [object()],
            ),
        )


def _tpu_location_inventory(
    state: LocationCoverageState,
) -> CatalogRead[TpuLocation]:
    """Return a value-free authoritative TPU location inventory observation."""
    return CatalogRead(
        _complete(),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_LOCATIONS,
                "global",
                LocationCoverageExpectation.EXPECTED,
                state,
                (
                    (_catalog_diagnostic(),)
                    if state is LocationCoverageState.NOT_SCANNED
                    else ()
                ),
            ),
        ),
    )


def _tpu_operations(
    location_read: CatalogRead[TpuLocation],
    *,
    accelerator_reads: tuple[CatalogRead[TpuAcceleratorType], ...] = (),
    runtime_reads: tuple[CatalogRead[TpuRuntimeVersion], ...] = (),
) -> tuple[
    WorkloadResolutionOperations,
    ScriptedCatalogReader[TpuAcceleratorType],
    ScriptedCatalogReader[TpuRuntimeVersion],
]:
    """Build the read-only operation around scripted provider boundaries."""
    accelerator_reader = ScriptedCatalogReader(accelerator_reads)
    runtime_reader = ScriptedCatalogReader(runtime_reads)
    return (
        WorkloadResolutionOperations(
            ScriptedReader((_complete(),)),
            ScriptedReader((_complete(),)),
            ScriptedCatalogReader(()),
            ScriptedCatalogReader(()),
            ScriptedCatalogReader((location_read,)),
            accelerator_reader,
            runtime_reader,
            MAINTAINED_ACCELERATOR_OVERLAY,
            FixedClock(),
        ),
        accelerator_reader,
        runtime_reader,
    )


@pytest.mark.parametrize("invalid_field", ["context", "requirement"])
def test_resolve_request_rejects_untyped_public_inputs(invalid_field: str) -> None:
    """Resolution requires both an explicit provider context and workload shape."""
    context: object = _context()
    requirement: object = _gpu_requirement()
    if invalid_field == "context":
        context = object()
    else:
        requirement = object()

    with pytest.raises(TypeError, match="quota resolution requires"):
        QuotaResolveRequest(
            cast("ProviderReadContext", context),
            cast("ComputeInstanceRequirement", requirement),
        )


def test_resolve_classifies_wholly_unavailable_effective_evidence_as_operational() -> (
    None
):
    """No usable effective slice is an unavailable operational failure."""
    operations = WorkloadResolutionOperations(
        ScriptedReader((_incomplete(),)),
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

    assert result.outcome.exit_class is ExitClass.OPERATIONAL_FAILURE
    assert result.outcome.code == StableSymbol("effective-quota-read-incomplete")
    assert result.data is None
    assert not result.completeness.is_complete
    assert not result.completeness.has_partial_data


def test_resolve_all_compatible_accepts_an_authoritatively_empty_tpu_inventory() -> (
    None
):
    """An exhaustive empty inventory rejects the shape without child catalog reads."""
    operations, accelerator_reader, runtime_reader = _tpu_operations(
        _tpu_location_inventory(LocationCoverageState.EMPTY)
    )
    requirement = replace(
        _legacy_tpu_requirement(),
        locations=AllCompatibleLocations(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.outcome.code == StableSymbol("unsupported-compatibility")
    assert result.completeness.is_complete
    assert result.data is not None
    assert result.data.locations == ()
    assert result.data.all_compatible_locations_exhaustive is True
    assert accelerator_reader.calls == []
    assert runtime_reader.calls == []


def test_resolve_rejects_a_candidate_absent_from_complete_tpu_inventory() -> None:
    """Authoritative location absence cannot be overridden by child catalog values."""
    _location, accelerator_read, runtime_read = _legacy_tpu_catalog_reads()
    operations, accelerator_reader, runtime_reader = _tpu_operations(
        _tpu_location_inventory(LocationCoverageState.EMPTY),
        accelerator_reads=(accelerator_read,),
        runtime_reads=(runtime_read,),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _legacy_tpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.outcome.code == StableSymbol("unsupported-compatibility")
    assert result.completeness.is_complete
    assert result.data is not None
    location = result.data.locations[0]
    assert location.location == "us-central1-b"
    assert location.disposition is WorkloadLocationDisposition.INCOMPATIBLE
    assert location.coverage[0].state is LocationCoverageState.EMPTY
    assert len(accelerator_reader.calls) == 1
    assert len(runtime_reader.calls) == 1


def test_resolve_tpu_region_candidate_never_calls_zone_only_child_endpoints() -> None:
    """A TPU region remains one unsupported result without silently choosing a zone."""
    location_read, _accelerator_read, _runtime_read = _legacy_tpu_catalog_reads()
    operations, accelerator_reader, runtime_reader = _tpu_operations(location_read)
    requirement = replace(
        _legacy_tpu_requirement(),
        locations=CandidateLocations(("us-central1",)),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.REJECTED_PRECONDITION
    assert result.outcome.code == StableSymbol("unsupported-compatibility")
    assert result.completeness.is_complete
    assert result.data is not None
    assert tuple(item.location for item in result.data.locations) == ("us-central1",)
    assert (
        result.data.locations[0].disposition is WorkloadLocationDisposition.INCOMPATIBLE
    )
    assert accelerator_reader.calls == []
    assert runtime_reader.calls == []


def test_resolve_tpu_region_retains_partial_inventory_coverage() -> None:
    """A matching child cannot hide aggregate TPU location pagination gaps."""
    location_read, _accelerator_read, _runtime_read = _legacy_tpu_catalog_reads()
    diagnostic = _catalog_diagnostic()
    partial_location_read = CatalogRead(
        location_read.read,
        (
            *location_read.location_coverage,
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_LOCATIONS,
                "global",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.NOT_SCANNED,
                (diagnostic,),
            ),
        ),
    )
    operations, accelerator_reader, runtime_reader = _tpu_operations(
        partial_location_read
    )
    requirement = replace(
        _legacy_tpu_requirement(),
        locations=CandidateLocations(("us-central1",)),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("missing-location-evidence")
    assert result.data is not None
    assert (
        result.data.locations[0].disposition is WorkloadLocationDisposition.INCOMPLETE
    )
    assert tuple(coverage.state for coverage in result.data.locations[0].coverage) == (
        LocationCoverageState.SUCCESS,
        LocationCoverageState.NOT_SCANNED,
    )
    assert accelerator_reader.calls == []
    assert runtime_reader.calls == []


def test_resolve_tpu_region_preserves_conflicting_child_coverage() -> None:
    """Duplicate child coverage cannot last-write-win into complete evidence."""
    location_read, _accelerator_read, _runtime_read = _legacy_tpu_catalog_reads()
    diagnostic = _catalog_diagnostic()
    conflicting_location_read = CatalogRead(
        location_read.read,
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_LOCATIONS,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.NOT_SCANNED,
                (diagnostic,),
            ),
            *location_read.location_coverage,
        ),
    )
    operations, accelerator_reader, runtime_reader = _tpu_operations(
        conflicting_location_read
    )
    requirement = replace(
        _legacy_tpu_requirement(),
        locations=CandidateLocations(("us-central1",)),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("missing-location-evidence")
    assert result.data is not None
    location = result.data.locations[0]
    assert location.disposition is WorkloadLocationDisposition.INCOMPLETE
    assert tuple(coverage.state for coverage in location.coverage) == (
        LocationCoverageState.NOT_SCANNED,
        LocationCoverageState.SUCCESS,
    )
    assert accelerator_reader.calls == []
    assert runtime_reader.calls == []


def test_resolve_tpu_region_rejects_duplicate_terminal_child_coverage() -> None:
    """Success and empty for one child remain contradictory missing evidence."""
    location_read, _accelerator_read, _runtime_read = _legacy_tpu_catalog_reads()
    conflicting_location_read = CatalogRead(
        location_read.read,
        (
            *location_read.location_coverage,
            CatalogLocationCoverage(
                CatalogEvidenceSource.TPU_LOCATIONS,
                "us-central1-b",
                LocationCoverageExpectation.EXPECTED,
                LocationCoverageState.EMPTY,
            ),
        ),
    )
    operations, accelerator_reader, runtime_reader = _tpu_operations(
        conflicting_location_read
    )
    requirement = replace(
        _legacy_tpu_requirement(),
        locations=CandidateLocations(("us-central1",)),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("missing-location-evidence")
    assert result.data is not None
    location = result.data.locations[0]
    assert location.disposition is WorkloadLocationDisposition.INCOMPLETE
    assert tuple(coverage.state for coverage in location.coverage) == (
        LocationCoverageState.SUCCESS,
        LocationCoverageState.EMPTY,
    )
    assert accelerator_reader.calls == []
    assert runtime_reader.calls == []


def test_resolve_tpu_region_handles_value_without_location_coverage() -> None:
    """A normalized location lacking coverage becomes typed incomplete evidence."""
    location_read, _accelerator_read, _runtime_read = _legacy_tpu_catalog_reads()
    inconsistent_location_read = CatalogRead(location_read.read, ())
    operations, accelerator_reader, runtime_reader = _tpu_operations(
        inconsistent_location_read
    )
    requirement = replace(
        _legacy_tpu_requirement(),
        locations=CandidateLocations(("us-central1",)),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("missing-location-evidence")
    assert result.data is not None
    coverage = result.data.locations[0].coverage
    assert len(coverage) == 1
    assert coverage[0].location == "us-central1-b"
    assert coverage[0].state is LocationCoverageState.NOT_SCANNED
    assert coverage[0].diagnostics
    assert accelerator_reader.calls == []
    assert runtime_reader.calls == []


def test_resolve_compute_region_attributes_only_incomplete_child_sources() -> None:
    """Regional candidates do not demand synthetic exact-region catalog records."""
    diagnostic = _catalog_diagnostic()
    requirement = replace(
        _gpu_requirement(),
        locations=CandidateLocations(("us-central1",)),
    )
    accelerator_read = _compute_accelerator_catalog_read()
    machine = _gpu_catalog_read().values[0]
    machine_read = CatalogRead(
        _complete(machine),
        (
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.NOT_SCANNED,
                (diagnostic,),
            ),
        ),
    )
    operations = WorkloadResolutionOperations(
        ScriptedReader((_complete(),)),
        ScriptedReader((_complete(),)),
        ScriptedCatalogReader((accelerator_read,)),
        ScriptedCatalogReader((machine_read,)),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        ScriptedCatalogReader(()),
        MAINTAINED_ACCELERATOR_OVERLAY,
        FixedClock(),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), requirement))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert tuple(gap.source.value for gap in result.completeness.gaps) == (
        "compute-machine-types",
    )


def test_tpu_resolution_cancellation_joins_started_provider_reads() -> None:
    """Cancelling catalog discovery also stops owned quota and usage reads."""

    async def exercise() -> None:
        effective = BlockingReader[ProviderRead[EffectiveQuotaEvidence]]()
        usage = BlockingReader[ProviderRead[UsageObservation]]()
        locations = BlockingReader[CatalogRead[TpuLocation]]()
        operations = WorkloadResolutionOperations(
            effective,
            usage,
            ScriptedCatalogReader(()),
            ScriptedCatalogReader(()),
            locations,
            ScriptedCatalogReader(()),
            ScriptedCatalogReader(()),
            MAINTAINED_ACCELERATOR_OVERLAY,
            FixedClock(),
        )
        call = asyncio.create_task(
            operations.resolve(
                QuotaResolveRequest(_context(), _legacy_tpu_requirement())
            )
        )
        await asyncio.gather(
            effective.started.wait(),
            usage.started.wait(),
            locations.started.wait(),
        )

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await call
        await asyncio.sleep(0)

        assert effective.cleaned.is_set()
        assert usage.cleaned.is_set()
        assert locations.cleaned.is_set()
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ] == []

    asyncio.run(exercise())


def test_resolve_retains_a_candidate_missing_from_incomplete_tpu_inventory() -> None:
    """Unscanned candidate evidence remains structured and incomplete, not absent."""
    _location, accelerator_read, runtime_read = _legacy_tpu_catalog_reads()
    operations, _, _ = _tpu_operations(
        _tpu_location_inventory(LocationCoverageState.NOT_SCANNED),
        accelerator_reads=(accelerator_read,),
        runtime_reads=(runtime_read,),
    )

    result = asyncio.run(
        operations.resolve(QuotaResolveRequest(_context(), _legacy_tpu_requirement()))
    )

    assert result.outcome.exit_class is ExitClass.INCOMPLETE_EVIDENCE
    assert result.outcome.code == StableSymbol("missing-location-evidence")
    assert result.completeness.has_partial_data
    assert result.data is not None
    location = result.data.locations[0]
    assert location.location == "us-central1-b"
    assert location.disposition is WorkloadLocationDisposition.INCOMPLETE
    assert location.coverage[0].state is LocationCoverageState.NOT_SCANNED
