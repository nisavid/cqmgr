"""Edge contracts for read-only Google accelerator-catalog adapters."""

# ruff: noqa: ASYNC109, D102, D107, S105, S106

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from google.cloud import compute_v1, tpu_v2
from google.cloud.location import locations_pb2

from cqmgr.adapters.google.compute_catalog import (
    ComputeAcceleratorTypesPage,
    ComputeAcceleratorTypesScope,
    ComputeMachineTypesPage,
    ComputeMachineTypesScope,
    GoogleComputeAcceleratorTypeReader,
    GoogleComputeMachineTypeReader,
    OfficialComputeAcceleratorTypesPageClient,
    OfficialComputeMachineTypesPageClient,
)
from cqmgr.adapters.google.read_policy import GoogleReadPolicy
from cqmgr.adapters.google.tpu_catalog import (
    GoogleTpuAcceleratorTypeReader,
    GoogleTpuLocationReader,
    GoogleTpuRuntimeVersionReader,
    OfficialTpuCatalogPageClient,
    TpuAcceleratorTypesPage,
    TpuLocationsPage,
    TpuRuntimeVersionsPage,
)
from cqmgr.application.ports.catalog_reads import (
    CatalogRead,
    ComputeAcceleratorTypeReadRequest,
    ComputeMachineTypeReadRequest,
    TpuAcceleratorTypeReadRequest,
    TpuLocationReadRequest,
    TpuRuntimeVersionReadRequest,
)
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    BudgetRequest,
    CancellationToken,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.catalog import LocationCoverageState
from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject, CredentialKind
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

NOW = datetime(2026, 7, 22, 9, tzinfo=UTC)


class RecordingBudget:
    """Grant local test reads without provider access."""

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        cancellation.raise_if_cancelled()
        return BudgetGrant(deadline - 1, request)


class NoJitter:
    """Keep the one-attempt test policy deterministic."""

    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        del attempt, identity
        return min(delay, 0.0)


@pytest.mark.parametrize(
    "wrapper",
    [
        OfficialComputeAcceleratorTypesPageClient,
        OfficialComputeMachineTypesPageClient,
    ],
)
def test_official_compute_page_clients_close_owned_transport(
    wrapper: type[
        OfficialComputeAcceleratorTypesPageClient
        | OfficialComputeMachineTypesPageClient
    ],
) -> None:
    """Every sync Compute wrapper exposes its generated transport shutdown."""
    closed: list[bool] = []
    client = SimpleNamespace(
        transport=SimpleNamespace(close=lambda: closed.append(True))
    )

    page_client = wrapper(cast("Any", client))
    asyncio.run(page_client.close())

    assert closed == [True]


def test_compute_close_waits_for_shielded_sync_worker_after_cancellation() -> None:
    """Transport shutdown follows real completion of a cancelled sync page read."""
    started = Event()
    release = Event()
    finished = Event()
    closed_after_finish: list[bool] = []

    class BlockingGeneratedClient:
        transport = SimpleNamespace(
            close=lambda: closed_after_finish.append(finished.is_set())
        )

        def aggregated_list(self, **kwargs: object) -> object:
            del kwargs
            started.set()
            release.wait()
            finished.set()
            response = SimpleNamespace(
                items={},
                next_page_token="",
                unreachables=(),
                warning=None,
            )
            return SimpleNamespace(pages=iter((response,)))

    async def exercise() -> None:
        page_client = OfficialComputeMachineTypesPageClient(
            cast("Any", BlockingGeneratedClient())
        )
        read_task = asyncio.create_task(
            page_client.machine_types(
                project="fixture-project",
                max_results=1,
                page_token="",
                return_partial_success=True,
                timeout_seconds=1.0,
            )
        )
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=0.5)
        read_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read_task

        try:
            close_task = asyncio.create_task(page_client.close())
            await asyncio.sleep(0.01)
            assert not close_task.done()
            assert closed_after_finish == []
        finally:
            release.set()
        await asyncio.wait_for(close_task, timeout=0.5)

    try:
        asyncio.run(exercise())
    finally:
        release.set()

    assert closed_after_finish == [True]


