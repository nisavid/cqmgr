"""Read-only Compute and Cloud TPU accelerator-catalog adapter contracts."""

# ruff: noqa: D102, D107, S105, S106

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, cast

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
    _finalize_requested_coverage,
)
from cqmgr.adapters.google.read_policy import GoogleReadPolicy, schema_diagnostic
from cqmgr.adapters.google.tpu_catalog import (
    GoogleTpuAcceleratorTypeReader,
    GoogleTpuLocationReader,
    GoogleTpuRuntimeVersionReader,
    TpuAcceleratorTypesPage,
    TpuLocationsPage,
    TpuRuntimeVersionsPage,
)
from cqmgr.application.ports.catalog_reads import (
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
from cqmgr.domain.catalog import (
    CatalogEvidenceSource,
    CatalogLifecycle,
    CatalogLocationCoverage,
    LocationCoverageExpectation,
    LocationCoverageState,
)
from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject, CredentialKind
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

FIXTURES = Path(__file__).parents[1] / "fixtures" / "google"
NOW = datetime(2026, 7, 22, 8, tzinfo=UTC)
GPU_COUNT = 8
EXPECTED_CANCEL_RACE_CALLS = 2
OFFICIAL_PAGE_SIZE = 17


class RecordingBudget:
    """In-memory request budget used without provider access."""

    def __init__(self) -> None:
        self.requests: list[BudgetRequest] = []

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return BudgetGrant(deadline - 1, request)


class NoJitter:
    """Deterministic read retry seam."""

    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        assert delay >= 0
        assert attempt >= 0
        assert identity
        return 0.0


class FakeComputePages:
    """Scripted Compute page client."""

    def __init__(
        self,
        pages: Sequence[ComputeMachineTypesPage | BaseException],
        *,
        delayed_zones: frozenset[str] = frozenset(),
        delay_aggregated: bool = False,
    ) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, int, str, bool, float]] = []
        self.zone_calls: list[tuple[str, str, int, str, float]] = []
        self.delayed_zones = delayed_zones
        self.delay_aggregated = delay_aggregated

    async def machine_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        self.calls.append(
            (project, max_results, page_token, return_partial_success, timeout_seconds)
        )
        if self.delay_aggregated:
            await asyncio.Event().wait()
            raise AssertionError
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page

    async def machine_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeMachineTypesPage:
        self.zone_calls.append(
            (project, zone, max_results, page_token, timeout_seconds)
        )
        if zone in self.delayed_zones:
            await asyncio.Event().wait()
            raise AssertionError
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page


class FakeComputeAcceleratorPages:
    """Scripted Compute accelerator-type page client."""

    def __init__(
        self,
        pages: Sequence[ComputeAcceleratorTypesPage | BaseException],
        *,
        delayed_zones: frozenset[str] = frozenset(),
        delay_aggregated: bool = False,
    ) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, int, str, bool, float]] = []
        self.zone_calls: list[tuple[str, str, int, str, float]] = []
        self.delayed_zones = delayed_zones
        self.delay_aggregated = delay_aggregated

    async def accelerator_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        self.calls.append(
            (
                project,
                max_results,
                page_token,
                return_partial_success,
                timeout_seconds,
            )
        )
        if self.delay_aggregated:
            await asyncio.Event().wait()
            raise AssertionError
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page

    async def accelerator_types_for_zone(
        self,
        *,
        project: str,
        zone: str,
        max_results: int,
        page_token: str,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        self.zone_calls.append(
            (project, zone, max_results, page_token, timeout_seconds)
        )
        if zone in self.delayed_zones:
            await asyncio.Event().wait()
            raise AssertionError
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page


class FakeTpuPages:
    """Scripted TPU location, accelerator, and runtime page client."""

    def __init__(
        self,
        *,
        locations: Sequence[TpuLocationsPage | BaseException] = (),
        accelerators: Sequence[TpuAcceleratorTypesPage | BaseException] = (),
        runtimes: Sequence[TpuRuntimeVersionsPage | BaseException] = (),
    ) -> None:
        self.location_pages = list(locations)
        self.accelerator_pages = list(accelerators)
        self.runtime_pages = list(runtimes)
        self.calls: list[tuple[str, str, str]] = []

    async def locations(self, **kwargs: object) -> TpuLocationsPage:
        self.calls.append(
            (
                "locations",
                cast("str", kwargs["name"]),
                cast("str", kwargs["page_token"]),
            )
        )
        return cast("TpuLocationsPage", _take(self.location_pages))

    async def accelerator_types(self, **kwargs: object) -> TpuAcceleratorTypesPage:
        self.calls.append(
            (
                "accelerators",
                cast("str", kwargs["parent"]),
                cast("str", kwargs["page_token"]),
            )
        )
        return cast("TpuAcceleratorTypesPage", _take(self.accelerator_pages))

    async def runtime_versions(self, **kwargs: object) -> TpuRuntimeVersionsPage:
        self.calls.append(
            (
                "runtimes",
                cast("str", kwargs["parent"]),
                cast("str", kwargs["page_token"]),
            )
        )
        return cast("TpuRuntimeVersionsPage", _take(self.runtime_pages))


def _take[ValueT](values: list[ValueT | BaseException]) -> ValueT:
    value = values.pop(0)
    if isinstance(value, BaseException):
        raise value
    return value


def _json(name: str) -> Mapping[str, object]:
    return cast("Mapping[str, object]", json.loads((FIXTURES / name).read_text()))


def _context(
    *,
    cancellation: CancellationToken | None = None,
    deadline: float | None = None,
) -> ProviderReadContext:
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
        deadline=deadline if deadline is not None else time.monotonic() + 30,
        cancellation=cancellation or CancellationToken(),
    )


