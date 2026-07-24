"""Late-imported Textual entry-point seam."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cqmgr.adapters.tui.app import CloudQuotaManagerApp
    from cqmgr.application.operations.lifecycle import LifecycleOperations
    from cqmgr.application.operations.lifecycle_requests import (
        LifecycleRequestOperations,
    )


def build_app(
    *,
    lifecycle: LifecycleOperations | None = None,
    lifecycle_preparation: LifecycleRequestOperations | None = None,
) -> CloudQuotaManagerApp:
    """Compose the TUI with the production or explicitly injected lifecycle."""
    from cqmgr.adapters.tui.app import CloudQuotaManagerApp  # noqa: PLC0415
    from cqmgr.bootstrap import (  # noqa: PLC0415
        build_audit_operations,
        build_lifecycle_runtime,
        build_read_only_operations,
    )

    if lifecycle is None and lifecycle_preparation is None:
        runtime = build_lifecycle_runtime()
        lifecycle = runtime.operations
        lifecycle_preparation = runtime.preparation
    return CloudQuotaManagerApp(
        build_read_only_operations(),
        build_audit_operations(),
        lifecycle=lifecycle,
        lifecycle_preparation=lifecycle_preparation,
    )


def run() -> None:
    """Build and run the app; the CLI validates interactivity first."""
    build_app().run()