class ComputePages:
    """Script materialized Compute pages at the provider boundary."""

    def __init__(
        self,
        pages: Sequence[ComputeMachineTypesPage | BaseException] = (),
    ) -> None:
        self.pages = list(pages)
        self.calls = 0

    async def machine_types(self, **kwargs: object) -> ComputeMachineTypesPage:
        del kwargs
        self.calls += 1
        value = self.pages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ComputeAcceleratorPages:
    """Script materialized Compute accelerator-type pages."""

    def __init__(
        self,
        pages: Sequence[ComputeAcceleratorTypesPage | BaseException] = (),
    ) -> None:
        self.pages = list(pages)
        self.calls = 0

    async def accelerator_types(self, **kwargs: object) -> ComputeAcceleratorTypesPage:
        del kwargs
        self.calls += 1
        value = self.pages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class TpuPages:
    """Script materialized TPU pages at the provider boundary."""

    def __init__(
        self,
        *,
        locations: Sequence[TpuLocationsPage | BaseException] = (),
        accelerators: Sequence[TpuAcceleratorTypesPage | BaseException] = (),
        runtimes: Sequence[TpuRuntimeVersionsPage | BaseException] = (),
    ) -> None:
        self.locations_pages = list(locations)
        self.accelerator_pages = list(accelerators)
        self.runtime_pages = list(runtimes)
        self.calls = 0

    async def locations(self, **kwargs: object) -> TpuLocationsPage:
        del kwargs
        self.calls += 1
        return cast("TpuLocationsPage", self._take(self.locations_pages))

    async def accelerator_types(self, **kwargs: object) -> TpuAcceleratorTypesPage:
        del kwargs
        self.calls += 1
        return cast("TpuAcceleratorTypesPage", self._take(self.accelerator_pages))

    async def runtime_versions(self, **kwargs: object) -> TpuRuntimeVersionsPage:
        del kwargs
        self.calls += 1
        return cast("TpuRuntimeVersionsPage", self._take(self.runtime_pages))

    @staticmethod
    def _take[ValueT](values: list[ValueT | BaseException]) -> ValueT:
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _policy() -> GoogleReadPolicy:
    return GoogleReadPolicy(RecordingBudget(), NoJitter(), maximum_attempts=1)


def _context(*, cancellation: CancellationToken | None = None) -> ProviderReadContext:
    return ProviderReadContext(
        project=CanonicalProject(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
            "public-schema-project",
            "Public Schema Project",
        ),
        identity=ADCIdentityEvidence.principal_unverified(
            credential_kind=CredentialKind.UNKNOWN,
            adc_quota_project=ADCQuotaProject("public-quota-project"),
        ),
        deadline=time.monotonic() + 30,
        cancellation=cancellation or CancellationToken(),
    )


def _diagnostic_codes[ValueT](result: CatalogRead[ValueT]) -> list[str]:
    return [item.code.value for item in result.read.diagnostics]


def test_readers_reject_nonpositive_pagination_limits() -> None:
    """Every catalog reader requires finite positive pagination bounds."""
    with pytest.raises(ValueError, match="page_size"):
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages(), _policy(), page_size=0
        )
    with pytest.raises(ValueError, match="maximum_pages"):
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages(), _policy(), maximum_pages=0
        )
    with pytest.raises(ValueError, match="page_size"):
        GoogleComputeMachineTypeReader(ComputePages(), _policy(), page_size=0)
    with pytest.raises(ValueError, match="maximum_pages"):
        GoogleComputeMachineTypeReader(ComputePages(), _policy(), maximum_pages=0)
    with pytest.raises(ValueError, match="page_size"):
        GoogleTpuLocationReader(TpuPages(), _policy(), page_size=0)
    with pytest.raises(ValueError, match="maximum_pages"):
        GoogleTpuAcceleratorTypeReader(TpuPages(), _policy(), maximum_pages=0)


