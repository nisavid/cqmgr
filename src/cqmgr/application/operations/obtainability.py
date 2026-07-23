"""Read-only application operation for exact Spot obtainability comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cqmgr.application.ports.obtainability import (
    CapacityAdviceReadRequest,
    CapacityHistoryReadRequest,
)
from cqmgr.domain.accelerator_overlay import (
    ComputeInstanceRequirement,
    ProvisioningModel,
    ResolvedWorkloadRequirement,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import ManagementPlane
from cqmgr.domain.obtainability import (
    CapacityHistory,
    DistributionShape,
    ObtainabilityCandidate,
    ObtainabilityComparison,
    ObtainabilityProductCoverage,
    SpotMachineConfiguration,
    UnrankedReason,
    rank_candidates,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import (
    Completeness,
    EvidenceGap,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    Provenance,
    StableSymbol,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from cqmgr.application.ports.obtainability import (
        CapacityAdviceReader,
        CapacityHistoryReader,
    )
    from cqmgr.application.ports.provider_reads import ProviderReadContext
    from cqmgr.domain.diagnostics import Diagnostic
    from cqmgr.domain.obtainability import CapacityAdvice
    from cqmgr.domain.quotas import ProviderRead


@dataclass(frozen=True, slots=True)
class AdviceSupport:
    """Request-specific provider support independent from catalog presence."""

    current_advice_supported: bool = True
    history_supported: bool = True

    def __post_init__(self) -> None:
        """Require both independent read-capability flags explicitly."""
        if not isinstance(self.current_advice_supported, bool) or not isinstance(
            self.history_supported,
            bool,
        ):
            msg = "advice support flags must be boolean"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ObtainabilityCompareRequest:
    """Compare one exact fixed Spot VM request shape across candidates."""

    context: ProviderReadContext
    candidates: tuple[ObtainabilityCandidate, ...]
    support: AdviceSupport = AdviceSupport()
    catalog_coverage: tuple[ObtainabilityProductCoverage, ...] = ()
    resolver_provenance: ResolvedWorkloadRequirement | None = None

    def __post_init__(self) -> None:
        """Require at least one unique candidate and one fixed machine request shape."""
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or any(
                not isinstance(item, ObtainabilityCandidate) for item in self.candidates
            )
        ):
            msg = "obtainability comparison requires typed candidates"
            raise ValueError(msg)
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            msg = "obtainability comparison candidates must be unique"
            raise ValueError(msg)
        shapes = {
            (
                item.machine,
                item.vm_count,
                item.distribution_shape,
            )
            for item in self.candidates
        }
        if len(shapes) != 1:
            msg = "obtainability comparison must keep one exact VM request fixed"
            raise ValueError(msg)
        if not isinstance(self.support, AdviceSupport):
            msg = "comparison support must use AdviceSupport"
            raise TypeError(msg)
        if not isinstance(self.catalog_coverage, tuple) or any(
            not isinstance(item, ObtainabilityProductCoverage)
            for item in self.catalog_coverage
        ):
            msg = "comparison catalog coverage must be typed"
            raise TypeError(msg)
        if self.resolver_provenance is not None and not isinstance(
            self.resolver_provenance,
            ResolvedWorkloadRequirement,
        ):
            msg = "comparison resolver provenance must be typed"
            raise TypeError(msg)


class ObtainabilityOperations:
    """Coordinate independently disableable advice and history read ports."""

    def __init__(
        self,
        advice: CapacityAdviceReader,
        history: CapacityHistoryReader,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind provider read ports and the operation observation clock."""
        self._advice = advice
        self._history = history
        self._clock = clock

    async def compare(  # noqa: C901, PLR0912, PLR0915 - explicit evidence matrix
        self,
        request: ObtainabilityCompareRequest,
    ) -> OperationResult[ObtainabilityComparison]:
        """Assess every exact candidate and rank only complete attributable evidence."""
        started_at = self._clock()
        evidence: list[
            tuple[
                ObtainabilityCandidate,
                CapacityAdvice | None,
                CapacityHistory | None,
            ]
        ] = []
        diagnostics: list[Diagnostic] = []
        provenance: list[Provenance] = []
        missing_sources: list[str] = []
        forced_reasons: dict[str, tuple[UnrankedReason, ...]] = {}
        for candidate in request.candidates:
            reasons: list[UnrankedReason] = []
            if request.support.current_advice_supported:
                advice_read = await self._advice.read(
                    CapacityAdviceReadRequest(request.context, candidate)
                )
                advice = advice_read.values[0] if len(advice_read.values) == 1 else None
                diagnostics.extend(advice_read.diagnostics)
                provenance.append(
                    _provenance(
                        "compute-capacity-advice",
                        candidate,
                        advice_read,
                    )
                )
                if not advice_read.complete:
                    missing_sources.append("compute-capacity-advice")
            else:
                advice = None
                reasons.append(UnrankedReason.CURRENT_ADVICE_UNSUPPORTED)

            history: CapacityHistory | None
            if not request.support.history_supported:
                history = None
                reasons.append(UnrankedReason.HISTORY_UNSUPPORTED)
            elif candidate.machine.is_n1_attached_gpu:
                history = None
            elif len(candidate.zones) == 1:
                zonal_read = await self._history.read(
                    CapacityHistoryReadRequest(
                        request.context,
                        candidate,
                        candidate.zones[0],
                        include_price=False,
                    )
                )
                regional_read = await self._history.read(
                    CapacityHistoryReadRequest(
                        request.context,
                        candidate,
                        candidate.endpoint_region,
                        include_price=True,
                    )
                )
                diagnostics.extend(
                    (*zonal_read.diagnostics, *regional_read.diagnostics)
                )
                provenance.extend(
                    (
                        _provenance(
                            "compute-capacity-history",
                            candidate,
                            zonal_read,
                        ),
                        _provenance(
                            "compute-capacity-history",
                            candidate,
                            regional_read,
                        ),
                    )
                )
                if not zonal_read.complete:
                    missing_sources.append("compute-capacity-history")
                if not regional_read.complete:
                    missing_sources.append("compute-capacity-history")
                history = _merge_zonal_and_regional_history(
                    candidate,
                    zonal_read,
                    regional_read,
                )
            else:
                history_read = await self._history.read(
                    CapacityHistoryReadRequest(
                        request.context,
                        candidate,
                        candidate.endpoint_region,
                        include_price=True,
                    )
                )
                diagnostics.extend(history_read.diagnostics)
                provenance.append(
                    _provenance(
                        "compute-capacity-history",
                        candidate,
                        history_read,
                    )
                )
                if not history_read.complete:
                    missing_sources.append("compute-capacity-history")
                history = (
                    history_read.values[0] if len(history_read.values) == 1 else None
                )
            evidence.append((candidate, advice, history))
            forced_reasons[candidate.candidate_id] = tuple(reasons)

        candidates = rank_candidates(
            tuple(evidence),
            forced_reasons=forced_reasons,
        )
        complete = not missing_sources
        authorization = any(
            item.code.value == "provider-read-authorization-failed"
            for item in diagnostics
        )
        unsupported = not request.support.current_advice_supported
        if unsupported:
            outcome = Outcome(
                StableSymbol("spot-advice-unsupported"),
                ExitClass.REJECTED_PRECONDITION,
            )
            boundary_reached = False
            completeness = Completeness.complete()
        elif complete:
            outcome = Outcome(StableSymbol("spot-advice-assessed"), ExitClass.SUCCESS)
            boundary_reached = True
            completeness = Completeness.complete()
        else:
            exit_class = (
                ExitClass.AUTHORIZATION
                if authorization
                else ExitClass.INCOMPLETE_EVIDENCE
            )
            outcome = Outcome(
                StableSymbol(
                    "spot-advice-authorization-failed"
                    if authorization
                    else "spot-advice-incomplete"
                ),
                exit_class,
            )
            boundary_reached = False
            gaps = tuple(
                EvidenceGap(
                    StableSymbol(source),
                    StableSymbol("required-evidence-unavailable"),
                )
                for source in dict.fromkeys(missing_sources)
            )
            completeness = (
                Completeness.unavailable(*gaps)
                if authorization
                else Completeness.incomplete(*gaps)
            )
        return OperationResult(
            operation=OperationName("obtainability.compare"),
            resource_scope=request.context.project.resource_scope,
            boundary=OperationBoundary(
                StableSymbol("spot-advice-assessed"),
                boundary_reached,
            ),
            outcome=outcome,
            completeness=completeness,
            started_at=started_at,
            finished_at=self._clock(),
            data=ObtainabilityComparison(
                candidates,
                catalog_coverage=request.catalog_coverage,
                resolver_provenance=request.resolver_provenance,
            ),
            diagnostics=tuple(diagnostics),
            provenance=tuple(provenance),
        )


