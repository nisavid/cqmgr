"""Stable accelerator-catalog and compatibility contracts."""

import pytest

from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorAttachment,
    AcceleratorCatalogEntry,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogGroupId,
    CatalogLifecycle,
    CatalogLocationCoverage,
    CatalogMetadata,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
    ManagementPlane,
    TpuAcceleratorConfig,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
    UnitConversionEvidence,
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
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind


def test_catalog_metadata_separates_schema_revision_and_content_digest() -> None:
    """A content refresh does not rename the public schema or catalog group."""
    metadata = CatalogMetadata(
        schema=ACCELERATOR_CATALOG_SCHEMA,
        revision="2026-07-22",
        content_digest="sha256:" + "a" * 64,
    )

    assert metadata.schema == "cqmgr.accelerator-catalog/v1"
    assert metadata.revision == "2026-07-22"
    assert metadata.content_digest == "sha256:" + "a" * 64
    assert {group.value for group in CatalogGroupId} == {
        "compute-accelerators",
        "cloud-tpu-legacy",
    }


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("schema", "cqmgr.accelerator-catalog/v2", "unsupported catalog schema"),
        ("revision", "", "revision must be non-empty"),
        ("content_digest", "a" * 64, "sha256"),
        ("content_digest", "sha256:" + "A" * 64, "sha256"),
    ],
)
def test_catalog_metadata_rejects_ambiguous_or_unsupported_identity(
    field: str,
    value: str,
    match: str,
) -> None:
    """Catalog snapshots carry one exact schema and lowercase SHA-256 digest."""
    values = {
        "schema": ACCELERATOR_CATALOG_SCHEMA,
        "revision": "2026-07-22",
        "content_digest": "sha256:" + "a" * 64,
    }
    values[field] = value

    with pytest.raises(ValueError, match=match):
        CatalogMetadata(**values)


def _slice(quota_id: str) -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        resource_scope=ResourceScope(ResourceScopeKind.PROJECT, "projects/123"),
        service="compute.googleapis.com",
        quota_id=quota_id,
        dimensions=NormalizedDimensions((("region", "us-central1"),)),
        quota_scope=QuotaScope.REGIONAL,
    )


def test_catalog_entry_keeps_plane_consumer_unit_and_conversion_independent() -> None:
    """Compute-owned quota can guide both Compute Engine and GKE workloads."""
    conversion = UnitConversionEvidence(
        source_unit="machine",
        quota_unit=QuotaUnit("chip"),
        quota_units_per_source=4,
        source_reference="https://example.test/tpu-shape-contract",
    )
    entry = AcceleratorCatalogEntry(
        group_id=CatalogGroupId.COMPUTE_ACCELERATORS,
        accelerator_id=AcceleratorId("tpu-v6e"),
        management_plane=ManagementPlane.COMPUTE,
        workload_consumers=(WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE),
        native_quota_unit=QuotaUnit("chip"),
        conversion=conversion,
    )

    assert entry.accelerator_id.value == "tpu-v6e"
    assert entry.management_plane is ManagementPlane.COMPUTE
    assert entry.workload_consumers == (
        WorkloadConsumer.COMPUTE_ENGINE,
        WorkloadConsumer.GKE,
    )
    assert entry.native_quota_unit == conversion.quota_unit
    assert ManagementPlane.TPU.value == "tpu"
    assert WorkloadConsumer.CLOUD_TPU_API.value == "cloud-tpu-api"


def test_cataloged_entry_can_remain_unguided_without_conversion_evidence() -> None:
    """Recognition never invents the unit conversion required by guidance."""
    entry = AcceleratorCatalogEntry(
        group_id=CatalogGroupId.CLOUD_TPU_LEGACY,
        accelerator_id=AcceleratorId("future-tpu"),
        management_plane=ManagementPlane.TPU,
        workload_consumers=(WorkloadConsumer.CLOUD_TPU_API,),
        native_quota_unit=QuotaUnit("core"),
        conversion=None,
    )

    assert entry.conversion is None
    with pytest.raises(ValueError, match="cannot guide"):
        entry.require_guided_conversion()