def _policy() -> GoogleReadPolicy:
    return GoogleReadPolicy(RecordingBudget(), NoJitter(), maximum_attempts=1)


def _compute_pages() -> list[ComputeMachineTypesPage]:
    pages = cast(
        "list[Mapping[str, object]]", _json("compute-machine-types-pages.json")["pages"]
    )
    result = []
    for raw_page in pages:
        scopes = []
        for scope, raw_scope in cast(
            "Mapping[str, Mapping[str, object]]", raw_page["items"]
        ).items():
            machines = []
            for raw in cast(
                "list[Mapping[str, object]]", raw_scope.get("machineTypes", [])
            ):
                accelerators = [
                    compute_v1.Accelerators(
                        guest_accelerator_type=item["guestAcceleratorType"],
                        guest_accelerator_count=item["guestAcceleratorCount"],
                    )
                    for item in cast(
                        "list[Mapping[str, object]]", raw.get("accelerators", [])
                    )
                ]
                deprecated = raw.get("deprecated")
                machines.append(
                    compute_v1.MachineType(
                        name=raw["name"],
                        zone=raw.get("zone"),
                        self_link=raw.get("selfLink"),
                        accelerators=accelerators,
                        deprecated=(
                            compute_v1.DeprecationStatus(
                                state=cast("Mapping[str, object]", deprecated)["state"]
                            )
                            if deprecated is not None
                            else None
                        ),
                    )
                )
            warning = cast("Mapping[str, object] | None", raw_scope.get("warning"))
            scopes.append(
                ComputeMachineTypesScope(
                    scope=scope,
                    machine_types=tuple(machines),
                    warning_code=cast(
                        "str | None", warning.get("code") if warning else None
                    ),
                )
            )
        result.append(
            ComputeMachineTypesPage(
                scopes=tuple(scopes),
                next_page_token=cast("str", raw_page["nextPageToken"]),
                unreachable_scopes=tuple(cast("list[str]", raw_page["unreachables"])),
                warning_code=None,
            )
        )
    return result


def _compute_accelerator_pages() -> list[ComputeAcceleratorTypesPage]:
    pages = cast(
        "list[Mapping[str, object]]",
        _json("compute-accelerator-types-pages.json")["pages"],
    )
    result = []
    for raw_page in pages:
        scopes = []
        for scope, raw_scope in cast(
            "Mapping[str, Mapping[str, object]]", raw_page["items"]
        ).items():
            accelerator_types = []
            for raw in cast(
                "list[Mapping[str, object]]",
                raw_scope.get("acceleratorTypes", []),
            ):
                deprecated = raw.get("deprecated")
                accelerator_types.append(
                    compute_v1.AcceleratorType(
                        name=raw["name"],
                        zone=raw.get("zone"),
                        self_link=raw.get("selfLink"),
                        deprecated=(
                            compute_v1.DeprecationStatus(
                                state=cast("Mapping[str, object]", deprecated)["state"]
                            )
                            if deprecated is not None
                            else None
                        ),
                    )
                )
            warning = cast("Mapping[str, object] | None", raw_scope.get("warning"))
            scopes.append(
                ComputeAcceleratorTypesScope(
                    scope=scope,
                    accelerator_types=tuple(accelerator_types),
                    warning_code=cast(
                        "str | None", warning.get("code") if warning else None
                    ),
                )
            )
        result.append(
            ComputeAcceleratorTypesPage(
                scopes=tuple(scopes),
                next_page_token=cast("str", raw_page["nextPageToken"]),
                unreachable_scopes=tuple(cast("list[str]", raw_page["unreachables"])),
                warning_code=None,
            )
        )
    return result


