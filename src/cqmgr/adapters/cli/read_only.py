"""Human and structured rendering for read-only CLI operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import click

from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.application.operations.quotas import QuotaBrowseData, QuotaInspectData
from cqmgr.application.operations.read_only import (
    IncompleteQuotaInspectData,
    ReadOnlyFailureData,
)
from cqmgr.domain.accelerator_overlay import (
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ResolvedWorkloadRequirement,
)
from cqmgr.domain.obtainability import ObtainabilityComparison

if TYPE_CHECKING:
    from cqmgr.domain.quota_queries import ProviderSourceCoverage, QuotaQueryItem
    from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity, QuotaQuantity
    from cqmgr.domain.results import OperationResult


@dataclass(frozen=True, slots=True)
class Presentation:
    """Read-only result controls that never suppress required result facts."""

    output: str
    no_color: bool
    quiet: bool

    def __post_init__(self) -> None:
        """Require the stable one-shot presentation options."""
        if self.output not in {"human", "json"}:
            msg = "read-only output must be human or json"
            raise ValueError(msg)
        if not isinstance(self.no_color, bool) or not isinstance(self.quiet, bool):
            msg = "read-only presentation flags must be boolean"
            raise TypeError(msg)


def _quantity(value: QuotaQuantity | None) -> str:
    """Render one native quantity without converting or dropping its unit."""
    return "unavailable" if value is None else f"{value.value} {value.unit.symbol}"


def _identity_lines(identity: EffectiveQuotaSliceIdentity) -> list[str]:
    """Render the whole effective-slice identity rather than a display surrogate."""
    dimensions = ", ".join(f"{key}={value}" for key, value in identity.dimensions.items)
    return [
        f"Slice resource scope: {identity.resource_scope.canonical_name}",
        f"Slice service: {identity.service}",
        f"Slice quota ID: {identity.quota_id}",
        f"Slice dimensions: {dimensions or 'none'}",
        f"Slice scope: {identity.quota_scope.value}",
    ]


def _item_lines(item: QuotaQueryItem) -> list[str]:
    """Render an inventory row with all independently observable quota facts."""
    return [
        *_identity_lines(item.identity),
        f"Display name: {item.display_name or 'unavailable'}",
        f"Location: {item.location or 'unavailable'}",
        f"Quota pool: {item.quota_pool or 'unavailable'}",
        "Accelerator: "
        + (
            item.accelerator_id.value
            if item.accelerator_id is not None
            else "unavailable"
        ),
        "Catalog groups: "
        + (", ".join(group.value for group in item.catalog_groups) or "none"),
        f"Discovered: {str(item.predicates.discovered).lower()}",
        f"Cataloged: {str(item.predicates.cataloged).lower()}",
        f"Guided: {str(item.predicates.guided).lower()}",
        f"Mutable: {str(item.predicates.mutable).lower()}",
        f"Effective: {_quantity(item.effective_value)}",
        f"Usage: {_quantity(item.usage_value)}",
        f"Desired: {_quantity(item.desired_value)}",
        f"Granted: {_quantity(item.granted_value)}",
        f"Evidence observed at: {_timestamp(item.evidence_observed_at)}",
        f"Reconciliation: {item.reconciliation.value}",
        f"Grant satisfaction: {item.grant_satisfaction.value}",
        f"Effective confirmation: {item.effective_confirmation.value}",
    ]


def _timestamp(value: object) -> str:
    """Render nullable UTC domain timestamps without making them a parse contract."""
    if value is None:
        return "unavailable"
    return value.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]


def _coverage_line(coverage: ProviderSourceCoverage) -> str:
    """Render each provider's queried, incomplete, or intentionally pruned state."""
    details = (
        f"pages {coverage.pages_completed}/{coverage.pages_attempted}; "
        f"observed {_timestamp(coverage.observed_at)}"
    )
    if coverage.page_cap_reached:
        details = f"{details}; page cap reached"
    if coverage.diagnostic_codes:
        codes = ", ".join(code.value for code in coverage.diagnostic_codes)
        details = f"{details}; diagnostics {codes}"
    return f"Source coverage: {coverage.service} {coverage.state.value}; {details}"


