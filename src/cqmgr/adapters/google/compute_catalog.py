"""Read-only Compute machine-type catalog adapter with scoped coverage."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from threading import Lock, Thread
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from google.cloud import compute_v1

from cqmgr.adapters.google.read_policy import (
    GoogleReadPolicy,
    page_cap_diagnostic,
    schema_diagnostic,
)
from cqmgr.application.ports.catalog_reads import (
    CatalogRead,
    ComputeAcceleratorTypeReadRequest,
    ComputeMachineTypeReadRequest,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    CatalogEvidenceSource,
    CatalogLifecycle,
    CatalogLocationCoverage,
    ComputeAcceleratorType,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
    is_canonical_zone,
)
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.quotas import ProviderRead, ProviderReadCoverage
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.schemas import ProviderSymbol

if TYPE_CHECKING:
    from collections.abc import Callable


class _BoundedDaemonWorkers[ResultT]:
    """Run sync provider calls without extending CLI shutdown."""

    def __init__(
        self,
        close_transport: Callable[[], None],
        *,
        maximum_workers: int,
        thread_name: str,
    ) -> None:
        """Bind a concurrency ceiling and transport owned by the workers."""
        self._close_transport = close_transport
        self._slots = asyncio.Semaphore(maximum_workers)
        self._thread_name = thread_name
        self._lock = Lock()
        self._active_workers = 0
        self._close_requested = False
        self._transport_closed = False

    async def run(self, operation: Callable[[], ResultT]) -> ResultT:
        """Await one daemon-backed provider call without making it cancellable."""
        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        result: asyncio.Future[ResultT] = loop.create_future()
        result.add_done_callback(self._observe_result)
        with self._lock:
            if self._close_requested:
                self._slots.release()
                msg = "Compute catalog client is closed"
                raise RuntimeError(msg)
            self._active_workers += 1
        worker = Thread(
            target=self._execute,
            args=(operation, loop, result),
            name=self._thread_name,
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            with self._lock:
                self._active_workers -= 1
            self._slots.release()
            raise
        return await asyncio.shield(result)

    async def close(self) -> None:
        """Return promptly and close the transport after its last call stops."""
        close_now = False
        with self._lock:
            if self._close_requested:
                return
            self._close_requested = True
            if self._active_workers == 0:
                self._transport_closed = True
                close_now = True
        if close_now:
            self._close_transport()

    def _execute(
        self,
        operation: Callable[[], ResultT],
        loop: asyncio.AbstractEventLoop,
        result: asyncio.Future[ResultT],
    ) -> None:
        """Run one sync call and publish its outcome while the loop remains live."""
        try:
            value = operation()
        except Exception as error:  # noqa: BLE001 - preserve provider SDK failures
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._deliver_error, result, error)
        else:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._deliver_result, result, value)
        finally:
            self._finish_worker()

    def _deliver_error(
        self,
        result: asyncio.Future[ResultT],
        error: BaseException,
    ) -> None:
        """Release capacity and expose one provider failure."""
        self._slots.release()
        if not result.done():
            result.set_exception(error)

    def _deliver_result(
        self,
        result: asyncio.Future[ResultT],
        value: ResultT,
    ) -> None:
        """Release capacity and expose one provider page."""
        self._slots.release()
        if not result.done():
            result.set_result(value)

    @staticmethod
    def _observe_result(result: asyncio.Future[ResultT]) -> None:
        """Consume a late provider failure after its caller was cancelled."""
        if not result.cancelled():
            _ = result.exception()

    def _finish_worker(self) -> None:
        """Close the transport from the final worker when shutdown was requested."""
        close_now = False
        with self._lock:
            self._active_workers -= 1
            if (
                self._close_requested
                and self._active_workers == 0
                and not self._transport_closed
            ):
                self._transport_closed = True
                close_now = True
        if close_now:
            with suppress(Exception):
                self._close_transport()


@dataclass(frozen=True, slots=True)
class ComputeAcceleratorTypesScope:
    """Adapter-internal materialized Compute accelerator-type scope."""

    scope: str
    accelerator_types: tuple[compute_v1.AcceleratorType, ...]
    warning_code: str | None = None


@dataclass(frozen=True, slots=True)
class ComputeAcceleratorTypesPage:
    """Adapter-internal materialized aggregated accelerator-type page."""

    scopes: tuple[ComputeAcceleratorTypesScope, ...]
    next_page_token: str
    unreachable_scopes: tuple[str, ...] = ()
    warning_code: str | None = None


@runtime_checkable
class ComputeAcceleratorTypesPageClient(Protocol):
    """Materialize one official Compute accelerator-types page asynchronously."""

    async def accelerator_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        """Return one materialized aggregated-list page."""
        raise NotImplementedError


@runtime_checkable
class ComputeAcceleratorTypesZonalPageClient(Protocol):
    """Materialize official Compute accelerator pages for exact zones."""

    async def accelerator_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        """Return one materialized zonal-list page."""
        raise NotImplementedError


class OfficialComputeAcceleratorTypesPageClient:
    """Fence the sync-only official Compute accelerator client."""

    def __init__(
        self,
        client: compute_v1.AcceleratorTypesClient,
        *,
        maximum_workers: int = 4,
    ) -> None:
        """Bind one client and cap concurrent daemon-worker dispatches."""
        _require_positive(maximum_workers, "Compute catalog maximum_workers")
        self._client = client
        self._workers = _BoundedDaemonWorkers[ComputeAcceleratorTypesPage](
            self._close_transport,
            maximum_workers=maximum_workers,
            thread_name="cqmgr-compute-accelerator-types",
        )

    async def accelerator_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        """Run exactly one sync generated-client page in a bounded worker."""
        return await self._workers.run(
            partial(
                self._accelerator_types,
                project=project,
                max_results=max_results,
                page_token=page_token,
                return_partial_success=return_partial_success,
                timeout_seconds=timeout_seconds,
            )
        )

    async def accelerator_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        """Run exactly one sync generated-client zonal page in a bounded worker."""
        return await self._workers.run(
            partial(
                self._accelerator_types_for_zone,
                project=project,
                zone=zone,
                max_results=max_results,
                page_token=page_token,
                timeout_seconds=timeout_seconds,
            )
        )

    async def close(self) -> None:
        """Release promptly while active sync calls retain their transport."""
        await self._workers.close()

    def _close_transport(self) -> None:
        """Close the generated transport once no sync call owns it."""
        self._client.transport.close()

    def _accelerator_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        request = compute_v1.AggregatedListAcceleratorTypesRequest(
            project=project,
            max_results=max_results,
            page_token=page_token,
            return_partial_success=return_partial_success,
        )
        pager = self._client.aggregated_list(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = next(pager.pages)
        scopes = tuple(
            ComputeAcceleratorTypesScope(
                scope=scope,
                accelerator_types=tuple(scoped.accelerator_types),
                warning_code=_warning_code(scoped.warning),
            )
            for scope, scoped in sorted(response.items.items())
        )
        return ComputeAcceleratorTypesPage(
            scopes=scopes,
            next_page_token=response.next_page_token,
            unreachable_scopes=tuple(response.unreachables),
            warning_code=_warning_code(response.warning),
        )

    def _accelerator_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        request = compute_v1.ListAcceleratorTypesRequest(
            project=project,
            zone=zone,
            max_results=max_results,
            page_token=page_token,
        )
        pager = self._client.list(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = next(pager.pages)
        return ComputeAcceleratorTypesPage(
            scopes=(
                ComputeAcceleratorTypesScope(
                    scope=f"zones/{zone}",
                    accelerator_types=tuple(response.items),
                    warning_code=_warning_code(response.warning),
                ),
            ),
            next_page_token=response.next_page_token,
        )


@dataclass(frozen=True, slots=True)
class ComputeMachineTypesScope:
    """Adapter-internal materialized Compute scope."""

    scope: str
    machine_types: tuple[compute_v1.MachineType, ...]
    warning_code: str | None = None


@dataclass(frozen=True, slots=True)
class ComputeMachineTypesPage:
    """Adapter-internal materialized aggregated machine-type page."""

    scopes: tuple[ComputeMachineTypesScope, ...]
    next_page_token: str
    unreachable_scopes: tuple[str, ...] = ()
    warning_code: str | None = None


@runtime_checkable
class ComputeMachineTypesPageClient(Protocol):
    """Materialize one official Compute aggregated-list page asynchronously."""

    async def machine_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        """Return one materialized aggregated-list page."""
        ...


@runtime_checkable
class ComputeMachineTypesZonalPageClient(Protocol):
    """Materialize official Compute machine pages for exact zones."""

    async def machine_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        """Return one materialized zonal-list page."""
        ...


class OfficialComputeMachineTypesPageClient:
    """Fence the sync-only official Compute client behind an async worker."""

    def __init__(
        self,
        client: compute_v1.MachineTypesClient,
        *,
        maximum_workers: int = 4,
    ) -> None:
        """Bind one client and cap concurrent daemon-worker dispatches."""
        _require_positive(maximum_workers, "Compute catalog maximum_workers")
        self._client = client
        self._workers = _BoundedDaemonWorkers[ComputeMachineTypesPage](
            self._close_transport,
            maximum_workers=maximum_workers,
            thread_name="cqmgr-compute-machine-types",
        )

    async def machine_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        """Run exactly one sync generated-client page in a bounded worker."""
        return await self._workers.run(
            partial(
                self._machine_types,
                project=project,
                max_results=max_results,
                page_token=page_token,
                return_partial_success=return_partial_success,
                timeout_seconds=timeout_seconds,
            )
        )

    async def machine_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        """Run exactly one sync generated-client zonal page in a bounded worker."""
        return await self._workers.run(
            partial(
                self._machine_types_for_zone,
                project=project,
                zone=zone,
                max_results=max_results,
                page_token=page_token,
                timeout_seconds=timeout_seconds,
            )
        )

    async def close(self) -> None:
        """Release promptly while active sync calls retain their transport."""
        await self._workers.close()

    def _close_transport(self) -> None:
        """Close the generated transport once no sync call owns it."""
        self._client.transport.close()

    def _machine_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        request = compute_v1.AggregatedListMachineTypesRequest(
            project=project,
            max_results=max_results,
            page_token=page_token,
            return_partial_success=return_partial_success,
        )
        pager = self._client.aggregated_list(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = next(pager.pages)
        scopes = tuple(
            ComputeMachineTypesScope(
                scope=scope,
                machine_types=tuple(scoped.machine_types),
                warning_code=_warning_code(scoped.warning),
            )
            for scope, scoped in sorted(response.items.items())
        )
        return ComputeMachineTypesPage(
            scopes=scopes,
            next_page_token=response.next_page_token,
            unreachable_scopes=tuple(response.unreachables),
            warning_code=_warning_code(response.warning),
        )

    def _machine_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        request = compute_v1.ListMachineTypesRequest(
            project=project,
            zone=zone,
            max_results=max_results,
            page_token=page_token,
        )
        pager = self._client.list(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = next(pager.pages)
        return ComputeMachineTypesPage(
            scopes=(
                ComputeMachineTypesScope(
                    scope=f"zones/{zone}",
                    machine_types=tuple(response.items),
                    warning_code=_warning_code(response.warning),
                ),
            ),
            next_page_token=response.next_page_token,
        )


def _warning_code(warning: object) -> str | None:
    code = getattr(warning, "code", None)
    if not code:
        return None
    name = getattr(code, "name", None)
    return name if isinstance(name, str) and name else str(code)


class GoogleComputeAcceleratorTypeReader:
    """Read every project-visible Compute accelerator declaration."""

    def __init__(
        self,
        client: (
            ComputeAcceleratorTypesPageClient | ComputeAcceleratorTypesZonalPageClient
        ),
        policy: GoogleReadPolicy,
        *,
        page_size: int = 100,
        maximum_pages: int = 100,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind bounded pagination, retry policy, and observation clock."""
        _require_positive(page_size, "Compute accelerator catalog page_size")
        _require_positive(maximum_pages, "Compute accelerator catalog maximum_pages")
        self._client = client
        self._policy = policy
        self._page_size = page_size
        self._maximum_pages = maximum_pages
        self._now = now

    async def read(  # noqa: C901, PLR0912, PLR0915 - preserve scoped outcomes
        self,
        request: ComputeAcceleratorTypeReadRequest,
    ) -> CatalogRead[ComputeAcceleratorType]:
        """Return bounded declarations with explicit empty and failed coverage."""
        if not isinstance(request, ComputeAcceleratorTypeReadRequest):
            msg = (
                "Compute accelerator reader requires ComputeAcceleratorTypeReadRequest"
            )
            raise TypeError(msg)
        project = request.context.project.project_id
        zones = request.zones
        aggregated_client: ComputeAcceleratorTypesPageClient | None = None
        zonal_client: ComputeAcceleratorTypesZonalPageClient | None = None
        if zones is None:
            if not isinstance(self._client, ComputeAcceleratorTypesPageClient):
                msg = "Compute accelerator client lacks aggregated list support"
                raise TypeError(msg)
            aggregated_client = self._client
        else:
            if not isinstance(self._client, ComputeAcceleratorTypesZonalPageClient):
                msg = "Compute accelerator client lacks exact-zone list support"
                raise TypeError(msg)
            zonal_client = self._client
        expectation = (
            LocationCoverageExpectation.REQUESTED
            if zones is not None
            else LocationCoverageExpectation.EXPECTED
        )
        zone_index = 0
        token = ""
        attempted = 0
        completed = 0
        cap = False
        values: list[ComputeAcceleratorType] = []
        diagnostics: list[Diagnostic] = []
        location_coverage: list[CatalogLocationCoverage] = []
        while attempted < self._maximum_pages:
            if zones is not None and zone_index >= len(zones):
                break
            zone = zones[zone_index] if zones is not None else None
            attempted += 1
            if zone is None:
                client = cast(
                    "ComputeAcceleratorTypesPageClient",
                    aggregated_client,
                )
                result = await self._policy.call(
                    request.context,
                    provider="compute",
                    phase="compute-accelerator-types-read",
                    identity=f"compute-accelerator-types:{project}:{token}",
                    dispatch=lambda timeout, page_token=token, client=client: (
                        client.accelerator_types(
                            project=project,
                            max_results=self._page_size,
                            page_token=page_token,
                            return_partial_success=True,
                            timeout_seconds=timeout,
                        )
                    ),
                )
            else:
                client = cast(
                    "ComputeAcceleratorTypesZonalPageClient",
                    zonal_client,
                )
                result = await self._policy.call(
                    request.context,
                    provider="compute",
                    phase="compute-accelerator-types-read",
                    identity=(f"compute-accelerator-types:{project}:{zone}:{token}"),
                    dispatch=(
                        lambda timeout, page_token=token, zone=zone, client=client: (
                            client.accelerator_types_for_zone(
                                project=project,
                                zone=zone,
                                max_results=self._page_size,
                                page_token=page_token,
                                timeout_seconds=timeout,
                            )
                        )
                    ),
                )
            if result.diagnostic is not None:
                diagnostics.append(result.diagnostic)
                location_coverage.append(
                    _accelerator_coverage(
                        zone or "global",
                        LocationCoverageState.FAILED,
                        result.diagnostic,
                        expectation=expectation,
                    )
                )
                if zones is None:
                    break
                zone_index += 1
                token = ""
                if result.diagnostic.code.value in {
                    "provider-read-cancelled",
                    "provider-read-deadline-exceeded",
                }:
                    location_coverage.extend(
                        _accelerator_coverage(
                            pending_zone,
                            LocationCoverageState.NOT_SCANNED,
                            result.diagnostic,
                            expectation=expectation,
                        )
                        for pending_zone in zones[zone_index:]
                    )
                    zone_index = len(zones)
                    break
                continue
            page = result.value
            if page is None:
                msg = "successful Compute accelerator page call must contain a page"
                raise RuntimeError(msg)
            completed += 1
            for scoped in page.scopes:
                _consume_accelerator_scope(
                    scoped,
                    project,
                    values,
                    diagnostics,
                    location_coverage,
                    expectation,
                )
            for unreachable in page.unreachable_scopes:
                diagnostic = _accelerator_coverage_diagnostic(
                    "compute-accelerator-catalog-location-unreachable"
                )
                diagnostics.append(diagnostic)
                try:
                    location = _scope_location(unreachable)
                except ValueError:
                    location = "global"
                location_coverage.append(
                    _accelerator_coverage(
                        location,
                        LocationCoverageState.FAILED,
                        diagnostic,
                        expectation=expectation,
                    )
                )
            if page.warning_code is not None:
                if page.warning_code == "NO_RESULTS_ON_PAGE":
                    location_coverage.append(
                        _accelerator_coverage(
                            zone or "global",
                            LocationCoverageState.EMPTY,
                            expectation=expectation,
                        )
                    )
                else:
                    diagnostic = _accelerator_coverage_diagnostic(
                        "compute-accelerator-catalog-page-warning"
                    )
                    diagnostics.append(diagnostic)
                    location_coverage.append(
                        _accelerator_coverage(
                            zone or "global",
                            LocationCoverageState.FAILED,
                            diagnostic,
                            expectation=expectation,
                        )
                    )
            token = page.next_page_token
            if not token:
                if zones is None:
                    break
                zone_index += 1
        cap = bool(token) or (zones is not None and zone_index < len(zones))
        if cap:
            diagnostic = page_cap_diagnostic(
                "compute-accelerator-types-read",
                "compute",
            )
            diagnostics.append(diagnostic)
            unscanned_locations = ("global",) if zones is None else zones[zone_index:]
            location_coverage.extend(
                CatalogLocationCoverage(
                    source=CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                    location=location,
                    expectation=expectation,
                    state=LocationCoverageState.NOT_SCANNED,
                    diagnostics=(diagnostic,),
                )
                for location in unscanned_locations
            )
        read = ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=self._now(),
            diagnostics=tuple(diagnostics),
        )
        finalized_coverage = (
            _finalize_requested_coverage(location_coverage)
            if zones is not None
            else tuple(location_coverage)
        )
        return CatalogRead(read, finalized_coverage)


