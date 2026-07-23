"""Independent accelerator-catalog facts and conjunctive filtering."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum

from cqmgr.domain.diagnostics import Diagnostic
from cqmgr.domain.quotas import ConstraintReference, QuotaUnit
from cqmgr.domain.schemas import ProviderSymbol

ACCELERATOR_CATALOG_SCHEMA = "cqmgr.accelerator-catalog/v1"
_SHA256_HEX_LENGTH = 64


class CatalogGroupId(StrEnum):
    """Stable public source groups for guided accelerator workflows."""

    COMPUTE_ACCELERATORS = "compute-accelerators"
    CLOUD_TPU_LEGACY = "cloud-tpu-legacy"


class ManagementPlane(StrEnum):
    """Quota-management plane selected before accelerator resolution."""

    COMPUTE = "compute"
    TPU = "tpu"


class WorkloadConsumer(StrEnum):
    """Workload surface consuming quota owned by a management plane."""

    COMPUTE_ENGINE = "compute-engine"
    GKE = "gke"
    CLOUD_TPU_API = "cloud-tpu-api"


class CatalogLifecycle(StrEnum):
    """Known exact Compute deprecation-state spellings."""

    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    OBSOLETE = "OBSOLETE"
    DELETED = "DELETED"


class CatalogEvidenceSource(StrEnum):
    """Stable provider-neutral catalog read sources."""

    COMPUTE_ACCELERATOR_TYPES = "compute-accelerator-types"
    COMPUTE_MACHINE_TYPES = "compute-machine-types"
    TPU_LOCATIONS = "tpu-locations"
    TPU_ACCELERATOR_TYPES = "tpu-accelerator-types"
    TPU_RUNTIME_VERSIONS = "tpu-runtime-versions"


class LocationCoverageExpectation(StrEnum):
    """Why one exact location belongs to the required read set."""

    REQUESTED = "requested"
    EXPECTED = "expected"


class LocationCoverageState(StrEnum):
    """Outcome of one required catalog source read at one location."""

    SUCCESS = "success"
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    NOT_SCANNED = "not-scanned"


@dataclass(frozen=True, slots=True)
class AcceleratorId:
    """One stable public accelerator-catalog identifier."""

    value: str

    def __post_init__(self) -> None:
        """Require an immutable lowercase kebab-case public identifier."""
        _require_stable_id(self.value, "accelerator_id")


@dataclass(frozen=True, slots=True)
class CatalogMetadata:
    """Independent schema, content revision, and immutable content identity."""

    schema: str
    revision: str
    content_digest: str

    def __post_init__(self) -> None:
        """Accept only the V1 schema and an exact lowercase SHA-256 digest."""
        if self.schema != ACCELERATOR_CATALOG_SCHEMA:
            msg = f"unsupported catalog schema: {self.schema!r}"
            raise ValueError(msg)
        if not isinstance(self.revision, str) or not self.revision:
            msg = "catalog revision must be non-empty"
            raise ValueError(msg)
        prefix = "sha256:"
        if not isinstance(self.content_digest, str):
            msg = "catalog content_digest must be a lowercase sha256 digest"
            raise TypeError(msg)
        digest = self.content_digest.removeprefix(prefix)
        if (
            not self.content_digest.startswith(prefix)
            or len(digest) != _SHA256_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            msg = "catalog content_digest must be a lowercase sha256 digest"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class UnitConversionEvidence:
    """Explicit source-to-native-quota conversion with reviewable evidence."""

    source_unit: str
    quota_unit: QuotaUnit
    quota_units_per_source: int
    source_reference: str

    def __post_init__(self) -> None:
        """Reject implicit, fractional, or unevidenced unit conversion."""
        if not isinstance(self.source_unit, str) or not self.source_unit:
            msg = "conversion source_unit must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.quota_unit, QuotaUnit):
            msg = "conversion quota_unit must be a QuotaUnit"
            raise TypeError(msg)
        if (
            isinstance(self.quota_units_per_source, bool)
            or not isinstance(self.quota_units_per_source, int)
            or self.quota_units_per_source <= 0
        ):
            msg = "quota_units_per_source must be a positive integer"
            raise ValueError(msg)
        if not isinstance(self.source_reference, str) or not self.source_reference:
            msg = "conversion source_reference must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AcceleratorCatalogEntry:
    """Versioned semantics for one accelerator without provider presence claims."""

    group_id: CatalogGroupId
    accelerator_id: AcceleratorId
    management_plane: ManagementPlane
    workload_consumers: tuple[WorkloadConsumer, ...]
    native_quota_unit: QuotaUnit
    conversion: UnitConversionEvidence | None

    def __post_init__(self) -> None:
        """Keep public identity, ownership, consumers, and unit evidence exact."""
        if not isinstance(self.group_id, CatalogGroupId):
            msg = "group_id must be a CatalogGroupId"
            raise TypeError(msg)
        if not isinstance(self.accelerator_id, AcceleratorId):
            msg = "accelerator_id must be an AcceleratorId"
            raise TypeError(msg)
        if not isinstance(self.management_plane, ManagementPlane):
            msg = "management_plane must be a ManagementPlane"
            raise TypeError(msg)
        if (
            not isinstance(self.workload_consumers, tuple)
            or not self.workload_consumers
            or any(
                not isinstance(consumer, WorkloadConsumer)
                for consumer in self.workload_consumers
            )
            or len(set(self.workload_consumers)) != len(self.workload_consumers)
        ):
            msg = "workload_consumers must be unique WorkloadConsumer values"
            raise ValueError(msg)
        if not isinstance(self.native_quota_unit, QuotaUnit):
            msg = "native_quota_unit must be a QuotaUnit"
            raise TypeError(msg)
        if self.conversion is not None and not isinstance(
            self.conversion, UnitConversionEvidence
        ):
            msg = "conversion must be UnitConversionEvidence or None"
            raise TypeError(msg)
        if (
            self.conversion is not None
            and self.native_quota_unit != self.conversion.quota_unit
        ):
            msg = "conversion quota unit must equal the native quota unit"
            raise ValueError(msg)

    def require_guided_conversion(self) -> UnitConversionEvidence:
        """Return explicit conversion evidence or stop guided resolution."""
        if self.conversion is None:
            msg = "catalog entry cannot guide without unit conversion evidence"
            raise ValueError(msg)
        return self.conversion


@dataclass(frozen=True, slots=True)
class AcceleratorConstraintSet:
    """Independent exact quota-slice references limiting one accelerator."""

    accelerator_id: AcceleratorId
    references: tuple[ConstraintReference, ...]

    def __post_init__(self) -> None:
        """Require exact, non-duplicated references without combining values."""
        if not isinstance(self.accelerator_id, AcceleratorId):
            msg = "accelerator_id must be an AcceleratorId"
            raise TypeError(msg)
        if (
            not isinstance(self.references, tuple)
            or not self.references
            or any(
                not isinstance(reference, ConstraintReference)
                for reference in self.references
            )
            or len(set(self.references)) != len(self.references)
        ):
            msg = "references must be unique ConstraintReference values"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AcceleratorAttachment:
    """One exact accelerator type and count attached to a machine shape."""

    accelerator_type: str
    count: int

    def __post_init__(self) -> None:
        """Require provider text and a strictly positive attachment count."""
        _require_nonempty_string(self.accelerator_type, "accelerator_type")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count <= 0
        ):
            msg = "accelerator attachment count must be a positive integer"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ComputeAcceleratorType:
    """Normalized project-visible Compute accelerator evidence for one zone."""

    name: str
    zone: str
    lifecycle: ProviderSymbol[CatalogLifecycle] | None

    def __post_init__(self) -> None:
        """Preserve exact identity and open provider lifecycle text."""
        _require_nonempty_string(self.name, "Compute accelerator type name")
        _require_location_id(self.zone, "Compute accelerator type zone")
        if self.lifecycle is not None and (
            not isinstance(self.lifecycle, ProviderSymbol)
            or self.lifecycle.enum_type is not CatalogLifecycle
        ):
            msg = "lifecycle must preserve CatalogLifecycle provider text"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ComputeMachineType:
    """Normalized project-visible Compute machine-type evidence for one zone."""

    name: str
    zone: str
    guest_accelerators: tuple[AcceleratorAttachment, ...]
    lifecycle: ProviderSymbol[CatalogLifecycle] | None

    def __post_init__(self) -> None:
        """Preserve exact machine evidence and open provider lifecycle text."""
        _require_nonempty_string(self.name, "machine type name")
        _require_location_id(self.zone, "machine type zone")
        if not isinstance(self.guest_accelerators, tuple) or any(
            not isinstance(attachment, AcceleratorAttachment)
            for attachment in self.guest_accelerators
        ):
            msg = "guest_accelerators must be AcceleratorAttachment values"
            raise TypeError(msg)
        if self.lifecycle is not None and (
            not isinstance(self.lifecycle, ProviderSymbol)
            or self.lifecycle.enum_type is not CatalogLifecycle
        ):
            msg = "lifecycle must preserve CatalogLifecycle provider text"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class TpuLocation:
    """One provider-returned Cloud TPU location."""

    name: str
    location_id: str

    def __post_init__(self) -> None:
        """Keep provider resource identity separate from its location ID."""
        _require_nonempty_string(self.name, "TPU location name")
        _require_location_id(self.location_id, "TPU location_id")


@dataclass(frozen=True, slots=True)
class TpuAcceleratorConfig:
    """One exact provider-returned TPU version and chip topology."""

    version: str
    topology: str

    def __post_init__(self) -> None:
        """Preserve unknown versions and topologies without classification."""
        _require_nonempty_string(self.version, "TPU configuration version")
        _require_nonempty_string(self.topology, "TPU configuration topology")


@dataclass(frozen=True, slots=True)
class TpuAcceleratorType:
    """One project-and-zone-visible legacy Cloud TPU accelerator type."""

    name: str
    zone: str
    accelerator_type: str
    configurations: tuple[TpuAcceleratorConfig, ...]

    def __post_init__(self) -> None:
        """Preserve exact accelerator identity and all returned configurations."""
        _require_nonempty_string(self.name, "TPU accelerator name")
        _require_location_id(self.zone, "TPU accelerator zone")
        _require_nonempty_string(self.accelerator_type, "TPU accelerator_type")
        if not isinstance(self.configurations, tuple) or any(
            not isinstance(configuration, TpuAcceleratorConfig)
            for configuration in self.configurations
        ):
            msg = "configurations must be TpuAcceleratorConfig values"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class TpuRuntimeVersion:
    """One project-and-zone-visible legacy Cloud TPU runtime version."""

    name: str
    zone: str
    version: str

    def __post_init__(self) -> None:
        """Preserve runtime provider identity without semantic inference."""
        _require_nonempty_string(self.name, "TPU runtime name")
        _require_location_id(self.zone, "TPU runtime zone")
        _require_nonempty_string(self.version, "TPU runtime version")


@dataclass(frozen=True, slots=True)
class CatalogLocationCoverage:
    """Per-source evidence coverage for one requested or expected location."""

    source: CatalogEvidenceSource
    location: str
    expectation: LocationCoverageExpectation
    state: LocationCoverageState
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        """Distinguish authoritative empty evidence from missing or failed work."""
        if not isinstance(self.source, CatalogEvidenceSource):
            msg = "coverage source must be a CatalogEvidenceSource"
            raise TypeError(msg)
        _require_location_id(self.location, "coverage location")
        if not isinstance(self.expectation, LocationCoverageExpectation):
            msg = "coverage expectation must be a LocationCoverageExpectation"
            raise TypeError(msg)
        if not isinstance(self.state, LocationCoverageState):
            msg = "coverage state must be a LocationCoverageState"
            raise TypeError(msg)
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(diagnostic, Diagnostic) for diagnostic in self.diagnostics
        ):
            msg = "coverage diagnostics must be Diagnostic values"
            raise TypeError(msg)
        needs_reason = self.state in {
            LocationCoverageState.UNSUPPORTED,
            LocationCoverageState.FAILED,
            LocationCoverageState.NOT_SCANNED,
        }
        if needs_reason and not self.diagnostics:
            msg = "unsupported, failed, and not-scanned coverage requires a reason"
            raise ValueError(msg)
        if not needs_reason and self.diagnostics:
            msg = "successful and empty coverage cannot carry failure diagnostics"
            raise ValueError(msg)

    @property
    def complete(self) -> bool:
        """Whether this source/location has an authoritative terminal answer."""
        return self.state in {
            LocationCoverageState.SUCCESS,
            LocationCoverageState.EMPTY,
            LocationCoverageState.UNSUPPORTED,
        }


def _require_stable_id(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or value != value.lower()
        or any(
            not component or not component.isalnum() for component in value.split("-")
        )
    ):
        msg = f"{field_name} must be a lowercase kebab-case identifier"
        raise ValueError(msg)


def _require_nonempty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)


def _require_location_id(value: object, field_name: str) -> None:
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or value != value.lower()
        or not value[0].isalnum()
        or not value[-1].isalnum()
        or any(character not in allowed for character in value)
    ):
        msg = f"{field_name} must be a lowercase canonical location ID"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CatalogPredicates:
    """Four independent facts about one discovered quota slice."""

    discovered: bool
    cataloged: bool
    guided: bool
    mutable: bool

    def __post_init__(self) -> None:
        """Require explicit product booleans without truth-value coercion."""
        for field in fields(self):
            if not isinstance(getattr(self, field.name), bool):
                msg = f"{field.name} must be bool"
                raise TypeError(msg)

    def matches(self, catalog_filter: CatalogFilter) -> bool:
        """Return whether every selected catalog facet matches this value."""
        if not isinstance(catalog_filter, CatalogFilter):
            msg = "catalog_filter must be a CatalogFilter"
            raise TypeError(msg)
        return catalog_filter.matches(self)


@dataclass(frozen=True, slots=True)
class CatalogFilter:
    """Optional catalog facets combined using logical conjunction."""

    discovered: bool | None = None
    cataloged: bool | None = None
    guided: bool | None = None
    mutable: bool | None = None

    def __post_init__(self) -> None:
        """Reject truth-like values instead of coercing provider data."""
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None and not isinstance(value, bool):
                msg = f"{field.name} filter must be bool or None"
                raise TypeError(msg)

    def matches(self, predicates: CatalogPredicates) -> bool:
        """Return whether all selected facets equal their independent facts."""
        if not isinstance(predicates, CatalogPredicates):
            msg = "predicates must be CatalogPredicates"
            raise TypeError(msg)
        return all(
            expected is None or expected is getattr(predicates, field.name)
            for field in fields(self)
            for expected in (getattr(self, field.name),)
        )
