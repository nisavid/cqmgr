"""Late-imported Textual entry-point seam."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cqmgr.adapters.tui.app import CloudQuotaManagerApp


def build_app() -> CloudQuotaManagerApp:
    """Compose the TUI from read-only provider and local audit operations only."""
    from cqmgr.adapters.tui.app import CloudQuotaManagerApp  # noqa: PLC0415
    from cqmgr.bootstrap import (  # noqa: PLC0415
        build_audit_operations,
        build_read_only_operations,
    )

    return CloudQuotaManagerApp(
        build_read_only_operations(),
        build_audit_operations(),
    )


def run() -> None:
    """Build and run the app; the CLI validates interactivity first."""
    build_app().run()