def test_readers_reject_requests_for_another_catalog_seam() -> None:
    """A typed reader cannot accidentally consume another catalog request."""
    context = _context()
    compute_request = ComputeMachineTypeReadRequest(context)
    location_request = TpuLocationReadRequest(context)

    with pytest.raises(TypeError, match="ComputeMachineTypeReadRequest"):
        asyncio.run(
            GoogleComputeMachineTypeReader(ComputePages(), _policy()).read(
                cast("ComputeMachineTypeReadRequest", location_request)
            )
        )
    with pytest.raises(TypeError, match="TpuLocationReadRequest"):
        asyncio.run(
            GoogleTpuLocationReader(TpuPages(), _policy()).read(
                cast("TpuLocationReadRequest", compute_request)
            )
        )
    with pytest.raises(TypeError, match="TpuAcceleratorTypeReadRequest"):
        asyncio.run(
            GoogleTpuAcceleratorTypeReader(TpuPages(), _policy()).read(
                cast("TpuAcceleratorTypeReadRequest", compute_request)
            )
        )
    with pytest.raises(TypeError, match="TpuRuntimeVersionReadRequest"):
        asyncio.run(
            GoogleTpuRuntimeVersionReader(TpuPages(), _policy()).read(
                cast("TpuRuntimeVersionReadRequest", compute_request)
            )
        )


def test_compute_reader_keeps_valid_items_when_a_scope_item_is_malformed() -> None:
    """Malformed machine evidence fails its scope without hiding valid siblings."""
    valid = compute_v1.MachineType(
        name="a3-highgpu-8g",
        zone="us-central1-a",
        self_link=(
            "https://www.googleapis.com/compute/v1/projects/public-schema-project/"
            "zones/us-central1-a/machineTypes/a3-highgpu-8g"
        ),
        deprecated=compute_v1.DeprecationStatus(state="PROVIDER_NEW_STATE"),
    )
    invalid = compute_v1.MachineType(
        name="bad-shape",
        zone="us-central1-a",
        self_link=(
            "https://www.googleapis.com/compute/v1/projects/public-schema-project/"
            "zones/us-central1-a/machineTypes/bad-shape"
        ),
        accelerators=[
            compute_v1.Accelerators(
                guest_accelerator_type="nvidia-h100-80gb",
                guest_accelerator_count=0,
            )
        ],
    )
    page = ComputeMachineTypesPage(
        scopes=(ComputeMachineTypesScope("zones/us-central1-a", (valid, invalid)),),
        next_page_token="",
    )

    result = asyncio.run(
        GoogleComputeMachineTypeReader(
            ComputePages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeMachineTypeReadRequest(_context()))
    )

    assert [item.name for item in result.values] == ["a3-highgpu-8g"]
    assert result.values[0].lifecycle is not None
    assert result.values[0].lifecycle.raw == "PROVIDER_NEW_STATE"
    assert result.values[0].lifecycle.known is None
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert _diagnostic_codes(result) == [
        "provider-schema-invalid",
        "compute-catalog-scope-invalid",
    ]


def test_compute_reader_rejects_items_outside_the_requested_project_and_zone() -> None:
    """Provider DTO identity cannot be relabeled as the requested project or zone."""
    page = ComputeMachineTypesPage(
        scopes=(
            ComputeMachineTypesScope(
                "zones/us-central1-a",
                (
                    compute_v1.MachineType(
                        name="a3-highgpu-8g",
                        zone="us-central1-a",
                        self_link=(
                            "https://www.googleapis.com/compute/v1/projects/other/"
                            "zones/us-central1-a/machineTypes/a3-highgpu-8g"
                        ),
                    ),
                    compute_v1.MachineType(
                        name="ct6e-standard-4t",
                        zone="us-east1-b",
                        self_link=(
                            "https://www.googleapis.com/compute/v1/projects/"
                            "public-schema-project/"
                            "zones/us-east1-b/machineTypes/ct6e-standard-4t"
                        ),
                    ),
                    compute_v1.MachineType(
                        name="a3-highgpu-8g",
                        zone="us-central1-a",
                        self_link=(
                            "https://www.googleapis.com/compute/v1/projects/"
                            "public-schema-project/"
                            "zones/us-central1-a/machineTypes/another-machine"
                        ),
                    ),
                    compute_v1.MachineType(name="identity-missing"),
                ),
            ),
        ),
        next_page_token="",
    )

    result = asyncio.run(
        GoogleComputeMachineTypeReader(ComputePages((page,)), _policy()).read(
            ComputeMachineTypeReadRequest(_context())
        )
    )

    assert result.values == ()
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert _diagnostic_codes(result) == [
        "provider-schema-invalid",
        "provider-schema-invalid",
        "provider-schema-invalid",
        "provider-schema-invalid",
        "compute-catalog-scope-invalid",
    ]
    assert not result.complete