def candidates_from_resolved_workload(
    resolved: ResolvedWorkloadRequirement,
    *,
    machine: SpotMachineConfiguration,
    distribution_shape: DistributionShape,
) -> tuple[ObtainabilityCandidate, ...]:
    """Expand only compatible Compute locations from one exact Spot resolution."""
    requirement = resolved.requirement
    if not isinstance(requirement, ComputeInstanceRequirement):
        msg = "obtainability requires a resolved compute-instance workload"
        raise TypeError(msg)
    if requirement.provisioning_model is not ProvisioningModel.SPOT:
        msg = "obtainability requires a Spot compute-instance workload"
        raise ValueError(msg)
    if requirement.machine_type != machine.machine_type:
        msg = "resolved workload and obtainability machine types must match"
        raise ValueError(msg)
    candidates = tuple(
        _candidate_from_resolved_location(
            location.location,
            machine,
            requirement.instance_count,
            distribution_shape,
        )
        for location in resolved.locations
        if location.disposition is WorkloadLocationDisposition.COMPATIBLE
        and location.management_plane is ManagementPlane.COMPUTE
    )
    if not candidates:
        msg = "workload resolution did not prove any compatible Compute locations"
        raise ValueError(msg)
    return candidates


def _candidate_from_resolved_location(
    location: str,
    machine: SpotMachineConfiguration,
    vm_count: int,
    distribution_shape: DistributionShape,
) -> ObtainabilityCandidate:
    region, separator, suffix = location.rpartition("-")
    is_zone = separator == "-" and len(suffix) == 1 and suffix.isalpha()
    return ObtainabilityCandidate(
        region if is_zone else location,
        (location,) if is_zone else (),
        machine,
        vm_count,
        distribution_shape,
    )


