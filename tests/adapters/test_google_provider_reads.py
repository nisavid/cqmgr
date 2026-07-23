"""Read-only Cloud Quotas and Monitoring adapter contracts."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, cast, override

import pytest
from google.api import metric_pb2
from google.api_core import exceptions as google_exceptions
from google.cloud import cloudquotas_v1, monitoring_v3
from google.protobuf import json_format

from cqmgr.adapters.google.cloud_quotas import (
    GoogleEffectiveQuotaReader,
    GoogleQuotaPreferenceReader,
    OfficialCloudQuotasPageClient,
    QuotaInfoPage,
    QuotaPreferencePage,
)
from cqmgr.adapters.google.monitoring import (
    GoogleUsageReader,
    OfficialMonitoringPageClient,
    TimeSeriesPage,
)
from cqmgr.adapters.google.read_policy import GoogleReadPolicy
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    BudgetRequest,
    CancellationToken,
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
)
from cqmgr.application.ports.provider_reads import (
    EffectiveQuotaReadRequest,
    ProviderReadContext,
    QuotaPreferenceReadRequest,
    UsageReadRequest,
)
from cqmgr.domain.accelerator_overlay import (
    MAINTAINED_ACCELERATOR_OVERLAY,
    CandidateLocations,
    ComputeInstanceRequirement,
    ProvisioningModel,
    WorkloadCatalogEvidence,
    WorkloadLocationDisposition,
)
from cqmgr.domain.catalog import (
    AcceleratorAttachment,
    CatalogEvidenceSource,
    CatalogLocationCoverage,
    ComputeAcceleratorType,
    ComputeMachineType,
    LocationCoverageExpectation,
    LocationCoverageState,
)
from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject, CredentialKind
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.quotas import QuotaScope
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

FIXTURES = Path(__file__).parents[1] / "fixtures" / "google"
NOW = datetime(2026, 7, 22, 3, tzinfo=UTC)
TWO = 2
GRANTED_VALUE = 64
INFO_PAGE_SIZE = 17
MONITORING_PAGE_SIZE = 23
EXPECTED_USAGE_FILTER = (
    'metric.type = "serviceruntime.googleapis.com/quota/allocation/usage" '
    'AND resource.type = "consumer_quota" '
    'AND resource.labels.service = "compute.googleapis.com"'
)


class OnePagePager:
    """Minimal generated-pager shape for official request tests."""

    def __init__(self, response: object) -> None:
        """Retain one generated response."""
        self.response = response

    @property
    def pages(self) -> AsyncIterator[object]:
        """Return an async iterator containing the retained response."""

        async def generate() -> AsyncIterator[object]:
            yield self.response

        return generate()


class FakeOfficialCloudQuotasClient:
    """Capture official request objects without network access."""

    def __init__(self) -> None:
        """Create an empty request ledger."""
        self.calls: list[tuple[str, object, object, object]] = []

    async def list_quota_infos(
        self,
        *,
        request: object,
        retry: object,
        timeout: object,  # noqa: ASYNC109
    ) -> OnePagePager:
        """Return one empty generated QuotaInfo response."""
        self.calls.append(("info", request, retry, timeout))
        response = cloudquotas_v1.ListQuotaInfosResponse(
            next_page_token="public-next-page"  # noqa: S106
        )
        return OnePagePager(response)

    async def list_quota_preferences(
        self,
        *,
        request: object,
        retry: object,
        timeout: object,  # noqa: ASYNC109
    ) -> OnePagePager:
        """Return one empty generated preference response."""
        self.calls.append(("preference", request, retry, timeout))
        response = cloudquotas_v1.ListQuotaPreferencesResponse()
        return OnePagePager(response)


class FakeOfficialMonitoringClient:
    """Capture official Monitoring request objects without network access."""

    def __init__(self) -> None:
        """Create an empty request ledger."""
        self.calls: list[tuple[object, object, object]] = []

    async def list_time_series(
        self,
        *,
        request: object,
        retry: object,
        timeout: object,  # noqa: ASYNC109
    ) -> OnePagePager:
        """Return one empty generated time-series response."""
        self.calls.append((request, retry, timeout))
        return OnePagePager(monitoring_v3.ListTimeSeriesResponse())


class RecordingBudget:
    """In-memory budget that records every provider attempt."""

    def __init__(self) -> None:
        """Create an empty call ledger."""
        self.requests: list[BudgetRequest] = []

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Record and grant one request."""
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return BudgetGrant(charged_at=deadline - 1, request=request)


