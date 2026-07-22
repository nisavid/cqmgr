"""Read-only Cloud TPU location, accelerator, and runtime catalog adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from google.cloud import tpu_v2
from google.cloud.location import locations_pb2

from cqmgr.adapters.google.read_policy import (
    GoogleReadPolicy,
    page_cap_diagnostic,
    schema_diagnostic,
)
from cqmgr.application.ports.catalog_reads import (
    CatalogRead,
    TpuAcceleratorTypeReadRequest,
    TpuLocationReadRequest,
    TpuRuntimeVersionReadRequest,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.catalog import (
    CatalogEvidenceSource,
    CatalogLocationCoverage,
    LocationCoverageExpectation,
    LocationCoverageState,
    TpuAcceleratorConfig,
    TpuAcceleratorType,
    TpuLocation,
    TpuRuntimeVersion,
)
from cqmgr.domain.quotas import ProviderRead, ProviderReadCoverage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cqmgr.domain.diagnostics import Diagnostic


class _TpuPage[ItemT](Protocol):
    """Structural materialized TPU page used by the shared bounded reader."""

    @property
    def items(self) -> tuple[ItemT, ...]:
        """Return materialized provider DTOs."""
        ...

    @property
    def next_page_token(self) -> str:
        """Return the provider continuation token."""
        ...


@dataclass(frozen=True, slots=True)
class TpuLocationsPage:
    """Adapter-internal materialized TPU locations page."""

    items: tuple[locations_pb2.Location, ...]
    next_page_token: str


@dataclass(frozen=True, slots=True)
class TpuAcceleratorTypesPage:
    """Adapter-internal materialized TPU accelerator-types page."""

    items: tuple[tpu_v2.AcceleratorType, ...]
    next_page_token: str


@dataclass(frozen=True, slots=True)
class TpuRuntimeVersionsPage:
    """Adapter-internal materialized TPU runtime-versions page."""

    items: tuple[tpu_v2.RuntimeVersion, ...]
    next_page_token: str


class TpuCatalogPageClient(Protocol):
    """Narrow official-client methods that each materialize one page."""

    async def locations(
        self,
        *,
        name: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> TpuLocationsPage:
        """Return one materialized locations page."""
        ...

    async def accelerator_types(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> TpuAcceleratorTypesPage:
        """Return one materialized accelerator-types page."""
        ...

    async def runtime_versions(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> TpuRuntimeVersionsPage:
        """Return one materialized runtime-versions page."""
        ...


class OfficialTpuCatalogPageClient:
    """Keep official requests, async pagers, and generated DTOs in the adapter."""

    def __init__(self, client: tpu_v2.TpuAsyncClient) -> None:
        """Bind one official native-async client."""
        self._client = client

    async def locations(
        self,
        *,
        name: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> TpuLocationsPage:
        """Materialize one exact locations response page."""
        request = locations_pb2.ListLocationsRequest(
            name=name,
            page_size=page_size,
            page_token=page_token,
        )
        response = await self._client.list_locations(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        return TpuLocationsPage(tuple(response.locations), response.next_page_token)

    async def accelerator_types(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> TpuAcceleratorTypesPage:
        """Materialize one exact accelerator-types response page."""
        request = tpu_v2.ListAcceleratorTypesRequest(
            parent=parent,
            page_size=page_size,
            page_token=page_token,
        )
        pager = await self._client.list_accelerator_types(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = await anext(pager.pages)
        return TpuAcceleratorTypesPage(
            tuple(response.accelerator_types),
            response.next_page_token,
        )

    async def runtime_versions(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> TpuRuntimeVersionsPage:
        """Materialize one exact runtime-versions response page."""
        request = tpu_v2.ListRuntimeVersionsRequest(
            parent=parent,
            page_size=page_size,
            page_token=page_token,
        )
        pager = await self._client.list_runtime_versions(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = await anext(pager.pages)
        return TpuRuntimeVersionsPage(
            tuple(response.runtime_versions),
            response.next_page_token,
        )


class _GoogleTpuCatalogReader:
    def __init__(
        self,
        client: TpuCatalogPageClient,
        policy: GoogleReadPolicy,
        *,
        page_size: int = 100,
        maximum_pages: int = 100,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        _require_positive(page_size, "TPU catalog page_size")
        _require_positive(maximum_pages, "TPU catalog maximum_pages")
        self._client = client
        self._policy = policy
        self._page_size = page_size
        self._maximum_pages = maximum_pages
        self._now = now


class GoogleTpuLocationReader(_GoogleTpuCatalogReader):
    """Read all Cloud TPU service locations for an explicit project."""

    async def read(self, request: TpuLocationReadRequest) -> CatalogRead[TpuLocation]:
        """Return bounded locations with independently visible coverage."""
        if not isinstance(request, TpuLocationReadRequest):
            msg = "TPU location reader requires TpuLocationReadRequest"
            raise TypeError(msg)
        parent = request.context.project.resource_scope.canonical_name
        token = ""
        attempted = completed = 0
        cap = False
        values: list[TpuLocation] = []
        diagnostics: list[Diagnostic] = []
        coverage: list[CatalogLocationCoverage] = []
        while attempted < self._maximum_pages:
            attempted += 1
            result = await self._policy.call(
                request.context,
                provider="cloud-tpu",
                phase="tpu-locations-read",
                identity=f"tpu-locations:{parent}:{token}",
                dispatch=lambda timeout, page_token=token: self._client.locations(
                    name=parent,
                    page_size=self._page_size,
                    page_token=page_token,
                    timeout_seconds=timeout,
                ),
            )
            if result.diagnostic is not None:
                diagnostics.append(result.diagnostic)
                coverage.append(
                    _coverage(
                        CatalogEvidenceSource.TPU_LOCATIONS,
                        "global",
                        LocationCoverageExpectation.EXPECTED,
                        LocationCoverageState.FAILED,
                        result.diagnostic,
                    )
                )
                break
            page = result.value
            if page is None:
                msg = "successful TPU location page must contain a page"
                raise RuntimeError(msg)
            completed += 1
            for item in page.items:
                try:
                    mapped = TpuLocation(item.name, item.location_id)
                except (TypeError, ValueError):
                    diagnostic = schema_diagnostic("tpu-locations-read", "cloud-tpu")
                    diagnostics.append(diagnostic)
                    coverage.append(
                        _coverage(
                            CatalogEvidenceSource.TPU_LOCATIONS,
                            "global",
                            LocationCoverageExpectation.EXPECTED,
                            LocationCoverageState.FAILED,
                            diagnostic,
                        )
                    )
                    continue
                values.append(mapped)
                coverage.append(
                    _coverage(
                        CatalogEvidenceSource.TPU_LOCATIONS,
                        mapped.location_id,
                        LocationCoverageExpectation.EXPECTED,
                        LocationCoverageState.SUCCESS,
                    )
                )
            token = page.next_page_token
            if not token:
                break
        else:
            cap = bool(token)
        if not values and completed and not diagnostics:
            coverage.append(
                _coverage(
                    CatalogEvidenceSource.TPU_LOCATIONS,
                    "global",
                    LocationCoverageExpectation.EXPECTED,
                    LocationCoverageState.EMPTY,
                )
            )
        if cap:
            diagnostic = page_cap_diagnostic("tpu-locations-read", "cloud-tpu")
            diagnostics.append(diagnostic)
            coverage.append(
                _coverage(
                    CatalogEvidenceSource.TPU_LOCATIONS,
                    "global",
                    LocationCoverageExpectation.EXPECTED,
                    LocationCoverageState.NOT_SCANNED,
                    diagnostic,
                )
            )
        return _result(
            values, attempted, completed, cap, diagnostics, coverage, self._now
        )


class GoogleTpuAcceleratorTypeReader(_GoogleTpuCatalogReader):
    """Read legacy Cloud TPU accelerator types for one requested zone."""

    async def read(
        self,
        request: TpuAcceleratorTypeReadRequest,
    ) -> CatalogRead[TpuAcceleratorType]:
        """Return bounded accelerator types for one requested zone."""
        if not isinstance(request, TpuAcceleratorTypeReadRequest):
            msg = "TPU accelerator reader requires TpuAcceleratorTypeReadRequest"
            raise TypeError(msg)
        return await _read_zone_pages(
            self,
            request.context,
            request.zone,
            source=CatalogEvidenceSource.TPU_ACCELERATOR_TYPES,
            phase="tpu-accelerator-types-read",
            read_page=self._client.accelerator_types,
            map_item=lambda item: _map_accelerator(item, request.zone),
        )


class GoogleTpuRuntimeVersionReader(_GoogleTpuCatalogReader):
    """Read legacy Cloud TPU runtime versions for one requested zone."""

    async def read(
        self,
        request: TpuRuntimeVersionReadRequest,
    ) -> CatalogRead[TpuRuntimeVersion]:
        """Return bounded runtime versions for one requested zone."""
        if not isinstance(request, TpuRuntimeVersionReadRequest):
            msg = "TPU runtime reader requires TpuRuntimeVersionReadRequest"
            raise TypeError(msg)
        return await _read_zone_pages(
            self,
            request.context,
            request.zone,
            source=CatalogEvidenceSource.TPU_RUNTIME_VERSIONS,
            phase="tpu-runtime-versions-read",
            read_page=self._client.runtime_versions,
            map_item=lambda item: TpuRuntimeVersion(
                name=item.name,
                zone=request.zone,
                version=item.version,
            ),
        )


async def _read_zone_pages[ItemT, ResultT](  # noqa: C901, PLR0913
    reader: _GoogleTpuCatalogReader,
    context: object,
    zone: str,
    *,
    source: CatalogEvidenceSource,
    phase: str,
    read_page: Callable[..., Awaitable[_TpuPage[ItemT]]],
    map_item: Callable[[ItemT], ResultT],
) -> CatalogRead[ResultT]:
    if not isinstance(context, ProviderReadContext):
        msg = "TPU catalog read requires ProviderReadContext"
        raise TypeError(msg)
    parent = f"{context.project.resource_scope.canonical_name}/locations/{zone}"
    token = ""
    attempted = completed = 0
    cap = False
    values: list[ResultT] = []
    diagnostics: list[Diagnostic] = []
    while attempted < reader._maximum_pages:  # noqa: SLF001
        attempted += 1
        result = await reader._policy.call(  # noqa: SLF001
            context,
            provider="cloud-tpu",
            phase=phase,
            identity=f"{phase}:{parent}:{token}",
            dispatch=lambda timeout, page_token=token: read_page(
                parent=parent,
                page_size=reader._page_size,  # noqa: SLF001
                page_token=page_token,
                timeout_seconds=timeout,
            ),
        )
        if result.diagnostic is not None:
            diagnostics.append(result.diagnostic)
            break
        page = result.value
        if page is None:
            msg = "successful TPU catalog page must contain a page"
            raise RuntimeError(msg)
        completed += 1
        for item in page.items:
            try:
                values.append(map_item(item))
            except (TypeError, ValueError, OverflowError):
                diagnostics.append(schema_diagnostic(phase, "cloud-tpu"))
        token = page.next_page_token
        if not token:
            break
    else:
        cap = bool(token)
    if cap:
        diagnostics.append(page_cap_diagnostic(phase, "cloud-tpu"))
    if cap:
        state = LocationCoverageState.NOT_SCANNED
        coverage_diagnostics = tuple(diagnostics)
    elif diagnostics:
        state = LocationCoverageState.FAILED
        coverage_diagnostics = tuple(diagnostics)
    else:
        state = LocationCoverageState.SUCCESS if values else LocationCoverageState.EMPTY
        coverage_diagnostics = ()
    coverage = CatalogLocationCoverage(
        source=source,
        location=zone,
        expectation=LocationCoverageExpectation.REQUESTED,
        state=state,
        diagnostics=coverage_diagnostics,
    )
    return _result(
        values,
        attempted,
        completed,
        cap,
        diagnostics,
        [coverage],
        reader._now,  # noqa: SLF001
    )


def _map_accelerator(
    item: tpu_v2.AcceleratorType,
    zone: str,
) -> TpuAcceleratorType:
    return TpuAcceleratorType(
        name=item.name,
        zone=zone,
        accelerator_type=item.type_,
        configurations=tuple(
            TpuAcceleratorConfig(
                version=(
                    config.type_.name
                    if hasattr(config.type_, "name")
                    else str(config.type_)
                ),
                topology=config.topology,
            )
            for config in item.accelerator_configs
        ),
    )


def _result[ReadT](  # noqa: PLR0913
    values: list[ReadT],
    attempted: int,
    completed: int,
    cap: bool,  # noqa: FBT001
    diagnostics: list[Diagnostic],
    coverage: list[CatalogLocationCoverage],
    now: Callable[[], datetime],
) -> CatalogRead[ReadT]:
    return CatalogRead(
        ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=now(),
            diagnostics=tuple(diagnostics),
        ),
        tuple(coverage),
    )


def _coverage(
    source: CatalogEvidenceSource,
    location: str,
    expectation: LocationCoverageExpectation,
    state: LocationCoverageState,
    diagnostic: Diagnostic | None = None,
) -> CatalogLocationCoverage:
    return CatalogLocationCoverage(
        source=source,
        location=location,
        expectation=expectation,
        state=state,
        diagnostics=(diagnostic,) if diagnostic is not None else (),
    )


def _require_positive(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{name} must be positive"
        raise ValueError(msg)
