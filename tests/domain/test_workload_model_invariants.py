"""Workload-first domain model invariants at the public construction seams."""

from collections.abc import Callable
from dataclasses import replace
from typing import cast

import pytest

from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    DimensionSelector,
    LocationSelectionMode,
    ProvisioningModel,
    QuotaConstraintAssessment,
    QuotaConstraintRequirement,
    QuotaSelector,
    ResolutionFailureReason,
    ResolvedWorkloadLocation,
    ResolvedWorkloadRequirement,
    SemanticAcceleratorOverlay,
    SpecializedHardwareCatalog,
    SpecializedHardwareRecord,
    WorkloadCatalogEvidence,
    WorkloadKind,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import (
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogLifecycle,
    CatalogLocationCoverage,
    LocationCoverageExpectation,
    LocationCoverageState,
    ManagementPlane,
    UnitConversionEvidence,
    WorkloadConsumer,
)
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaContainerType,
    QuotaIncreaseEligibility,
    QuotaIneligibilityReason,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind


def _identity() -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
        "compute.googleapis.com",
        "GPUS-PER-GPU-FAMILY-per-project-region",
        NormalizedDimensions(
            (("gpu_family", "NVIDIA_B200"), ("region", "us-central1"))
        ),
        QuotaScope.REGIONAL,
    )


def _evidence(
    *,
    display_name: str = "GPUs per family per region",
    identity: EffectiveQuotaSliceIdentity | None = None,
    unit: QuotaUnit | None = None,
    locations: tuple[str, ...] = ("us-central1",),
) -> EffectiveQuotaEvidence:
    exact_identity = identity or _identity()
    return EffectiveQuotaEvidence(
        identity=exact_identity,
        effective_value=QuotaQuantity(16, unit or QuotaUnit("1")),
        metric="compute.googleapis.com/quota",
        declared_dimensions=tuple(key for key, _ in exact_identity.dimensions.items),
        applicable_locations=locations,
        eligibility=QuotaIncreaseEligibility(
            eligible=True,
            reason=ProviderSymbol(
                "INELIGIBILITY_REASON_UNSPECIFIED", QuotaIneligibilityReason
            ),
        ),
        fixed=False,
        concurrent=False,
        precise=True,
        refresh_interval=None,
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
        quota_display_name=display_name,
    )


def _selector(*, quota_id: str | None = None) -> QuotaSelector:
    return QuotaSelector(
        service="compute.googleapis.com",
        quota_id=quota_id,
        quota_display_name="GPUs per family per region",
        dimensions=(
            DimensionSelector("gpu_family", "NVIDIA_B200"),
            DimensionSelector("region"),
        ),
        native_unit=QuotaUnit("1"),
        quota_scope=QuotaScope.REGIONAL,
        location_dimension="region",
    )


def _compatible_location() -> ResolvedWorkloadLocation:
    identity = _identity()
    conversion = UnitConversionEvidence(
        source_unit="card",
        quota_unit=QuotaUnit("1"),
        quota_units_per_source=1,
        source_reference="https://docs.cloud.google.com/compute/resource-usage",
    )
    constraint_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-b200"),
        (ConstraintReference(identity),),
    )
    requirement = QuotaConstraintRequirement(
        identity,
        8,
        QuotaQuantity(8, QuotaUnit("1")),
        conversion,
    )
    assessment = QuotaConstraintAssessment(
        identity,
        effective=QuotaQuantity(16, QuotaUnit("1")),
        usage=QuotaQuantity(8, QuotaUnit("1")),
        required=QuotaQuantity(8, QuotaUnit("1")),
        permits=True,
    )
    return ResolvedWorkloadLocation(
        location="us-central1-a",
        disposition=WorkloadLocationDisposition.COMPATIBLE,
        accelerator_id=AcceleratorId("nvidia-b200"),
        owning_service="compute.googleapis.com",
        management_plane=ManagementPlane.COMPUTE,
        supported_consumers=(
            WorkloadConsumer.COMPUTE_ENGINE,
            WorkloadConsumer.GKE,
        ),
        quota_pool="standard",
        deployable_accelerator_quantity=8,
        constraint_set=constraint_set,
        constraint_requirements=(requirement,),
        coverage=(),
        assessments=(assessment,),
    )