def _browse_lines(data: QuotaBrowseData) -> list[str]:
    """Render collection scope, source coverage, paging, and each exact row."""
    lines = [
        *_query_lines(data.query),
        f"Inventory revision: {data.inventory_revision or 'unavailable'}",
        f"Evidence contract: {data.evidence_contract or 'unavailable'}",
        f"Query observed at: {_timestamp(data.observed_at)}",
        f"Ordered: {str(data.ordered).lower()}",
        f"Total: {data.total if data.total is not None else 'unavailable'}",
        f"Cursor: {data.next_cursor or 'none'}",
        f"Snapshot: {data.snapshot_id or 'none'}",
    ]
    if data.catalog is not None:
        lines.extend(
            (
                f"Catalog schema: {data.catalog.schema}",
                f"Catalog revision: {data.catalog.revision}",
                f"Catalog digest: {data.catalog.content_digest}",
            )
        )
    if data.reason is not None:
        lines.append(f"Reason: {data.reason}")
    lines.extend(_coverage_line(item) for item in data.source_coverage)
    for constraint_set in data.constraint_sets:
        lines.extend(_constraint_set_lines(constraint_set))
    for index, item in enumerate(data.items, start=1):
        lines.append(f"Quota slice {index}:")
        lines.extend(_item_lines(item))
    return lines


def _query_lines(query: object) -> list[str]:
    """Render the canonical query identity retained by an initial or cursor page."""
    if query is None:
        return ["Quota query: unavailable"]
    filters = query.filters  # type: ignore[union-attr]
    sorts = query.sort  # type: ignore[union-attr]
    return [
        "Queried services: " + (", ".join(query.services) or "none"),  # type: ignore[union-attr]
        f"Query text: {filters.text or 'none'}",
        "Query catalog groups: " + _enum_values(filters.catalog_groups),
        "Query accelerators: " + _enum_values(filters.accelerators),
        "Query locations: " + _text_values(filters.locations),
        "Query quota scopes: " + _enum_values(filters.quota_scopes),
        "Query quota pools: " + _text_values(filters.quota_pools),
        f"Query cataloged: {_optional_boolean(value=filters.cataloged)}",
        f"Query guided: {_optional_boolean(value=filters.guided)}",
        f"Query mutable: {_optional_boolean(value=filters.mutable)}",
        "Query reconciliation: " + _enum_values(filters.reconciliations),
        "Query grant satisfaction: " + _enum_values(filters.grant_satisfactions),
        "Query effective confirmation: "
        + _enum_values(filters.effective_confirmations),
        "Query sort: "
        + (
            ", ".join(f"{item.field.value}:{item.direction.value}" for item in sorts)
            or "canonical-slice-identity"
        ),
    ]


def _enum_values(values: object) -> str:
    """Render a tuple of public value objects."""
    return ", ".join(item.value for item in values) or "all"  # type: ignore[union-attr]


def _text_values(values: object) -> str:
    """Render a tuple of canonical text selectors."""
    return ", ".join(values) or "all"  # type: ignore[arg-type]


def _optional_boolean(*, value: bool | None) -> str:
    """Render an optional typed boolean facet."""
    return "all" if value is None else str(value).lower()


