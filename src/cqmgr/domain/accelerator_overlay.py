"""Maintained accelerator semantics joined conservatively to live quota evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogEvidenceSource,
    CatalogGroupId,
    CatalogLifecycle,
    CatalogLocationCoverage,
    CatalogMetadata,
    CatalogPredicates,
    ComputeAcceleratorType,
    ComputeMachineType,
    LocationCoverageState,
    ManagementPlane,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
    UnitConversionEvidence,
    WorkloadConsumer,
)
from cqmgr.domain.quota_queries import QuotaQueryItem
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.schemas import ProviderSymbol

_REVIEW_DATE = date(2026, 7, 14)
_COMPUTE_QUOTA_SOURCE = "https://docs.cloud.google.com/compute/resource-usage"
_TPU_QUOTA_SOURCE = "https://docs.cloud.google.com/tpu/docs/quota"
_MIN_DNS_LABELS = 2
_LOCATION_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class AmbiguousOverlayMatchError(ValueError):
    """Raised when maintained selectors do not identify one semantic mapping."""


class ProvisioningModel(StrEnum):
    """Provisioning model relevant to maintained quota-pool selection."""

    STANDARD = "standard"
    SPOT = "spot"
    FLEX_START = "flex-start"
    RESERVATION_BOUND = "reservation-bound"


class WorkloadQuantityBasis(StrEnum):
    """Workload quantity independently converted for one quota constraint."""

    ACCELERATOR_QUANTITY = "accelerator-quantity"
    INSTANCE_COUNT = "instance-count"
    SLICE_COUNT = "slice-count"


class WorkloadKind(StrEnum):
    """Stable public discriminator for a workload-first input shape."""

    COMPUTE_INSTANCE = "compute-instance"
    CLOUD_TPU_SLICE = "cloud-tpu-slice"


class LocationSelectionMode(StrEnum):
    """Stable public discriminator for candidate enumeration semantics."""

    CANDIDATES = "candidates"
    ALL_COMPATIBLE = "all-compatible"


class ResolutionFailureReason(StrEnum):
    """Fail-closed workload-resolution reason without capacity semantics."""

    AMBIGUOUS = "ambiguous"
    UNSUPPORTED_CONVERSION = "unsupported-conversion"
    UNSUPPORTED_COMPATIBILITY = "unsupported-compatibility"
    PROVIDER_IDENTITY = "provider-identity"
    MISSING_LOCATION_EVIDENCE = "missing-location-evidence"
    INELIGIBLE = "ineligible"


class WorkloadResolutionError(ValueError):
    """Typed failure to resolve one workload to exact quota constraints."""

    reason: ResolutionFailureReason

    def __init__(self, reason: ResolutionFailureReason, message: str) -> None:
        """Retain a stable machine-readable reason alongside the message."""
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CandidateLocations:
    """Explicit candidate locations retained in caller order."""

    mode: LocationSelectionMode = field(
        init=False,
        default=LocationSelectionMode.CANDIDATES,
    )
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require one or more unique canonical zones."""
        if (
            not isinstance(self.values, tuple)
            or not self.values
            or any(not _is_canonical_zone(value) for value in self.values)
        ):
            msg = "candidate locations must contain canonical zones"
            raise ValueError(msg)
        if len(set(self.values)) != len(self.values):
            msg = "candidate locations must be unique"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AllCompatibleLocations:
    """Request every location proven compatible by covered provider evidence."""

    mode: LocationSelectionMode = field(
        init=False,
        default=LocationSelectionMode.ALL_COMPATIBLE,
    )


LocationSelection = CandidateLocations | AllCompatibleLocations


@dataclass(frozen=True, slots=True)
class ComputeInstanceRequirement:
    """Deployable Compute instance shape without caller-supplied accelerator facts."""

    kind: WorkloadKind = field(init=False, default=WorkloadKind.COMPUTE_INSTANCE)
    machine_type: str
    instance_count: int
    provisioning_model: ProvisioningModel
    locations: LocationSelection

    def __post_init__(self) -> None:
        """Require the complete public Compute-instance input shape."""
        _require_nonempty(self.machine_type, "machine_type")
        _require_positive_count(self.instance_count, "instance_count")
        if not isinstance(self.provisioning_model, ProvisioningModel):
            msg = "provisioning_model must be a ProvisioningModel"
            raise TypeError(msg)
        if not isinstance(self.locations, (CandidateLocations, AllCompatibleLocations)):
            msg = "locations must select candidates or all compatible locations"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class CloudTpuSliceRequirement:
    """Deployable Cloud TPU slice shape without derived quota selectors."""

    kind: WorkloadKind = field(init=False, default=WorkloadKind.CLOUD_TPU_SLICE)
    accelerator_type: str
    topology: str
    runtime_version: str
    slice_count: int
    provisioning_model: ProvisioningModel
    locations: LocationSelection

    def __post_init__(self) -> None:
        """Require the complete public Cloud-TPU-slice input shape."""
        _require_nonempty(self.accelerator_type, "accelerator_type")
        _require_nonempty(self.topology, "topology")
        _require_nonempty(self.runtime_version, "runtime_version")
        _require_positive_count(self.slice_count, "slice_count")
        if not isinstance(self.provisioning_model, ProvisioningModel):
            msg = "provisioning_model must be a ProvisioningModel"
            raise TypeError(msg)
        if not isinstance(self.locations, (CandidateLocations, AllCompatibleLocations)):
            msg = "locations must select candidates or all compatible locations"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class DimensionSelector:
    """One required live dimension key and optional exact provider value."""

    key: str
    value: str | None = None

    def __post_init__(self) -> None:
        """Preserve exact provider spelling while rejecting empty selectors."""
        _require_nonempty(self.key, "dimension selector key")
        if self.value is not None:
            _require_nonempty(self.value, "dimension selector value")


@dataclass(frozen=True, slots=True)
class QuotaSelector:
    """Exact maintained recognition evidence for one kind of live quota slice."""

    service: str
    quota_id: str | None
    quota_display_name: str | None
    dimensions: tuple[DimensionSelector, ...]
    native_unit: QuotaUnit
    quota_scope: QuotaScope
    location_dimension: str | None

    def __post_init__(self) -> None:
        """Require enough exact evidence to recognize without inventing identity."""
        if not _is_canonical_service_dns(self.service):
            msg = "selector service must be a canonical service DNS name"
            raise ValueError(msg)
        for name, value in (
            ("quota_id", self.quota_id),
            ("quota_display_name", self.quota_display_name),
        ):
            if value is not None:
                _require_nonempty(value, name)
        if self.quota_id is None and self.quota_display_name is None:
            msg = "selector requires a quota ID or documented display name"
            raise ValueError(msg)
        if (
            not isinstance(self.dimensions, tuple)
            or any(
                not isinstance(dimension, DimensionSelector)
                for dimension in self.dimensions
            )
            or len({dimension.key for dimension in self.dimensions})
            != len(self.dimensions)
        ):
            msg = "selector dimensions must have unique DimensionSelector keys"
            raise ValueError(msg)
        if not isinstance(self.native_unit, QuotaUnit):
            msg = "selector native_unit must be a QuotaUnit"
            raise TypeError(msg)
        if not isinstance(self.quota_scope, QuotaScope):
            msg = "selector quota_scope must be a QuotaScope"
            raise TypeError(msg)
        if self.location_dimension is not None:
            _require_nonempty(self.location_dimension, "location_dimension")
            if self.location_dimension not in {
                dimension.key for dimension in self.dimensions
            }:
                msg = "location_dimension must be a required dimension key"
                raise ValueError(msg)

    def matches(self, evidence: EffectiveQuotaEvidence) -> bool:
        """Recognize only exact authoritative evidence required by this selector."""
        if not isinstance(evidence, EffectiveQuotaEvidence):
            msg = "selector evidence must be EffectiveQuotaEvidence"
            raise TypeError(msg)
        identity = evidence.identity
        if (
            identity.service != self.service
            or identity.quota_scope is not self.quota_scope
            or evidence.effective_value.unit != self.native_unit
        ):
            return False
        if self.quota_id is not None:
            if identity.quota_id != self.quota_id:
                return False
        elif evidence.quota_display_name != self.quota_display_name:
            return False
        live_dimensions = dict(identity.dimensions.items)
        selector_dimension_keys = {dimension.key for dimension in self.dimensions}
        if set(live_dimensions) != selector_dimension_keys:
            return False
        if any(
            dimension.value is not None
            and live_dimensions[dimension.key] != dimension.value
            for dimension in self.dimensions
        ):
            return False
        return self.location_dimension is None or (
            live_dimensions[self.location_dimension] in evidence.applicable_locations
        )