def _unresolved_location(location: str) -> ResolvedWorkloadLocation:
    return ResolvedWorkloadLocation(
        location=location,
        disposition=WorkloadLocationDisposition.INCOMPLETE,
        accelerator_id=None,
        owning_service=None,
        management_plane=None,
        supported_consumers=(),
        quota_pool=None,
        deployable_accelerator_quantity=None,
        constraint_set=None,
        constraint_requirements=(),
        coverage=(),
        failure_reason=ResolutionFailureReason.MISSING_LOCATION_EVIDENCE,
    )


def test_workload_inputs_expose_stable_discriminators_and_ordered_candidates() -> None:
    """Public workload-first shapes retain exact selection and kind semantics."""
    locations = CandidateLocations(("us-east1", "us-central1-a"))
    compute = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=2,
        provisioning_model=ProvisioningModel.SPOT,
        locations=locations,
    )
    tpu = CloudTpuSliceRequirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count=3,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=AllCompatibleLocations(),
    )

    assert locations.values == ("us-east1", "us-central1-a")
    assert locations.mode is LocationSelectionMode.CANDIDATES
    assert compute.kind is WorkloadKind.COMPUTE_INSTANCE
    assert tpu.kind is WorkloadKind.CLOUD_TPU_SLICE
    assert tpu.locations.mode is LocationSelectionMode.ALL_COMPATIBLE


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ((), "canonical regions or zones"),
        (["us-central1-a"], "canonical regions or zones"),
        (("global",), "canonical regions or zones"),
        (("us-central",), "canonical regions or zones"),
        (("US-central1-a",), "canonical regions or zones"),
        (("us-central1-a", "us-central1-a"), "unique"),
    ],
)
def test_candidate_locations_reject_ambiguous_or_noncanonical_selections(
    values: object,
    message: str,
) -> None:
    """Candidate enumeration cannot silently normalize or deduplicate locations."""
    with pytest.raises(ValueError, match=message):
        CandidateLocations(cast("tuple[str, ...]", values))


@pytest.mark.parametrize(
    ("requirement", "message"),
    [
        (
            lambda: ComputeInstanceRequirement(
                machine_type="",
                instance_count=1,
                provisioning_model=ProvisioningModel.STANDARD,
                locations=CandidateLocations(("us-central1-a",)),
            ),
            "machine_type must be non-empty",
        ),
        (
            lambda: ComputeInstanceRequirement(
                machine_type="a4-highgpu-8g",
                instance_count=0,
                provisioning_model=ProvisioningModel.STANDARD,
                locations=CandidateLocations(("us-central1-a",)),
            ),
            "instance_count must be a positive integer",
        ),
        (
            lambda: ComputeInstanceRequirement(
                machine_type="a4-highgpu-8g",
                instance_count=True,
                provisioning_model=ProvisioningModel.STANDARD,
                locations=CandidateLocations(("us-central1-a",)),
            ),
            "instance_count must be a positive integer",
        ),
        (
            lambda: CloudTpuSliceRequirement(
                accelerator_type="v6e-8",
                topology="",
                runtime_version="tpu-vm-base",
                slice_count=1,
                provisioning_model=ProvisioningModel.STANDARD,
                locations=CandidateLocations(("us-central1-b",)),
            ),
            "topology must be non-empty",
        ),
        (
            lambda: CloudTpuSliceRequirement(
                accelerator_type="v6e-8",
                topology="2x4",
                runtime_version="tpu-vm-base",
                slice_count=-1,
                provisioning_model=ProvisioningModel.STANDARD,
                locations=CandidateLocations(("us-central1-b",)),
            ),
            "slice_count must be a positive integer",
        ),
    ],
)
def test_workload_inputs_reject_incomplete_or_nonpositive_shapes(
    requirement: Callable[[], object],
    message: str,
) -> None:
    """Resolution inputs must describe a positive deployable shape."""
    with pytest.raises(ValueError, match=message):
        requirement()


