"""Pilot contracts for the Textual shell and quota inspector."""

# Test fakes intentionally mirror public protocols and favor literal domain fixtures.
# ruff: noqa: ANN401, D102, D107, FBT003, PLR2004

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from click.testing import CliRunner
from textual.filter import Monochrome, NoColor
from textual.widgets import Button, DataTable, Input, Static

import cqmgr.cli as cli_module
from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.adapters.tui.app import CloudQuotaManagerApp
from cqmgr.application.operations.apply import (
    ApplyChildData,
    ApplyData,
    ApplyRequest,
)
from cqmgr.application.operations.audit import (
    AuditInspectData,
    AuditListData,
    AuditVerifyData,
)
from cqmgr.application.operations.plans import (
    ComposeChild,
    ComposeRequest,
    PlanReviewData,
    PlanReviewRequest,
    PreviewData,
    PreviewRequest,
    RequestPlanOperations,
)
from cqmgr.application.operations.quotas import QuotaBrowseData, QuotaInspectData
from cqmgr.application.operations.read_only import (
    QuotaInspectSelector,
    ReadOnlyFailureData,
    ReadOnlyQuotaQuery,
    ReadOnlyScopeInput,
)
from cqmgr.application.operations.watch import WatchRequest
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
    QuotaConstraintRequirement,
    ResolutionFailureReason,
    ResolvedWorkloadLocation,
    ResolvedWorkloadRequirement,
    WorkloadLocationDisposition,
)
from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    UnknownDispatchResolution,
)
from cqmgr.domain.audit import (
    AUDIT_GENESIS_HASH,
    AuditQuery,
    AuditRecord,
    AuditRecordDraft,
    AuditRecordKind,
    AuditVerification,
)
from cqmgr.domain.catalog import (
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogPredicates,
    ManagementPlane,
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
from cqmgr.domain.identity import (
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
    ProviderIdentityEvidence,
)
from cqmgr.domain.obtainability import (
    AdviceShard,
    CapacityAdvice,
    CapacityHistory,
    DistributionShape,
    GpuAttachment,
    ObtainabilityBand,
    ObtainabilityCandidate,
    ObtainabilityComparison,
    ObtainabilityProductCoverage,
    PreemptionInterval,
    PriceInterval,
    RankedCandidate,
    SpotMachineConfiguration,
    UnrankedReason,
)
from cqmgr.domain.plans import (
    ContactBinding,
    EvidenceBinding,
    PlanKind,
    PlanLedgerState,
    PlanPrincipal,
    QuotaRequestBundlePlan,
    QuotaRequestPlanChild,
    TargetStrategy,
    review_plan,
)
from cqmgr.domain.quota_queries import ProviderSourceCoverage, QuotaQueryItem
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
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
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
    WatchDisposition,
)
from cqmgr.domain.watch import (
    WatchAggregate,
    WatchChildIdentity,
    WatchChildSummary,
    WatchEventKind,
    WatchResultData,
    WatchStreamEvent,
    WatchSubject,
)

if TYPE_CHECKING:
    import pytest

    from cqmgr.application.operations.obtainability import (
        PreparedObtainabilityComparison,
    )

NOW = datetime(2026, 7, 23, 20, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
OTHER_PROJECT_SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/987654321")
ALT_SCOPE = ResourceScope(ResourceScopeKind.FOLDER, "folders/987654321")
UNIT = QuotaUnit("1")
DEFAULT_SCOPE_INPUT = ReadOnlyScopeInput()


def _static(app: CloudQuotaManagerApp, selector: str) -> Static:
    return app.query_one(selector, Static)


def _input(app: CloudQuotaManagerApp, selector: str) -> Input:
    return app.query_one(selector, Input)


def _table(app: CloudQuotaManagerApp, selector: str) -> DataTable[object]:
    return app.query_one(selector, DataTable)


def _button(app: CloudQuotaManagerApp, selector: str) -> Button:
    return app.query_one(selector, Button)


def _item(
    quota_id: str,
    *,
    service: str,
    predicates: CatalogPredicates,
    location: str,
) -> QuotaQueryItem:
    identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        service,
        quota_id,
        NormalizedDimensions((("location", location),)),
        QuotaScope.REGIONAL,
    )
    return QuotaQueryItem(
        identity=identity,
        display_name=quota_id.replace("-", " "),
        accelerator_id=None,
        location=location,
        quota_pool="standard",
        predicates=predicates,
        effective_value=QuotaQuantity(8, UNIT),
        usage_value=QuotaQuantity(2, UNIT),
        evidence_observed_at=NOW,
    )


ITEMS = (
    _item(
        "GPUS-ALL-REGIONS-per-project",
        service="compute.googleapis.com",
        predicates=CatalogPredicates(True, True, True, True),
        location="us-central1",
    ),
    _item(
        "NEW-PROVIDER-HARDWARE",
        service="compute.googleapis.com",
        predicates=CatalogPredicates(True, False, False, False),
        location="us-east1",
    ),
    _item(
        "TPU-V6E-CHIPS",
        service="tpu.googleapis.com",
        predicates=CatalogPredicates(True, True, False, False),
        location="us-central2",
    ),
)


def _browse_result(
    *,
    items: tuple[QuotaQueryItem, ...] = ITEMS,
    complete: bool = True,
    tpu_queried: bool = True,
    diagnostics: tuple[Diagnostic, ...] = (),
) -> OperationResult[QuotaBrowseData]:
    coverage = (
        ProviderSourceCoverage.complete(
            "compute.googleapis.com",
            pages_attempted=1,
            pages_completed=1,
            observed_at=NOW,
        ),
        (
            (
                ProviderSourceCoverage.complete(
                    "tpu.googleapis.com",
                    pages_attempted=1,
                    pages_completed=1,
                    observed_at=NOW,
                )
                if complete
                else ProviderSourceCoverage.incomplete(
                    "tpu.googleapis.com",
                    pages_attempted=2,
                    pages_completed=1,
                    observed_at=NOW,
                )
            )
            if tpu_queried
            else ProviderSourceCoverage.intentionally_unqueried("tpu.googleapis.com")
        ),
    )
    completeness = (
        Completeness.complete()
        if complete
        else Completeness.incomplete(
            EvidenceGap(
                StableSymbol("cloud-tpu"),
                StableSymbol("provider-page-incomplete"),
            )
        )
    )
    return OperationResult(
        operation=OperationName("quota.list"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("logical-page-read"),
            reached=complete,
        ),
        outcome=Outcome(
            StableSymbol("succeeded" if complete else "provider-source-incomplete"),
            ExitClass.SUCCESS if complete else ExitClass.INCOMPLETE_EVIDENCE,
        ),
        completeness=completeness,
        started_at=NOW,
        finished_at=NOW,
        data=QuotaBrowseData(
            query=None,
            items=items,
            constraint_sets=(),
            ordered=complete,
            total=len(items) if complete else None,
            next_cursor=None,
            snapshot_id="snapshot-40",
            source_coverage=coverage,
            observed_at=NOW,
        ),
        diagnostics=diagnostics,
        identity_evidence=ProviderIdentityEvidence(
            credential_kind=CredentialKind.DIRECT_USER,
            verification=PrincipalVerification.VERIFIED,
            acting_principal=PrincipalIdentity(
                "principal://accounts.google.com/operator@example.com"
            ),
        ),
    )


PARTIAL_DIAGNOSTIC = Diagnostic(
    code=DiagnosticCode("tpu-location-page-failed"),
    severity=Severity.WARNING,
    phase=DiagnosticPhase("provider-read"),
    source=DiagnosticSource("cloud-tpu"),
    retry=RetryDisposition.AFTER_REFRESH,
    message=RedactedText(
        "Cloud TPU location evidence is incomplete; refresh the inventory."
    ),
)


def _failure_result() -> OperationResult[ReadOnlyFailureData]:
    return OperationResult(
        operation=OperationName("quota.list"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("logical-page-read"),
            reached=False,
        ),
        outcome=Outcome(
            StableSymbol("provider-read-failed"),
            ExitClass.OPERATIONAL_FAILURE,
        ),
        completeness=Completeness.unavailable(
            EvidenceGap(
                StableSymbol("cloud-quotas"),
                StableSymbol("transport-failed"),
            )
        ),
        started_at=NOW,
        finished_at=NOW,
        data=ReadOnlyFailureData("provider-read-failed"),
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode("provider-read-failed"),
                severity=Severity.ERROR,
                phase=DiagnosticPhase("provider-read"),
                source=DiagnosticSource("cloud-quotas"),
                retry=RetryDisposition.AFTER_BACKOFF,
                message=RedactedText(
                    "The quota inventory could not be read; retry after backoff."
                ),
            ),
        ),
    )


def _obtainability_result() -> OperationResult[ObtainabilityComparison]:
    machine = SpotMachineConfiguration("a3-highgpu-8g")
    candidate = ObtainabilityCandidate(
        "us-central1",
        ("us-central1-a", "us-central1-b"),
        machine,
        2,
        DistributionShape.BALANCED,
    )
    advice = CapacityAdvice(
        Decimal("0.8"),
        "7d",
        (
            AdviceShard("us-central1-a", machine.machine_type, 1, "SPOT"),
            AdviceShard("us-central1-b", machine.machine_type, 1, "SPOT"),
        ),
        NOW,
    )
    history = CapacityHistory(
        machine.machine_type,
        candidate.endpoint_region,
        tuple(
            PreemptionInterval(
                NOW - timedelta(days=30 - index),
                NOW - timedelta(days=29 - index),
                Decimal("0.12"),
            )
            for index in range(30)
        ),
        (
            PriceInterval(
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                Decimal("3.25"),
            ),
        ),
        NOW,
    )
    comparison = ObtainabilityComparison(
        (
            RankedCandidate(
                candidate,
                advice,
                history,
                ObtainabilityBand.HIGH,
                Decimal("0.12"),
                Decimal("6.50"),
                1,
                (),
            ),
        )
    )
    return OperationResult(
        operation=OperationName("obtainability.compare"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("spot-advice-assessed"),
            reached=True,
        ),
        outcome=Outcome(StableSymbol("spot-advice-assessed"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=comparison,
        identity_evidence=ProviderIdentityEvidence(
            credential_kind=CredentialKind.DIRECT_USER,
            verification=PrincipalVerification.VERIFIED,
            acting_principal=PrincipalIdentity(
                "principal://accounts.google.com/operator@example.com"
            ),
        ),
    )


def _incomplete_obtainability_result() -> OperationResult[ObtainabilityComparison]:
    """Retain ranked ties and unsupported N1 history with incomplete evidence."""
    machine = SpotMachineConfiguration("a3-highgpu-8g")
    candidates = (
        ObtainabilityCandidate(
            "us-central1",
            ("us-central1-a", "us-central1-b"),
            machine,
            2,
            DistributionShape.BALANCED,
        ),
        ObtainabilityCandidate(
            "us-east1",
            ("us-east1-b", "us-east1-c"),
            machine,
            2,
            DistributionShape.BALANCED,
        ),
    )
    advice = CapacityAdvice(Decimal("0.8"), "7d", (), NOW)
    history = CapacityHistory(
        machine.machine_type,
        "us-central1",
        (),
        (),
        NOW,
    )
    n1_machine = SpotMachineConfiguration(
        "n1-standard-8",
        GpuAttachment("nvidia-tesla-t4", 1),
    )
    n1_candidate = ObtainabilityCandidate(
        "us-west1",
        ("us-west1-a",),
        n1_machine,
        1,
        DistributionShape.ANY_SINGLE_ZONE,
    )
    comparison = ObtainabilityComparison(
        (
            RankedCandidate(
                candidates[0],
                advice,
                history,
                ObtainabilityBand.HIGH,
                Decimal("0.12"),
                Decimal("6.50"),
                1,
                (),
            ),
            RankedCandidate(
                candidates[1],
                advice,
                replace(history, location="us-east1"),
                ObtainabilityBand.HIGH,
                Decimal("0.12"),
                Decimal("6.50"),
                2,
                (),
            ),
            RankedCandidate(
                n1_candidate,
                CapacityAdvice(Decimal("0.6"), "1d", (), NOW),
                None,
                ObtainabilityBand.MEDIUM,
                None,
                None,
                None,
                (UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU,),
            ),
        ),
        catalog_coverage=(
            ObtainabilityProductCoverage(
                "a3-highgpu-8g",
                "compute.googleapis.com",
                True,
                True,
                True,
            ),
            ObtainabilityProductCoverage(
                "n1-attached-gpu",
                "compute.googleapis.com",
                True,
                True,
                False,
                ("capacityHistory does not support N1 attached GPUs",),
            ),
        ),
    )
    gap = EvidenceGap(
        StableSymbol("capacity-history"),
        StableSymbol("candidate-history-incomplete"),
    )
    return OperationResult(
        operation=OperationName("obtainability.compare"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("spot-advice-assessed"),
            reached=False,
        ),
        outcome=Outcome(
            StableSymbol("provider-source-incomplete"),
            ExitClass.INCOMPLETE_EVIDENCE,
        ),
        completeness=Completeness.incomplete(gap),
        started_at=NOW,
        finished_at=NOW,
        data=comparison,
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode("capacity-history-incomplete"),
                severity=Severity.WARNING,
                phase=DiagnosticPhase("provider-read"),
                source=DiagnosticSource("capacity-history"),
                retry=RetryDisposition.AFTER_REFRESH,
                message=RedactedText(
                    "One candidate history source is incomplete; ranking is partial."
                ),
            ),
        ),
        provenance=(
            Provenance(
                StableSymbol("capacity-advice"),
                NOW,
                StableSymbol("complete"),
                lifecycle_or_preview_status=RedactedText("Preview"),
                request_identity=RedactedText("a3-highgpu-8g x2 balanced"),
            ),
            Provenance(
                StableSymbol("capacity-history"),
                NOW,
                StableSymbol("incomplete"),
                request_identity=RedactedText("candidate histories"),
            ),
        ),
    )


def _compatible_compute_location(location: str) -> ResolvedWorkloadLocation:
    identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        "GPUS-PER-GPU-FAMILY-per-project-region",
        NormalizedDimensions((("region", location.rpartition("-")[0]),)),
        QuotaScope.REGIONAL,
    )
    conversion = UnitConversionEvidence(
        "card",
        UNIT,
        1,
        "https://docs.cloud.google.com/compute/resource-usage",
    )
    return ResolvedWorkloadLocation(
        location=location,
        disposition=WorkloadLocationDisposition.COMPATIBLE,
        accelerator_id=AcceleratorId("nvidia-h100"),
        owning_service="compute.googleapis.com",
        management_plane=ManagementPlane.COMPUTE,
        supported_consumers=(WorkloadConsumer.COMPUTE_ENGINE,),
        quota_pool="preemptible",
        deployable_accelerator_quantity=8,
        constraint_set=AcceleratorConstraintSet(
            AcceleratorId("nvidia-h100"),
            (ConstraintReference(identity),),
        ),
        constraint_requirements=(
            QuotaConstraintRequirement(
                identity,
                8,
                QuotaQuantity(8, UNIT),
                conversion,
            ),
        ),
        coverage=(),
    )