def _tpu_pages() -> tuple[
    list[TpuLocationsPage], list[TpuAcceleratorTypesPage], list[TpuRuntimeVersionsPage]
]:
    fixture = _json("tpu-catalog-pages.json")
    location_pages = [
        TpuLocationsPage(
            items=tuple(
                locations_pb2.Location(
                    name=cast("str", item["name"]),
                    location_id=cast("str", item["locationId"]),
                    display_name=cast("str", item["displayName"]),
                )
                for item in cast("list[Mapping[str, object]]", page["locations"])
            ),
            next_page_token=cast("str", page["nextPageToken"]),
        )
        for page in cast("list[Mapping[str, object]]", fixture["locationPages"])
    ]
    accelerator_raw = cast(
        "Mapping[str, list[Mapping[str, object]]]", fixture["acceleratorPages"]
    )["us-central1-b"]
    accelerator_pages = [
        TpuAcceleratorTypesPage(
            items=tuple(
                tpu_v2.AcceleratorType(
                    name=item["name"],
                    type_=item["type"],
                    accelerator_configs=[
                        tpu_v2.AcceleratorConfig(
                            type_=config["type"], topology=config["topology"]
                        )
                        for config in cast(
                            "list[Mapping[str, object]]", item["acceleratorConfigs"]
                        )
                    ],
                )
                for item in cast("list[Mapping[str, object]]", page["acceleratorTypes"])
            ),
            next_page_token=cast("str", page["nextPageToken"]),
        )
        for page in accelerator_raw
    ]
    runtime_raw = cast(
        "Mapping[str, list[Mapping[str, object]]]", fixture["runtimePages"]
    )["us-central1-b"]
    runtime_pages = [
        TpuRuntimeVersionsPage(
            items=tuple(
                tpu_v2.RuntimeVersion(name=item["name"], version=item["version"])
                for item in cast("list[Mapping[str, object]]", page["runtimeVersions"])
            ),
            next_page_token=cast("str", page["nextPageToken"]),
        )
        for page in runtime_raw
    ]
    return location_pages, accelerator_pages, runtime_pages


def test_compute_accelerator_reader_preserves_identity_lifecycle_and_coverage() -> None:
    """Compute accelerator discovery retains provider truth and partial failures."""
    client = FakeComputeAcceleratorPages(_compute_accelerator_pages())
    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            client, _policy(), page_size=1, now=lambda: NOW
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert [(item.name, item.zone) for item in result.values] == [
        ("nvidia-b200", "us-central1-a"),
        ("provider-next-x", "us-central1-b"),
    ]
    assert result.values[0].lifecycle is not None
    assert result.values[0].lifecycle.known is CatalogLifecycle.DEPRECATED
    assert result.values[1].lifecycle is not None
    assert result.values[1].lifecycle.raw == "PROVIDER_FUTURE_STATE"
    assert result.values[1].lifecycle.known is None
    assert [item.state for item in result.location_coverage] == [
        LocationCoverageState.SUCCESS,
        LocationCoverageState.SUCCESS,
        LocationCoverageState.EMPTY,
        LocationCoverageState.FAILED,
    ]
    assert {item.source for item in result.location_coverage} == {
        CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES
    }
    assert "provider-schema-invalid" not in {
        item.code.value for item in result.read.diagnostics
    }
    assert not result.complete
    assert all(call[0] == "public-schema-project" for call in client.calls)
    assert all(call[3] is True for call in client.calls)


def test_compute_accelerator_page_cap_retains_discovery_as_incomplete() -> None:
    """Unscanned accelerator pages retain values without an exhaustive claim."""
    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            FakeComputeAcceleratorPages(_compute_accelerator_pages()),
            _policy(),
            maximum_pages=1,
            now=lambda: NOW,
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert [item.name for item in result.values] == ["nvidia-b200"]
    assert result.read.coverage.page_cap_reached
    assert result.location_coverage[-1].state is LocationCoverageState.NOT_SCANNED
    assert not result.complete


def test_compute_reader_preserves_scopes_lifecycle_accelerators_and_warnings() -> None:
    """Compute normalization retains exact scoped evidence and partial failures."""
    client = FakeComputePages(_compute_pages())
    result = asyncio.run(
        GoogleComputeMachineTypeReader(
            client, _policy(), page_size=1, now=lambda: NOW
        ).read(ComputeMachineTypeReadRequest(_context()))
    )

    assert [(item.name, item.zone) for item in result.values] == [
        ("a3-highgpu-8g", "us-central1-a"),
        ("ct6e-standard-4t", "us-central1-b"),
    ]
    assert result.values[0].lifecycle is not None
    assert result.values[0].lifecycle.known is CatalogLifecycle.DEPRECATED
    assert result.values[1].lifecycle is None
    assert result.values[0].guest_accelerators[0].count == GPU_COUNT
    assert [item.state for item in result.location_coverage] == [
        LocationCoverageState.SUCCESS,
        LocationCoverageState.SUCCESS,
        LocationCoverageState.EMPTY,
        LocationCoverageState.FAILED,
    ]
    assert "provider-schema-invalid" not in {
        item.code.value for item in result.read.diagnostics
    }
    assert not result.complete
    assert all(call[0] == "public-schema-project" for call in client.calls)
    assert all(call[3] is True for call in client.calls)


def test_compute_page_cap_retains_values_and_marks_read_incomplete() -> None:
    """A bounded page cap cannot silently report complete catalog evidence."""
    result = asyncio.run(
        GoogleComputeMachineTypeReader(
            FakeComputePages(_compute_pages()),
            _policy(),
            maximum_pages=1,
            now=lambda: NOW,
        ).read(ComputeMachineTypeReadRequest(_context()))
    )

    assert [item.name for item in result.values] == ["a3-highgpu-8g"]
    assert result.read.coverage.page_cap_reached
    assert not result.complete