class CancellingBudget(RecordingBudget):
    """Commit a charge while cancellation races the return path."""

    @override
    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Record a durable grant, then signal cancellation before dispatch."""
        grant = await super().acquire(
            request,
            deadline=deadline,
            cancellation=cancellation,
        )
        cancellation.cancel()
        return grant


class FailingBudget(RecordingBudget):
    """Raise one typed local coordination outcome before provider dispatch."""

    def __init__(self, error: Exception) -> None:
        """Retain the typed outcome."""
        super().__init__()
        self.error = error

    @override
    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Raise without recording a successful durable budget charge."""
        del request, deadline, cancellation
        raise self.error


class NoJitter:
    """Deterministic zero-delay retry seam."""

    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        """Return no delay after validating policy inputs."""
        assert delay >= 0
        assert attempt >= 0
        assert identity
        return 0.0


class FakeCloudQuotasPages:
    """Scripted one-page client with no network access."""

    def __init__(
        self,
        info_pages: Sequence[QuotaInfoPage | BaseException] | None = None,
        preference_pages: Sequence[QuotaPreferencePage | BaseException] | None = None,
    ) -> None:
        """Configure independent scripted read sequences."""
        self.info_pages = list(info_pages or [])
        self.preference_pages = list(preference_pages or [])
        self.calls: list[tuple[str, str, str, float]] = []

    async def quota_infos(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> QuotaInfoPage:
        """Return the next scripted QuotaInfo page."""
        self.calls.append(("info", parent, page_token, timeout_seconds))
        assert page_size == 1
        result = self.info_pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def quota_preferences(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> QuotaPreferencePage:
        """Return the next scripted preference page."""
        self.calls.append(("preference", parent, page_token, timeout_seconds))
        assert page_size == 1
        result = self.preference_pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeMonitoringPages:
    """Scripted Monitoring page client."""

    def __init__(self, pages: list[TimeSeriesPage | BaseException]) -> None:
        """Configure one scripted Monitoring sequence."""
        self.pages = list(pages)
        self.calls: list[tuple[str, str, str, float]] = []

    async def time_series(  # noqa: PLR0913
        self,
        *,
        name: str,
        filter_expression: str,
        interval: monitoring_v3.TimeInterval,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> TimeSeriesPage:
        """Return the next scripted time-series page."""
        assert interval.start_time
        assert interval.end_time
        assert page_size == 1
        self.calls.append((name, filter_expression, page_token, timeout_seconds))
        result = self.pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _context(
    *,
    adc_quota_project: bool = True,
    cancellation: CancellationToken | None = None,
    deadline: float = 100.0,
) -> ProviderReadContext:
    identity = ADCIdentityEvidence.principal_unverified(
        credential_kind=CredentialKind.UNKNOWN,
        adc_quota_project=(
            ADCQuotaProject("transport-project") if adc_quota_project else None
        ),
    )
    return ProviderReadContext(
        project=CanonicalProject(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/415104041262"),
            "public-schema-project",
            "Public Schema Project",
        ),
        identity=identity,
        deadline=deadline,
        cancellation=cancellation or CancellationToken(),
    )


def _policy(
    budget: RecordingBudget,
    *,
    times: list[float] | None = None,
) -> GoogleReadPolicy:
    clock = iter(times or [0.0] * 20)
    return GoogleReadPolicy(
        budget,
        NoJitter(),
        timeout_seconds=7.0,
        monotonic=lambda: next(clock),
    )


def _json(name: str) -> Mapping[str, object]:
    return cast("Mapping[str, object]", json.loads((FIXTURES / name).read_text()))


def _quota_info_pages() -> list[QuotaInfoPage]:
    pages = cast("list[Mapping[str, object]]", _json("quota-info-pages.json")["pages"])
    results = []
    for page in pages:
        infos = []
        for raw in cast("list[Mapping[str, object]]", page["quotaInfos"]):
            pb = cloudquotas_v1.QuotaInfo.pb()()
            json_format.ParseDict(dict(raw), pb, ignore_unknown_fields=True)
            infos.append(cloudquotas_v1.QuotaInfo(pb))
        results.append(QuotaInfoPage(tuple(infos), cast("str", page["nextPageToken"])))
    return results


def _preference_page() -> QuotaPreferencePage:
    raw = _json("quota-preference-page.json")
    items = []
    for value in cast("list[Mapping[str, object]]", raw["quotaPreferences"]):
        pb = cloudquotas_v1.QuotaPreference.pb()()
        json_format.ParseDict(dict(value), pb, ignore_unknown_fields=True)
        items.append(cloudquotas_v1.QuotaPreference(pb))
    return QuotaPreferencePage(tuple(items), cast("str", raw["nextPageToken"]))


def _monitoring_page() -> TimeSeriesPage:
    raw = _json("monitoring-usage-page.json")
    items = []
    for value in cast("list[Mapping[str, object]]", raw["timeSeries"]):
        pb = monitoring_v3.TimeSeries.pb()()
        json_format.ParseDict(dict(value), pb, ignore_unknown_fields=True)
        items.append(monitoring_v3.TimeSeries(pb))
    return TimeSeriesPage(tuple(items), cast("str", raw["nextPageToken"]))


def test_effective_quota_reader_preserves_pages_values_and_explicit_global_scope() -> (
    None
):
    """An explicit global applicable location normalizes to global quota scope."""
    budget = RecordingBudget()
    client = FakeCloudQuotasPages(info_pages=_quota_info_pages())
    reader = GoogleEffectiveQuotaReader(
        client, _policy(budget), page_size=1, now=lambda: NOW
    )

    result = asyncio.run(
        reader.read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert result.complete
    assert result.coverage.pages_completed == TWO
    assert [value.identity.quota_scope for value in result.values] == [
        QuotaScope.REGIONAL,
        QuotaScope.GLOBAL,
    ]
    assert result.values[0].effective_value.value == 2**63 - 1
    assert result.values[0].container_type.raw == "CONTAINER_TYPE_UNSPECIFIED"
    assert result.values[0].ongoing_rollout
    assert result.values[1].applicable_locations == ("global",)
    assert client.calls[1][2] == "public-page-2"
    assert len(budget.requests) == TWO
    assert budget.requests[0].adc_quota_project is not None


def test_effective_quota_reader_output_resolves_maintained_gpu_companions() -> None:
    """Normalized provider evidence matches both maintained GPU constraints."""
    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=_quota_info_pages()),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )
    catalog = WorkloadCatalogEvidence(
        compute_machine_types=(
            ComputeMachineType(
                "a3-highgpu-8g",
                "us-central1-a",
                (AcceleratorAttachment("nvidia-h100-80gb", 8),),
                None,
            ),
        ),
        tpu_locations=(),
        tpu_accelerator_types=(),
        tpu_runtime_versions=(),
        coverage=(
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_ACCELERATOR_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
            CatalogLocationCoverage(
                CatalogEvidenceSource.COMPUTE_MACHINE_TYPES,
                "us-central1-a",
                LocationCoverageExpectation.REQUESTED,
                LocationCoverageState.SUCCESS,
            ),
        ),
        compute_accelerator_types=(
            ComputeAcceleratorType("nvidia-h100-80gb", "us-central1-a", None),
        ),
    )
    requirement = ComputeInstanceRequirement(
        machine_type="a3-highgpu-8g",
        instance_count=1,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a",)),
    )

    resolved = MAINTAINED_ACCELERATOR_OVERLAY.resolve(
        requirement,
        (
            result.values[0],
            replace(result.values[1], eligibility=result.values[0].eligibility),
        ),
        catalog,
    )

    location = resolved.locations[0]
    assert location.disposition is WorkloadLocationDisposition.COMPATIBLE
    assert tuple(
        item.identity.quota_id for item in location.constraint_requirements
    ) == (
        "GPUS-ALL-REGIONS-per-project",
        "GPUS-PER-GPU-FAMILY-per-project-region",
    )


def test_official_cloud_quotas_wrapper_uses_one_page_and_disables_retry() -> None:
    """Generated pagers and retry objects terminate inside the adapter."""
    client = FakeOfficialCloudQuotasClient()
    wrapper = OfficialCloudQuotasPageClient(
        cast("cloudquotas_v1.CloudQuotasAsyncClient", client)
    )

    info = asyncio.run(
        wrapper.quota_infos(
            parent="projects/1/locations/global/services/compute.googleapis.com",
            page_size=INFO_PAGE_SIZE,
            page_token="public-page-token",  # noqa: S106
            timeout_seconds=3.5,
        )
    )
    preference = asyncio.run(
        wrapper.quota_preferences(
            parent="projects/1/locations/global",
            page_size=19,
            page_token="",
            timeout_seconds=4.5,
        )
    )

    info_request = cast("cloudquotas_v1.ListQuotaInfosRequest", client.calls[0][1])
    assert info_request.page_size == INFO_PAGE_SIZE
    assert info_request.page_token == "public-page-token"  # noqa: S105
    assert client.calls[0][2:] == (None, 3.5)
    assert info.next_page_token == "public-next-page"  # noqa: S105
    assert preference.items == ()


def test_official_monitoring_wrapper_uses_full_view_and_disables_retry() -> None:
    """The generated Monitoring request is exact, bounded, and read-only."""
    client = FakeOfficialMonitoringClient()
    wrapper = OfficialMonitoringPageClient(
        cast("monitoring_v3.MetricServiceAsyncClient", client)
    )
    interval = monitoring_v3.TimeInterval(
        start_time=datetime(2026, 7, 22, tzinfo=UTC),
        end_time=datetime(2026, 7, 23, tzinfo=UTC),
    )

    page = asyncio.run(
        wrapper.time_series(
            name="projects/1",
            filter_expression="public-filter",
            interval=interval,
            page_size=MONITORING_PAGE_SIZE,
            page_token="public-page-token",  # noqa: S106
            timeout_seconds=2.5,
        )
    )

    request = cast("monitoring_v3.ListTimeSeriesRequest", client.calls[0][0])
    assert request.name == "projects/1"
    assert request.filter == "public-filter"
    assert request.view == monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL
    assert request.page_size == MONITORING_PAGE_SIZE
    assert client.calls[0][1:] == (None, 2.5)
    assert page.items == ()


def test_preference_reader_preserves_lifecycle_etag_and_timestamps() -> None:
    """Existing preferences retain safe reconciliation evidence, never contact."""
    budget = RecordingBudget()
    reader = GoogleQuotaPreferenceReader(
        FakeCloudQuotasPages(preference_pages=[_preference_page()]),
        _policy(budget),
        page_size=1,
        now=lambda: NOW,
    )

    result = asyncio.run(reader.read(QuotaPreferenceReadRequest(_context())))

    assert result.complete
    preference = result.values[0]
    assert preference.preferred_value == 2**63 - 1
    assert preference.granted_value == GRANTED_VALUE
    assert preference.etag == "public-etag-example"
    assert preference.reconciling
    assert preference.update_time == datetime(2026, 7, 21, 2, 3, 4, tzinfo=UTC)
    assert "public.fixture@example.com" not in repr(result)


def test_preference_reader_attributes_known_schema_failure_to_its_service() -> None:
    """One malformed TPU item cannot invalidate a complete Compute partition."""
    compute = cloudquotas_v1.QuotaPreference(_preference_page().items[0])
    tpu = cloudquotas_v1.QuotaPreference(compute)
    tpu.service = "tpu.googleapis.com"
    cloudquotas_v1.QuotaPreference.pb(tpu).ClearField("quota_config")
    reader = GoogleQuotaPreferenceReader(
        FakeCloudQuotasPages(
            preference_pages=[QuotaPreferencePage((compute, tpu), "")]
        ),
        _policy(RecordingBudget()),
        page_size=1,
        now=lambda: NOW,
    )

    result = asyncio.run(
        reader.read(
            QuotaPreferenceReadRequest(
                _context(),
                ("compute.googleapis.com", "tpu.googleapis.com"),
            )
        )
    )

    assert result.complete_for("compute.googleapis.com")
    assert not result.complete_for("tpu.googleapis.com")
    assert tuple(item.identity.service for item in result.values) == (
        "compute.googleapis.com",
    )


def test_preference_reader_ignores_known_unselected_service_schema_failure() -> None:
    """A selected Compute inventory does not own a malformed known TPU item."""
    tpu = cloudquotas_v1.QuotaPreference(_preference_page().items[0])
    tpu.service = "tpu.googleapis.com"
    cloudquotas_v1.QuotaPreference.pb(tpu).ClearField("quota_config")
    reader = GoogleQuotaPreferenceReader(
        FakeCloudQuotasPages(preference_pages=[QuotaPreferencePage((tpu,), "")]),
        _policy(RecordingBudget()),
        page_size=1,
        now=lambda: NOW,
    )

    result = asyncio.run(
        reader.read(
            QuotaPreferenceReadRequest(
                _context(),
                ("compute.googleapis.com",),
            )
        )
    )

    assert result.complete_for("compute.googleapis.com")
    assert result.values == ()
    assert result.diagnostics == ()


def test_page_cap_retains_values_but_marks_effective_read_incomplete() -> None:
    """A required next page cannot be hidden behind usable first-page evidence."""
    pages = _quota_info_pages()
    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=pages),
            _policy(RecordingBudget()),
            page_size=1,
            maximum_pages=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert result.values
    assert not result.complete
    assert result.coverage.page_cap_reached
    assert result.diagnostics[0].code.value == "provider-page-cap-reached"


def test_transient_page_failure_retries_and_charges_every_attempt() -> None:
    """A documented throttling response retries only inside the shared deadline."""
    budget = RecordingBudget()
    pages: list[QuotaInfoPage | BaseException] = [
        google_exceptions.ResourceExhausted("token=private"),
        _quota_info_pages()[0],
    ]
    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=pages),
            _policy(budget),
            page_size=1,
            maximum_pages=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert len(budget.requests) == TWO
    assert not result.complete  # the successful page still advertises another page
    assert "private" not in repr(result)