def test_workload_inputs_reject_untyped_modes_and_incomplete_tpu_identity() -> None:
    """Workload shapes cannot normalize strings into domain enums or selectors."""
    candidate = CandidateLocations(("us-central1-a",))
    with pytest.raises(TypeError, match="ProvisioningModel"):
        ComputeInstanceRequirement(
            machine_type="a4-highgpu-8g",
            instance_count=1,
            provisioning_model=cast("ProvisioningModel", "standard"),
            locations=candidate,
        )
    with pytest.raises(TypeError, match="select candidates"):
        ComputeInstanceRequirement(
            machine_type="a4-highgpu-8g",
            instance_count=1,
            provisioning_model=ProvisioningModel.STANDARD,
            locations=cast("CandidateLocations", "us-central1-a"),
        )
    with pytest.raises(ValueError, match="accelerator_type must be non-empty"):
        CloudTpuSliceRequirement(
            accelerator_type="",
            topology="2x4",
            runtime_version="tpu-vm-base",
            slice_count=1,
            provisioning_model=ProvisioningModel.STANDARD,
            locations=candidate,
        )
    with pytest.raises(ValueError, match="runtime_version must be non-empty"):
        CloudTpuSliceRequirement(
            accelerator_type="v6e-8",
            topology="2x4",
            runtime_version="",
            slice_count=1,
            provisioning_model=ProvisioningModel.STANDARD,
            locations=candidate,
        )
    with pytest.raises(TypeError, match="ProvisioningModel"):
        CloudTpuSliceRequirement(
            accelerator_type="v6e-8",
            topology="2x4",
            runtime_version="tpu-vm-base",
            slice_count=1,
            provisioning_model=cast("ProvisioningModel", "standard"),
            locations=candidate,
        )
    with pytest.raises(TypeError, match="select candidates"):
        CloudTpuSliceRequirement(
            accelerator_type="v6e-8",
            topology="2x4",
            runtime_version="tpu-vm-base",
            slice_count=1,
            provisioning_model=ProvisioningModel.STANDARD,
            locations=cast("CandidateLocations", "us-central1-a"),
        )


def test_quota_selector_uses_display_name_only_without_an_exact_quota_id() -> None:
    """A display-name selector remains strict about every other identity field."""
    selector = _selector()

    assert selector.matches(_evidence())
    assert not selector.matches(_evidence(display_name="Provider-renamed label"))
    assert not selector.matches(
        _evidence(identity=replace(_identity(), service="tpu.googleapis.com"))
    )
    assert not selector.matches(_evidence(unit=QuotaUnit("GiBy")))
    assert not selector.matches(
        _evidence(
            identity=replace(
                _identity(),
                dimensions=NormalizedDimensions((("gpu_family", "NVIDIA_B200"),)),
            )
        )
    )
    assert not selector.matches(
        _evidence(
            identity=replace(
                _identity(),
                dimensions=NormalizedDimensions(
                    (
                        ("gpu_family", "NVIDIA_H100"),
                        ("region", "us-central1"),
                    )
                ),
            )
        )
    )
    assert not selector.matches(_evidence(locations=("us-east1",)))


@pytest.mark.parametrize(
    ("invalid_selector", "error_type", "message"),
    [
        (
            lambda: replace(_selector(quota_id="exact-id"), service="compute"),
            ValueError,
            "canonical service",
        ),
        (
            lambda: replace(
                _selector(quota_id="exact-id"),
                quota_id=None,
                quota_display_name=None,
            ),
            ValueError,
            "requires a quota ID",
        ),
        (
            lambda: replace(
                _selector(quota_id="exact-id"),
                dimensions=(
                    DimensionSelector("region"),
                    DimensionSelector("region"),
                ),
            ),
            ValueError,
            "unique",
        ),
        (
            lambda: replace(
                _selector(quota_id="exact-id"),
                location_dimension="zone",
            ),
            ValueError,
            "required dimension key",
        ),
        (
            lambda: replace(
                _selector(quota_id="exact-id"),
                native_unit=cast("QuotaUnit", "1"),
            ),
            TypeError,
            "QuotaUnit",
        ),
        (
            lambda: replace(
                _selector(quota_id="exact-id"),
                quota_scope=cast("QuotaScope", "regional"),
            ),
            TypeError,
            "QuotaScope",
        ),
    ],
)
def test_quota_selector_rejects_underspecified_provider_identity(
    invalid_selector: Callable[[], object],
    error_type: type[Exception],
    message: str,
) -> None:
    """Maintained selectors cannot accept approximate provider identity."""
    with pytest.raises(error_type, match=message):
        invalid_selector()


