"""Public Click lifecycle routes over the shared typed facade."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import ANY

import pytest
from click.testing import CliRunner

import cqmgr.cli as cli_module
from cqmgr.adapters.cli.lifecycle import (
    LifecycleCliRuntime,
    PlanReferenceInput,
    RequestCompositionInput,
    WatchCliInput,
)
from cqmgr.application.operations.lifecycle import LifecycleOperations
from cqmgr.application.operations.watch import WatchStartError
from cqmgr.domain.accelerator_overlay import ComputeInstanceRequirement
from cqmgr.domain.plans import TargetStrategy
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.status import WatchCondition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from _pytest.monkeypatch import MonkeyPatch

NOW = datetime(2026, 7, 24, 12, tzinfo=UTC)
INSTANCE_COUNT = 2
USAGE_EXIT = 2


def _result(
    operation: str,
    boundary: str,
    data: object,
) -> OperationResult[object]:
    return OperationResult(
        operation=OperationName(operation),
        resource_scope=None,
        boundary=OperationBoundary(StableSymbol(boundary), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=data,
    )


class _Facade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def compose(self, request: object) -> object:
        self.calls.append(("compose", request))
        return request

    def preview(self, request: object) -> OperationResult[object]:
        self.calls.append(("preview", request))
        return _result(
            "request.preview",
            "plan-previewed",
            {"plan_digest": "sha256:" + ("a" * 64)},
        )

    def review(self, request: object) -> OperationResult[object]:
        self.calls.append(("review", request))
        return _result("plan.review", "plan-reviewed", {"authenticated": True})

    async def apply(self, request: object) -> OperationResult[object]:
        self.calls.append(("apply", request))
        return _result(
            "plan.apply",
            "plan-applied",
            {"children": ["accepted", "unattempted"]},
        )

    async def watch(self, request: object) -> AsyncIterator[object]:
        self.calls.append(("watch", request))
        yield ("watch-event", request)


class _Factory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def compose(self, value: RequestCompositionInput) -> object:
        self.calls.append(("compose", value))
        return ("compose-request", value)

    def preview(self, value: RequestCompositionInput) -> object:
        self.calls.append(("preview", value))
        return ("preview-request", value)

    def review(self, value: PlanReferenceInput) -> object:
        self.calls.append(("review", value))
        return ("review-request", value)

    def apply(
        self,
        value: PlanReferenceInput,
        acknowledgement: str,
        *,
        quota_contact: object | None = None,
    ) -> object:
        self.calls.append(("apply", (value, acknowledgement, quota_contact)))
        return ("apply-request", value, acknowledgement)

    def watch(self, value: object) -> object:
        self.calls.append(("watch", value))
        return ("watch-request", value)


def _runtime(monkeypatch: MonkeyPatch) -> tuple[_Facade, _Factory]:
    facade = _Facade()
    factory = _Factory()
    shared = LifecycleOperations(
        facade,  # type: ignore[arg-type]
        facade,  # type: ignore[arg-type]
        facade,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        cli_module,
        "build_lifecycle_cli_runtime",
        lambda: LifecycleCliRuntime(shared, factory),  # type: ignore[arg-type]
    )
    return facade, factory


class _Preparation:
    def __init__(self, *, preview: object | None) -> None:
        self.preview = preview
        self.intents: list[tuple[object, float]] = []

    async def prepare(
        self,
        intent: object,
        *,
        deadline: float,
        require_preview: bool,
    ) -> object:
        assert require_preview
        self.intents.append((intent, deadline))
        return SimpleNamespace(
            composition=("async-compose-request", intent),
            preview=self.preview,
        )


def _async_runtime(
    monkeypatch: MonkeyPatch,
    *,
    preview: object | None,
) -> tuple[_Facade, _Factory, _Preparation]:
    facade, factory = _runtime(monkeypatch)
    preparation = _Preparation(preview=preview)
    shared = LifecycleOperations(
        facade,  # type: ignore[arg-type]
        facade,  # type: ignore[arg-type]
        facade,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        cli_module,
        "build_lifecycle_cli_runtime",
        lambda: LifecycleCliRuntime(
            shared,
            factory,  # type: ignore[arg-type]
            preparation,  # type: ignore[arg-type]
        ),
    )
    return facade, factory, preparation


def test_lifecycle_groups_and_aliases_publish_only_canonical_paths() -> None:
    """Request and Plan leaves expose exact aliases without publishing aliases."""
    runner = CliRunner()

    request_help = runner.invoke(cli_module.main, ["req", "p", "--help"])
    plan_help = runner.invoke(cli_module.main, ["pl", "a", "--help"])

    assert request_help.exit_code == 0
    assert request_help.stdout.startswith("Usage: cqmgr request preview [OPTIONS]\n")
    assert plan_help.exit_code == 0
    assert plan_help.stdout.startswith("Usage: cqmgr plan apply [OPTIONS]\n")
    assert "--expert" in request_help.output
    assert "--expert" not in plan_help.output


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("human", "Watch: watch-interrupted (exit 130)\n"),
        ("jsonl", "Watch: watch-interrupted (exit 130)\n"),
    ],
)
def test_watch_start_failure_emits_stable_error_and_exit(
    monkeypatch: MonkeyPatch,
    output: str,
    expected: str,
) -> None:
    """A pre-stream Watch failure never escapes as a raw application exception."""
    facade, _ = _runtime(monkeypatch)

    async def fail_watch(request: object) -> AsyncIterator[object]:
        del request
        failure_code = "watch-interrupted"
        raise WatchStartError(failure_code, ExitClass.INTERRUPTED)
        yield  # pragma: no cover - retains the async-iterator contract

    monkeypatch.setattr(facade, "watch", fail_watch)
    result = CliRunner().invoke(
        cli_module.main,
        [
            "request",
            "watch",
            "--intent-id",
            "intent-1",
            "--condition",
            "granted",
            "--deadline",
            "2026-07-25T00:00:00Z",
            "--output",
            output,
        ],
    )

    assert result.exit_code == int(ExitClass.INTERRUPTED)
    assert result.stdout == ""
    assert result.stderr == expected
    assert not isinstance(result.exception, WatchStartError)


def test_exact_preview_dispatches_through_shared_facade_without_contact_output(
    monkeypatch: MonkeyPatch,
) -> None:
    """Protected contact reaches only the request factory; stdout stays result-only."""
    facade, factory = _runtime(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "request",
            "preview",
            "--resource-scope",
            "projects/123",
            "--service",
            "compute.googleapis.com",
            "--quota-id",
            "GPU-DIRECT",
            "--location",
            "us-central1",
            "--dimension",
            "region=us-central1",
            "--target",
            "8",
            "--expert",
            "--quota-contact-stdin",
            "--plan-out",
            "request.plan",
            "--output",
            "json",
        ],
        input="operator@example.com\n",
    )

    assert result.exit_code == 0, result.output
    assert "operator@example.com" not in result.output
    name, value = factory.calls[0]
    assert name == "preview"
    assert value.quota_contact is not None  # type: ignore[union-attr]
    assert value.expert is True  # type: ignore[union-attr]
    assert value.quota_contact.reveal() == b"operator@example.com"  # type: ignore[union-attr]
    assert value.plan_out == Path("request.plan")  # type: ignore[union-attr]
    assert facade.calls[0][0] == "preview"


def test_preview_uses_async_preparation_without_sync_factory(
    monkeypatch: MonkeyPatch,
) -> None:
    """The production seam resolves fresh evidence once before shared Preview."""
    facade, factory, preparation = _async_runtime(
        monkeypatch,
        preview=("async-preview-request",),
    )

    result = CliRunner().invoke(
        cli_module.main,
        [
            "request",
            "preview",
            "--resource-scope",
            "projects/123",
            "--service",
            "compute.googleapis.com",
            "--quota-id",
            "GPU-DIRECT",
            "--location",
            "us-central1",
            "--dimension",
            "region=us-central1",
            "--target",
            "8",
            "--quota-contact-stdin",
            "--output",
            "json",
        ],
        input="operator@example.com\n",
    )

    assert result.exit_code == 0, result.output
    assert factory.calls == []
    assert facade.calls == [("preview", ("async-preview-request",))]
    assert len(preparation.intents) == 1
    intent, deadline = preparation.intents[0]
    assert intent.expert is False  # type: ignore[union-attr]
    assert deadline > 0


def test_preview_async_preparation_fails_closed_without_contact(
    monkeypatch: MonkeyPatch,
) -> None:
    """Missing protected contact resolution never reaches Preview operations."""
    facade, factory, _ = _async_runtime(monkeypatch, preview=None)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "request",
            "preview",
            "--resource-scope",
            "projects/123",
            "--service",
            "compute.googleapis.com",
            "--quota-id",
            "GPU-DIRECT",
            "--location",
            "us-central1",
            "--dimension",
            "region=us-central1",
            "--target",
            "8",
        ],
    )

    assert result.exit_code == 1
    assert "resolvable protected quota contact" in result.output
    assert factory.calls == []
    assert facade.calls == []


def test_plan_review_and_apply_dispatch_exact_reference_and_acknowledgement(
    monkeypatch: MonkeyPatch,
) -> None:
    """Plan leaves preserve one reference and the operator-entered exact scope."""
    facade, factory = _runtime(monkeypatch)
    runner = CliRunner()
    digest = "sha256:" + ("a" * 64)

    review = runner.invoke(
        cli_module.main,
        ["plan", "review", "--plan", digest, "--output", "json"],
    )
    apply = runner.invoke(
        cli_module.main,
        [
            "plan",
            "apply",
            "--plan-file",
            "request.plan",
            "--acknowledge-resource-scope",
            "projects/123",
            "--quota-contact-stdin",
            "--output",
            "json",
        ],
        input="operator@example.com\n",
    )

    assert review.exit_code == apply.exit_code == 0
    assert factory.calls[0] == (
        "review",
        PlanReferenceInput(digest=digest, path=None),
    )
    assert factory.calls[1] == (
        "apply",
        (
            PlanReferenceInput(digest=None, path=Path("request.plan")),
            "projects/123",
            ANY,
        ),
    )
    assert [name for name, _ in facade.calls] == ["review", "apply"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["plan", "review"],
        [
            "plan",
            "review",
            "--plan",
            "digest",
            "--plan-file",
            "request.plan",
        ],
        [
            "plan",
            "apply",
            "--acknowledge-resource-scope",
            "projects/123",
        ],
        [
            "plan",
            "apply",
            "--plan",
            "digest",
            "--plan-file",
            "request.plan",
            "--acknowledge-resource-scope",
            "projects/123",
        ],
    ],
)
def test_plan_selector_usage_fails_before_runtime_construction(
    arguments: list[str],
) -> None:
    """Missing or conflicting Plan references remain usage errors while gated."""
    result = CliRunner().invoke(cli_module.main, arguments)

    assert result.exit_code == USAGE_EXIT
    assert "exactly one digest or path" in result.output
    assert "lifecycle operations are unavailable" not in result.output


def test_compute_preview_preserves_workload_shape_and_default_strategy(
    monkeypatch: MonkeyPatch,
) -> None:
    """Workload Preview reaches the factory as one typed requirement."""
    facade, factory = _runtime(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "req",
            "p",
            "--resource-scope",
            "projects/123",
            "--machine-type",
            "a3-highgpu-8g",
            "--instance-count",
            "2",
            "--attached-accelerator-type",
            "nvidia-h100-80gb",
            "--attached-accelerator-count",
            "8",
            "--provisioning-model",
            "spot",
            "--candidate",
            "us-central1-a",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    value = factory.calls[0][1]
    assert isinstance(value, RequestCompositionInput)
    assert isinstance(value.workload, ComputeInstanceRequirement)
    assert value.workload.instance_count == INSTANCE_COUNT
    assert value.target_strategy is TargetStrategy.MINIMUM
    assert value.targets == ()
    assert facade.calls[0][0] == "preview"


def test_watch_dispatches_absolute_deadline_and_non_tty_jsonl(
    monkeypatch: MonkeyPatch,
) -> None:
    """Watch converts public UTC input once and streams through the shared facade."""
    facade, factory = _runtime(monkeypatch)
    presented: list[tuple[object, object]] = []
    monkeypatch.setattr(
        cli_module,
        "emit_watch_event",
        lambda event, presentation: presented.append((event, presentation)),
    )

    result = CliRunner().invoke(
        cli_module.main,
        [
            "request",
            "watch",
            "--intent-id",
            "sha256:" + ("b" * 64),
            "--condition",
            "granted",
            "--deadline",
            "2026-07-25T00:00:00Z",
        ],
    )

    assert result.exit_code == 0, result.output
    name, value = factory.calls[0]
    assert name == "watch"
    assert value == WatchCliInput(
        intent_id="sha256:" + ("b" * 64),
        condition=WatchCondition.GRANTED,
        resume=None,
        deadline=datetime(2026, 7, 25, tzinfo=UTC),
    )
    assert facade.calls[0][0] == "watch"
    assert presented[0][1].output == "jsonl"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--intent-id", "intent", "--resume", "token", "--condition", "granted"],
        ["--intent-id", "intent"],
        ["--resume", "token", "--condition", "fulfilled"],
        ["--resume", "token", "--deadline", "not-a-timestamp"],
        ["--resume", "token", "--deadline", "2026-07-25 00:00:00+00:00"],
    ],
)
def test_watch_rejects_incomplete_or_cross_wired_selectors(
    monkeypatch: MonkeyPatch,
    arguments: list[str],
) -> None:
    """Invalid Watch selectors stop before runtime construction."""
    _runtime(monkeypatch)
    result = CliRunner().invoke(
        cli_module.main,
        ["request", "watch", *arguments],
    )

    assert result.exit_code == USAGE_EXIT
