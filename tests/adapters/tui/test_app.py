"""Pilot contracts for the Textual shell and quota inspector."""

# Test fakes intentionally mirror public protocols and favor literal domain fixtures.
# ruff: noqa: ANN401, D102, D107, FBT003, PLR2004

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from click.testing import CliRunner
from textual.filter import Monochrome, NoColor
from textual.widgets import Button, DataTable, Input, Static

import cqmgr.cli as cli_module
from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.adapters.tui.app import CloudQuotaManagerApp
from cqmgr.application.operations.audit import (
    AuditInspectData,
    AuditListData,
    AuditVerifyData,
)
from cqmgr.application.operations.quotas import QuotaBrowseData, QuotaInspectData
from cqmgr.application.operations.read_only import (
    QuotaInspectSelector,
    ReadOnlyFailureData,
    ReadOnlyQuotaQuery,
    ReadOnlyScopeInput,
)
from cqmgr.cli import main
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
    ResolutionFailureReason,
    ResolvedWorkloadLocation,
    ResolvedWorkloadRequirement,
    WorkloadLocationDisposition,
)
from cqmgr.domain.audit import (
    AUDIT_GENESIS_HASH,
    AuditQuery,
    AuditRecord,
    AuditRecordDraft,
    AuditRecordKind,
    AuditVerification,
)
from cqmgr.domain.catalog import CatalogPredicates
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
from cqmgr.domain.quota_queries import ProviderSourceCoverage, QuotaQueryItem
from cqmgr.domain.quotas import (
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
    StableSymbol,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import QuotaRequestStatus, Reconciliation

if TYPE_CHECKING:
    import pytest

    from cqmgr.application.ports.coordination import CancellationToken

NOW = datetime(2026, 7, 23, 20, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
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
            _input(app, "#workload-machine-type").value = "a3-highgpu-8g"
            _input(app, "#workload-count").value = "2"
            _input(app, "#workload-provisioning").value = "spot"
            _input(app, "#workload-locations").value = "us-central1-a, us-east1-b"
            _button(app, "#workload-submit").press()
            await pilot.pause()

            compute, compute_options = operations.resolve_calls[0]
            assert isinstance(compute, ComputeInstanceRequirement)
            assert compute.machine_type == "a3-highgpu-8g"
            assert compute.instance_count == 2
            assert isinstance(compute.locations, CandidateLocations)
            assert compute.locations.values == ("us-central1-a", "us-east1-b")
            assert compute_options["scope_input"] == ReadOnlyScopeInput()
            assert app.last_copied_cli is not None
            assert app.last_copied_cli.startswith(
                "cqmgr quota resolve compute-instance "
            )
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
        main,
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
            )
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
