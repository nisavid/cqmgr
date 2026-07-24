"""Primitive CLI decoding for read-only quota queries and workloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cqmgr.application.operations.read_only import ReadOnlyQuotaQuery
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
)
from cqmgr.domain.catalog import AcceleratorId, CatalogGroupId
from cqmgr.domain.obtainability import (
    DistributionShape,
    GpuAttachment,
    ObtainabilityCandidate,
    SpotMachineConfiguration,
)
from cqmgr.domain.quota_queries import (
    QuotaQuery,
    QuotaQueryFilters,
    QuotaSort,
    QuotaSortField,
    SortDirection,
)
from cqmgr.domain.quotas import NormalizedDimensions, QuotaScope
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    Reconciliation,
)

if TYPE_CHECKING:
    from cqmgr.domain.scopes import ResourceScope


def parse_quota_query(  # noqa: PLR0913
    resource_scope: ResourceScope,
    *,
    services: tuple[str, ...] = (),
    catalog_groups: tuple[str, ...] = (),
    accelerators: tuple[str, ...] = (),
    locations: tuple[str, ...] = (),
    quota_scopes: tuple[str, ...] = (),
    quota_pools: tuple[str, ...] = (),
    cataloged: str | None = None,
    guided: str | None = None,
    mutable: str | None = None,
    reconciliations: tuple[str, ...] = (),
    grant_satisfactions: tuple[str, ...] = (),
    effective_confirmations: tuple[str, ...] = (),
    text: str | None = None,
    sorts: tuple[str, ...] = (),
) -> QuotaQuery:
    """Decode documented quota-list primitives into one normalized query."""
    parsed = parse_read_only_quota_query(
        services=services,
        catalog_groups=catalog_groups,
        accelerators=accelerators,
        locations=locations,
        quota_scopes=quota_scopes,
        quota_pools=quota_pools,
        cataloged=cataloged,
        guided=guided,
        mutable=mutable,
        reconciliations=reconciliations,
        grant_satisfactions=grant_satisfactions,
        effective_confirmations=effective_confirmations,
        text=text,
        sorts=sorts,
    )
    return QuotaQuery(resource_scope, filters=parsed.filters, sort=parsed.sort)


def parse_read_only_quota_query(  # noqa: PLR0913
    *,
    services: tuple[str, ...] = (),
    catalog_groups: tuple[str, ...] = (),
    accelerators: tuple[str, ...] = (),
    locations: tuple[str, ...] = (),
    quota_scopes: tuple[str, ...] = (),
    quota_pools: tuple[str, ...] = (),
    cataloged: str | None = None,
    guided: str | None = None,
    mutable: str | None = None,
    reconciliations: tuple[str, ...] = (),
    grant_satisfactions: tuple[str, ...] = (),
    effective_confirmations: tuple[str, ...] = (),
    text: str | None = None,
    sorts: tuple[str, ...] = (),
) -> ReadOnlyQuotaQuery:
    """Decode quota-list primitives while deferring scope resolution."""
    return ReadOnlyQuotaQuery(
        filters=QuotaQueryFilters(
            services=services,
            catalog_groups=tuple(CatalogGroupId(value) for value in catalog_groups),
            accelerators=tuple(AcceleratorId(value) for value in accelerators),
            locations=locations,
            quota_scopes=tuple(QuotaScope(value) for value in quota_scopes),
            quota_pools=quota_pools,
            cataloged=_parse_boolean(cataloged, "cataloged"),
            guided=_parse_boolean(guided, "guided"),
            mutable=_parse_boolean(mutable, "mutable"),
            reconciliations=tuple(Reconciliation(value) for value in reconciliations),
            grant_satisfactions=tuple(
                GrantSatisfaction(value) for value in grant_satisfactions
            ),
            effective_confirmations=tuple(
                EffectiveConfirmation(value) for value in effective_confirmations
            ),
            text=text,
        ),
        sort=tuple(_parse_sort(value) for value in sorts),
    )


def parse_compute_instance_requirement(  # noqa: PLR0913
    *,
    machine_type: str,
    instance_count: str,
    provisioning_model: str,
    locations: tuple[str, ...],
    all_compatible: bool,
    attached_accelerator_type: str | None = None,
    attached_accelerator_count: str | None = None,
) -> ComputeInstanceRequirement:
    """Decode one Compute workload shape with one explicit location mode."""
    if (attached_accelerator_type is None) != (attached_accelerator_count is None):
        msg = "attached accelerator type and count must be supplied together"
        raise ValueError(msg)
    return ComputeInstanceRequirement(
        machine_type=machine_type,
        instance_count=_parse_positive_integer(instance_count, "instance count"),
        provisioning_model=ProvisioningModel(provisioning_model),
        locations=_parse_locations(locations, all_compatible=all_compatible),
        attached_accelerator_type=attached_accelerator_type,
        attached_accelerator_count=(
            None
            if attached_accelerator_count is None
            else _parse_positive_integer(
                attached_accelerator_count,
                "attached accelerator count",
            )
        ),
    )


def parse_cloud_tpu_slice_requirement(  # noqa: PLR0913
    *,
    accelerator_type: str,
    topology: str,
    runtime_version: str,
    slice_count: str,
    provisioning_model: str,
    locations: tuple[str, ...],
    all_compatible: bool,
) -> CloudTpuSliceRequirement:
    """Decode one Cloud TPU workload shape with one explicit location mode."""
    return CloudTpuSliceRequirement(
        accelerator_type=accelerator_type,
        topology=topology,
        runtime_version=runtime_version,
        slice_count=_parse_positive_integer(slice_count, "slice count"),
        provisioning_model=ProvisioningModel(provisioning_model),
        locations=_parse_locations(locations, all_compatible=all_compatible),
    )


def parse_dimensions(values: tuple[str, ...]) -> NormalizedDimensions:
    """Decode repeatable exact ``KEY=VALUE`` selectors without inferring scope."""
    pairs = []
    for value in values:
        key, separator, dimension_value = value.partition("=")
        if not separator or not key or not dimension_value:
            msg = "dimension must use KEY=VALUE"
            raise ValueError(msg)
        pairs.append((key, dimension_value))
    return NormalizedDimensions(pairs)


def parse_obtainability_candidates(  # noqa: PLR0913
    *,
    machine_type: str,
    gpu_type: str | None,
    gpu_count: str | None,
    vm_count: str,
    distribution_shape: str,
    candidates: tuple[str, ...],
) -> tuple[ObtainabilityCandidate, ...]:
    """Decode complete exact Spot request snapshots without broadening locations."""
    machine, count, shape = parse_obtainability_shape(
        machine_type=machine_type,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        vm_count=vm_count,
        distribution_shape=distribution_shape,
    )
    if not candidates:
        msg = "at least one obtainability candidate is required"
        raise ValueError(msg)
    parsed = []
    for value in candidates:
        region, separator, zone_text = value.partition("=")
        if not region or (separator and not zone_text):
            msg = "candidate must use REGION or REGION=ZONE[,ZONE...]"
            raise ValueError(msg)
        zones = tuple(zone_text.split(",")) if separator else ()
        if any(not zone for zone in zones):
            msg = "candidate zones must be non-empty"
            raise ValueError(msg)
        parsed.append(
            ObtainabilityCandidate(
                region,
                zones,
                machine,
                count,
                shape,
            )
        )
    result = tuple(parsed)
    if len({item.candidate_id for item in result}) != len(result):
        msg = "obtainability candidates must be unique"
        raise ValueError(msg)
    return result


def parse_obtainability_shape(
    *,
    machine_type: str,
    gpu_type: str | None,
    gpu_count: str | None,
    vm_count: str,
    distribution_shape: str,
) -> tuple[SpotMachineConfiguration, int, DistributionShape]:
    """Decode the fixed machine, quantity, and distribution shared by candidates."""
    if (gpu_type is None) != (gpu_count is None):
        msg = "GPU type and count must be supplied together"
        raise ValueError(msg)
    gpu = (
        None
        if gpu_type is None or gpu_count is None
        else GpuAttachment(
            gpu_type,
            _parse_positive_integer(gpu_count, "GPU count"),
        )
    )
    machine = SpotMachineConfiguration(machine_type, gpu)
    count = _parse_positive_integer(vm_count, "VM count")
    shape = DistributionShape(distribution_shape)
    return machine, count, shape


def _parse_boolean(value: str | None, name: str) -> bool | None:
    """Decode the closed public boolean vocabulary."""
    if value is None:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    msg = f"{name} must be true or false"
    raise ValueError(msg)


def _parse_sort(value: str) -> QuotaSort:
    """Decode one sort field with its optional explicit direction."""
    field, separator, direction = value.partition(":")
    if not field or (separator and (not direction or ":" in direction)):
        msg = "sort must use FIELD or FIELD:asc|desc"
        raise ValueError(msg)
    return QuotaSort(
        QuotaSortField(field),
        SortDirection(direction) if separator else SortDirection.ASC,
    )


def _parse_positive_integer(value: str, name: str) -> int:
    """Decode one positive ASCII integer without accepting numeric lookalikes."""
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    parsed = int(value)
    if parsed < 1:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return parsed


def _parse_locations(
    locations: tuple[str, ...],
    *,
    all_compatible: bool,
) -> CandidateLocations | AllCompatibleLocations:
    """Require candidate and exhaustive location modes to be mutually exclusive."""
    if not isinstance(all_compatible, bool):
        msg = "all-compatible must be boolean"
        raise TypeError(msg)
    if all_compatible == bool(locations):
        msg = "select exactly one location mode"
        raise ValueError(msg)
    return AllCompatibleLocations() if all_compatible else CandidateLocations(locations)
