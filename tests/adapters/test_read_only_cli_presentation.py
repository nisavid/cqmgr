"""Public presentation contracts for read-only CLI operation results."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from cqmgr.adapters.cli.read_only import Presentation, emit_read_only_result
from cqmgr.application.operations.quotas import QuotaBrowseData, QuotaInspectData
from cqmgr.application.operations.read_only import (
    IncompleteQuotaInspectData,
    QuotaInspectSelector,
    ReadOnlyFailureData,
)
from cqmgr.domain.accelerator_overlay import (
    CandidateLocations,
    ComputeInstanceRequirement,
    ProvisioningModel,
    QuotaConstraintAssessment,
    QuotaConstraintRequirement,
    ResolvedWorkloadLocation,
    ResolvedWorkloadRequirement,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    AcceleratorConstraintSet,
    AcceleratorId,
    CatalogMetadata,
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
from cqmgr.domain.quota_queries import (
    PROVIDER_INVENTORY_REVISION,
    QUOTA_QUERY_EVIDENCE_CONTRACT,
    ProviderSourceCoverage,
    QuotaQuery,
    QuotaQueryFilters,
    QuotaQueryItem,
    QuotaSort,
    QuotaSortField,
    SortDirection,
)
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    MonitoringPoint,
    MonitoringValue,
    MonitoringValueKind,
    NormalizedDimensions,
    QuotaContainerType,
    QuotaIncreaseEligibility,
    QuotaIneligibilityReason,
    QuotaPreferenceEvidence,
    QuotaPreferenceOrigin,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
    UsageObservation,
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
    StableSymbol,
)
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 23, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
UNIT = QuotaUnit("1")
IDENTITY = EffectiveQuotaSliceIdentity(
    SCOPE,
    "compute.googleapis.com",
    "GPUS-PER-GPU-FAMILY-per-project-region",
    NormalizedDimensions((("gpu_family", "NVIDIA_H100"), ("region", "us-central1"))),
    QuotaScope.REGIONAL,
)


def test_json_presentation_uses_the_canonical_operation_result_mapping(
    capsys: object,
) -> None:
    """Browse JSON is compact, deterministic, and preserves source coverage."""
    item = QuotaQueryItem(
        identity=IDENTITY,
        display_name="NVIDIA H100 GPUs",
        accelerator_id=AcceleratorId("nvidia-h100"),
        location="us-central1",
        quota_pool="standard",
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=True,
            guided=True,
            mutable=False,
        ),
        effective_value=QuotaQuantity(64, UNIT),
        evidence_observed_at=NOW,
    )
    result = OperationResult(
        operation=OperationName("quota.list"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("logical-page-read"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=QuotaBrowseData(
            query=None,
            items=(item,),
            constraint_sets=(),
            ordered=True,
            total=1,
            next_cursor="opaque-next",
            snapshot_id="snapshot-1",
            source_coverage=(
                ProviderSourceCoverage.complete(
                    "compute.googleapis.com",
                    pages_attempted=1,
                    pages_completed=1,
                    observed_at=NOW,
                ),
                ProviderSourceCoverage.intentionally_unqueried("tpu.googleapis.com"),
            ),
            catalog=CatalogMetadata(
                ACCELERATOR_CATALOG_SCHEMA,
                "2026-07-23",
                "sha256:" + "a" * 64,
            ),
            evidence_contract=QUOTA_QUERY_EVIDENCE_CONTRACT,
            inventory_revision=PROVIDER_INVENTORY_REVISION,
            observed_at=NOW,
        ),
        identity_evidence=ProviderIdentityEvidence(
            credential_kind=CredentialKind.IMPERSONATED,
            verification=PrincipalVerification.VERIFIED,
            acting_principal=PrincipalIdentity(
                "serviceAccount:quota-reader@example.iam.gserviceaccount.com"
            ),
            impersonation_chain=(
                PrincipalIdentity(
                    "serviceAccount:source@example.iam.gserviceaccount.com"
                ),
                PrincipalIdentity(
                    "serviceAccount:quota-reader@example.iam.gserviceaccount.com"
                ),
            ),
        ),
    )

    exit_class = emit_read_only_result(
        result,
        Presentation(output="json", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == 0
    assert captured.err == ""
    assert (
        captured.out
        == json.dumps(
            json.loads(captured.out),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    payload = json.loads(captured.out)
    assert payload["resource_scope"] == {"type": "project", "name": "projects/123"}
    assert payload["data"]["source_coverage"][1]["state"] == "intentionally-unqueried"
    assert payload["data"]["next_cursor"] == "opaque-next"
    assert payload["data"]["catalog"]["content_digest"] == "sha256:" + "a" * 64
    assert payload["data"]["evidence_contract"] == QUOTA_QUERY_EVIDENCE_CONTRACT
    assert payload["data"]["inventory_revision"] == PROVIDER_INVENTORY_REVISION
    assert payload["data"]["observed_at"] == "2026-07-23T00:00:00Z"
    assert payload["identity_evidence"] == {
        "acting_principal": (
            "serviceAccount:quota-reader@example.iam.gserviceaccount.com"
        ),
        "credential_kind": "impersonated",
        "impersonation_chain": [
            "serviceAccount:source@example.iam.gserviceaccount.com",
            "serviceAccount:quota-reader@example.iam.gserviceaccount.com",
        ],
        "verification": "verified",
    }


def test_nonzero_inspection_result_uses_stderr_common_envelope(
    capsys: object,
) -> None:
    """Incomplete exact-slice inspection keeps all identity and failure facts."""
    result = OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("exact-slice-inspected"), reached=False
        ),
        outcome=Outcome(
            StableSymbol("incomplete-provider-evidence"),
            ExitClass.INCOMPLETE_EVIDENCE,
        ),
        completeness=Completeness.incomplete(
            EvidenceGap(StableSymbol("cloud-quotas"), StableSymbol("page-failed"))
        ),
        started_at=NOW,
        finished_at=NOW,
        data=QuotaInspectData(
            IDENTITY,
            None,
            None,
            None,
            None,
            None,
            None,
            "not-found",
        ),
    )

    exit_class = emit_read_only_result(
        result,
        Presentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == ExitClass.INCOMPLETE_EVIDENCE
    assert captured.out == ""
    assert "Operation: quota.inspect" in captured.err
    assert "Outcome: incomplete-provider-evidence (exit 6)" in captured.err
    assert "Boundary: exact-slice-inspected (not reached)" in captured.err
    assert "Complete: false" in captured.err
    assert "Resource scope: projects/123" in captured.err
    assert "Slice service: compute.googleapis.com" in captured.err
    assert "Slice quota ID: GPUS-PER-GPU-FAMILY-per-project-region" in captured.err
    assert (
        "Slice dimensions: gpu_family=NVIDIA_H100, region=us-central1" in captured.err
    )
    assert "Reason: not-found" in captured.err
    assert "\x1b" not in captured.err


def test_incomplete_inspection_human_output_retains_exact_evidence(
    capsys: object,
) -> None:
    """Partial inspect evidence renders as stable human facts, never a repr."""
    item = QuotaQueryItem(
        identity=IDENTITY,
        display_name="NVIDIA H100 GPUs",
        accelerator_id=AcceleratorId("nvidia-h100"),
        location="us-central1",
        quota_pool="standard",
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=True,
            guided=True,
            mutable=False,
        ),
        effective_value=QuotaQuantity(64, UNIT),
        evidence_observed_at=NOW,
    )
    coverage = (
        ProviderSourceCoverage.incomplete(
            "compute.googleapis.com",
            pages_attempted=2,
            pages_completed=1,
            observed_at=NOW,
        ),
        ProviderSourceCoverage.intentionally_unqueried("tpu.googleapis.com"),
    )
    selector = QuotaInspectSelector(
        IDENTITY.service,
        IDENTITY.quota_id,
        "us-central1",
        IDENTITY.dimensions,
    )
    result = OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("exact-slice-inspected"),
            reached=False,
        ),
        outcome=Outcome(
            StableSymbol("incomplete-evidence"),
            ExitClass.INCOMPLETE_EVIDENCE,
        ),
        completeness=Completeness.incomplete(
            EvidenceGap(StableSymbol("cloud-quotas"), StableSymbol("page-failed"))
        ),
        started_at=NOW,
        finished_at=NOW,
        data=IncompleteQuotaInspectData(
            selector=selector,
            matching_items=(item,),
            source_coverage=coverage,
            reason="provider-read-incomplete",
        ),
    )

    exit_class = emit_read_only_result(
        result,
        Presentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == ExitClass.INCOMPLETE_EVIDENCE
    assert captured.out == ""
    assert "Selector service: compute.googleapis.com" in captured.err
    assert "Selector quota ID: GPUS-PER-GPU-FAMILY-per-project-region" in captured.err
    assert "Selector location: us-central1" in captured.err
    assert "Slice service: compute.googleapis.com" in captured.err
    assert "Slice quota ID: GPUS-PER-GPU-FAMILY-per-project-region" in captured.err
    assert "Source coverage: compute.googleapis.com incomplete" in captured.err
    assert "Reason: provider-read-incomplete" in captured.err
    assert "IncompleteQuotaInspectData(" not in captured.err


def test_human_browse_preserves_query_classification_and_constraints(
    capsys: object,
) -> None:
    """Browse output keeps normalized input, classification, and relationships."""
    item = QuotaQueryItem(
        identity=IDENTITY,
        display_name="NVIDIA H100 GPUs",
        accelerator_id=AcceleratorId("nvidia-h100"),
        location="us-central1",
        quota_pool="standard",
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=True,
            guided=True,
            mutable=False,
        ),
        effective_value=QuotaQuantity(64, UNIT),
        evidence_observed_at=NOW,
    )
    constraint_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-h100"),
        (ConstraintReference(IDENTITY),),
    )
    query = QuotaQuery(
        SCOPE,
        QuotaQueryFilters(
            services=("compute",),
            locations=("us-central1",),
        ),
        (QuotaSort(QuotaSortField.QUOTA_ID, SortDirection.DESC),),
    )
    result = OperationResult(
        operation=OperationName("quota.list"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("logical-page-read"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=QuotaBrowseData(
            query=query,
            items=(item,),
            constraint_sets=(constraint_set,),
            ordered=True,
            total=1,
            next_cursor=None,
            snapshot_id="snapshot-1",
            source_coverage=(
                ProviderSourceCoverage.complete(
                    "compute.googleapis.com",
                    pages_attempted=1,
                    pages_completed=1,
                    observed_at=NOW,
                ),
                ProviderSourceCoverage.intentionally_unqueried("tpu.googleapis.com"),
            ),
            catalog=CatalogMetadata(
                ACCELERATOR_CATALOG_SCHEMA,
                "2026-07-23",
                "sha256:" + "b" * 64,
            ),
            evidence_contract=QUOTA_QUERY_EVIDENCE_CONTRACT,
            inventory_revision=PROVIDER_INVENTORY_REVISION,
            observed_at=NOW,
        ),
    )

    emit_read_only_result(
        result,
        Presentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert "Queried services: compute.googleapis.com" in captured.out
    assert "Query locations: us-central1" in captured.out
    assert "Query sort: quota-id:desc" in captured.out
    assert f"Inventory revision: {PROVIDER_INVENTORY_REVISION}" in captured.out
    assert f"Evidence contract: {QUOTA_QUERY_EVIDENCE_CONTRACT}" in captured.out
    assert "Catalog schema: cqmgr.accelerator-catalog/v1" in captured.out
    assert "Catalog revision: 2026-07-23" in captured.out
    assert "Catalog digest: sha256:" + "b" * 64 in captured.out
    assert "Query observed at: 2026-07-23T00:00:00Z" in captured.out
    assert "Accelerator: nvidia-h100" in captured.out
    assert "Cataloged: true" in captured.out
    assert "Guided: true" in captured.out
    assert "Mutable: false" in captured.out
    assert "Constraint set accelerator: nvidia-h100" in captured.out
    assert "Constraint slice quota ID: GPUS-PER-GPU-FAMILY-per-project-region" in (
        captured.out
    )
    assert "Source coverage: tpu.googleapis.com intentionally-unqueried" in captured.out


def test_human_inspection_preserves_provider_evidence_and_relationships(
    capsys: object,
) -> None:
    """Exact-slice human output retains every independently observable fact."""
    eligibility = QuotaIncreaseEligibility(
        eligible=True,
        reason=ProviderSymbol("OTHER", QuotaIneligibilityReason),
    )
    evidence = EffectiveQuotaEvidence(
        identity=IDENTITY,
        effective_value=QuotaQuantity(64, UNIT),
        metric="compute.googleapis.com/quota/gpus",
        declared_dimensions=("gpu_family", "region"),
        applicable_locations=("us-central1",),
        eligibility=eligibility,
        fixed=False,
        concurrent=True,
        precise=True,
        refresh_interval="60s",
        ongoing_rollout=False,
        container_type=ProviderSymbol("PROJECT", QuotaContainerType),
    )
    preference = QuotaPreferenceEvidence(
        provider_name=(
            "projects/123/locations/global/quotaPreferences/h100-us-central1"
        ),
        identity=IDENTITY,
        preferred_value=80,
        granted_value=72,
        etag="public-etag",
        reconciling=True,
        state_detail="reconciling",
        trace_id="public-trace",
        create_time=NOW - timedelta(minutes=2),
        update_time=NOW - timedelta(minutes=1),
        request_origin=ProviderSymbol("CLOUD_CONSOLE", QuotaPreferenceOrigin),
    )
    usage = UsageObservation(
        resource_scope=SCOPE,
        metric_type="serviceruntime.googleapis.com/quota/allocation/usage",
        metric_labels=NormalizedDimensions((("quota_metric", "gpus"),)),
        resource_type="consumer_quota",
        resource_labels=NormalizedDimensions((("project_id", "123"),)),
        points=(
            MonitoringPoint(
                NOW - timedelta(minutes=5),
                NOW,
                MonitoringValue(MonitoringValueKind.INT64, 12),
            ),
        ),
        unit="1",
    )
    constraint_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-h100"),
        (ConstraintReference(IDENTITY),),
    )
    result = OperationResult(
        operation=OperationName("quota.inspect"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("exact-slice-inspected"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=QuotaInspectData(
            identity=IDENTITY,
            evidence=evidence,
            item=None,
            preference=preference,
            usage=usage,
            status=None,
            constraint_set=constraint_set,
        ),
    )

    emit_read_only_result(
        result,
        Presentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert "Eligible for increase: true" in captured.out
    assert "Eligibility reason: OTHER" in captured.out
    assert "Fixed quota: false" in captured.out
    assert "Concurrent: true" in captured.out
    assert "Precise: true" in captured.out
    assert "Refresh interval: 60s" in captured.out
    assert "Preference desired: 80" in captured.out
    assert "Preference granted: 72" in captured.out
    assert "Preference etag: public-etag" in captured.out
    assert "Preference observed at: 2026-07-22T23:59:00Z" in captured.out
    assert "Usage point: 12 (int64)" in captured.out
    assert (
        "Usage interval: 2026-07-22T23:55:00Z through 2026-07-23T00:00:00Z"
    ) in captured.out
    assert "Constraint set accelerator: nvidia-h100" in captured.out
    assert "Constraint slice service: compute.googleapis.com" in captured.out


def test_human_resolution_preserves_each_location_and_constraint(
    capsys: object,
) -> None:
    """Resolution does not collapse the location, native-unit, or permit facts."""
    companion_identity = EffectiveQuotaSliceIdentity(
        SCOPE,
        "compute.googleapis.com",
        "INSTANCES-PER-PROJECT-REGION",
        NormalizedDimensions((("region", "us-central1"),)),
        QuotaScope.REGIONAL,
    )
    constraint_set = AcceleratorConstraintSet(
        AcceleratorId("nvidia-h100"),
        (ConstraintReference(IDENTITY), ConstraintReference(companion_identity)),
    )
    requirement = ComputeInstanceRequirement(
        machine_type="a3-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )
    location = ResolvedWorkloadLocation(
        location="us-central1-a",
        disposition=WorkloadLocationDisposition.COMPATIBLE,
        accelerator_id=AcceleratorId("nvidia-h100"),
        owning_service="compute.googleapis.com",
        management_plane=ManagementPlane.COMPUTE,
        supported_consumers=(WorkloadConsumer.COMPUTE_ENGINE, WorkloadConsumer.GKE),
        quota_pool="standard",
        deployable_accelerator_quantity=8,
        constraint_set=constraint_set,
        constraint_requirements=(
            QuotaConstraintRequirement(
                IDENTITY,
                8,
                QuotaQuantity(8, UNIT),
                UnitConversionEvidence("accelerator", UNIT, 1, "catalog/v1"),
            ),
            QuotaConstraintRequirement(
                companion_identity,
                1,
                QuotaQuantity(1, UNIT),
                UnitConversionEvidence("instance", UNIT, 1, "catalog/v1"),
            ),
        ),
        assessments=(
            QuotaConstraintAssessment(
                identity=IDENTITY,
                effective=QuotaQuantity(16, UNIT),
                usage=QuotaQuantity(4, UNIT),
                required=QuotaQuantity(8, UNIT),
                permits=True,
            ),
            QuotaConstraintAssessment(
                identity=companion_identity,
                effective=QuotaQuantity(20, UNIT),
                usage=QuotaQuantity(19, UNIT),
                required=QuotaQuantity(1, UNIT),
                permits=True,
            ),
        ),
        coverage=(),
    )
    result = OperationResult(
        operation=OperationName("quota.resolve"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(
            StableSymbol("workload-requirement-resolved"), reached=True
        ),
        outcome=Outcome(StableSymbol("requirement-resolved"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=ResolvedWorkloadRequirement(requirement, (location,), None),
    )

    exit_class = emit_read_only_result(
        result,
        Presentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == 0
    assert captured.err == ""
    assert "Resource scope: projects/123" in captured.out
    assert "Requirement: compute-instance" in captured.out
    assert "Machine type: a3-highgpu-8g" in captured.out
    assert "Instance count: 1" in captured.out
    assert "Provisioning model: standard" in captured.out
    assert "Location mode: candidates" in captured.out
    assert "Location: us-central1-a" in captured.out
    assert "Disposition: compatible" in captured.out
    assert "Supported consumers: compute-engine, gke" in captured.out
    assert "Constraint required: 8 1 (source quantity: 8)" in captured.out
    assert "Constraint effective: 16 1" in captured.out
    assert "Constraint usage: 4 1" in captured.out
    assert "Constraint permits: true" in captured.out
    assert "Slice service: compute.googleapis.com" in captured.out
    assert "Permits: true" in captured.out
    first_assessment = captured.out.split("Constraint assessment 1:\n", 1)[1]
    assert first_assessment.index(
        "Slice quota ID: GPUS-PER-GPU-FAMILY-per-project-region"
    ) < first_assessment.index("Constraint effective: 16 1")
    second_assessment = captured.out.split("Constraint assessment 2:\n", 1)[1]
    assert second_assessment.index(
        "Slice quota ID: INSTANCES-PER-PROJECT-REGION"
    ) < second_assessment.index("Constraint effective: 20 1")
    assert "\x1b" not in captured.out


def test_human_setup_failure_preserves_the_stable_reason(capsys: object) -> None:
    """Facade setup and usage failures render their safe typed reason."""
    result = OperationResult(
        operation=OperationName("quota.list"),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol("logical-page-read"), reached=False),
        outcome=Outcome(
            StableSymbol("resource-scope-unavailable"), ExitClass.REJECTED_PRECONDITION
        ),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=ReadOnlyFailureData("resource-scope-unavailable"),
    )

    exit_class = emit_read_only_result(
        result,
        Presentation(output="human", no_color=True, quiet=True),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert exit_class == ExitClass.REJECTED_PRECONDITION
    assert captured.out == ""
    assert "Reason: resource-scope-unavailable" in captured.err


def test_human_provider_result_preserves_identity_and_safe_diagnostics(
    capsys: object,
) -> None:
    """Provider identity and actionable diagnostics remain visible without JSON."""
    result = OperationResult(
        operation=OperationName("quota.list"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("logical-page-read"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=QuotaBrowseData(
            query=None,
            items=(),
            constraint_sets=(),
            ordered=True,
            total=0,
            next_cursor=None,
            snapshot_id="snapshot-1",
        ),
        diagnostics=(
            Diagnostic(
                DiagnosticCode("principal-unverified"),
                Severity.WARNING,
                DiagnosticPhase("identity-resolution"),
                DiagnosticSource("application-default-credentials"),
                RetryDisposition.NEVER,
                RedactedText(
                    "Configure ADC with a stable verifiable principal outside cqmgr."
                ),
            ),
        ),
        identity_evidence=ProviderIdentityEvidence(
            credential_kind=CredentialKind.IMPERSONATED,
            verification=PrincipalVerification.VERIFIED,
            acting_principal=PrincipalIdentity(
                "serviceAccount:quota-reader@example.iam.gserviceaccount.com"
            ),
            impersonation_chain=(
                PrincipalIdentity(
                    "serviceAccount:source@example.iam.gserviceaccount.com"
                ),
                PrincipalIdentity(
                    "serviceAccount:quota-reader@example.iam.gserviceaccount.com"
                ),
            ),
        ),
    )

    emit_read_only_result(
        result,
        Presentation(output="human", no_color=True, quiet=False),
    )

    captured = capsys.readouterr()  # type: ignore[union-attr]
    assert "Credential kind: impersonated" in captured.out
    assert "Principal verification: verified" in captured.out
    assert (
        "Acting principal: serviceAccount:quota-reader@example.iam.gserviceaccount.com"
    ) in captured.out
    assert (
        "Impersonation chain: "
        "serviceAccount:source@example.iam.gserviceaccount.com -> "
        "serviceAccount:quota-reader@example.iam.gserviceaccount.com"
    ) in captured.out
    assert "Diagnostic principal-unverified (warning)" not in captured.out
    assert (
        "Guidance: Configure ADC with a stable verifiable principal" not in captured.out
    )
    assert "Diagnostic principal-unverified (warning)" in captured.err
    assert "Guidance: Configure ADC with a stable verifiable principal" in captured.err