def test_compute_exact_zone_readers_never_request_unrelated_zones() -> None:
    """An exact candidate dispatches only per-zone reads for both Compute sources."""
    machine_page = replace(_compute_pages()[0], next_page_token="")
    accelerator_page = replace(
        _compute_accelerator_pages()[0],
        next_page_token="",
    )
    delayed_zone = "us-east1-b"
    machine_client = FakeComputePages(
        (machine_page,),
        delayed_zones=frozenset((delayed_zone,)),
        delay_aggregated=True,
    )
    accelerator_client = FakeComputeAcceleratorPages(
        (accelerator_page,),
        delayed_zones=frozenset((delayed_zone,)),
        delay_aggregated=True,
    )
    request_zones = ("us-central1-a",)

    machine_result = asyncio.run(
        asyncio.wait_for(
            GoogleComputeMachineTypeReader(
                machine_client,
                _policy(),
                now=lambda: NOW,
            ).read(ComputeMachineTypeReadRequest(_context(), request_zones)),
            timeout=0.25,
        )
    )
    accelerator_result = asyncio.run(
        asyncio.wait_for(
            GoogleComputeAcceleratorTypeReader(
                accelerator_client,
                _policy(),
                now=lambda: NOW,
            ).read(ComputeAcceleratorTypeReadRequest(_context(), request_zones)),
            timeout=0.25,
        )
    )

    assert [item.name for item in machine_result.values] == ["a3-highgpu-8g"]
    assert [item.name for item in accelerator_result.values] == ["nvidia-b200"]
    assert machine_client.calls == []
    assert accelerator_client.calls == []
    assert [call[1] for call in machine_client.zone_calls] == ["us-central1-a"]
    assert [call[1] for call in accelerator_client.zone_calls] == ["us-central1-a"]
    assert delayed_zone not in {
        call[1] for call in (*machine_client.zone_calls, *accelerator_client.zone_calls)
    }
    assert all(
        item.expectation is LocationCoverageExpectation.REQUESTED
        for item in (
            *machine_result.location_coverage,
            *accelerator_result.location_coverage,
        )
    )


def test_compute_exact_zone_machine_pages_yield_one_success_coverage() -> None:
    """One exhausted zone chain retains every machine under one coverage record."""
    zone = "us-central1-a"

    def page(name: str, next_page_token: str) -> ComputeMachineTypesPage:
        return ComputeMachineTypesPage(
            (
                ComputeMachineTypesScope(
                    f"zones/{zone}",
                    (
                        compute_v1.MachineType(
                            name=name,
                            zone=zone,
                            self_link=(
                                "https://www.googleapis.com/compute/v1/projects/"
                                f"public-schema-project/zones/{zone}/"
                                f"machineTypes/{name}"
                            ),
                        ),
                    ),
                ),
            ),
            next_page_token,
        )

    result = asyncio.run(
        GoogleComputeMachineTypeReader(
            FakeComputePages(
                (
                    page("a3-highgpu-8g", "public-next-page"),
                    page("g2-standard-4", ""),
                )
            ),
            _policy(),
            now=lambda: NOW,
        ).read(ComputeMachineTypeReadRequest(_context(), (zone,)))
    )

    assert [item.name for item in result.values] == [
        "a3-highgpu-8g",
        "g2-standard-4",
    ]
    assert [
        (
            item.source,
            item.location,
            item.expectation,
            item.state,
        )
        for item in result.location_coverage
    ] == [
        (
            CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
            zone,
            LocationCoverageExpectation.REQUESTED,
            LocationCoverageState.SUCCESS,
        )
    ]


def test_compute_exact_zone_accelerator_pages_yield_one_success_coverage() -> None:
    """One exhausted zone chain retains every accelerator under one coverage record."""
    zone = "us-central1-a"

    def page(name: str, next_page_token: str) -> ComputeAcceleratorTypesPage:
        return ComputeAcceleratorTypesPage(
            (
                ComputeAcceleratorTypesScope(
                    f"zones/{zone}",
                    (
                        compute_v1.AcceleratorType(
                            name=name,
                            zone=zone,
                            self_link=(
                                "https://www.googleapis.com/compute/v1/projects/"
                                f"public-schema-project/zones/{zone}/"
                                f"acceleratorTypes/{name}"
                            ),
                        ),
                    ),
                ),
            ),
            next_page_token,
        )

    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            FakeComputeAcceleratorPages(
                (
                    page("nvidia-b200", "public-next-page"),
                    page("nvidia-h100-80gb", ""),
                )
            ),
            _policy(),
            now=lambda: NOW,
        ).read(ComputeAcceleratorTypeReadRequest(_context(), (zone,)))
    )

    assert [item.name for item in result.values] == [
        "nvidia-b200",
        "nvidia-h100-80gb",
    ]
    assert [
        (
            item.source,
            item.location,
            item.expectation,
            item.state,
        )
        for item in result.location_coverage
    ] == [
        (
            CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
            zone,
            LocationCoverageExpectation.REQUESTED,
            LocationCoverageState.SUCCESS,
        )
    ]