def test_permanent_and_unknown_transport_failures_are_static_and_incomplete() -> None:
    """Provider exceptions and their private text never cross the adapter."""
    for error, code in (
        (
            google_exceptions.PermissionDenied("/Users/private/adc.json"),
            "provider-read-authorization-failed",
        ),
        (RuntimeError("ya29.private-token"), "provider-read-failed"),
    ):
        budget = RecordingBudget()
        result = asyncio.run(
            GoogleEffectiveQuotaReader(
                FakeCloudQuotasPages(info_pages=[error]),
                _policy(budget),
                page_size=1,
                now=lambda: NOW,
            ).read(
                EffectiveQuotaReadRequest(
                    _context(adc_quota_project=False),
                    "compute.googleapis.com",
                )
            )
        )
        assert not result.complete
        assert result.coverage.pages_completed == 0
        assert result.diagnostics[0].code.value == code
        assert "private" not in repr(result)
        assert budget.requests[0].adc_quota_project is None


def test_not_found_is_a_permanent_provider_failure() -> None:
    """A documented missing resource never receives unknown retry guidance."""
    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(
                info_pages=[google_exceptions.NotFound("private provider text")]
            ),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert not result.complete
    assert result.diagnostics[0].code.value == "provider-read-not-found"
    assert result.diagnostics[0].retry.value == "never"


