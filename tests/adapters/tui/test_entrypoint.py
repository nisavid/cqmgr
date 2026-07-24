"""Late Textual entry-point composition contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import cqmgr.tui as tui_module
from cqmgr import bootstrap
from cqmgr.adapters.tui.app import CloudQuotaManagerApp

if TYPE_CHECKING:
    import pytest


class _RecordingRuntime:
    def __init__(
        self,
        *,
        read_only: object,
        operations: object,
        preparation: object,
        requests: object,
    ) -> None:
        self.read_only = read_only
        self.operations = operations
        self.preparation = preparation
        self.requests = requests
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


def _unexpected_read_only() -> None:
    message = "default TUI must reuse the lifecycle read graph"
    raise AssertionError(message)


def test_build_app_injects_precomposed_production_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default TUI receives one production operation and preparation graph."""
    read_only = object()
    audit = object()
    lifecycle = object()
    preparation = object()
    requests = object()
    runtime = _RecordingRuntime(
        read_only=read_only,
        operations=lifecycle,
        preparation=preparation,
        requests=requests,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_read_only_operations",
        _unexpected_read_only,
    )
    monkeypatch.setattr(bootstrap, "build_audit_operations", lambda: audit)
    monkeypatch.setattr(
        bootstrap,
        "build_lifecycle_runtime",
        lambda: runtime,
    )

    app = tui_module.build_app()

    assert isinstance(app, CloudQuotaManagerApp)
    assert app.read_only is read_only
    assert app.audit is audit
    assert app.lifecycle is lifecycle
    assert app.lifecycle_preparation is preparation
    assert app.lifecycle_requests is requests
    assert not hasattr(app, "apply")
    assert not hasattr(app, "mutation")
    asyncio.run(app.on_unmount())
    asyncio.run(app.on_unmount())
    assert runtime.close_calls == 1


def test_build_app_accepts_one_precomposed_lifecycle_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future explicit trust bootstrap can inject the shared facade unchanged."""
    read_only = object()
    audit = object()
    lifecycle = object()
    monkeypatch.setattr(bootstrap, "build_read_only_operations", lambda: read_only)
    monkeypatch.setattr(bootstrap, "build_audit_operations", lambda: audit)

    app = tui_module.build_app(lifecycle=lifecycle)  # type: ignore[arg-type]

    assert app.read_only is read_only
    assert app.audit is audit
    assert app.lifecycle is lifecycle


def test_run_owns_the_full_screen_app_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The late entry point constructs once and hands control to Textual."""

    class RecordingApp:
        def __init__(self) -> None:
            self.calls = 0

        def run(self) -> None:
            self.calls += 1

    app = RecordingApp()
    monkeypatch.setattr(tui_module, "build_app", lambda: app)

    tui_module.run()

    assert app.calls == 1
