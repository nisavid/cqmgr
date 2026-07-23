"""Lazy shared runtime mechanics for the Google read-only composition root."""

from __future__ import annotations

import asyncio
import inspect
from threading import RLock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from cqmgr.adapters.google.identity import (
        ADCCredentialSnapshot,
        ADCRuntime,
    )


class CachedADCRuntime:
    """Cache ADC snapshots and expose the latest credential to lazy clients."""

    def __init__(
        self,
        delegate: ADCRuntime,
        *,
        default_scopes: Sequence[str],
    ) -> None:
        """Bind one authoritative loader and its default client scopes."""
        self._delegate = delegate
        self._default_scopes = tuple(default_scopes)
        self._snapshots: dict[
            tuple[tuple[str, ...], str | None], ADCCredentialSnapshot
        ] = {}
        self._active: ADCCredentialSnapshot | None = None
        self._lock = RLock()

    def load(
        self,
        *,
        scopes: Sequence[str],
        quota_project_id: str | None,
        timeout_seconds: float = 10.0,
    ) -> ADCCredentialSnapshot:
        """Load each exact ADC context once and make it active for clients."""
        key = (tuple(scopes), quota_project_id)
        with self._lock:
            snapshot = self._snapshots.get(key)
            if snapshot is None:
                snapshot = self._delegate.load(
                    scopes=key[0],
                    quota_project_id=quota_project_id,
                    timeout_seconds=timeout_seconds,
                )
                self._snapshots[key] = snapshot
            self._active = snapshot
            return snapshot

    def credential(self) -> object:
        """Return the active shared credential, loading the default context lazily."""
        with self._lock:
            snapshot = self._active
        if snapshot is None:
            snapshot = self.load(
                scopes=self._default_scopes,
                quota_project_id=None,
            )
        return snapshot.credential

    def refresh(
        self,
        snapshot: ADCCredentialSnapshot,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Refresh through the authoritative Google Auth runtime."""
        self._delegate.refresh(snapshot, timeout_seconds=timeout_seconds)

    def fetch_user_info(
        self,
        snapshot: ADCCredentialSnapshot,
        *,
        timeout_seconds: float = 10.0,
    ) -> Mapping[str, object]:
        """Fetch direct-user proof through the authoritative runtime."""
        return self._delegate.fetch_user_info(
            snapshot,
            timeout_seconds=timeout_seconds,
        )


class LazyClientProxy[ClientT]:
    """Construct one official client only when its first method is requested."""

    def __init__(
        self,
        factory: Callable[[], ClientT],
        *,
        closer: Callable[[ClientT], Awaitable[None] | None] | None = None,
    ) -> None:
        """Retain a side-effect-free client factory."""
        self._factory = factory
        self._closer = closer
        self._client: ClientT | None = None
        self._lock = RLock()

    def _get(self) -> ClientT:
        with self._lock:
            if self._client is None:
                self._client = self._factory()
            return self._client

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Forward the requested public method to the lazily built client."""
        return getattr(self._get(), name)

    async def aclose(self) -> None:
        """Close a constructed owned client without defeating lazy startup."""
        with self._lock:
            client = self._client
            self._client = None
        if client is None:
            return
        if self._closer is not None:
            result = self._closer(client)
        else:
            close = getattr(client, "close", None)
            if close is None:
                return
            result = close()
        if inspect.isawaitable(result):
            _ = await result


class OwnedClientPool:
    """Best-effort shutdown for every lazy client owned by one invocation."""

    def __init__(self, *clients: LazyClientProxy[Any]) -> None:
        """Retain the complete set of invocation-scoped client proxies."""
        self._clients = clients

    async def aclose(self) -> None:
        """Close all constructed clients even if one client rejects shutdown."""
        await asyncio.gather(
            *(client.aclose() for client in self._clients),
            return_exceptions=True,
        )
