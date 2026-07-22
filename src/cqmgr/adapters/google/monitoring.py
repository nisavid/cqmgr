"""Read-only official Cloud Monitoring usage adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from google.api import metric_pb2
from google.cloud import monitoring_v3

from cqmgr.adapters.google.read_policy import (
    GoogleReadPolicy,
    page_cap_diagnostic,
    schema_diagnostic,
)
from cqmgr.application.ports.provider_reads import UsageReadRequest
from cqmgr.domain.quotas import (
    MonitoringPoint,
    MonitoringValue,
    MonitoringValueKind,
    NormalizedDimensions,
    ProviderRead,
    ProviderReadCoverage,
    UsageObservation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_ALLOCATION_USAGE_METRIC = "serviceruntime.googleapis.com/quota/allocation/usage"


@dataclass(frozen=True, slots=True)
class TimeSeriesPage:
    """Adapter-internal materialized Monitoring page."""

    items: tuple[monitoring_v3.TimeSeries, ...]
    next_page_token: str


class MonitoringPageClient(Protocol):
    """Narrow generated-client seam that materializes one time-series page."""

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
        """Return one official Monitoring page without exposing a pager."""
        ...


class OfficialMonitoringPageClient:
    """Keep official requests, pagers, retry objects, and DTOs in the adapter."""

    def __init__(self, client: monitoring_v3.MetricServiceAsyncClient) -> None:
        """Bind one shared-credential official async client."""
        self._client = client

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
        """Materialize exactly the requested Monitoring response page."""
        request = monitoring_v3.ListTimeSeriesRequest(
            name=name,
            filter=filter_expression,
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            page_size=page_size,
            page_token=page_token,
        )
        pager = await self._client.list_time_series(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = await anext(pager.pages)
        return TimeSeriesPage(tuple(response.time_series), response.next_page_token)


class GoogleUsageReader:
    """Read quota allocation usage while preserving every point interval."""

    def __init__(
        self,
        client: MonitoringPageClient,
        policy: GoogleReadPolicy,
        *,
        page_size: int = 100,
        maximum_pages: int = 100,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind finite pagination policy."""
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size < 1
        ):
            msg = "Monitoring page_size must be positive"
            raise ValueError(msg)
        if (
            isinstance(maximum_pages, bool)
            or not isinstance(maximum_pages, int)
            or maximum_pages < 1
        ):
            msg = "Monitoring maximum_pages must be positive"
            raise ValueError(msg)
        self._client = client
        self._policy = policy
        self._page_size = page_size
        self._maximum_pages = maximum_pages
        self._now = now

    async def read(self, request: UsageReadRequest) -> ProviderRead[UsageObservation]:
        """List exact usage evidence with page caps and source failures visible."""
        if not isinstance(request, UsageReadRequest):
            msg = "usage reader requires UsageReadRequest"
            raise TypeError(msg)
        name = request.context.project.resource_scope.canonical_name
        interval = monitoring_v3.TimeInterval(
            start_time=request.interval_start,
            end_time=request.interval_end,
        )
        token = ""
        attempted = 0
        completed = 0
        values: list[UsageObservation] = []
        diagnostics = []
        cap = False
        while attempted < self._maximum_pages:
            attempted += 1
            result = await self._policy.call(
                request.context,
                provider="cloud-monitoring",
                phase="monitoring-usage-read",
                identity=f"monitoring-usage:{name}:{token}",
                dispatch=lambda timeout, page_token=token: self._client.time_series(
                    name=name,
                    filter_expression=request.filter,
                    interval=interval,
                    page_size=self._page_size,
                    page_token=page_token,
                    timeout_seconds=timeout,
                ),
            )
            if result.diagnostic is not None:
                diagnostics.append(result.diagnostic)
                break
            page = result.value
            if page is None:
                msg = "successful page call must contain a page"
                raise RuntimeError(msg)
            completed += 1
            for item in page.items:
                try:
                    values.append(_map_time_series(item, request))
                except (TypeError, ValueError, OverflowError):
                    diagnostics.append(
                        schema_diagnostic("monitoring-usage-read", "cloud-monitoring")
                    )
            token = page.next_page_token
            if not token:
                break
        else:
            cap = bool(token)
        if cap:
            diagnostics.append(
                page_cap_diagnostic("monitoring-usage-read", "cloud-monitoring")
            )
        return ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=self._now(),
            diagnostics=tuple(diagnostics),
        )


def _map_time_series(
    series: monitoring_v3.TimeSeries,
    request: UsageReadRequest,
) -> UsageObservation:
    metric_labels = series.metric.labels
    resource_labels = series.resource.labels
    if (
        series.metric.type != _ALLOCATION_USAGE_METRIC
        or series.metric_kind != metric_pb2.MetricDescriptor.MetricKind.GAUGE
        or series.value_type != metric_pb2.MetricDescriptor.ValueType.INT64
        or series.unit != "1"
        or not metric_labels.get("quota_metric")
        or series.resource.type != "consumer_quota"
        or resource_labels.get("project_id") != request.context.project.project_id
        or not resource_labels.get("service")
        or not resource_labels.get("location")
    ):
        msg = "Monitoring response contains a non-usage metric"
        raise ValueError(msg)
    return UsageObservation(
        resource_scope=request.context.project.resource_scope,
        metric_type=series.metric.type,
        metric_labels=NormalizedDimensions(series.metric.labels.items()),
        resource_type=series.resource.type,
        resource_labels=NormalizedDimensions(series.resource.labels.items()),
        points=tuple(
            _map_point(point, value_field="int64_value") for point in series.points
        ),
        unit=series.unit or None,
    )


def _map_point(
    point: monitoring_v3.Point,
    *,
    value_field: str,
) -> MonitoringPoint:
    point_pb = monitoring_v3.Point.pb(point)
    interval_pb = point_pb.interval
    if not interval_pb.HasField("end_time"):
        msg = "Monitoring point requires end_time"
        raise ValueError(msg)
    end = interval_pb.end_time.ToDatetime(tzinfo=UTC)
    start = None
    if interval_pb.HasField("start_time"):
        start = interval_pb.start_time.ToDatetime(tzinfo=UTC)
        if start != end:
            msg = "GAUGE Monitoring point interval must be instantaneous"
            raise ValueError(msg)
    actual_value_field = point_pb.value.WhichOneof("value")
    kinds = {
        "bool_value": MonitoringValueKind.BOOL,
        "int64_value": MonitoringValueKind.INT64,
        "double_value": MonitoringValueKind.DOUBLE,
        "string_value": MonitoringValueKind.STRING,
    }
    kind = kinds.get(actual_value_field)
    if kind is None or actual_value_field != value_field:
        msg = "Monitoring point has unsupported value type"
        raise ValueError(msg)
    value = getattr(point_pb.value, actual_value_field)
    return MonitoringPoint(start, end, MonitoringValue(kind, value))