def _resolved_compute_result(
    requirement: ComputeInstanceRequirement,
    *,
    locations: tuple[str, ...],
) -> OperationResult[ResolvedWorkloadRequirement]:
    return OperationResult(
        operation=OperationName("quota.resolve"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("workload-resolved"),
            reached=True,
        ),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=ResolvedWorkloadRequirement(
            requirement,
            tuple(_compatible_compute_location(location) for location in locations),
            (
                True
                if isinstance(requirement.locations, AllCompatibleLocations)
                else None
            ),
        ),
        identity_evidence=ProviderIdentityEvidence(
            credential_kind=CredentialKind.DIRECT_USER,
            verification=PrincipalVerification.VERIFIED,
            acting_principal=PrincipalIdentity(
                "principal://accounts.google.com/operator@example.com"
            ),
        ),
    )


AUDIT_RECORD = AuditRecord(
    record_id="audit-00000000000000000001",
    sequence=1,
    segment=1,
    draft=AuditRecordDraft(
        kind=AuditRecordKind.PREVIEW_EVIDENCE,
        operation=OperationName("request.preview"),
        resource_scope=SCOPE,
        occurred_at=NOW,
        outcome=StableSymbol("plan-created"),
    ),
    previous_hash=AUDIT_GENESIS_HASH,
    record_hash="sha256:" + ("a" * 64),
)


def _audit_result(
    records: tuple[AuditRecord, ...] = (),
) -> OperationResult[AuditListData]:
    return OperationResult(
        operation=OperationName("audit.list"),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol("audit-query-read"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=AuditListData(AuditQuery(), records, None),
    )


def _inspect_result(
    item: QuotaQueryItem = ITEMS[0],
) -> OperationResult[QuotaInspectData]:
    status = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.SETTLED,
        baseline=QuotaQuantity(4, UNIT),
        desired=QuotaQuantity(8, UNIT),
        granted=QuotaQuantity(8, UNIT),
        effective=QuotaQuantity(8, UNIT),
        status_observed_at=NOW,
        effective_observed_at=NOW,
    )
    return OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("exact-slice-inspected"),
            reached=True,
        ),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=QuotaInspectData(
            identity=item.identity,
            evidence=None,
            item=item,
            preference=None,
            usage=None,
            status=status,
            constraint_set=None,
        ),
        identity_evidence=ProviderIdentityEvidence(
            credential_kind=CredentialKind.DIRECT_USER,
            verification=PrincipalVerification.VERIFIED,
            acting_principal=PrincipalIdentity(
                "principal://accounts.google.com/operator@example.com"
            ),
        ),
    )


class ScriptedReadOnlyOperations:
    """Record the exact typed operations invoked by the TUI."""

    def __init__(
        self,
        result: OperationResult[QuotaBrowseData],
        inspect_result: OperationResult[QuotaInspectData] | None = None,
    ) -> None:
        self.result = result
        self.inspect_result = inspect_result or _inspect_result()
        self.browse_calls: list[tuple[ReadOnlyQuotaQuery, dict[str, Any]]] = []
        self.inspect_calls: list[tuple[QuotaInspectSelector, dict[str, Any]]] = []
        self.resolve_calls: list[
            tuple[ComputeInstanceRequirement | CloudTpuSliceRequirement, dict[str, Any]]
        ] = []
        self.obtainability_result = _obtainability_result()
        self.obtainability_calls: list[
            tuple[tuple[ObtainabilityCandidate, ...], dict[str, Any]]
        ] = []
        self.obtainability_all_calls: list[
            tuple[ComputeInstanceRequirement, dict[str, Any]]
        ] = []
        self.obtainability_prepared_calls: list[
            tuple[PreparedObtainabilityComparison, dict[str, Any]]
        ] = []
        self.resolve_result: OperationResult[ResolvedWorkloadRequirement] | None = None
        self.closed = False

    async def browse(  # noqa: PLR0913 - mirrors the production protocol
        self,
        query: ReadOnlyQuotaQuery | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaBrowseData]:
        assert query is not None
        options = {
            "cursor": cursor,
            "limit": limit,
            "deadline": deadline,
            "cancellation": cancellation,
            "scope_input": scope_input,
        }
        self.browse_calls.append((query, options))
        return self.result

    async def inspect(
        self,
        selector: QuotaInspectSelector,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaInspectData]:
        options = {
            "deadline": deadline,
            "cancellation": cancellation,
            "scope_input": scope_input,
        }
        self.inspect_calls.append((selector, options))
        return self.inspect_result

    async def resolve(
        self,
        requirement: ComputeInstanceRequirement | CloudTpuSliceRequirement,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ResolvedWorkloadRequirement]:
        options = {
            "deadline": deadline,
            "cancellation": cancellation,
            "scope_input": scope_input,
        }
        self.resolve_calls.append((requirement, options))
        if self.resolve_result is not None:
            return self.resolve_result
        if isinstance(requirement.locations, CandidateLocations):
            locations = tuple(
                ResolvedWorkloadLocation(
                    location=location,
                    disposition=WorkloadLocationDisposition.INCOMPATIBLE,
                    accelerator_id=None,
                    owning_service=None,
                    management_plane=None,
                    supported_consumers=(),
                    quota_pool=None,
                    deployable_accelerator_quantity=None,
                    constraint_set=None,
                    constraint_requirements=(),
                    coverage=(),
                    failure_reason=ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY,
                )
                for location in requirement.locations.values
            )
            exhaustive = None
        else:
            assert isinstance(requirement.locations, AllCompatibleLocations)
            locations = ()
            exhaustive = True
        return OperationResult(
            operation=OperationName("quota.resolve"),
            resource_scope=SCOPE,
            boundary=OperationBoundary(
                StableSymbol("workload-resolved"),
                reached=True,
            ),
            outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
            completeness=Completeness.complete(),
            started_at=NOW,
            finished_at=NOW,
            data=ResolvedWorkloadRequirement(requirement, locations, exhaustive),
        )

    async def compare_obtainability(
        self,
        candidates: tuple[ObtainabilityCandidate, ...],
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison]:
        self.obtainability_calls.append(
            (
                candidates,
                {
                    "deadline": deadline,
                    "cancellation": cancellation,
                    "scope_input": scope_input,
                },
            )
        )
        return self.obtainability_result

    async def compare_obtainability_all_compatible(  # noqa: PLR0913
        self,
        requirement: ComputeInstanceRequirement,
        *,
        machine: SpotMachineConfiguration,
        distribution_shape: DistributionShape,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison]:
        self.obtainability_all_calls.append(
            (
                requirement,
                {
                    "machine": machine,
                    "distribution_shape": distribution_shape,
                    "deadline": deadline,
                    "cancellation": cancellation,
                    "scope_input": scope_input,
                },
            )
        )
        return self.obtainability_result

    async def compare_obtainability_prepared(
        self,
        prepared_comparison: PreparedObtainabilityComparison,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison]:
        self.obtainability_prepared_calls.append(
            (
                prepared_comparison,
                {
                    "deadline": deadline,
                    "cancellation": cancellation,
                    "scope_input": scope_input,
                },
            )
        )
        return self.obtainability_result

    async def aclose(self) -> None:
        self.closed = True


class ScriptedAuditOperations:
    """Return one local audit page through the typed operation seam."""

    def __init__(self, records: tuple[AuditRecord, ...] = ()) -> None:
        self.records = records
        self.list_calls: list[AuditQuery] = []
        self.inspect_calls: list[str] = []
        self.verify_calls: list[tuple[str | None, str | None]] = []

    async def list(self, query: AuditQuery) -> OperationResult[AuditListData]:
        self.list_calls.append(query)
        return _audit_result(self.records)

    async def inspect(self, record_id: str) -> OperationResult[AuditInspectData]:
        self.inspect_calls.append(record_id)
        record = next(
            (record for record in self.records if record.record_id == record_id),
            None,
        )
        return OperationResult(
            operation=OperationName("audit.inspect"),
            resource_scope=None,
            boundary=OperationBoundary(
                StableSymbol("audit-record-read"),
                reached=record is not None,
            ),
            outcome=Outcome(
                StableSymbol("succeeded" if record is not None else "not-found"),
                (
                    ExitClass.SUCCESS
                    if record is not None
                    else ExitClass.REJECTED_PRECONDITION
                ),
            ),
            completeness=Completeness.complete(),
            started_at=NOW,
            finished_at=NOW,
            data=AuditInspectData(record_id, record),
        )

    async def verify(
        self,
        *,
        from_record_id: str | None = None,
        through_record_id: str | None = None,
    ) -> OperationResult[AuditVerifyData]:
        self.verify_calls.append((from_record_id, through_record_id))
        verification = AuditVerification(
            valid=True,
            verified_from=(self.records[0].record_id if self.records else None),
            verified_through=(self.records[-1].record_id if self.records else None),
        )
        return OperationResult(
            operation=OperationName("audit.verify"),
            resource_scope=None,
            boundary=OperationBoundary(
                StableSymbol("audit-chain-valid"),
                reached=True,
            ),
            outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
            completeness=Completeness.complete(),
            started_at=NOW,
            finished_at=NOW,
            data=AuditVerifyData(
                from_record_id,
                through_record_id,
                verification,
            ),
        )


class DelayedAuditOperations(ScriptedAuditOperations):
    """Delay one local Audit page until the user has left its workspace."""

    def __init__(self, records: tuple[AuditRecord, ...] = ()) -> None:
        super().__init__(records)
        self.list_started = asyncio.Event()
        self.release_list = asyncio.Event()
        self.list_returned = asyncio.Event()

    @override
    async def list(self, query: AuditQuery) -> OperationResult[AuditListData]:
        self.list_calls.append(query)
        self.list_started.set()
        await self.release_list.wait()
        self.list_returned.set()
        return _audit_result(self.records)


class DelayedAuditWorkerOperations(ScriptedAuditOperations):
    """Delay Audit detail workers until the user has left their workspace."""

    def __init__(self, records: tuple[AuditRecord, ...] = ()) -> None:
        super().__init__(records)
        self.inspect_started = asyncio.Event()
        self.release_inspect = asyncio.Event()
        self.inspect_returned = asyncio.Event()
        self.verify_started = asyncio.Event()
        self.release_verify = asyncio.Event()
        self.verify_returned = asyncio.Event()

    @override
    async def inspect(self, record_id: str) -> OperationResult[AuditInspectData]:
        self.inspect_started.set()
        await self.release_inspect.wait()
        result = await super().inspect(record_id)
        self.inspect_returned.set()
        return result

    @override
    async def verify(
        self,
        *,
        from_record_id: str | None = None,
        through_record_id: str | None = None,
    ) -> OperationResult[AuditVerifyData]:
        self.verify_started.set()
        await self.release_verify.wait()
        result = await super().verify(
            from_record_id=from_record_id,
            through_record_id=through_record_id,
        )
        self.verify_returned.set()
        return result


class SupersededReadOnlyOperations(ScriptedReadOnlyOperations):
    """Hold the first read until the TUI cancels it, then complete the refresh."""

    def __init__(self) -> None:
        super().__init__(_browse_result())
        self.first_started = asyncio.Event()
        self.tokens: list[CancellationToken] = []

    @override
    async def browse(
        self,
        query: ReadOnlyQuotaQuery | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaBrowseData]:
        assert query is not None
        options = {
            "cursor": cursor,
            "limit": limit,
            "deadline": deadline,
            "cancellation": cancellation,
            "scope_input": scope_input,
        }
        self.browse_calls.append((query, options))
        token = cancellation
        assert token is not None
        self.tokens.append(token)
        if len(self.browse_calls) == 1:
            self.first_started.set()
            await token.wait()
        return self.result


class FailedReadOnlyOperations(ScriptedReadOnlyOperations):
    """Raise one worker-level failure before an operation result exists."""

    @override
    async def browse(
        self,
        query: ReadOnlyQuotaQuery | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaBrowseData]:
        del query, cursor, limit, deadline, cancellation, scope_input
        msg = "simulated provider worker failure"
        raise RuntimeError(msg)


class BrowseInspectRaceOperations(ScriptedReadOnlyOperations):
    """Delay a refresh past a completed exact-slice inspection."""

    def __init__(
        self,
        refresh_result: OperationResult[QuotaBrowseData] | None = None,
    ) -> None:
        super().__init__(_browse_result())
        self.refresh_result = refresh_result or self.result
        self.refresh_started = asyncio.Event()
        self.release_refresh = asyncio.Event()
        self.refresh_returned = asyncio.Event()

    @override
    async def browse(
        self,
        query: ReadOnlyQuotaQuery | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaBrowseData]:
        if not self.browse_calls:
            return await super().browse(
                query,
                cursor=cursor,
                limit=limit,
                deadline=deadline,
                cancellation=cancellation,
                scope_input=scope_input,
            )
        assert query is not None
        self.browse_calls.append(
            (
                query,
                {
                    "cursor": cursor,
                    "limit": limit,
                    "deadline": deadline,
                    "cancellation": cancellation,
                    "scope_input": scope_input,
                },
            )
        )
        self.refresh_started.set()
        await self.release_refresh.wait()
        self.refresh_returned.set()
        return self.refresh_result


