"""Maintained accelerator semantics joined conservatively to live quota evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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
    ComputeMachineType,
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

_REVIEW_DATE = date(2026, 7, 14)
_COMPUTE_QUOTA_SOURCE = "https://docs.cloud.google.com/compute/resource-usage"
_TPU_QUOTA_SOURCE = "https://docs.cloud.google.com/tpu/docs/quota"
_MIN_DNS_LABELS = 2


class AmbiguousOverlayMatchError(ValueError):
    """Raised when maintained selectors do not identify one semantic mapping."""


class ProvisioningModel(StrEnum):
    """Provisioning model relevant to maintained quota-pool selection."""

    STANDARD = "standard"
    SPOT = "spot"
    FLEX_START = "flex-start"
    RESERVATION_BOUND = "reservation-bound"


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
class GpuWorkloadRequirement:
    """Exact GPU workload shape and selected region and zone."""

    accelerator_id: AcceleratorId
    workload_consumer: WorkloadConsumer
    accelerator_count: int
    machine_type: str
    provisioning_model: ProvisioningModel
    region: str
    zone: str

    def __post_init__(self) -> None:
        """Require the complete GPU shape without deriving a location."""
        _require_accelerator_count(self.accelerator_count)
        if not isinstance(self.accelerator_id, AcceleratorId):
            msg = "GPU accelerator_id must be an AcceleratorId"
            raise TypeError(msg)
        if not isinstance(self.workload_consumer, WorkloadConsumer):
            msg = "GPU workload_consumer must be a WorkloadConsumer"
            raise TypeError(msg)
        if self.workload_consumer not in {
            WorkloadConsumer.COMPUTE_ENGINE,
            WorkloadConsumer.GKE,
        }:
            msg = "GPU workload_consumer must use the Compute management plane"
            raise ValueError(msg)
        _require_nonempty(self.machine_type, "GPU machine_type")
        if not isinstance(self.provisioning_model, ProvisioningModel):
            msg = "GPU provisioning_model must be a ProvisioningModel"
            raise TypeError(msg)
        _require_location(self.region, "GPU region")
        _require_location(self.zone, "GPU zone")
        _require_zone_in_region(self.zone, self.region, "GPU")


@dataclass(frozen=True, slots=True)
class TpuWorkloadRequirement:
    """Management-plane-first TPU shape with explicit applicable fields."""

    management_plane: ManagementPlane
    accelerator_id: AcceleratorId
    workload_consumer: WorkloadConsumer
    accelerator_count: int
    provisioning_model: ProvisioningModel
    region: str | None
    zone: str
    machine_type: str | None
    topology: str | None
    runtime_version: str | None

    def __post_init__(self) -> None:
        """Reject a TPU shape expressed in the wrong management-plane vocabulary."""
        if not isinstance(self.management_plane, ManagementPlane):
            msg = "TPU management_plane must be selected explicitly"
            raise TypeError(msg)
        if not isinstance(self.accelerator_id, AcceleratorId):
            msg = "TPU accelerator_id must be an AcceleratorId"
            raise TypeError(msg)
        if not isinstance(self.workload_consumer, WorkloadConsumer):
            msg = "TPU workload_consumer must be a WorkloadConsumer"
            raise TypeError(msg)
        expected_consumers = (
            {WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE}
            if self.management_plane is ManagementPlane.COMPUTE
            else {WorkloadConsumer.CLOUD_TPU_API}
        )
        if self.workload_consumer not in expected_consumers:
            msg = "TPU workload_consumer does not match its management plane"
            raise ValueError(msg)
        _require_accelerator_count(self.accelerator_count)
        if not isinstance(self.provisioning_model, ProvisioningModel):
            msg = "TPU provisioning_model must be a ProvisioningModel"
            raise TypeError(msg)
        _require_canonical_zone(self.zone, "TPU zone")
        if self.management_plane is ManagementPlane.COMPUTE:
            if self.region is None or self.machine_type is None:
                msg = "Compute TPU requires exact region and machine_type"
                raise ValueError(msg)
            _require_location(self.region, "Compute TPU region")
            _require_nonempty(self.machine_type, "Compute TPU machine_type")
            _require_zone_in_region(self.zone, self.region, "Compute TPU")
            if self.runtime_version is not None:
                msg = "Compute TPU does not accept a legacy runtime_version"
                raise ValueError(msg)
        elif (
            self.region is not None
            or self.machine_type is not None
            or self.topology is None
            or self.runtime_version is None
        ):
            msg = (
                "legacy TPU requires topology and runtime_version without "
                "Compute region or machine_type"
            )
            raise ValueError(msg)
        else:
            _require_nonempty(self.topology, "legacy TPU topology")
            _require_nonempty(self.runtime_version, "legacy TPU runtime_version")


WorkloadRequirement = GpuWorkloadRequirement | TpuWorkloadRequirement


@dataclass(frozen=True, slots=True)
class WorkloadCatalogEvidence:
    """Provider-neutral live catalog and per-location coverage for resolution."""

    compute_machine_types: tuple[ComputeMachineType, ...]
    tpu_locations: tuple[TpuLocation, ...]
    tpu_accelerator_types: tuple[TpuAcceleratorType, ...]
    tpu_runtime_versions: tuple[TpuRuntimeVersion, ...]
    coverage: tuple[CatalogLocationCoverage, ...]

    def __post_init__(self) -> None:
        """Require immutable evidence values from the declared provider ports."""
        fields_and_types = (
            (self.compute_machine_types, ComputeMachineType),
            (self.tpu_locations, TpuLocation),
            (self.tpu_accelerator_types, TpuAcceleratorType),
            (self.tpu_runtime_versions, TpuRuntimeVersion),
            (self.coverage, CatalogLocationCoverage),
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
class ResolvedQuotaRequirement:
    """Native-unit workload requirement bound to exact quota constraints."""

    requirement: WorkloadRequirement
    owning_service: str
    required_amount: QuotaQuantity
    conversion: UnitConversionEvidence
    constraint_set: AcceleratorConstraintSet

    def __post_init__(self) -> None:
        """Require complete typed resolution evidence."""
        if not isinstance(
            self.requirement, (GpuWorkloadRequirement, TpuWorkloadRequirement)
        ):
            msg = "resolved requirement must retain its typed workload input"
            raise TypeError(msg)
        if not _is_canonical_service_dns(self.owning_service):
            msg = "resolved owning_service must be canonical"
            raise ValueError(msg)
        if not isinstance(self.required_amount, QuotaQuantity):
            msg = "resolved required_amount must be QuotaQuantity"
            raise TypeError(msg)
        if not isinstance(self.conversion, UnitConversionEvidence):
            msg = "resolved conversion must be UnitConversionEvidence"
            raise TypeError(msg)
        if not isinstance(self.constraint_set, AcceleratorConstraintSet):
            msg = "resolved constraint_set must be AcceleratorConstraintSet"
            raise TypeError(msg)


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
    companion_selectors: tuple[QuotaSelector, ...]
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
    if not isinstance(mapping.companion_selectors, tuple) or any(
        not isinstance(selector, QuotaSelector)
        for selector in mapping.companion_selectors
    ):
        msg = "companion_selectors must be QuotaSelector values"
        raise TypeError(msg)


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
        return QuotaQueryItem(
            identity=evidence.identity,
            display_name=evidence.quota_display_name,
            accelerator_id=None if mapping is None else mapping.accelerator_id,
            location=_evidence_location(evidence, mapping),
            quota_pool=None if mapping is None else mapping.quota_pool,
            predicates=CatalogPredicates(
                discovered=True,
                cataloged=mapping is not None,
                guided=mapping is not None and mapping.guided,
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
        for selector in mapping.companion_selectors:
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

    def resolve(
        self,
        requirement: WorkloadRequirement,
        quota_evidences: tuple[EffectiveQuotaEvidence, ...],
        catalog_evidence: WorkloadCatalogEvidence,
    ) -> ResolvedQuotaRequirement:
        """Resolve one exact workload or stop on any missing or ambiguous evidence."""
        if not isinstance(
            requirement, (GpuWorkloadRequirement, TpuWorkloadRequirement)
        ):
            msg = "resolver requirement must be a typed GPU or TPU requirement"
            raise TypeError(msg)
        if not isinstance(quota_evidences, tuple) or any(
            not isinstance(evidence, EffectiveQuotaEvidence)
            for evidence in quota_evidences
        ):
            msg = "resolver quota_evidences must be EffectiveQuotaEvidence values"
            raise TypeError(msg)
        if not isinstance(catalog_evidence, WorkloadCatalogEvidence):
            msg = "resolver catalog_evidence must be WorkloadCatalogEvidence"
            raise TypeError(msg)

        management_plane = (
            ManagementPlane.COMPUTE
            if isinstance(requirement, GpuWorkloadRequirement)
            else requirement.management_plane
        )
        candidates = tuple(
            mapping
            for mapping in self.mappings
            if mapping.accelerator_id == requirement.accelerator_id
            and mapping.management_plane is management_plane
            and requirement.workload_consumer in mapping.workload_consumers
            and requirement.provisioning_model in mapping.provisioning_models
        )
        if not candidates:
            raise WorkloadResolutionError(
                ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY,
                "No maintained mapping supports the exact workload shape.",
            )
        if len(candidates) > 1:
            raise WorkloadResolutionError(
                ResolutionFailureReason.AMBIGUOUS,
                "More than one maintained mapping supports the workload.",
            )
        mapping = candidates[0]
        if mapping.conversion is None:
            raise WorkloadResolutionError(
                ResolutionFailureReason.UNSUPPORTED_CONVERSION,
                "The maintained mapping lacks exact identity or unit conversion "
                "evidence.",
            )
        _require_compatible_catalog(requirement, mapping, catalog_evidence)

        quota_location = (
            requirement.region
            if isinstance(requirement, GpuWorkloadRequirement)
            or requirement.management_plane is ManagementPlane.COMPUTE
            else requirement.zone
        )
        primary = tuple(
            evidence
            for evidence in quota_evidences
            if mapping.selector.matches(evidence)
            and _selector_location(mapping.selector, evidence) == quota_location
        )
        if not primary:
            raise WorkloadResolutionError(
                ResolutionFailureReason.PROVIDER_IDENTITY,
                "No live exact quota slice matches the maintained selector and "
                "location.",
            )
        if len(primary) > 1:
            raise WorkloadResolutionError(
                ResolutionFailureReason.AMBIGUOUS,
                "Multiple live exact quota slices match the workload requirement.",
            )
        constraint_set = self._constraint_set_for_resolution(
            requirement.accelerator_id, primary[0], quota_evidences
        )
        _require_eligible_constraints(constraint_set, quota_evidences)
        return ResolvedQuotaRequirement(
            requirement=requirement,
            owning_service=mapping.selector.service,
            required_amount=QuotaQuantity(
                requirement.accelerator_count
                * mapping.conversion.quota_units_per_source,
                mapping.conversion.quota_unit,
            ),
            conversion=mapping.conversion,
            constraint_set=constraint_set,
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


def _require_compatible_catalog(
    requirement: WorkloadRequirement,
    mapping: OverlayMapping,
    catalog: WorkloadCatalogEvidence,
) -> None:
    if isinstance(requirement, GpuWorkloadRequirement) or (
        isinstance(requirement, TpuWorkloadRequirement)
        and requirement.management_plane is ManagementPlane.COMPUTE
    ):
        _require_compute_catalog(requirement, mapping, catalog)
        return

    _require_legacy_tpu_catalog(requirement, mapping, catalog)


def _require_compute_catalog(
    requirement: WorkloadRequirement,
    mapping: OverlayMapping,
    catalog: WorkloadCatalogEvidence,
) -> None:
    _require_complete_location(
        catalog,
        CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
        requirement.zone,
    )
    machine_type = requirement.machine_type
    if machine_type is None or machine_type not in mapping.machine_types:
        _unsupported_compatibility()
    machines = tuple(
        machine
        for machine in catalog.compute_machine_types
        if machine.name == machine_type and machine.zone == requirement.zone
    )
    if len(machines) != 1:
        _unsupported_compatibility()
    machine = machines[0]
    if machine.lifecycle is not None and machine.lifecycle.known in {
        CatalogLifecycle.OBSOLETE,
        CatalogLifecycle.DELETED,
    }:
        _unsupported_compatibility()
    if not any(
        attachment.accelerator_type in mapping.provider_accelerator_types
        and attachment.count == requirement.accelerator_count
        for attachment in machine.guest_accelerators
    ):
        _unsupported_compatibility()


def _require_legacy_tpu_catalog(
    requirement: TpuWorkloadRequirement,
    mapping: OverlayMapping,
    catalog: WorkloadCatalogEvidence,
) -> None:

    _require_complete_location(
        catalog, CatalogEvidenceSource.TPU_LOCATIONS, requirement.zone
    )
    _require_complete_location(
        catalog, CatalogEvidenceSource.TPU_ACCELERATOR_TYPES, requirement.zone
    )
    _require_complete_location(
        catalog, CatalogEvidenceSource.TPU_RUNTIME_VERSIONS, requirement.zone
    )
    if not any(
        location.location_id == requirement.zone for location in catalog.tpu_locations
    ):
        _unsupported_compatibility()
    if requirement.topology not in mapping.topologies:
        _unsupported_compatibility()
    if requirement.runtime_version not in mapping.runtime_versions:
        _unsupported_compatibility()
    if requirement.accelerator_count not in mapping.accelerator_counts:
        _unsupported_compatibility()
    if not any(
        accelerator.zone == requirement.zone
        and accelerator.accelerator_type in mapping.provider_accelerator_types
        and any(
            configuration.topology == requirement.topology
            for configuration in accelerator.configurations
        )
        for accelerator in catalog.tpu_accelerator_types
    ):
        _unsupported_compatibility()
    if not any(
        runtime.zone == requirement.zone
        and runtime.version == requirement.runtime_version
        for runtime in catalog.tpu_runtime_versions
    ):
        _unsupported_compatibility()


def _require_complete_location(
    catalog: WorkloadCatalogEvidence,
    source: CatalogEvidenceSource,
    location: str,
) -> None:
    records = tuple(
        coverage
        for coverage in catalog.coverage
        if coverage.source is source and coverage.location == location
    )
    if len(records) != 1 or not records[0].complete:
        raise WorkloadResolutionError(
            ResolutionFailureReason.MISSING_LOCATION_EVIDENCE,
            "Required selected-location catalog evidence is incomplete.",
        )


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


def _canonical_mapping_bytes(mapping: OverlayMapping) -> bytes:
    conversion = (
        _encode_field("conversion", b"none")
        if mapping.conversion is None
        else _encode_field(
            "conversion",
            b"".join(
                (
                    _encode_text("source-unit", mapping.conversion.source_unit),
                    _encode_text("quota-unit", mapping.conversion.quota_unit.symbol),
                    _encode_text(
                        "quota-units-per-source",
                        str(mapping.conversion.quota_units_per_source),
                    ),
                    _encode_text(
                        "source-reference", mapping.conversion.source_reference
                    ),
                )
            ),
        )
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
                    _selector_bytes(selector)
                    for selector in mapping.companion_selectors
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


def _require_accelerator_count(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = "accelerator_count must be a positive integer"
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


def _require_zone_in_region(zone: str, region: str, workload: str) -> None:
    if zone.rsplit("-", maxsplit=1)[0] != region:
        msg = f"{workload} zone must belong to its explicit region"
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
        and region[-1:].isdigit()
        and len(suffix) == 1
        and suffix.isalpha()
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
    companion_selectors=(
        QuotaSelector(
            service="compute.googleapis.com",
            quota_id="GPUS-ALL-REGIONS-per-project",
            quota_display_name=None,
            dimensions=(),
            native_unit=QuotaUnit("1"),
            quota_scope=QuotaScope.GLOBAL,
            location_dimension=None,
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
    companion_selectors=(),
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
    companion_selectors=(),
    source_url=_TPU_QUOTA_SOURCE,
    reviewed_on=_REVIEW_DATE,
    provider_accelerator_types=("v6e-8",),
    topologies=("2x4",),
    runtime_versions=("tpu-vm-base",),
    accelerator_counts=(8,),
    provisioning_models=(ProvisioningModel.STANDARD,),
)

MAINTAINED_ACCELERATOR_OVERLAY = SemanticAcceleratorOverlay(
    (_H100_REGIONAL, _COMPUTE_TPU_V6E_STANDARD, _LEGACY_TPU_V6E_STANDARD)
)