def test_cancellation_after_budget_commit_stops_before_dispatch() -> None:
    """A committed conservative charge does not authorize a cancelled read."""
    budget = CancellingBudget()
    client = FakeCloudQuotasPages(info_pages=_quota_info_pages())
    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            client,
            _policy(budget),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert not result.complete
    assert result.diagnostics[0].code.value == "provider-read-cancelled"
    assert len(budget.requests) == 1
    assert client.calls == []


def test_cancellation_interrupts_active_provider_dispatch_without_task_leaks() -> None:
    """Thread-safe cancellation promptly stops and joins an active provider task."""

    async def exercise() -> None:
        token = CancellationToken()
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def dispatch(timeout_seconds: float) -> object:
            assert timeout_seconds > 0
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        call = asyncio.create_task(
            _policy(RecordingBudget()).call(
                _context(cancellation=token),
                provider="cloud-quotas",
                phase="quota-info-read",
                identity="active-dispatch",
                dispatch=dispatch,
            )
        )
        await started.wait()
        cancelling_thread = Thread(target=token.cancel)
        cancelling_thread.start()
        cancelling_thread.join()

        result = await asyncio.wait_for(call, timeout=0.5)
        await asyncio.sleep(0)

        assert result.diagnostic is not None
        assert result.diagnostic.code.value == "provider-read-cancelled"
        assert cleaned.is_set()
        assert not cancelling_thread.is_alive()
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ] == []

    asyncio.run(exercise())


