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

    lifecycle_requests = None
    lifecycle_shutdown = None
    if lifecycle is None and lifecycle_preparation is None:
        runtime = build_lifecycle_runtime()
        lifecycle = runtime.operations
        lifecycle_preparation = runtime.preparation
        lifecycle_requests = runtime.requests
        lifecycle_shutdown = runtime.aclose
        read_only = runtime.read_only
        if read_only is None:
            message = "lifecycle runtime must expose its read-only operation graph"
            raise RuntimeError(message)
    else:
        read_only = build_read_only_operations()
    return CloudQuotaManagerApp(
        read_only,
        build_audit_operations(),
        lifecycle=lifecycle,
        lifecycle_preparation=lifecycle_preparation,
        lifecycle_requests=lifecycle_requests,
        lifecycle_shutdown=lifecycle_shutdown,
    )


def run() -> None:
    """Build and run the app; the CLI validates interactivity first."""
    build_app().run()