def test_specialized_hardware_record_keeps_discovery_and_guidance_distinct() -> None:
    """Provider declarations remain exact and guidance requires maintained identity."""
    known = SpecializedHardwareRecord(
        service="compute.googleapis.com",
        provider_accelerator_type="nvidia-b200",
        location="us-central1-a",
        accelerator_id=AcceleratorId("nvidia-b200"),
        guided=True,
        lifecycle=ProviderSymbol("ACTIVE", CatalogLifecycle),
    )

    assert known.guided
    with pytest.raises(ValueError, match="V1 inventory"):
        replace(known, service="example.googleapis.com")
    with pytest.raises(ValueError, match="provider accelerator type"):
        replace(known, provider_accelerator_type="")
    with pytest.raises(ValueError, match="canonical zone"):
        replace(known, location="us-central1")
    with pytest.raises(TypeError, match="AcceleratorId"):
        replace(known, accelerator_id=cast("AcceleratorId", "nvidia-b200"))
    with pytest.raises(TypeError, match="must be boolean"):
        replace(known, guided=cast("bool", 1))
    with pytest.raises(ValueError, match="maintained accelerator identity"):
        replace(known, accelerator_id=None)
    wrong_lifecycle = ProviderSymbol("ACTIVE", QuotaIneligibilityReason)
    with pytest.raises(TypeError, match="preserve provider text"):
        replace(
            known,
            lifecycle=cast("ProviderSymbol[CatalogLifecycle]", wrong_lifecycle),
        )


def test_workload_catalog_evidence_requires_immutable_typed_provider_values() -> None:
    """Catalog evidence cannot admit mutable or cross-source values."""
    assert WorkloadCatalogEvidence.empty() == WorkloadCatalogEvidence(
        (), (), (), (), ()
    )

    with pytest.raises(TypeError, match="typed tuples"):
        WorkloadCatalogEvidence([], (), (), (), ())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="typed tuples"):
        WorkloadCatalogEvidence((), (), (), (), ("not-coverage",))  # type: ignore[arg-type]


def test_specialized_hardware_catalog_rejects_unproven_exhaustiveness() -> None:
    """The exhaustive flag must be derived from complete required-source coverage."""
    complete = tuple(
        CatalogLocationCoverage(
            source,
            "global",
            LocationCoverageExpectation.EXPECTED,
            LocationCoverageState.EMPTY,
        )
        for source in CatalogEvidenceSource
    )
    incomplete = complete[:-1]

    assert SpecializedHardwareCatalog((), complete, exhaustive=True).exhaustive
    assert not SpecializedHardwareCatalog((), incomplete, exhaustive=False).exhaustive
    with pytest.raises(ValueError, match="must match recorded coverage"):
        SpecializedHardwareCatalog((), complete, exhaustive=False)
    with pytest.raises(ValueError, match="must match recorded coverage"):
        SpecializedHardwareCatalog((), incomplete, exhaustive=True)


def test_specialized_hardware_catalog_rejects_untyped_claim_components() -> None:
    """Inventory completeness cannot be asserted over mutable or untyped evidence."""
    with pytest.raises(TypeError, match="records must be typed tuples"):
        SpecializedHardwareCatalog(
            cast("tuple[SpecializedHardwareRecord, ...]", []),
            (),
            exhaustive=False,
        )
    with pytest.raises(TypeError, match="coverage must be typed tuples"):
        SpecializedHardwareCatalog(
            (),
            cast("tuple[CatalogLocationCoverage, ...]", ["coverage"]),
            exhaustive=False,
        )
    with pytest.raises(TypeError, match="exhaustive must be boolean"):
        SpecializedHardwareCatalog((), (), exhaustive=cast("bool", 0))


def test_quota_assessment_derives_sufficiency_from_each_native_slice() -> None:
    """A limiting slice permits only when usage plus demand fits effective quota."""
    identity = _identity()
    permitted = QuotaConstraintAssessment(
        identity,
        effective=QuotaQuantity(16, QuotaUnit("1")),
        usage=QuotaQuantity(8, QuotaUnit("1")),
        required=QuotaQuantity(8, QuotaUnit("1")),
        permits=True,
    )
    denied = QuotaConstraintAssessment(
        identity,
        effective=QuotaQuantity(15, QuotaUnit("1")),
        usage=QuotaQuantity(8, QuotaUnit("1")),
        required=QuotaQuantity(8, QuotaUnit("1")),
        permits=False,
    )

    assert permitted.permits
    assert not denied.permits
    with pytest.raises(ValueError, match="one native unit"):
        replace(permitted, required=QuotaQuantity(8, QuotaUnit("GiBy")))
    with pytest.raises(ValueError, match="non-negative"):
        replace(permitted, usage=QuotaQuantity(-1, QuotaUnit("1")))
    with pytest.raises(ValueError, match="must equal"):
        replace(permitted, permits=False)