class GoogleComputeMachineTypeReader:
    """Read project-visible machine types without inferring machine semantics."""

    def __init__(
        self,
        client: ComputeMachineTypesPageClient | ComputeMachineTypesZonalPageClient,
        policy: GoogleReadPolicy,
        *,
        page_size: int = 100,
        maximum_pages: int = 100,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind bounded pagination, retry policy, and observation clock."""
        _require_positive(page_size, "Compute catalog page_size")
        _require_positive(maximum_pages, "Compute catalog maximum_pages")
        self._client = client
        self._policy = policy
        self._page_size = page_size
        self._maximum_pages = maximum_pages
        self._now = now

    async def read(  # noqa: C901, PLR0912, PLR0915 - preserve scoped outcomes
        self,
        request: ComputeMachineTypeReadRequest,
    ) -> CatalogRead[ComputeMachineType]:
        """Return all bounded scopes with explicit empty and failed coverage."""
        if not isinstance(request, ComputeMachineTypeReadRequest):
            msg = "Compute catalog reader requires ComputeMachineTypeReadRequest"
            raise TypeError(msg)
        project = request.context.project.project_id
        zones = request.zones
        aggregated_client: ComputeMachineTypesPageClient | None = None
        zonal_client: ComputeMachineTypesZonalPageClient | None = None
        if zones is None:
            if not isinstance(self._client, ComputeMachineTypesPageClient):
                msg = "Compute machine client lacks aggregated list support"
                raise TypeError(msg)
            aggregated_client = self._client
        else:
            if not isinstance(self._client, ComputeMachineTypesZonalPageClient):
                msg = "Compute machine client lacks exact-zone list support"
                raise TypeError(msg)
            zonal_client = self._client
        expectation = (
            LocationCoverageExpectation.REQUESTED
            if zones is not None
            else LocationCoverageExpectation.EXPECTED
        )
        zone_index = 0
        token = ""
        attempted = 0
        completed = 0
        cap = False
        values: list[ComputeMachineType] = []
        diagnostics: list[Diagnostic] = []
        location_coverage: list[CatalogLocationCoverage] = []
        while attempted < self._maximum_pages:
            if zones is not None and zone_index >= len(zones):
                break
            zone = zones[zone_index] if zones is not None else None
            attempted += 1
            if zone is None:
                client = cast("ComputeMachineTypesPageClient", aggregated_client)
                result = await self._policy.call(
                    request.context,
                    provider="compute",
                    phase="compute-machine-types-read",
                    identity=f"compute-machine-types:{project}:{token}",
                    dispatch=lambda timeout, page_token=token, client=client: (
                        client.machine_types(
                            project=project,
                            max_results=self._page_size,
                            page_token=page_token,
                            return_partial_success=True,
                            timeout_seconds=timeout,
                        )
                    ),
                )
            else:
                client = cast("ComputeMachineTypesZonalPageClient", zonal_client)
                result = await self._policy.call(
                    request.context,
                    provider="compute",
                    phase="compute-machine-types-read",
                    identity=f"compute-machine-types:{project}:{zone}:{token}",
                    dispatch=(
                        lambda timeout, page_token=token, zone=zone, client=client: (
                            client.machine_types_for_zone(
                                project=project,
                                zone=zone,
                                max_results=self._page_size,
                                page_token=page_token,
                                timeout_seconds=timeout,
                            )
                        )
                    ),
                )
            if result.diagnostic is not None:
                diagnostics.append(result.diagnostic)
                location_coverage.append(
                    _coverage(
                        zone or "global",
                        LocationCoverageState.FAILED,
                        result.diagnostic,
                        expectation=expectation,
                    )
                )
                if zones is None:
                    break
                zone_index += 1
                token = ""
                if result.diagnostic.code.value in {
                    "provider-read-cancelled",
                    "provider-read-deadline-exceeded",
                }:
                    location_coverage.extend(
                        _coverage(
                            pending_zone,
                            LocationCoverageState.NOT_SCANNED,
                            result.diagnostic,
                            expectation=expectation,
                        )
                        for pending_zone in zones[zone_index:]
                    )
                    zone_index = len(zones)
                    break
                continue
            page = result.value
            if page is None:
                msg = "successful Compute catalog page call must contain a page"
                raise RuntimeError(msg)
            completed += 1
            for scoped in page.scopes:
                _consume_scope(
                    scoped,
                    project,
                    values,
                    diagnostics,
                    location_coverage,
                    expectation,
                )
            for unreachable in page.unreachable_scopes:
                diagnostic = _coverage_diagnostic(
                    "compute-catalog-location-unreachable"
                )
                diagnostics.append(diagnostic)
                location_coverage.append(
                    _coverage(
                        _scope_location(unreachable),
                        LocationCoverageState.FAILED,
                        diagnostic,
                        expectation=expectation,
                    )
                )
            if page.warning_code is not None:
                if page.warning_code == "NO_RESULTS_ON_PAGE":
                    location_coverage.append(
                        _coverage(
                            zone or "global",
                            LocationCoverageState.EMPTY,
                            expectation=expectation,
                        )
                    )
                else:
                    diagnostic = _coverage_diagnostic("compute-catalog-page-warning")
                    diagnostics.append(diagnostic)
                    location_coverage.append(
                        _coverage(
                            zone or "global",
                            LocationCoverageState.FAILED,
                            diagnostic,
                            expectation=expectation,
                        )
                    )
            token = page.next_page_token
            if not token:
                if zones is None:
                    break
                zone_index += 1
        cap = bool(token) or (zones is not None and zone_index < len(zones))
        if cap:
            diagnostic = page_cap_diagnostic("compute-machine-types-read", "compute")
            diagnostics.append(diagnostic)
            unscanned_locations = ("global",) if zones is None else zones[zone_index:]
            location_coverage.extend(
                CatalogLocationCoverage(
                    source=CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                    location=location,
                    expectation=expectation,
                    state=LocationCoverageState.NOT_SCANNED,
                    diagnostics=(diagnostic,),
                )
                for location in unscanned_locations
            )
        read = ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=self._now(),
            diagnostics=tuple(diagnostics),
        )
        finalized_coverage = (
            _finalize_requested_coverage(location_coverage)
            if zones is not None
            else tuple(location_coverage)
        )
        return CatalogRead(read, finalized_coverage)


def _consume_accelerator_scope(  # noqa: PLR0913 - explicit coverage evidence
    scoped: ComputeAcceleratorTypesScope,
    project: str,
    values: list[ComputeAcceleratorType],
    diagnostics: list[Diagnostic],
    coverage: list[CatalogLocationCoverage],
    expectation: LocationCoverageExpectation,
) -> None:
    try:
        location = _scope_location(scoped.scope)
    except ValueError:
        diagnostic = schema_diagnostic("compute-accelerator-types-read", "compute")
        diagnostics.append(diagnostic)
        coverage.append(
            _accelerator_coverage(
                "global",
                LocationCoverageState.FAILED,
                diagnostic,
                expectation=expectation,
            )
        )
        return
    scope_failed = False
    for item in scoped.accelerator_types:
        try:
            values.append(_map_accelerator_type(item, project, location))
        except (TypeError, ValueError, OverflowError):
            diagnostic = schema_diagnostic(
                "compute-accelerator-types-read",
                "compute",
            )
            diagnostics.append(diagnostic)
            scope_failed = True
    if scoped.warning_code is not None and scoped.warning_code != "NO_RESULTS_ON_PAGE":
        diagnostic = _accelerator_coverage_diagnostic(
            "compute-accelerator-catalog-scope-warning"
        )
        diagnostics.append(diagnostic)
        coverage.append(
            _accelerator_coverage(
                location,
                LocationCoverageState.FAILED,
                diagnostic,
                expectation=expectation,
            )
        )
    elif scope_failed:
        diagnostic = _accelerator_coverage_diagnostic(
            "compute-accelerator-catalog-scope-invalid"
        )
        diagnostics.append(diagnostic)
        coverage.append(
            _accelerator_coverage(
                location,
                LocationCoverageState.FAILED,
                diagnostic,
                expectation=expectation,
            )
        )
    else:
        coverage.append(
            _accelerator_coverage(
                location,
                (
                    LocationCoverageState.SUCCESS
                    if scoped.accelerator_types
                    else LocationCoverageState.EMPTY
                ),
                expectation=expectation,
            )
        )


def _map_accelerator_type(
    item: compute_v1.AcceleratorType,
    project: str,
    zone: str,
) -> ComputeAcceleratorType:
    _verify_accelerator_type_identity(item, project, zone)
    lifecycle = (
        ProviderSymbol(item.deprecated.state, CatalogLifecycle)
        if item.deprecated.state
        else None
    )
    return ComputeAcceleratorType(
        name=item.name,
        zone=zone,
        lifecycle=lifecycle,
    )


def _verify_accelerator_type_identity(
    item: compute_v1.AcceleratorType,
    project: str,
    zone: str,
) -> None:
    if not _is_canonical_compute_resource_name(item.name):
        msg = "Compute accelerator type must have one canonical resource name"
        raise ValueError(msg)
    expected_zone_link = (
        f"https://www.googleapis.com/compute/v1/projects/{project}/zones/{zone}"
    )
    expected_self_link = f"{expected_zone_link}/acceleratorTypes/{item.name}"
    if _canonical_resource_zone(item.zone, project) != zone:
        msg = "Compute accelerator type zone must match its project and scope"
        raise ValueError(msg)
    if item.self_link != expected_self_link:
        msg = "Compute accelerator type self link must match its identity"
        raise ValueError(msg)


def _accelerator_coverage(
    location: str,
    state: LocationCoverageState,
    diagnostic: Diagnostic | None = None,
    *,
    expectation: LocationCoverageExpectation = LocationCoverageExpectation.EXPECTED,
) -> CatalogLocationCoverage:
    return CatalogLocationCoverage(
        source=CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
        location=location,
        expectation=expectation,
        state=state,
        diagnostics=(diagnostic,) if diagnostic is not None else (),
    )


def _accelerator_coverage_diagnostic(code: str) -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode(code),
        severity=Severity.WARNING,
        phase=DiagnosticPhase("compute-accelerator-types-read"),
        source=DiagnosticSource("compute"),
        retry=RetryDisposition.AFTER_REFRESH,
        message=RedactedText(
            "Compute returned incomplete accelerator-type evidence for one location."
        ),
    )


def _finalize_requested_coverage(
    coverage: list[CatalogLocationCoverage],
) -> tuple[CatalogLocationCoverage, ...]:
    """Collapse paged exact-zone evidence into one fail-closed source record."""
    grouped: dict[
        tuple[
            CatalogEvidenceSource,
            str,
            LocationCoverageExpectation,
        ],
        list[CatalogLocationCoverage],
    ] = {}
    for item in coverage:
        grouped.setdefault(
            (item.source, item.location, item.expectation),
            [],
        ).append(item)

    finalized: list[CatalogLocationCoverage] = []
    for (source, location, expectation), items in grouped.items():
        states = {item.state for item in items}
        if LocationCoverageState.FAILED in states:
            state = LocationCoverageState.FAILED
        elif LocationCoverageState.NOT_SCANNED in states:
            state = LocationCoverageState.NOT_SCANNED
        elif LocationCoverageState.UNSUPPORTED in states:
            state = LocationCoverageState.UNSUPPORTED
        elif LocationCoverageState.SUCCESS in states:
            state = LocationCoverageState.SUCCESS
        else:
            state = LocationCoverageState.EMPTY
        finalized.append(
            CatalogLocationCoverage(
                source=source,
                location=location,
                expectation=expectation,
                state=state,
                diagnostics=tuple(
                    diagnostic for item in items for diagnostic in item.diagnostics
                ),
            )
        )
    return tuple(finalized)


def _consume_scope(  # noqa: PLR0913 - explicit coverage evidence
    scoped: ComputeMachineTypesScope,
    project: str,
    values: list[ComputeMachineType],
    diagnostics: list[Diagnostic],
    coverage: list[CatalogLocationCoverage],
    expectation: LocationCoverageExpectation,
) -> None:
    try:
        location = _scope_location(scoped.scope)
    except ValueError:
        diagnostic = schema_diagnostic("compute-machine-types-read", "compute")
        diagnostics.append(diagnostic)
        coverage.append(
            _coverage(
                "global",
                LocationCoverageState.FAILED,
                diagnostic,
                expectation=expectation,
            )
        )
        return
    scope_failed = False
    for item in scoped.machine_types:
        try:
            values.append(_map_machine_type(item, project, location))
        except (TypeError, ValueError, OverflowError):
            diagnostic = schema_diagnostic("compute-machine-types-read", "compute")
            diagnostics.append(diagnostic)
            scope_failed = True
    if scoped.warning_code is not None and scoped.warning_code != "NO_RESULTS_ON_PAGE":
        diagnostic = _coverage_diagnostic("compute-catalog-scope-warning")
        diagnostics.append(diagnostic)
        coverage.append(
            _coverage(
                location,
                LocationCoverageState.FAILED,
                diagnostic,
                expectation=expectation,
            )
        )
    elif scope_failed:
        diagnostic = _coverage_diagnostic("compute-catalog-scope-invalid")
        diagnostics.append(diagnostic)
        coverage.append(
            _coverage(
                location,
                LocationCoverageState.FAILED,
                diagnostic,
                expectation=expectation,
            )
        )
    else:
        coverage.append(
            _coverage(
                location,
                (
                    LocationCoverageState.SUCCESS
                    if scoped.machine_types
                    else LocationCoverageState.EMPTY
                ),
                expectation=expectation,
            )
        )


def _map_machine_type(
    item: compute_v1.MachineType,
    project: str,
    zone: str,
) -> ComputeMachineType:
    _verify_machine_type_identity(item, project, zone)
    lifecycle = (
        ProviderSymbol(item.deprecated.state, CatalogLifecycle)
        if item.deprecated.state
        else None
    )
    return ComputeMachineType(
        name=item.name,
        zone=zone,
        guest_accelerators=tuple(
            AcceleratorAttachment(
                accelerator_type=accelerator.guest_accelerator_type,
                count=accelerator.guest_accelerator_count,
            )
            for accelerator in item.accelerators
        ),
        lifecycle=lifecycle,
    )


def _verify_machine_type_identity(
    item: compute_v1.MachineType,
    project: str,
    zone: str,
) -> None:
    if not _is_canonical_compute_resource_name(item.name):
        msg = "Compute machine type must have one canonical resource name"
        raise ValueError(msg)
    expected_zone_link = (
        f"https://www.googleapis.com/compute/v1/projects/{project}/zones/{zone}"
    )
    expected_self_link = f"{expected_zone_link}/machineTypes/{item.name}"
    if _canonical_resource_zone(item.zone, project) != zone:
        msg = "Compute machine type zone must match its requested project and scope"
        raise ValueError(msg)
    if item.self_link != expected_self_link:
        msg = "Compute machine type self link must match its name, project, and scope"
        raise ValueError(msg)


def _scope_location(scope: str) -> str:
    prefix = "zones/"
    if not isinstance(scope, str) or not scope.startswith(prefix):
        msg = "Compute machine-type scope must identify one zone"
        raise ValueError(msg)
    location = scope.removeprefix(prefix)
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    if (
        not location
        or not location.isascii()
        or location != location.lower()
        or not location[0].isalnum()
        or not location[-1].isalnum()
        or any(character not in allowed for character in location)
        or not is_canonical_zone(location)
    ):
        msg = "Compute machine-type scope must identify one zone"
        raise ValueError(msg)
    return location


def _canonical_resource_zone(value: object, project: str) -> str:
    """Normalize the two official Compute zone identity representations."""
    if isinstance(value, str) and is_canonical_zone(value):
        return value
    prefix = f"https://www.googleapis.com/compute/v1/projects/{project}/zones/"
    if not isinstance(value, str) or not value.startswith(prefix):
        msg = "Compute resource zone must be a canonical short or full identity"
        raise ValueError(msg)
    zone = value.removeprefix(prefix)
    if not is_canonical_zone(zone):
        msg = "Compute resource zone must be a canonical short or full identity"
        raise ValueError(msg)
    return zone


def _is_canonical_compute_resource_name(value: object) -> bool:
    """Require one unambiguous Compute collection resource name."""
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and value == value.lower()
        and value[0].isalnum()
        and value[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in value)
    )


def _coverage(
    location: str,
    state: LocationCoverageState,
    diagnostic: Diagnostic | None = None,
    *,
    expectation: LocationCoverageExpectation = LocationCoverageExpectation.EXPECTED,
) -> CatalogLocationCoverage:
    return CatalogLocationCoverage(
        source=CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
        location=location,
        expectation=expectation,
        state=state,
        diagnostics=(diagnostic,) if diagnostic is not None else (),
    )


def _coverage_diagnostic(code: str) -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode(code),
        severity=Severity.WARNING,
        phase=DiagnosticPhase("compute-machine-types-read"),
        source=DiagnosticSource("compute"),
        retry=RetryDisposition.AFTER_REFRESH,
        message=RedactedText(
            "Compute returned incomplete machine-type evidence for one location."
        ),
    )


def _require_positive(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{name} must be positive"
        raise ValueError(msg)