def test_compute_zonal_subcalls_share_remaining_deadline_and_diagnostics() -> None:
    """Each zonal page gets remaining time and an expired zone keeps source/phase."""

    def page(zone: str, name: str) -> ComputeMachineTypesPage:
        return ComputeMachineTypesPage(
            (
                ComputeMachineTypesScope(
                    f"zones/{zone}",
                    (
                        compute_v1.MachineType(
                            name=name,
                            zone=zone,
                            self_link=(
                                "https://www.googleapis.com/compute/v1/projects/"
                                f"public-schema-project/zones/{zone}/"
                                f"machineTypes/{name}"
                            ),
                        ),
                    ),
                ),
            ),
            "",
        )

    client = FakeComputePages(
        (
            page("us-central1-a", "a3-highgpu-8g"),
            page("us-east1-b", "g2-standard-4"),
        )
    )
    ticks = iter((0.0, 2.0, 4.0, 6.0, 10.0))
    policy = GoogleReadPolicy(
        RecordingBudget(),
        NoJitter(),
        timeout_seconds=20.0,
        maximum_attempts=1,
        monotonic=lambda: next(ticks),
    )

    result = asyncio.run(
        GoogleComputeMachineTypeReader(client, policy, now=lambda: NOW).read(
            ComputeMachineTypeReadRequest(
                _context(deadline=10.0),
                ("us-central1-a", "us-east1-b", "us-west1-c"),
            )
        )
    )

    assert [call[1] for call in client.zone_calls] == [
        "us-central1-a",
        "us-east1-b",
    ]
    assert [call[-1] for call in client.zone_calls] == [8.0, 4.0]
    deadline = next(
        item
        for item in result.read.diagnostics
        if item.code.value == "provider-read-deadline-exceeded"
    )
    assert deadline.source.value == "compute"
    assert deadline.phase.value == "compute-machine-types-read"
    assert result.location_coverage[-1].location == "us-west1-c"
    assert result.location_coverage[-1].state is LocationCoverageState.FAILED


def test_tpu_readers_keep_sources_and_zone_coverage_independent() -> None:
    """Locations, accelerators, and runtimes retain separate source coverage."""
    locations, accelerators, runtimes = _tpu_pages()
    client = FakeTpuPages(
        locations=locations, accelerators=accelerators, runtimes=runtimes
    )
    location_result = asyncio.run(
        GoogleTpuLocationReader(client, _policy(), page_size=1, now=lambda: NOW).read(
            TpuLocationReadRequest(_context())
        )
    )
    accelerator_result = asyncio.run(
        GoogleTpuAcceleratorTypeReader(client, _policy(), now=lambda: NOW).read(
            TpuAcceleratorTypeReadRequest(_context(), "us-central1-b")
        )
    )
    runtime_result = asyncio.run(
        GoogleTpuRuntimeVersionReader(client, _policy(), now=lambda: NOW).read(
            TpuRuntimeVersionReadRequest(_context(), "us-central1-b")
        )
    )

    assert [item.location_id for item in location_result.values] == [
        "us-central1-b",
        "us-east5-a",
    ]
    assert accelerator_result.values[0].configurations[0].version == "V6E"
    assert accelerator_result.values[0].configurations[0].topology == "2x4"
    assert runtime_result.values[0].version == "tpu-vm-base"
    assert {item.source for item in accelerator_result.location_coverage} == {
        CatalogEvidenceSource.TPU_ACCELERATOR_TYPES
    }
    assert {item.source for item in runtime_result.location_coverage} == {
        CatalogEvidenceSource.TPU_RUNTIME_VERSIONS
    }
    assert all(
        result.complete
        for result in (location_result, accelerator_result, runtime_result)
    )


def test_failed_tpu_zone_source_is_incomplete_not_empty() -> None:
    """One failed TPU source remains distinct from an authoritative empty read."""
    _, _, runtimes = _tpu_pages()
    client = FakeTpuPages(
        accelerators=[RuntimeError("discarded provider text")], runtimes=runtimes
    )
    accelerator_result = asyncio.run(
        GoogleTpuAcceleratorTypeReader(client, _policy(), now=lambda: NOW).read(
            TpuAcceleratorTypeReadRequest(_context(), "us-central1-b")
        )
    )
    runtime_result = asyncio.run(
        GoogleTpuRuntimeVersionReader(client, _policy(), now=lambda: NOW).read(
            TpuRuntimeVersionReadRequest(_context(), "us-central1-b")
        )
    )

    assert accelerator_result.values == ()
    assert accelerator_result.location_coverage[0].state is LocationCoverageState.FAILED
    assert not accelerator_result.complete
    assert runtime_result.complete


