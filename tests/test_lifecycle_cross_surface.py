"""Cross-surface acceptance proof for the shared mutation lifecycle facade."""

# Hermetic protocol doubles intentionally expose their calls as test evidence.
# ruff: noqa: ANN401, PLR2004, SLF001

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from click.testing import CliRunner
from textual.widgets import Static

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


class _CurrentPlanRevalidator:
    """Return exact current facts from the hermetic Preview plan."""

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
                    effective=child.effective,
                    usage=child.usage,
                    preference_name=child.preference_name,
                    preference_etag=child.preference_etag,
                    evidence=child.evidence,
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

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def compose(self, value: RequestCompositionInput) -> ComposeRequest:
        del value
        message = "cross-surface test composes through Textual"
        raise AssertionError(message)

    def preview(self, value: RequestCompositionInput) -> PreviewRequest:
        del value
        message = "cross-surface test previews through Textual"
        raise AssertionError(message)

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
    ) -> ApplyRequest:
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


def _harness(root: Path) -> _Harness:
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
        revalidator=cast("ApplyRevalidator", _CurrentPlanRevalidator()),
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
    return _Harness(facade, _RequestFactory(clock), audit, writer, clock)


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


async def _textual_watch(
    facade: LifecycleOperations,
    request: WatchRequest,
) -> tuple[OperationResult[Any], str]:
    app = CloudQuotaManagerApp(
        _OfflineReads(),  # type: ignore[arg-type]
        _UnusedAuditSurface(),  # type: ignore[arg-type]
        lifecycle=facade,
    )
    async with app.run_test(size=(110, 38)) as pilot:
        await pilot.pause()
        app.prepare_watch(request, bound_scope=SCOPE)
        app._submit_lifecycle_watch()
        await pilot.pause()
        await pilot.pause()
        assert app.last_result is not None
        resume = app._lifecycle_state.latest_resume
        assert resume is not None
        detail = str(app.query_one("#lifecycle-detail", Static).content)
        assert "Terminal outcome: granted" in detail
        assert "Resume token: available" in detail
        return app.last_result, resume


def _json_result(
    runner: CliRunner,
    arguments: list[str],
) -> dict[str, object]:
    result = runner.invoke(cli_module.main, arguments)
    assert result.exit_code == 0, result.output
    return cast("dict[str, object]", json.loads(result.stdout))


@pytest.mark.parametrize("kind", [PlanKind.SINGLE, PlanKind.BUNDLE])
def test_textual_plan_is_reviewed_applied_and_resumed_through_cli(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: PlanKind,
) -> None:
    """Both surfaces preserve the same typed lifecycle and durable evidence."""
    harness = _harness(tmp_path / kind.value)
    monkeypatch.setattr(
        cli_module,
        "build_lifecycle_cli_runtime",
        lambda: LifecycleCliRuntime(
            harness.facade,
            cast("LifecycleCliRequestFactory", harness.factory),
        ),
    )
    captured_results: list[OperationResult[Any]] = []
    original_emit_result = cli_module.emit_lifecycle_result

    def capture_result(
        result: OperationResult[Any],
        presentation: object,
    ) -> int:
        captured_results.append(result)
        return original_emit_result(result, presentation)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "emit_lifecycle_result", capture_result)

    preview = asyncio.run(_textual_preview(harness.facade, _preview_request(kind)))
    digest = preview.data.plan_digest
    assert digest is not None
    assert preview.data.plan is not None

    runner = CliRunner()
    review_json = _json_result(
        runner,
        ["plan", "review", "--plan", digest, "--output", "json"],
    )
    review = captured_results[-1]
    assert review_json == operation_result_mapping(review)
    assert review.diagnostics == preview.diagnostics
    assert review.data.review is not None
    assert review.data.review.plan == preview.data.plan
    assert review.data.review.digest == digest

    apply_json = _json_result(
        runner,
        [
            "plan",
            "apply",
            "--plan",
            digest,
            "--acknowledge-resource-scope",
            SCOPE.canonical_name,
            "--output",
            "json",
        ],
    )
    applied = captured_results[-1]
    assert apply_json == operation_result_mapping(applied)
    assert applied.diagnostics == review.diagnostics
    assert tuple(child.disposition for child in applied.data.children) == (
        ApplyChildDisposition.ACCEPTED,
    ) * len(preview.data.plan.children)
    assert tuple(write.child_id for write in harness.writer.requests) == tuple(
        child.child_id for child in preview.data.plan.children
    )

    audit_records = harness.audit.query(AuditQuery(limit=100)).records
    returned_audit_ids = {
        preview.data.audit_record_id,
        *applied.data.audit_record_ids,
    }
    assert returned_audit_ids <= {record.record_id for record in audit_records}
    preview_record = next(
        record
        for record in audit_records
        if record.record_id == preview.data.audit_record_id
    )
    preview_facts = {
        (fact.name, fact.value.value) for fact in preview_record.draft.facts
    }
    assert (AuditFactName.PLAN_DIGEST, digest) in preview_facts
    assert sum(
        name is AuditFactName.PLAN_CHILD for name, _value in preview_facts
    ) == len(preview.data.plan.children)
    apply_disposition_facts = {
        fact.value.value
        for record in audit_records
        for fact in record.draft.facts
        if fact.name is AuditFactName.DISPOSITION
    }
    assert "accepted" in apply_disposition_facts

    intent_id = applied.data.intent_id
    assert intent_id is not None
    typed_watch, resume = asyncio.run(
        _textual_watch(
            harness.facade,
            WatchRequest(
                intent_id=intent_id,
                condition=WatchCondition.GRANTED,
                resume=None,
                authentication_key=KEY,
                installation_id=INSTALLATION_ID,
                deadline=harness.clock.monotonic() + 10,
                cancellation=CancellationToken(),
            ),
        )
    )
    assert typed_watch.data.resume == resume
    assert typed_watch.diagnostics == ()

    captured_events: list[WatchStreamEvent] = []
    original_emit_event = cli_module.emit_watch_event

    def capture_event(event: WatchStreamEvent, presentation: object) -> None:
        captured_events.append(event)
        original_emit_event(event, presentation)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "emit_watch_event", capture_event)
    watched = runner.invoke(
        cli_module.main,
        [
            "request",
            "watch",
            "--resume",
            resume,
            "--deadline",
            "2026-07-25T00:00:00Z",
            "--output",
            "jsonl",
        ],
    )
    assert watched.exit_code == 0, watched.output
    jsonl_events = [json.loads(line) for line in watched.stdout.splitlines()]
    assert len(jsonl_events) == len(captured_events)
    for mapping, event in zip(jsonl_events, captured_events, strict=True):
        assert mapping["resume"] == event.resume
        assert mapping["diagnostics"] == []
        assert mapping["aggregate"]["disposition"] == event.aggregate.disposition.value
        assert [
            child["child"]["disposition"] for child in mapping["aggregate"]["children"]
        ] == [child.child.disposition.value for child in event.aggregate.children]
        if event.result is not None:
            assert mapping["result"] == operation_result_mapping(event.result)
            assert event.result.data.resume == event.resume