def _inspect_lines(data: QuotaInspectData) -> list[str]:
    """Render exact inspection identity, evidence, and status without guessing."""
    lines = _identity_lines(data.identity)
    if data.evidence is not None:
        lines.extend(
            (
                f"Effective: {_quantity(data.evidence.effective_value)}",
                f"Evidence metric: {data.evidence.metric}",
                "Applicable locations: "
                + (", ".join(data.evidence.applicable_locations) or "none"),
                "Declared dimensions: "
                + (", ".join(data.evidence.declared_dimensions) or "none"),
                "Eligible for increase: "
                f"{str(data.evidence.eligibility.eligible).lower()}",
                f"Eligibility reason: {data.evidence.eligibility.reason.raw}",
                f"Fixed quota: {str(data.evidence.fixed).lower()}",
                f"Concurrent: {str(data.evidence.concurrent).lower()}",
                f"Precise: {str(data.evidence.precise).lower()}",
                f"Refresh interval: {data.evidence.refresh_interval or 'unavailable'}",
                f"Ongoing rollout: {str(data.evidence.ongoing_rollout).lower()}",
                f"Container type: {data.evidence.container_type.raw}",
            )
        )
    if data.item is not None:
        lines.extend(_item_lines(data.item))
    if data.status is not None:
        lines.extend(
            (
                f"Status reconciliation: {data.status.reconciliation.value}",
                f"Status grant satisfaction: {data.status.grant_satisfaction.value}",
                "Status effective confirmation: "
                f"{data.status.effective_confirmation.value}",
                f"Status desired: {_quantity(data.status.desired)}",
                f"Status granted: {_quantity(data.status.granted)}",
                f"Status effective: {_quantity(data.status.effective)}",
                f"Status observed at: {_timestamp(data.status.status_observed_at)}",
            )
        )
    if data.preference is not None:
        preference = data.preference
        lines.extend(
            (
                f"Preference identity: {preference.provider_name}",
                f"Preference desired: {preference.preferred_value}",
                "Preference granted: "
                + (
                    str(preference.granted_value)
                    if preference.granted_value is not None
                    else "unavailable"
                ),
                f"Preference etag: {preference.etag or 'unavailable'}",
                f"Preference reconciling: {str(preference.reconciling).lower()}",
                f"Preference state: {preference.state_detail or 'unavailable'}",
                f"Preference origin: {preference.request_origin.raw}",
                f"Preference created at: {_timestamp(preference.create_time)}",
                f"Preference observed at: {_timestamp(preference.update_time)}",
            )
        )
    if data.usage is not None:
        lines.extend(_usage_lines(data.usage))
    for constraint_set in data.constraint_sets:
        lines.extend(_constraint_set_lines(constraint_set))
    if data.reason is not None:
        lines.append(f"Reason: {data.reason}")
    return lines


def _incomplete_inspect_lines(data: IncompleteQuotaInspectData) -> list[str]:
    """Render retained exact observations from an incomplete inspect lookup."""
    selector = data.selector
    dimensions = ", ".join(f"{key}={value}" for key, value in selector.dimensions.items)
    lines = [
        f"Selector service: {selector.service}",
        f"Selector quota ID: {selector.quota_id}",
        f"Selector location: {selector.location}",
        f"Selector dimensions: {dimensions or 'none'}",
        f"Reason: {data.reason}",
    ]
    lines.extend(_coverage_line(coverage) for coverage in data.source_coverage)
    for index, item in enumerate(data.matching_items, start=1):
        lines.append(f"Matching quota slice {index}:")
        lines.extend(_item_lines(item))
    return lines


def _usage_lines(usage: object) -> list[str]:
    """Render the complete normalized Monitoring series retained by inspection."""
    lines = [
        f"Usage metric: {usage.metric_type}",  # type: ignore[union-attr]
        f"Usage resource type: {usage.resource_type}",  # type: ignore[union-attr]
        f"Usage unit: {usage.unit or 'unavailable'}",  # type: ignore[union-attr]
        "Usage metric labels: " + _dimensions_text(usage.metric_labels),  # type: ignore[union-attr]
        "Usage resource labels: " + _dimensions_text(usage.resource_labels),  # type: ignore[union-attr]
    ]
    for point in usage.points:  # type: ignore[union-attr]
        lines.extend(
            (
                f"Usage point: {point.value.value} ({point.value.kind.value})",
                "Usage interval: "
                f"{_timestamp(point.interval_start)} through "
                f"{_timestamp(point.interval_end)}",
            )
        )
    return lines


def _dimensions_text(dimensions: object) -> str:
    """Render normalized dimension pairs in their canonical order."""
    return (
        ", ".join(f"{key}={value}" for key, value in dimensions.items)  # type: ignore[union-attr]
        or "none"
    )


