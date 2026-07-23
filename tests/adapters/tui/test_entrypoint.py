"""Late Textual entry-point composition contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cqmgr.tui as tui_module
from cqmgr import bootstrap
from cqmgr.adapters.tui.app import CloudQuotaManagerApp

if TYPE_CHECKING:
    import pytest


def test_build_app_composes_only_read_only_and_local_audit_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TUI composition has no provider-mutation operation or port."""
    read_only = object()
    audit = object()
    monkeypatch.setattr(bootstrap, "build_read_only_operations", lambda: read_only)
    monkeypatch.setattr(bootstrap, "build_audit_operations", lambda: audit)

    app = tui_module.build_app()

    assert isinstance(app, CloudQuotaManagerApp)
    assert app.read_only is read_only
    assert app.audit is audit
    assert not hasattr(app, "apply")
    assert not hasattr(app, "mutation")


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