@dataclass(frozen=True, slots=True)
class CompanionRequirementMapping:
    """One companion selector with its own workload basis and conversion."""

    selector: QuotaSelector
    quantity_basis: WorkloadQuantityBasis
    conversion: UnitConversionEvidence

    def __post_init__(self) -> None:
        """Require typed, native-unit-compatible companion evidence."""
        if not isinstance(self.selector, QuotaSelector):
            msg = "companion requirement selector must be a QuotaSelector"
            raise TypeError(msg)
        if not isinstance(self.quantity_basis, WorkloadQuantityBasis):
            msg = "companion quantity_basis must be a WorkloadQuantityBasis"
            raise TypeError(msg)
        if not isinstance(self.conversion, UnitConversionEvidence):
            msg = "companion conversion must be UnitConversionEvidence"
            raise TypeError(msg)
        if self.conversion.quota_unit != self.selector.native_unit:
            msg = "companion conversion must use the selector native unit"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WorkloadCatalogEvidence:
    """Provider-neutral live catalog and per-location coverage for resolution."""

    compute_machine_types: tuple[ComputeMachineType, ...]
    tpu_locations: tuple[TpuLocation, ...]
    tpu_accelerator_types: tuple[TpuAcceleratorType, ...]
    tpu_runtime_versions: tuple[TpuRuntimeVersion, ...]
    coverage: tuple[CatalogLocationCoverage, ...]
    compute_accelerator_types: tuple[ComputeAcceleratorType, ...] = ()

    def __post_init__(self) -> None:
        """Require immutable evidence values from the declared provider ports."""
        fields_and_types = (
            (self.compute_machine_types, ComputeMachineType),
            (self.tpu_locations, TpuLocation),
            (self.tpu_accelerator_types, TpuAcceleratorType),
            (self.tpu_runtime_versions, TpuRuntimeVersion),
            (self.coverage, CatalogLocationCoverage),
            (self.compute_accelerator_types, ComputeAcceleratorType),
        )
        if any(
            not isinstance(values, tuple)
            or any(not isinstance(value, expected) for value in values)
            for values, expected in fields_and_types
        ):
            msg = "workload catalog evidence fields must contain typed tuples"
            raise TypeError(msg)

    @classmethod
    def empty(cls) -> WorkloadCatalogEvidence:
        """Return explicit empty evidence for fail-closed resolution tests."""
        return cls((), (), (), (), ())


@dataclass(frozen=True, slots=True)
class SpecializedHardwareRecord:
    """One provider-declared specialized-hardware identity at one location."""

    service: str
    provider_accelerator_type: str
    location: str
    accelerator_id: AcceleratorId | None
    guided: bool
    lifecycle: ProviderSymbol[CatalogLifecycle] | None

    def __post_init__(self) -> None:
        """Keep discovery authoritative while guidance remains fail-closed."""
        if self.service not in {"compute.googleapis.com", "tpu.googleapis.com"}:
            msg = "specialized hardware service must belong to the V1 inventory"
            raise ValueError(msg)
        _require_nonempty(self.provider_accelerator_type, "provider accelerator type")
        _require_canonical_zone(self.location, "specialized hardware location")
        if self.accelerator_id is not None and not isinstance(
            self.accelerator_id, AcceleratorId
        ):
            msg = "specialized hardware accelerator_id must be AcceleratorId"
            raise TypeError(msg)
        if not isinstance(self.guided, bool):
            msg = "specialized hardware guided must be boolean"
            raise TypeError(msg)
        if self.guided and self.accelerator_id is None:
            msg = "guided hardware requires a maintained accelerator identity"
            raise ValueError(msg)
        if self.lifecycle is not None and (
            not isinstance(self.lifecycle, ProviderSymbol)
            or self.lifecycle.enum_type is not CatalogLifecycle
        ):
            msg = "specialized hardware lifecycle must preserve provider text"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class SpecializedHardwareCatalog:
    """Release-relative provider declarations with an honest coverage claim."""

    records: tuple[SpecializedHardwareRecord, ...]
    coverage: tuple[CatalogLocationCoverage, ...]
    exhaustive: bool

    def __post_init__(self) -> None:
        """Forbid an exhaustive claim without every required subsource."""
        if not isinstance(self.records, tuple) or any(
            not isinstance(item, SpecializedHardwareRecord) for item in self.records
        ):
            msg = "specialized hardware records must be typed tuples"
            raise TypeError(msg)
        if not isinstance(self.coverage, tuple) or any(
            not isinstance(item, CatalogLocationCoverage) for item in self.coverage
        ):
            msg = "specialized hardware coverage must be typed tuples"
            raise TypeError(msg)
        if not isinstance(self.exhaustive, bool):
            msg = "specialized hardware exhaustive must be boolean"
            raise TypeError(msg)
        proven = _specialized_hardware_coverage_is_exhaustive(
            self.records, self.coverage
        )
        if self.exhaustive is not proven:
            msg = "specialized hardware exhaustive must match recorded coverage"
            raise ValueError(msg)


def _specialized_hardware_coverage_is_exhaustive(
    records: tuple[SpecializedHardwareRecord, ...],
    coverage: tuple[CatalogLocationCoverage, ...],
) -> bool:
    required_sources = set(CatalogEvidenceSource)
    covered_sources = {item.source for item in coverage}
    if required_sources > covered_sources or not all(
        item.complete for item in coverage
    ):
        return False
    record_sources = {
        "compute.googleapis.com": CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
        "tpu.googleapis.com": CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
    }
    complete_locations = {
        (item.source, item.location) for item in coverage if item.complete
    }
    return all(
        (record_sources[record.service], record.location) in complete_locations
        for record in records
    )


@dataclass(frozen=True, slots=True)
class QuotaConstraintAssessment:
    """Quota sufficiency for one independently limiting exact slice."""

    identity: EffectiveQuotaSliceIdentity
    effective: QuotaQuantity
    usage: QuotaQuantity
    required: QuotaQuantity
    permits: bool

    def __post_init__(self) -> None:
        """Require exact native-unit arithmetic and its derived conclusion."""
        if not isinstance(self.identity, EffectiveQuotaSliceIdentity):
            msg = "quota constraint assessment requires an exact slice identity"
            raise TypeError(msg)
        quantities = (self.effective, self.usage, self.required)
        if any(not isinstance(quantity, QuotaQuantity) for quantity in quantities):
            msg = "quota constraint assessment values must be QuotaQuantity"
            raise TypeError(msg)
        if len({quantity.unit for quantity in quantities}) != 1:
            msg = "quota constraint assessment values must use one native unit"
            raise ValueError(msg)
        if self.usage.value < 0:
            msg = "quota constraint assessment usage must be non-negative"
            raise ValueError(msg)
        if not isinstance(self.permits, bool):
            msg = "quota constraint assessment permits must be boolean"
            raise TypeError(msg)
        expected = self.usage.value + self.required.value <= self.effective.value
        if self.permits is not expected:
            msg = "quota constraint permits must equal usage plus required sufficiency"
            raise ValueError(msg)