def _constraint_set_lines(constraint_set: object) -> list[str]:
    """Render one anchored accelerator relationship without collapsing slices."""
    lines = [
        f"Constraint set accelerator: {constraint_set.accelerator_id.value}"  # type: ignore[union-attr]
    ]
    for reference in constraint_set.references:  # type: ignore[union-attr]
        identity = reference.slice_identity
        lines.extend(
            (
                f"Constraint slice service: {identity.service}",
                f"Constraint slice quota ID: {identity.quota_id}",
                f"Constraint slice dimensions: {_dimensions_text(identity.dimensions)}",
                f"Constraint slice scope: {identity.quota_scope.value}",
            )
        )
    return lines


def _resolution_lines(data: ResolvedWorkloadRequirement) -> list[str]:
    """Render every candidate location and each independently limiting slice."""
    requirement = data.requirement
    lines = [
        f"Requirement: {requirement.kind.value}",
        *_workload_shape_lines(requirement),
        f"Location mode: {requirement.locations.mode.value}",
        "All-compatible locations exhaustive: "
        + (
            "unavailable"
            if data.all_compatible_locations_exhaustive is None
            else str(data.all_compatible_locations_exhaustive).lower()
        ),
    ]
    for location in data.locations:
        lines.extend(
            (
                f"Location: {location.location}",
                f"Disposition: {location.disposition.value}",
                f"Owning service: {location.owning_service or 'unavailable'}",
                "Management plane: "
                + (
                    location.management_plane.value
                    if location.management_plane is not None
                    else "unavailable"
                ),
                "Supported consumers: "
                + (
                    ", ".join(item.value for item in location.supported_consumers)
                    or "none"
                ),
                f"Quota pool: {location.quota_pool or 'unavailable'}",
                "Deployable accelerator quantity: "
                + (
                    str(location.deployable_accelerator_quantity)
                    if location.deployable_accelerator_quantity is not None
                    else "unavailable"
                ),
                "Proven attached accelerator type: "
                f"{location.attached_accelerator_type or 'unavailable'}",
                "Proven attached accelerator count: "
                + (
                    str(location.attached_accelerator_count)
                    if location.attached_accelerator_count is not None
                    else "unavailable"
                ),
                "Permits: "
                + (
                    str(location.permits).lower()
                    if location.permits is not None
                    else "unavailable"
                ),
            )
        )
        if location.failure_reason is not None:
            lines.append(f"Reason: {location.failure_reason.value}")
        for requirement_item in location.constraint_requirements:
            lines.extend(_identity_lines(requirement_item.identity))
            lines.append(
                "Constraint required: "
                f"{_quantity(requirement_item.required)} "
                f"(source quantity: {requirement_item.source_quantity})"
            )
        for index, assessment in enumerate(location.assessments, start=1):
            lines.extend(
                (
                    f"Constraint assessment {index}:",
                    *_identity_lines(assessment.identity),
                    f"Constraint effective: {_quantity(assessment.effective)}",
                    f"Constraint usage: {_quantity(assessment.usage)}",
                    f"Constraint permits: {str(assessment.permits).lower()}",
                )
            )
        for coverage in location.coverage:
            lines.append(
                "Catalog coverage: "
                f"{coverage.source.value} {coverage.location} "
                f"{coverage.expectation.value} {coverage.state.value}"
            )
            for diagnostic in coverage.diagnostics:
                lines.extend(
                    (
                        "Catalog diagnostic "
                        f"{diagnostic.code.value} ({diagnostic.severity.value})",
                        f"Catalog guidance: {diagnostic.message}",
                    )
                )
    return lines


def _workload_shape_lines(
    requirement: ComputeInstanceRequirement | CloudTpuSliceRequirement,
) -> list[str]:
    """Render every caller-owned workload input without derived selector guesses."""
    if isinstance(requirement, ComputeInstanceRequirement):
        lines = [
            f"Machine type: {requirement.machine_type}",
            f"Instance count: {requirement.instance_count}",
            f"Provisioning model: {requirement.provisioning_model.value}",
        ]
        if requirement.attached_accelerator_type is not None:
            lines.extend(
                (
                    "Attached accelerator type: "
                    f"{requirement.attached_accelerator_type}",
                    "Attached accelerator count: "
                    f"{requirement.attached_accelerator_count}",
                )
            )
        return lines
    return [
        f"Accelerator type: {requirement.accelerator_type}",
        f"Topology: {requirement.topology}",
        f"Runtime version: {requirement.runtime_version}",
        f"Slice count: {requirement.slice_count}",
        f"Provisioning model: {requirement.provisioning_model.value}",
    ]


