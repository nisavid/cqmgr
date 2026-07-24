"""Textual shell for Cloud Quota Manager operations."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast, override

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, Static

from cqmgr.adapters.cli.copy_cli import (
    obtainability_all_compatible_copy_cli,
    obtainability_compare_copy_cli,
    plan_apply_copy_cli,
    plan_review_copy_cli,
    quota_inspect_copy_cli,
    quota_list_copy_cli,
    quota_resolve_copy_cli,
)
from cqmgr.adapters.cli.read_only_requests import (
    parse_cloud_tpu_slice_requirement,
    parse_compute_instance_requirement,
    parse_obtainability_candidates,
    parse_obtainability_shape,
)
from cqmgr.application.operations.audit import (
    AuditInspectData,
    AuditListData,
    AuditVerifyData,
)
from cqmgr.application.operations.obtainability import (
    PreparedObtainabilityComparison,
    candidates_from_resolved_workload,
    prepare_obtainability_comparison,
)
from cqmgr.application.operations.quotas import QuotaBrowseData, QuotaInspectData
from cqmgr.application.operations.read_only import (
    IncompleteQuotaInspectData,
    QuotaInspectSelector,
    ReadOnlyFailureData,
    ReadOnlyQuotaQuery,
    ReadOnlyScopeInput,
)
from cqmgr.application.operations.watch import WatchStartError
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.domain.accelerator_overlay import (
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
    ResolvedWorkloadRequirement,
    WorkloadLocationDisposition,
)
from cqmgr.domain.apply_records import (
    ApplyChildDisposition,
    UnknownDispatchResolution,
)
from cqmgr.domain.audit import AuditQuery
from cqmgr.domain.obtainability import (
    DistributionShape,
    GpuAttachment,
    ObtainabilityCandidate,
    ObtainabilityComparison,
    SpotMachineConfiguration,
)
from cqmgr.domain.quota_queries import QuotaQueryFilters, QuotaQueryItem

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from textual import events
    from textual.binding import BindingType
    from textual.worker import Worker

    from cqmgr.application.operations.apply import (
        ApplyChildData,
        ApplyData,
        ApplyRequest,
    )
    from cqmgr.application.operations.lifecycle import LifecycleOperations
    from cqmgr.application.operations.plans import (
        ComposeRequest,
        Composition,
        PlanReviewData,
        PlanReviewRequest,
        PreviewData,
        PreviewRequest,
    )
    from cqmgr.application.operations.watch import WatchRequest
    from cqmgr.domain.diagnostics import Diagnostic
    from cqmgr.domain.obtainability import RankedCandidate
    from cqmgr.domain.plans import QuotaPlan, QuotaRequestPlanChild
    from cqmgr.domain.quotas import EffectiveQuotaSliceIdentity, QuotaQuantity
    from cqmgr.domain.results import OperationResult
    from cqmgr.domain.scopes import ResourceScope
    from cqmgr.domain.watch import WatchStreamEvent


_DEFAULT_SCOPE_INPUT = ReadOnlyScopeInput()


class ReadOnlyOperationsLike(Protocol):
    """Typed read-only operation seam shared with the CLI."""

    async def browse(  # noqa: PLR0913 - mirrors the shared application seam
        self,
        query: ReadOnlyQuotaQuery | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[Any]:
        """Browse one bounded logical query."""

    async def inspect(
        self,
        selector: QuotaInspectSelector,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[Any]:
        """Inspect one exact quota slice."""

    async def resolve(
        self,
        requirement: ComputeInstanceRequirement | CloudTpuSliceRequirement,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[Any]:
        """Resolve one workload-first requirement."""

    async def compare_obtainability_prepared(
        self,
        prepared_comparison: PreparedObtainabilityComparison,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
    ) -> OperationResult[Any]:
        """Compare one frozen resolver-backed candidate set without expansion."""

    async def aclose(self) -> None:
        """Close invocation-scoped provider clients."""


class AuditOperationsLike(Protocol):
    """Typed local audit operation seam shared with the CLI."""

    async def list(self, query: AuditQuery) -> OperationResult[AuditListData]:
        """Read one bounded local audit page."""

    async def inspect(self, record_id: str) -> OperationResult[AuditInspectData]:
        """Inspect one retained audit record."""

    async def verify(
        self,
        *,
        from_record_id: str | None = None,
        through_record_id: str | None = None,
    ) -> OperationResult[AuditVerifyData]:
        """Verify one retained audit range."""


@dataclass(frozen=True, slots=True)
class QuotaSelection:
    """One selected row and its stable public selector."""

    item: QuotaQueryItem
    selector: QuotaInspectSelector


@dataclass(frozen=True, slots=True)
class WorkspaceCopyCli:
    """One canonical command owned by exactly one sibling workspace."""

    workspace: str
    command: str

    def __post_init__(self) -> None:
        """Reject cross-workspace or empty copy state."""
        if self.workspace not in {"quotas", "obtainability", "audit"}:
            msg = "copy CLI workspace must be canonical"
            raise ValueError(msg)
        if not self.command:
            msg = "copy CLI command must be non-empty"
            raise ValueError(msg)


class ObtainabilityEntryMode(StrEnum):
    """How the operator entered the sibling Obtainability workspace."""

    STANDALONE = "standalone"
    CONTEXTUAL = "contextual"


@dataclass(frozen=True, slots=True)
class ObtainabilityRequestFingerprint:
    """Canonical digest of every operator-editable fixed-request input."""

    value: str


@dataclass(frozen=True, slots=True)
class ObtainabilityFormDraft:
    """One decoded TUI request before resolver-backed eligibility."""

    machine: SpotMachineConfiguration
    vm_count: int
    distribution_shape: DistributionShape
    candidates: tuple[ObtainabilityCandidate, ...]
    all_compatible: bool

    @property
    def fingerprint(self) -> ObtainabilityRequestFingerprint:
        """Bind mode, fixed shape, and exact explicit candidate identities."""
        machine = self.machine
        encoded = "\x1f".join(
            (
                machine.machine_type,
                "" if machine.gpu is None else machine.gpu.accelerator_type,
                "" if machine.gpu is None else str(machine.gpu.count),
                str(machine.local_ssd_count),
                str(self.vm_count),
                self.distribution_shape.value,
                "all-compatible" if self.all_compatible else "explicit",
                *(candidate.candidate_id for candidate in self.candidates),
            )
        )
        return ObtainabilityRequestFingerprint(
            "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        )


@dataclass(frozen=True, slots=True)
class ObtainabilityWorkflowState:
    """Typed confirmation state for one exact Obtainability request."""

    entry_mode: ObtainabilityEntryMode = ObtainabilityEntryMode.STANDALONE
    pending_expansion: PreparedObtainabilityComparison | None = None
    pending_fingerprint: ObtainabilityRequestFingerprint | None = None
    pending_all_compatible: bool = False
    confirmed_fingerprint: ObtainabilityRequestFingerprint | None = None
    confirmed_candidate_ids: tuple[str, ...] = ()


class LifecycleRoute(StrEnum):
    """Focused mutation or observation route inside the Quotas workspace."""

    COMPOSE = "compose"
    PLAN_REVIEW = "plan-review"
    APPLY = "apply"
    WATCH = "watch"


@dataclass(slots=True)
class LifecycleRouteState:
    """Typed focused-route state without weakening application inputs."""

    route: LifecycleRoute | None = None
    bound_scope: ResourceScope | None = None
    pending_preview: PreviewRequest | None = None
    pending_apply: ApplyRequest | None = None
    apply_in_progress: bool = False
    apply_result: OperationResult[ApplyData] | None = None
    pending_watch: WatchRequest | None = None
    latest_resume: str | None = None
    affected_selectors: tuple[QuotaInspectSelector, ...] = ()
    previous_scope_locked: bool = False
    previous_focus_id: str | None = None


class CloudQuotaManagerApp(App[None]):
    """Adaptive Textual shell over shared typed lifecycle operations."""

    TITLE = "Cloud Quota Manager"
    SUB_TITLE = "Quota inspector"
    ENABLE_COMMAND_PALETTE = True
    CSS = """
    Screen {
        background: #15191d;
        color: #eef1f3;
    }

    #instrument-bar {
        height: 3;
        padding: 0 1;
        background: #22282e;
        border-bottom: solid #89939c;
    }

    #workspace-nav {
        height: 3;
        padding: 0 1;
        background: #1b2025;
        border-bottom: solid #4d5963;
    }

    #workspace-nav Button {
        min-width: 16;
        margin-right: 1;
        background: #2a3138;
        color: #eef1f3;
        border: none;
    }

    #workspace-nav Button.active-workspace {
        background: #334c63;
        color: #ffffff;
        text-style: bold;
    }

    #status-line {
        height: 3;
        padding: 0 1;
        background: #20262b;
        color: #dce2e7;
        border-bottom: solid #4d5963;
    }

    #quota-workbench, #audit-workspace, #obtainability-workspace,
    #lifecycle-route {
        height: 1fr;
    }

    #lifecycle-route {
        padding: 1 2;
        background: #1d2328;
    }

    #lifecycle-scope {
        height: auto;
        padding-bottom: 1;
        color: #d9e7f2;
        border-bottom: solid #89939c;
    }

    #lifecycle-detail {
        height: 1fr;
        padding: 1 0;
    }

    #lifecycle-controls {
        height: auto;
    }

    #lifecycle-controls Input {
        margin-bottom: 1;
    }

    #lifecycle-controls Button {
        margin-right: 1;
    }

    #scope-filter-rail {
        width: 30;
        min-width: 24;
        padding: 1;
        background: #1d2328;
        border-right: solid #67727b;
    }

    #quota-ledger-pane {
        width: 2fr;
        min-width: 34;
        padding: 0 1;
    }

    #quota-detail-pane {
        width: 1fr;
        min-width: 30;
        padding: 1;
        background: #1d2328;
        border-left: solid #67727b;
    }

    #quota-ledger {
        height: 1fr;
    }

    #coverage-summary {
        height: auto;
        min-height: 2;
        color: #cbd3da;
    }

    #copy-cli-preview {
        height: auto;
        max-height: 5;
        color: #d9e7f2;
        background: #232b31;
        padding: 0 1;
    }

    #audit-table {
        height: 1fr;
    }

    #audit-detail {
        height: 12;
        padding: 1;
        border-top: solid #67727b;
    }

    #obtainability-form {
        width: 36;
        min-width: 30;
        padding: 1;
        background: #1d2328;
        border-right: solid #67727b;
    }

    #obtainability-result-pane {
        width: 1fr;
        padding: 1;
    }

    #obtainability-detail {
        height: 1fr;
    }

    #obtainability-copy-cli {
        height: auto;
        max-height: 6;
        color: #d9e7f2;
        background: #232b31;
        padding: 0 1;
    }

    .medium #obtainability-form {
        width: 32;
    }

    .narrow #obtainability-workspace {
        layout: vertical;
    }

    .narrow #obtainability-form {
        width: 1fr;
        height: auto;
        max-height: 18;
        border-right: none;
        border-bottom: solid #67727b;
    }

    .medium #scope-filter-rail, .narrow #scope-filter-rail {
        display: none;
    }

    .narrow #quota-detail-pane {
        display: none;
    }

    .narrow.detail-route #quota-ledger-pane {
        display: none;
    }

    .narrow.detail-route #quota-detail-pane {
        display: block;
        width: 1fr;
        border-left: none;
    }

    .hidden {
        display: none;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "workspace('quotas')", "Quotas", show=False),
        Binding("o", "workspace('obtainability')", "Obtainability", show=False),
        Binding("a", "workspace('audit')", "Audit", show=False),
        Binding("/", "focus_filters", "Filters"),
        Binding("escape", "return_to_ledger", "Back"),
        Binding("ctrl+k", "command_palette", "Commands"),
        Binding("question_mark", "help", "Help"),
        Binding("r", "refresh", "Refresh"),
    ]

    _WIDE_MINIMUM = 120
    _MEDIUM_MINIMUM = 80
    _PROVIDER_OPERATION_SECONDS = 60.0
    _OBTAINABILITY_INPUT_IDS = (
        "obtainability-machine-type",
        "obtainability-gpu-type",
        "obtainability-gpu-count",
        "obtainability-vm-count",
        "obtainability-distribution",
        "obtainability-candidates",
    )

    def __init__(  # noqa: PLR0913 - complete surface composition contract
        self,
        read_only: ReadOnlyOperationsLike,
        audit: AuditOperationsLike,
        *,
        lifecycle: LifecycleOperations | None = None,
        scope_input: ReadOnlyScopeInput = _DEFAULT_SCOPE_INPUT,
        scope_locked: bool = False,
        no_color: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Inject the same typed operation boundaries used by the CLI."""
        if no_color:
            previous_no_color = os.environ.get("NO_COLOR")
            os.environ["NO_COLOR"] = "1"
            try:
                super().__init__()
            finally:
                if previous_no_color is None:
                    os.environ.pop("NO_COLOR", None)
                else:
                    os.environ["NO_COLOR"] = previous_no_color
        else:
            super().__init__()
        self.read_only = read_only
        self.audit = audit
        self.lifecycle = lifecycle
        self.scope_input = scope_input
        self.scope_locked = scope_locked
        self._monotonic = monotonic
        self.layout_mode = "medium"
        self.active_workspace = "quotas"
        self.current_query = ReadOnlyQuotaQuery()
        self.last_result: OperationResult[Any] | None = None
        self._copy_cli_resource_scope = scope_input.explicit_resource_scope
        self._copy_cli_by_workspace: dict[str, WorkspaceCopyCli] = {}
        self.selected_quota: QuotaSelection | None = None
        self._quota_items: dict[str, QuotaQueryItem] = {}
        self._detail_route = False
        self._workload_kind: str | None = None
        self._last_focus_id: str | None = None
        self._provider_worker: Worker[Any] | None = None
        self._cancellation: CancellationToken | None = None
        self._provider_generation = 0
        self._workspace_generation = 0
        self._audit_operation_generation = 0
        self._resolved_compute: ResolvedWorkloadRequirement | None = None
        self._obtainability_state = ObtainabilityWorkflowState()
        self._active_obtainability_fingerprint: (
            ObtainabilityRequestFingerprint | None
        ) = None
        self._active_obtainability_all_compatible = False
        self._active_obtainability_inputs: dict[str, str] = {}
        self._lifecycle_state = LifecycleRouteState()
        self._post_apply_reconciliation_active = False
        self._reviewed_constraints_by_digest: dict[
            str,
            tuple[QuotaInspectSelector, ...],
        ] = {}

    @override
    def compose(self) -> ComposeResult:  # noqa: PLR0915
        """Compose one integrated instrument rather than floating cards."""
        yield Static(
            self._offline_instrument_text(),
            id="instrument-bar",
            markup=False,
        )
        with Horizontal(id="workspace-nav"):
            yield Button("Quotas", id="workspace-quotas")
            yield Button("Obtainability", id="workspace-obtainability")
            yield Button("Audit", id="workspace-audit")
        yield Static(
            "READY — provider evidence not yet observed",
            id="status-line",
            markup=False,
        )
        with Horizontal(id="quota-workbench"):
            with Vertical(id="scope-filter-rail"):
                yield Static(
                    "Filters\nService and catalog facets prune provider reads.",
                    markup=False,
                )
                yield Input(
                    placeholder="Filter quota ID, name, or dimensions",
                    id="filter-text",
                )
                yield Input(
                    placeholder="Service: compute, tpu, or blank",
                    id="filter-service",
                )
                yield Button("Apply filters", id="apply-filters")
                yield Button("Resolve Compute instance", id="resolve-compute")
                yield Button("Resolve Cloud TPU slice", id="resolve-tpu")
            with Vertical(id="quota-ledger-pane"):
                yield Static(
                    "Provider coverage: awaiting read",
                    id="coverage-summary",
                    markup=False,
                )
                yield DataTable(id="quota-ledger", cursor_type="row")
            with VerticalScroll(id="quota-detail-pane"):
                yield Static(
                    "Quota detail\nSelect an exact effective quota slice.",
                    id="quota-detail",
                    markup=False,
                )
                with Vertical(id="workload-form", classes="hidden"):
                    yield Static(
                        "Workload resolver\n"
                        "Resolve each candidate independently; no capacity claim.",
                        id="workload-breadcrumb",
                        markup=False,
                    )
                    yield Input(
                        placeholder="Machine type",
                        id="workload-machine-type",
                    )
                    yield Input(
                        placeholder="Optional attached GPU type",
                        id="workload-gpu-type",
                    )
                    yield Input(
                        placeholder="Optional attached GPU count",
                        id="workload-gpu-count",
                    )
                    yield Input(
                        placeholder="Cloud TPU accelerator type",
                        id="workload-accelerator-type",
                    )
                    yield Input(
                        placeholder="Cloud TPU topology",
                        id="workload-topology",
                    )
                    yield Input(
                        placeholder="Cloud TPU runtime version",
                        id="workload-runtime-version",
                    )
                    yield Input(
                        placeholder="Instance or slice count",
                        id="workload-count",
                    )
                    yield Input(
                        placeholder="Provisioning model",
                        id="workload-provisioning",
                    )
                    yield Input(
                        placeholder="Comma-separated candidates, or all",
                        id="workload-locations",
                    )
                    yield Button("Resolve workload", id="workload-submit")
                yield Button("Back to quota ledger", id="detail-back")
                yield Button(
                    "Compare Spot obtainability",
                    id="workload-obtainability",
                    classes="hidden",
                )
                yield Button("Copy CLI", id="copy-cli")
                yield Static(
                    "Copy CLI unavailable until an operation is fully specified.",
                    id="copy-cli-preview",
                    markup=False,
                )
        with VerticalScroll(id="lifecycle-route", classes="hidden"):
            yield Static(
                "Quotas / Lifecycle",
                id="lifecycle-breadcrumb",
                markup=False,
            )
            yield Static(
                "Resource scope is not bound.",
                id="lifecycle-scope",
                markup=False,
            )
            yield Static(
                "Open a complete Compose, Plan Review, Apply, or Watch input.",
                id="lifecycle-detail",
                markup=False,
            )
            with Vertical(id="lifecycle-controls"):
                yield Input(
                    placeholder="Type the exact resource scope to acknowledge Apply",
                    id="apply-scope-acknowledgement",
                    disabled=True,
                )
                with Horizontal():
                    yield Button(
                        "Preview exact request",
                        id="lifecycle-preview",
                        disabled=True,
                    )
                    yield Button(
                        "Apply ordered plan",
                        id="lifecycle-apply",
                        disabled=True,
                    )
                    yield Button(
                        "Start Watch",
                        id="lifecycle-watch",
                        disabled=True,
                    )
                    yield Button("Copy CLI", id="lifecycle-copy", disabled=True)
                    yield Button("Back to Quotas", id="lifecycle-back")
            yield Static(
                "Copy CLI unavailable until an operation is fully specified.",
                id="lifecycle-copy-cli",
                markup=False,
            )
            yield Static("", id="lifecycle-copy-instruction", markup=False)
        with Horizontal(id="obtainability-workspace", classes="hidden"):
            with VerticalScroll(id="obtainability-form"):
                yield Static(
                    "Obtainability / Standalone\n"
                    "Fix one exact Spot VM request. Candidate locations never expand "
                    "silently.",
                    id="obtainability-breadcrumb",
                    markup=False,
                )
                yield Input(
                    placeholder="Machine type",
                    id="obtainability-machine-type",
                )
                yield Input(
                    placeholder="Optional attached GPU type",
                    id="obtainability-gpu-type",
                )
                yield Input(
                    placeholder="Optional attached GPU count",
                    id="obtainability-gpu-count",
                )
                yield Input(
                    placeholder="VM count",
                    id="obtainability-vm-count",
                )
                yield Input(
                    placeholder="Distribution: any, any-single-zone, or balanced",
                    id="obtainability-distribution",
                )
                yield Input(
                    placeholder=(
                        "Explicit candidates separated by spaces: "
                        "REGION[=ZONE[,ZONE...]]"
                    ),
                    id="obtainability-candidates",
                )
                yield Button(
                    "Compare explicit candidates",
                    id="obtainability-compare",
                )
                yield Button(
                    "Compare all compatible locations",
                    id="obtainability-compare-all",
                )
                yield Button(
                    "Confirm inherited fields and candidate expansion",
                    id="obtainability-confirm",
                )
                yield Static(
                    "Candidate expansion: explicit candidates only.",
                    id="obtainability-expansion",
                    markup=False,
                )
            with VerticalScroll(id="obtainability-result-pane"):
                yield Static(
                    "Complete the fixed request and choose an explicit "
                    "candidate mode.\n"
                    "Obtainability is Preview evidence, not capacity.",
                    id="obtainability-detail",
                    markup=False,
                )
                yield Button("Copy CLI", id="obtainability-copy")
                yield Button(
                    "Return to Quotas / Resolve / Compute instance",
                    id="obtainability-return",
                    classes="hidden",
                )
                yield Static(
                    "Copy CLI unavailable until a comparison is fully specified.",
                    id="obtainability-copy-cli",
                    markup=False,
                )
        with Vertical(id="audit-workspace", classes="hidden"):
            yield Static(
                "Audit\nAppend-only local evidence. No provider access.",
                markup=False,
            )
            yield Button("Verify retained chain", id="audit-verify")
            yield DataTable(id="audit-table", cursor_type="row")
            yield Static(
                "Select a retained record to inspect its exact chain facts.",
                id="audit-detail",
                markup=False,
            )
        yield Footer()

    async def on_mount(self) -> None:
        """Initialize tables, layout, and the default federated inspector."""
        ledger = self.query_one("#quota-ledger", DataTable)
        ledger.add_columns(
            "Quota ID",
            "Service",
            "Location",
            "Effective",
            "Usage",
            "Catalog",
            "Guidance",
            "Mutable",
            "Status",
        )
        audit_table = self.query_one("#audit-table", DataTable)
        audit_table.add_columns("Record", "Operation", "Outcome", "Occurred")
        self._set_layout(self.size.width)
        self._set_active_workspace("quotas")
        self._start_quota_load(self.current_query)

    async def on_unmount(self) -> None:
        """Cancel active reads and release provider clients."""
        if self._cancellation is not None:
            self._cancellation.cancel()
        pending_watch = self._lifecycle_state.pending_watch
        if pending_watch is not None:
            pending_watch.cancellation.cancel()
        await self.read_only.aclose()

    def on_resize(self, event: events.Resize) -> None:
        """Adapt shell structure at the documented terminal widths."""
        self._set_layout(event.size.width)

    def _set_layout(self, width: int) -> None:
        mode = (
            "wide"
            if width >= self._WIDE_MINIMUM
            else "medium"
            if width >= self._MEDIUM_MINIMUM
            else "narrow"
        )
        self.layout_mode = mode
        self.remove_class("wide", "medium", "narrow")
        self.add_class(mode)
        self.set_class(self._detail_route and mode == "narrow", "detail-route")

    def _start_quota_load(self, query: ReadOnlyQuotaQuery) -> None:
        if self.active_workspace != "quotas":
            return
        generation = self._claim_provider_view()
        self._cancellation = CancellationToken()
        self._provider_worker = self.run_worker(
            self._load_quotas(query, self._cancellation, generation),
            group="quota-load",
            exclusive=True,
            exit_on_error=False,
        )

    def _claim_provider_view(self) -> int:
        if self._cancellation is not None:
            self._cancellation.cancel()
        self._provider_generation += 1
        return self._provider_generation

    def _owns_provider_view(self, generation: int) -> bool:
        return generation == self._provider_generation

    def _owns_quota_view(self, generation: int) -> bool:
        return self.active_workspace == "quotas" and self._owns_provider_view(
            generation
        )

    def _owns_obtainability_view(self, generation: int) -> bool:
        return self.active_workspace == "obtainability" and self._owns_provider_view(
            generation
        )

    async def _load_quotas(
        self,
        query: ReadOnlyQuotaQuery,
        cancellation: CancellationToken,
        generation: int,
    ) -> None:
        if cancellation.cancelled or not self._owns_quota_view(generation):
            return
        self._set_status("READING — querying required provider inventory")
        try:
            result = await self.read_only.browse(
                query,
                deadline=self._deadline(),
                cancellation=cancellation,
                scope_input=self.scope_input,
            )
        except asyncio.CancelledError:
            if self._owns_quota_view(generation):
                self._set_status("CANCELLED — prior provider read superseded")
            raise
        except Exception:  # noqa: BLE001 - no typed result exists for worker failure
            if self._owns_quota_view(generation):
                self._set_status(
                    "ERROR — quota inventory unavailable; retry the read-only operation"
                )
            return
        if cancellation.cancelled or not self._owns_quota_view(generation):
            if self._owns_quota_view(generation):
                self._set_status("CANCELLED — prior provider read superseded")
            return
        self.last_result = result
        self._render_instrument(result)
        self._render_quota_result(result)

    def _render_quota_result(self, result: OperationResult[Any]) -> None:
        data = result.data
        if isinstance(data, QuotaBrowseData):
            self._populate_quota_ledger(data)
            self._render_coverage(data)
            self._set_status(self._result_status(result))
            if result.resource_scope is not None:
                command = quota_list_copy_cli(
                    result.resource_scope,
                    self.current_query,
                )
                self._show_copy_cli(command)
            return
        reason = (
            data.reason
            if isinstance(data, ReadOnlyFailureData)
            else result.outcome.code.value
        )
        diagnostic_text = "\n".join(
            f"{diagnostic.severity.value.upper()} {diagnostic.code.value}: "
            f"{diagnostic.message} (retry: {diagnostic.retry.value})"
            for diagnostic in result.diagnostics
        )
        self.query_one("#quota-detail", Static).update(
            f"Quota inspector unavailable\nReason: {reason}\n"
            f"{diagnostic_text + chr(10) if diagnostic_text else ''}"
            "No provider mutation was attempted."
        )
        self._set_status(self._result_status(result))

    def _populate_quota_ledger(self, data: QuotaBrowseData) -> None:
        table = self.query_one("#quota-ledger", DataTable)
        table.clear()
        self._quota_items.clear()
        for index, item in enumerate(data.items):
            key = f"quota-{index}"
            self._quota_items[key] = item
            table.add_row(
                item.identity.quota_id,
                item.identity.service,
                item.location or "global",
                self._quantity(item.effective_value),
                self._quantity(item.usage_value),
                self._yes_no(item.predicates.cataloged),
                self._yes_no(item.predicates.guided),
                self._yes_no(item.predicates.mutable),
                (
                    f"{item.reconciliation.value} / "
                    f"{item.grant_satisfaction.value} / "
                    f"{item.effective_confirmation.value}"
                ),
                key=key,
            )
        if data.items:
            table.focus()

    def _render_coverage(self, data: QuotaBrowseData) -> None:
        aggregate = (
            "complete"
            if self.last_result and self.last_result.completeness.is_complete
            else "incomplete"
        )
        lines = [
            "Provider coverage:",
            *(
                f"{coverage.service}: {coverage.state.value} "
                f"({coverage.pages_completed}/{coverage.pages_attempted} pages)"
                for coverage in data.source_coverage
            ),
            f"Aggregate completeness: {aggregate}",
        ]
        if self.last_result is not None:
            lines.extend(
                f"{diagnostic.severity.value.upper()} "
                f"{diagnostic.code.value}: {diagnostic.message}"
                for diagnostic in self.last_result.diagnostics
            )
        self.query_one("#coverage-summary", Static).update("  ".join(lines))

    def _render_instrument(self, result: OperationResult[Any]) -> None:
        if result.resource_scope is not None:
            self._copy_cli_resource_scope = result.resource_scope
        scope = (
            result.resource_scope.canonical_name
            if result.resource_scope is not None
            else self._scope_label()
        )
        identity = result.identity_evidence
        if identity is None or identity.acting_principal is None:
            principal = "acting principal unavailable"
        else:
            principal = identity.acting_principal.value
        lock = "LOCKED" if self.scope_locked else "unlocked"
        complete = "complete" if result.completeness.is_complete else "INCOMPLETE"
        self.query_one("#instrument-bar", Static).update(
            f"Resource scope: {scope} [{lock}]\n"
            f"Acting principal: {principal} · Evidence: {complete}"
        )

    def _offline_instrument_text(self) -> str:
        return (
            f"Resource scope: {self._scope_label()} "
            f"[{'LOCKED' if self.scope_locked else 'unlocked'}]\n"
            "Acting principal: deferred (offline) · Evidence: not observed"
        )

    def _scope_label(self) -> str:
        explicit = self.scope_input.explicit_resource_scope
        return explicit.canonical_name if explicit is not None else "not selected"

    def _set_status(self, text: str) -> None:
        self.query_one("#status-line", Static).update(text)

    @staticmethod
    def _result_status(result: OperationResult[Any]) -> str:
        if result.succeeded:
            return "COMPLETE — operation boundary reached"
        return (
            f"{result.outcome.code.value.upper()} — "
            f"exit {int(result.outcome.exit_class)}; "
            "evidence "
            f"{'complete' if result.completeness.is_complete else 'incomplete'}"
        )

    def open_compose(
        self,
        request: ComposeRequest,
        *,
        preview: PreviewRequest | None = None,
        copy_cli: str | None = None,
    ) -> Composition:
        """Open one exact typed composition without inventing missing evidence."""
        lifecycle = self._require_lifecycle()
        composition = lifecycle.compose(request)
        self._enter_lifecycle_route(
            LifecycleRoute.COMPOSE,
            request.resource_scope,
        )
        self._lifecycle_state.pending_preview = preview
        self._render_composition(composition)
        self.query_one("#lifecycle-preview", Button).disabled = preview is None
        self._set_lifecycle_copy_cli(copy_cli)
        self._set_status(
            "COMPOSED — review every ordered child before Preview"
            if composition.reached
            else "COMPOSE REJECTED — required evidence or acknowledgement is missing"
        )
        return composition

    def open_preview(
        self,
        request: PreviewRequest,
        *,
        copy_cli: str | None = None,
    ) -> OperationResult[PreviewData]:
        """Run Preview and retain the exact surface-neutral result."""
        lifecycle = self._require_lifecycle()
        self._enter_lifecycle_route(
            LifecycleRoute.COMPOSE,
            request.composition.resource_scope,
        )
        result = lifecycle.preview(request)
        self.last_result = result
        self._render_preview(result)
        self._set_lifecycle_copy_cli(copy_cli)
        return result

    def open_plan_review(
        self,
        request: PlanReviewRequest,
    ) -> OperationResult[PlanReviewData]:
        """Review one digest or portable plan without applying it."""
        lifecycle = self._require_lifecycle()
        result = lifecycle.review(request)
        self.last_result = result
        review = result.data.review
        scope = review.plan.resource_scope if review is not None else None
        self._enter_lifecycle_route(LifecycleRoute.PLAN_REVIEW, scope)
        if review is not None:
            self._reviewed_constraints_by_digest[review.digest] = (
                self._selectors_for_identities(
                    tuple(
                        constraint.slice_identity
                        for constraint in review.plan.constraints
                    )
                )
            )
        self._render_plan_review(result)
        self._set_lifecycle_copy_cli(
            plan_review_copy_cli(digest=request.digest, path=request.path)
        )
        return result

    def prepare_apply(self, request: ApplyRequest) -> None:
        """Require a fresh typed scope acknowledgement before dispatch."""
        self._require_lifecycle()
        scope = request.resource_scope_acknowledgement
        self._enter_lifecycle_route(LifecycleRoute.APPLY, scope)
        self._lifecycle_state.pending_apply = request
        self._lifecycle_state.affected_selectors = (
            self._reviewed_constraints_by_digest.get(request.digest, ())
        )
        acknowledgement = self.query_one(
            "#apply-scope-acknowledgement",
            Input,
        )
        acknowledgement.value = ""
        acknowledgement.disabled = False
        acknowledgement.focus()
        self.query_one("#lifecycle-apply", Button).disabled = True
        self.query_one("#lifecycle-detail", Static).update(
            "Apply confirmation\n"
            f"Bound resource scope: {scope.canonical_name}\n"
            "Type the exact resource scope below. This value is never prefilled.\n"
            "Apply is ordered and non-atomic. Accepted earlier children are not "
            "rolled back if a later child fails or becomes unknown."
        )
        self._set_lifecycle_copy_cli(plan_apply_copy_cli(digest=request.digest))
        self._set_status("CONFIRMATION REQUIRED — no provider dispatch has started")

    def prepare_watch(
        self,
        request: WatchRequest,
        *,
        bound_scope: ResourceScope,
        copy_cli: str | None = None,
    ) -> None:
        """Prepare one explicit condition and deadline without polling yet."""
        self._require_lifecycle()
        self._enter_lifecycle_route(LifecycleRoute.WATCH, bound_scope)
        self._lifecycle_state.pending_watch = request
        self.query_one("#lifecycle-watch", Button).disabled = False
        selector = (
            f"intent={request.intent_id}"
            if request.intent_id is not None
            else "resume=opaque authenticated token"
        )
        condition = (
            request.condition.value
            if request.condition is not None
            else "recovered from resume token"
        )
        self.query_one("#lifecycle-detail", Static).update(
            "Watch confirmation\n"
            f"Subject: {selector}\n"
            f"Condition: {condition}\n"
            f"Deadline: explicit monotonic {request.deadline}\n"
            "Polling begins only after Start Watch."
        )
        self._set_lifecycle_copy_cli(copy_cli)
        self._set_status("WATCH READY — condition and deadline are explicit")

    def _require_lifecycle(self) -> LifecycleOperations:
        lifecycle = self.lifecycle
        if lifecycle is None:
            msg = "mutation and lifecycle operations are unavailable"
            raise RuntimeError(msg)
        return lifecycle

    def _enter_lifecycle_route(
        self,
        route: LifecycleRoute,
        scope: ResourceScope | None,
    ) -> None:
        pending_watch = self._lifecycle_state.pending_watch
        if pending_watch is not None:
            pending_watch.cancellation.cancel()
        if self._lifecycle_state.route is None:
            previous_lock = self.scope_locked
            focused = self.focused
            previous_focus_id = None if focused is None else focused.id
        else:
            previous_lock = self._lifecycle_state.previous_scope_locked
            previous_focus_id = self._lifecycle_state.previous_focus_id
        self._claim_provider_view()
        self.active_workspace = "quotas"
        self._lifecycle_state = LifecycleRouteState(
            route=route,
            bound_scope=scope,
            previous_scope_locked=previous_lock,
            previous_focus_id=previous_focus_id,
        )
        self.scope_locked = scope is not None
        self.query_one("#quota-workbench").add_class("hidden")
        self.query_one("#obtainability-workspace").add_class("hidden")
        self.query_one("#audit-workspace").add_class("hidden")
        self.query_one("#lifecycle-route").remove_class("hidden")
        for name in ("quotas", "obtainability", "audit"):
            self.query_one(f"#workspace-{name}", Button).set_class(
                name == "quotas",
                "active-workspace",
            )
        self.query_one("#lifecycle-breadcrumb", Static).update(
            f"Quotas / {route.value.replace('-', ' ').title()}"
        )
        self.query_one("#lifecycle-scope", Static).update(
            "Resource scope: "
            + (scope.canonical_name if scope is not None else "unavailable")
            + (" [LOCKED]" if scope is not None else " [unbound]")
        )
        self._render_lifecycle_instrument(scope)
        for button_id in (
            "lifecycle-preview",
            "lifecycle-apply",
            "lifecycle-watch",
            "lifecycle-copy",
        ):
            self.query_one(f"#{button_id}", Button).disabled = True
        acknowledgement = self.query_one("#apply-scope-acknowledgement", Input)
        acknowledgement.value = ""
        acknowledgement.disabled = True
        self.query_one("#lifecycle-copy-cli", Static).update(
            "Copy CLI unavailable until an operation is fully specified."
        )
        self.query_one("#lifecycle-copy-instruction", Static).update("")

    def _clear_lifecycle_route(self) -> None:
        pending_watch = self._lifecycle_state.pending_watch
        if pending_watch is not None:
            pending_watch.cancellation.cancel()
        previous_lock = self._lifecycle_state.previous_scope_locked
        self._lifecycle_state = LifecycleRouteState()
        self.scope_locked = previous_lock
        self.query_one("#lifecycle-route").add_class("hidden")

    def _render_lifecycle_instrument(self, scope: ResourceScope | None) -> None:
        self.query_one("#instrument-bar", Static).update(
            "Resource scope: "
            + (scope.canonical_name if scope is not None else "unavailable")
            + (" [LOCKED]\n" if scope is not None else " [unbound]\n")
            + "Acting principal: retained from typed operation evidence"
        )

    def _leave_lifecycle_route(self) -> None:
        route = self._lifecycle_state.route
        bound_scope = self._lifecycle_state.bound_scope
        affected = self._lifecycle_state.affected_selectors
        apply_result = self._lifecycle_state.apply_result
        previous_focus_id = self._lifecycle_state.previous_focus_id
        self._clear_lifecycle_route()
        self.query_one("#quota-workbench").remove_class("hidden")
        self.query_one("#instrument-bar", Static).update(
            self._offline_instrument_text()
        )
        if (
            route is LifecycleRoute.APPLY
            and affected
            and apply_result is not None
            and (apply_result.resource_scope is not None or bound_scope is not None)
        ):
            apply_scope = bound_scope or apply_result.resource_scope
            if apply_scope is None:  # pragma: no cover - guarded above
                msg = "post-Apply reconciliation requires a resource scope"
                raise RuntimeError(msg)
            scope_input = ReadOnlyScopeInput(explicit_resource_scope=apply_scope)
            self.scope_input = scope_input
            self._copy_cli_resource_scope = apply_scope
            previous_selection = self.selected_quota
            if (
                previous_selection is not None
                and previous_selection.item.identity.resource_scope != apply_scope
            ):
                previous_selection = None
                self.selected_quota = None
            generation = self._claim_provider_view()
            cancellation = CancellationToken()
            self._cancellation = cancellation
            self._post_apply_reconciliation_active = True
            self._set_status(f"REFRESHING — re-reading {len(affected)} affected slices")
            self.run_worker(
                self._reconcile_affected_slices(
                    affected,
                    apply_result,
                    self.current_query,
                    previous_selection,
                    previous_focus_id,
                    cancellation,
                    generation,
                    scope_input,
                ),
                group="post-apply-refresh",
                exclusive=True,
                exit_on_error=False,
            )
            return
        self._set_status("READY — returned to Quotas")
        if self.selected_quota is not None:
            self.query_one("#quota-detail-pane").scroll_home(animate=False)
        self.query_one("#quota-ledger", DataTable).focus()

    async def _reconcile_affected_slices(  # noqa: PLR0913
        self,
        selectors: tuple[QuotaInspectSelector, ...],
        apply_result: OperationResult[ApplyData],
        query: ReadOnlyQuotaQuery,
        previous_selection: QuotaSelection | None,
        previous_focus_id: str | None,
        cancellation: CancellationToken,
        generation: int,
        scope_input: ReadOnlyScopeInput,
    ) -> None:
        """Re-read affected slices while retaining the navigation guard."""
        try:
            await self._perform_affected_slice_reconciliation(
                selectors,
                apply_result,
                query,
                previous_selection,
                previous_focus_id,
                cancellation,
                generation,
                scope_input,
            )
        finally:
            self._post_apply_reconciliation_active = False

    async def _perform_affected_slice_reconciliation(  # noqa: PLR0913
        self,
        selectors: tuple[QuotaInspectSelector, ...],
        apply_result: OperationResult[ApplyData],
        query: ReadOnlyQuotaQuery,
        previous_selection: QuotaSelection | None,
        previous_focus_id: str | None,
        cancellation: CancellationToken,
        generation: int,
        scope_input: ReadOnlyScopeInput,
    ) -> None:
        """Re-read every affected slice, then refresh the preserved ledger query."""
        apply_data = apply_result.data
        refreshed: list[tuple[QuotaInspectSelector, OperationResult[Any]]] = []
        for selector in selectors:
            if cancellation.cancelled or not self._owns_quota_view(generation):
                return
            result = await self.read_only.inspect(
                selector,
                deadline=self._deadline(),
                cancellation=cancellation,
                scope_input=scope_input,
            )
            refreshed.append((selector, result))
        if cancellation.cancelled or not self._owns_quota_view(generation):
            return
        browse = await self.read_only.browse(
            query,
            deadline=self._deadline(),
            cancellation=cancellation,
            scope_input=scope_input,
        )
        if cancellation.cancelled or not self._owns_quota_view(generation):
            return
        self.last_result = browse
        self._render_instrument(browse)
        self._render_quota_result(browse)
        self._restore_quota_selection(previous_selection, browse)
        lines = ["Post-Apply affected-slice refresh"]
        for selector, result in refreshed:
            lines.append(
                f"{selector.service} / {selector.quota_id} / "
                f"{selector.location}: {result.outcome.code.value}"
            )
            child = next(
                (
                    item
                    for item in apply_data.children
                    if self._selector_for_identity(item.slice_identity) == selector
                ),
                None,
            )
            if child is not None:
                lines.extend(self._post_apply_child_lines(child))
                continue
            no_op = next(
                (
                    item
                    for item in apply_data.verified_no_ops
                    if self._selector_for_identity(item.slice_identity) == selector
                ),
                None,
            )
            if no_op is not None:
                lines.extend(self._post_apply_no_op_lines(no_op))
        lines.extend(self._result_fact_lines(apply_result))
        self.query_one("#quota-detail", Static).update("\n".join(lines))
        self._set_status(
            f"REFRESHED — {len(refreshed)}/{len(selectors)} affected slices; "
            "query and selection preserved"
        )
        if previous_focus_id is not None:
            self.query_one(f"#{previous_focus_id}").focus()
        else:
            self.query_one("#quota-ledger", DataTable).focus()

    def _restore_quota_selection(
        self,
        previous: QuotaSelection | None,
        result: OperationResult[Any],
    ) -> None:
        if previous is None or not isinstance(result.data, QuotaBrowseData):
            return
        for index, item in enumerate(result.data.items):
            selector = QuotaInspectSelector(
                item.identity.service,
                item.identity.quota_id,
                item.location or "global",
                item.identity.dimensions,
            )
            if selector == previous.selector:
                self.selected_quota = QuotaSelection(item, selector)
                self.query_one("#quota-ledger", DataTable).move_cursor(
                    row=index,
                    column=0,
                )
                return

    def _render_composition(self, composition: Composition) -> None:
        request = composition.request
        lines = [
            f"{request.kind.value.title()} request composition",
            f"Target strategy: {request.strategy.value}",
            f"Resource scope: {request.resource_scope.canonical_name}",
            f"Expert mode: {self._yes_no(request.expert)}",
        ]
        if request.selected_location is not None:
            lines.append(f"Selected location: {request.selected_location}")
        lines.append(
            "Supplied acknowledgements: "
            + (", ".join(request.acknowledgements) or "none")
        )
        if composition.incapability_reasons:
            lines.append(
                "Cannot Preview: " + ", ".join(composition.incapability_reasons)
            )
        for index, child in enumerate(composition.children, start=1):
            source = next(
                item for item in request.children if item.child_id == child.child_id
            )
            lines.extend(
                (
                    f"{index}. {child.child_id}",
                    f"   Slice: {child.slice_identity.service} / "
                    f"{child.slice_identity.quota_id}",
                    f"   Dimensions: {self._dimensions(child.slice_identity)}",
                    f"   Quota scope: {child.slice_identity.quota_scope.value}",
                    f"   Target: {self._quantity(child.target)}",
                    f"   Effective: {self._quantity(child.effective)}",
                    f"   Usage: {self._quantity(child.usage)}",
                    f"   Preference: {source.preference_name or 'none'}",
                    f"   Preference ETag: {source.preference_etag or 'none'}",
                    "   Observed at: "
                    + (
                        source.observed_at.isoformat()
                        if source.observed_at is not None
                        else "unavailable"
                    ),
                    "   Disposition: "
                    + ("verified no-op" if child.no_op else "mutation"),
                    "   Warnings: " + (", ".join(source.warnings) or "none"),
                    "   Required acknowledgements: "
                    + (", ".join(child.required_acknowledgements) or "none"),
                )
            )
        lines.append(
            "Apply consequence: ordered and non-atomic; accepted earlier children "
            "are never rolled back."
        )
        self.query_one("#lifecycle-detail", Static).update("\n".join(lines))

    def _render_preview(self, result: OperationResult[PreviewData]) -> None:
        self._render_instrument(result)
        data = result.data
        self._render_composition(data.composition)
        detail = str(self.query_one("#lifecycle-detail", Static).content)
        lines = [
            detail,
            f"Preview outcome: {result.outcome.code.value}",
            f"Plan digest: {data.plan_digest or 'none (verified no-op)'}",
            f"Apply capability: {self._yes_no(data.apply_capability)}",
            f"Audit record: {data.audit_record_id or 'none'}",
        ]
        if data.plan is not None:
            lines.append(
                "Issuing installation trust: "
                + ("Apply-capable" if data.apply_capability else "not Apply-capable")
            )
            lines.extend(self._preview_plan_fact_lines(data.plan, result.finished_at))
        lines.extend(self._result_fact_lines(result))
        self.query_one("#lifecycle-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))

    def _render_plan_review(
        self,
        result: OperationResult[PlanReviewData],
    ) -> None:
        self._render_instrument(result)
        review = result.data.review
        lines = [
            "Plan Review",
            f"Outcome: {result.outcome.code.value}",
        ]
        if review is None:
            lines.append("Canonical plan contents are not trustworthy.")
        else:
            plan = review.plan
            lines.extend(
                (
                    f"Kind: {plan.kind.value}",
                    f"Digest: {review.digest}",
                    f"Resource scope: {plan.resource_scope.canonical_name}",
                    f"Target strategy: {plan.target_strategy.value}",
                    f"Principal: {plan.principal.stable_identity}",
                    f"Quota contact source: {plan.contact_binding.source.value}",
                    f"Expires: {plan.expires_at.isoformat()}",
                    f"Authenticated: {self._yes_no(review.authenticated)}",
                    f"State: {review.state.value}",
                    f"Apply capability: {self._yes_no(review.apply_capability)}",
                    "Incapability reasons: "
                    + (
                        ", ".join(item.value for item in review.incapability_reasons)
                        or "none"
                    ),
                )
            )
            for index, child in enumerate(plan.children, start=1):
                dimensions = self._dimensions(child.slice_identity)
                lines.extend(
                    (
                        f"{index}. {child.child_id}",
                        f"   Slice: {child.slice_identity.service} / "
                        f"{child.slice_identity.quota_id}",
                        f"   Dimensions: {dimensions}",
                        f"   Target: {self._quantity(child.target)}",
                        f"   Effective: {self._quantity(child.effective)}",
                        f"   Usage: {self._quantity(child.usage)}",
                        f"   Workload: {self._quantity(child.workload)}",
                        f"   Prior desired: {self._quantity(child.prior_desired)}",
                        f"   Granted: {self._quantity(child.granted)}",
                        f"   Derivation: {child.target_derivation.value}",
                        f"   Preference: {child.preference_name or 'none'}",
                        f"   Preference ETag: {child.preference_etag or 'none'}",
                        "   Preference state: "
                        + (
                            "bound"
                            if child.preference_name is not None
                            else "not retained"
                        ),
                        "   Warnings: "
                        + (", ".join(item.value for item in child.warnings) or "none"),
                        "   Required acknowledgements: "
                        + (
                            ", ".join(
                                item.value for item in child.required_acknowledgements
                            )
                            or "none"
                        ),
                        "   Supplied acknowledgements: "
                        + (
                            ", ".join(item.value for item in child.acknowledgements)
                            or "none"
                        ),
                        "   Unresolved acknowledgements: "
                        + (
                            ", ".join(
                                item.value for item in child.unresolved_acknowledgements
                            )
                            or "none"
                        ),
                    )
                )
                for evidence in child.evidence:
                    age = max(
                        0.0,
                        (result.finished_at - evidence.observed_at).total_seconds(),
                    )
                    lines.extend(
                        (
                            f"   Evidence {evidence.name.value}: "
                            f"{evidence.value_digest}",
                            f"      Observed: {evidence.observed_at.isoformat()}",
                            f"      Age: {age} seconds",
                        )
                    )
            for index, child in enumerate(
                getattr(plan, "no_op_children", ()),
                start=1,
            ):
                dimensions = self._dimensions(child.slice_identity)
                lines.extend(
                    (
                        f"No-op {index}. {child.child_id}",
                        f"   Slice: {child.slice_identity.service} / "
                        f"{child.slice_identity.quota_id}",
                        f"   Dimensions: {dimensions}",
                        f"   Target: {self._quantity(child.target)}",
                        f"   Effective: {self._quantity(child.effective)}",
                        f"   Usage: {self._quantity(child.usage)}",
                        f"   Workload: {self._quantity(child.workload)}",
                        f"   Prior desired: {self._quantity(child.prior_desired)}",
                        f"   Granted: {self._quantity(child.granted)}",
                        f"   Derivation: {child.target_derivation.value}",
                        f"   Preference: {child.preference_name or 'none'}",
                        f"   Preference ETag: {child.preference_etag or 'none'}",
                        "   Disposition: verified no-op; no provider dispatch",
                        "   Required acknowledgements: "
                        + (
                            ", ".join(
                                item.value for item in child.required_acknowledgements
                            )
                            or "none"
                        ),
                        "   Supplied acknowledgements: "
                        + (
                            ", ".join(item.value for item in child.acknowledgements)
                            or "none"
                        ),
                        "   Unresolved acknowledgements: "
                        + (
                            ", ".join(
                                item.value for item in child.unresolved_acknowledgements
                            )
                            or "none"
                        ),
                    )
                )
                for evidence in child.evidence:
                    age = max(
                        0.0,
                        (result.finished_at - evidence.observed_at).total_seconds(),
                    )
                    lines.extend(
                        (
                            f"   Evidence {evidence.name.value}: "
                            f"{evidence.value_digest}",
                            f"      Observed: {evidence.observed_at.isoformat()}",
                            f"      Age: {age} seconds",
                        )
                    )
            for index, constraint in enumerate(plan.constraints, start=1):
                identity = constraint.slice_identity
                lines.extend(
                    (
                        f"Constraint {index}: {identity.service} / {identity.quota_id}",
                        f"   Dimensions: {self._dimensions(identity)}",
                    )
                )
            lines.append(
                "Apply is ordered and non-atomic. Accepted earlier children are "
                "not rolled back."
            )
        lines.extend(self._result_fact_lines(result))
        self.query_one("#lifecycle-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))

    def _submit_lifecycle_preview(self) -> None:
        request = self._lifecycle_state.pending_preview
        if request is None:
            self._set_status("PREVIEW UNAVAILABLE — complete the request first")
            return
        self.open_preview(request, copy_cli=self._lifecycle_copy_cli())

    def _submit_lifecycle_apply(self) -> None:
        request = self._lifecycle_state.pending_apply
        if request is None:
            self._set_status("APPLY UNAVAILABLE — review an Apply-capable plan")
            return
        self.query_one("#lifecycle-apply", Button).disabled = True
        self.query_one("#apply-scope-acknowledgement", Input).disabled = True
        self._lifecycle_state.apply_in_progress = True
        self._set_status("APPLYING — revalidating every child before dispatch")
        self.run_worker(
            self._apply_lifecycle(request),
            group="lifecycle-apply",
            exclusive=True,
            exit_on_error=False,
        )

    async def _apply_lifecycle(self, request: ApplyRequest) -> None:
        lifecycle = self._require_lifecycle()
        result = await lifecycle.apply(request)
        if (
            self._lifecycle_state.route is not LifecycleRoute.APPLY
            or self._lifecycle_state.pending_apply is not request
        ):
            return
        self.last_result = result
        self._lifecycle_state.apply_in_progress = False
        self._lifecycle_state.apply_result = result
        self._lifecycle_state.affected_selectors = self._merge_selectors(
            self._lifecycle_state.affected_selectors,
            self._selectors_for_identities(
                tuple(
                    child.slice_identity
                    for child in (*result.data.children, *result.data.verified_no_ops)
                )
            ),
        )
        self._render_apply_result(result)
        self._set_status(self._result_status(result))
        self._leave_lifecycle_route()

    def _render_apply_result(self, result: OperationResult[ApplyData]) -> None:
        self._render_instrument(result)
        data = result.data
        lines = [
            "Apply result",
            f"Outcome: {result.outcome.code.value}",
            f"Kind: {data.kind.value if data.kind is not None else 'unavailable'}",
            f"Plan digest: {data.plan_digest}",
            f"Intent ID: {data.intent_id or 'none'}",
            "Aggregate success: " + self._yes_no(result.succeeded),
        ]
        for index, child in enumerate(data.children, start=1):
            lines.extend(
                (
                    f"{index}. {child.child_id}",
                    f"   Disposition: {child.disposition.value}",
                    f"   Slice: {child.slice_identity.service} / "
                    f"{child.slice_identity.quota_id}",
                    f"   Dimensions: {self._dimensions(child.slice_identity)}",
                    f"   Quota scope: {child.slice_identity.quota_scope.value}",
                    f"   Target: {self._quantity(child.target)}",
                    f"   Preference: {child.preference_identity}",
                    f"   ETag: {child.etag or 'none'}",
                    f"   Trace ID: {child.trace_id or 'none'}",
                    "   Provider outcome: "
                    + (
                        child.provider_outcome.value
                        if child.provider_outcome
                        else "none"
                    ),
                    "   Unknown resolution: "
                    + (
                        child.unknown_resolution.value
                        if child.unknown_resolution is not None
                        else "none"
                    ),
                    "   Audit record IDs: "
                    + (", ".join(child.audit_record_ids) or "none"),
                    "   Submitted at: "
                    + (
                        child.submitted_at.isoformat()
                        if child.submitted_at is not None
                        else "none"
                    ),
                    "   Warnings: "
                    + (", ".join(item.value for item in child.warnings) or "none"),
                    "   Required acknowledgements: "
                    + (
                        ", ".join(
                            item.value for item in child.required_acknowledgements
                        )
                        or "none"
                    ),
                    "   Supplied acknowledgements: "
                    + (
                        ", ".join(item.value for item in child.acknowledgements)
                        or "none"
                    ),
                    "   Unresolved acknowledgements: "
                    + (
                        ", ".join(
                            item.value for item in child.unresolved_acknowledgements
                        )
                        or "none"
                    ),
                    "   Watchable now: "
                    + self._yes_no(
                        child.disposition is ApplyChildDisposition.ACCEPTED
                        or (
                            child.disposition is ApplyChildDisposition.UNKNOWN
                            and child.unknown_resolution
                            is UnknownDispatchResolution.ACCEPTED
                        )
                    ),
                )
            )
        for index, child in enumerate(data.verified_no_ops, start=1):
            lines.extend(
                (
                    f"No-op {index}. {child.child_id}",
                    f"   Slice: {child.slice_identity.service} / "
                    f"{child.slice_identity.quota_id}",
                    f"   Dimensions: {self._dimensions(child.slice_identity)}",
                    f"   Target: {self._quantity(child.target)}",
                    f"   Effective: {self._quantity(child.effective)}",
                    f"   Usage: {self._quantity(child.usage)}",
                    f"   Workload: {self._quantity(child.workload)}",
                    f"   Prior desired: {self._quantity(child.prior_desired)}",
                    f"   Granted: {self._quantity(child.granted)}",
                    f"   Derivation: {child.target_derivation.value}",
                    f"   Preference: {child.preference_name or 'none'}",
                    f"   Preference ETag: {child.preference_etag or 'none'}",
                    "   Disposition: verified no-op; no provider dispatch",
                    "   Warnings: "
                    + (", ".join(item.value for item in child.warnings) or "none"),
                    "   Required acknowledgements: "
                    + (
                        ", ".join(
                            item.value for item in child.required_acknowledgements
                        )
                        or "none"
                    ),
                    "   Supplied acknowledgements: "
                    + (
                        ", ".join(item.value for item in child.acknowledgements)
                        or "none"
                    ),
                    "   Unresolved acknowledgements: "
                    + (
                        ", ".join(
                            item.value for item in child.unresolved_acknowledgements
                        )
                        or "none"
                    ),
                )
            )
            for evidence in child.evidence:
                age = max(
                    0.0,
                    (result.finished_at - evidence.observed_at).total_seconds(),
                )
                lines.extend(
                    (
                        f"   Evidence {evidence.name.value}: {evidence.value_digest}",
                        f"      Observed: {evidence.observed_at.isoformat()}",
                        f"      Age: {age} seconds",
                    )
                )
        if data.quarantine_identity is not None:
            lines.append(f"Quarantine: {data.quarantine_identity}")
        lines.append(
            "Audit record IDs: " + (", ".join(data.audit_record_ids) or "none")
        )
        lines.append(
            "Accepted children remain accepted. Failed, unknown, and unattempted "
            "children remain distinct."
        )
        lines.extend(self._result_fact_lines(result))
        self.query_one("#lifecycle-detail", Static).update("\n".join(lines))
        self.query_one("#apply-scope-acknowledgement", Input).disabled = True
        self.query_one("#lifecycle-apply", Button).disabled = True

    def _submit_lifecycle_watch(self) -> None:
        request = self._lifecycle_state.pending_watch
        if request is None:
            self._set_status("WATCH UNAVAILABLE — select a durable Apply intent")
            return
        self.query_one("#lifecycle-watch", Button).disabled = True
        self._set_status("WATCHING — awaiting the initial authoritative observation")
        self.run_worker(
            self._watch_lifecycle(request),
            group="lifecycle-watch",
            exclusive=True,
            exit_on_error=False,
        )

    async def _watch_lifecycle(self, request: WatchRequest) -> None:
        lifecycle = self._require_lifecycle()
        try:
            async for event in lifecycle.watch(request):
                if (
                    self._lifecycle_state.route is not LifecycleRoute.WATCH
                    or self._lifecycle_state.pending_watch is not request
                ):
                    return
                self._lifecycle_state.latest_resume = event.resume
                self._render_watch_event(event)
        except WatchStartError as error:
            if (
                self._lifecycle_state.route is LifecycleRoute.WATCH
                and self._lifecycle_state.pending_watch is request
            ):
                self._set_status(
                    f"{error.code.value.upper()} — exit {int(error.exit_class)}"
                )

    def _render_watch_event(self, event: WatchStreamEvent) -> None:
        subject = event.subject
        lines = [
            "Watch",
            f"Event: {event.event.value}",
            f"Sequence: {event.sequence}",
            f"Stream ID: {event.stream_id}",
            f"Observed at: {event.observed_at.isoformat()}",
            f"Subject kind: {subject.kind.value}",
            f"Intent ID: {subject.intent_id}",
            f"Resource scope: {subject.resource_scope.canonical_name}",
            f"Plan digest: {subject.plan_digest}",
            f"Condition: {subject.condition.value}",
            f"Aggregate: {event.aggregate.disposition.value}",
            f"Accepted Watch set: {event.aggregate.accepted_children}",
        ]
        for index, summary in enumerate(event.aggregate.children, start=1):
            child = summary.child
            lines.extend(
                (
                    f"{index}. {child.child_id}",
                    f"   Slice: {child.slice_identity.service} / "
                    f"{child.slice_identity.quota_id}",
                    f"   Dimensions: {self._dimensions(child.slice_identity)}",
                    f"   Quota scope: {child.slice_identity.quota_scope.value}",
                    f"   Target: {self._quantity(child.target)}",
                    f"   Preference: {child.preference_identity}",
                    f"   Lineage ETag: {child.lineage_etag or 'none'}",
                    f"   Lineage trace ID: {child.lineage_trace_id or 'none'}",
                    f"   Baseline: {self._quantity(child.baseline)}",
                    f"   Apply disposition: {child.disposition.value}",
                    "   Unknown resolution: "
                    + (
                        child.unknown_resolution.value
                        if child.unknown_resolution is not None
                        else "none"
                    ),
                    f"   Resolution checkpoint: {child.resolution_checkpoint}",
                    f"   Watchable now: {self._yes_no(child.watchable)}",
                )
            )
            status = summary.status
            if status is None:
                lines.append("   Lifecycle: not in accepted Watch set")
            else:
                lines.extend(
                    (
                        f"   Reconciliation: {status.reconciliation.value}",
                        f"   Grant satisfaction: {status.grant_satisfaction.value}",
                        "   Effective confirmation: "
                        f"{status.effective_confirmation.value}",
                        f"   Desired: {self._quantity(status.desired)}",
                        f"   Granted: {self._quantity(status.granted)}",
                        f"   Effective: {self._quantity(status.effective)}",
                        f"   Status observed: {status.status_observed_at.isoformat()}",
                        "   Effective observed: "
                        + (
                            status.effective_observed_at.isoformat()
                            if status.effective_observed_at is not None
                            else "none"
                        ),
                    )
                )
        lines.extend(
            self._diagnostic_fact_lines(
                event.diagnostics,
                label="Event diagnostic",
            )
        )
        lines.append("Resume token: available (opaque, authenticated, non-secret)")
        if event.result is not None:
            self.last_result = event.result
            lines.extend(
                (
                    f"Terminal outcome: {event.result.outcome.code.value}",
                    f"Deadline: {event.result.data.deadline.isoformat()}",
                    f"Elapsed seconds: {event.result.data.elapsed_seconds}",
                    "Last material observation: "
                    f"{event.result.data.last_material_observed_at.isoformat()}",
                )
            )
            lines.extend(self._result_fact_lines(event.result))
            self._set_status(self._result_status(event.result))
        else:
            self._set_status(
                f"WATCH {event.event.value.upper()} — "
                f"{event.aggregate.disposition.value}"
            )
        self.query_one("#lifecycle-detail", Static).update("\n".join(lines))

    def _lifecycle_copy_cli(self) -> str | None:
        content = self.query_one("#lifecycle-copy-cli", Static).content
        text = str(content)
        return None if text.startswith("Copy CLI unavailable") else text

    def _set_lifecycle_copy_cli(self, command: str | None) -> None:
        if command is not None and (not isinstance(command, str) or not command):
            msg = "lifecycle Copy CLI command must be non-empty or None"
            raise ValueError(msg)
        self.query_one("#lifecycle-copy-cli", Static).update(
            command or "Copy CLI unavailable until an operation is fully specified."
        )
        arguments = () if command is None else tuple(shlex.split(command))
        self.query_one("#lifecycle-copy-instruction", Static).update(
            (
                "Before running this command, provide exactly one protected "
                "quota-contact line on standard input. This instruction is "
                "display-only and is not copied."
            )
            if "--quota-contact-stdin" in arguments
            else ""
        )
        self.query_one("#lifecycle-copy", Button).disabled = command is None

    @classmethod
    def _selectors_for_identities(
        cls,
        identities: tuple[EffectiveQuotaSliceIdentity, ...],
    ) -> tuple[QuotaInspectSelector, ...]:
        return cls._merge_selectors(
            (),
            tuple(cls._selector_for_identity(identity) for identity in identities),
        )

    @classmethod
    def _selector_for_identity(
        cls,
        identity: EffectiveQuotaSliceIdentity,
    ) -> QuotaInspectSelector:
        return QuotaInspectSelector(
            identity.service,
            identity.quota_id,
            cls._slice_location(identity),
            identity.dimensions,
        )

    @staticmethod
    def _post_apply_child_lines(child: ApplyChildData) -> tuple[str, ...]:
        return (
            f"   Apply child {child.child_id}: {child.disposition.value}",
            "   Provider outcome: "
            + (
                child.provider_outcome.value
                if child.provider_outcome is not None
                else "none"
            ),
            f"   Preference: {child.preference_identity}",
            f"   ETag: {child.etag or 'none'}",
            f"   Trace ID: {child.trace_id or 'none'}",
            "   Unknown resolution: "
            + (
                child.unknown_resolution.value
                if child.unknown_resolution is not None
                else "none"
            ),
            "   Submitted at: "
            + (
                child.submitted_at.isoformat()
                if child.submitted_at is not None
                else "none"
            ),
            "   Warnings: "
            + (", ".join(item.value for item in child.warnings) or "none"),
            "   Required acknowledgements: "
            + (
                ", ".join(item.value for item in child.required_acknowledgements)
                or "none"
            ),
            "   Supplied acknowledgements: "
            + (", ".join(item.value for item in child.acknowledgements) or "none"),
            "   Unresolved acknowledgements: "
            + (
                ", ".join(item.value for item in child.unresolved_acknowledgements)
                or "none"
            ),
            "   Audit record IDs: " + (", ".join(child.audit_record_ids) or "none"),
        )

    @staticmethod
    def _post_apply_no_op_lines(child: QuotaRequestPlanChild) -> tuple[str, ...]:
        return (
            f"   Apply child {child.child_id}: verified no-op",
            "   Submitted at: none (no provider dispatch)",
            "   Warnings: "
            + (", ".join(item.value for item in child.warnings) or "none"),
            "   Required acknowledgements: "
            + (
                ", ".join(item.value for item in child.required_acknowledgements)
                or "none"
            ),
            "   Supplied acknowledgements: "
            + (", ".join(item.value for item in child.acknowledgements) or "none"),
            "   Unresolved acknowledgements: "
            + (
                ", ".join(item.value for item in child.unresolved_acknowledgements)
                or "none"
            ),
        )

    def _preview_plan_fact_lines(
        self,
        plan: QuotaPlan,
        finished_at: datetime,
    ) -> tuple[str, ...]:
        """Render every safe bound Plan fact produced by Preview."""
        lines = [
            f"Kind: {plan.kind.value}",
            f"Bound resource scope: {plan.resource_scope.canonical_name}",
            f"Target strategy: {plan.target_strategy.value}",
            "Selected location: "
            + (getattr(plan, "selected_location", None) or "none"),
            "Normalized workload: "
            + (
                getattr(plan, "normalized_workload", None)
                or "not applicable (single exact slice)"
            ),
            f"Principal: {plan.principal.stable_identity}",
            f"Quota contact source: {plan.contact_binding.source.value}",
            f"Issuing installation: {plan.installation_id}",
            f"Issued: {plan.issued_at.isoformat()}",
            f"Expires: {plan.expires_at.isoformat()}",
        ]
        for index, child in enumerate(plan.children, start=1):
            lines.extend(
                self._preview_plan_child_lines(
                    child,
                    label=f"Plan child {index}",
                    finished_at=finished_at,
                )
            )
        for index, child in enumerate(
            getattr(plan, "no_op_children", ()),
            start=1,
        ):
            lines.extend(
                self._preview_plan_child_lines(
                    child,
                    label=f"Plan no-op {index}",
                    finished_at=finished_at,
                )
            )
            lines.append("   Disposition: verified no-op; no provider dispatch")
        for index, constraint in enumerate(plan.constraints, start=1):
            identity = constraint.slice_identity
            lines.extend(
                (
                    f"Constraint {index}: {identity.service} / {identity.quota_id}",
                    f"   Dimensions: {self._dimensions(identity)}",
                    f"   Quota scope: {identity.quota_scope.value}",
                )
            )
        return tuple(lines)

    def _preview_plan_child_lines(
        self,
        child: QuotaRequestPlanChild,
        *,
        label: str,
        finished_at: datetime,
    ) -> tuple[str, ...]:
        lines = [
            f"{label}: {child.child_id}",
            f"   Slice: {child.slice_identity.service} / "
            f"{child.slice_identity.quota_id}",
            f"   Dimensions: {self._dimensions(child.slice_identity)}",
            f"   Quota scope: {child.slice_identity.quota_scope.value}",
            f"   Target: {self._quantity(child.target)}",
            f"   Effective: {self._quantity(child.effective)}",
            f"   Usage: {self._quantity(child.usage)}",
            f"   Workload: {self._quantity(child.workload)}",
            f"   Prior desired: {self._quantity(child.prior_desired)}",
            f"   Granted: {self._quantity(child.granted)}",
            f"   Derivation: {child.target_derivation.value}",
            f"   Preference: {child.preference_name or 'none'}",
            f"   Preference ETag: {child.preference_etag or 'none'}",
            "   Warnings: "
            + (", ".join(item.value for item in child.warnings) or "none"),
            "   Required acknowledgements: "
            + (
                ", ".join(item.value for item in child.required_acknowledgements)
                or "none"
            ),
            "   Supplied acknowledgements: "
            + (", ".join(item.value for item in child.acknowledgements) or "none"),
            "   Unresolved acknowledgements: "
            + (
                ", ".join(item.value for item in child.unresolved_acknowledgements)
                or "none"
            ),
        ]
        for evidence in child.evidence:
            age = max(
                0.0,
                (finished_at - evidence.observed_at).total_seconds(),
            )
            lines.extend(
                (
                    f"   Evidence {evidence.name.value}: {evidence.value_digest}",
                    f"      Observed: {evidence.observed_at.isoformat()}",
                    f"      Age: {age} seconds",
                )
            )
        return tuple(lines)

    @staticmethod
    def _merge_selectors(
        first: tuple[QuotaInspectSelector, ...],
        second: tuple[QuotaInspectSelector, ...],
    ) -> tuple[QuotaInspectSelector, ...]:
        merged: list[QuotaInspectSelector] = []
        for selector in (*first, *second):
            if selector not in merged:
                merged.append(selector)
        return tuple(merged)

    @staticmethod
    def _slice_location(identity: EffectiveQuotaSliceIdentity) -> str:
        dimensions = dict(identity.dimensions.items)
        return (
            dimensions.get("zone")
            or dimensions.get("region")
            or dimensions.get("location")
            or "global"
        )

    @staticmethod
    def _dimensions(identity: EffectiveQuotaSliceIdentity) -> str:
        return (
            ", ".join(f"{key}={value}" for key, value in identity.dimensions.items)
            or "none"
        )

    @staticmethod
    def _result_fact_lines(result: OperationResult[Any]) -> tuple[str, ...]:
        """Render every safe diagnostic and provenance field without flattening it."""
        lines: list[str] = [
            f"Operation: {result.operation.value}",
            "Resource scope: "
            + (
                result.resource_scope.canonical_name
                if result.resource_scope is not None
                else "unavailable"
            ),
            f"Outcome: {result.outcome.code.value}",
            f"Exit class: {int(result.outcome.exit_class)}",
            f"Boundary: {result.boundary.condition.value}",
            "Boundary reached: " + ("yes" if result.boundary.reached else "no"),
            "Complete: " + ("yes" if result.completeness.is_complete else "no"),
            f"Started: {result.started_at.isoformat()}",
            f"Finished: {result.finished_at.isoformat()}",
        ]
        lines.extend(
            f"Evidence gap: {gap.source.value} / {gap.reason.value}"
            for gap in result.completeness.gaps
        )
        identity = result.identity_evidence
        if identity is not None:
            lines.extend(
                (
                    f"Credential kind: {identity.credential_kind.value}",
                    f"Identity verification: {identity.verification.value}",
                    "Acting principal: "
                    + (
                        identity.acting_principal.value
                        if identity.acting_principal is not None
                        else "unavailable"
                    ),
                    "Impersonation chain: "
                    + (
                        ", ".join(
                            principal.value
                            for principal in identity.impersonation_chain
                        )
                        or "none"
                    ),
                )
            )
        lines.extend(CloudQuotaManagerApp._diagnostic_fact_lines(result.diagnostics))
        for provenance in result.provenance:
            lines.extend(
                (
                    f"Provenance: {provenance.source.value}",
                    f"   Observed: {provenance.observed_at.isoformat()}",
                    f"   Coverage: {provenance.coverage.value}",
                    "   Interval start: "
                    + (
                        provenance.interval_started_at.isoformat()
                        if provenance.interval_started_at is not None
                        else "none"
                    ),
                    "   Interval finish: "
                    + (
                        provenance.interval_finished_at.isoformat()
                        if provenance.interval_finished_at is not None
                        else "none"
                    ),
                    "   Lifecycle or Preview status: "
                    f"{provenance.lifecycle_or_preview_status or 'none'}",
                    f"   Request identity: {provenance.request_identity or 'none'}",
                )
            )
        return tuple(lines)

    @staticmethod
    def _diagnostic_fact_lines(
        diagnostics: tuple[Diagnostic, ...],
        *,
        label: str = "Diagnostic",
    ) -> tuple[str, ...]:
        """Render every safe diagnostic field under its context label."""
        lines: list[str] = []
        for diagnostic in diagnostics:
            lines.extend(
                (
                    f"{label}: {diagnostic.severity.value} {diagnostic.code.value}",
                    f"   Phase: {diagnostic.phase.value}",
                    f"   Source: {diagnostic.source.value}",
                    f"   Retry: {diagnostic.retry.value}",
                    f"   Message: {diagnostic.message}",
                    "   Field paths: "
                    + (
                        ", ".join(
                            ".".join(path.segments) for path in diagnostic.field_paths
                        )
                        or "none"
                    ),
                )
            )
            metadata = diagnostic.provider_metadata
            if metadata is not None:
                lines.extend(
                    (
                        f"   HTTP status: {metadata.http_status or 'none'}",
                        f"   gRPC status: {metadata.grpc_status or 'none'}",
                        f"   Provider reason: {metadata.reason or 'none'}",
                        "   Provider preference: "
                        f"{metadata.preference_identity or 'none'}",
                        f"   Provider ETag: {metadata.etag or 'none'}",
                        f"   Provider trace: {metadata.trace_identity or 'none'}",
                        f"   Provider request: {metadata.request_identity or 'none'}",
                    )
                )
        return tuple(lines)

    @staticmethod
    def _quantity(value: QuotaQuantity | None) -> str:
        return "unavailable" if value is None else f"{value.value} {value.unit.symbol}"

    @staticmethod
    def _yes_no(value: bool) -> str:  # noqa: FBT001 - semantic formatter
        return "yes" if value else "no"

    def _deadline(self) -> float:
        return cast("float", self._monotonic()) + self._PROVIDER_OPERATION_SECONDS

    @property
    def last_copied_cli(self) -> str | None:
        """Return only the canonical command owned by the active workspace."""
        copy_cli = self._copy_cli_by_workspace.get(self.active_workspace)
        return None if copy_cli is None else copy_cli.command

    def _show_copy_cli(self, command: str) -> None:
        copy_cli = WorkspaceCopyCli(self.active_workspace, command)
        self._copy_cli_by_workspace[self.active_workspace] = copy_cli
        self._sync_copy_cli_preview()

    def _show_obtainability_copy_cli(
        self,
        candidates: tuple[ObtainabilityCandidate, ...],
    ) -> None:
        """Expose a safe equivalent command as soon as its fixed input is confirmed."""
        scope: ResourceScope | None = self._copy_cli_resource_scope
        if scope is None:
            return
        self._show_copy_cli(obtainability_compare_copy_cli(scope, candidates))

    def _show_obtainability_all_compatible_copy_cli(
        self,
        draft: ObtainabilityFormDraft,
    ) -> None:
        """Expose the unresolved all-compatible mode without inventing candidates."""
        scope: ResourceScope | None = self._copy_cli_resource_scope
        if scope is None:
            return
        requirement = self._obtainability_requirement(
            draft,
            locations=(),
            all_compatible=True,
        )
        self._show_copy_cli(
            obtainability_all_compatible_copy_cli(
                scope,
                requirement,
                machine=draft.machine,
                distribution_shape=draft.distribution_shape,
            )
        )

    def _sync_copy_cli_preview(self) -> None:
        """Expose only the active workspace's canonical command."""
        command = self.last_copied_cli
        if self.active_workspace == "quotas":
            self.query_one("#copy-cli-preview", Static).update(
                command or "Copy CLI unavailable until an operation is fully specified."
            )
        elif self.active_workspace == "obtainability":
            self.query_one("#obtainability-copy-cli", Static).update(
                command or "Copy CLI unavailable until a comparison is fully specified."
            )

    def interface_snapshot(self) -> str:
        """Return a deterministic semantic snapshot for reviewed Pilot states."""
        result = self.last_result
        lines = [
            f"layout={self.layout_mode}",
            f"workspace={self.active_workspace}",
            self._offline_instrument_text()
            if result is None
            else self._instrument_snapshot(result),
        ]
        if result is not None:
            lines.append(f"outcome={result.outcome.code.value}")
            lines.append(
                "complete=" + ("yes" if result.completeness.is_complete else "no")
            )
            if isinstance(result.data, QuotaBrowseData):
                lines.extend(
                    f"{coverage.service}: {coverage.state.value}"
                    for coverage in result.data.source_coverage
                )
                for item in result.data.items:
                    lines.extend(
                        (
                            f"{item.identity.service} {item.identity.quota_id} "
                            f"{item.location or 'global'}",
                            "discovered="
                            f"{self._yes_no(item.predicates.discovered)} "
                            f"cataloged={self._yes_no(item.predicates.cataloged)} "
                            f"guided={self._yes_no(item.predicates.guided)} "
                            f"mutable={self._yes_no(item.predicates.mutable)}",
                        )
                    )
        if self.last_copied_cli is not None:
            lines.append(f"copy-cli={self.last_copied_cli}")
        if self.is_mounted:
            lines.extend(
                (
                    "status=" + str(self.query_one("#status-line", Static).content),
                    "coverage="
                    + str(self.query_one("#coverage-summary", Static).content),
                    "detail=" + str(self.query_one("#quota-detail", Static).content),
                    "audit=" + str(self.query_one("#audit-detail", Static).content),
                )
            )
            if self.active_workspace == "obtainability":
                machine_type = self.query_one(
                    "#obtainability-machine-type",
                    Input,
                ).value
                gpu_type = self.query_one("#obtainability-gpu-type", Input).value
                gpu_count = self.query_one("#obtainability-gpu-count", Input).value
                lines.extend(
                    (
                        "obtainability-breadcrumb="
                        + str(
                            self.query_one(
                                "#obtainability-breadcrumb",
                                Static,
                            ).content
                        ),
                        "obtainability-request="
                        f"machine={machine_type} "
                        f"gpu={gpu_type or 'none'} "
                        f"gpu-count={gpu_count or 'none'} "
                        "vm-count="
                        + self.query_one("#obtainability-vm-count", Input).value
                        + " distribution="
                        + self.query_one(
                            "#obtainability-distribution",
                            Input,
                        ).value,
                        "obtainability-candidates="
                        + self.query_one("#obtainability-candidates", Input).value,
                        "obtainability-expansion="
                        + str(
                            self.query_one(
                                "#obtainability-expansion",
                                Static,
                            ).content
                        ),
                        "obtainability-detail="
                        + str(
                            self.query_one(
                                "#obtainability-detail",
                                Static,
                            ).content
                        ),
                        "obtainability-copy-cli="
                        + str(
                            self.query_one(
                                "#obtainability-copy-cli",
                                Static,
                            ).content
                        ),
                    )
                )
            if self._lifecycle_state.route is not None:
                lines.extend(
                    (
                        "lifecycle-breadcrumb="
                        + str(
                            self.query_one(
                                "#lifecycle-breadcrumb",
                                Static,
                            ).content
                        ),
                        "lifecycle-scope="
                        + str(
                            self.query_one(
                                "#lifecycle-scope",
                                Static,
                            ).content
                        ),
                        "lifecycle-detail="
                        + str(
                            self.query_one(
                                "#lifecycle-detail",
                                Static,
                            ).content
                        ),
                        "lifecycle-copy-cli="
                        + str(
                            self.query_one(
                                "#lifecycle-copy-cli",
                                Static,
                            ).content
                        ),
                    )
                )
        return "\n".join(lines)

    def _instrument_snapshot(self, result: OperationResult[Any]) -> str:
        scope = (
            result.resource_scope.canonical_name
            if result.resource_scope is not None
            else self._scope_label()
        )
        principal = (
            result.identity_evidence.acting_principal.value
            if result.identity_evidence is not None
            and result.identity_evidence.acting_principal is not None
            else "unavailable"
        )
        return (
            f"scope={scope}\n"
            f"scope-lock={'LOCKED' if self.scope_locked else 'unlocked'}\n"
            f"principal={principal}"
        )

    def action_workspace(self, workspace: str) -> None:
        """Switch among the three sibling workspaces without losing inspector state."""
        if workspace == "obtainability" and self.active_workspace != "obtainability":
            self._prepare_standalone_obtainability()
        self._set_active_workspace(workspace)

    def _set_active_workspace(self, workspace: str) -> None:
        if (
            self._lifecycle_state.apply_in_progress
            or self._post_apply_reconciliation_active
        ):
            self._set_status(
                "APPLY RECONCILIATION ACTIVE — workspace navigation is deferred"
            )
            return
        if workspace not in {"quotas", "obtainability", "audit"}:
            return
        if self._lifecycle_state.route is not None:
            self._clear_lifecycle_route()
        self._workspace_generation += 1
        workspace_generation = self._workspace_generation
        if workspace == "audit" or (
            self.active_workspace == "quotas" and workspace != "quotas"
        ):
            self._claim_provider_view()
        self.active_workspace = workspace
        for name in ("quotas", "obtainability", "audit"):
            widget_id = "quota-workbench" if name == "quotas" else f"{name}-workspace"
            self.query_one(f"#{widget_id}").set_class(name != workspace, "hidden")
            button = self.query_one(f"#workspace-{name}", Button)
            button.set_class(name == workspace, "active-workspace")
        self._sync_copy_cli_preview()
        if workspace == "audit":
            operation_generation = self._claim_audit_operation()
            self.run_worker(
                self._load_audit(workspace_generation, operation_generation),
                group="audit-load",
                exclusive=True,
                exit_on_error=False,
            )

    async def _load_audit(
        self,
        workspace_generation: int,
        operation_generation: int,
    ) -> None:
        result = await self.audit.list(AuditQuery())
        if not self._owns_audit_operation(
            workspace_generation,
            operation_generation,
        ):
            return
        self.last_result = result
        table = self.query_one("#audit-table", DataTable)
        table.clear()
        if isinstance(result.data, AuditListData):
            for record in result.data.records:
                table.add_row(
                    record.record_id,
                    record.draft.operation.value,
                    record.draft.outcome.value if record.draft.outcome else "none",
                    record.draft.occurred_at.isoformat(),
                    key=record.record_id,
                )
        self._set_status(self._result_status(result))

    def _owns_workspace_view(self, workspace: str, generation: int) -> bool:
        return (
            generation == self._workspace_generation
            and self.active_workspace == workspace
        )

    def _claim_audit_operation(self) -> int:
        self._audit_operation_generation += 1
        return self._audit_operation_generation

    def _owns_audit_operation(
        self,
        workspace_generation: int,
        operation_generation: int,
    ) -> bool:
        return (
            self._owns_workspace_view("audit", workspace_generation)
            and operation_generation == self._audit_operation_generation
        )

    def action_focus_filters(self) -> None:
        """Focus the workspace filter entry using the documented slash binding."""
        if self.active_workspace == "quotas" and self._lifecycle_state.route is None:
            self.query_one("#filter-text", Input).focus()

    def action_return_to_ledger(self) -> None:
        """Return from a narrow detail route and restore ledger focus."""
        if self._lifecycle_state.route is not None:
            self._leave_lifecycle_route()
            return
        if self._workload_kind is not None:
            self._workload_kind = None
            self.query_one("#workload-form").add_class("hidden")
            self.query_one("#quota-detail").remove_class("hidden")
        if self._detail_route:
            self._detail_route = False
            self.remove_class("detail-route")
            self.query_one("#quota-ledger", DataTable).focus()

    def action_refresh(self) -> None:
        """Refresh the current typed query, cancelling any superseded read."""
        if self.active_workspace == "quotas" and self._lifecycle_state.route is None:
            self._start_quota_load(self.current_query)

    def action_help(self) -> None:
        """Expose the keyboard contract without depending on glyphs or color."""
        self.notify(
            "Tab/Shift-Tab move focus; arrows move rows; Enter opens; "
            "Escape returns; / focuses filters; Ctrl-K opens commands.",
            title="Keyboard help",
        )

    def on_button_pressed(  # noqa: C901, PLR0912
        self,
        event: Button.Pressed,
    ) -> None:
        """Route explicit shell and filter controls."""
        button_id = event.button.id
        if button_id and button_id.startswith("workspace-"):
            self.action_workspace(button_id.removeprefix("workspace-"))
        elif button_id == "apply-filters" and self.active_workspace == "quotas":
            self._apply_filters()
        elif button_id == "detail-back":
            self.action_return_to_ledger()
        elif button_id == "lifecycle-back":
            self._leave_lifecycle_route()
        elif button_id == "lifecycle-preview":
            self._submit_lifecycle_preview()
        elif button_id == "lifecycle-apply":
            self._submit_lifecycle_apply()
        elif button_id == "lifecycle-watch":
            self._submit_lifecycle_watch()
        elif button_id == "lifecycle-copy":
            command = self._lifecycle_copy_cli()
            if command is not None:
                self.copy_to_clipboard(command)
        elif button_id == "resolve-compute":
            self._open_workload_route("compute-instance")
        elif button_id == "resolve-tpu":
            self._open_workload_route("cloud-tpu-slice")
        elif button_id == "workload-submit" and self.active_workspace == "quotas":
            self._submit_workload()
        elif button_id == "workload-obtainability":
            self._open_contextual_obtainability()
        elif (
            button_id == "obtainability-compare"
            and self.active_workspace == "obtainability"
        ):
            self._submit_obtainability()
        elif (
            button_id == "obtainability-compare-all"
            and self.active_workspace == "obtainability"
        ):
            self._submit_obtainability_all()
        elif button_id == "obtainability-confirm":
            self._confirm_obtainability()
        elif button_id == "obtainability-return":
            self._set_active_workspace("quotas")
        elif button_id == "audit-verify" and self.active_workspace == "audit":
            operation_generation = self._claim_audit_operation()
            self.run_worker(
                self._verify_audit(
                    self._workspace_generation,
                    operation_generation,
                ),
                group="audit-verify",
                exclusive=True,
                exit_on_error=False,
            )
        elif button_id in {"copy-cli", "obtainability-copy"}:
            command = self.last_copied_cli
            if command is not None:
                self.copy_to_clipboard(command)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Invalidate every provider-backed Obtainability artifact on edit."""
        input_id = event.input.id
        if input_id == "apply-scope-acknowledgement":
            request = self._lifecycle_state.pending_apply
            expected = (
                request.resource_scope_acknowledgement.canonical_name
                if request is not None
                else None
            )
            confirmed = expected is not None and event.value == expected
            self.query_one("#lifecycle-apply", Button).disabled = not confirmed
            self._set_status(
                "CONFIRMED — Apply will revalidate every child before dispatch"
                if confirmed
                else "CONFIRMATION REQUIRED — type the exact bound resource scope"
            )
            return
        if (
            input_id is not None
            and input_id.startswith("obtainability-")
            and self.active_workspace == "obtainability"
        ):
            try:
                current = self._decode_obtainability_form(
                    all_compatible=self._active_obtainability_all_compatible,
                )
            except (TypeError, ValueError):
                current = None
            if (
                current is not None
                and current.fingerprint == self._active_obtainability_fingerprint
                and self._active_obtainability_inputs.get(input_id) == event.value
            ):
                return
            self._claim_provider_view()
            self._active_obtainability_fingerprint = None
            self._active_obtainability_all_compatible = (
                current.all_compatible if current is not None else False
            )
            self._active_obtainability_inputs.clear()
            entry_mode = self._obtainability_state.entry_mode
            self._obtainability_state = replace(
                self._obtainability_state,
                pending_expansion=None,
                pending_fingerprint=None,
                pending_all_compatible=False,
                confirmed_fingerprint=None,
                confirmed_candidate_ids=(),
            )
            self._copy_cli_by_workspace.pop("obtainability", None)
            self.last_result = None
            self.query_one("#instrument-bar", Static).update(
                self._offline_instrument_text()
            )
            self.query_one("#obtainability-detail", Static).update(
                "Complete the fixed request and choose an explicit candidate mode.\n"
                "Obtainability is Preview evidence, not capacity."
            )
            self._sync_copy_cli_preview()
            self._set_status(
                "INPUT CHANGED — confirm the exact request before comparison"
            )
            if current is not None and entry_mode is ObtainabilityEntryMode.STANDALONE:
                if current.all_compatible:
                    self._show_obtainability_all_compatible_copy_cli(current)
                elif current.candidates:
                    self._show_obtainability_copy_cli(current.candidates)

    def _apply_filters(self) -> None:
        if self.active_workspace != "quotas":
            return
        text = self.query_one("#filter-text", Input).value.strip() or None
        service_value = self.query_one("#filter-service", Input).value.strip()
        try:
            filters = QuotaQueryFilters(
                services=(service_value,) if service_value else (),
                text=text,
            )
        except (TypeError, ValueError) as error:
            self._set_status(f"INVALID FILTER — {error}")
            return
        self.current_query = ReadOnlyQuotaQuery(filters=filters)
        self._start_quota_load(self.current_query)

    def _open_workload_route(self, kind: str) -> None:
        """Open one typed workload form without losing inspector state."""
        self._workload_kind = kind
        self._detail_route = self.layout_mode == "narrow"
        self.set_class(self._detail_route, "detail-route")
        self.query_one("#quota-detail").add_class("hidden")
        self.query_one("#workload-form").remove_class("hidden")
        label = "Compute instance" if kind == "compute-instance" else "Cloud TPU slice"
        self.query_one("#workload-breadcrumb", Static).update(
            f"Quotas / Resolve / {label}\n"
            "Candidate locations remain independent. Quota is not capacity."
        )
        machine = self.query_one("#workload-machine-type", Input)
        machine.disabled = kind != "compute-instance"
        for selector in ("#workload-gpu-type", "#workload-gpu-count"):
            self.query_one(selector, Input).disabled = kind != "compute-instance"
        for selector in (
            "#workload-accelerator-type",
            "#workload-topology",
            "#workload-runtime-version",
        ):
            self.query_one(selector, Input).disabled = kind != "cloud-tpu-slice"
        (
            machine
            if kind == "compute-instance"
            else self.query_one("#workload-accelerator-type", Input)
        ).focus()

    def _submit_workload(self) -> None:
        """Decode form primitives through the same public parser as Click."""
        if self.active_workspace != "quotas":
            return
        kind = self._workload_kind
        if kind is None:
            self._set_status("INVALID WORKLOAD — select a workload route")
            return
        location_text = self.query_one("#workload-locations", Input).value.strip()
        all_compatible = location_text.casefold() == "all"
        locations = (
            ()
            if all_compatible
            else tuple(
                item.strip() for item in location_text.split(",") if item.strip()
            )
        )
        try:
            if kind == "compute-instance":
                requirement = parse_compute_instance_requirement(
                    machine_type=self.query_one(
                        "#workload-machine-type",
                        Input,
                    ).value.strip(),
                    instance_count=self.query_one(
                        "#workload-count",
                        Input,
                    ).value.strip(),
                    provisioning_model=self.query_one(
                        "#workload-provisioning",
                        Input,
                    ).value.strip(),
                    locations=locations,
                    all_compatible=all_compatible,
                    attached_accelerator_type=(
                        self.query_one("#workload-gpu-type", Input).value.strip()
                        or None
                    ),
                    attached_accelerator_count=(
                        self.query_one("#workload-gpu-count", Input).value.strip()
                        or None
                    ),
                )
            else:
                requirement = parse_cloud_tpu_slice_requirement(
                    accelerator_type=self.query_one(
                        "#workload-accelerator-type",
                        Input,
                    ).value.strip(),
                    topology=self.query_one(
                        "#workload-topology",
                        Input,
                    ).value.strip(),
                    runtime_version=self.query_one(
                        "#workload-runtime-version",
                        Input,
                    ).value.strip(),
                    slice_count=self.query_one(
                        "#workload-count",
                        Input,
                    ).value.strip(),
                    provisioning_model=self.query_one(
                        "#workload-provisioning",
                        Input,
                    ).value.strip(),
                    locations=locations,
                    all_compatible=all_compatible,
                )
        except (TypeError, ValueError) as error:
            self._set_status(f"INVALID WORKLOAD — {error}")
            return
        generation = self._claim_provider_view()
        self.run_worker(
            self.resolve_workload(requirement, generation),
            group="workload-resolve",
            exclusive=True,
            exit_on_error=False,
        )

    def _prepare_standalone_obtainability(self) -> None:
        self._obtainability_state = ObtainabilityWorkflowState()
        self.query_one("#obtainability-breadcrumb", Static).update(
            "Obtainability / Standalone\n"
            "Fix one exact Spot VM request. Candidate locations never expand silently."
        )
        self.query_one("#obtainability-expansion", Static).update(
            "Candidate expansion: explicit candidates only."
        )
        self.query_one("#obtainability-return").add_class("hidden")

    def _open_contextual_obtainability(self) -> None:
        resolved = self._resolved_compute
        if resolved is None or not isinstance(
            resolved.requirement,
            ComputeInstanceRequirement,
        ):
            self._set_status(
                "OBTAINABILITY UNAVAILABLE — resolve a supported Spot Compute shape"
            )
            return
        requirement = resolved.requirement
        self.query_one(
            "#obtainability-machine-type", Input
        ).value = requirement.machine_type
        self.query_one("#obtainability-gpu-type", Input).value = (
            requirement.attached_accelerator_type or ""
        )
        self.query_one("#obtainability-gpu-count", Input).value = (
            ""
            if requirement.attached_accelerator_count is None
            else str(requirement.attached_accelerator_count)
        )
        self.query_one("#obtainability-vm-count", Input).value = str(
            requirement.instance_count
        )
        self.query_one(
            "#obtainability-distribution", Input
        ).value = DistributionShape.ANY.value
        compatible = tuple(
            location.location
            for location in resolved.locations
            if location.disposition is WorkloadLocationDisposition.COMPATIBLE
        )
        candidate_values = tuple(
            self._candidate_text(location) for location in compatible
        )
        try:
            candidates = parse_obtainability_candidates(
                machine_type=requirement.machine_type,
                gpu_type=requirement.attached_accelerator_type,
                gpu_count=(
                    None
                    if requirement.attached_accelerator_count is None
                    else str(requirement.attached_accelerator_count)
                ),
                vm_count=str(requirement.instance_count),
                distribution_shape=DistributionShape.ANY.value,
                candidates=candidate_values,
            )
            draft = ObtainabilityFormDraft(
                SpotMachineConfiguration(
                    requirement.machine_type,
                    gpu=(
                        None
                        if requirement.attached_accelerator_type is None
                        or requirement.attached_accelerator_count is None
                        else GpuAttachment(
                            requirement.attached_accelerator_type,
                            requirement.attached_accelerator_count,
                        )
                    ),
                ),
                requirement.instance_count,
                DistributionShape.ANY,
                candidates,
                all_compatible=False,
            )
            prepared = prepare_obtainability_comparison(resolved, candidates)
        except (TypeError, ValueError):
            self._set_status(
                "OBTAINABILITY UNAVAILABLE — no exact Spot Compute candidates"
            )
            return
        self._obtainability_state = ObtainabilityWorkflowState(
            entry_mode=ObtainabilityEntryMode.CONTEXTUAL,
            pending_expansion=prepared,
            pending_fingerprint=draft.fingerprint,
        )
        self.query_one("#obtainability-candidates", Input).value = " ".join(
            candidate_values
        )
        self._mark_obtainability_submission(draft)
        self.query_one("#obtainability-breadcrumb", Static).update(
            "Obtainability / Contextual\n"
            "Inherited from Quotas / Resolve / Compute instance\n"
            "Confirm the complete inherited shape before provider advice."
        )
        self.query_one("#obtainability-expansion", Static).update(
            "Candidate expansion: "
            + (", ".join(compatible) if compatible else "none proven compatible")
        )
        self.query_one("#obtainability-return").remove_class("hidden")
        self._set_active_workspace("obtainability")
        self._set_status("CONFIRM INHERITED FIELDS — no advice query has started")

    @staticmethod
    def _candidate_text(location: str) -> str:
        region, separator, suffix = location.rpartition("-")
        return (
            f"{region}={location}"
            if separator == "-" and len(suffix) == 1 and suffix.isalpha()
            else location
        )

    def _confirm_obtainability(self) -> None:
        state = self._obtainability_state
        if (
            state.entry_mode is ObtainabilityEntryMode.CONTEXTUAL
            and state.pending_expansion is None
        ):
            self._set_status(
                "PREPARE INHERITED FIELDS — resolve the edited request "
                "before confirmation"
            )
            return
        try:
            draft = self._decode_obtainability_form(
                all_compatible=state.pending_all_compatible,
            )
        except (TypeError, ValueError) as error:
            self._set_status(f"INVALID OBTAINABILITY REQUEST — {error}")
            return
        if (
            state.pending_fingerprint is not None
            and state.pending_fingerprint != draft.fingerprint
        ):
            self._obtainability_state = replace(
                state,
                confirmed_fingerprint=None,
                confirmed_candidate_ids=(),
            )
            self._set_status(
                "INPUT CHANGED — preview candidate expansion again before confirmation"
            )
            return
        candidate_ids = (
            tuple(
                candidate.candidate_id
                for candidate in state.pending_expansion.candidates
            )
            if state.pending_expansion is not None
            else tuple(candidate.candidate_id for candidate in draft.candidates)
        )
        self._obtainability_state = replace(
            state,
            confirmed_fingerprint=draft.fingerprint,
            confirmed_candidate_ids=candidate_ids,
        )
        candidates = (
            state.pending_expansion.candidates
            if state.pending_expansion is not None
            else draft.candidates
        )
        if state.pending_all_compatible:
            self._show_obtainability_all_compatible_copy_cli(draft)
        else:
            self._show_obtainability_copy_cli(candidates)
        self._set_status("CONFIRMED — inherited fields and candidate mode are explicit")

    def _submit_obtainability(self) -> None:
        """Decode an explicit fixed request through the same parser as Click."""
        try:
            draft = self._decode_obtainability_form(all_compatible=False)
        except (TypeError, ValueError) as error:
            self._set_status(f"INVALID OBTAINABILITY REQUEST — {error}")
            return
        state = self._obtainability_state
        if state.entry_mode is ObtainabilityEntryMode.CONTEXTUAL:
            if (
                state.pending_expansion is None
                or state.pending_fingerprint != draft.fingerprint
            ):
                generation = self._claim_provider_view()
                cancellation = CancellationToken()
                self._cancellation = cancellation
                self._mark_obtainability_submission(draft)
                self.run_worker(
                    self._preview_contextual_obtainability(
                        draft,
                        cancellation,
                        generation,
                    ),
                    group="obtainability-expand",
                    exclusive=True,
                    exit_on_error=False,
                )
                return
            confirmed_ids = tuple(
                candidate.candidate_id
                for candidate in state.pending_expansion.candidates
            )
            if (
                state.confirmed_fingerprint != draft.fingerprint
                or state.confirmed_candidate_ids != confirmed_ids
            ):
                self._set_status(
                    "CONFIRM INHERITED FIELDS — no advice query has started"
                )
                return
            generation = self._claim_provider_view()
            cancellation = CancellationToken()
            self._cancellation = cancellation
            self._mark_obtainability_submission(draft)
            self.run_worker(
                self._compare_obtainability_prepared(
                    state.pending_expansion,
                    cancellation,
                    generation,
                ),
                group="obtainability-compare",
                exclusive=True,
                exit_on_error=False,
            )
            return
        generation = self._claim_provider_view()
        cancellation = CancellationToken()
        self._cancellation = cancellation
        self._mark_obtainability_submission(draft)
        self._show_obtainability_copy_cli(draft.candidates)
        self.run_worker(
            self._compare_obtainability(draft, cancellation, generation),
            group="obtainability-compare",
            exclusive=True,
            exit_on_error=False,
        )

    def _submit_obtainability_all(self) -> None:
        """Resolve an all-compatible expansion before any comparison begins."""
        try:
            draft = self._decode_obtainability_form(all_compatible=True)
            requirement = self._obtainability_requirement(
                draft,
                locations=(),
                all_compatible=True,
            )
        except (TypeError, ValueError) as error:
            self._set_status(f"INVALID OBTAINABILITY REQUEST — {error}")
            return
        self._show_obtainability_all_compatible_copy_cli(draft)
        state = self._obtainability_state
        if (
            state.pending_expansion is None
            or state.pending_fingerprint != draft.fingerprint
        ):
            self._obtainability_state = replace(
                state,
                pending_expansion=None,
                pending_fingerprint=None,
                pending_all_compatible=False,
                confirmed_fingerprint=None,
                confirmed_candidate_ids=(),
            )
            generation = self._claim_provider_view()
            cancellation = CancellationToken()
            self._cancellation = cancellation
            self._mark_obtainability_submission(draft)
            self.run_worker(
                self._preview_obtainability_expansion(
                    requirement,
                    draft,
                    cancellation,
                    generation,
                ),
                group="obtainability-expand",
                exclusive=True,
                exit_on_error=False,
            )
            return
        confirmed_ids = tuple(
            candidate.candidate_id for candidate in state.pending_expansion.candidates
        )
        if (
            state.confirmed_fingerprint != draft.fingerprint
            or state.confirmed_candidate_ids != confirmed_ids
        ):
            self._set_status("CONFIRM CANDIDATE EXPANSION — no comparison has started")
            return
        generation = self._claim_provider_view()
        cancellation = CancellationToken()
        self._cancellation = cancellation
        self._mark_obtainability_submission(draft)
        self.run_worker(
            self._compare_obtainability_prepared(
                state.pending_expansion,
                cancellation,
                generation,
            ),
            group="obtainability-compare",
            exclusive=True,
            exit_on_error=False,
        )

    def _mark_obtainability_submission(self, draft: ObtainabilityFormDraft) -> None:
        """Keep already-submitted form events from superseding their own worker."""
        self._active_obtainability_fingerprint = draft.fingerprint
        self._active_obtainability_all_compatible = draft.all_compatible
        self._active_obtainability_inputs = {
            input_id: self.query_one(f"#{input_id}", Input).value
            for input_id in self._OBTAINABILITY_INPUT_IDS
        }

    def _decode_obtainability_form(
        self,
        *,
        all_compatible: bool,
    ) -> ObtainabilityFormDraft:
        """Decode every form field once through the shared public parsers."""
        gpu_type = self.query_one("#obtainability-gpu-type", Input).value.strip()
        gpu_count = self.query_one("#obtainability-gpu-count", Input).value.strip()
        machine, count, shape = parse_obtainability_shape(
            machine_type=self.query_one(
                "#obtainability-machine-type",
                Input,
            ).value.strip(),
            gpu_type=gpu_type or None,
            gpu_count=gpu_count or None,
            vm_count=self.query_one(
                "#obtainability-vm-count",
                Input,
            ).value.strip(),
            distribution_shape=self.query_one(
                "#obtainability-distribution",
                Input,
            ).value.strip(),
        )
        candidate_text = self.query_one(
            "#obtainability-candidates",
            Input,
        ).value.strip()
        candidates = (
            ()
            if all_compatible
            else parse_obtainability_candidates(
                machine_type=machine.machine_type,
                gpu_type=(
                    None if machine.gpu is None else machine.gpu.accelerator_type
                ),
                gpu_count=None if machine.gpu is None else str(machine.gpu.count),
                vm_count=str(count),
                distribution_shape=shape.value,
                candidates=tuple(candidate_text.split()),
            )
        )
        return ObtainabilityFormDraft(
            machine,
            count,
            shape,
            candidates,
            all_compatible,
        )

    @staticmethod
    def _obtainability_requirement(
        draft: ObtainabilityFormDraft,
        *,
        locations: tuple[str, ...],
        all_compatible: bool,
    ) -> ComputeInstanceRequirement:
        """Carry the exact optional attachment into resolver-owned proof."""
        gpu = draft.machine.gpu
        return parse_compute_instance_requirement(
            machine_type=draft.machine.machine_type,
            instance_count=str(draft.vm_count),
            provisioning_model=ProvisioningModel.SPOT.value,
            locations=locations,
            all_compatible=all_compatible,
            attached_accelerator_type=(None if gpu is None else gpu.accelerator_type),
            attached_accelerator_count=None if gpu is None else str(gpu.count),
        )

    async def _preview_obtainability_expansion(
        self,
        requirement: ComputeInstanceRequirement,
        draft: ObtainabilityFormDraft,
        cancellation: CancellationToken,
        generation: int,
    ) -> None:
        result = await self.read_only.resolve(
            requirement,
            deadline=self._deadline(),
            cancellation=cancellation,
            scope_input=self.scope_input,
        )
        if cancellation.cancelled or not self._owns_obtainability_view(generation):
            return
        data = result.data
        if not isinstance(data, ResolvedWorkloadRequirement):
            self._set_status(
                "EXPANSION UNAVAILABLE — compatible locations were not resolved"
            )
            return
        self._resolved_compute = data
        try:
            candidates = candidates_from_resolved_workload(
                data,
                machine=draft.machine,
                distribution_shape=draft.distribution_shape,
            )
            prepared = prepare_obtainability_comparison(data, candidates)
        except (TypeError, ValueError):
            self._set_status(
                "EXPANSION UNAVAILABLE — no cataloged Spot Compute candidates"
            )
            return
        self._obtainability_state = replace(
            self._obtainability_state,
            pending_expansion=prepared,
            pending_fingerprint=draft.fingerprint,
            pending_all_compatible=True,
            confirmed_fingerprint=None,
            confirmed_candidate_ids=(),
        )
        compatible = tuple(
            candidate.zones[0] if candidate.zones else candidate.endpoint_region
            for candidate in candidates
        )
        self.query_one("#obtainability-expansion", Static).update(
            "Candidate expansion before comparison: "
            + (", ".join(compatible) or "none proven compatible")
        )
        self._set_status("CONFIRM CANDIDATE EXPANSION — no comparison has started")

    async def _preview_contextual_obtainability(
        self,
        draft: ObtainabilityFormDraft,
        cancellation: CancellationToken,
        generation: int,
    ) -> None:
        """Prepare one edited contextual request before operator confirmation."""
        locations = tuple(
            dict.fromkeys(
                location
                for candidate in draft.candidates
                for location in (candidate.zones or (candidate.endpoint_region,))
            )
        )
        requirement = self._obtainability_requirement(
            draft,
            locations=locations,
            all_compatible=False,
        )
        self._set_status("READING — preparing edited inherited fields")
        result = await self.read_only.resolve(
            requirement,
            deadline=self._deadline(),
            cancellation=cancellation,
            scope_input=self.scope_input,
        )
        if cancellation.cancelled or not self._owns_obtainability_view(generation):
            return
        if not isinstance(result.data, ResolvedWorkloadRequirement):
            self.last_result = result
            self._render_instrument(result)
            self._render_obtainability_result(result)
            return
        try:
            prepared = prepare_obtainability_comparison(
                result.data,
                draft.candidates,
            )
        except (TypeError, ValueError):
            self._set_status(
                "PREPARATION UNAVAILABLE — exact candidates were not resolved"
            )
            return
        self._obtainability_state = replace(
            self._obtainability_state,
            pending_expansion=prepared,
            pending_fingerprint=draft.fingerprint,
            pending_all_compatible=False,
            confirmed_fingerprint=None,
            confirmed_candidate_ids=(),
        )
        self._set_status("CONFIRM INHERITED FIELDS — no advice query has started")

    async def _compare_obtainability(
        self,
        draft: ObtainabilityFormDraft,
        cancellation: CancellationToken,
        generation: int,
    ) -> None:
        if cancellation.cancelled or not self._owns_obtainability_view(generation):
            return
        locations = tuple(
            dict.fromkeys(
                location
                for candidate in draft.candidates
                for location in (candidate.zones or (candidate.endpoint_region,))
            )
        )
        requirement = self._obtainability_requirement(
            draft,
            locations=locations,
            all_compatible=False,
        )
        self._set_status("READING — checking exact Spot Compute eligibility")
        try:
            resolved_result = await self.read_only.resolve(
                requirement,
                deadline=self._deadline(),
                cancellation=cancellation,
                scope_input=self.scope_input,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - no typed result exists for worker failure
            if self._owns_obtainability_view(generation):
                self._set_status(
                    "ERROR — obtainability comparison unavailable; retry the "
                    "read-only operation"
                )
            return
        if cancellation.cancelled or not self._owns_obtainability_view(generation):
            return
        if not isinstance(resolved_result.data, ResolvedWorkloadRequirement):
            self.last_result = resolved_result
            self._render_instrument(resolved_result)
            self._render_obtainability_result(resolved_result)
            return
        prepared = prepare_obtainability_comparison(
            resolved_result.data,
            draft.candidates,
        )
        await self._compare_obtainability_prepared(
            prepared,
            cancellation,
            generation,
        )

    async def _compare_obtainability_prepared(
        self,
        prepared: PreparedObtainabilityComparison,
        cancellation: CancellationToken,
        generation: int,
    ) -> None:
        """Compare the exact resolver-backed candidates already confirmed."""
        if cancellation.cancelled or not self._owns_obtainability_view(generation):
            return
        self._set_status("READING — comparing exact Spot VM candidates")
        result = await self.read_only.compare_obtainability_prepared(
            prepared,
            deadline=self._deadline(),
            cancellation=cancellation,
            scope_input=self.scope_input,
        )
        if cancellation.cancelled or not self._owns_obtainability_view(generation):
            return
        self.last_result = result
        self._render_instrument(result)
        self._render_obtainability_result(result)
        if result.resource_scope is not None:
            if self._obtainability_state.pending_all_compatible:
                try:
                    draft = self._decode_obtainability_form(all_compatible=True)
                except (TypeError, ValueError):
                    return
                self._show_obtainability_all_compatible_copy_cli(draft)
            else:
                self._show_obtainability_copy_cli(prepared.candidates)

    def _render_obtainability_result(self, result: OperationResult[Any]) -> None:
        data = result.data
        lines = [
            "Obtainability comparison",
            f"Outcome: {result.outcome.code.value}",
            "Complete: " + ("yes" if result.completeness.is_complete else "no"),
            "Partial evidence retained: "
            + ("yes" if result.completeness.has_partial_data else "no"),
        ]
        lines.extend(
            f"Evidence gap: {gap.source.value} / {gap.reason.value}"
            for gap in result.completeness.gaps
        )
        lines.extend(
            f"{diagnostic.severity.value.upper()} {diagnostic.code.value}: "
            f"{diagnostic.message} (retry: {diagnostic.retry.value})"
            for diagnostic in result.diagnostics
        )
        if isinstance(data, ObtainabilityComparison):
            if data.resolver_provenance is not None:
                provenance = data.resolver_provenance
                exhaustive = (
                    "unavailable"
                    if provenance.all_compatible_locations_exhaustive is None
                    else str(provenance.all_compatible_locations_exhaustive).lower()
                )
                location_lines = tuple(
                    f"{location.location}: {location.disposition.value}"
                    + (
                        f" ({location.failure_reason.value})"
                        if location.failure_reason is not None
                        else ""
                    )
                    for location in provenance.locations
                )
                self.query_one("#obtainability-expansion", Static).update(
                    "Candidate expansion before comparison:\n"
                    f"All-compatible locations exhaustive: {exhaustive}\n"
                    + (
                        "\n".join(location_lines)
                        if location_lines
                        else "No locations returned by the resolver."
                    )
                )
            lines.extend(
                (
                    f"Provider status: {data.preview_status}",
                    "Capacity guarantee: "
                    + ("no" if data.no_capacity_guarantee else "yes"),
                )
            )
            if (
                self._obtainability_state.entry_mode
                is ObtainabilityEntryMode.CONTEXTUAL
            ):
                lines.append("Return context: Quotas / Resolve / Compute instance")
            for coverage in data.catalog_coverage:
                lines.extend(
                    (
                        f"Catalog product: {coverage.product_id}",
                        f"Catalog service: {coverage.service}",
                        f"Cataloged: {str(coverage.cataloged).lower()}",
                        "Current advice supported: "
                        f"{str(coverage.current_advice_supported).lower()}",
                        f"History supported: {str(coverage.history_supported).lower()}",
                        "Coverage reasons: " + (", ".join(coverage.reasons) or "none"),
                    )
                )
            ranked = tuple(
                assessment
                for assessment in data.candidates
                if not assessment.unranked_reasons
            )
            unranked = tuple(
                assessment
                for assessment in data.candidates
                if assessment.unranked_reasons
            )
            lines.append(f"Ranked candidates: {len(ranked)}")
            for assessment in ranked:
                lines.extend(
                    self._obtainability_candidate_lines(
                        assessment,
                        data.tied_candidate_ids,
                    )
                )
            lines.append(f"Unranked candidates: {len(unranked)}")
            for assessment in unranked:
                lines.extend(
                    self._obtainability_candidate_lines(
                        assessment,
                        data.tied_candidate_ids,
                    )
                )
        elif isinstance(data, ReadOnlyFailureData):
            lines.append(f"Reason: {data.reason}")
        lines.extend(self._obtainability_provenance_lines(result))
        self.query_one("#obtainability-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))

    @staticmethod
    def _obtainability_provenance_lines(
        result: OperationResult[Any],
    ) -> tuple[str, ...]:
        lines: list[str] = []
        for item in result.provenance:
            request = (
                item.request_identity.value
                if item.request_identity is not None
                else "none"
            )
            interval_start = (
                item.interval_started_at.isoformat()
                if item.interval_started_at is not None
                else "none"
            )
            interval_finish = (
                item.interval_finished_at.isoformat()
                if item.interval_finished_at is not None
                else "none"
            )
            status = (
                item.lifecycle_or_preview_status.value
                if item.lifecycle_or_preview_status is not None
                else "none"
            )
            lines.extend(
                (
                    f"Evidence source: {item.source.value} · "
                    f"coverage {item.coverage.value} · "
                    f"observed {item.observed_at.isoformat()} · request {request}",
                    "Evidence interval: "
                    f"{interval_start} through {interval_finish} · status {status}",
                )
            )
        return tuple(lines)

    @staticmethod
    def _obtainability_candidate_lines(
        assessment: RankedCandidate,
        tied_candidate_ids: frozenset[str],
    ) -> tuple[str, ...]:
        """Render one ranked or unranked candidate from domain-owned facts."""
        candidate = assessment.candidate
        machine = candidate.machine
        lines = [
            f"Candidate identity: {candidate.candidate_id}",
            f"Endpoint region: {candidate.endpoint_region}",
            "Candidate zones: " + (", ".join(candidate.zones) or "none"),
            f"Machine type: {machine.machine_type}",
            "GPU: "
            + (
                f"{machine.gpu.accelerator_type} x{machine.gpu.count}"
                if machine.gpu is not None
                else "none"
            ),
            f"Local SSD count: {machine.local_ssd_count}",
            f"VM quantity: {candidate.vm_count}",
            f"Distribution shape: {candidate.distribution_shape.value}",
            "Rank: "
            + (str(assessment.rank) if assessment.rank is not None else "unranked"),
            "Unranked reasons: "
            + (
                ", ".join(reason.value for reason in assessment.unranked_reasons)
                or "none"
            ),
            "Exact rank-component tie: "
            + (
                "yes; canonical candidate identity breaks the tie"
                if candidate.candidate_id in tied_candidate_ids
                else "no"
            ),
        ]
        advice = assessment.advice
        if advice is None:
            lines.extend(
                (
                    "Obtainability score: unavailable",
                    "Estimated uptime: unavailable",
                    "Provider shards: unavailable",
                )
            )
        else:
            band = assessment.band.value if assessment.band is not None else "unknown"
            lines.extend(
                (
                    f"Obtainability score: {advice.obtainability} ({band})",
                    f"Estimated uptime: {advice.estimated_uptime}",
                    f"Advice observed: {advice.retrieved_at.isoformat()}",
                    f"Advice source: {advice.source}",
                )
            )
            if advice.shards:
                lines.extend(
                    f"Recommended shard: {shard.zone} · "
                    f"{shard.machine_type} · {shard.vm_count} VM · "
                    f"{shard.provisioning_model}"
                    for shard in advice.shards
                )
            else:
                lines.append("Recommended shards: none returned")

        lines.append(
            "30-day p90 preemption: "
            + (
                str(assessment.preemption_p90)
                if assessment.preemption_p90 is not None
                else "unavailable"
            )
        )
        preemption = assessment.preemption_derivation
        if preemption is not None:
            lines.extend(
                "Preemption interval: "
                f"{interval.started_at.isoformat()} through "
                f"{interval.finished_at.isoformat()} · rate {interval.rate}"
                for interval in preemption.intervals
            )
            lines.append(
                "P90 derivation: nearest-rank "
                f"{preemption.nearest_rank} of {len(preemption.intervals)} = "
                f"{preemption.selected_rate}"
            )

        lines.append(
            "Total-request hourly price: "
            + (
                f"USD {assessment.total_request_hourly_price_usd}"
                if assessment.total_request_hourly_price_usd is not None
                else "unavailable"
            )
        )
        price = assessment.price_derivation
        if price is not None:
            lines.extend(
                (
                    "Price interval: "
                    f"{price.interval.started_at.isoformat()} through "
                    f"{price.interval.finished_at.isoformat()} · "
                    f"USD {price.interval.usd_per_vm_hour} per VM-hour",
                    "Price derivation: "
                    f"USD {price.interval.usd_per_vm_hour} x {price.vm_count} VMs = "
                    f"USD {price.total_request_hourly_price_usd} per hour",
                )
            )

        if assessment.history is not None:
            lines.extend(
                (
                    f"History location: {assessment.history.location}",
                    f"History observed: {assessment.history.retrieved_at.isoformat()}",
                    f"History source: {assessment.history.source}",
                    f"History preemption buckets: {len(assessment.history.preemption)}",
                    f"History price intervals: {len(assessment.history.prices)}",
                )
            )
        return tuple(lines)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Inspect quota rows or local audit rows using their typed identities."""
        if event.data_table.id == "quota-ledger" and self.active_workspace == "quotas":
            item = self._quota_items.get(str(event.row_key.value))
            if item is not None:
                self._select_quota(item)
        elif event.data_table.id == "audit-table" and self.active_workspace == "audit":
            operation_generation = self._claim_audit_operation()
            self.run_worker(
                self._inspect_audit(
                    str(event.row_key.value),
                    self._workspace_generation,
                    operation_generation,
                ),
                group="audit-inspect",
                exclusive=True,
                exit_on_error=False,
            )

    def _select_quota(self, item: QuotaQueryItem) -> None:
        if self.active_workspace != "quotas":
            return
        selector = QuotaInspectSelector(
            item.identity.service,
            item.identity.quota_id,
            item.location or "global",
            item.identity.dimensions,
        )
        self.selected_quota = QuotaSelection(item, selector)
        self._detail_route = self.layout_mode == "narrow"
        self.set_class(self._detail_route, "detail-route")
        self._last_focus_id = "quota-ledger"
        generation = self._claim_provider_view()
        self.run_worker(
            self._inspect_quota(selector, generation),
            group="quota-inspect",
            exclusive=True,
            exit_on_error=False,
        )

    async def _inspect_quota(
        self,
        selector: QuotaInspectSelector,
        generation: int,
    ) -> None:
        if not self._owns_quota_view(generation):
            return
        result = await self.read_only.inspect(
            selector,
            deadline=self._deadline(),
            scope_input=self.scope_input,
        )
        if not self._owns_quota_view(generation):
            return
        self.last_result = result
        self._render_instrument(result)
        self._render_quota_detail(result)
        if result.resource_scope is not None:
            self._show_copy_cli(quota_inspect_copy_cli(result.resource_scope, selector))

    def _render_quota_detail(self, result: OperationResult[Any]) -> None:
        data = result.data
        lines = [
            "Quota detail",
            f"Operation: {result.operation.value}",
            f"Outcome: {result.outcome.code.value}",
            "Complete: " + ("yes" if result.completeness.is_complete else "no"),
        ]
        if isinstance(data, QuotaInspectData):
            lines.extend(self._inspect_lines(data))
        elif isinstance(data, IncompleteQuotaInspectData):
            lines.extend(
                (
                    f"Selector: {data.selector.service} / {data.selector.quota_id}",
                    f"Reason: {data.reason}",
                    f"Retained matching slices: {len(data.matching_items)}",
                )
            )
        elif isinstance(data, ReadOnlyFailureData):
            lines.append(f"Reason: {data.reason}")
        self.query_one("#quota-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))

    @classmethod
    def _inspect_lines(cls, data: QuotaInspectData) -> tuple[str, ...]:
        identity = data.identity
        dimensions = ", ".join(
            f"{key}={value}" for key, value in identity.dimensions.items
        )
        lines = [
            f"Service: {identity.service}",
            f"Quota ID: {identity.quota_id}",
            f"Dimensions: {dimensions or 'none'}",
            f"Quota scope: {identity.quota_scope.value}",
        ]
        if data.item is not None:
            lines.extend(
                (
                    f"Effective: {cls._quantity(data.item.effective_value)}",
                    f"Usage: {cls._quantity(data.item.usage_value)}",
                    "Predicates: "
                    f"discovered={cls._yes_no(data.item.predicates.discovered)} "
                    f"cataloged={cls._yes_no(data.item.predicates.cataloged)} "
                    f"guided={cls._yes_no(data.item.predicates.guided)} "
                    f"mutable={cls._yes_no(data.item.predicates.mutable)}",
                )
            )
        if data.status is not None:
            lines.extend(
                (
                    f"Reconciliation: {data.status.reconciliation.value}",
                    f"Grant satisfaction: {data.status.grant_satisfaction.value}",
                    "Effective confirmation: "
                    f"{data.status.effective_confirmation.value}",
                )
            )
        if data.reason is not None:
            lines.append(f"Reason: {data.reason}")
        return tuple(lines)

    async def _inspect_audit(
        self,
        record_id: str,
        workspace_generation: int,
        operation_generation: int,
    ) -> None:
        result = await self.audit.inspect(record_id)
        if not self._owns_audit_operation(
            workspace_generation,
            operation_generation,
        ):
            return
        self.last_result = result
        data = result.data
        lines = [
            f"Operation: {result.operation.value}",
            f"Outcome: {result.outcome.code.value}",
        ]
        if isinstance(data, AuditInspectData):
            lines.append(f"Record ID: {data.record_id or 'none'}")
            if data.record is not None:
                lines.extend(
                    (
                        f"Sequence: {data.record.sequence}",
                        f"Kind: {data.record.draft.kind.value}",
                        f"Previous hash: {data.record.previous_hash}",
                        f"Record hash: {data.record.record_hash}",
                    )
                )
        self.query_one("#audit-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))

    async def _verify_audit(
        self,
        workspace_generation: int,
        operation_generation: int,
    ) -> None:
        """Verify the complete retained audit chain through the shared operation."""
        result = await self.audit.verify()
        if not self._owns_audit_operation(
            workspace_generation,
            operation_generation,
        ):
            return
        self.last_result = result
        data = result.data
        lines = [
            f"Operation: {result.operation.value}",
            f"Outcome: {result.outcome.code.value}",
        ]
        if isinstance(data, AuditVerifyData) and data.verification is not None:
            verification = data.verification
            lines.extend(
                (
                    f"Chain valid: {self._yes_no(verification.valid)}",
                    f"Verified from: {verification.verified_from or 'none'}",
                    f"Verified through: {verification.verified_through or 'none'}",
                )
            )
            if verification.failure is not None:
                lines.append(f"Failure: {verification.failure.code.value}")
        self.query_one("#audit-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))

    async def resolve_workload(
        self,
        requirement: ComputeInstanceRequirement | CloudTpuSliceRequirement,
        generation: int,
    ) -> OperationResult[Any]:
        """Resolve one typed workload and retain its exact cross-surface result."""
        result = await self.read_only.resolve(
            requirement,
            deadline=self._deadline(),
            scope_input=self.scope_input,
        )
        if not self._owns_quota_view(generation):
            return result
        self.last_result = result
        self._render_instrument(result)
        self._render_workload_result(result)
        if result.resource_scope is not None:
            self._show_copy_cli(
                quota_resolve_copy_cli(result.resource_scope, requirement)
            )
        return result

    def _render_workload_result(self, result: OperationResult[Any]) -> None:
        self.query_one("#workload-form").add_class("hidden")
        self.query_one("#quota-detail").remove_class("hidden")
        self.query_one("#workload-obtainability").add_class("hidden")
        self._resolved_compute = None
        lines = [
            "Workload resolution",
            f"Outcome: {result.outcome.code.value}",
            "Complete: " + ("yes" if result.completeness.is_complete else "no"),
        ]
        if isinstance(result.data, ResolvedWorkloadRequirement):
            lines.append(f"Requirement: {result.data.requirement.kind.value}")
            for location in result.data.locations:
                lines.extend(
                    (
                        f"Location: {location.location}",
                        f"Disposition: {location.disposition.value}",
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
            requirement = result.data.requirement
            if (
                isinstance(requirement, ComputeInstanceRequirement)
                and requirement.provisioning_model is ProvisioningModel.SPOT
                and any(
                    location.disposition is WorkloadLocationDisposition.COMPATIBLE
                    for location in result.data.locations
                )
            ):
                self._resolved_compute = result.data
                self.query_one("#workload-obtainability").remove_class("hidden")
        elif isinstance(result.data, ReadOnlyFailureData):
            lines.append(f"Reason: {result.data.reason}")
        self.query_one("#quota-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))