class DelayedQuotaInspectOperations(ScriptedReadOnlyOperations):
    """Delay one quota inspection until the user has left Quotas."""

    def __init__(self, inspect_result: OperationResult[QuotaInspectData]) -> None:
        super().__init__(_browse_result(), inspect_result)
        self.inspect_started = asyncio.Event()
        self.release_inspect = asyncio.Event()
        self.inspect_returned = asyncio.Event()

    @override
    async def inspect(
        self,
        selector: QuotaInspectSelector,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaInspectData]:
        self.inspect_started.set()
        await self.release_inspect.wait()
        result = await super().inspect(
            selector,
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        self.inspect_returned.set()
        return result


class DelayedObtainabilityOperations(ScriptedReadOnlyOperations):
    """Delay one advice comparison until the user has left Obtainability."""

    def __init__(self) -> None:
        super().__init__(_browse_result())
        self.compare_started = asyncio.Event()
        self.release_compare = asyncio.Event()
        self.compare_returned = asyncio.Event()

    @override
    async def compare_obtainability_prepared(
        self,
        prepared_comparison: PreparedObtainabilityComparison,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison]:
        self.compare_started.set()
        await self.release_compare.wait()
        result = await super().compare_obtainability_prepared(
            prepared_comparison,
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        self.compare_returned.set()
        return result


class SecondDelayedObtainabilityOperations(ScriptedReadOnlyOperations):
    """Hold the edited comparison shape for edit-race coverage."""

    def __init__(self) -> None:
        super().__init__(_browse_result())
        self.compare_started = asyncio.Event()
        self.release_compare = asyncio.Event()
        self.compare_returned = asyncio.Event()

    @override
    async def compare_obtainability_prepared(
        self,
        prepared_comparison: PreparedObtainabilityComparison,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[ObtainabilityComparison]:
        delayed = prepared_comparison.candidates[0].vm_count == 3
        if delayed:
            self.compare_started.set()
            await self.release_compare.wait()
        result = await super().compare_obtainability_prepared(
            prepared_comparison,
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if delayed:
            self.compare_returned.set()
        return result


class ScriptedLifecycleOperations:
    """Retain exact lifecycle requests and return scripted typed evidence."""

    def __init__(
        self,
        apply_result: OperationResult[ApplyData],
        *,
        preview_result: OperationResult[PreviewData] | None = None,
        review_result: OperationResult[PlanReviewData] | None = None,
        watch_events: tuple[WatchStreamEvent, ...] = (),
    ) -> None:
        self.apply_result = apply_result
        self.preview_result = preview_result
        self.review_result = review_result
        self.watch_events = watch_events
        self.compose_calls: list[ComposeRequest] = []
        self.preview_calls: list[PreviewRequest] = []
        self.review_calls: list[PlanReviewRequest] = []
        self.apply_calls: list[ApplyRequest] = []
        self.watch_calls: list[WatchRequest] = []

    def compose(self, request: ComposeRequest) -> Any:
        self.compose_calls.append(request)
        return RequestPlanOperations.compose(request)

    def preview(self, request: PreviewRequest) -> OperationResult[PreviewData]:
        self.preview_calls.append(request)
        if self.preview_result is None:
            msg = "Preview was not scripted"
            raise AssertionError(msg)
        return self.preview_result

    def review(self, request: PlanReviewRequest) -> OperationResult[PlanReviewData]:
        self.review_calls.append(request)
        if self.review_result is None:
            msg = "Review was not scripted"
            raise AssertionError(msg)
        return self.review_result

    async def apply(self, request: ApplyRequest) -> OperationResult[ApplyData]:
        self.apply_calls.append(request)
        return self.apply_result

    async def watch(self, request: WatchRequest) -> Any:
        self.watch_calls.append(request)
        for event in self.watch_events:
            yield event


class DelayedApplyLifecycleOperations(ScriptedLifecycleOperations):
    """Hold Apply open so Pilot can exercise consequential navigation."""

    def __init__(self, apply_result: OperationResult[ApplyData]) -> None:
        super().__init__(apply_result)
        self.apply_started = asyncio.Event()
        self.release_apply = asyncio.Event()

    @override
    async def apply(self, request: ApplyRequest) -> OperationResult[ApplyData]:
        self.apply_calls.append(request)
        self.apply_started.set()
        await self.release_apply.wait()
        return self.apply_result


class DelayedPostApplyReads(ScriptedReadOnlyOperations):
    """Hold the first affected-slice refresh open after successful Apply."""

    def __init__(self, result: OperationResult[QuotaBrowseData]) -> None:
        super().__init__(result)
        self.inspect_started = asyncio.Event()
        self.release_inspect = asyncio.Event()

    @override
    async def inspect(
        self,
        selector: QuotaInspectSelector,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[QuotaInspectData]:
        self.inspect_started.set()
        await self.release_inspect.wait()
        return await super().inspect(
            selector,
            deadline=deadline,
            cancellation=cancellation,
            scope_input=scope_input,
        )


def _mutation_slice(quota_id: str) -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        quota_id,
        NormalizedDimensions((("region", "us-central1"),)),
        QuotaScope.REGIONAL,
    )


def _apply_result() -> OperationResult[ApplyData]:
    dispositions = (
        (
            "direct",
            ApplyChildDisposition.ACCEPTED,
            StableSymbol("accepted"),
            None,
        ),
        (
            "unchanged",
            ApplyChildDisposition.FAILED,
            StableSymbol("unchanged"),
            None,
        ),
        (
            "uncertain",
            ApplyChildDisposition.UNKNOWN,
            StableSymbol("transport-unknown"),
            UnknownDispatchResolution.ACCEPTED,
        ),
        (
            "companion",
            ApplyChildDisposition.UNATTEMPTED,
            None,
            None,
        ),
    )
    children = tuple(
        ApplyChildData(
            child_id=child_id,
            disposition=disposition,
            slice_identity=_mutation_slice(f"QUOTA-{index}"),
            target=QuotaQuantity(8, UNIT),
            preference_identity=f"preferences/{child_id}",
            etag=(
                f"etag-{index}"
                if disposition is ApplyChildDisposition.ACCEPTED
                else None
            ),
            provider_outcome=provider_outcome,
            unknown_resolution=resolution,
            trace_id=f"trace-{child_id}",
            audit_record_ids=(f"audit-{child_id}",),
            submitted_at=(
                None if disposition is ApplyChildDisposition.UNATTEMPTED else NOW
            ),
            warnings=(StableSymbol("expert-review-required"),),
            required_acknowledgements=(StableSymbol("decrease-below-usage"),),
            acknowledgements=(StableSymbol("decrease-below-usage"),),
        )
        for index, (
            child_id,
            disposition,
            provider_outcome,
            resolution,
        ) in enumerate(dispositions)
    )
    return OperationResult(
        operation=OperationName("plan.apply"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("all-children-accepted"),
            reached=False,
        ),
        outcome=Outcome(
            StableSymbol("partial-dispatch"),
            ExitClass.STALE_OR_CONFLICTING,
        ),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=ApplyData(
            plan_digest="sha256:" + ("a" * 64),
            kind=PlanKind.BUNDLE,
            intent_id="intent-42",
            children=children,
            audit_record_ids=("audit-apply",),
        ),
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode("dispatch-partial"),
                severity=Severity.WARNING,
                phase=DiagnosticPhase("provider-write"),
                source=DiagnosticSource("cloud-quotas"),
                retry=RetryDisposition.AFTER_REFRESH,
                message=RedactedText("One child requires reconciliation."),
            ),
        ),
        provenance=(
            Provenance(
                StableSymbol("cloud-quotas"),
                NOW,
                StableSymbol("all-dispatched-children"),
                request_identity=RedactedText("intent-42"),
            ),
        ),
    )


def _successful_apply_result() -> OperationResult[ApplyData]:
    child = ApplyChildData(
        child_id="direct",
        disposition=ApplyChildDisposition.ACCEPTED,
        slice_identity=_mutation_slice("QUOTA-DIRECT"),
        target=QuotaQuantity(8, UNIT),
        preference_identity="preferences/direct",
        etag="etag-direct",
        trace_id="trace-direct",
        provider_outcome=StableSymbol("accepted"),
        audit_record_ids=("audit-direct",),
    )
    return OperationResult(
        operation=OperationName("plan.apply"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("all-children-accepted"),
            reached=True,
        ),
        outcome=Outcome(StableSymbol("submitted"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=ApplyData(
            plan_digest="sha256:" + ("a" * 64),
            kind=PlanKind.SINGLE,
            intent_id="intent-success",
            children=(child,),
            audit_record_ids=("audit-apply",),
        ),
    )


def _plan_child(
    child_id: str,
    *,
    direct_rank: int,
    required_acknowledgements: tuple[StableSymbol, ...] = (),
    acknowledgements: tuple[StableSymbol, ...] = (),
) -> QuotaRequestPlanChild:
    return QuotaRequestPlanChild(
        child_id=child_id,
        slice_identity=_mutation_slice(f"QUOTA-{child_id.upper()}"),
        target=QuotaQuantity(8, UNIT),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(2, UNIT),
        workload=QuotaQuantity(4, UNIT),
        prior_desired=QuotaQuantity(6, UNIT),
        granted=QuotaQuantity(5, UNIT),
        preference_name=f"preferences/{child_id}",
        preference_etag=f"etag-{child_id}",
        target_strategy=TargetStrategy.MINIMUM,
        target_derivation=StableSymbol("usage-plus-workload"),
        direct_accelerator_rank=direct_rank,
        scope_breadth_rank=1 + direct_rank,
        warnings=(StableSymbol("remaining-bottleneck"),),
        required_acknowledgements=required_acknowledgements,
        acknowledgements=acknowledgements,
        evidence=(
            EvidenceBinding(
                StableSymbol("effective-quota"),
                "sha256:" + ("e" * 64),
                NOW - timedelta(seconds=30),
            ),
        ),
    )


def _review_result() -> OperationResult[PlanReviewData]:
    children = (
        _plan_child(
            "direct",
            direct_rank=0,
            required_acknowledgements=(
                StableSymbol("decrease-below-usage"),
                StableSymbol("decrease-over-ten-percent"),
            ),
            acknowledgements=(StableSymbol("decrease-below-usage"),),
        ),
        _plan_child("companion", direct_rank=1),
    )
    plan = QuotaRequestBundlePlan(
        resource_scope=SCOPE,
        kind=PlanKind.BUNDLE,
        selected_location="us-central1",
        target_strategy=TargetStrategy.MINIMUM,
        normalized_workload="compute-instance:a4-highgpu-8g:1",
        children=children,
        constraints=tuple(
            ConstraintReference(child.slice_identity) for child in children
        ),
        principal=PlanPrincipal("principal://accounts/42"),
        contact_binding=ContactBinding(
            StableSymbol("direct-user"),
            "principal://accounts/42",
            "hmac-sha256:" + ("c" * 64),
        ),
        installation_id="installation-42",
        issued_at=NOW - timedelta(minutes=30),
        expires_at=NOW - timedelta(minutes=15),
    )
    review = review_plan(
        plan,
        digest="sha256:" + ("a" * 64),
        authenticated=False,
        local_installation_id="installation-42",
        state=PlanLedgerState.CONSUMED,
        now=NOW,
    )
    return OperationResult(
        operation=OperationName("plan.review"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("plan-reviewed"), reached=True),
        outcome=Outcome(StableSymbol("plan-reviewed"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=PlanReviewData(review),
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode("plan-inapplicable"),
                severity=Severity.WARNING,
                phase=DiagnosticPhase("plan-review"),
                source=DiagnosticSource("local-plan"),
                retry=RetryDisposition.AFTER_NEW_PREVIEW,
                message=RedactedText("The retained plan cannot be applied."),
            ),
        ),
        provenance=(
            Provenance(
                StableSymbol("local-plan"),
                NOW,
                StableSymbol("authenticated-bytes"),
                lifecycle_or_preview_status=RedactedText("consumed"),
            ),
        ),
    )


def _preview_plan_result() -> OperationResult[PreviewData]:
    """Build one Apply-capable Preview with complete bound Plan evidence."""
    reviewed = _review_result().data.review
    assert reviewed is not None
    assert isinstance(reviewed.plan, QuotaRequestBundlePlan)
    children = (
        replace(
            reviewed.plan.children[0],
            acknowledgements=reviewed.plan.children[0].required_acknowledgements,
        ),
        reviewed.plan.children[1],
    )
    plan = replace(
        reviewed.plan,
        children=children,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    composition = RequestPlanOperations.compose(
        ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.MINIMUM,
            resource_scope=SCOPE,
            children=tuple(
                ComposeChild(
                    child_id=child.child_id,
                    slice_identity=child.slice_identity,
                    effective=child.effective,
                    usage=child.usage,
                    workload=child.workload,
                    preferred=child.prior_desired,
                    granted=child.granted,
                    preference_name=child.preference_name,
                    preference_etag=child.preference_etag,
                    direct_accelerator_rank=child.direct_accelerator_rank,
                    scope_breadth_rank=child.scope_breadth_rank,
                    warnings=tuple(item.value for item in child.warnings),
                    observed_at=NOW,
                    evidence=child.evidence,
                )
                for child in children
            ),
            selected_location="us-central1",
            acknowledgements=tuple(item.value for item in children[0].acknowledgements),
        )
    )
    assert composition.reached
    return OperationResult(
        operation=OperationName("plan.preview"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("safe-complete-preview"), True),
        outcome=Outcome(StableSymbol("plan-issued"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=PreviewData(
            composition=composition,
            plan=plan,
            plan_digest="sha256:" + ("a" * 64),
            audit_record_id="audit-preview",
            apply_capability=True,
        ),
    )


def _watch_terminal_event(
    *,
    outcome: str = "requested-outcome-unmet",
    exit_class: ExitClass = ExitClass.REQUESTED_OUTCOME_UNMET,
    pending: bool = False,
) -> WatchStreamEvent:
    children = (
        WatchChildIdentity(
            "granted",
            0,
            _mutation_slice("QUOTA-GRANTED"),
            QuotaQuantity(8, UNIT),
            ApplyChildDisposition.ACCEPTED,
            "preferences/granted",
            "etag-granted",
            None,
            baseline=QuotaQuantity(4, UNIT),
        ),
        WatchChildIdentity(
            "partial",
            1,
            _mutation_slice("QUOTA-PARTIAL"),
            QuotaQuantity(8, UNIT),
            ApplyChildDisposition.ACCEPTED,
            "preferences/partial",
            "etag-partial",
            None,
            baseline=QuotaQuantity(4, UNIT),
        ),
        WatchChildIdentity(
            "unattempted",
            2,
            _mutation_slice("QUOTA-UNATTEMPTED"),
            QuotaQuantity(8, UNIT),
            ApplyChildDisposition.UNATTEMPTED,
            "preferences/unattempted",
            None,
            None,
            baseline=QuotaQuantity(4, UNIT),
        ),
    )
    subject = WatchSubject(
        PlanKind.BUNDLE,
        SCOPE,
        WatchCondition.GRANTED,
        "intent-42",
        "sha256:" + ("a" * 64),
        children,
    )

    def status(granted: int) -> QuotaRequestStatus:
        return QuotaRequestStatus.derive(
            reconciliation=(
                Reconciliation.RECONCILING if pending else Reconciliation.SETTLED
            ),
            baseline=QuotaQuantity(4, UNIT),
            desired=QuotaQuantity(8, UNIT),
            granted=None if pending else QuotaQuantity(granted, UNIT),
            effective=None,
            status_observed_at=NOW,
            effective_observed_at=None,
        )

    summaries = (
        WatchChildSummary(children[0], status(8)),
        WatchChildSummary(children[1], status(6)),
        WatchChildSummary(children[2], None),
    )
    aggregate = WatchAggregate.derive(subject, summaries)
    data = WatchResultData(
        subject,
        aggregate,
        "cqmgr.watch-resume/v1:opaque",
        NOW + timedelta(minutes=10),
        60.0,
        NOW,
    )
    result = OperationResult(
        operation=OperationName("request.watch"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("granted"), reached=False),
        outcome=Outcome(StableSymbol(outcome), exit_class),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=data,
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode("watch-terminal-warning"),
                severity=Severity.WARNING,
                phase=DiagnosticPhase("watch"),
                source=DiagnosticSource("local-watch"),
                retry=RetryDisposition.AFTER_REFRESH,
                message=RedactedText("Watch retained its terminal evidence."),
            ),
        ),
        provenance=(
            Provenance(
                StableSymbol("local-watch"),
                NOW,
                StableSymbol("accepted-watch-set"),
                lifecycle_or_preview_status=RedactedText("terminal"),
            ),
        ),
    )
    return WatchStreamEvent(
        "stream-42",
        3,
        WatchEventKind.TERMINAL,
        data.resume,
        NOW,
        subject,
        aggregate,
        result=result,
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode("watch-material-warning"),
                severity=Severity.WARNING,
                phase=DiagnosticPhase("watch"),
                source=DiagnosticSource("local-watch"),
                retry=RetryDisposition.AFTER_REFRESH,
                message=RedactedText("Watch retained a material warning."),
            ),
        ),
    )


def test_wide_shell_opens_federated_quota_inspector_with_semantic_evidence() -> None:
    """The default workspace preserves provider truth and independent predicates."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()

            assert app.layout_mode == "wide"
            assert app.active_workspace == "quotas"
            assert app.last_result is operations.result
            assert len(operations.browse_calls) == 1
            query, options = operations.browse_calls[0]
            assert query == ReadOnlyQuotaQuery()
            assert options["scope_input"].explicit_resource_scope is None
            assert "projects/123456789" in str(_static(app, "#instrument-bar").content)
            assert "principal://accounts.google.com/operator@example.com" in str(
                _static(app, "#instrument-bar").content
            )
            table = _table(app, "#quota-ledger")
            assert table.row_count == 3
            visible = app.interface_snapshot()
            assert "compute.googleapis.com: complete" in visible
            assert "tpu.googleapis.com: complete" in visible
            assert "NEW-PROVIDER-HARDWARE" in visible
            assert "discovered=yes cataloged=no guided=no mutable=no" in visible
            assert "TPU-V6E-CHIPS" in visible
            assert "cataloged=yes guided=no mutable=no" in visible

    asyncio.run(scenario())


def test_single_no_op_preview_preserves_drift_expert_and_contact_placeholders(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single Preview keeps expert evidence and protected contact input explicit."""

    async def scenario() -> None:
        child = ComposeChild(
            child_id="single",
            slice_identity=_mutation_slice("QUOTA-SINGLE"),
            effective=QuotaQuantity(4, UNIT),
            usage=QuotaQuantity(3, UNIT),
            workload=None,
            manual_target=QuotaQuantity(4, UNIT),
            preferred=QuotaQuantity(4, UNIT),
            granted=QuotaQuantity(4, UNIT),
            preference_settled=True,
            direct_accelerator_rank=0,
            scope_breadth_rank=1,
            observed_at=NOW,
            preference_name="preferences/single",
            preference_etag="etag-single",
            warnings=("drift-observed", "expert-override"),
        )
        compose_request = ComposeRequest(
            kind=PlanKind.SINGLE,
            strategy=TargetStrategy.MANUAL,
            resource_scope=SCOPE,
            children=(child,),
            expert=True,
        )
        preview_request = PreviewRequest(
            composition=compose_request,
            principal=PlanPrincipal("principal://accounts/42"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/42",
                "hmac-sha256:" + ("c" * 64),
            ),
            installation_id="installation-42",
            authentication_key=SecretValue(b"k" * 32),
            identity_verified=True,
            contact_verified=True,
            keyring_mutation_capable=True,
            normalized_workload="exact-slice",
            now=NOW,
        )
        composition = RequestPlanOperations.compose(compose_request)
        preview_result = OperationResult(
            operation=OperationName("request.preview"),
            resource_scope=SCOPE,
            boundary=OperationBoundary(
                StableSymbol("safe-complete-preview"),
                reached=True,
            ),
            outcome=Outcome(StableSymbol("verified-no-op"), ExitClass.SUCCESS),
            completeness=Completeness.complete(),
            started_at=NOW,
            finished_at=NOW,
            data=PreviewData(
                composition=composition,
                plan=None,
                plan_digest=None,
                audit_record_id="audit-42",
                apply_capability=False,
            ),
            diagnostics=(
                Diagnostic(
                    code=DiagnosticCode("preview-evidence-retained"),
                    severity=Severity.WARNING,
                    phase=DiagnosticPhase("preview"),
                    source=DiagnosticSource("local-plan"),
                    retry=RetryDisposition.AFTER_REFRESH,
                    message=RedactedText("Preview retained a material warning."),
                ),
            ),
            provenance=(
                Provenance(
                    StableSymbol("local-plan"),
                    NOW,
                    StableSymbol("complete-preflight"),
                    lifecycle_or_preview_status=RedactedText("verified-no-op"),
                ),
            ),
        )
        lifecycle = ScriptedLifecycleOperations(
            _apply_result(),
            preview_result=preview_result,
        )
        app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(_browse_result()),
            ScriptedAuditOperations(),
            lifecycle=lifecycle,  # type: ignore[arg-type]
        )
        command = (
            "cqmgr request preview --resource-scope projects/123456789 "
            "--quota-contact-stdin"
        )
        copied: list[str] = []
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

        async with app.run_test(size=(110, 38)) as pilot:
            await pilot.pause()
            app.open_compose(
                compose_request,
                preview=preview_request,
                copy_cli=command,
            )

            detail = str(_static(app, "#lifecycle-detail").content)
            assert "Single request composition" in detail
            assert "Expert mode: yes" in detail
            assert "drift-observed" in detail
            assert "expert-override" in detail
            assert "Disposition: verified no-op" in detail
            assert str(_static(app, "#lifecycle-copy-cli").content) == command
            assert "--quota-contact-stdin" in command
            assert not _button(app, "#lifecycle-copy").disabled
            instruction = str(_static(app, "#lifecycle-copy-instruction").content)
            assert "provide exactly one protected quota-contact line" in instruction
            assert instruction not in command
            copied_command = app._lifecycle_copy_cli()  # noqa: SLF001
            assert copied_command is not None
            app.copy_to_clipboard(copied_command)
            assert copied == [command]

            app._submit_lifecycle_preview()  # noqa: SLF001 - exercise route dispatch
            await pilot.pause()
            detail = str(_static(app, "#lifecycle-detail").content)
            assert lifecycle.preview_calls == [preview_request]
            assert "Preview outcome: verified-no-op" in detail
            assert "Plan digest: none (verified no-op)" in detail
            assert "Apply capability: no" in detail
            assert "Dimensions: region=us-central1" in detail
            assert "Quota scope: regional" in detail
            assert "Preference: preferences/single" in detail
            assert "Preference ETag: etag-single" in detail
            assert f"Observed at: {NOW.isoformat()}" in detail
            assert "Outcome: verified-no-op" in detail
            assert "Exit class: 0" in detail
            assert "Diagnostic: warning preview-evidence-retained" in detail
            assert "Provenance: local-plan" in detail
            assert "Boundary: safe-complete-preview" in detail
            assert f"Started: {NOW.isoformat()}" in detail
            assert str(_static(app, "#lifecycle-copy-cli").content) == command

    asyncio.run(scenario())


def test_preview_plan_renders_complete_bound_evidence() -> None:
    """Preview exposes the Apply-capable Plan facts needed for safe handoff."""

    async def scenario() -> None:
        preview = _preview_plan_result()
        lifecycle = ScriptedLifecycleOperations(
            _apply_result(),
            preview_result=preview,
        )
        app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(_browse_result()),
            ScriptedAuditOperations(),
            lifecycle=lifecycle,  # type: ignore[arg-type]
        )

        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            result = app.open_preview(
                PreviewRequest(
                    composition=preview.data.composition.request,
                    principal=PlanPrincipal("principal://accounts/42"),
                    contact_binding=ContactBinding(
                        StableSymbol("direct-user"),
                        "principal://accounts/42",
                        "hmac-sha256:" + ("c" * 64),
                    ),
                    installation_id="installation-42",
                    authentication_key=SecretValue(b"k" * 32),
                    identity_verified=True,
                    contact_verified=True,
                    keyring_mutation_capable=True,
                    normalized_workload="compute-instance:a4-highgpu-8g:1",
                    now=NOW,
                )
            )

            assert result is preview
            detail = str(_static(app, "#lifecycle-detail").content)
            assert "Kind: bundle" in detail
            assert "Normalized workload: compute-instance:a4-highgpu-8g:1" in detail
            assert "Principal: principal://accounts/42" in detail
            assert "Issuing installation: installation-42" in detail
            assert f"Issued: {NOW.isoformat()}" in detail
            assert f"Expires: {(NOW + timedelta(minutes=15)).isoformat()}" in detail
            assert "Prior desired: 6 1" in detail
            assert "Granted: 5 1" in detail
            assert "Warnings: remaining-bottleneck" in detail
            assert (
                "Required acknowledgements: decrease-below-usage, "
                "decrease-over-ten-percent"
            ) in detail
            assert (
                "Supplied acknowledgements: decrease-below-usage, "
                "decrease-over-ten-percent"
            ) in detail
            assert "Unresolved acknowledgements: none" in detail
            assert "Evidence effective-quota: sha256:" in detail
            assert "Constraint 1: compute.googleapis.com / QUOTA-DIRECT" in detail
            assert "operator@example.com" not in detail

    asyncio.run(scenario())