def test_outer_task_cancellation_propagates_after_dispatch_cleanup() -> None:
    """Harness cancellation remains CancelledError after child tasks are joined."""

    async def exercise() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def dispatch(timeout_seconds: float) -> object:
            assert timeout_seconds > 0
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        call = asyncio.create_task(
            _policy(RecordingBudget()).call(
                _context(),
                provider="cloud-quotas",
                phase="quota-info-read",
                identity="outer-cancellation",
                dispatch=dispatch,
            )
        )
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0)

        assert cleaned.is_set()
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ] == []

    asyncio.run(exercise())


def test_cancellation_interrupts_retry_backoff_without_task_leaks() -> None:
    """Cancellation promptly stops and joins a retry backoff task."""

    async def exercise() -> None:
        token = CancellationToken()
        backoff_started = asyncio.Event()
        backoff_cleaned = asyncio.Event()
        dispatch_calls = 0

        async def dispatch(timeout_seconds: float) -> object:
            nonlocal dispatch_calls
            assert timeout_seconds > 0
            dispatch_calls += 1
            private_message = "private"
            raise google_exceptions.ServiceUnavailable(private_message)

        async def sleep(delay: float) -> None:
            assert delay >= 0
            backoff_started.set()
            try:
                await asyncio.Future()
            finally:
                backoff_cleaned.set()

        policy = GoogleReadPolicy(
            RecordingBudget(),
            NoJitter(),
            maximum_attempts=2,
            monotonic=lambda: 0.0,
            sleep=sleep,
        )
        call = asyncio.create_task(
            policy.call(
                _context(cancellation=token),
                provider="cloud-quotas",
                phase="quota-info-read",
                identity="retry-backoff",
                dispatch=dispatch,
            )
        )
        await backoff_started.wait()
        cancelling_thread = Thread(target=token.cancel)
        cancelling_thread.start()
        cancelling_thread.join()

        result = await asyncio.wait_for(call, timeout=0.5)
        await asyncio.sleep(0)

        assert result.diagnostic is not None
        assert result.diagnostic.code.value == "provider-read-cancelled"
        assert dispatch_calls == 1
        assert backoff_cleaned.is_set()
        assert not cancelling_thread.is_alive()
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ] == []

    asyncio.run(exercise())