def test_compute_accelerator_reader_rejects_misattributed_provider_items() -> None:
    """Compute accelerator evidence must prove its project, scope, and name."""
    valid = compute_v1.AcceleratorType(
        name="nvidia-b200",
        zone="us-central1-a",
        self_link=(
            "https://www.googleapis.com/compute/v1/projects/public-schema-project/"
            "zones/us-central1-a/acceleratorTypes/nvidia-b200"
        ),
    )
    page = ComputeAcceleratorTypesPage(
        scopes=(
            ComputeAcceleratorTypesScope(
                "zones/us-central1-a",
                (
                    valid,
                    compute_v1.AcceleratorType(
                        name="provider-next-x",
                        zone="us-central1-a",
                        self_link=(
                            "https://www.googleapis.com/compute/v1/projects/other/"
                            "zones/us-central1-a/acceleratorTypes/provider-next-x"
                        ),
                    ),
                    compute_v1.AcceleratorType(
                        name="provider-next-y",
                        zone="us-east1-b",
                        self_link=(
                            "https://www.googleapis.com/compute/v1/projects/"
                            "public-schema-project/zones/us-east1-b/"
                            "acceleratorTypes/provider-next-y"
                        ),
                    ),
                    compute_v1.AcceleratorType(name="identity-missing"),
                ),
            ),
        ),
        next_page_token="",
    )

    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert [item.name for item in result.values] == ["nvidia-b200"]
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert _diagnostic_codes(result) == [
        "provider-schema-invalid",
        "provider-schema-invalid",
        "provider-schema-invalid",
        "compute-accelerator-catalog-scope-invalid",
    ]
    assert not result.complete


@pytest.mark.parametrize(
    ("warning", "state"),
    [
        ("NO_RESULTS_ON_PAGE", LocationCoverageState.EMPTY),
        ("PROVIDER_NEW_WARNING", LocationCoverageState.FAILED),
    ],
)
def test_compute_reader_normalizes_page_warnings(
    warning: str,
    state: LocationCoverageState,
) -> None:
    """Compute page warnings distinguish authoritative empty from failed evidence."""
    page = ComputeMachineTypesPage((), "", warning_code=warning)

    result = asyncio.run(
        GoogleComputeMachineTypeReader(ComputePages((page,)), _policy()).read(
            ComputeMachineTypeReadRequest(_context())
        )
    )

    assert result.values == ()
    assert result.location_coverage[0].state is state
    assert result.complete is (state is LocationCoverageState.EMPTY)


def test_compute_reader_reports_provider_failure_without_provider_text() -> None:
    """Provider exceptions become safe failed coverage and no catalog values."""
    provider_text = "discarded-provider-detail"

    result = asyncio.run(
        GoogleComputeMachineTypeReader(
            ComputePages((RuntimeError(provider_text),)), _policy()
        ).read(ComputeMachineTypeReadRequest(_context()))
    )

    assert result.values == ()
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert _diagnostic_codes(result) == ["provider-read-failed"]
    assert provider_text not in repr(result)


def test_pre_cancelled_reads_do_not_dispatch_provider_calls() -> None:
    """Caller cancellation remains visible and stops both provider seams early."""
    cancellation = CancellationToken()
    cancellation.cancel()
    context = _context(cancellation=cancellation)
    compute = ComputePages()
    tpu = TpuPages()

    compute_result = asyncio.run(
        GoogleComputeMachineTypeReader(compute, _policy()).read(
            ComputeMachineTypeReadRequest(context)
        )
    )
    tpu_result = asyncio.run(
        GoogleTpuLocationReader(tpu, _policy()).read(TpuLocationReadRequest(context))
    )

    assert compute.calls == tpu.calls == 0
    assert _diagnostic_codes(compute_result) == ["provider-read-cancelled"]
    assert _diagnostic_codes(tpu_result) == ["provider-read-cancelled"]
    assert all(
        result.location_coverage[0].state is LocationCoverageState.FAILED
        for result in (compute_result, tpu_result)
    )