def test_bundle_compose_and_non_atomic_apply_keep_scope_and_children_explicit(  # noqa: PLR0915
) -> None:
    """Compose and Apply keep every gate and ordered child outcome visible."""

    async def scenario() -> None:  # noqa: PLR0915
        no_op = ComposeChild(
            child_id="settled",
            slice_identity=_mutation_slice("QUOTA-SETTLED"),
            effective=QuotaQuantity(4, UNIT),
            usage=QuotaQuantity(2, UNIT),
            workload=QuotaQuantity(1, UNIT),
            manual_target=QuotaQuantity(4, UNIT),
            preferred=QuotaQuantity(4, UNIT),
            granted=QuotaQuantity(4, UNIT),
            preference_settled=True,
            direct_accelerator_rank=0,
            scope_breadth_rank=1,
            observed_at=NOW,
        )
        mutation = ComposeChild(
            child_id="direct",
            slice_identity=_mutation_slice("QUOTA-DIRECT"),
            effective=QuotaQuantity(4, UNIT),
            usage=QuotaQuantity(3, UNIT),
            workload=QuotaQuantity(4, UNIT),
            manual_target=QuotaQuantity(2, UNIT),
            direct_accelerator_rank=0,
            scope_breadth_rank=1,
            observed_at=NOW,
            warnings=("remaining-companion-bottleneck",),
        )
        compose_request = ComposeRequest(
            kind=PlanKind.BUNDLE,
            strategy=TargetStrategy.MANUAL,
            resource_scope=SCOPE,
            children=(no_op, mutation),
            selected_location="us-central1",
            expert=True,
            acknowledgements=(
                "decrease-below-usage",
                "decrease-over-ten-percent",
            ),
        )
        result = _apply_result()
        lifecycle = ScriptedLifecycleOperations(result)
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            lifecycle=lifecycle,  # type: ignore[arg-type]
        )

        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app._select_quota(operations.result.data.items[0])  # noqa: SLF001
            await pilot.pause()
            selected_before = app.selected_quota
            assert selected_before is not None
            operations.inspect_calls.clear()
            composition = app.open_compose(compose_request)

            assert composition.reached
            assert app.scope_locked
            detail = str(_static(app, "#lifecycle-detail").content)
            assert "Bundle request composition" in detail
            assert "Target strategy: manual" in detail
            assert "Disposition: verified no-op" in detail
            assert "Disposition: mutation" in detail
            assert "remaining-companion-bottleneck" in detail
            assert (
                "Required acknowledgements: decrease-below-usage, "
                "decrease-over-ten-percent"
            ) in detail
            assert (
                "Supplied acknowledgements: decrease-below-usage, "
                "decrease-over-ten-percent"
            ) in detail
            assert "ordered and non-atomic" in detail

            apply_request = ApplyRequest(
                digest="sha256:" + ("a" * 64),
                authentication_key=SecretValue(b"k" * 32),
                local_installation_id="installation-42",
                resource_scope_acknowledgement=SCOPE,
                principal=PlanPrincipal("principal://accounts/42"),
                contact_binding=ContactBinding(
                    StableSymbol("direct-user"),
                    "principal://accounts/42",
                    "hmac-sha256:" + ("c" * 64),
                ),
                contact_value="operator@example.com",
                now=NOW,
            )
            app.prepare_apply(apply_request)
            acknowledgement = _input(app, "#apply-scope-acknowledgement")
            assert acknowledgement.value == ""
            assert _button(app, "#lifecycle-apply").disabled
            copied_apply = str(_static(app, "#lifecycle-copy-cli").content)
            assert "--acknowledge-resource-scope '<RESOURCE_SCOPE>'" in copied_apply
            assert SCOPE.canonical_name not in copied_apply

            acknowledgement.value = SCOPE.canonical_name
            await pilot.pause()
            assert not _button(app, "#lifecycle-apply").disabled
            await pilot.click("#lifecycle-apply")
            await pilot.pause()

            assert lifecycle.apply_calls == [apply_request]
            detail = str(_static(app, "#lifecycle-detail").content)
            assert "Disposition: accepted" in detail
            assert "Disposition: failed" in detail
            assert "Provider outcome: unchanged" in detail
            assert "Dimensions: region=us-central1" in detail
            assert "ETag: etag-0" in detail
            assert "Trace ID: trace-direct" in detail
            assert "Unknown resolution: accepted" in detail
            unknown_block = detail.split("3. uncertain", maxsplit=1)[1].split(
                "4. companion",
                maxsplit=1,
            )[0]
            assert "Watchable now: yes" in unknown_block
            assert "Audit record IDs: audit-direct" in detail
            assert "Audit record IDs: audit-apply" in detail
            assert "Diagnostic: warning dispatch-partial" in detail
            assert "Provenance: cloud-quotas" in detail
            assert "Disposition: unknown" in detail
            assert "Disposition: unattempted" in detail
            assert "Accepted children remain accepted" in detail
            assert not result.succeeded

            await pilot.click("#lifecycle-back")
            await pilot.pause()
            assert not app.scope_locked
            assert not app.query_one("#quota-workbench").has_class("hidden")
            assert app.current_query == ReadOnlyQuotaQuery()
            assert app.selected_quota is not None
            assert app.selected_quota.selector == selected_before.selector
            assert {
                selector.quota_id for selector, _options in operations.inspect_calls
            } == {
                "QUOTA-0",
                "QUOTA-1",
                "QUOTA-2",
                "QUOTA-3",
            }
            assert "REFRESHED — 4/4 affected slices" in str(
                _static(app, "#status-line").content
            )
            refreshed = str(_static(app, "#quota-detail").content)
            assert "Apply child direct: accepted" in refreshed
            assert "Apply child unchanged: failed" in refreshed
            assert "Apply child uncertain: unknown" in refreshed
            assert "Apply child companion: unattempted" in refreshed
            assert _table(app, "#quota-ledger").has_focus

    asyncio.run(scenario())