def test_capped_tpu_zone_source_is_not_scanned() -> None:
    """A remaining TPU provider page is classified as required work not scanned."""
    _, accelerators, _ = _tpu_pages()
    capped_page = replace(accelerators[0], next_page_token="public-next-page")
    result = asyncio.run(
        GoogleTpuAcceleratorTypeReader(
            FakeTpuPages(accelerators=[capped_page]),
            _policy(),
            maximum_pages=1,
            now=lambda: NOW,
        ).read(TpuAcceleratorTypeReadRequest(_context(), "us-central1-b"))
    )

    assert result.read.coverage.page_cap_reached
    assert result.location_coverage[0].state is LocationCoverageState.NOT_SCANNED
    assert not result.complete


@pytest.mark.parametrize("scope", ["zones/us central1-a", "zones/us-central1"])
def test_invalid_compute_scope_is_explicit_failed_coverage(scope: str) -> None:
    """A malformed Compute scope cannot escape as authoritative empty evidence."""
    page = ComputeMachineTypesPage(
        scopes=(ComputeMachineTypesScope(scope, ()),),
        next_page_token="",
    )
    result = asyncio.run(
        GoogleComputeMachineTypeReader(
            FakeComputePages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeMachineTypeReadRequest(_context()))
    )

    assert result.values == ()
    assert result.location_coverage[0].location == "global"
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert not result.complete


@pytest.mark.parametrize(
    ("zone", "self_link"),
    [
        (
            "https://compute.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a",
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a/"
            "machineTypes/a3-highgpu-8g",
        ),
        (
            "https://www.googleapis.com/compute/v1/projects/other/zones/us-central1-a",
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a/"
            "machineTypes/a3-highgpu-8g",
        ),
        (
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-east1-b",
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a/"
            "machineTypes/a3-highgpu-8g",
        ),
        (
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/regions/us-central1",
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a/"
            "machineTypes/a3-highgpu-8g",
        ),
        (
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a",
            "https://compute.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a/"
            "machineTypes/a3-highgpu-8g",
        ),
        (
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a",
            "https://www.googleapis.com/compute/v1/projects/"
            "public-schema-project/zones/us-central1-a/"
            "acceleratorTypes/a3-highgpu-8g",
        ),
    ],
)
def test_compute_reader_rejects_nonmatching_full_resource_identities(
    zone: str,
    self_link: str,
) -> None:
    """Full identities must match the API, project, zone, collection, and name."""
    page = ComputeMachineTypesPage(
        scopes=(
            ComputeMachineTypesScope(
                "zones/us-central1-a",
                (
                    compute_v1.MachineType(
                        name="a3-highgpu-8g",
                        zone=zone,
                        self_link=self_link,
                    ),
                ),
            ),
        ),
        next_page_token="",
    )

    result = asyncio.run(
        GoogleComputeMachineTypeReader(
            FakeComputePages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeMachineTypeReadRequest(_context()))
    )

    assert result.values == ()
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert [item.code.value for item in result.read.diagnostics] == [
        "provider-schema-invalid",
        "compute-catalog-scope-invalid",
    ]


def test_invalid_tpu_location_is_explicit_failed_coverage() -> None:
    """An untrusted TPU location ID leaves one visible source-coverage failure."""
    page = TpuLocationsPage(
        (locations_pb2.Location(name="projects/123/locations/x", location_id="X"),),
        "",
    )
    result = asyncio.run(
        GoogleTpuLocationReader(
            FakeTpuPages(locations=(page,)), _policy(), now=lambda: NOW
        ).read(TpuLocationReadRequest(_context()))
    )

    assert result.values == ()
    assert result.location_coverage[0].location == "global"
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert not result.complete


@pytest.mark.parametrize(
    "zone",
    [
        "",
        "US-CENTRAL1-B",
        "locations/us-central1-b",
        "us central1-b",
        "us_central1-b",
        "-us-central1-b",
        "us-central1-b-",
    ],
)
def test_tpu_requests_reject_noncanonical_zone(zone: str) -> None:
    """Per-zone reads reject ambiguous or noncanonical location inputs."""
    with pytest.raises(ValueError, match="zone"):
        TpuAcceleratorTypeReadRequest(_context(), zone)


@pytest.mark.parametrize(
    "zones",
    [
        (),
        ("us-central1",),
        ("US-CENTRAL1-A",),
        ("us-central1-a", "us-central1-a"),
    ],
)
def test_compute_requests_reject_invalid_explicit_zones(
    zones: tuple[str, ...],
) -> None:
    """Explicit Compute catalog scope requires unique canonical zones."""
    with pytest.raises(ValueError, match="zones"):
        ComputeMachineTypeReadRequest(_context(), zones)
    with pytest.raises(ValueError, match="zones"):
        ComputeAcceleratorTypeReadRequest(_context(), zones)