def test_constraint_set_retains_exact_independent_slice_references() -> None:
    """Catalog grouping never synthesizes a combined quota identity or value."""
    regional = ConstraintReference(_slice("TPUS-PER-REGION"))
    global_ = ConstraintReference(_slice("TPUS-ALL-REGIONS"))

    constraint_set = AcceleratorConstraintSet(
        accelerator_id=AcceleratorId("tpu-v6e"),
        references=(regional, global_),
    )

    assert constraint_set.references == (regional, global_)
    assert constraint_set.references[0].slice_identity.quota_id == "TPUS-PER-REGION"
    assert constraint_set.references[1].slice_identity.quota_id == "TPUS-ALL-REGIONS"


def test_compute_machine_type_preserves_unknown_provider_lifecycle() -> None:
    """Generated-client enum expansion remains exact provider evidence."""
    lifecycle = ProviderSymbol("FUTURE_STATE", CatalogLifecycle)
    machine = ComputeMachineType(
        name="a3-highgpu-8g",
        zone="us-central1-a",
        guest_accelerators=(AcceleratorAttachment("nvidia-h100-80gb", 8),),
        lifecycle=lifecycle,
    )

    assert machine.lifecycle is lifecycle
    assert machine.lifecycle.known is None
    assert machine.lifecycle.raw == "FUTURE_STATE"
    assert ProviderSymbol("DEPRECATED", CatalogLifecycle).known is (
        CatalogLifecycle.DEPRECATED
    )


def test_tpu_live_catalog_records_keep_provider_strings_and_shapes_exact() -> None:
    """Live TPU location, topology, and runtime evidence is not catalog inference."""
    location = TpuLocation(
        name="projects/123/locations/us-central1-b",
        location_id="us-central1-b",
    )
    configuration = TpuAcceleratorConfig(version="V6E", topology="2x4")
    accelerator = TpuAcceleratorType(
        name="projects/123/locations/us-central1-b/acceleratorTypes/v6e-8",
        zone="us-central1-b",
        accelerator_type="v6e-8",
        configurations=(configuration,),
    )
    runtime = TpuRuntimeVersion(
        name="projects/123/locations/us-central1-b/runtimeVersions/tpu-ubuntu2204-base",
        zone="us-central1-b",
        version="tpu-ubuntu2204-base",
    )

    assert location.location_id == accelerator.zone == runtime.zone
    assert accelerator.configurations == (configuration,)
    assert accelerator.configurations[0].version == "V6E"
    assert runtime.version == "tpu-ubuntu2204-base"


def test_catalog_evidence_rejects_noncanonical_location_text() -> None:
    """Provider-neutral location fields retain canonical IDs only."""
    with pytest.raises(ValueError, match="canonical location ID"):
        TpuLocation(
            name="projects/123/locations/us-central1-b",
            location_id="us central1 b",
        )


def test_catalog_location_coverage_distinguishes_empty_failed_and_omitted() -> None:
    """An empty provider response never stands in for a failed location read."""
    empty = CatalogLocationCoverage(
        source=CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
        location="us-central1-b",
        expectation=LocationCoverageExpectation.EXPECTED,
        state=LocationCoverageState.EMPTY,
    )
    failure = Diagnostic(
        code=DiagnosticCode("catalog-location-failed"),
        severity=Severity.ERROR,
        phase=DiagnosticPhase("catalog-read"),
        source=DiagnosticSource("tpu-accelerator-types"),
        retry=RetryDisposition.AFTER_BACKOFF,
        message=RedactedText("The TPU accelerator list failed."),
    )
    failed = CatalogLocationCoverage(
        source=CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
        location="us-central1-c",
        expectation=LocationCoverageExpectation.EXPECTED,
        state=LocationCoverageState.FAILED,
        diagnostics=(failure,),
    )
    omitted = CatalogLocationCoverage(
        source=CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
        location="us-east1-a",
        expectation=LocationCoverageExpectation.REQUESTED,
        state=LocationCoverageState.NOT_SCANNED,
        diagnostics=(failure,),
    )

    assert empty.complete
    assert not failed.complete
    assert not omitted.complete
    assert empty.diagnostics == ()
    assert failed.diagnostics == (failure,)
