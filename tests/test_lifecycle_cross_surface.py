"""Cross-surface acceptance proof for the shared mutation lifecycle facade."""

# Hermetic protocol doubles intentionally expose their calls as test evidence.
# ruff: noqa: ANN401, PLR2004, SLF001

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, cast, override

import pytest
from click.testing import CliRunner
from textual.widgets import Input, Static

import cqmgr.cli as cli_module
from cqmgr.adapters.cli.lifecycle import (
    LifecycleCliRuntime,
    PlanReferenceInput,
    RequestCompositionInput,
    WatchCliInput,
)
from cqmgr.adapters.persistence.apply_records import LocalApplyRecordRepository
from cqmgr.adapters.persistence.audit import FilesystemAuditJournal
from cqmgr.adapters.persistence.plans import LocalPlanRepository
from cqmgr.adapters.persistence.watch import LocalWatchCheckpointRepository
from cqmgr.adapters.serialization.plans import PlanCodec
from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.adapters.serialization.watch import HmacWatchResumeCodec
from cqmgr.adapters.tui.app import CloudQuotaManagerApp
from cqmgr.application.operations.apply import (
    ApplyPlanOperations,
    ApplyRequest,
)
from cqmgr.application.operations.lifecycle import LifecycleOperations
from cqmgr.application.operations.plans import (
    ComposeChild,
    ComposeRequest,
    PlanReviewRequest,
    PreviewRequest,
    RequestPlanOperations,
)
from cqmgr.application.operations.watch import (
    WatchOperations,
    WatchRequest,
)
from cqmgr.application.ports.apply import ApplyRevalidation, RefreshedApplyChild
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    CancellationToken,
)
from cqmgr.application.ports.provider_writes import (
    QuotaPreferenceUnknownResolutionResult,
    QuotaPreferenceWrite,
    QuotaPreferenceWriteResult,
    UnknownWriteResolution,
)
from cqmgr.application.ports.secrets import (
    SecretBackendKind,
    SecretPurpose,
    SecretStoreOutcome,
    SecretStoreProbe,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
)
from cqmgr.application.ports.watch import WatchObservation
from cqmgr.domain.apply_records import ApplyChildDisposition
from cqmgr.domain.audit import AuditFactName, AuditQuery
from cqmgr.domain.diagnostics import (
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
)
from cqmgr.domain.identity import PrincipalIdentity
from cqmgr.domain.plans import (
    ContactBinding,
    PlanKind,
    PlanPrincipal,
    QuotaPlan,
    TargetStrategy,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import OperationResult, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    QuotaRequestStatus,
    Reconciliation,
    WatchCondition,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cqmgr.adapters.cli.lifecycle import LifecycleCliRequestFactory
    from cqmgr.application.ports.apply import ApplyRevalidator
    from cqmgr.application.ports.apply_records import ApplyRecordRepository
    from cqmgr.application.ports.audit import AuditJournal
    from cqmgr.application.ports.coordination import BudgetRequest
    from cqmgr.application.ports.plans import PlanCodec as PlanCodecPort
    from cqmgr.application.ports.plans import PlanRepository
    from cqmgr.application.ports.provider_writes import (
        QuotaPreferenceUnknownResolver,
        QuotaPreferenceWriter,
    )
    from cqmgr.application.ports.watch import (
        WatchCheckpointRepository,
        WatchObservationRequest,
    )
    from cqmgr.domain.audit import AuditRecordDraft
    from cqmgr.domain.watch import WatchStreamEvent

NOW = datetime(2026, 7, 24, 12, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
UNIT = QuotaUnit("1")
KEY = SecretValue(b"k" * 32)
PRINCIPAL = PlanPrincipal("principal://accounts/123")
CONTACT = ContactBinding(
    StableSymbol("direct-user"),
    PRINCIPAL.stable_identity,
    "hmac-sha256:" + ("c" * 64),
)
INSTALLATION_ID = "installation-123"


class _MemoryConsumptionStore:
    """Avoid every native keyring or credential boundary in this acceptance test."""

    def __init__(self) -> None:
        self.values: dict[SecretStoreReference, SecretValue] = {}

    def probe(self) -> SecretStoreProbe:
        return SecretStoreProbe(SecretBackendKind.MACOS_KEYCHAIN, "test-memory")

    def get_consumption_marker(
        self,
        reference: SecretStoreReference,
    ) -> SecretStoreOutcome:
        value = self.values.get(reference)
        if value is None:
            return SecretStoreOutcome(SecretStoreStatus.MISSING)
        return SecretStoreOutcome.available(value)

    def create_consumption_marker(
        self,
        reference: SecretStoreReference,
        secret: SecretValue,
    ) -> SecretStoreOutcome:
        if reference in self.values:
            return SecretStoreOutcome(SecretStoreStatus.CONFLICT)
        self.values[reference] = secret
        return SecretStoreOutcome(SecretStoreStatus.CREATED)

    def delete(self, reference: SecretStoreReference) -> SecretStoreOutcome:
        if reference.purpose is SecretPurpose.PLAN_CONSUMPTION:
            return SecretStoreOutcome(SecretStoreStatus.UNSUPPORTED)
        if self.values.pop(reference, None) is None:
            return SecretStoreOutcome(SecretStoreStatus.MISSING)
        return SecretStoreOutcome(SecretStoreStatus.DELETED)


class _CurrentSourceRevalidator:
    """Return independently known current facts from the Preview source."""

    def __init__(self, kind: PlanKind) -> None:
        self._children = {
            child.child_id: child for child in _composition(kind).children
        }

    async def refresh(self, plan: QuotaPlan, _now: datetime) -> ApplyRevalidation:
        return ApplyRevalidation(
            resource_scope=plan.resource_scope,
            principal=plan.principal,
            contact_binding=plan.contact_binding,
            contact_value="operator@example.com",
            constraints=plan.constraints,
            children=tuple(
                RefreshedApplyChild(
                    child_id=child.child_id,
                    slice_identity=child.slice_identity,
                    effective=self._children[child.child_id].effective,
                    usage=self._children[child.child_id].usage,
                    preference_name=self._children[child.child_id].preference_name,
                    preference_etag=self._children[child.child_id].preference_etag,
                    evidence=self._children[child.child_id].evidence,
                )
                for child in plan.children
            ),
        )


class _AcceptedWriter:
    """Accept each ordered write without reaching a provider."""

    def __init__(self) -> None:
        self.requests: list[QuotaPreferenceWrite] = []

    async def dispatch(
        self,
        request: QuotaPreferenceWrite,
    ) -> QuotaPreferenceWriteResult:
        self.requests.append(request)
        return QuotaPreferenceWriteResult(
            accepted=True,
            outcome=StableSymbol("submitted"),
            etag=f"etag-{request.child_id}",
            trace_id=f"trace-{request.child_id}",
        )


class _UnknownResolver:
    async def resolve_unknown(
        self,
        _request: QuotaPreferenceWrite,
    ) -> QuotaPreferenceUnknownResolutionResult:
        return QuotaPreferenceUnknownResolutionResult(UnknownWriteResolution.UNRESOLVED)


class _Clock:
    def __init__(self) -> None:
        self.wall = NOW + timedelta(minutes=2)
        self.monotonic_value = 100.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    async def sleep(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class _Budgets:
    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        assert deadline > 100
        cancellation.raise_if_cancelled()
        return BudgetGrant(100.0, request)


class _NoJitter:
    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        assert attempt >= 0
        assert identity
        return delay


class _SettledReader:
    """Return a settled grant with the accepted Apply lineage."""

    async def observe(
        self,
        request: WatchObservationRequest,
    ) -> WatchObservation:
        child = request.child
        status = QuotaRequestStatus.derive(
            reconciliation=Reconciliation.SETTLED,
            baseline=child.baseline,
            desired=child.target,
            granted=child.target,
            effective=child.target,
            status_observed_at=NOW + timedelta(minutes=2),
            effective_observed_at=NOW + timedelta(minutes=2),
        )
        return WatchObservation(
            status=status,
            preference_target=child.target,
            etag=child.lineage_etag,
            trace_id=child.lineage_trace_id,
            observed_at=NOW + timedelta(minutes=2),
        )


class _OfflineReads:
    """Keep Textual mounted without any provider access."""

    async def browse(self, *_args: object, **_kwargs: object) -> Any:
        message = "provider reads are intentionally unavailable"
        raise OSError(message)

    async def aclose(self) -> None:
        return None


class _UnusedAuditSurface:
    pass


class _RequestFactory:
    """Resolve CLI adapter values to the same protected test context."""

    def __init__(self, clock: _Clock, kind: PlanKind) -> None:
        self.clock = clock
        self.kind = kind

    def compose(self, value: RequestCompositionInput) -> ComposeRequest:
        assert value.target_strategy is TargetStrategy.MANUAL
        assert (value.selector is not None) == (self.kind is PlanKind.SINGLE)
        return _composition(self.kind)

    def preview(self, value: RequestCompositionInput) -> PreviewRequest:
        return replace(_preview_request(self.kind), plan_out=value.plan_out)

    def review(self, value: PlanReferenceInput) -> PlanReviewRequest:
        return PlanReviewRequest(
            digest=value.digest,
            path=value.path,
            authentication_key=KEY if value.digest is not None else None,
            local_installation_id=INSTALLATION_ID,
            now=NOW + timedelta(minutes=1),
        )

    def apply(
        self,
        value: PlanReferenceInput,
        acknowledgement: str,
        *,
        quota_contact: SecretValue | None = None,
    ) -> ApplyRequest:
        del quota_contact
        assert value.digest is not None
        return ApplyRequest(
            digest=value.digest,
            authentication_key=KEY,
            local_installation_id=INSTALLATION_ID,
            resource_scope_acknowledgement=ResourceScope(
                ResourceScopeKind.PROJECT,
                acknowledgement,
            ),
            principal=PRINCIPAL,
            contact_binding=CONTACT,
            contact_value="operator@example.com",
            now=NOW + timedelta(minutes=1),
        )

    def watch(self, value: WatchCliInput) -> WatchRequest:
        return WatchRequest(
            intent_id=value.intent_id,
            condition=value.condition,
            resume=value.resume,
            authentication_key=KEY,
            installation_id=INSTALLATION_ID,
            deadline=self.clock.monotonic() + 10,
            cancellation=CancellationToken(),
        )


@dataclass
class _Harness:
    facade: LifecycleOperations
    factory: _RequestFactory
    audit: FilesystemAuditJournal
    writer: _AcceptedWriter
    clock: _Clock


def _slice(quota_id: str) -> EffectiveQuotaSliceIdentity:
    return EffectiveQuotaSliceIdentity(
        resource_scope=SCOPE,
        service="compute.googleapis.com",
        quota_id=quota_id,
        dimensions=NormalizedDimensions((("region", "us-central1"),)),
        quota_scope=QuotaScope.REGIONAL,
    )


def _composition(kind: PlanKind) -> ComposeRequest:
    children = (
        ComposeChild(
            child_id="direct" if kind is PlanKind.BUNDLE else "single",
            slice_identity=_slice("GPU-DIRECT"),
            effective=QuotaQuantity(4, UNIT),
            usage=QuotaQuantity(2, UNIT),
            workload=QuotaQuantity(4, UNIT),
            manual_target=QuotaQuantity(8, UNIT),
            direct_accelerator_rank=0,
            scope_breadth_rank=1,
            observed_at=NOW,
        ),
        *(
            (
                ComposeChild(
                    child_id="companion",
                    slice_identity=_slice("GPU-ALL"),
                    effective=QuotaQuantity(5, UNIT),
                    usage=QuotaQuantity(3, UNIT),
                    workload=QuotaQuantity(4, UNIT),
                    manual_target=QuotaQuantity(9, UNIT),
                    direct_accelerator_rank=1,
                    scope_breadth_rank=2,
                    observed_at=NOW,
                ),
            )
            if kind is PlanKind.BUNDLE
            else ()
        ),
    )
    return ComposeRequest(
        kind=kind,
        strategy=TargetStrategy.MANUAL,
        resource_scope=SCOPE,
        children=children,
        selected_location="us-central1" if kind is PlanKind.BUNDLE else None,
    )


def _preview_request(kind: PlanKind) -> PreviewRequest:
    return PreviewRequest(
        composition=_composition(kind),
        principal=PRINCIPAL,
        contact_binding=CONTACT,
        installation_id=INSTALLATION_ID,
        authentication_key=KEY,
        identity_verified=True,
        contact_verified=True,
        keyring_mutation_capable=True,
        normalized_workload=(
            "compute-instance:a4-highgpu-8g:1"
            if kind is PlanKind.BUNDLE
            else "exact-slice"
        ),
        now=NOW,
    )


def _harness(root: Path, kind: PlanKind) -> _Harness:
    repository = LocalPlanRepository(root / "plans", _MemoryConsumptionStore())
    apply_records = LocalApplyRecordRepository(root / "apply")
    audit = FilesystemAuditJournal(root / "audit")
    writer = _AcceptedWriter()
    clock = _Clock()
    plans = RequestPlanOperations(
        repository=cast("PlanRepository", repository),
        audit=cast("AuditJournal", audit),
        codec=cast("PlanCodecPort", PlanCodec()),
    )
    apply = ApplyPlanOperations(
        repository=cast("PlanRepository", repository),
        apply_records=cast("ApplyRecordRepository", apply_records),
        audit=cast("AuditJournal", audit),
        codec=cast("PlanCodecPort", PlanCodec()),
        revalidator=cast("ApplyRevalidator", _CurrentSourceRevalidator(kind)),
        writer=cast("QuotaPreferenceWriter", writer),
        unknown_resolver=cast(
            "QuotaPreferenceUnknownResolver",
            _UnknownResolver(),
        ),
    )
    watch = WatchOperations(
        apply_records=cast("ApplyRecordRepository", apply_records),
        checkpoints=cast(
            "WatchCheckpointRepository",
            LocalWatchCheckpointRepository(root / "watch"),
        ),
        resume_codec=HmacWatchResumeCodec(),
        reader=_SettledReader(),
        budgets=_Budgets(),
        clock=clock,
        stream_ids=lambda: "stream-cross-surface",
        jitter=_NoJitter(),
        poll_interval_seconds=1,
    )
    facade = LifecycleOperations(plans, apply, watch)
    return _Harness(facade, _RequestFactory(clock, kind), audit, writer, clock)


async def _textual_preview(
    facade: LifecycleOperations,
    request: PreviewRequest,
) -> OperationResult[Any]:
    app = CloudQuotaManagerApp(
        _OfflineReads(),  # type: ignore[arg-type]
        _UnusedAuditSurface(),  # type: ignore[arg-type]
        lifecycle=facade,
    )
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        result = app.open_preview(request)
        await pilot.pause()
        assert app.last_result is result
        assert result.data.plan_digest is not None
        assert result.data.plan_digest in str(
            app.query_one("#lifecycle-detail", Static).content
        )
        return cast("OperationResult[Any]", result)


def _json_result(
    runner: CliRunner,
    arguments: list[str],
) -> dict[str, object]:
    result = runner.invoke(cli_module.main, arguments)
    assert result.exit_code == 0, result.output
    return cast("dict[str, object]", json.loads(result.stdout))


class _RecordingApp(CloudQuotaManagerApp):
    """Retain every complete typed Watch event consumed by Textual."""

    def __init__(self, facade: LifecycleOperations) -> None:
        super().__init__(
            _OfflineReads(),  # type: ignore[arg-type]
            _UnusedAuditSurface(),  # type: ignore[arg-type]
            lifecycle=facade,
        )
        self.watch_events: list[WatchStreamEvent] = []

    @override
    def _render_watch_event(self, event: WatchStreamEvent) -> None:
        self.watch_events.append(event)
        super()._render_watch_event(event)


async def _textual_review_apply_watch(
    harness: _Harness,
    digest: str,
) -> tuple[
    OperationResult[Any],
    OperationResult[Any],
    tuple[WatchStreamEvent, ...],
]:
    """Drive Review, Apply, and initial Watch through one Textual session."""
    app = _RecordingApp(harness.facade)
    review_request = harness.factory.review(PlanReferenceInput(digest, None))
    apply_request = harness.factory.apply(
        PlanReferenceInput(digest, None),
        SCOPE.canonical_name,
    )
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        review = app.open_plan_review(review_request)
        app.prepare_apply(apply_request)
        app.query_one(
            "#apply-scope-acknowledgement", Input
        ).value = SCOPE.canonical_name
        app._submit_lifecycle_apply()
        await pilot.pause()
        await pilot.pause()
        applied = app.last_result
        assert applied is not None
        assert applied.operation.value == "plan.apply"
        assert applied.data.intent_id is not None
        app.prepare_watch(
            WatchRequest(
                intent_id=applied.data.intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id=INSTALLATION_ID,
                deadline=harness.clock.monotonic() + 10,
                cancellation=CancellationToken(),
            ),
            bound_scope=SCOPE,
        )
        app._submit_lifecycle_watch()
        await pilot.pause()
        await pilot.pause()
        assert app.watch_events
        return review, applied, tuple(app.watch_events)


async def _textual_resume_watch(
    harness: _Harness,
    resume: str,
) -> tuple[WatchStreamEvent, ...]:
    """Resume one Watch stream through Textual and retain every typed event."""
    app = _RecordingApp(harness.facade)
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        app.prepare_watch(
            WatchRequest(
                intent_id=None,
                condition=None,
                resume=resume,
                authentication_key=KEY,
                installation_id=INSTALLATION_ID,
                deadline=harness.clock.monotonic() + 10,
                cancellation=CancellationToken(),
            ),
            bound_scope=SCOPE,
        )
        app._submit_lifecycle_watch()
        await pilot.pause()
        await pilot.pause()
        assert app.watch_events
        return tuple(app.watch_events)


def _install_cli_runtime(
    monkeypatch: pytest.MonkeyPatch,
    harness: _Harness,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "build_lifecycle_cli_runtime",
        lambda: LifecycleCliRuntime(
            harness.facade,
            cast("LifecycleCliRequestFactory", harness.factory),
        ),
    )


def _cli_result(
    monkeypatch: pytest.MonkeyPatch,
    harness: _Harness,
    arguments: list[str],
) -> tuple[OperationResult[Any], dict[str, object]]:
    """Return both the typed result and complete CLI JSON representation."""
    captured: list[OperationResult[Any]] = []
    original = cli_module.emit_lifecycle_result

    def capture(result: OperationResult[Any], presentation: object) -> int:
        captured.append(result)
        return original(result, presentation)  # type: ignore[arg-type]

    with monkeypatch.context() as context:
        _install_cli_runtime(context, harness)
        context.setattr(cli_module, "emit_lifecycle_result", capture)
        mapping = _json_result(CliRunner(), arguments)
    assert len(captured) == 1
    assert mapping == operation_result_mapping(captured[0])
    return captured[0], mapping


def _cli_watch(
    monkeypatch: pytest.MonkeyPatch,
    harness: _Harness,
    arguments: list[str],
) -> tuple[tuple[WatchStreamEvent, ...], tuple[dict[str, object], ...]]:
    """Return every typed event and its complete CLI JSONL representation."""
    captured: list[WatchStreamEvent] = []
    original = cli_module.emit_watch_event

    def capture(event: WatchStreamEvent, presentation: object) -> None:
        captured.append(event)
        original(event, presentation)  # type: ignore[arg-type]

    with monkeypatch.context() as context:
        _install_cli_runtime(context, harness)
        context.setattr(cli_module, "emit_watch_event", capture)
        result = CliRunner().invoke(cli_module.main, arguments)
    assert result.exit_code == 0, result.output
    mappings = tuple(
        cast("dict[str, object]", json.loads(line))
        for line in result.stdout.splitlines()
    )
    assert len(mappings) == len(captured)
    assert mappings == tuple(
        cast("dict[str, object]", _watch_value(event)) for event in captured
    )
    return tuple(captured), mappings


def _watch_value(value: object) -> object:  # noqa: C901, PLR0911
    """Independently map every typed Watch fact to its public JSON value."""
    if isinstance(value, OperationResult):
        return operation_result_mapping(value)
    if isinstance(value, ResourceScope):
        return {"type": value.kind.value, "name": value.canonical_name}
    if isinstance(value, QuotaQuantity):
        return {"value": value.base10, "unit": value.unit.symbol}
    if isinstance(
        value,
        (
            StableSymbol,
            DiagnosticCode,
            DiagnosticPhase,
            DiagnosticSource,
            PrincipalIdentity,
            RedactedText,
        ),
    ):
        return value.value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _watch_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _watch_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_watch_value(item) for item in value]
    return value


def _cli_preview_arguments(kind: PlanKind) -> list[str]:
    common = [
        "request",
        "preview",
        "--resource-scope",
        SCOPE.canonical_name,
        "--output",
        "json",
    ]
    if kind is PlanKind.SINGLE:
        return [
            *common,
            "--service",
            "compute.googleapis.com",
            "--quota-id",
            "GPU-DIRECT",
            "--location",
            "us-central1",
            "--target",
            "8",
        ]
    return [
        *common,
        "--machine-type",
        "a4-highgpu-8g",
        "--instance-count",
        "1",
        "--provisioning-model",
        "standard",
        "--candidate",
        "us-central1-a",
        "--target-strategy",
        "manual",
        "--target",
        "direct=8",
        "--target",
        "companion=9",
    ]


def _review_arguments(digest: str) -> list[str]:
    return ["plan", "review", "--plan", digest, "--output", "json"]


def _apply_arguments(digest: str) -> list[str]:
    return [
        "plan",
        "apply",
        "--plan",
        digest,
        "--acknowledge-resource-scope",
        SCOPE.canonical_name,
        "--output",
        "json",
    ]


def _initial_watch_arguments(intent_id: str) -> list[str]:
    return [
        "request",
        "watch",
        "--intent-id",
        intent_id,
        "--condition",
        "granted",
        "--deadline",
        "2026-07-25T00:00:00Z",
        "--output",
        "jsonl",
    ]


def _resume_watch_arguments(resume: str) -> list[str]:
    return [
        "request",
        "watch",
        "--resume",
        resume,
        "--deadline",
        "2026-07-25T00:00:00Z",
        "--output",
        "jsonl",
    ]


@dataclass(frozen=True)
class _WorkflowEvidence:
    preview: OperationResult[Any]
    review: OperationResult[Any]
    applied: OperationResult[Any]
    initial_watch: tuple[WatchStreamEvent, ...]
    resumed_watch: tuple[WatchStreamEvent, ...]
    audit_drafts: tuple[AuditRecordDraft, ...]
    writes: tuple[QuotaPreferenceWrite, ...]


def _audit_drafts(harness: _Harness) -> tuple[AuditRecordDraft, ...]:
    return tuple(
        record.draft for record in harness.audit.query(AuditQuery(limit=100)).records
    )


def _textual_created_workflow(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: PlanKind,
) -> _WorkflowEvidence:
    harness = _harness(root, kind)
    preview = asyncio.run(_textual_preview(harness.facade, _preview_request(kind)))
    digest = preview.data.plan_digest
    assert digest is not None
    review, _review_json = _cli_result(
        monkeypatch,
        harness,
        _review_arguments(digest),
    )
    applied, _apply_json = _cli_result(
        monkeypatch,
        harness,
        _apply_arguments(digest),
    )
    assert applied.data.intent_id is not None
    initial_watch, _initial_jsonl = _cli_watch(
        monkeypatch,
        harness,
        _initial_watch_arguments(applied.data.intent_id),
    )
    resume = initial_watch[-1].resume
    resumed_watch = asyncio.run(_textual_resume_watch(harness, resume))
    return _WorkflowEvidence(
        preview,
        review,
        applied,
        initial_watch,
        resumed_watch,
        _audit_drafts(harness),
        tuple(harness.writer.requests),
    )


def _cli_created_workflow(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: PlanKind,
) -> _WorkflowEvidence:
    harness = _harness(root, kind)
    preview, _preview_json = _cli_result(
        monkeypatch,
        harness,
        _cli_preview_arguments(kind),
    )
    digest = preview.data.plan_digest
    assert digest is not None
    review, applied, initial_watch = asyncio.run(
        _textual_review_apply_watch(harness, digest)
    )
    resume = initial_watch[-1].resume
    resumed_watch, _resumed_jsonl = _cli_watch(
        monkeypatch,
        harness,
        _resume_watch_arguments(resume),
    )
    return _WorkflowEvidence(
        preview,
        review,
        applied,
        initial_watch,
        resumed_watch,
        _audit_drafts(harness),
        tuple(harness.writer.requests),
    )


@pytest.mark.parametrize("kind", [PlanKind.SINGLE, PlanKind.BUNDLE])
def test_lifecycle_is_semantically_equal_in_both_cross_surface_directions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: PlanKind,
) -> None:
    """Either surface can create a Plan completed and resumed by the other."""
    textual_created = _textual_created_workflow(
        tmp_path / "textual" / kind.value,
        monkeypatch,
        kind,
    )
    cli_created = _cli_created_workflow(
        tmp_path / "cli" / kind.value,
        monkeypatch,
        kind,
    )

    assert operation_result_mapping(textual_created.preview) == (
        operation_result_mapping(cli_created.preview)
    )
    assert operation_result_mapping(textual_created.review) == (
        operation_result_mapping(cli_created.review)
    )
    assert operation_result_mapping(textual_created.applied) == (
        operation_result_mapping(cli_created.applied)
    )
    assert textual_created.preview.diagnostics == cli_created.preview.diagnostics
    assert textual_created.review.diagnostics == cli_created.review.diagnostics
    assert textual_created.applied.diagnostics == cli_created.applied.diagnostics
    assert textual_created.initial_watch == cli_created.initial_watch
    assert textual_created.resumed_watch == cli_created.resumed_watch
    assert textual_created.initial_watch[-1].resume == (
        cli_created.initial_watch[-1].resume
    )
    assert textual_created.resumed_watch[-1].resume == (
        cli_created.resumed_watch[-1].resume
    )
    assert textual_created.audit_drafts == cli_created.audit_drafts
    assert textual_created.writes == cli_created.writes
    assert textual_created.preview.data.plan is not None
    assert tuple(
        child.disposition for child in textual_created.applied.data.children
    ) == (ApplyChildDisposition.ACCEPTED,) * len(
        textual_created.preview.data.plan.children
    )
    assert all(
        child.usage is not None for child in textual_created.preview.data.plan.children
    )
    assert textual_created.initial_watch[-1].result is not None
    assert textual_created.initial_watch[-1].result.diagnostics == ()
    assert any(
        fact.name is AuditFactName.PLAN_DIGEST
        for draft in textual_created.audit_drafts
        for fact in draft.facts
    )
    assert any(
        fact.name is AuditFactName.DISPOSITION and fact.value.value == "accepted"
        for draft in textual_created.audit_drafts
        for fact in draft.facts
    )