def _data_lines(data: object) -> list[str]:  # noqa: PLR0911
    """Select the public data shape without exposing adapter implementation state."""
    if isinstance(data, QuotaBrowseData):
        return _browse_lines(data)
    if isinstance(data, QuotaInspectData):
        return _inspect_lines(data)
    if isinstance(data, IncompleteQuotaInspectData):
        return _incomplete_inspect_lines(data)
    if isinstance(data, ResolvedWorkloadRequirement):
        return _resolution_lines(data)
    if isinstance(data, ObtainabilityComparison):
        return _obtainability_lines(data)
    if isinstance(data, ReadOnlyFailureData):
        return [f"Reason: {data.reason}"]
    if data is None:
        return ["Result data: unavailable"]
    return [json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)]


def _obtainability_lines(data: ObtainabilityComparison) -> list[str]:
    """Render exact request identity, transparent rank, and every evidence interval."""
    lines = [
        f"Provider status: {data.preview_status}",
        f"Capacity guarantee: {'no' if data.no_capacity_guarantee else 'yes'}",
    ]
    if data.resolver_provenance is not None:
        lines.append("Resolver provenance:")
        lines.extend(_resolution_lines(data.resolver_provenance))
    for coverage in data.catalog_coverage:
        lines.extend(
            (
                f"Catalog product: {coverage.product_id}",
                f"Catalog product service: {coverage.service}",
                f"Cataloged: {str(coverage.cataloged).lower()}",
                "Current advice supported: "
                f"{str(coverage.current_advice_supported).lower()}",
                f"History supported: {str(coverage.history_supported).lower()}",
                "Coverage reasons: " + (", ".join(coverage.reasons) or "none"),
            )
        )
    for index, assessment in enumerate(data.candidates, start=1):
        candidate = assessment.candidate
        machine = candidate.machine
        lines.extend(
            (
                f"Obtainability candidate {index}:",
                f"Candidate identity: {candidate.candidate_id}",
                f"Candidate endpoint region: {candidate.endpoint_region}",
                "Candidate zones: " + (", ".join(candidate.zones) or "none"),
                f"Machine type: {machine.machine_type}",
                "GPU type: "
                + (machine.gpu.accelerator_type if machine.gpu is not None else "none"),
                "GPU count: "
                + (str(machine.gpu.count) if machine.gpu is not None else "none"),
                f"Local SSD count: {machine.local_ssd_count}",
                f"VM quantity: {candidate.vm_count}",
                f"Distribution shape: {candidate.distribution_shape.value}",
                "Rank: "
                + (str(assessment.rank) if assessment.rank is not None else "unranked"),
                "Unranked reasons: "
                + (
                    ", ".join(item.value for item in assessment.unranked_reasons)
                    or "none"
                ),
            )
        )
        advice = assessment.advice
        if advice is None:
            lines.extend(
                (
                    "Provider obtainability: unavailable",
                    "Estimated uptime: unavailable",
                )
            )
        else:
            band = assessment.band
            lines.extend(
                (
                    "Provider obtainability: "
                    f"{advice.obtainability} "
                    f"({band.value if band is not None else 'unavailable'})",
                    f"Estimated uptime: {advice.estimated_uptime}",
                    f"Advice retrieved at: {_timestamp(advice.retrieved_at)}",
                    f"Advice source: {advice.source}",
                )
            )
            for shard in advice.shards:
                lines.extend(
                    (
                        f"Recommended shard zone: {shard.zone}",
                        f"Recommended shard machine type: {shard.machine_type}",
                        f"Recommended shard VM count: {shard.vm_count}",
                        "Recommended shard provisioning model: "
                        f"{shard.provisioning_model}",
                    )
                )
        lines.extend(
            (
                "30-day p90 preemption rate: "
                + (
                    str(assessment.preemption_p90)
                    if assessment.preemption_p90 is not None
                    else "unavailable"
                ),
                "Total-request hourly price: "
                + (
                    f"USD {assessment.total_request_hourly_price_usd}"
                    if assessment.total_request_hourly_price_usd is not None
                    else "unavailable"
                ),
            )
        )
        history = assessment.history
        if history is not None:
            lines.extend(
                (
                    f"History location: {history.location}",
                    f"History retrieved at: {_timestamp(history.retrieved_at)}",
                    f"History source: {history.source}",
                    "Preemption attributable: "
                    f"{str(history.preemption_attributable).lower()}",
                    f"Price attributable: {str(history.price_attributable).lower()}",
                    "Price covers complete machine request: "
                    f"{str(history.price_covers_complete_machine).lower()}",
                )
            )
            lines.extend(
                (
                    "Preemption interval: "
                    f"{_timestamp(item.started_at)} through "
                    f"{_timestamp(item.finished_at)}; rate {item.rate}"
                )
                for item in history.preemption
            )
            lines.extend(
                (
                    "Price interval: "
                    f"{_timestamp(item.started_at)} through "
                    f"{_timestamp(item.finished_at)}; "
                    f"USD {item.usd_per_vm_hour} per VM hour"
                )
                for item in history.prices
            )
    return lines


