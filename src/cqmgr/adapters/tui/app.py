"""Textual shell for read-only Cloud Quota Manager operations."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast, override

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, Static

from cqmgr.adapters.cli.copy_cli import (
    quota_inspect_copy_cli,
    quota_list_copy_cli,
    quota_resolve_copy_cli,
)
from cqmgr.adapters.cli.read_only_requests import (
    parse_cloud_tpu_slice_requirement,
    parse_compute_instance_requirement,
)
from cqmgr.application.operations.audit import (
    AuditInspectData,
    AuditListData,
    AuditVerifyData,
)
from cqmgr.application.operations.quotas import QuotaBrowseData, QuotaInspectData
from cqmgr.application.operations.read_only import (
    IncompleteQuotaInspectData,
    QuotaInspectSelector,
    ReadOnlyFailureData,
    ReadOnlyQuotaQuery,
    ReadOnlyScopeInput,
)
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.domain.accelerator_overlay import (
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ResolvedWorkloadRequirement,
)
from cqmgr.domain.audit import AuditQuery
from cqmgr.domain.quota_queries import QuotaQueryFilters, QuotaQueryItem

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual import events
    from textual.binding import BindingType
    from textual.worker import Worker

    from cqmgr.domain.quotas import QuotaQuantity
    from cqmgr.domain.results import OperationResult


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


class CloudQuotaManagerApp(App[None]):
    """Adaptive Textual shell over typed read-only operations."""

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

    #quota-workbench, #audit-workspace, #obtainability-workspace {
        height: 1fr;
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

    #obtainability-workspace {
        padding: 2;
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

    def __init__(  # noqa: PLR0913 - complete surface composition contract
        self,
        read_only: ReadOnlyOperationsLike,
        audit: AuditOperationsLike,
        *,
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
        self.scope_input = scope_input
        self.scope_locked = scope_locked
        self._monotonic = monotonic
        self.layout_mode = "medium"
        self.active_workspace = "quotas"
        self.current_query = ReadOnlyQuotaQuery()
        self.last_result: OperationResult[Any] | None = None
        self.last_copied_cli: str | None = None
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

    @override
    def compose(self) -> ComposeResult:
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
                yield Button("Copy CLI", id="copy-cli")
                yield Static(
                    "Copy CLI unavailable until an operation is fully specified.",
                    id="copy-cli-preview",
                    markup=False,
                )
        with Vertical(id="obtainability-workspace", classes="hidden"):
            yield Static(
                "Obtainability\nExact Spot VM comparison arrives in the next "
                "implementation slice. Quota evidence remains distinct from capacity.",
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

    async def _load_quotas(
        self,
        query: ReadOnlyQuotaQuery,
        cancellation: CancellationToken,
        generation: int,
    ) -> None:
        if cancellation.cancelled or not self._owns_provider_view(generation):
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
            if self._owns_provider_view(generation):
                self._set_status("CANCELLED — prior provider read superseded")
            raise
        except Exception:  # noqa: BLE001 - no typed result exists for worker failure
            if self._owns_provider_view(generation):
                self._set_status(
                    "ERROR — quota inventory unavailable; retry the read-only operation"
                )
            return
        if cancellation.cancelled or not self._owns_provider_view(generation):
            if self._owns_provider_view(generation):
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

    @staticmethod
    def _quantity(value: QuotaQuantity | None) -> str:
        return "unavailable" if value is None else f"{value.value} {value.unit.symbol}"

    @staticmethod
    def _yes_no(value: bool) -> str:  # noqa: FBT001 - semantic formatter
        return "yes" if value else "no"

    def _deadline(self) -> float:
        return cast("float", self._monotonic()) + self._PROVIDER_OPERATION_SECONDS

    def _show_copy_cli(self, command: str) -> None:
        self.last_copied_cli = command
        self.query_one("#copy-cli-preview", Static).update(command)

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
        self._set_active_workspace(workspace)

    def _set_active_workspace(self, workspace: str) -> None:
        if workspace not in {"quotas", "obtainability", "audit"}:
            return
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
        if self.active_workspace == "quotas":
            self.query_one("#filter-text", Input).focus()

    def action_return_to_ledger(self) -> None:
        """Return from a narrow detail route and restore ledger focus."""
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
        if self.active_workspace == "quotas":
            self._start_quota_load(self.current_query)

    def action_help(self) -> None:
        """Expose the keyboard contract without depending on glyphs or color."""
        self.notify(
            "Tab/Shift-Tab move focus; arrows move rows; Enter opens; "
            "Escape returns; / focuses filters; Ctrl-K opens commands.",
            title="Keyboard help",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route explicit shell and filter controls."""
        button_id = event.button.id
        if button_id and button_id.startswith("workspace-"):
            self._set_active_workspace(button_id.removeprefix("workspace-"))
        elif button_id == "apply-filters":
            self._apply_filters()
        elif button_id == "detail-back":
            self.action_return_to_ledger()
        elif button_id == "resolve-compute":
            self._open_workload_route("compute-instance")
        elif button_id == "resolve-tpu":
            self._open_workload_route("cloud-tpu-slice")
        elif button_id == "workload-submit":
            self._submit_workload()
        elif button_id == "audit-verify":
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
        elif button_id == "copy-cli" and self.last_copied_cli is not None:
            self.copy_to_clipboard(self.last_copied_cli)

    def _apply_filters(self) -> None:
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
        self.run_worker(
            self.resolve_workload(requirement),
            group="workload-resolve",
            exclusive=True,
            exit_on_error=False,
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Inspect quota rows or local audit rows using their typed identities."""
        if event.data_table.id == "quota-ledger":
            item = self._quota_items.get(str(event.row_key.value))
            if item is not None:
                self._select_quota(item)
        elif event.data_table.id == "audit-table":
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
        if not self._owns_provider_view(generation):
            return
        result = await self.read_only.inspect(
            selector,
            deadline=self._deadline(),
            scope_input=self.scope_input,
        )
        if not self._owns_provider_view(generation):
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
    ) -> OperationResult[Any]:
        """Resolve one typed workload and retain its exact cross-surface result."""
        generation = self._claim_provider_view()
        result = await self.read_only.resolve(
            requirement,
            deadline=self._deadline(),
            scope_input=self.scope_input,
        )
        if not self._owns_provider_view(generation):
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
        elif isinstance(result.data, ReadOnlyFailureData):
            lines.append(f"Reason: {result.data.reason}")
        self.query_one("#quota-detail", Static).update("\n".join(lines))
        self._set_status(self._result_status(result))