def test_tpu_location_reader_distinguishes_empty_from_unscanned_pages() -> None:
    """An empty final page is complete while a capped continuation is not scanned."""
    empty_result = asyncio.run(
        GoogleTpuLocationReader(
            TpuPages(locations=(TpuLocationsPage((), ""),)),
            _policy(),
            now=lambda: NOW,
        ).read(TpuLocationReadRequest(_context()))
    )
    capped_result = asyncio.run(
        GoogleTpuLocationReader(
            TpuPages(locations=(TpuLocationsPage((), "next-page"),)),
            _policy(),
            maximum_pages=1,
            now=lambda: NOW,
        ).read(TpuLocationReadRequest(_context()))
    )

    assert empty_result.location_coverage[0].state is LocationCoverageState.EMPTY
    assert empty_result.complete
    assert [item.state for item in capped_result.location_coverage] == [
        LocationCoverageState.EMPTY,
        LocationCoverageState.NOT_SCANNED,
    ]
    assert capped_result.read.coverage.page_cap_reached
    assert not capped_result.complete


def test_tpu_zone_readers_fail_coverage_for_malformed_items() -> None:
    """Malformed accelerator and runtime items cannot appear as empty evidence."""
    client = TpuPages(
        accelerators=(
            TpuAcceleratorTypesPage(
                (tpu_v2.AcceleratorType(name="", type_="v6e-8"),),
                "",
            ),
        ),
        runtimes=(
            TpuRuntimeVersionsPage(
                (tpu_v2.RuntimeVersion(name="runtime", version=""),),
                "",
            ),
        ),
    )
    context = _context()

    accelerator_result = asyncio.run(
        GoogleTpuAcceleratorTypeReader(client, _policy()).read(
            TpuAcceleratorTypeReadRequest(context, "us-central1-b")
        )
    )
    runtime_result = asyncio.run(
        GoogleTpuRuntimeVersionReader(client, _policy()).read(
            TpuRuntimeVersionReadRequest(context, "us-central1-b")
        )
    )

    for result in (accelerator_result, runtime_result):
        assert result.values == ()
        assert result.location_coverage[0].state is LocationCoverageState.FAILED
        assert _diagnostic_codes(result) == ["provider-schema-invalid"]
        assert not result.complete


def test_tpu_readers_reject_items_outside_the_requested_parent() -> None:
    """Provider DTO identity cannot be relabeled as the requested project or zone."""
    client = TpuPages(
        locations=(
            TpuLocationsPage(
                (
                    locations_pb2.Location(
                        name="projects/other/locations/us-east1-b",
                        location_id="us-east1-b",
                    ),
                ),
                "",
            ),
        ),
        accelerators=(
            TpuAcceleratorTypesPage(
                (
                    tpu_v2.AcceleratorType(
                        name=(
                            "projects/other/locations/us-east1-b/acceleratorTypes/v6e-8"
                        ),
                        type_="v6e-8",
                    ),
                    tpu_v2.AcceleratorType(
                        name=(
                            "projects/123456789/locations/us-central1-b/"
                            "acceleratorTypes/v4-8"
                        ),
                        type_="v6e-8",
                    ),
                ),
                "",
            ),
        ),
        runtimes=(
            TpuRuntimeVersionsPage(
                (
                    tpu_v2.RuntimeVersion(
                        name=(
                            "projects/other/locations/us-east1-b/"
                            "runtimeVersions/tpu-vm-base"
                        ),
                        version="tpu-vm-base",
                    ),
                    tpu_v2.RuntimeVersion(
                        name=(
                            "projects/123456789/locations/us-central1-b/"
                            "runtimeVersions/tpu-vm-v4-base"
                        ),
                        version="tpu-vm-base",
                    ),
                ),
                "",
            ),
        ),
    )
    context = _context()

    results = (
        asyncio.run(
            GoogleTpuLocationReader(client, _policy()).read(
                TpuLocationReadRequest(context)
            )
        ),
        asyncio.run(
            GoogleTpuAcceleratorTypeReader(client, _policy()).read(
                TpuAcceleratorTypeReadRequest(context, "us-central1-b")
            )
        ),
        asyncio.run(
            GoogleTpuRuntimeVersionReader(client, _policy()).read(
                TpuRuntimeVersionReadRequest(context, "us-central1-b")
            )
        ),
    )

    for index, result in enumerate(results):
        assert result.values == ()
        assert result.location_coverage[0].state is LocationCoverageState.FAILED
        assert _diagnostic_codes(result) == ["provider-schema-invalid"] * (
            1 if index == 0 else 2
        )
        assert not result.complete


