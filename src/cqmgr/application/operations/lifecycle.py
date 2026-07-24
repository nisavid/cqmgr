"""Shared surface-neutral mutation and lifecycle operation facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cqmgr.application.operations.apply import (
        ApplyData,
        ApplyPlanOperations,
        ApplyProgressObserver,
        ApplyRequest,
    )
    from cqmgr.application.operations.plans import (
        ComposeRequest,
        Composition,
        PlanReviewData,
        PlanReviewRequest,
        PreviewData,
        PreviewRequest,
        RequestPlanOperations,
    )
    from cqmgr.application.operations.watch import (
        WatchOperations,
        WatchRequest,
    )
    from cqmgr.domain.results import OperationResult
    from cqmgr.domain.watch import WatchStreamEvent


class LifecycleOperations:
    """Expose one typed Plan, Apply, and Watch boundary to every surface."""

    def __init__(
        self,
        plans: RequestPlanOperations,
        apply: ApplyPlanOperations,
        watch: WatchOperations,
    ) -> None:
        """Bind existing safety-owning operations without exposing their ports."""
        self._plans = plans
        self._apply = apply
        self._watch = watch

    def compose(self, request: ComposeRequest) -> Composition:
        """Compose one exact single or ordered bundle request."""
        return self._plans.compose(request)

    def preview(self, request: PreviewRequest) -> OperationResult[PreviewData]:
        """Issue or verify one plan through the shared Preview operation."""
        return self._plans.preview(request)

    def review(self, request: PlanReviewRequest) -> OperationResult[PlanReviewData]:
        """Review one local or portable plan without applying it."""
        return self._plans.review(request)

    async def apply(
        self,
        request: ApplyRequest,
        *,
        on_progress: ApplyProgressObserver | None = None,
    ) -> OperationResult[ApplyData]:
        """Consume one plan through the sole typed Apply operation."""
        if on_progress is None:
            return await self._apply.apply(request)
        return await self._apply.apply(request, on_progress=on_progress)

    def watch(self, request: WatchRequest) -> AsyncIterator[WatchStreamEvent]:
        """Observe one durable Apply intent through the typed Watch stream."""
        return self._watch.watch(request)