def test_quota_constraints_reject_untyped_identity_and_conversion_evidence() -> None:
    """Per-slice demand and sufficiency require exact typed provider identity."""
    identity = _identity()
    conversion = UnitConversionEvidence(
        source_unit="card",
        quota_unit=QuotaUnit("1"),
        quota_units_per_source=1,
        source_reference="https://docs.cloud.google.com/compute/resource-usage",
    )
    assessment = QuotaConstraintAssessment(
        identity,
        effective=QuotaQuantity(16, QuotaUnit("1")),
        usage=QuotaQuantity(8, QuotaUnit("1")),
        required=QuotaQuantity(8, QuotaUnit("1")),
        permits=True,
    )
    requirement = QuotaConstraintRequirement(
        identity,
        8,
        QuotaQuantity(8, QuotaUnit("1")),
        conversion,
    )

    with pytest.raises(TypeError, match="exact slice identity"):
        replace(assessment, identity=cast("EffectiveQuotaSliceIdentity", "quota"))
    with pytest.raises(TypeError, match="must be QuotaQuantity"):
        replace(assessment, effective=cast("QuotaQuantity", 16))
    with pytest.raises(TypeError, match="must be boolean"):
        replace(assessment, permits=cast("bool", 1))
    with pytest.raises(TypeError, match="exact slice identity"):
        replace(requirement, identity=cast("EffectiveQuotaSliceIdentity", "quota"))
    with pytest.raises(TypeError, match="must be QuotaQuantity"):
        replace(requirement, required=cast("QuotaQuantity", 8))
    with pytest.raises(ValueError, match="positive integer"):
        replace(requirement, source_quantity=0)
    with pytest.raises(TypeError, match="must be UnitConversionEvidence"):
        replace(
            requirement,
            conversion=cast("UnitConversionEvidence", "one-card"),
        )
    with pytest.raises(ValueError, match="one native unit"):
        replace(
            requirement,
            required=QuotaQuantity(8, QuotaUnit("GiBy")),
        )
    with pytest.raises(ValueError, match="source quantity times conversion"):
        replace(requirement, required=QuotaQuantity(7, QuotaUnit("1")))


def test_resolved_location_keeps_constraints_and_assessments_aligned() -> None:
    """Per-location sufficiency cannot drift from its exact constraint set."""
    location = _compatible_location()
    denied = replace(
        location.assessments[0],
        effective=QuotaQuantity(15, QuotaUnit("1")),
        permits=False,
    )

    assert location.permits is True
    assert replace(location, assessments=(denied,)).permits is False
    assert replace(location, assessments=()).permits is None
    with pytest.raises(ValueError, match="complete derived quota facts"):
        replace(location, accelerator_id=None)
    with pytest.raises(ValueError, match="cannot carry a failure reason"):
        replace(location, failure_reason=ResolutionFailureReason.AMBIGUOUS)
    with pytest.raises(ValueError, match="cover every exact slice"):
        replace(location, constraint_requirements=())
    replacement = replace(
        location.constraint_requirements[0],
        source_quantity=7,
        required=QuotaQuantity(7, QuotaUnit("1")),
    )
    with pytest.raises(ValueError, match="each exact constraint requirement"):
        replace(location, constraint_requirements=(replacement,))


def test_unresolved_location_cannot_claim_resolved_quota_facts() -> None:
    """Failed compatibility remains explicit and cannot masquerade as guidance."""
    unresolved = _unresolved_location("us-central1-a")

    assert unresolved.permits is None
    with pytest.raises(ValueError, match="requires a failure reason"):
        replace(unresolved, failure_reason=None)
    compatible = _compatible_location()
    with pytest.raises(ValueError, match="cannot claim constraint requirements"):
        replace(
            unresolved,
            constraint_requirements=compatible.constraint_requirements,
        )