def test_caller_deadline_interrupts_uncooperative_provider_dispatch() -> None:
    """The caller deadline stops and joins a client that ignores its timeout."""

    async def exercise() -> None:
        dispatch_started = asyncio.Event()
        dispatch_cleaned = asyncio.Event()

        async def dispatch(timeout_seconds: float) -> object:
            assert timeout_seconds > 0
            dispatch_started.set()
            try:
                await asyncio.Future()
            finally:
                dispatch_cleaned.set()

        deadline = time.monotonic() + 0.02
        policy = GoogleReadPolicy(
            RecordingBudget(),
            NoJitter(),
            monotonic=time.monotonic,
        )
        result = await asyncio.wait_for(
            policy.call(
                _context(deadline=deadline),
                provider="cloud-quotas",
                phase="quota-info-read",
                identity="caller-deadline",
                dispatch=dispatch,
            ),
            timeout=0.5,
        )
        await asyncio.sleep(0)

        assert dispatch_started.is_set()
        assert result.diagnostic is not None
        assert result.diagnostic.code.value == "provider-read-deadline-exceeded"
        assert dispatch_cleaned.is_set()
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ] == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (CoordinationCancelledError(), "provider-read-cancelled"),
        (
            CoordinationDeadlineExceededError(),
            "provider-read-deadline-exceeded",
        ),
    ],
)
def test_budget_wait_preserves_typed_stop_outcome(
    error: Exception,
    code: str,
) -> None:
    """Cancellation and deadline expiry while waiting retain their semantics."""
    client = FakeCloudQuotasPages(info_pages=_quota_info_pages())
    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            client,
            _policy(FailingBudget(error)),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert not result.complete
    assert result.diagnostics[0].code.value == code
    assert client.calls == []


def test_unavailable_adc_stops_before_budget_or_provider_access() -> None:
    """Unreadable credentials cannot dispatch a provider call."""
    budget = RecordingBudget()
    client = FakeCloudQuotasPages(info_pages=_quota_info_pages())
    context = _context()
    unavailable = ADCIdentityEvidence.unavailable(
        credential_kind=CredentialKind.UNKNOWN,
        code="adc-unavailable",
        guidance="Repair ADC outside cqmgr.",
    )
    request = EffectiveQuotaReadRequest(
        ProviderReadContext(
            context.project,
            unavailable,
            context.deadline,
            context.cancellation,
        ),
        "compute.googleapis.com",
    )

    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            client,
            _policy(budget),
            page_size=1,
            now=lambda: NOW,
        ).read(request)
    )

    assert not result.complete
    assert result.diagnostics[0].code.value == "provider-credentials-unavailable"
    assert budget.requests == []
    assert client.calls == []


def test_unknown_provider_enum_is_preserved_without_known_semantics() -> None:
    """A future public enum number remains exact schema-skew evidence."""
    page = _quota_info_pages()[0]
    pb = cloudquotas_v1.QuotaInfo.pb(page.items[0])
    pb.quota_increase_eligibility.ineligibility_reason = 99
    future = QuotaInfoPage((cloudquotas_v1.QuotaInfo(pb),), "")

    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=[future]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert result.values[0].eligibility.reason.raw == "UNRECOGNIZED_99"
    assert result.values[0].eligibility.reason.known is None