def _merge_zonal_and_regional_history(
    candidate: ObtainabilityCandidate,
    zonal: ProviderRead[CapacityHistory],
    regional: ProviderRead[CapacityHistory],
) -> CapacityHistory | None:
    zonal_value = zonal.values[0] if len(zonal.values) == 1 else None
    regional_value = regional.values[0] if len(regional.values) == 1 else None
    if zonal_value is None and regional_value is None:
        return None
    reference = zonal_value or regional_value
    if reference is None:  # pragma: no cover - narrowed by the preceding gate
        msg = "history merge requires one provider observation"
        raise AssertionError(msg)
    return CapacityHistory(
        machine_type=candidate.machine.machine_type,
        location=(
            candidate.zones[0] if zonal_value is not None else candidate.endpoint_region
        ),
        preemption=() if zonal_value is None else zonal_value.preemption,
        prices=() if regional_value is None else regional_value.prices,
        retrieved_at=max(
            item.retrieved_at
            for item in (zonal_value, regional_value)
            if item is not None
        ),
        preemption_attributable=zonal_value is not None,
        price_attributable=regional_value is not None,
        price_covers_complete_machine=(
            regional_value is not None and regional_value.price_covers_complete_machine
        ),
        source=reference.source,
    )


def _provenance(
    source: str,
    candidate: ObtainabilityCandidate,
    read: ProviderRead[object],
) -> Provenance:
    """Retain source, retrieval time, coverage, Preview status, and request identity."""
    return Provenance(
        source=StableSymbol(source),
        observed_at=read.observed_at,
        coverage=StableSymbol("complete" if read.complete else "incomplete"),
        lifecycle_or_preview_status=RedactedText("Preview"),
        request_identity=RedactedText(candidate.candidate_id),
    )