def test_resolved_requirement_preserves_candidate_order_and_coverage_semantics() -> (
    None
):
    """Candidate and all-compatible results expose different completeness claims."""
    candidates = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-east1", "us-central1-a")),
    )
    east = _unresolved_location("us-east1")
    central = _unresolved_location("us-central1-a")
    result = ResolvedWorkloadRequirement(candidates, (east, central), None)

    assert result.locations == (east, central)
    with pytest.raises(ValueError, match="caller order"):
        replace(result, locations=(central, east))
    with pytest.raises(ValueError, match="unique typed"):
        replace(result, locations=(east, east))
    with pytest.raises(ValueError, match="cannot claim exhaustive"):
        replace(result, all_compatible_locations_exhaustive=False)

    all_compatible = replace(candidates, locations=AllCompatibleLocations())
    exhaustive = ResolvedWorkloadRequirement(
        all_compatible,
        (),
        all_compatible_locations_exhaustive=True,
    )
    assert exhaustive.all_compatible_locations_exhaustive is True
    with pytest.raises(TypeError, match="must state exhaustive"):
        replace(exhaustive, all_compatible_locations_exhaustive=None)


def test_overlay_public_operations_reject_untyped_boundary_values() -> None:
    """Overlay joins fail closed instead of coercing malformed provider evidence."""
    overlay = MAINTAINED_ACCELERATOR_OVERLAY
    evidence = _evidence()
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )

    with pytest.raises(TypeError, match="must be EffectiveQuotaEvidence"):
        _selector().matches(cast("EffectiveQuotaEvidence", "quota"))
    with pytest.raises(TypeError, match="requires EffectiveQuotaEvidence"):
        overlay.classify(
            cast("EffectiveQuotaEvidence", "quota"),
            freshly_validated_mutable=False,
        )
    with pytest.raises(TypeError, match="must be bool"):
        overlay.classify(
            evidence,
            freshly_validated_mutable=cast("bool", 1),
        )
    with pytest.raises(TypeError, match="must be an AcceleratorId"):
        overlay.constraint_set(
            cast("AcceleratorId", "nvidia-b200"),
            evidence,
            (evidence,),
        )
    with pytest.raises(TypeError, match="must be EffectiveQuotaEvidence"):
        overlay.constraint_set(
            AcceleratorId("nvidia-b200"),
            cast("EffectiveQuotaEvidence", "quota"),
            (evidence,),
        )
    with pytest.raises(TypeError, match="EffectiveQuotaEvidence values"):
        overlay.constraint_set(
            AcceleratorId("nvidia-b200"),
            evidence,
            cast("tuple[EffectiveQuotaEvidence, ...]", [evidence]),
        )
    with pytest.raises(TypeError, match="must be EffectiveQuotaEvidence"):
        overlay.constraint_sets(
            cast("EffectiveQuotaEvidence", "quota"),
            (evidence,),
        )
    with pytest.raises(TypeError, match="EffectiveQuotaEvidence values"):
        overlay.constraint_sets(
            evidence,
            cast("tuple[EffectiveQuotaEvidence, ...]", [evidence]),
        )
    with pytest.raises(TypeError, match="requires WorkloadCatalogEvidence"):
        overlay.discover_specialized_hardware(
            cast("WorkloadCatalogEvidence", "catalog")
        )
    with pytest.raises(TypeError, match="compute-instance or cloud-tpu-slice"):
        overlay.resolve(
            cast("ComputeInstanceRequirement", "workload"),
            (),
            WorkloadCatalogEvidence.empty(),
        )
    with pytest.raises(TypeError, match="must be EffectiveQuotaEvidence values"):
        overlay.resolve(
            requirement,
            cast("tuple[EffectiveQuotaEvidence, ...]", []),
            WorkloadCatalogEvidence.empty(),
        )
    with pytest.raises(TypeError, match="must be WorkloadCatalogEvidence"):
        overlay.resolve(
            requirement,
            (),
            cast("WorkloadCatalogEvidence", "catalog"),
        )


def test_overlay_rejects_duplicate_semantic_mapping_identity() -> None:
    """One catalog identity cannot resolve through two maintained mappings."""
    mapping = MAINTAINED_ACCELERATOR_OVERLAY.mappings[0]

    with pytest.raises(ValueError, match="identities must be unique"):
        SemanticAcceleratorOverlay((mapping, mapping))