def _human_lines(result: OperationResult[Any]) -> list[str]:
    """Build one ANSI-free human result with a common error envelope."""
    data_lines = _data_lines(result.data)
    identity_lines = _identity_evidence_lines(result)
    resource_scope = (
        result.resource_scope.canonical_name
        if result.resource_scope is not None
        else "none"
    )
    if int(result.outcome.exit_class) == 0:
        return [
            f"Operation: {result.operation.value}",
            f"Resource scope: {resource_scope}",
            f"Complete: {str(result.completeness.is_complete).lower()}",
            *identity_lines,
            *data_lines,
        ]
    reached = "reached" if result.boundary.reached else "not reached"
    return [
        f"Operation: {result.operation.value}",
        f"Outcome: {result.outcome.code.value} (exit {int(result.outcome.exit_class)})",
        f"Boundary: {result.boundary.condition.value} ({reached})",
        f"Complete: {str(result.completeness.is_complete).lower()}",
        f"Resource scope: {resource_scope}",
        *identity_lines,
        *data_lines,
    ]


def _identity_evidence_lines(result: OperationResult[Any]) -> list[str]:
    """Render only the sanitized provider identity facts retained in the result."""
    evidence = result.identity_evidence
    if evidence is None:
        return []
    acting_principal = (
        evidence.acting_principal.value
        if evidence.acting_principal is not None
        else "unavailable"
    )
    chain = " -> ".join(item.value for item in evidence.impersonation_chain)
    return [
        f"Credential kind: {evidence.credential_kind.value}",
        f"Principal verification: {evidence.verification.value}",
        f"Acting principal: {acting_principal}",
        f"Impersonation chain: {chain or 'none'}",
    ]


def _diagnostic_lines(result: OperationResult[Any]) -> list[str]:
    """Render each safe diagnostic and its recovery guidance in stable order."""
    lines: list[str] = []
    for diagnostic in result.diagnostics:
        lines.extend(
            (
                f"Diagnostic {diagnostic.code.value} ({diagnostic.severity.value})",
                "Diagnostic context: "
                f"{diagnostic.source.value}; {diagnostic.phase.value}; "
                f"retry {diagnostic.retry.value}",
                f"Guidance: {diagnostic.message}",
            )
        )
    return lines


def emit_read_only_result(
    result: OperationResult[Any],
    presentation: Presentation,
) -> int:
    """Write one selected read-only result form and return its exit class."""
    exit_class = int(result.outcome.exit_class)
    if presentation.output == "json":
        click.echo(
            json.dumps(
                operation_result_mapping(result),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        for line in _human_lines(result):
            click.echo(line, err=exit_class != 0)
        for line in _diagnostic_lines(result):
            click.echo(line, err=True)
    return exit_class
