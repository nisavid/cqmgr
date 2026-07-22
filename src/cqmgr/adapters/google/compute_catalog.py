"""Read-only Compute machine-type catalog adapter with scoped coverage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from google.cloud import compute_v1

from cqmgr.adapters.google.read_policy import (
    GoogleReadPolicy,
    page_cap_diagnostic,
    schema_diagnostic,
)
from cqmgr.application.ports.catalog_reads import (
    CatalogRead,
    ComputeMachineTypeReadRequest,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    CatalogEvidenceSource,
    CatalogLifecycle,
    CatalogLocationCoverage,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
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


class OfficialComputeMachineTypesPageClient:
    """Fence the sync-only official Compute client behind an async worker."""

    def __init__(
        self,
        client: compute_v1.MachineTypesClient,
        *,
        maximum_workers: int = 4,
    ) -> None:
        """Bind one client and cap concurrent default-executor dispatches."""
        _require_positive(maximum_workers, "Compute catalog maximum_workers")
        self._client = client
        self._worker_slots = asyncio.Semaphore(maximum_workers)

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
        await self._worker_slots.acquire()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._machine_types,
                project=project,
                max_results=max_results,
                page_token=page_token,
                return_partial_success=return_partial_success,
                timeout_seconds=timeout_seconds,
            )
        )
        worker.add_done_callback(self._release_worker_slot)
        return await asyncio.shield(worker)

    def _release_worker_slot(
        self,
        worker: asyncio.Task[ComputeMachineTypesPage],
    ) -> None:
        """Release concurrency only after the uncancellable sync call stops."""
        self._worker_slots.release()
        if not worker.cancelled():
            worker.exception()

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


def _warning_code(warning: object) -> str | None:
    code = getattr(warning, "code", None)
    if not code:
        return None
    name = getattr(code, "name", None)
    return name if isinstance(name, str) and name else str(code)


class GoogleComputeMachineTypeReader:
    """Read project-visible machine types without inferring machine semantics."""

    def __init__(
        self,
        client: ComputeMachineTypesPageClient,
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

    async def read(  # noqa: C901 - preserves every scoped coverage outcome
        self,
        request: ComputeMachineTypeReadRequest,
    ) -> CatalogRead[ComputeMachineType]:
        """Return all bounded scopes with explicit empty and failed coverage."""
        if not isinstance(request, ComputeMachineTypeReadRequest):
            msg = "Compute catalog reader requires ComputeMachineTypeReadRequest"
            raise TypeError(msg)
        project = request.context.project.project_id
        token = ""
        attempted = 0
        completed = 0
        cap = False
        values: list[ComputeMachineType] = []
        diagnostics: list[Diagnostic] = []
        location_coverage: list[CatalogLocationCoverage] = []
        while attempted < self._maximum_pages:
            attempted += 1
            result = await self._policy.call(
                request.context,
                provider="compute",
                phase="compute-machine-types-read",
                identity=f"compute-machine-types:{project}:{token}",
                dispatch=lambda timeout, page_token=token: self._client.machine_types(
                    project=project,
                    max_results=self._page_size,
                    page_token=page_token,
                    return_partial_success=True,
                    timeout_seconds=timeout,
                ),
            )
            if result.diagnostic is not None:
                diagnostics.append(result.diagnostic)
                location_coverage.append(
                    _coverage("global", LocationCoverageState.FAILED, result.diagnostic)
                )
                break
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
                    )
                )
            if page.warning_code is not None:
                if page.warning_code == "NO_RESULTS_ON_PAGE":
                    location_coverage.append(
                        _coverage("global", LocationCoverageState.EMPTY)
                    )
                else:
                    diagnostic = _coverage_diagnostic("compute-catalog-page-warning")
                    diagnostics.append(diagnostic)
                    location_coverage.append(
                        _coverage("global", LocationCoverageState.FAILED, diagnostic)
                    )
            token = page.next_page_token
            if not token:
                break
        else:
            cap = bool(token)
        if cap:
            diagnostic = page_cap_diagnostic("compute-machine-types-read", "compute")
            diagnostics.append(diagnostic)
            location_coverage.append(
                CatalogLocationCoverage(
                    source=CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                    location="global",
                    expectation=LocationCoverageExpectation.EXPECTED,
                    state=LocationCoverageState.NOT_SCANNED,
                    diagnostics=(diagnostic,),
                )
            )
        read = ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=self._now(),
            diagnostics=tuple(diagnostics),
        )
        return CatalogRead(read, tuple(location_coverage))


def _consume_scope(
    scoped: ComputeMachineTypesScope,
    project: str,
    values: list[ComputeMachineType],
    diagnostics: list[Diagnostic],
    coverage: list[CatalogLocationCoverage],
) -> None:
    try:
        location = _scope_location(scoped.scope)
    except ValueError:
        diagnostic = schema_diagnostic("compute-machine-types-read", "compute")
        diagnostics.append(diagnostic)
        coverage.append(_coverage("global", LocationCoverageState.FAILED, diagnostic))
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
        coverage.append(_coverage(location, LocationCoverageState.FAILED, diagnostic))
    elif scope_failed:
        diagnostic = _coverage_diagnostic("compute-catalog-scope-invalid")
        diagnostics.append(diagnostic)
        coverage.append(_coverage(location, LocationCoverageState.FAILED, diagnostic))
    else:
        coverage.append(
            _coverage(
                location,
                (
                    LocationCoverageState.SUCCESS
                    if scoped.machine_types
                    else LocationCoverageState.EMPTY
                ),
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
    expected_zone_link = (
        f"https://www.googleapis.com/compute/v1/projects/{project}/zones/{zone}"
    )
    expected_self_link = f"{expected_zone_link}/machineTypes/{item.name}"
    if item.zone != zone:
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
        or not _is_canonical_zone(location)
    ):
        msg = "Compute machine-type scope must identify one zone"
        raise ValueError(msg)
    return location


def _is_canonical_zone(value: str) -> bool:
    """Distinguish one exact zone from a region-shaped location."""
    region, separator, suffix = value.rpartition("-")
    return (
        separator == "-"
        and "-" in region
        and all(region.split("-"))
        and region[-1:].isdigit()
        and len(suffix) == 1
        and suffix.isalpha()
    )


def _coverage(
    location: str,
    state: LocationCoverageState,
    diagnostic: Diagnostic | None = None,
) -> CatalogLocationCoverage:
    return CatalogLocationCoverage(
        source=CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
        location=location,
        expectation=LocationCoverageExpectation.EXPECTED,
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