def test_partial_apply_returns_with_every_child_outcome() -> None:
    """A stopping Apply returns automatically without flattening child evidence."""

    async def scenario() -> None:
        result = _apply_result()
        lifecycle = ScriptedLifecycleOperations(result)
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            lifecycle=lifecycle,  # type: ignore[arg-type]
        )
        request = ApplyRequest(
            digest=result.data.plan_digest,
            authentication_key=SecretValue(b"k" * 32),
            local_installation_id="installation-42",
            resource_scope_acknowledgement=SCOPE,
            principal=PlanPrincipal("principal://accounts/42"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/42",
                "hmac-sha256:" + ("c" * 64),
            ),
            contact_value="operator@example.com",
            now=NOW,
        )

        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            operations.inspect_calls.clear()
            app.prepare_apply(request)
            _input(app, "#apply-scope-acknowledgement").value = SCOPE.canonical_name
            await pilot.pause()
            await pilot.click("#lifecycle-apply")
            await pilot.pause()
            await pilot.pause()

            assert not result.succeeded
            assert app.query_one("#lifecycle-route").has_class("hidden")
            assert not app.query_one("#quota-workbench").has_class("hidden")
            assert len(operations.inspect_calls) == len(result.data.children)
            detail = str(_static(app, "#quota-detail").content)
            assert "Apply child direct: accepted" in detail
            assert "Apply child unchanged: failed" in detail
            assert "Apply child uncertain: unknown" in detail
            assert "Apply child companion: unattempted" in detail
            assert f"Submitted at: {NOW.isoformat()}" in detail
            assert "Submitted at: none" in detail
            assert "Warnings: expert-review-required" in detail
            assert "Required acknowledgements: decrease-below-usage" in detail
            assert "Supplied acknowledgements: decrease-below-usage" in detail
            assert "Unresolved acknowledgements: none" in detail

    asyncio.run(scenario())


def test_successful_apply_returns_and_reconciles_under_its_bound_scope() -> None:
    """Apply owns navigation through bound-scope affected-slice reconciliation."""

    async def scenario() -> None:
        operations = DelayedPostApplyReads(_browse_result())
        lifecycle = DelayedApplyLifecycleOperations(_successful_apply_result())
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            lifecycle=lifecycle,  # type: ignore[arg-type]
            scope_input=ReadOnlyScopeInput(
                explicit_resource_scope=OTHER_PROJECT_SCOPE,
            ),
        )
        request = ApplyRequest(
            digest="sha256:" + ("a" * 64),
            authentication_key=SecretValue(b"k" * 32),
            local_installation_id="installation-42",
            resource_scope_acknowledgement=SCOPE,
            principal=PlanPrincipal("principal://accounts/42"),
            contact_binding=ContactBinding(
                StableSymbol("direct-user"),
                "principal://accounts/42",
                "hmac-sha256:" + ("c" * 64),
            ),
            contact_value="operator@example.com",
            now=NOW,
        )

        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app.prepare_apply(request)
            _input(app, "#apply-scope-acknowledgement").value = SCOPE.canonical_name
            await pilot.pause()
            await pilot.click("#lifecycle-apply")
            await asyncio.wait_for(lifecycle.apply_started.wait(), timeout=3)

            await pilot.click("#workspace-audit")
            assert app.active_workspace == "quotas"
            assert not app.query_one("#lifecycle-route").has_class("hidden")

            lifecycle.release_apply.set()
            await asyncio.wait_for(operations.inspect_started.wait(), timeout=3)
            assert app.query_one("#lifecycle-route").has_class("hidden")
            assert not app.query_one("#quota-workbench").has_class("hidden")
            assert app.scope_input.explicit_resource_scope == SCOPE

            await pilot.click("#workspace-audit")
            assert app.active_workspace == "quotas"

            operations.release_inspect.set()
            await pilot.pause()
            await pilot.pause()

            assert all(
                options["scope_input"].explicit_resource_scope == SCOPE
                for _selector, options in operations.inspect_calls
            )
            assert (
                operations.browse_calls[-1][1]["scope_input"].explicit_resource_scope
                == SCOPE
            )
            detail = str(_static(app, "#quota-detail").content)
            assert "QUOTA-DIRECT" in detail
            assert "Apply child direct: accepted" in detail
            assert "Provider outcome: accepted" in detail
            assert "Preference: preferences/direct" in detail
            assert "ETag: etag-direct" in detail
            assert "Trace ID: trace-direct" in detail
            assert "REFRESHED — 1/1 affected slices" in str(
                _static(app, "#status-line").content
            )

    asyncio.run(scenario())


def test_plan_review_and_watch_preserve_incapability_and_mixed_grant_evidence(  # noqa: PLR0915
) -> None:
    """Review and Watch retain exact trust reasons and orthogonal child state."""

    async def scenario() -> None:  # noqa: PLR0915
        review_result = _review_result()
        watch_event = _watch_terminal_event()
        lifecycle = ScriptedLifecycleOperations(
            _apply_result(),
            review_result=review_result,
            watch_events=(watch_event,),
        )
        app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(_browse_result()),
            ScriptedAuditOperations(),
            lifecycle=lifecycle,  # type: ignore[arg-type]
        )
        review_request = PlanReviewRequest(
            digest="sha256:" + ("a" * 64),
            path=None,
            authentication_key=None,
            local_installation_id="installation-42",
            now=NOW,
        )

        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app.open_plan_review(review_request)

            assert app.scope_locked
            detail = str(_static(app, "#lifecycle-detail").content)
            assert "Kind: bundle" in detail
            assert "Authenticated: no" in detail
            assert "State: consumed" in detail
            assert "Apply capability: no" in detail
            assert "expired" in detail
            assert "foreign-or-unauthenticated" in detail
            assert "ordered and non-atomic" in detail
            assert "Dimensions: region=us-central1" in detail
            assert "Effective: 4 1" in detail
            assert "Usage: 2 1" in detail
            assert "Workload: 4 1" in detail
            assert "Prior desired: 6 1" in detail
            assert "Granted: 5 1" in detail
            assert "Preference: preferences/direct" in detail
            assert "Preference ETag: etag-direct" in detail
            assert (
                "Required acknowledgements: decrease-below-usage, "
                "decrease-over-ten-percent"
            ) in detail
            assert "Supplied acknowledgements: decrease-below-usage" in detail
            assert "Unresolved acknowledgements: decrease-over-ten-percent" in detail
            assert "Evidence effective-quota" in detail
            assert "Age: 30.0 seconds" in detail
            assert "Diagnostic: warning plan-inapplicable" in detail
            assert "Provenance: local-plan" in detail
            copied_review = str(_static(app, "#lifecycle-copy-cli").content)
            assert copied_review.startswith("cqmgr plan review --plan sha256:")

            request = WatchRequest(
                intent_id="intent-42",
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=SecretValue(b"k" * 32),
                installation_id="installation-42",
                deadline=12345.0,
                cancellation=CancellationToken(),
            )
            watch_copy_cli = (
                "cqmgr request watch --intent-id intent-42 --condition granted "
                "--deadline 2026-07-23T21:00:00+00:00"
            )
            superseded = replace(request, cancellation=CancellationToken())
            app.prepare_watch(superseded, bound_scope=SCOPE)
            app.prepare_watch(
                request,
                bound_scope=SCOPE,
                copy_cli=watch_copy_cli,
            )
            assert superseded.cancellation.cancelled
            assert "Condition: granted" in str(
                _static(app, "#lifecycle-detail").content
            )
            assert str(_static(app, "#lifecycle-copy-cli").content) == watch_copy_cli
            app._submit_lifecycle_watch()  # noqa: SLF001 - exercise worker dispatch
            await pilot.pause()

            assert lifecycle.watch_calls == [request]
            detail = str(_static(app, "#lifecycle-detail").content)
            assert "Aggregate: unmet" in detail
            assert f"Observed at: {NOW.isoformat()}" in detail
            assert "Stream ID: stream-42" in detail
            assert "Slice: compute.googleapis.com / QUOTA-GRANTED" in detail
            assert "Dimensions: region=us-central1" in detail
            assert "Quota scope: regional" in detail
            assert "Target: 8 1" in detail
            assert "Preference: preferences/granted" in detail
            assert "Lineage ETag: etag-granted" in detail
            assert "Lineage trace ID: none" in detail
            assert "Baseline: 4 1" in detail
            assert f"Status observed: {NOW.isoformat()}" in detail
            assert "Apply disposition: accepted" in detail
            assert "Grant satisfaction: full" in detail
            assert "Grant satisfaction: partial" in detail
            assert "Apply disposition: unattempted" in detail
            assert "Lifecycle: not in accepted Watch set" in detail
            assert "Terminal outcome: requested-outcome-unmet" in detail
            assert "Event diagnostic: warning watch-material-warning" in detail
            assert "Diagnostic: warning watch-terminal-warning" in detail
            assert "Provenance: local-watch" in detail
            assert "Last material observation:" in detail
            assert "Resume token: available" in detail
            assert watch_event.aggregate.disposition is WatchDisposition.UNMET
            await pilot.click("#lifecycle-back")
            assert request.cancellation.cancelled

    asyncio.run(scenario())


def test_watch_timeout_interruption_and_resume_remain_explicit() -> None:
    """Terminal observation outcomes preserve pending request state and recovery."""

    async def run_case(
        outcome: str,
        exit_class: ExitClass,
        *,
        resumed: bool,
    ) -> None:
        event = _watch_terminal_event(
            outcome=outcome,
            exit_class=exit_class,
            pending=True,
        )
        lifecycle = ScriptedLifecycleOperations(
            _apply_result(),
            watch_events=(event,),
        )
        app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(_browse_result()),
            ScriptedAuditOperations(),
            lifecycle=lifecycle,  # type: ignore[arg-type]
        )
        request = WatchRequest(
            intent_id=None if resumed else "intent-42",
            condition=None if resumed else WatchCondition.GRANTED,
            resume="cqmgr.watch-resume/v1:previous" if resumed else None,
            authentication_key=SecretValue(b"k" * 32),
            installation_id="installation-42",
            deadline=12345.0,
            cancellation=CancellationToken(),
        )

        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            app.prepare_watch(request, bound_scope=SCOPE)
            prepared = str(_static(app, "#lifecycle-detail").content)
            assert (
                "resume=opaque authenticated token" in prepared
                if resumed
                else "intent=intent-42" in prepared
            )
            app._submit_lifecycle_watch()  # noqa: SLF001 - exercise worker dispatch
            await pilot.pause()

            detail = str(_static(app, "#lifecycle-detail").content)
            assert "Aggregate: pending" in detail
            assert f"Terminal outcome: {outcome}" in detail
            assert "Resume token: available" in detail
            assert lifecycle.watch_calls == [request]

    async def scenario() -> None:
        await run_case("watch-timeout", ExitClass.TIMEOUT, resumed=False)
        await run_case("watch-interrupted", ExitClass.INTERRUPTED, resumed=False)
        await run_case("watch-timeout", ExitClass.TIMEOUT, resumed=True)

    asyncio.run(scenario())


def test_filters_call_the_shared_typed_query_and_show_pruned_coverage() -> None:
    """Service/text controls keep CLI query semantics and source coverage."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 36)) as pilot:
            await pilot.pause()
            operations.result = _browse_result(
                items=(ITEMS[0],),
                tpu_queried=False,
            )

            await pilot.press("/")
            assert _input(app, "#filter-text").has_focus
            _input(app, "#filter-text").value = "H100"
            _input(app, "#filter-service").value = "compute"
            await pilot.click("#apply-filters")
            await pilot.pause()

            query, _ = operations.browse_calls[-1]
            assert query.filters.text == "H100"
            assert query.filters.services == ("compute.googleapis.com",)
            assert _table(app, "#quota-ledger").row_count == 1
            snapshot = app.interface_snapshot()
            assert "tpu.googleapis.com: intentionally-unqueried" in snapshot
            assert "copy-cli=cqmgr quota list" in snapshot
            assert "--service compute.googleapis.com" in snapshot
            assert "--text H100" in snapshot

    asyncio.run(scenario())


def test_workload_routes_decode_both_shapes_and_preserve_canonical_copy_cli() -> None:
    """Compute and Cloud TPU forms invoke the same typed resolver seam as the CLI."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()

            await pilot.click("#resolve-compute")
            _input(app, "#workload-machine-type").value = "n1-standard-16"
            _input(app, "#workload-gpu-type").value = "nvidia-tesla-t4"
            _input(app, "#workload-gpu-count").value = "2"
            _input(app, "#workload-count").value = "2"
            _input(app, "#workload-provisioning").value = "spot"
            _input(app, "#workload-locations").value = "us-central1-a, us-east1-b"
            _button(app, "#workload-submit").press()
            await pilot.pause()

            compute, compute_options = operations.resolve_calls[0]
            assert isinstance(compute, ComputeInstanceRequirement)
            assert compute.machine_type == "n1-standard-16"
            assert compute.instance_count == 2
            assert compute.attached_accelerator_type == "nvidia-tesla-t4"
            assert compute.attached_accelerator_count == 2
            assert isinstance(compute.locations, CandidateLocations)
            assert compute.locations.values == ("us-central1-a", "us-east1-b")
            assert compute_options["scope_input"] == ReadOnlyScopeInput()
            assert app.last_copied_cli is not None
            assert app.last_copied_cli.startswith(
                "cqmgr quota resolve compute-instance "
            )
            assert "--attached-accelerator-type nvidia-tesla-t4" in (
                app.last_copied_cli
            )
            assert "--attached-accelerator-count 2" in app.last_copied_cli
            assert "Location: us-central1-a" in str(
                _static(app, "#quota-detail").content
            )
            assert "Disposition: incompatible" in str(
                _static(app, "#quota-detail").content
            )
            assert "Reason: unsupported-compatibility" in str(
                _static(app, "#quota-detail").content
            )

            await pilot.click("#resolve-tpu")
            _input(app, "#workload-accelerator-type").value = "v6e-8"
            _input(app, "#workload-topology").value = "2x4"
            _input(app, "#workload-runtime-version").value = "tpu-vm-base"
            _input(app, "#workload-count").value = "3"
            _input(app, "#workload-provisioning").value = "standard"
            _input(app, "#workload-locations").value = "all"
            _button(app, "#workload-submit").press()
            await pilot.pause()

            cloud_tpu, _ = operations.resolve_calls[1]
            assert isinstance(cloud_tpu, CloudTpuSliceRequirement)
            assert cloud_tpu.accelerator_type == "v6e-8"
            assert cloud_tpu.topology == "2x4"
            assert cloud_tpu.slice_count == 3
            assert isinstance(cloud_tpu.locations, AllCompatibleLocations)
            assert app.last_copied_cli is not None
            assert app.last_copied_cli.startswith(
                "cqmgr quota resolve cloud-tpu-slice "
            )
            assert "--all-compatible-locations" in app.last_copied_cli

    asyncio.run(scenario())