def test_schema_skew_keeps_page_evidence_incomplete() -> None:
    """Malformed required quota fields cannot satisfy a future mutation gate."""
    item = _quota_info_pages()[1].items[0]
    item.metric_unit = ""
    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=[QuotaInfoPage((item,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert result.values == ()
    assert not result.complete
    assert result.diagnostics[0].code.value == "provider-schema-invalid"


@pytest.mark.parametrize("missing", ["eligibility", "details"])
def test_missing_required_quota_info_messages_are_incomplete(missing: str) -> None:
    """Absent nested messages cannot become false or zero effective evidence."""
    item = cloudquotas_v1.QuotaInfo(_quota_info_pages()[0].items[0])
    pb = cloudquotas_v1.QuotaInfo.pb(item)
    if missing == "eligibility":
        pb.ClearField("quota_increase_eligibility")
    else:
        pb.dimensions_infos[0].ClearField("details")

    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=[QuotaInfoPage((item,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert result.values == ()
    assert not result.complete
    assert result.diagnostics[0].code.value == "provider-schema-invalid"


@pytest.mark.parametrize("mutation", ["locations", "location-value", "dimension"])
def test_incomplete_quota_info_slice_identity_is_rejected(mutation: str) -> None:
    """A slice requires covered locations and only declared dimension keys."""
    item = cloudquotas_v1.QuotaInfo(_quota_info_pages()[0].items[0])
    slice_ = item.dimensions_infos[0]
    if mutation == "locations":
        del slice_.applicable_locations[:]
    elif mutation == "location-value":
        slice_.applicable_locations[0] = ""
    else:
        slice_.dimensions["future_dimension"] = "future-value"

    result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=[QuotaInfoPage((item,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )

    assert not result.complete
    assert result.values == ()
    assert result.diagnostics[0].code.value == "provider-schema-invalid"


def test_missing_preference_config_is_incomplete() -> None:
    """An absent quotaConfig cannot become a preferred value of zero."""
    item = cloudquotas_v1.QuotaPreference(_preference_page().items[0])
    cloudquotas_v1.QuotaPreference.pb(item).ClearField("quota_config")

    result = asyncio.run(
        GoogleQuotaPreferenceReader(
            FakeCloudQuotasPages(preference_pages=[QuotaPreferencePage((item,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(QuotaPreferenceReadRequest(_context()))
    )

    assert result.values == ()
    assert not result.complete
    assert result.diagnostics[0].code.value == "provider-schema-invalid"


def test_provider_resource_names_must_match_normalized_identity() -> None:
    """Scalar identity cannot override a mismatched provider resource name."""
    info = cloudquotas_v1.QuotaInfo(_quota_info_pages()[0].items[0])
    info.name = (
        "projects/415104041262/locations/global/services/wrong.googleapis.com/"
        "quotaInfos/wrong"
    )
    preference = cloudquotas_v1.QuotaPreference(_preference_page().items[0])
    preference.name = "projects/415104041262/locations/global/notPreferences/bad"

    info_result = asyncio.run(
        GoogleEffectiveQuotaReader(
            FakeCloudQuotasPages(info_pages=[QuotaInfoPage((info,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(EffectiveQuotaReadRequest(_context(), "compute.googleapis.com"))
    )
    preference_result = asyncio.run(
        GoogleQuotaPreferenceReader(
            FakeCloudQuotasPages(
                preference_pages=[QuotaPreferencePage((preference,), "")]
            ),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(QuotaPreferenceReadRequest(_context()))
    )

    assert not info_result.complete
    assert info_result.values == ()
    assert not preference_result.complete
    assert preference_result.values == ()


@pytest.mark.parametrize("preferred", [-1, 0])
def test_preference_accepts_documented_lower_boundary(preferred: int) -> None:
    """Unlimited minus one and ordinary zero remain valid absolute targets."""
    item = cloudquotas_v1.QuotaPreference(_preference_page().items[0])
    item.quota_config.preferred_value = preferred
    result = asyncio.run(
        GoogleQuotaPreferenceReader(
            FakeCloudQuotasPages(preference_pages=[QuotaPreferencePage((item,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(QuotaPreferenceReadRequest(_context()))
    )
    assert result.complete
    assert result.values[0].preferred_value == preferred


def test_preference_below_provider_minimum_is_incomplete() -> None:
    """Schema-skew below the documented -1 minimum fails closed."""
    item = cloudquotas_v1.QuotaPreference(_preference_page().items[0])
    item.quota_config.preferred_value = -2
    result = asyncio.run(
        GoogleQuotaPreferenceReader(
            FakeCloudQuotasPages(preference_pages=[QuotaPreferencePage((item,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(QuotaPreferenceReadRequest(_context()))
    )
    assert not result.complete
    assert result.values == ()


def test_monitoring_reader_preserves_all_points_intervals_values_and_labels() -> None:
    """Usage remains separate time-series evidence without invented freshness."""
    budget = RecordingBudget()
    client = FakeMonitoringPages([_monitoring_page()])
    request = UsageReadRequest(
        _context(),
        "compute.googleapis.com",
        datetime(2026, 7, 22, tzinfo=UTC),
        datetime(2026, 7, 23, tzinfo=UTC),
    )

    result = asyncio.run(
        GoogleUsageReader(client, _policy(budget), page_size=1, now=lambda: NOW).read(
            request
        )
    )

    assert result.complete
    (integer_observation,) = result.values
    assert integer_observation.metric_labels.items[0][0] == "quota_metric"
    assert len(integer_observation.points) == 1
    assert integer_observation.points[0].interval_start == datetime(
        2026, 7, 22, 2, tzinfo=UTC
    )
    assert integer_observation.points[0].value.value == 2**63 - 1
    assert integer_observation.resource_labels.items == (
        ("location", "us-central1"),
        ("project_id", "public-schema-project"),
        ("service", "compute.googleapis.com"),
    )
    assert client.calls[0][1] == EXPECTED_USAGE_FILTER


def test_monitoring_usage_query_is_typed_and_complete_when_empty() -> None:
    """A valid exact service query may complete with no observations, never zero."""
    client = FakeMonitoringPages([TimeSeriesPage((), "")])
    result = asyncio.run(
        GoogleUsageReader(
            client,
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(
            UsageReadRequest(
                _context(),
                "compute.googleapis.com",
                datetime(2026, 7, 22, tzinfo=UTC),
                datetime(2026, 7, 23, tzinfo=UTC),
            )
        )
    )

    assert result.complete
    assert result.values == ()
    assert client.calls[0][1] == EXPECTED_USAGE_FILTER


def test_monitoring_provider_filter_expression_is_not_a_usage_selector() -> None:
    """Provider filter text cannot cross the typed usage-reader port."""
    with pytest.raises(ValueError, match="service"):
        UsageReadRequest(
            _context(),
            'metric.type = "arbitrary.googleapis.com/wrong"',
            datetime(2026, 7, 22, tzinfo=UTC),
            datetime(2026, 7, 23, tzinfo=UTC),
        )


def test_monitoring_schema_skew_and_partial_page_fail_closed() -> None:
    """Unsupported point shapes and a failed required page remain incomplete."""
    page = _monitoring_page()
    bad = page.items[0]
    bad.metric.type = "future.googleapis.com/not-quota-usage"
    client = FakeMonitoringPages(
        [
            TimeSeriesPage((bad,), "next"),
            google_exceptions.ServiceUnavailable("private"),
        ]
    )
    result = asyncio.run(
        GoogleUsageReader(
            client,
            GoogleReadPolicy(
                RecordingBudget(), NoJitter(), maximum_attempts=1, monotonic=lambda: 0.0
            ),
            page_size=1,
            now=lambda: NOW,
        ).read(
            UsageReadRequest(
                _context(),
                "compute.googleapis.com",
                datetime(2026, 7, 22, tzinfo=UTC),
                datetime(2026, 7, 23, tzinfo=UTC),
            )
        )
    )

    assert not result.complete
    assert result.coverage.pages_attempted == TWO
    assert result.coverage.pages_completed == 1
    assert {item.code.value for item in result.diagnostics} == {
        "provider-schema-invalid",
        "provider-read-transient-failure",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "metric-kind",
        "value-type",
        "unit",
        "quota-metric",
        "project-id",
        "resource-type",
        "service",
        "location",
    ],
)
def test_monitoring_declarations_and_project_attribution_fail_closed(
    mutation: str,
) -> None:
    """Schema-skewed or cross-project usage never becomes complete evidence."""
    series = monitoring_v3.TimeSeries(_monitoring_page().items[0])
    if mutation == "metric-kind":
        series.metric_kind = metric_pb2.MetricDescriptor.MetricKind.CUMULATIVE
    elif mutation == "value-type":
        series.value_type = metric_pb2.MetricDescriptor.ValueType.DOUBLE
        series.points[0].value.double_value = 12.5
    elif mutation == "unit":
        series.unit = "{requests}"
    elif mutation == "quota-metric":
        series.metric.labels["quota_metric"] = ""
    elif mutation == "project-id":
        series.resource.labels.pop("project_id")
    elif mutation == "resource-type":
        series.resource.type = "future_consumer_quota"
    elif mutation == "service":
        series.resource.labels["service"] = "storage.googleapis.com"
    else:
        series.resource.labels.pop("location")
    result = asyncio.run(
        GoogleUsageReader(
            FakeMonitoringPages([TimeSeriesPage((series,), "")]),
            _policy(RecordingBudget()),
            page_size=1,
            now=lambda: NOW,
        ).read(
            UsageReadRequest(
                _context(),
                "compute.googleapis.com",
                datetime(2026, 7, 22, tzinfo=UTC),
                datetime(2026, 7, 23, tzinfo=UTC),
            )
        )
    )

    assert not result.complete
    assert result.values == ()
    assert result.diagnostics[0].code.value == "provider-schema-invalid"


@pytest.mark.parametrize("value", [0, -1, True, float("inf")])
def test_adapter_pagination_policy_is_bounded(value: object) -> None:
    """Page policy cannot be zero, boolean, negative, or unbounded."""
    with pytest.raises(ValueError, match="maximum_pages"):
        GoogleUsageReader(
            FakeMonitoringPages([]),
            _policy(RecordingBudget()),
            maximum_pages=cast("int", value),
        )