@pytest.mark.parametrize(
    "request_type",
    [ComputeMachineTypeReadRequest, ComputeAcceleratorTypeReadRequest],
)
def test_compute_requests_reject_an_empty_explicit_zone_set(
    request_type: type[
        ComputeMachineTypeReadRequest | ComputeAcceleratorTypeReadRequest
    ],
) -> None:
    """An empty exact-zone selection is distinct from malformed zone identities."""
    with pytest.raises(ValueError, match="zones must contain at least one location"):
        request_type(_context(), ())


def test_compute_requests_reject_mutable_explicit_zones() -> None:
    """A mutable sequence cannot cross the immutable catalog request seam."""
    with pytest.raises(TypeError, match="zones"):
        ComputeMachineTypeReadRequest(
            _context(),
            cast("tuple[str, ...]", ["us-central1-a"]),
        )


def test_requested_coverage_preserves_unsupported_precedence_and_reason() -> None:
    """Unsupported exact-zone evidence remains complete and keeps its reason."""
    zone = "us-central1-a"
    diagnostic = schema_diagnostic("compute-machine-types-read", "compute")

    result = _finalize_requested_coverage(
        [
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                zone,
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.EMPTY,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                zone,
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.UNSUPPORTED,
                (diagnostic,),
            ),
        ]
    )

    assert result == (
        CatalogLocationCoverage(
            CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
            zone,
            LocationCoverageExpectation.REQUESTED,
            LocationCoverageState.UNSUPPORTED,
            (diagnostic,),
        ),
    )


def test_official_compute_wrapper_uses_partial_success_without_retry() -> None:
    """The sync wrapper requests partial success and disables generated retry."""
    page = compute_v1.MachineTypeAggregatedList(
        next_page_token="public-next",
        warning=compute_v1.Warning(code="NO_RESULTS_ON_PAGE"),
        items={
            "zones/us-central1-a": compute_v1.MachineTypesScopedList(
                warning=compute_v1.Warning(code="NO_RESULTS_ON_PAGE")
            )
        },
    )

    class Pager:
        pages = iter((page,))

    class Client:
        def __init__(self) -> None:
            self.call: tuple[object, object, object] | None = None

        def aggregated_list(
            self, *, request: object, retry: object, timeout: object
        ) -> Pager:
            self.call = (request, retry, timeout)
            return Pager()

    client = Client()
    result = asyncio.run(
        OfficialComputeMachineTypesPageClient(
            cast("compute_v1.MachineTypesClient", client)
        ).machine_types(
            project="public-project",
            max_results=17,
            page_token="public-page",
            return_partial_success=True,
            timeout_seconds=2.5,
        )
    )

    request = cast(
        "compute_v1.AggregatedListMachineTypesRequest",
        cast("tuple[object, object, object]", client.call)[0],
    )
    assert request.return_partial_success
    assert not request.filter
    assert request.page_token == "public-page"
    assert cast("tuple[object, object, object]", client.call)[1:] == (None, 2.5)
    assert result.next_page_token == "public-next"
    assert result.warning_code == "NO_RESULTS_ON_PAGE"
    assert result.scopes[0].warning_code == "NO_RESULTS_ON_PAGE"


def test_official_compute_accelerator_wrapper_uses_aggregated_partial_success() -> None:
    """The generated accelerator client receives one exact bounded read request."""
    page = compute_v1.AcceleratorTypeAggregatedList(
        next_page_token="public-next",
        warning=compute_v1.Warning(code="NO_RESULTS_ON_PAGE"),
        items={
            "zones/us-central1-a": compute_v1.AcceleratorTypesScopedList(
                accelerator_types=(compute_v1.AcceleratorType(name="nvidia-b200"),)
            )
        },
    )

    class Pager:
        pages = iter((page,))

    class Client:
        def __init__(self) -> None:
            self.call: tuple[object, object, object] | None = None

        def aggregated_list(
            self, *, request: object, retry: object, timeout: object
        ) -> Pager:
            self.call = (request, retry, timeout)
            return Pager()

    client = Client()
    result = asyncio.run(
        OfficialComputeAcceleratorTypesPageClient(
            cast("compute_v1.AcceleratorTypesClient", client)
        ).accelerator_types(
            project="public-project",
            max_results=OFFICIAL_PAGE_SIZE,
            page_token="public-page",
            return_partial_success=True,
            timeout_seconds=2.5,
        )
    )

    request = cast(
        "compute_v1.AggregatedListAcceleratorTypesRequest",
        cast("tuple[object, object, object]", client.call)[0],
    )
    assert request.project == "public-project"
    assert request.max_results == OFFICIAL_PAGE_SIZE
    assert request.page_token == "public-page"
    assert request.return_partial_success
    assert cast("tuple[object, object, object]", client.call)[1:] == (None, 2.5)
    assert result.next_page_token == "public-next"
    assert result.warning_code == "NO_RESULTS_ON_PAGE"
    assert result.scopes[0].accelerator_types[0].name == "nvidia-b200"