def test_obtainability_standalone_explicit_candidates_share_typed_cli_semantics() -> (
    None
):
    """Compare only explicit standalone candidates and preserve evidence."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "balanced"
            _input(
                app, "#obtainability-candidates"
            ).value = "us-central1=us-central1-a,us-central1-b"
            _button(app, "#obtainability-compare").press()
            await pilot.pause()

            assert len(operations.obtainability_prepared_calls) == 1
            prepared, options = operations.obtainability_prepared_calls[0]
            candidates = prepared.candidates
            assert len(candidates) == 1
            assert candidates[0].endpoint_region == "us-central1"
            assert candidates[0].zones == ("us-central1-a", "us-central1-b")
            assert candidates[0].machine.machine_type == "a3-highgpu-8g"
            assert candidates[0].vm_count == 2
            assert candidates[0].distribution_shape is DistributionShape.BALANCED
            assert options["scope_input"] == ReadOnlyScopeInput()
            assert operations.obtainability_all_calls == []

            detail = str(_static(app, "#obtainability-detail").content)
            assert "Candidate identity: sha256:" in detail
            assert "Ranked candidates: 1" in detail
            assert "Unranked candidates: 0" in detail
            assert "Obtainability score: 0.8 (high)" in detail
            assert "Recommended shard: us-central1-a" in detail
            assert "Rank: 1" in detail
            assert "30-day p90 preemption: 0.12" in detail
            assert "Preemption interval:" in detail
            assert "P90 derivation: nearest-rank 27 of 30 = 0.12" in detail
            assert "Total-request hourly price: USD 6.50" in detail
            assert "Price interval:" in detail
            assert "Price derivation: USD 3.25 x 2 VMs = USD 6.50 per hour" in detail
            assert "Capacity guarantee: no" in detail
            assert app.last_copied_cli is not None
            assert app.last_copied_cli.startswith("cqmgr obtainability compare ")
            assert (
                "--candidate us-central1=us-central1-a,us-central1-b"
                in app.last_copied_cli
            )
            assert "--all-compatible-locations" not in app.last_copied_cli

    asyncio.run(scenario())


def test_copy_cli_is_scoped_to_each_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling workspace can never copy another workspace's command."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
        copied: list[str] = []
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            quota_command = app.last_copied_cli
            assert quota_command is not None
            assert quota_command.startswith("cqmgr quota list ")

            await pilot.click("#workspace-obtainability")
            await pilot.pause()
            assert app.last_copied_cli is None
            assert "unavailable" in str(_static(app, "#obtainability-copy-cli").content)
            await pilot.click("#obtainability-copy")
            assert copied == []

            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "any-single-zone"
            _input(app, "#obtainability-candidates").value = "us-central1=us-central1-a"
            await pilot.click("#obtainability-compare")
            await pilot.pause()
            obtainability_command = app.last_copied_cli
            assert obtainability_command is not None
            assert obtainability_command.startswith("cqmgr obtainability compare ")

            await pilot.click("#workspace-quotas")
            await pilot.pause()
            assert app.last_copied_cli == quota_command
            await pilot.click("#copy-cli")
            assert copied == [quota_command]

    asyncio.run(scenario())


def test_fully_specified_obtainability_exposes_copy_cli_before_provider_result() -> (
    None
):
    """A safe equivalent command does not wait for provider advice."""

    async def scenario() -> None:
        operations = DelayedObtainabilityOperations()
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "n1-standard-16"
            _input(app, "#obtainability-gpu-type").value = "nvidia-tesla-t4"
            _input(app, "#obtainability-gpu-count").value = "2"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "any-single-zone"
            _input(app, "#obtainability-candidates").value = "us-central1=us-central1-a"
            await pilot.pause()

            command = app.last_copied_cli
            assert command is not None
            assert command.startswith("cqmgr obtainability compare ")
            assert "--resource-scope projects/123456789" in command
            assert "--gpu-type nvidia-tesla-t4" in command
            assert "--candidate us-central1=us-central1-a" in command
            assert app.last_result is None

            _button(app, "#obtainability-compare").press()
            await asyncio.wait_for(operations.compare_started.wait(), timeout=3)
            requirement, _options = operations.resolve_calls[-1]
            assert isinstance(requirement, ComputeInstanceRequirement)
            assert requirement.attached_accelerator_type == "nvidia-tesla-t4"
            assert requirement.attached_accelerator_count == 2
            assert app.last_copied_cli == command

            operations.release_compare.set()
            await asyncio.wait_for(operations.compare_returned.wait(), timeout=3)
            await pilot.pause()

    asyncio.run(scenario())


def test_obtainability_edit_clears_stale_state_and_supersedes_active_result() -> None:
    """An input edit synchronously owns the workspace before workers return."""

    async def scenario() -> None:
        operations = SecondDelayedObtainabilityOperations()
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "any-single-zone"
            _input(app, "#obtainability-candidates").value = "us-central1=us-central1-a"
            await pilot.click("#obtainability-compare")
            await pilot.pause()

            assert app.last_result is operations.obtainability_result
            initial_command = app.last_copied_cli
            assert initial_command is not None

            _input(app, "#obtainability-vm-count").value = "3"
            await pilot.pause()
            assert app.last_result is None
            assert app.last_copied_cli is not None
            assert app.last_copied_cli != initial_command
            assert "--vm-count 3" in app.last_copied_cli
            _button(app, "#obtainability-compare").press()
            await asyncio.wait_for(operations.compare_started.wait(), timeout=3)

            _input(app, "#obtainability-vm-count").value = "4"
            await pilot.pause()
            operations.release_compare.set()
            await asyncio.wait_for(operations.compare_returned.wait(), timeout=3)
            await pilot.pause()

            assert app.last_result is None
            assert app.last_copied_cli is not None
            assert "--vm-count 4" in app.last_copied_cli
            assert "Complete the fixed request" in str(
                _static(app, "#obtainability-detail").content
            )
            assert "INPUT CHANGED" in str(_static(app, "#status-line").content)

    asyncio.run(scenario())


def test_contextual_obtainability_requires_confirmation_of_inherited_fields(  # noqa: PLR0915
) -> None:
    """A resolved Spot shape stays visible and cannot compare before confirmation."""

    async def scenario() -> None:  # noqa: PLR0915
        requirement = ComputeInstanceRequirement(
            "n1-standard-16",
            2,
            ProvisioningModel.SPOT,
            CandidateLocations(("us-central1-a",)),
            attached_accelerator_type="nvidia-tesla-t4",
            attached_accelerator_count=2,
        )
        operations = ScriptedReadOnlyOperations(_browse_result())
        operations.resolve_result = _resolved_compute_result(
            requirement,
            locations=("us-central1-a",),
        )
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.click("#resolve-compute")
            _input(app, "#workload-machine-type").value = "n1-standard-16"
            _input(app, "#workload-gpu-type").value = "nvidia-tesla-t4"
            _input(app, "#workload-gpu-count").value = "2"
            _input(app, "#workload-count").value = "2"
            _input(app, "#workload-provisioning").value = "spot"
            _input(app, "#workload-locations").value = "us-central1-a"
            _button(app, "#workload-submit").press()
            await pilot.pause()

            _button(app, "#workload-obtainability").press()
            await pilot.pause()
            assert app.active_workspace == "obtainability"
            assert _input(app, "#obtainability-machine-type").value == "n1-standard-16"
            assert _input(app, "#obtainability-gpu-type").value == "nvidia-tesla-t4"
            assert _input(app, "#obtainability-gpu-count").value == "2"
            assert _input(app, "#obtainability-vm-count").value == "2"
            assert (
                _input(app, "#obtainability-candidates").value
                == "us-central1=us-central1-a"
            )
            assert "Inherited from Quotas / Resolve / Compute instance" in str(
                _static(app, "#obtainability-breadcrumb").content
            )
            assert len(operations.resolve_calls) == 1

            await pilot.click("#workspace-obtainability")
            await pilot.press("o")
            assert "Inherited from Quotas / Resolve / Compute instance" in str(
                _static(app, "#obtainability-breadcrumb").content
            )

            await pilot.click("#obtainability-compare")
            assert operations.obtainability_calls == []
            assert "CONFIRM INHERITED FIELDS" in str(
                _static(app, "#status-line").content
            )

            _button(app, "#obtainability-confirm").press()
            await pilot.pause()
            assert app.last_copied_cli is not None
            assert "--candidate us-central1=us-central1-a" in app.last_copied_cli
            assert "--gpu-type nvidia-tesla-t4" in app.last_copied_cli
            assert "--gpu-count 2" in app.last_copied_cli
            assert "Inherited from Quotas / Resolve / Compute instance" in str(
                _static(app, "#obtainability-breadcrumb").content
            )
            _button(app, "#obtainability-compare").press()
            await pilot.pause()

            assert len(operations.obtainability_prepared_calls) == 1
            prepared, _options = operations.obtainability_prepared_calls[0]
            assert len(operations.resolve_calls) == 1
            assert prepared.resolver_provenance is operations.resolve_result.data
            candidates = prepared.candidates
            assert candidates[0].zones == ("us-central1-a",)
            assert candidates[0].machine.gpu == GpuAttachment(
                "nvidia-tesla-t4",
                2,
            )
            assert "Return context: Quotas / Resolve / Compute instance" in str(
                _static(app, "#obtainability-detail").content
            )

            _input(app, "#obtainability-candidates").value = "us-central1=us-central1-b"
            await pilot.pause()
            _button(app, "#obtainability-compare").press()
            await pilot.pause()
            assert len(operations.obtainability_prepared_calls) == 1
            assert (
                "confirm inherited fields"
                in str(_static(app, "#status-line").content).casefold()
            )

            _button(app, "#obtainability-return").press()
            await pilot.pause()
            assert app.active_workspace == "quotas"
            assert "Workload resolution" in str(_static(app, "#quota-detail").content)

    asyncio.run(scenario())


def test_all_compatible_obtainability_expansion_is_visible_and_explicit() -> (  # noqa: PLR0915
    None
):
    """All-compatible mode requires confirmation and retains resolver provenance."""

    async def scenario() -> None:  # noqa: PLR0915
        requirement = ComputeInstanceRequirement(
            "a3-highgpu-8g",
            2,
            ProvisioningModel.SPOT,
            AllCompatibleLocations(),
        )
        resolved = _resolved_compute_result(
            requirement,
            locations=("us-central1-a", "us-east1-b"),
        ).data
        assert isinstance(resolved, ResolvedWorkloadRequirement)
        operations = ScriptedReadOnlyOperations(_browse_result())
        comparison = operations.obtainability_result.data
        assert isinstance(comparison, ObtainabilityComparison)
        operations.obtainability_result = replace(
            operations.obtainability_result,
            data=replace(comparison, resolver_provenance=resolved),
        )
        operations.resolve_result = _resolved_compute_result(
            requirement,
            locations=("us-central1-a", "us-east1-b"),
        )
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "balanced"

            _button(app, "#obtainability-compare-all").press()
            await pilot.pause()
            assert operations.obtainability_all_calls == []
            assert len(operations.resolve_calls) == 1
            assert "no comparison has started" in str(
                _static(app, "#obtainability-expansion").content
            ).casefold() or "us-central1-a" in str(
                _static(app, "#obtainability-expansion").content
            )
            assert "us-central1-a" in str(
                _static(app, "#obtainability-expansion").content
            )
            assert app.last_copied_cli is not None
            assert "--all-compatible-locations" in app.last_copied_cli
            assert "--candidate" not in app.last_copied_cli

            _button(app, "#obtainability-confirm").press()
            _button(app, "#obtainability-compare-all").press()
            await pilot.pause()

            assert len(operations.obtainability_prepared_calls) == 1
            prepared, _options = operations.obtainability_prepared_calls[0]
            assert isinstance(
                prepared.resolver_provenance.requirement.locations,
                AllCompatibleLocations,
            )
            assert all(
                candidate.distribution_shape is DistributionShape.BALANCED
                for candidate in prepared.candidates
            )
            assert len(operations.resolve_calls) == 1
            expansion = str(_static(app, "#obtainability-expansion").content)
            assert "us-central1-a" in expansion
            assert "us-east1-b" in expansion
            assert app.last_copied_cli is not None
            assert "--all-compatible-locations" in app.last_copied_cli
            assert "--candidate" not in app.last_copied_cli

            _input(app, "#obtainability-candidates").value = "us-west1=us-west1-a"
            await pilot.pause()
            assert app.last_result is None
            assert app.last_copied_cli is not None
            assert "--all-compatible-locations" in app.last_copied_cli
            assert "--candidate" not in app.last_copied_cli

            _input(app, "#obtainability-vm-count").value = "3"
            await pilot.pause()
            assert app.last_copied_cli is not None
            assert "--all-compatible-locations" in app.last_copied_cli
            assert "--vm-count 3" in app.last_copied_cli
            _button(app, "#obtainability-compare-all").press()
            await pilot.pause()
            assert len(operations.resolve_calls) == 2
            assert len(operations.obtainability_prepared_calls) == 1
            assert (
                "confirm candidate expansion"
                in str(_static(app, "#status-line").content).casefold()
            )

    asyncio.run(scenario())


def test_obtainability_snapshot_preserves_incomplete_evidence_ties_and_n1_limits() -> (
    None
):
    """Semantic snapshots expose partial ranking without implying capacity."""

    async def scenario(size: tuple[int, int], expected_layout: str) -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        operations.obtainability_result = _incomplete_obtainability_result()
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            no_color=True,
        )

        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "balanced"
            _input(
                app, "#obtainability-candidates"
            ).value = (
                "us-central1=us-central1-a,us-central1-b us-east1=us-east1-b,us-east1-c"
            )
            _button(app, "#obtainability-compare").press()
            await pilot.pause()

            snapshot = app.interface_snapshot()
            assert f"layout={expected_layout}" in snapshot
            assert "workspace=obtainability" in snapshot
            assert "obtainability-breadcrumb=Obtainability / Standalone" in snapshot
            assert (
                "obtainability-expansion=Candidate expansion: explicit candidates only."
                in snapshot
            )
            assert "Evidence gap: capacity-history / candidate-history-incomplete" in (
                snapshot
            )
            assert (
                "WARNING capacity-history-incomplete: One candidate history source "
                "is incomplete; ranking is partial."
            ) in snapshot
            assert (
                "Exact rank-component tie: yes; canonical candidate identity "
                "breaks the tie"
            ) in snapshot
            assert "Ranked candidates: 2" in snapshot
            assert "Unranked candidates: 1" in snapshot
            assert "Unranked reasons: history-unsupported-n1-attached-gpu" in snapshot
            assert (
                "Coverage reasons: capacityHistory does not support N1 attached GPUs"
                in snapshot
            )
            assert "Capacity guarantee: no" in snapshot
            assert "\x1b" not in snapshot

    async def all_sizes() -> None:
        await scenario((140, 42), "wide")
        await scenario((100, 36), "medium")
        await scenario((72, 28), "narrow")

    asyncio.run(all_sizes())


def test_obtainability_snapshot_retains_complete_resolver_provenance() -> None:
    """Every resolver disposition and expansion limit remains visible without color."""

    async def scenario(size: tuple[int, int], expected_layout: str) -> None:
        requirement = ComputeInstanceRequirement(
            "a3-highgpu-8g",
            2,
            ProvisioningModel.SPOT,
            AllCompatibleLocations(),
        )
        rejected = tuple(
            ResolvedWorkloadLocation(
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
                coverage=(),
                failure_reason=reason,
            )
            for location, disposition, reason in (
                (
                    "us-east1-b",
                    WorkloadLocationDisposition.INCOMPATIBLE,
                    ResolutionFailureReason.UNSUPPORTED_COMPATIBILITY,
                ),
                (
                    "us-west1-a",
                    WorkloadLocationDisposition.INCOMPLETE,
                    ResolutionFailureReason.MISSING_LOCATION_EVIDENCE,
                ),
            )
        )
        resolved = ResolvedWorkloadRequirement(
            requirement,
            (_compatible_compute_location("us-central1-a"), *rejected),
            all_compatible_locations_exhaustive=False,
        )
        operations = ScriptedReadOnlyOperations(_browse_result())
        comparison = operations.obtainability_result.data
        assert isinstance(comparison, ObtainabilityComparison)
        operations.obtainability_result = replace(
            operations.obtainability_result,
            data=replace(comparison, resolver_provenance=resolved),
        )
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            no_color=True,
        )

        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "balanced"
            _input(
                app, "#obtainability-candidates"
            ).value = "us-central1=us-central1-a,us-central1-b"
            _button(app, "#obtainability-compare").press()
            await pilot.pause()

            snapshot = app.interface_snapshot()
            assert f"layout={expected_layout}" in snapshot
            assert "All-compatible locations exhaustive: false" in snapshot
            assert "us-central1-a: compatible" in snapshot
            assert "us-east1-b: incompatible (unsupported-compatibility)" in snapshot
            assert "us-west1-a: incomplete (missing-location-evidence)" in snapshot
            assert "\x1b" not in snapshot

    async def all_sizes() -> None:
        await scenario((140, 42), "wide")
        await scenario((100, 36), "medium")
        await scenario((72, 28), "narrow")

    asyncio.run(all_sizes())


def test_deferred_workload_worker_cannot_reclaim_ownership_after_leaving_quotas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued resolver cannot claim provider ownership after workspace departure."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            scheduled: list[Any] = []

            def defer_worker(work: Any, **options: Any) -> None:
                del options
                scheduled.append(work)

            monkeypatch.setattr(app, "run_worker", defer_worker)
            await pilot.click("#resolve-compute")
            _input(app, "#workload-machine-type").value = "a3-highgpu-8g"
            _input(app, "#workload-count").value = "1"
            _input(app, "#workload-provisioning").value = "spot"
            _input(app, "#workload-locations").value = "us-central1-a"
            _button(app, "#workload-submit").press()
            await pilot.pause()
            assert len(scheduled) == 1

            await pilot.click("#workspace-obtainability")
            await pilot.pause()
            quota_result = app.last_result
            quota_status = str(_static(app, "#status-line").content)
            quota_instrument = str(_static(app, "#instrument-bar").content)
            quota_detail = str(_static(app, "#quota-detail").content)
            quota_copy_cli = app.last_copied_cli

            await scheduled.pop()
            await pilot.pause()

            assert len(operations.resolve_calls) == 1
            assert app.active_workspace == "obtainability"
            assert app.last_result is quota_result
            assert str(_static(app, "#status-line").content) == quota_status
            assert str(_static(app, "#instrument-bar").content) == quota_instrument
            assert str(_static(app, "#quota-detail").content) == quota_detail
            assert app.last_copied_cli == quota_copy_cli

    asyncio.run(scenario())


def test_delayed_quota_selection_does_not_dispatch_after_workspace_departure() -> None:
    """A queued quota-row message is ignored outside the Quotas workspace."""

    async def run_case(workspace: str) -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            table = _table(app, "#quota-ledger")
            event = DataTable.RowSelected(table, 0, next(iter(table.rows)))

            await pilot.click(f"#workspace-{workspace}")
            await pilot.pause()
            workspace_result = app.last_result

            app.on_data_table_row_selected(event)
            await pilot.pause()

            assert app.active_workspace == workspace
            assert operations.inspect_calls == []
            assert app.last_result is workspace_result

    async def scenario() -> None:
        await run_case("audit")
        await run_case("obtainability")

    asyncio.run(scenario())


def test_delayed_quota_buttons_do_not_dispatch_after_workspace_departure() -> None:
    """Queued filter and workload actions are ignored outside Quotas."""

    async def filter_case() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            _input(app, "#filter-text").value = "GPU"
            event = Button.Pressed(_button(app, "#apply-filters"))
            await pilot.click("#workspace-obtainability")
            await pilot.pause()

            app.on_button_pressed(event)
            await pilot.pause()

            assert len(operations.browse_calls) == 1
            assert app.current_query == ReadOnlyQuotaQuery()

    async def workload_case() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())

        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.click("#resolve-compute")
            _input(app, "#workload-machine-type").value = "a3-highgpu-8g"
            _input(app, "#workload-count").value = "1"
            _input(app, "#workload-provisioning").value = "spot"
            _input(app, "#workload-locations").value = "us-central1-a"
            event = Button.Pressed(_button(app, "#workload-submit"))
            await pilot.click("#workspace-obtainability")
            await pilot.pause()

            app.on_button_pressed(event)
            await pilot.pause()

            assert operations.resolve_calls == []

    async def scenario() -> None:
        await filter_case()
        await workload_case()

    asyncio.run(scenario())


def test_narrow_inspection_preserves_selection_return_focus_and_status_axes() -> None:
    """One-pane detail keeps exact context and Escape restores the quota ledger."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            scope_locked=True,
            no_color=True,
        )

        async with app.run_test(size=(72, 28)) as pilot:
            await pilot.pause()
            assert app.layout_mode == "narrow"
            table = _table(app, "#quota-ledger")
            assert table.has_focus

            await pilot.press("enter")
            await pilot.pause()

            assert len(operations.inspect_calls) == 1
            selector, options = operations.inspect_calls[0]
            assert selector.service == "compute.googleapis.com"
            assert selector.quota_id == "GPUS-ALL-REGIONS-per-project"
            assert options["scope_input"] == ReadOnlyScopeInput()
            assert app.has_class("detail-route")
            detail = str(_static(app, "#quota-detail").content)
            assert "Reconciliation: settled" in detail
            assert "Grant satisfaction: full" in detail
            assert "Effective confirmation: confirmed" in detail
            assert "LOCKED" in str(_static(app, "#instrument-bar").content)
            assert app.last_result is operations.inspect_result
            assert app.last_copied_cli is not None
            assert app.last_copied_cli.startswith("cqmgr quota inspect ")
            assert " q " not in app.last_copied_cli
            assert "\x1b" not in app.interface_snapshot()

            await pilot.press("escape")
            await pilot.pause()
            assert not app.has_class("detail-route")
            assert table.has_focus
            assert app.selected_quota is not None
            assert app.selected_quota.item is ITEMS[0]

            await pilot.resize_terminal(100, 32)
            await pilot.pause()
            assert app.layout_mode == "medium"
            assert app.selected_quota.item is ITEMS[0]

    asyncio.run(scenario())


