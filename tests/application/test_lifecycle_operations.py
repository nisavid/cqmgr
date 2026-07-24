"""Shared surface-neutral lifecycle facade contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cqmgr.application.operations.lifecycle import LifecycleOperations

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Plans:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def compose(self, request: object) -> object:
        self.calls.append(("compose", request))
        return ("composition", request)

    def preview(self, request: object) -> object:
        self.calls.append(("preview", request))
        return ("preview", request)

    def review(self, request: object) -> object:
        self.calls.append(("review", request))
        return ("review", request)


class _Apply:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def apply(self, request: object) -> object:
        self.calls.append(request)
        return ("apply", request)


class _Watch:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def watch(self, request: object) -> AsyncIterator[tuple[str, object]]:
        self.calls.append(request)
        yield ("watch", request)


def test_every_surface_shares_existing_typed_lifecycle_operations() -> None:
    """Every adapter delegates the same typed inputs to the same operations."""

    async def run() -> None:
        assert await operations.apply(apply_request) == (  # type: ignore[arg-type]
            "apply",
            apply_request,
        )
        assert [item async for item in operations.watch(watch_request)] == [  # type: ignore[arg-type]
            ("watch", watch_request)
        ]

    plans = _Plans()
    apply = _Apply()
    watch = _Watch()
    operations = LifecycleOperations(plans, apply, watch)  # type: ignore[arg-type]
    compose_request = object()
    preview_request = object()
    review_request = object()
    apply_request = object()
    watch_request = object()

    assert operations.compose(compose_request) == (  # type: ignore[arg-type]
        "composition",
        compose_request,
    )
    assert operations.preview(preview_request) == (  # type: ignore[arg-type]
        "preview",
        preview_request,
    )
    assert operations.review(review_request) == (  # type: ignore[arg-type]
        "review",
        review_request,
    )
    asyncio.run(run())
    assert plans.calls == [
        ("compose", compose_request),
        ("preview", preview_request),
        ("review", review_request),
    ]
    assert apply.calls == [apply_request]
    assert watch.calls == [watch_request]