def test_official_compute_machine_wrapper_uses_exact_zonal_list() -> None:
    """The generated machine client receives the exact requested zone."""
    page = compute_v1.MachineTypeList(
        items=(compute_v1.MachineType(name="a3-highgpu-8g"),),
        next_page_token="public-next",
    )

    class Pager:
        pages = iter((page,))

    class Client:
        def __init__(self) -> None:
            self.call: tuple[object, object, object] | None = None

        def list(self, *, request: object, retry: object, timeout: object) -> Pager:
            self.call = (request, retry, timeout)
            return Pager()

    client = Client()
    result = asyncio.run(
        OfficialComputeMachineTypesPageClient(
            cast("compute_v1.MachineTypesClient", client)
        ).machine_types_for_zone(
            project="public-project",
            zone="us-central1-a",
            max_results=OFFICIAL_PAGE_SIZE,
            page_token="public-page",
            timeout_seconds=2.5,
        )
    )

    request = cast(
        "compute_v1.ListMachineTypesRequest",
        cast("tuple[object, object, object]", client.call)[0],
    )
    assert request.project == "public-project"
    assert request.zone == "us-central1-a"
    assert request.max_results == OFFICIAL_PAGE_SIZE
    assert request.page_token == "public-page"
    assert cast("tuple[object, object, object]", client.call)[1:] == (None, 2.5)
    assert result.scopes[0].scope == "zones/us-central1-a"
    assert result.scopes[0].machine_types[0].name == "a3-highgpu-8g"
    assert result.next_page_token == "public-next"


def test_official_compute_accelerator_wrapper_uses_exact_zonal_list() -> None:
    """The generated accelerator client receives the exact requested zone."""
    page = compute_v1.AcceleratorTypeList(
        items=(compute_v1.AcceleratorType(name="nvidia-b200"),),
        next_page_token="public-next",
    )

    class Pager:
        pages = iter((page,))

    class Client:
        def __init__(self) -> None:
            self.call: tuple[object, object, object] | None = None

        def list(self, *, request: object, retry: object, timeout: object) -> Pager:
            self.call = (request, retry, timeout)
            return Pager()

    client = Client()
    result = asyncio.run(
        OfficialComputeAcceleratorTypesPageClient(
            cast("compute_v1.AcceleratorTypesClient", client)
        ).accelerator_types_for_zone(
            project="public-project",
            zone="us-central1-a",
            max_results=OFFICIAL_PAGE_SIZE,
            page_token="public-page",
            timeout_seconds=2.5,
        )
    )

    request = cast(
        "compute_v1.ListAcceleratorTypesRequest",
        cast("tuple[object, object, object]", client.call)[0],
    )
    assert request.project == "public-project"
    assert request.zone == "us-central1-a"
    assert request.max_results == OFFICIAL_PAGE_SIZE
    assert request.page_token == "public-page"
    assert cast("tuple[object, object, object]", client.call)[1:] == (None, 2.5)
    assert result.scopes[0].scope == "zones/us-central1-a"
    assert result.scopes[0].accelerator_types[0].name == "nvidia-b200"
    assert result.next_page_token == "public-next"


def test_official_compute_wrapper_bounds_sync_dispatch_concurrency() -> None:
    """The sync-only Compute client never exceeds its explicit worker bound."""

    class Pager:
        def __init__(self) -> None:
            self.pages = iter((compute_v1.MachineTypeAggregatedList(),))

    class Client:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.lock = Lock()

        def aggregated_list(self, **kwargs: object) -> Pager:
            del kwargs
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return Pager()

    client = Client()
    wrapper = OfficialComputeMachineTypesPageClient(
        cast("compute_v1.MachineTypesClient", client),
        maximum_workers=1,
    )

    async def run_reads() -> None:
        await asyncio.gather(
            *(
                wrapper.machine_types(
                    project="public-project",
                    max_results=1,
                    page_token="",
                    return_partial_success=True,
                    timeout_seconds=1,
                )
                for _ in range(3)
            )
        )

    asyncio.run(run_reads())

    assert client.maximum_active == 1


def test_cancelled_compute_waiter_holds_slot_until_sync_worker_finishes() -> None:
    """Cancellation cannot release a slot while its sync provider call still runs."""
    started = Event()
    release = Event()

    class Pager:
        def __init__(self) -> None:
            self.pages = iter((compute_v1.MachineTypeAggregatedList(),))

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def aggregated_list(self, **kwargs: object) -> Pager:
            del kwargs
            self.calls += 1
            started.set()
            assert release.wait(timeout=1)
            return Pager()

    client = Client()
    wrapper = OfficialComputeMachineTypesPageClient(
        cast("compute_v1.MachineTypesClient", client),
        maximum_workers=1,
    )

    async def read() -> ComputeMachineTypesPage:
        return await wrapper.machine_types(
            project="public-project",
            max_results=1,
            page_token="",
            return_partial_success=True,
            timeout_seconds=1,
        )

    async def run_race() -> None:
        first = asyncio.create_task(read())
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(read())
        await asyncio.sleep(0)
        assert client.calls == 1
        release.set()
        await second

    asyncio.run(run_race())

    assert client.calls == EXPECTED_CANCEL_RACE_CALLS