def test_explicit_no_color_activates_textual_filter_without_leaking_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit no-color uses Textual's renderer and restores the environment."""
    monkeypatch.delenv("NO_COLOR", raising=False)

    app = CloudQuotaManagerApp(
        ScriptedReadOnlyOperations(_browse_result()),
        ScriptedAuditOperations(),
        no_color=True,
    )

    assert app.no_color is True
    assert any(
        isinstance(line_filter, NoColor | Monochrome)
        for line_filter in app.get_line_filters()
    )
    assert "NO_COLOR" not in os.environ


def test_audit_workspace_lists_inspects_and_verifies_local_evidence() -> None:
    """Audit navigation stays local and presents exact append-only chain facts."""

    async def scenario() -> None:
        read_only = ScriptedReadOnlyOperations(_browse_result())
        audit = ScriptedAuditOperations((AUDIT_RECORD,))
        app = CloudQuotaManagerApp(read_only, audit)

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-audit")
            await pilot.pause()

            assert app.active_workspace == "audit"
            assert audit.list_calls == [AuditQuery()]
            table = _table(app, "#audit-table")
            assert table.row_count == 1
            table.focus()
            await pilot.press("enter")
            await pilot.pause()

            assert audit.inspect_calls == [AUDIT_RECORD.record_id]
            detail = str(_static(app, "#audit-detail").content)
            assert f"Record ID: {AUDIT_RECORD.record_id}" in detail
            assert "Kind: preview-evidence" in detail
            assert f"Previous hash: {AUDIT_GENESIS_HASH}" in detail

            _button(app, "#audit-verify").press()
            await pilot.pause()
            assert audit.verify_calls == [(None, None)]
            detail = str(_static(app, "#audit-detail").content)
            assert "Chain valid: yes" in detail
            assert f"Verified through: {AUDIT_RECORD.record_id}" in detail

            await pilot.click("#workspace-quotas")
            await pilot.pause()
            assert app.active_workspace == "quotas"
            assert _table(app, "#quota-ledger").row_count == 3

    asyncio.run(scenario())


def test_partial_failure_and_superseded_reads_remain_explicit_and_safe() -> None:
    """Incomplete evidence, worker failure, and cancellation retain honest meaning."""

    async def partial_scenario() -> None:
        partial = _browse_result(
            complete=False,
            diagnostics=(PARTIAL_DIAGNOSTIC,),
        )
        app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(partial),
            ScriptedAuditOperations(),
        )
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            assert app.last_result is partial
            assert _table(app, "#quota-ledger").row_count == 3
            coverage = str(_static(app, "#coverage-summary").content)
            assert "tpu.googleapis.com: incomplete" in coverage
            assert "Aggregate completeness: incomplete" in coverage
            assert "tpu-location-page-failed" in coverage
            assert "Cloud TPU location evidence is incomplete" in coverage
            assert "PROVIDER-SOURCE-INCOMPLETE" in str(
                _static(app, "#status-line").content
            )

    async def failure_scenario() -> None:
        typed_failure = _failure_result()
        typed_app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(typed_failure),  # type: ignore[arg-type]
            ScriptedAuditOperations(),
        )
        async with typed_app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            assert typed_app.last_result is typed_failure
            detail = str(_static(typed_app, "#quota-detail").content)
            assert "Reason: provider-read-failed" in detail
            assert "provider-read-failed" in detail
            assert "retry after backoff" in detail

        app = CloudQuotaManagerApp(
            FailedReadOnlyOperations(_browse_result()),
            ScriptedAuditOperations(),
        )
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            assert app.last_result is None
            assert _table(app, "#quota-ledger").row_count == 0
            assert "ERROR" in str(_static(app, "#status-line").content)
            assert (
                "provider mutation"
                not in str(_static(app, "#status-line").content).casefold()
            )

    async def cancellation_scenario() -> None:
        operations = SupersededReadOnlyOperations()
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
        async with app.run_test(size=(90, 30)) as pilot:
            await operations.first_started.wait()
            app.action_refresh()
            await pilot.pause()
            assert len(operations.browse_calls) == 2
            assert operations.tokens[0].cancelled
            assert app.last_result is operations.result
            assert _table(app, "#quota-ledger").row_count == 3
            assert "COMPLETE" in str(_static(app, "#status-line").content)

    asyncio.run(partial_scenario())
    asyncio.run(failure_scenario())
    asyncio.run(cancellation_scenario())


def test_completed_inspection_owns_result_and_copy_cli_over_older_refresh() -> None:
    """An older browse cannot replace a newer exact-slice detail operation."""

    async def scenario() -> None:
        operations = BrowseInspectRaceOperations()
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_refresh()
            await operations.refresh_started.wait()

            _table(app, "#quota-ledger").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.last_result is operations.inspect_result
            assert app.last_copied_cli is not None
            assert app.last_copied_cli.startswith("cqmgr quota inspect ")
            assert "Operation: quota.inspect" in str(
                _static(app, "#quota-detail").content
            )

            operations.release_refresh.set()
            await operations.refresh_returned.wait()
            await pilot.pause()

            assert app.last_result is operations.inspect_result
            assert app.last_copied_cli is not None
            assert app.last_copied_cli.startswith("cqmgr quota inspect ")
            assert "Operation: quota.inspect" in str(
                _static(app, "#quota-detail").content
            )

    asyncio.run(scenario())