class WorkloadLocationDisposition(StrEnum):
    """One independently evaluated candidate-location outcome."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class QuotaConstraintRequirement:
    """One exact limiting slice and its independently converted native demand."""

    identity: EffectiveQuotaSliceIdentity
    source_quantity: int
    required: QuotaQuantity
    conversion: UnitConversionEvidence

    def __post_init__(self) -> None:
        """Require demand to equal the declared source quantity and conversion."""
        if not isinstance(self.identity, EffectiveQuotaSliceIdentity):
            msg = "constraint requirement needs an exact slice identity"
            raise TypeError(msg)
        _require_positive_count(self.source_quantity, "constraint source_quantity")
        if not isinstance(self.required, QuotaQuantity):
            msg = "constraint required amount must be QuotaQuantity"
            raise TypeError(msg)
        if not isinstance(self.conversion, UnitConversionEvidence):
            msg = "constraint conversion must be UnitConversionEvidence"
            raise TypeError(msg)
        if self.required.unit != self.conversion.quota_unit:
            msg = "constraint requirement and conversion must use one native unit"
            raise ValueError(msg)
        if (
            self.required.value
            != self.source_quantity * self.conversion.quota_units_per_source
        ):
            msg = "constraint requirement must equal source quantity times conversion"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ResolvedWorkloadLocation:
    """Catalog-derived workload facts and exact constraints for one location."""

    location: str
    disposition: WorkloadLocationDisposition
    accelerator_id: AcceleratorId | None
    owning_service: str | None
    management_plane: ManagementPlane | None
    supported_consumers: tuple[WorkloadConsumer, ...]
    quota_pool: str | None
    deployable_accelerator_quantity: int | None
    constraint_set: AcceleratorConstraintSet | None
    constraint_requirements: tuple[QuotaConstraintRequirement, ...]
    coverage: tuple[CatalogLocationCoverage, ...]
    assessments: tuple[QuotaConstraintAssessment, ...] = ()
    failure_reason: ResolutionFailureReason | None = None

    def __post_init__(self) -> None:  # noqa: C901, PLR0912
        """Keep successful facts complete and failures explicit."""
        _require_canonical_zone(self.location, "resolved location")
        if not isinstance(self.disposition, WorkloadLocationDisposition):
            msg = "location disposition must be WorkloadLocationDisposition"
            raise TypeError(msg)
        if not isinstance(self.coverage, tuple) or any(
            not isinstance(item, CatalogLocationCoverage) for item in self.coverage
        ):
            msg = "location coverage must contain CatalogLocationCoverage"
            raise TypeError(msg)
        compatible = self.disposition is WorkloadLocationDisposition.COMPATIBLE
        resolved_values = (
            self.accelerator_id,
            self.owning_service,
            self.management_plane,
            self.quota_pool,
            self.deployable_accelerator_quantity,
            self.constraint_set,
        )
        if compatible and any(value is None for value in resolved_values):
            msg = "compatible location requires complete derived quota facts"
            raise ValueError(msg)
        if compatible and self.failure_reason is not None:
            msg = "compatible location cannot carry a failure reason"
            raise ValueError(msg)
        if not compatible and self.failure_reason is None:
            msg = "unresolved location requires a failure reason"
            raise ValueError(msg)
        if not isinstance(self.supported_consumers, tuple) or any(
            not isinstance(item, WorkloadConsumer) for item in self.supported_consumers
        ):
            msg = "supported_consumers must contain WorkloadConsumer values"
            raise TypeError(msg)
        if self.deployable_accelerator_quantity is not None:
            _require_positive_count(
                self.deployable_accelerator_quantity,
                "deployable_accelerator_quantity",
            )
        if not isinstance(self.constraint_requirements, tuple) or any(
            not isinstance(item, QuotaConstraintRequirement)
            for item in self.constraint_requirements
        ):
            msg = "constraint_requirements must contain QuotaConstraintRequirement"
            raise TypeError(msg)
        if compatible:
            constraint_set = self.constraint_set
            if constraint_set is None:
                msg = "compatible location requires an exact constraint set"
                raise ValueError(msg)
            expected = tuple(
                reference.slice_identity for reference in constraint_set.references
            )
            actual = tuple(item.identity for item in self.constraint_requirements)
            if actual != expected:
                msg = "constraint requirements must cover every exact slice in order"
                raise ValueError(msg)
        elif self.constraint_requirements:
            msg = "unresolved location cannot claim constraint requirements"
            raise ValueError(msg)
        if not isinstance(self.assessments, tuple) or any(
            not isinstance(item, QuotaConstraintAssessment) for item in self.assessments
        ):
            msg = "assessments must contain QuotaConstraintAssessment"
            raise TypeError(msg)
        if self.assessments:
            expected_assessments = tuple(
                (item.identity, item.required) for item in self.constraint_requirements
            )
            actual_assessments = tuple(
                (item.identity, item.required) for item in self.assessments
            )
            if actual_assessments != expected_assessments:
                msg = "assessments must use each exact constraint requirement"
                raise ValueError(msg)

    @property
    def permits(self) -> bool | None:
        """Whether every independently limiting assessed slice permits the shape."""
        if not self.assessments:
            return None
        return all(assessment.permits for assessment in self.assessments)


ModernWorkloadRequirement = ComputeInstanceRequirement | CloudTpuSliceRequirement


@dataclass(frozen=True, slots=True)
class ResolvedWorkloadRequirement:
    """One workload input evaluated independently at every selected location."""

    requirement: ModernWorkloadRequirement
    locations: tuple[ResolvedWorkloadLocation, ...]
    all_compatible_locations_exhaustive: bool | None

    def __post_init__(self) -> None:
        """Preserve one ordered result per independently evaluated location."""
        if not isinstance(
            self.requirement, (ComputeInstanceRequirement, CloudTpuSliceRequirement)
        ):
            msg = "resolved workload requires a public workload-first input"
            raise TypeError(msg)
        if (
            not isinstance(self.locations, tuple)
            or any(
                not isinstance(item, ResolvedWorkloadLocation)
                for item in self.locations
            )
            or len({item.location for item in self.locations}) != len(self.locations)
        ):
            msg = "resolved workload locations must be unique typed results"
            raise ValueError(msg)
        all_compatible = isinstance(self.requirement.locations, AllCompatibleLocations)
        if all_compatible and not isinstance(
            self.all_compatible_locations_exhaustive, bool
        ):
            msg = "all-compatible resolution must state exhaustive coverage"
            raise TypeError(msg)
        if not all_compatible and self.all_compatible_locations_exhaustive is not None:
            msg = "candidate resolution cannot claim exhaustive catalog coverage"
            raise ValueError(msg)
        if (
            isinstance(self.requirement.locations, CandidateLocations)
            and tuple(item.location for item in self.locations)
            != self.requirement.locations.values
        ):
            msg = "candidate resolution must retain every candidate in caller order"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class OverlayMapping:
    """One maintained semantic mapping with first-party provenance."""

    group_id: CatalogGroupId
    accelerator_id: AcceleratorId
    management_plane: ManagementPlane
    workload_consumers: tuple[WorkloadConsumer, ...]
    selector: QuotaSelector
    quota_pool: str
    conversion: UnitConversionEvidence | None
    companion_requirements: tuple[CompanionRequirementMapping, ...]
    source_url: str
    reviewed_on: date
    machine_types: tuple[str, ...] = ()
    provider_accelerator_types: tuple[str, ...] = ()
    topologies: tuple[str, ...] = ()
    runtime_versions: tuple[str, ...] = ()
    accelerator_counts: tuple[int, ...] = ()
    provisioning_models: tuple[ProvisioningModel, ...] = (ProvisioningModel.STANDARD,)

    def __post_init__(self) -> None:
        """Keep semantic identity, selector evidence, and provenance explicit."""
        _validate_mapping_identity(self)
        _validate_mapping_conversion(self)
        _validate_mapping_compatibility(self)

    @property
    def guided(self) -> bool:
        """Whether exact identity and conversion evidence support guidance."""
        return self.conversion is not None


def _validate_mapping_identity(mapping: OverlayMapping) -> None:
    if not isinstance(mapping.group_id, CatalogGroupId):
        msg = "mapping group_id must be a CatalogGroupId"
        raise TypeError(msg)
    if not isinstance(mapping.accelerator_id, AcceleratorId):
        msg = "mapping accelerator_id must be an AcceleratorId"
        raise TypeError(msg)
    if not isinstance(mapping.management_plane, ManagementPlane):
        msg = "mapping management_plane must be a ManagementPlane"
        raise TypeError(msg)
    if (
        not isinstance(mapping.workload_consumers, tuple)
        or not mapping.workload_consumers
        or any(
            not isinstance(consumer, WorkloadConsumer)
            for consumer in mapping.workload_consumers
        )
    ):
        msg = "mapping workload_consumers must be WorkloadConsumer values"
        raise TypeError(msg)
    if not isinstance(mapping.selector, QuotaSelector):
        msg = "mapping selector must be a QuotaSelector"
        raise TypeError(msg)
    if not _is_stable_id(mapping.quota_pool):
        msg = "mapping quota_pool must be a lowercase stable identifier"
        raise ValueError(msg)
    if not _is_official_source(mapping.source_url):
        msg = "mapping source_url must be an official Google Cloud HTTPS URL"
        raise ValueError(msg)
    if not isinstance(mapping.reviewed_on, date):
        msg = "mapping reviewed_on must be a date"
        raise TypeError(msg)


def _validate_mapping_conversion(mapping: OverlayMapping) -> None:
    if mapping.conversion is not None and not isinstance(
        mapping.conversion, UnitConversionEvidence
    ):
        msg = "mapping conversion must be UnitConversionEvidence or None"
        raise TypeError(msg)
    if (
        mapping.conversion is not None
        and mapping.conversion.quota_unit != mapping.selector.native_unit
    ):
        msg = "mapping conversion must use the selector native unit"
        raise ValueError(msg)
    if not isinstance(mapping.companion_requirements, tuple) or any(
        not isinstance(companion, CompanionRequirementMapping)
        for companion in mapping.companion_requirements
    ):
        msg = "companion_requirements must be CompanionRequirementMapping values"
        raise TypeError(msg)
    companion_selector_bytes = tuple(
        _selector_bytes(companion.selector)
        for companion in mapping.companion_requirements
    )
    if len(set(companion_selector_bytes)) != len(companion_selector_bytes):
        msg = "companion requirement selectors must be unique"
        raise ValueError(msg)
    if _selector_bytes(mapping.selector) in companion_selector_bytes:
        msg = "companion requirement selector must differ from the primary selector"
        raise ValueError(msg)
    for companion in mapping.companion_requirements:
        _validate_companion_quantity_basis(mapping, companion)


def _validate_companion_quantity_basis(
    mapping: OverlayMapping,
    companion: CompanionRequirementMapping,
) -> None:
    basis = companion.quantity_basis
    source_unit = companion.conversion.source_unit
    if basis is WorkloadQuantityBasis.INSTANCE_COUNT:
        if mapping.management_plane is not ManagementPlane.COMPUTE:
            msg = "instance-count companions require the Compute management plane"
            raise ValueError(msg)
        if source_unit != "instance":
            msg = "instance-count companion conversion source unit must be instance"
            raise ValueError(msg)
    elif basis is WorkloadQuantityBasis.SLICE_COUNT:
        if mapping.management_plane is not ManagementPlane.TPU:
            msg = "slice-count companions require the TPU management plane"
            raise ValueError(msg)
        if source_unit != "slice":
            msg = "slice-count companion conversion source unit must be slice"
            raise ValueError(msg)
    elif mapping.conversion is None:
        msg = "accelerator-quantity companions require a primary conversion"
        raise ValueError(msg)
    elif source_unit != mapping.conversion.source_unit:
        msg = "accelerator-quantity companion must use the primary source unit"
        raise ValueError(msg)


def _validate_mapping_compatibility(mapping: OverlayMapping) -> None:
    for field_name, values in (
        ("machine_types", mapping.machine_types),
        ("provider_accelerator_types", mapping.provider_accelerator_types),
        ("topologies", mapping.topologies),
        ("runtime_versions", mapping.runtime_versions),
    ):
        if not isinstance(values, tuple) or any(
            not isinstance(value, str) or not value for value in values
        ):
            msg = f"mapping {field_name} must contain non-empty strings"
            raise TypeError(msg)
    if not isinstance(mapping.accelerator_counts, tuple) or any(
        isinstance(count, bool) or not isinstance(count, int) or count <= 0
        for count in mapping.accelerator_counts
    ):
        msg = "mapping accelerator_counts must contain positive integers"
        raise TypeError(msg)
    if not isinstance(mapping.provisioning_models, tuple) or any(
        not isinstance(model, ProvisioningModel)
        for model in mapping.provisioning_models
    ):
        msg = "mapping provisioning_models must use ProvisioningModel values"
        raise TypeError(msg)


@dataclass(frozen=True, slots=True, init=False)
class SemanticAcceleratorOverlay:
    """Canonical immutable maintained mappings with deterministic identity."""

    mappings: tuple[OverlayMapping, ...]
    metadata: CatalogMetadata

    def __init__(self, mappings: tuple[OverlayMapping, ...]) -> None:
        """Canonicalize content and derive revision and SHA-256 identity."""
        if (
            not isinstance(mappings, tuple)
            or not mappings
            or any(not isinstance(mapping, OverlayMapping) for mapping in mappings)
        ):
            msg = "overlay mappings must be a non-empty tuple of OverlayMapping"
            raise ValueError(msg)
        canonical_mappings = tuple(sorted(mappings, key=_canonical_mapping_bytes))
        if len({_mapping_key(mapping) for mapping in canonical_mappings}) != len(
            canonical_mappings
        ):
            msg = "overlay mapping identities must be unique"
            raise ValueError(msg)
        revision = max(
            mapping.reviewed_on for mapping in canonical_mappings
        ).isoformat()
        canonical_content = b"".join(
            (
                _encode_text("schema", ACCELERATOR_CATALOG_SCHEMA),
                _encode_text("revision", revision),
                _encode_sequence(
                    "mappings",
                    tuple(
                        _canonical_mapping_bytes(mapping)
                        for mapping in canonical_mappings
                    ),
                ),
            )
        )
        digest = hashlib.sha256(canonical_content).hexdigest()
        object.__setattr__(self, "mappings", canonical_mappings)
        object.__setattr__(
            self,
            "metadata",
            CatalogMetadata(ACCELERATOR_CATALOG_SCHEMA, revision, f"sha256:{digest}"),
        )

    def classify(
        self,
        evidence: EffectiveQuotaEvidence,
        *,
        freshly_validated_mutable: bool,
    ) -> QuotaQueryItem:
        """Join semantics without replacing live identity or inferring mutability."""
        if not isinstance(evidence, EffectiveQuotaEvidence):
            msg = "overlay classification requires EffectiveQuotaEvidence"
            raise TypeError(msg)
        if not isinstance(freshly_validated_mutable, bool):
            msg = "freshly_validated_mutable must be bool"
            raise TypeError(msg)
        matches = tuple(
            mapping for mapping in self.mappings if mapping.selector.matches(evidence)
        )
        if len(matches) > 1:
            msg = "live quota evidence matches more than one overlay mapping"
            raise AmbiguousOverlayMatchError(msg)
        mapping = matches[0] if matches else None
        companion_matches = tuple(
            candidate
            for candidate in self.mappings
            if any(
                companion.selector.matches(evidence)
                for companion in candidate.companion_requirements
            )
        )
        cataloged = mapping is not None or bool(companion_matches)
        guided = (mapping is not None and mapping.guided) or any(
            candidate.guided for candidate in companion_matches
        )
        return QuotaQueryItem(
            identity=evidence.identity,
            display_name=evidence.quota_display_name,
            accelerator_id=None if mapping is None else mapping.accelerator_id,
            location=_evidence_location(evidence, mapping),
            quota_pool=None if mapping is None else mapping.quota_pool,
            predicates=CatalogPredicates(
                discovered=True,
                cataloged=cataloged,
                guided=guided,
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
        """Relate one anchored primary slice to only its applicable companions."""
        if not isinstance(accelerator_id, AcceleratorId):
            msg = "constraint accelerator_id must be an AcceleratorId"
            raise TypeError(msg)
        if not isinstance(anchor, EffectiveQuotaEvidence):
            msg = "constraint anchor must be EffectiveQuotaEvidence"
            raise TypeError(msg)
        if not isinstance(evidences, tuple) or any(
            not isinstance(evidence, EffectiveQuotaEvidence) for evidence in evidences
        ):
            msg = "constraint evidences must be EffectiveQuotaEvidence values"
            raise TypeError(msg)
        mappings = tuple(
            mapping
            for mapping in self.mappings
            if mapping.accelerator_id == accelerator_id
            and mapping.selector.matches(anchor)
        )
        if not mappings:
            return None
        if len(mappings) > 1:
            msg = "anchor matches more than one overlay mapping"
            raise AmbiguousOverlayMatchError(msg)
        mapping = mappings[0]
        anchor_location = _selector_location(mapping.selector, anchor)
        identities = {anchor.identity}
        for companion in mapping.companion_requirements:
            selector = companion.selector
            companion_matches = tuple(
                evidence
                for evidence in evidences
                if evidence.identity.resource_scope == anchor.identity.resource_scope
                and selector.matches(evidence)
                and (
                    selector.location_dimension is None
                    or _selector_location(selector, evidence) == anchor_location
                )
            )
            if not companion_matches:
                return None
            if len(companion_matches) > 1:
                msg = "required companion selector matches more than one live slice"
                raise AmbiguousOverlayMatchError(msg)
            identities.add(companion_matches[0].identity)
        references = tuple(
            ConstraintReference(identity)
            for identity in sorted(identities, key=_identity_key)
        )
        return AcceleratorConstraintSet(accelerator_id, references)

    def constraint_sets(
        self,
        evidence: EffectiveQuotaEvidence,
        evidences: tuple[EffectiveQuotaEvidence, ...],
    ) -> tuple[AcceleratorConstraintSet, ...]:
        """Return every independently anchored set containing one exact slice."""
        if not isinstance(evidence, EffectiveQuotaEvidence):
            msg = "constraint evidence must be EffectiveQuotaEvidence"
            raise TypeError(msg)
        if not isinstance(evidences, tuple) or any(
            not isinstance(item, EffectiveQuotaEvidence) for item in evidences
        ):
            msg = "constraint evidences must be EffectiveQuotaEvidence values"
            raise TypeError(msg)
        anchor_reference = ConstraintReference(evidence.identity)
        constraint_sets = []
        for mapping in self.mappings:
            for primary in evidences:
                if (
                    primary.identity.resource_scope != evidence.identity.resource_scope
                    or not mapping.selector.matches(primary)
                ):
                    continue
                constraint_set = self.constraint_set(
                    mapping.accelerator_id,
                    primary,
                    evidences,
                )
                if (
                    constraint_set is not None
                    and anchor_reference in constraint_set.references
                ):
                    constraint_sets.append(constraint_set)
        if len(set(constraint_sets)) != len(constraint_sets):
            msg = "exact slice resolves to a duplicated anchored constraint set"
            raise AmbiguousOverlayMatchError(msg)
        return tuple(sorted(constraint_sets, key=_constraint_set_key))

    def discover_specialized_hardware(
        self,
        catalog: WorkloadCatalogEvidence,
    ) -> SpecializedHardwareCatalog:
        """Retain every provider declaration while enabling only exact guidance."""
        if not isinstance(catalog, WorkloadCatalogEvidence):
            msg = "specialized hardware discovery requires WorkloadCatalogEvidence"
            raise TypeError(msg)
        records: list[SpecializedHardwareRecord] = []
        for evidence in catalog.compute_accelerator_types:
            candidates = tuple(
                mapping
                for mapping in self.mappings
                if mapping.management_plane is ManagementPlane.COMPUTE
                and evidence.name in mapping.provider_accelerator_types
            )
            safe_lifecycle = evidence.lifecycle is None or evidence.lifecycle.known in {
                CatalogLifecycle.ACTIVE,
                CatalogLifecycle.DEPRECATED,
            }
            exact = candidates[0] if len(candidates) == 1 else None
            records.append(
                SpecializedHardwareRecord(
                    service="compute.googleapis.com",
                    provider_accelerator_type=evidence.name,
                    location=evidence.zone,
                    accelerator_id=None if exact is None else exact.accelerator_id,
                    guided=(
                        exact is not None
                        and exact.conversion is not None
                        and safe_lifecycle
                    ),
                    lifecycle=evidence.lifecycle,
                )
            )
        for evidence in catalog.tpu_accelerator_types:
            candidates = tuple(
                mapping
                for mapping in self.mappings
                if mapping.management_plane is ManagementPlane.TPU
                and evidence.accelerator_type in mapping.provider_accelerator_types
                and any(
                    configuration.topology in mapping.topologies
                    for configuration in evidence.configurations
                )
            )
            exact = candidates[0] if len(candidates) == 1 else None
            records.append(
                SpecializedHardwareRecord(
                    service="tpu.googleapis.com",
                    provider_accelerator_type=evidence.accelerator_type,
                    location=evidence.zone,
                    accelerator_id=None if exact is None else exact.accelerator_id,
                    guided=exact is not None and exact.conversion is not None,
                    lifecycle=None,
                )
            )
        ordered = tuple(
            sorted(
                records,
                key=lambda item: (
                    item.service,
                    item.provider_accelerator_type,
                    item.location,
                ),
            )
        )
        exhaustive = _specialized_hardware_coverage_is_exhaustive(
            ordered, catalog.coverage
        )
        return SpecializedHardwareCatalog(ordered, catalog.coverage, exhaustive)

    def resolve(
        self,
        requirement: ModernWorkloadRequirement,
        quota_evidences: tuple[EffectiveQuotaEvidence, ...],
        catalog_evidence: WorkloadCatalogEvidence,
    ) -> ResolvedWorkloadRequirement:
        """Resolve one workload-first shape independently at each location."""
        if not isinstance(
            requirement, (ComputeInstanceRequirement, CloudTpuSliceRequirement)
        ):
            msg = "resolver requirement must be compute-instance or cloud-tpu-slice"
            raise TypeError(msg)
        return self._resolve_workload_first(
            requirement, quota_evidences, catalog_evidence
        )

    def _resolve_workload_first(
        self,
        requirement: ModernWorkloadRequirement,
        quota_evidences: tuple[EffectiveQuotaEvidence, ...],
        catalog_evidence: WorkloadCatalogEvidence,
    ) -> ResolvedWorkloadRequirement:
        """Resolve each selected location without combining alternatives."""
        if not isinstance(quota_evidences, tuple) or any(
            not isinstance(evidence, EffectiveQuotaEvidence)
            for evidence in quota_evidences
        ):
            msg = "resolver quota_evidences must be EffectiveQuotaEvidence values"
            raise TypeError(msg)
        if not isinstance(catalog_evidence, WorkloadCatalogEvidence):
            msg = "resolver catalog_evidence must be WorkloadCatalogEvidence"
            raise TypeError(msg)
        locations = _selected_locations(requirement, catalog_evidence)
        resolved = tuple(
            self._resolve_workload_location(
                requirement, location, quota_evidences, catalog_evidence
            )
            for location in locations
        )
        exhaustive = (
            _all_compatible_coverage_complete(requirement, catalog_evidence)
            if isinstance(requirement.locations, AllCompatibleLocations)
            else None
        )
        return ResolvedWorkloadRequirement(requirement, resolved, exhaustive)

    def _resolve_workload_location(
        self,
        requirement: ModernWorkloadRequirement,
        location: str,
        quota_evidences: tuple[EffectiveQuotaEvidence, ...],
        catalog: WorkloadCatalogEvidence,
    ) -> ResolvedWorkloadLocation:
        """Evaluate one location using only evidence attributable to it."""
        coverage = _location_coverage(requirement, location, catalog)
        try:
            facts = _derive_workload_facts(
                self.mappings, requirement, location, catalog
            )
            mapping, deployable_quantity = facts
            if mapping.conversion is None:
                raise WorkloadResolutionError(  # noqa: TRY301
                    ResolutionFailureReason.UNSUPPORTED_CONVERSION,
                    "The catalog mapping lacks exact native-unit conversion evidence.",
                )
            quota_location = (
                location.rsplit("-", maxsplit=1)[0]
                if isinstance(requirement, ComputeInstanceRequirement)
                else location
            )
            primary = tuple(
                evidence
                for evidence in quota_evidences
                if mapping.selector.matches(evidence)
                and _selector_location(mapping.selector, evidence) == quota_location
            )
            if not primary:
                raise WorkloadResolutionError(  # noqa: TRY301
                    ResolutionFailureReason.PROVIDER_IDENTITY,
                    "No exact live quota slice matches this location.",
                )
            if len(primary) > 1:
                raise WorkloadResolutionError(  # noqa: TRY301
                    ResolutionFailureReason.AMBIGUOUS,
                    "Multiple exact live quota slices match this location.",
                )
            constraint_set = self._constraint_set_for_resolution(
                mapping.accelerator_id, primary[0], quota_evidences
            )
            _require_eligible_constraints(constraint_set, quota_evidences)
            constraint_requirements = _constraint_requirements(
                constraint_set,
                quota_evidences,
                requirement,
                mapping,
                deployable_quantity,
            )
        except WorkloadResolutionError as error:
            return _unresolved_location(location, coverage, error.reason)
        return ResolvedWorkloadLocation(
            location=location,
            disposition=WorkloadLocationDisposition.COMPATIBLE,
            accelerator_id=mapping.accelerator_id,
            owning_service=mapping.selector.service,
            management_plane=mapping.management_plane,
            supported_consumers=mapping.workload_consumers,
            quota_pool=mapping.quota_pool,
            deployable_accelerator_quantity=deployable_quantity,
            constraint_set=constraint_set,
            constraint_requirements=constraint_requirements,
            coverage=coverage,
        )

    def _constraint_set_for_resolution(
        self,
        accelerator_id: AcceleratorId,
        anchor: EffectiveQuotaEvidence,
        evidences: tuple[EffectiveQuotaEvidence, ...],
    ) -> AcceleratorConstraintSet:
        try:
            constraint_set = self.constraint_set(accelerator_id, anchor, evidences)
        except AmbiguousOverlayMatchError as error:
            raise WorkloadResolutionError(
                ResolutionFailureReason.AMBIGUOUS,
                "A required companion selector matches multiple live slices.",
            ) from error
        if constraint_set is None:
            raise WorkloadResolutionError(
                ResolutionFailureReason.PROVIDER_IDENTITY,
                "Exact live quota constraints could not be related.",
            )
        return constraint_set


def _require_eligible_constraints(
    constraint_set: AcceleratorConstraintSet,
    evidences: tuple[EffectiveQuotaEvidence, ...],
) -> None:
    """Require every exact limiting slice to be provider-eligible for guidance."""
    by_identity = {evidence.identity: evidence for evidence in evidences}
    if any(
        (
            not by_identity[reference.slice_identity].eligibility.eligible
            or by_identity[reference.slice_identity].fixed
        )
        for reference in constraint_set.references
    ):
        raise WorkloadResolutionError(
            ResolutionFailureReason.INELIGIBLE,
            "At least one exact quota constraint is not eligible for an increase.",
        )


def _selected_locations(
    requirement: ModernWorkloadRequirement,
    catalog: WorkloadCatalogEvidence,
) -> tuple[str, ...]:
    if isinstance(requirement.locations, CandidateLocations):
        return requirement.locations.values
    source = (
        CatalogEvidenceSource.COMPUTE_MACHINE_TYPES
        if isinstance(requirement, ComputeInstanceRequirement)
        else CatalogEvidenceSource.TPU_LOCATIONS
    )
    return tuple(
        sorted(
            {
                item.location
                for item in catalog.coverage
                if item.source is source and item.location != "global"
            }
        )
    )


def _all_compatible_coverage_complete(
    requirement: ModernWorkloadRequirement,
    catalog: WorkloadCatalogEvidence,
) -> bool:
    if isinstance(requirement, ComputeInstanceRequirement):
        return _compute_all_compatible_coverage_complete(catalog)

    location_records = tuple(
        item
        for item in catalog.coverage
        if item.source is CatalogEvidenceSource.TPU_LOCATIONS
    )
    if not location_records or any(not item.complete for item in location_records):
        return False
    locations = {item.location_id for item in catalog.tpu_locations}
    if not locations:
        return any(
            item.location == "global" and item.state is LocationCoverageState.EMPTY
            for item in location_records
        )
    required_sources = (
        CatalogEvidenceSource.TPU_LOCATIONS,
        CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
        CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
    )
    for location in locations:
        for source in required_sources:
            records = tuple(
                item
                for item in catalog.coverage
                if item.source is source and item.location == location
            )
            if len(records) != 1 or not records[0].complete:
                return False
    return True


def _compute_all_compatible_coverage_complete(
    catalog: WorkloadCatalogEvidence,
) -> bool:
    machine_records = tuple(
        item
        for item in catalog.coverage
        if item.source is CatalogEvidenceSource.COMPUTE_MACHINE_TYPES
    )
    accelerator_records = tuple(
        item
        for item in catalog.coverage
        if item.source is CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES
    )
    if (
        not machine_records
        or not accelerator_records
        or any(not item.complete for item in (*machine_records, *accelerator_records))
    ):
        return False
    if len(accelerator_records) == 1 and (
        accelerator_records[0].location == "global"
        and accelerator_records[0].state is LocationCoverageState.EMPTY
    ):
        return True
    locations = {item.location for item in machine_records if item.location != "global"}
    return all(
        len(
            records := tuple(
                item for item in accelerator_records if item.location == location
            )
        )
        == 1
        and records[0].complete
        for location in locations
    )


def _location_coverage(
    requirement: ModernWorkloadRequirement,
    location: str,
    catalog: WorkloadCatalogEvidence,
) -> tuple[CatalogLocationCoverage, ...]:
    if isinstance(requirement, ComputeInstanceRequirement):
        return _compute_location_coverage(catalog, location)
    sources = (
        CatalogEvidenceSource.TPU_LOCATIONS,
        CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
        CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
    )
    return tuple(
        item
        for item in catalog.coverage
        if item.location == location and item.source in sources
    )


def _compute_location_coverage(
    catalog: WorkloadCatalogEvidence,
    location: str,
) -> tuple[CatalogLocationCoverage, ...]:
    exact_accelerator_records = tuple(
        item
        for item in catalog.coverage
        if item.source is CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES
        and item.location == location
    )
    return tuple(
        item
        for item in catalog.coverage
        if (
            item.location == location
            and item.source
            in (
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
            )
        )
        or (
            not exact_accelerator_records
            and item.source is CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES
            and item.location == "global"
            and item.state is LocationCoverageState.EMPTY
        )
    )


def _derive_workload_facts(  # noqa: C901, PLR0912
    mappings: tuple[OverlayMapping, ...],
    requirement: ModernWorkloadRequirement,
    location: str,
    catalog: WorkloadCatalogEvidence,
) -> tuple[OverlayMapping, int]:
    if isinstance(requirement, ComputeInstanceRequirement):
        required_sources = (
            CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
            CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
        )
        records = _compute_location_coverage(catalog, location)
        if (
            len(records) != len(required_sources)
            or {item.source for item in records} != set(required_sources)
            or any(not item.complete for item in records)
        ):
            raise WorkloadResolutionError(
                ResolutionFailureReason.MISSING_LOCATION_EVIDENCE,
                "Compute accelerator and machine evidence is incomplete "
                "for this location.",
            )
        if any(item.state is not LocationCoverageState.SUCCESS for item in records):
            _unsupported_compatibility()
        machines = tuple(
            machine
            for machine in catalog.compute_machine_types
            if machine.name == requirement.machine_type and machine.zone == location
        )
        if len(machines) > 1:
            raise WorkloadResolutionError(
                ResolutionFailureReason.AMBIGUOUS,
                "Multiple machine-shape records match this location.",
            )
        if not machines:
            _unsupported_compatibility()
        machine = machines[0]
        if machine.lifecycle is not None and (
            machine.lifecycle.known
            not in {CatalogLifecycle.ACTIVE, CatalogLifecycle.DEPRECATED}
        ):
            _unsupported_compatibility()
        candidates = tuple(
            (mapping, attachment, declaration)
            for attachment in machine.guest_accelerators
            for declaration in catalog.compute_accelerator_types
            for mapping in mappings
            if declaration.name == attachment.accelerator_type
            and declaration.zone == location
            and (
                declaration.lifecycle is None
                or declaration.lifecycle.known
                in {CatalogLifecycle.ACTIVE, CatalogLifecycle.DEPRECATED}
            )
            and mapping.management_plane is ManagementPlane.COMPUTE
            and requirement.machine_type in mapping.machine_types
            and attachment.accelerator_type in mapping.provider_accelerator_types
            and requirement.provisioning_model in mapping.provisioning_models
        )
        if len(candidates) > 1:
            raise WorkloadResolutionError(
                ResolutionFailureReason.AMBIGUOUS,
                "Machine-shape evidence maps to multiple accelerator semantics.",
            )
        if not candidates:
            _unsupported_compatibility()
        mapping, attachment, _declaration = candidates[0]
        return mapping, attachment.count * requirement.instance_count
    required_sources = (
        CatalogEvidenceSource.TPU_LOCATIONS,
        CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
        CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
    )
    records = tuple(
        item
        for item in catalog.coverage
        if item.location == location and item.source in required_sources
    )
    if (
        len(records) != len(required_sources)
        or {item.source for item in records} != set(required_sources)
        or any(not item.complete for item in records)
    ):
        raise WorkloadResolutionError(
            ResolutionFailureReason.MISSING_LOCATION_EVIDENCE,
            "Cloud TPU catalog evidence is incomplete for this location.",
        )
    if any(item.state is not LocationCoverageState.SUCCESS for item in records):
        _unsupported_compatibility()
    if not any(item.location_id == location for item in catalog.tpu_locations):
        _unsupported_compatibility()
    accelerators = tuple(
        item
        for item in catalog.tpu_accelerator_types
        if item.zone == location
        and item.accelerator_type == requirement.accelerator_type
        and any(
            configuration.topology == requirement.topology
            for configuration in item.configurations
        )
    )
    runtimes = tuple(
        item
        for item in catalog.tpu_runtime_versions
        if item.zone == location and item.version == requirement.runtime_version
    )
    if len(accelerators) > 1 or len(runtimes) > 1:
        raise WorkloadResolutionError(
            ResolutionFailureReason.AMBIGUOUS,
            "Cloud TPU catalog evidence is ambiguous for this location.",
        )
    if not accelerators or not runtimes:
        _unsupported_compatibility()
    mapping_candidates = tuple(
        mapping
        for mapping in mappings
        if mapping.management_plane is ManagementPlane.TPU
        and requirement.accelerator_type in mapping.provider_accelerator_types
        and requirement.topology in mapping.topologies
        and requirement.runtime_version in mapping.runtime_versions
        and requirement.provisioning_model in mapping.provisioning_models
        and len(mapping.accelerator_counts) == 1
    )
    if len(mapping_candidates) > 1:
        raise WorkloadResolutionError(
            ResolutionFailureReason.AMBIGUOUS,
            "Cloud TPU evidence maps to multiple accelerator semantics.",
        )
    if not mapping_candidates:
        _unsupported_compatibility()
    mapping = mapping_candidates[0]
    return mapping, mapping.accelerator_counts[0] * requirement.slice_count


def _unresolved_location(
    location: str,
    coverage: tuple[CatalogLocationCoverage, ...],
    reason: ResolutionFailureReason,
) -> ResolvedWorkloadLocation:
    disposition = {
        ResolutionFailureReason.AMBIGUOUS: WorkloadLocationDisposition.AMBIGUOUS,
        ResolutionFailureReason.MISSING_LOCATION_EVIDENCE: (
            WorkloadLocationDisposition.INCOMPLETE
        ),
    }.get(reason, WorkloadLocationDisposition.INCOMPATIBLE)
    return ResolvedWorkloadLocation(
        location=location,
        disposition=disposition,
        accelerator_id=None,
        owning_service=None,
        management_plane=None,
        supported_consumers=(),
        quota_pool=None,
        deployable_accelerator_quantity=None,
        constraint_set=None,
        constraint_requirements=(),
        coverage=coverage,
        failure_reason=reason,
    )


def _constraint_requirements(
    constraint_set: AcceleratorConstraintSet,
    evidences: tuple[EffectiveQuotaEvidence, ...],
    requirement: ModernWorkloadRequirement,
    mapping: OverlayMapping,
    deployable_quantity: int,
) -> tuple[QuotaConstraintRequirement, ...]:
    """Convert every exact constraint independently or stop at a unit boundary."""
    result: list[QuotaConstraintRequirement] = []
    for reference in constraint_set.references:
        matches = tuple(
            evidence
            for evidence in evidences
            if evidence.identity == reference.slice_identity
        )
        if len(matches) > 1:
            raise WorkloadResolutionError(
                ResolutionFailureReason.AMBIGUOUS,
                "Multiple evidence records exist for one exact quota constraint.",
            )
        if not matches:
            raise WorkloadResolutionError(
                ResolutionFailureReason.PROVIDER_IDENTITY,
                "One exact quota constraint lacks provider evidence.",
            )
        evidence = matches[0]
        rules: list[tuple[WorkloadQuantityBasis, UnitConversionEvidence]] = []
        if mapping.selector.matches(evidence):
            if mapping.conversion is None:
                raise WorkloadResolutionError(
                    ResolutionFailureReason.UNSUPPORTED_CONVERSION,
                    "The primary constraint lacks native-unit conversion evidence.",
                )
            rules.append(
                (
                    WorkloadQuantityBasis.ACCELERATOR_QUANTITY,
                    mapping.conversion,
                )
            )
        rules.extend(
            (companion.quantity_basis, companion.conversion)
            for companion in mapping.companion_requirements
            if companion.selector.matches(evidence)
        )
        if len(rules) > 1:
            raise WorkloadResolutionError(
                ResolutionFailureReason.AMBIGUOUS,
                "One exact quota constraint matches multiple quantity mappings.",
            )
        if not rules:
            raise WorkloadResolutionError(
                ResolutionFailureReason.UNSUPPORTED_CONVERSION,
                "One exact quota constraint lacks a quantity mapping.",
            )
        quantity_basis, conversion = rules[0]
        unit = evidence.effective_value.unit
        if unit != conversion.quota_unit:
            raise WorkloadResolutionError(
                ResolutionFailureReason.UNSUPPORTED_CONVERSION,
                (
                    "A quota constraint does not match its native-unit "
                    "conversion evidence."
                ),
            )
        source_quantity = _workload_source_quantity(
            requirement,
            quantity_basis,
            deployable_quantity,
        )
        try:
            required = QuotaQuantity(
                source_quantity * conversion.quota_units_per_source,
                unit,
            )
        except ValueError as error:
            raise WorkloadResolutionError(
                ResolutionFailureReason.UNSUPPORTED_CONVERSION,
                (
                    "The converted workload requirement exceeds the supported "
                    "native quota range."
                ),
            ) from error
        result.append(
            QuotaConstraintRequirement(
                reference.slice_identity,
                source_quantity,
                required,
                conversion,
            )
        )
    return tuple(result)


def _workload_source_quantity(
    requirement: ModernWorkloadRequirement,
    basis: WorkloadQuantityBasis,
    deployable_quantity: int,
) -> int:
    """Select the declared workload quantity for one independent conversion."""
    if basis is WorkloadQuantityBasis.ACCELERATOR_QUANTITY:
        return deployable_quantity
    if basis is WorkloadQuantityBasis.INSTANCE_COUNT and isinstance(
        requirement, ComputeInstanceRequirement
    ):
        return requirement.instance_count
    if basis is WorkloadQuantityBasis.SLICE_COUNT and isinstance(
        requirement, CloudTpuSliceRequirement
    ):
        return requirement.slice_count
    raise WorkloadResolutionError(
        ResolutionFailureReason.UNSUPPORTED_CONVERSION,
        "A quota constraint quantity basis is incompatible with the workload kind.",
    )


def _unsupported_compatibility() -> None:
    raise WorkloadResolutionError(
        ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY,
        "Live catalog evidence does not support the exact workload shape.",
    )


def _selector_location(
    selector: QuotaSelector, evidence: EffectiveQuotaEvidence
) -> str | None:
    if selector.location_dimension is None:
        return "global" if selector.quota_scope is QuotaScope.GLOBAL else None
    return dict(evidence.identity.dimensions.items)[selector.location_dimension]


def _evidence_location(
    evidence: EffectiveQuotaEvidence,
    mapping: OverlayMapping | None,
) -> str | None:
    dimensions = dict(evidence.identity.dimensions.items)
    if mapping is not None and mapping.selector.location_dimension is not None:
        return dimensions[mapping.selector.location_dimension]
    for key in ("region", "zone", "location"):
        if key in dimensions:
            return dimensions[key]
    if len(evidence.applicable_locations) == 1:
        return evidence.applicable_locations[0]
    return None


def _mapping_key(mapping: OverlayMapping) -> tuple[str, str, str]:
    return (
        mapping.group_id.value,
        mapping.accelerator_id.value,
        mapping.quota_pool,
    )


def _identity_key(identity: EffectiveQuotaSliceIdentity) -> tuple[object, ...]:
    return (
        identity.resource_scope.canonical_name,
        identity.service,
        identity.quota_id,
        identity.dimensions.items,
        identity.quota_scope.value,
    )


def _constraint_set_key(value: AcceleratorConstraintSet) -> tuple[object, ...]:
    return (
        value.accelerator_id.value,
        tuple(
            _identity_key(reference.slice_identity) for reference in value.references
        ),
    )


def _canonical_mapping_bytes(mapping: OverlayMapping) -> bytes:
    conversion = (
        _encode_field("conversion", b"none")
        if mapping.conversion is None
        else _encode_field("conversion", _conversion_bytes(mapping.conversion))
    )
    return b"".join(
        (
            _encode_text("group-id", mapping.group_id.value),
            _encode_text("accelerator-id", mapping.accelerator_id.value),
            _encode_text("management-plane", mapping.management_plane.value),
            _encode_sequence(
                "workload-consumers",
                tuple(
                    _encode_text("consumer", consumer.value)
                    for consumer in mapping.workload_consumers
                ),
            ),
            _encode_field("selector", _selector_bytes(mapping.selector)),
            _encode_text("quota-pool", mapping.quota_pool),
            conversion,
            _encode_sequence(
                "companions",
                tuple(
                    _companion_requirement_bytes(companion)
                    for companion in mapping.companion_requirements
                ),
            ),
            _encode_text("source-url", mapping.source_url),
            _encode_text("reviewed-on", mapping.reviewed_on.isoformat()),
            _encode_text_sequence("machine-types", mapping.machine_types),
            _encode_text_sequence(
                "provider-accelerator-types", mapping.provider_accelerator_types
            ),
            _encode_text_sequence("topologies", mapping.topologies),
            _encode_text_sequence("runtime-versions", mapping.runtime_versions),
            _encode_text_sequence(
                "accelerator-counts",
                tuple(str(count) for count in mapping.accelerator_counts),
            ),
            _encode_text_sequence(
                "provisioning-models",
                tuple(model.value for model in mapping.provisioning_models),
            ),
        )
    )


def _companion_requirement_bytes(
    companion: CompanionRequirementMapping,
) -> bytes:
    return b"".join(
        (
            _encode_field("selector", _selector_bytes(companion.selector)),
            _encode_text("quantity-basis", companion.quantity_basis.value),
            _encode_field("conversion", _conversion_bytes(companion.conversion)),
        )
    )


def _conversion_bytes(conversion: UnitConversionEvidence) -> bytes:
    return b"".join(
        (
            _encode_text("source-unit", conversion.source_unit),
            _encode_text("quota-unit", conversion.quota_unit.symbol),
            _encode_text(
                "quota-units-per-source",
                str(conversion.quota_units_per_source),
            ),
            _encode_text("source-reference", conversion.source_reference),
        )
    )


def _selector_bytes(selector: QuotaSelector) -> bytes:
    return b"".join(
        (
            _encode_text("service", selector.service),
            _encode_optional_text("quota-id", selector.quota_id),
            _encode_optional_text("quota-display-name", selector.quota_display_name),
            _encode_sequence(
                "dimensions",
                tuple(
                    b"".join(
                        (
                            _encode_text("key", dimension.key),
                            _encode_optional_text("value", dimension.value),
                        )
                    )
                    for dimension in selector.dimensions
                ),
            ),
            _encode_text("native-unit", selector.native_unit.symbol),
            _encode_text("quota-scope", selector.quota_scope.value),
            _encode_optional_text("location-dimension", selector.location_dimension),
        )
    )


def _encode_text_sequence(name: str, values: tuple[str, ...]) -> bytes:
    return _encode_sequence(
        name, tuple(_encode_text("value", value) for value in values)
    )


def _encode_optional_text(name: str, value: str | None) -> bytes:
    return _encode_field(name, b"none" if value is None else b"text" + value.encode())


def _encode_text(name: str, value: str) -> bytes:
    return _encode_field(name, value.encode())


def _encode_sequence(name: str, values: tuple[bytes, ...]) -> bytes:
    payload = len(values).to_bytes(8, "big") + b"".join(
        len(value).to_bytes(8, "big") + value for value in values
    )
    return _encode_field(name, payload)


def _encode_field(name: str, payload: bytes) -> bytes:
    encoded_name = name.encode()
    return (
        len(encoded_name).to_bytes(8, "big")
        + encoded_name
        + len(payload).to_bytes(8, "big")
        + payload
    )


def _require_nonempty(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        msg = f"{field_name} must be non-empty text"
        raise ValueError(msg)


def _require_positive_count(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{field_name} must be a positive integer"
        raise ValueError(msg)


def _require_location(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or value != value.lower()
        or "/" in value
    ):
        msg = f"{field_name} must be a lowercase canonical location"
        raise ValueError(msg)


def _require_canonical_zone(value: object, field_name: str) -> None:
    _require_location(value, field_name)
    if not _is_canonical_zone(value):
        msg = f"{field_name} must be an exact canonical zone"
        raise ValueError(msg)


def _is_official_source(value: object) -> bool:
    return isinstance(value, str) and value.startswith(
        ("https://docs.cloud.google.com/", "https://cloud.google.com/")
    )


def _is_stable_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and value == value.lower()
        and all(component and component.isalnum() for component in value.split("-"))
    )


def _is_canonical_service_dns(service: object) -> bool:
    if (
        not isinstance(service, str)
        or not service.isascii()
        or service != service.lower()
    ):
        return False
    labels = service.split(".")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    return len(labels) >= _MIN_DNS_LABELS and all(
        label
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character in allowed for character in label)
        for label in labels
    )


def _is_canonical_zone(value: object) -> bool:
    if not isinstance(value, str):
        return False
    region, separator, suffix = value.rpartition("-")
    return (
        separator == "-"
        and "-" in region
        and all(region.split("-"))
        and all(character in _LOCATION_CHARACTERS for character in value)
        and region[-1:].isdigit()
        and len(suffix) == 1
        and suffix.isalpha()
    )


_B200_A4_REGIONAL = OverlayMapping(
    group_id=CatalogGroupId.COMPUTE_ACCELERATORS,
    accelerator_id=AcceleratorId("nvidia-b200"),
    management_plane=ManagementPlane.COMPUTE,
    workload_consumers=(WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE),
    selector=QuotaSelector(
        service="compute.googleapis.com",
        quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
        quota_display_name="GPUs per family per region",
        dimensions=(
            DimensionSelector("gpu_family", "NVIDIA_B200"),
            DimensionSelector("region"),
        ),
        native_unit=QuotaUnit("1"),
        quota_scope=QuotaScope.REGIONAL,
        location_dimension="region",
    ),
    quota_pool="standard",
    conversion=UnitConversionEvidence(
        source_unit="card",
        quota_unit=QuotaUnit("1"),
        quota_units_per_source=1,
        source_reference=_COMPUTE_QUOTA_SOURCE,
    ),
    companion_requirements=(
        CompanionRequirementMapping(
            selector=QuotaSelector(
                service="compute.googleapis.com",
                quota_id="GPUS-ALL-REGIONS-per-project",
                quota_display_name=None,
                dimensions=(),
                native_unit=QuotaUnit("1"),
                quota_scope=QuotaScope.GLOBAL,
                location_dimension=None,
            ),
            quantity_basis=WorkloadQuantityBasis.ACCELERATOR_QUANTITY,
            conversion=UnitConversionEvidence(
                source_unit="card",
                quota_unit=QuotaUnit("1"),
                quota_units_per_source=1,
                source_reference=_COMPUTE_QUOTA_SOURCE,
            ),
        ),
    ),
    source_url=_COMPUTE_QUOTA_SOURCE,
    reviewed_on=_REVIEW_DATE,
    machine_types=("a4-highgpu-8g",),
    provider_accelerator_types=("nvidia-b200",),
    provisioning_models=(ProvisioningModel.STANDARD,),
)

_H100_REGIONAL = OverlayMapping(
    group_id=CatalogGroupId.COMPUTE_ACCELERATORS,
    accelerator_id=AcceleratorId("nvidia-h100"),
    management_plane=ManagementPlane.COMPUTE,
    workload_consumers=(WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE),
    selector=QuotaSelector(
        service="compute.googleapis.com",
        quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
        quota_display_name="GPUs per family per region",
        dimensions=(
            DimensionSelector("gpu_family", "NVIDIA_H100"),
            DimensionSelector("region"),
        ),
        native_unit=QuotaUnit("1"),
        quota_scope=QuotaScope.REGIONAL,
        location_dimension="region",
    ),
    quota_pool="standard",
    conversion=UnitConversionEvidence(
        source_unit="card",
        quota_unit=QuotaUnit("1"),
        quota_units_per_source=1,
        source_reference=_COMPUTE_QUOTA_SOURCE,
    ),
    companion_requirements=(
        CompanionRequirementMapping(
            selector=QuotaSelector(
                service="compute.googleapis.com",
                quota_id="GPUS-ALL-REGIONS-per-project",
                quota_display_name=None,
                dimensions=(),
                native_unit=QuotaUnit("1"),
                quota_scope=QuotaScope.GLOBAL,
                location_dimension=None,
            ),
            quantity_basis=WorkloadQuantityBasis.ACCELERATOR_QUANTITY,
            conversion=UnitConversionEvidence(
                source_unit="card",
                quota_unit=QuotaUnit("1"),
                quota_units_per_source=1,
                source_reference=_COMPUTE_QUOTA_SOURCE,
            ),
        ),
    ),
    source_url=_COMPUTE_QUOTA_SOURCE,
    reviewed_on=_REVIEW_DATE,
    machine_types=("a3-highgpu-8g",),
    provider_accelerator_types=("nvidia-h100-80gb",),
    provisioning_models=(ProvisioningModel.STANDARD,),
)

_COMPUTE_TPU_V6E_STANDARD = OverlayMapping(
    group_id=CatalogGroupId.COMPUTE_ACCELERATORS,
    accelerator_id=AcceleratorId("tpu-v6e"),
    management_plane=ManagementPlane.COMPUTE,
    workload_consumers=(WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE),
    selector=QuotaSelector(
        service="compute.googleapis.com",
        quota_id=None,
        quota_display_name="TPUs per TPU family",
        dimensions=(
            DimensionSelector("tpu_family", "CT6E"),
            DimensionSelector("region"),
        ),
        native_unit=QuotaUnit("1"),
        quota_scope=QuotaScope.REGIONAL,
        location_dimension="region",
    ),
    quota_pool="standard",
    conversion=UnitConversionEvidence(
        source_unit="chip",
        quota_unit=QuotaUnit("1"),
        quota_units_per_source=1,
        source_reference=_COMPUTE_QUOTA_SOURCE,
    ),
    companion_requirements=(),
    source_url=_COMPUTE_QUOTA_SOURCE,
    reviewed_on=_REVIEW_DATE,
    machine_types=("ct6e-standard-4t",),
    provider_accelerator_types=("tpu-v6e",),
    accelerator_counts=(4,),
    provisioning_models=(ProvisioningModel.STANDARD,),
)

_LEGACY_TPU_V6E_STANDARD = OverlayMapping(
    group_id=CatalogGroupId.CLOUD_TPU_LEGACY,
    accelerator_id=AcceleratorId("tpu-v6e"),
    management_plane=ManagementPlane.TPU,
    workload_consumers=(WorkloadConsumer.CLOUD_TPU_API,),
    selector=QuotaSelector(
        service="tpu.googleapis.com",
        quota_id=None,
        quota_display_name="TPU v6e cores per project per zone",
        dimensions=(DimensionSelector("zone"),),
        native_unit=QuotaUnit("core"),
        quota_scope=QuotaScope.ZONAL,
        location_dimension="zone",
    ),
    quota_pool="standard",
    conversion=UnitConversionEvidence(
        source_unit="core",
        quota_unit=QuotaUnit("core"),
        quota_units_per_source=1,
        source_reference=_TPU_QUOTA_SOURCE,
    ),
    companion_requirements=(),
    source_url=_TPU_QUOTA_SOURCE,
    reviewed_on=_REVIEW_DATE,
    provider_accelerator_types=("v6e-8",),
    topologies=("2x4",),
    runtime_versions=("tpu-vm-base",),
    accelerator_counts=(8,),
    provisioning_models=(ProvisioningModel.STANDARD,),
)

MAINTAINED_ACCELERATOR_OVERLAY = SemanticAcceleratorOverlay(
    (
        _B200_A4_REGIONAL,
        _H100_REGIONAL,
        _COMPUTE_TPU_V6E_STANDARD,
        _LEGACY_TPU_V6E_STANDARD,
    )
)