def test_official_tpu_wrapper_builds_exact_requests_without_generated_retries() -> None:
    """Official TPU calls retain explicit parent, pagination, timeout, and no retry."""

    class Transport:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, object, object]] = []
            self.transport = Transport()

        async def list_locations(
            self, *, request: object, retry: object, timeout: object
        ) -> object:
            self.calls.append(("locations", request, retry, timeout))
            return SimpleNamespace(locations=(), next_page_token="locations-next")

        async def list_accelerator_types(
            self, *, request: object, retry: object, timeout: object
        ) -> object:
            self.calls.append(("accelerators", request, retry, timeout))
            page = SimpleNamespace(accelerator_types=(), next_page_token="types-next")
            return SimpleNamespace(pages=_async_pages(page))

        async def list_runtime_versions(
            self, *, request: object, retry: object, timeout: object
        ) -> object:
            self.calls.append(("runtimes", request, retry, timeout))
            page = SimpleNamespace(runtime_versions=(), next_page_token="runtime-next")
            return SimpleNamespace(pages=_async_pages(page))

    client = Client()
    wrapper = OfficialTpuCatalogPageClient(cast("tpu_v2.TpuAsyncClient", client))

    async def read_pages() -> tuple[
        TpuLocationsPage, TpuAcceleratorTypesPage, TpuRuntimeVersionsPage
    ]:
        return (
            await wrapper.locations(
                name="projects/123456789",
                page_size=17,
                page_token="locations-page",
                timeout_seconds=2.5,
            ),
            await wrapper.accelerator_types(
                parent="projects/123456789/locations/us-central1-b",
                page_size=18,
                page_token="types-page",
                timeout_seconds=3.5,
            ),
            await wrapper.runtime_versions(
                parent="projects/123456789/locations/us-central1-b",
                page_size=19,
                page_token="runtime-page",
                timeout_seconds=4.5,
            ),
        )

    pages = asyncio.run(read_pages())
    asyncio.run(wrapper.close())
    location_request = cast("locations_pb2.ListLocationsRequest", client.calls[0][1])
    accelerator_request = cast("tpu_v2.ListAcceleratorTypesRequest", client.calls[1][1])
    runtime_request = cast("tpu_v2.ListRuntimeVersionsRequest", client.calls[2][1])

    assert (location_request.name, location_request.page_size) == (
        "projects/123456789",
        17,
    )
    assert location_request.page_token == "locations-page"
    assert client.transport.closed
    assert (accelerator_request.parent, accelerator_request.page_size) == (
        "projects/123456789/locations/us-central1-b",
        18,
    )
    assert accelerator_request.page_token == "types-page"
    assert (runtime_request.parent, runtime_request.page_size) == (
        "projects/123456789/locations/us-central1-b",
        19,
    )
    assert runtime_request.page_token == "runtime-page"
    assert [(call[2], call[3]) for call in client.calls] == [
        (None, 2.5),
        (None, 3.5),
        (None, 4.5),
    ]
    assert [page.next_page_token for page in pages] == [
        "locations-next",
        "types-next",
        "runtime-next",
    ]


async def _async_pages(page: object) -> AsyncIterator[object]:
    yield page