def test_audit_workspace_owns_state_over_older_quota_refresh() -> None:
    """An older provider read cannot replace the active local Audit operation."""

    async def scenario() -> None:
        operations = BrowseInspectRaceOperations()
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_refresh()
            await operations.refresh_started.wait()

            await pilot.click("#workspace-audit")
            await pilot.pause()
            audit_result = app.last_result
            assert audit_result is not None
            assert audit_result.operation == OperationName("audit.list")
            audit_status = str(_static(app, "#status-line").content)
            audit_instrument = str(_static(app, "#instrument-bar").content)
            audit_copy_cli = app.last_copied_cli

            operations.release_refresh.set()
            await operations.refresh_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "audit"
            assert app.last_result is audit_result
            assert str(_static(app, "#status-line").content) == audit_status
            assert str(_static(app, "#instrument-bar").content) == audit_instrument
            assert app.last_copied_cli == audit_copy_cli

    asyncio.run(scenario())


def test_obtainability_workspace_owns_state_over_older_quota_refresh() -> None:
    """An older quota refresh cannot replace the active Obtainability state."""

    async def scenario() -> None:
        refresh_result = replace(
            _browse_result(complete=False),
            resource_scope=ALT_SCOPE,
            identity_evidence=None,
        )
        operations = BrowseInspectRaceOperations(refresh_result)
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_refresh()
            await operations.refresh_started.wait()

            await pilot.click("#workspace-obtainability")
            await pilot.pause()
            quota_result = app.last_result
            quota_status = str(_static(app, "#status-line").content)
            quota_instrument = str(_static(app, "#instrument-bar").content)
            quota_copy_cli = app.last_copied_cli

            operations.release_refresh.set()
            await operations.refresh_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "obtainability"
            assert app.last_result is quota_result
            assert str(_static(app, "#status-line").content) == quota_status
            assert str(_static(app, "#instrument-bar").content) == quota_instrument
            assert app.last_copied_cli == quota_copy_cli

    asyncio.run(scenario())


def test_obtainability_workspace_owns_state_over_older_quota_inspection() -> None:
    """An older quota inspection cannot replace the active Obtainability state."""

    async def scenario() -> None:
        inspect_result = replace(
            _inspect_result(ITEMS[1]),
            resource_scope=ALT_SCOPE,
            identity_evidence=None,
        )
        operations = DelayedQuotaInspectOperations(inspect_result)
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            _table(app, "#quota-ledger").focus()
            await pilot.press("enter")
            await operations.inspect_started.wait()

            await pilot.click("#workspace-obtainability")
            await pilot.pause()
            quota_result = app.last_result
            quota_status = str(_static(app, "#status-line").content)
            quota_instrument = str(_static(app, "#instrument-bar").content)
            quota_copy_cli = app.last_copied_cli

            operations.release_inspect.set()
            await operations.inspect_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "obtainability"
            assert app.last_result is quota_result
            assert str(_static(app, "#status-line").content) == quota_status
            assert str(_static(app, "#instrument-bar").content) == quota_instrument
            assert app.last_copied_cli == quota_copy_cli

    asyncio.run(scenario())


def test_quota_workspace_owns_state_over_older_obtainability_comparison() -> None:
    """A stale advice response cannot repaint the UI after leaving Obtainability."""

    async def scenario() -> None:
        operations = DelayedObtainabilityOperations()
        app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "balanced"
            _input(
                app, "#obtainability-candidates"
            ).value = "us-central1=us-central1-a,us-central1-b"
            _button(app, "#obtainability-compare").press()
            await operations.compare_started.wait()

            await pilot.click("#workspace-quotas")
            await pilot.pause()
            quota_result = app.last_result
            quota_status = str(_static(app, "#status-line").content)
            quota_instrument = str(_static(app, "#instrument-bar").content)
            quota_detail = str(_static(app, "#quota-detail").content)
            quota_copy_cli = app.last_copied_cli

            operations.release_compare.set()
            await operations.compare_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "quotas"
            assert app.last_result is quota_result
            assert str(_static(app, "#status-line").content) == quota_status
            assert str(_static(app, "#instrument-bar").content) == quota_instrument
            assert str(_static(app, "#quota-detail").content) == quota_detail
            assert app.last_copied_cli == quota_copy_cli

    asyncio.run(scenario())


def test_quota_workspace_owns_state_over_older_audit_load() -> None:
    """An older Audit load cannot replace the active quota operation."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        audit = DelayedAuditOperations()
        app = CloudQuotaManagerApp(operations, audit)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-audit")
            await audit.list_started.wait()

            await pilot.click("#workspace-quotas")
            app.action_refresh()
            await pilot.pause()
            quota_result = app.last_result
            assert quota_result is operations.result
            quota_status = str(_static(app, "#status-line").content)
            quota_instrument = str(_static(app, "#instrument-bar").content)
            quota_copy_cli = app.last_copied_cli

            audit.release_list.set()
            await audit.list_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "quotas"
            assert app.last_result is quota_result
            assert str(_static(app, "#status-line").content) == quota_status
            assert str(_static(app, "#instrument-bar").content) == quota_instrument
            assert app.last_copied_cli == quota_copy_cli

    asyncio.run(scenario())


def test_quota_workspace_owns_state_over_older_audit_inspection() -> None:
    """An older Audit inspection cannot replace the active quota operation."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        audit = DelayedAuditWorkerOperations((AUDIT_RECORD,))
        app = CloudQuotaManagerApp(operations, audit)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-audit")
            await pilot.pause()

            _table(app, "#audit-table").focus()
            await pilot.press("enter")
            await audit.inspect_started.wait()

            await pilot.click("#workspace-quotas")
            app.action_refresh()
            await pilot.pause()
            quota_result = app.last_result
            assert quota_result is operations.result
            quota_status = str(_static(app, "#status-line").content)
            audit_detail = str(_static(app, "#audit-detail").content)

            audit.release_inspect.set()
            await audit.inspect_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "quotas"
            assert app.last_result is quota_result
            assert str(_static(app, "#status-line").content) == quota_status
            assert str(_static(app, "#audit-detail").content) == audit_detail

    asyncio.run(scenario())


def test_quota_workspace_owns_state_over_older_audit_verification() -> None:
    """An older Audit verification cannot replace the active quota operation."""

    async def scenario() -> None:
        operations = ScriptedReadOnlyOperations(_browse_result())
        audit = DelayedAuditWorkerOperations((AUDIT_RECORD,))
        app = CloudQuotaManagerApp(operations, audit)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-audit")
            await pilot.pause()

            _button(app, "#audit-verify").press()
            await audit.verify_started.wait()

            await pilot.click("#workspace-quotas")
            app.action_refresh()
            await pilot.pause()
            quota_result = app.last_result
            assert quota_result is operations.result
            quota_status = str(_static(app, "#status-line").content)
            audit_detail = str(_static(app, "#audit-detail").content)

            audit.release_verify.set()
            await audit.verify_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "quotas"
            assert app.last_result is quota_result
            assert str(_static(app, "#status-line").content) == quota_status
            assert str(_static(app, "#audit-detail").content) == audit_detail

    asyncio.run(scenario())


def test_newer_audit_verification_owns_state_over_older_audit_load() -> None:
    """A newer Audit verification cannot be replaced by an older page load."""

    async def scenario() -> None:
        audit = DelayedAuditOperations((AUDIT_RECORD,))
        app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(_browse_result()),
            audit,
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-audit")
            await audit.list_started.wait()

            _button(app, "#audit-verify").press()
            await pilot.pause()
            verify_result = app.last_result
            assert verify_result is not None
            assert verify_result.operation == OperationName("audit.verify")
            verify_status = str(_static(app, "#status-line").content)
            verify_detail = str(_static(app, "#audit-detail").content)
            assert _table(app, "#audit-table").row_count == 0

            audit.release_list.set()
            await audit.list_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "audit"
            assert app.last_result is verify_result
            assert str(_static(app, "#status-line").content) == verify_status
            assert str(_static(app, "#audit-detail").content) == verify_detail
            assert _table(app, "#audit-table").row_count == 0

    asyncio.run(scenario())


def test_newer_audit_verification_owns_state_over_older_audit_inspection() -> None:
    """A newer Audit verification cannot be replaced by an older inspection."""

    async def scenario() -> None:
        audit = DelayedAuditWorkerOperations((AUDIT_RECORD,))
        app = CloudQuotaManagerApp(
            ScriptedReadOnlyOperations(_browse_result()),
            audit,
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-audit")
            await pilot.pause()

            _table(app, "#audit-table").focus()
            await pilot.press("enter")
            await audit.inspect_started.wait()

            _button(app, "#audit-verify").press()
            await audit.verify_started.wait()
            audit.release_verify.set()
            await audit.verify_returned.wait()
            await pilot.pause()
            verify_result = app.last_result
            assert verify_result is not None
            assert verify_result.operation == OperationName("audit.verify")
            verify_status = str(_static(app, "#status-line").content)
            verify_detail = str(_static(app, "#audit-detail").content)

            audit.release_inspect.set()
            await audit.inspect_returned.wait()
            await pilot.pause()

            assert app.active_workspace == "audit"
            assert app.last_result is verify_result
            assert str(_static(app, "#status-line").content) == verify_status
            assert str(_static(app, "#audit-detail").content) == verify_detail

    asyncio.run(scenario())


def test_tui_and_cli_consume_the_same_typed_query_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Navigation differs while operation inputs and result facts stay equal."""
    operations = ScriptedReadOnlyOperations(_browse_result())

    async def tui_scenario() -> None:
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
            no_color=True,
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            assert app.last_result is operations.result

    asyncio.run(tui_scenario())
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )

    cli = CliRunner().invoke(
        cli_module.main,
        [
            "quota",
            "list",
            "--resource-scope",
            SCOPE.canonical_name,
            "--output",
            "json",
            "--no-color",
            "--quiet",
        ],
    )

    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.stdout) == operation_result_mapping(operations.result)
    tui_query, tui_options = operations.browse_calls[0]
    cli_query, cli_options = operations.browse_calls[1]
    assert tui_query == cli_query == ReadOnlyQuotaQuery()
    assert (
        tui_options["scope_input"].explicit_resource_scope
        == cli_options["scope_input"].explicit_resource_scope
        == SCOPE
    )


def test_obtainability_tui_and_cli_consume_the_same_typed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both interfaces preserve the same request and serialized result facts."""
    operations = ScriptedReadOnlyOperations(_browse_result())

    async def tui_scenario() -> None:
        app = CloudQuotaManagerApp(
            operations,
            ScriptedAuditOperations(),
            scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
            no_color=True,
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await pilot.click("#workspace-obtainability")
            _input(app, "#obtainability-machine-type").value = "a3-highgpu-8g"
            _input(app, "#obtainability-vm-count").value = "2"
            _input(app, "#obtainability-distribution").value = "balanced"
            _input(
                app, "#obtainability-candidates"
            ).value = "us-central1=us-central1-a,us-central1-b"
            _button(app, "#obtainability-compare").press()
            await pilot.pause()
            assert app.last_result is operations.obtainability_result

    asyncio.run(tui_scenario())
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )

    cli = CliRunner().invoke(
        cli_module.main,
        [
            "obtainability",
            "compare",
            "--resource-scope",
            SCOPE.canonical_name,
            "--machine-type",
            "a3-highgpu-8g",
            "--vm-count",
            "2",
            "--distribution-shape",
            "balanced",
            "--candidate",
            "us-central1=us-central1-a,us-central1-b",
            "--output",
            "json",
            "--no-color",
            "--quiet",
        ],
    )

    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.stdout) == operation_result_mapping(
        operations.obtainability_result
    )
    tui_prepared, tui_options = operations.obtainability_prepared_calls[0]
    tui_candidates = tui_prepared.candidates
    cli_candidates, cli_options = operations.obtainability_calls[0]
    assert tui_candidates == cli_candidates
    assert (
        tui_options["scope_input"].explicit_resource_scope
        == cli_options["scope_input"].explicit_resource_scope
        == SCOPE
    )


def test_reviewed_semantic_snapshots_cover_required_terminal_and_result_states() -> (
    None
):
    """Cover widths, partial/error, unsupported, and confirmed states."""

    def expected(name: str) -> str:
        return (
            (Path(__file__).parents[2] / "snapshots" / "tui" / f"{name}.txt")
            .read_text(encoding="utf-8")
            .rstrip("\n")
        )

    async def snapshot(
        name: str,
        app: CloudQuotaManagerApp,
        *,
        size: tuple[int, int],
        after: Any = None,
    ) -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            if after is not None:
                await after(app, pilot)
            assert app.interface_snapshot() == expected(name)

    async def open_detail(app: CloudQuotaManagerApp, pilot: Any) -> None:
        del app
        await pilot.press("enter")
        await pilot.pause()

    async def resolve_unsupported(
        app: CloudQuotaManagerApp,
        pilot: Any,
    ) -> None:
        await app.resolve_workload(
            ComputeInstanceRequirement(
                machine_type="a3-highgpu-8g",
                instance_count=1,
                provisioning_model=ProvisioningModel.SPOT,
                locations=CandidateLocations(("us-central1-a",)),
            ),
            app._claim_provider_view(),  # noqa: SLF001 - bind direct call ownership
        )
        await pilot.pause()

    async def scenario() -> None:
        await snapshot(
            "wide-complete",
            CloudQuotaManagerApp(
                ScriptedReadOnlyOperations(_browse_result()),
                ScriptedAuditOperations(),
            ),
            size=(140, 42),
        )
        await snapshot(
            "medium-incomplete",
            CloudQuotaManagerApp(
                ScriptedReadOnlyOperations(
                    _browse_result(
                        complete=False,
                        diagnostics=(PARTIAL_DIAGNOSTIC,),
                    )
                ),
                ScriptedAuditOperations(),
            ),
            size=(100, 32),
        )
        await snapshot(
            "narrow-locked-confirmed",
            CloudQuotaManagerApp(
                ScriptedReadOnlyOperations(_browse_result()),
                ScriptedAuditOperations(),
                scope_locked=True,
                no_color=True,
            ),
            size=(72, 28),
            after=open_detail,
        )
        await snapshot(
            "unsupported-workload",
            CloudQuotaManagerApp(
                ScriptedReadOnlyOperations(_browse_result()),
                ScriptedAuditOperations(),
            ),
            size=(100, 32),
            after=resolve_unsupported,
        )
        await snapshot(
            "provider-error",
            CloudQuotaManagerApp(
                ScriptedReadOnlyOperations(
                    _failure_result()  # type: ignore[arg-type]
                ),
                ScriptedAuditOperations(),
            ),
            size=(100, 32),
        )

    asyncio.run(scenario())
