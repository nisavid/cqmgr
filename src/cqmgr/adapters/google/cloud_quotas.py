"""Read-only official Cloud Quotas adapters with normalized evidence."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

from google.api_core import exceptions as google_exceptions
from google.cloud import cloudquotas_v1

from cqmgr.adapters.google.read_policy import (
    GoogleReadPolicy,
    page_cap_diagnostic,
    schema_diagnostic,
)
from cqmgr.application.ports.coordination import (
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
)
from cqmgr.application.ports.provider_reads import (
    EffectiveQuotaReadRequest,
    QuotaPreferenceReadRequest,
)
from cqmgr.application.ports.watch import (
    WatchObservation,
    WatchObservationRequest,
    WatchObservationTransientError,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaEvidence,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    ProviderRead,
    ProviderReadCoverage,
    QuotaContainerType,
    QuotaIncreaseEligibility,
    QuotaIneligibilityReason,
    QuotaPreferenceEvidence,
    QuotaPreferenceOrigin,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.status import QuotaRequestStatus, Reconciliation

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cqmgr.domain.watch import WatchChildIdentity


@dataclass(frozen=True, slots=True)
class QuotaInfoPage:
    """Adapter-internal materialized QuotaInfo page."""

    items: tuple[cloudquotas_v1.QuotaInfo, ...]
    next_page_token: str


@dataclass(frozen=True, slots=True)
class QuotaPreferencePage:
    """Adapter-internal materialized QuotaPreference page."""

    items: tuple[cloudquotas_v1.QuotaPreference, ...]
    next_page_token: str


class CloudQuotasPageClient(Protocol):
    """Narrow generated-client seam that materializes exactly one page."""

    async def quota_infos(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> QuotaInfoPage:
        """Return one official QuotaInfo page without exposing a pager."""
        ...

    async def quota_preferences(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> QuotaPreferencePage:
        """Return one official QuotaPreference page without exposing a pager."""
        ...


class CloudQuotasObservationClient(Protocol):
    """Read exact Cloud Quotas resources without exposing generated DTO calls."""

    async def quota_preference(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaPreference:
        """Return one exact bound QuotaPreference."""
        ...

    async def quota_info(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaInfo:
        """Return one exact QuotaInfo."""
        ...


class OfficialCloudQuotasPageClient:
    """Keep official requests, pagers, retry objects, and DTOs in the adapter."""

    def __init__(self, client: cloudquotas_v1.CloudQuotasAsyncClient) -> None:
        """Bind one shared-credential official async client."""
        self._client = client

    async def close(self) -> None:
        """Close the owned generated async client."""
        await self._client.transport.close()

    async def quota_infos(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> QuotaInfoPage:
        """Materialize exactly the requested QuotaInfo response page."""
        request = cloudquotas_v1.ListQuotaInfosRequest(
            parent=parent,
            page_size=page_size,
            page_token=page_token,
        )
        pager = await self._client.list_quota_infos(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = await anext(pager.pages)
        return QuotaInfoPage(tuple(response.quota_infos), response.next_page_token)

    async def quota_preferences(
        self,
        *,
        parent: str,
        page_size: int,
        page_token: str,
        timeout_seconds: float,
    ) -> QuotaPreferencePage:
        """Materialize exactly the requested QuotaPreference response page."""
        request = cloudquotas_v1.ListQuotaPreferencesRequest(
            parent=parent,
            page_size=page_size,
            page_token=page_token,
        )
        pager = await self._client.list_quota_preferences(
            request=request,
            retry=None,
            timeout=timeout_seconds,
        )
        response = await anext(pager.pages)
        return QuotaPreferencePage(
            tuple(response.quota_preferences), response.next_page_token
        )


class OfficialCloudQuotasObservationClient:
    """Keep exact GET requests, retry objects, and DTOs in the adapter."""

    def __init__(self, client: cloudquotas_v1.CloudQuotasAsyncClient) -> None:
        """Bind one shared-credential official async client."""
        self._client = client

    async def close(self) -> None:
        """Close the owned generated async client."""
        await self._client.transport.close()

    async def quota_preference(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaPreference:
        """Get one exact QuotaPreference without generated retry."""
        return await self._client.get_quota_preference(
            request=cloudquotas_v1.GetQuotaPreferenceRequest(name=name),
            retry=None,
            timeout=timeout_seconds,
        )

    async def quota_info(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaInfo:
        """Get one exact QuotaInfo without generated retry."""
        return await self._client.get_quota_info(
            request=cloudquotas_v1.GetQuotaInfoRequest(name=name),
            retry=None,
            timeout=timeout_seconds,
        )


class _CloudQuotasReader:
    def __init__(
        self,
        client: CloudQuotasPageClient,
        policy: GoogleReadPolicy,
        *,
        page_size: int = 100,
        maximum_pages: int = 100,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size < 1
        ):
            msg = "Cloud Quotas page_size must be positive"
            raise ValueError(msg)
        if (
            isinstance(maximum_pages, bool)
            or not isinstance(maximum_pages, int)
            or maximum_pages < 1
        ):
            msg = "Cloud Quotas maximum_pages must be positive"
            raise ValueError(msg)
        self._client = client
        self._policy = policy
        self._page_size = page_size
        self._maximum_pages = maximum_pages
        self._now = now


class GoogleEffectiveQuotaReader(_CloudQuotasReader):
    """Read and normalize complete effective QuotaInfo pages."""

    async def read(
        self, request: EffectiveQuotaReadRequest
    ) -> ProviderRead[EffectiveQuotaEvidence]:
        """List one exact service without inferring quota scope from global path."""
        if not isinstance(request, EffectiveQuotaReadRequest):
            msg = "effective quota reader requires EffectiveQuotaReadRequest"
            raise TypeError(msg)
        _require_service(request.service)
        parent = (
            f"{request.context.project.resource_scope.canonical_name}"
            f"/locations/global/services/{request.service}"
        )
        token = ""
        attempted = 0
        completed = 0
        values: list[EffectiveQuotaEvidence] = []
        diagnostics = []
        cap = False
        while attempted < self._maximum_pages:
            attempted += 1
            result = await self._policy.call(
                request.context,
                provider="cloud-quotas",
                phase="effective-quota-read",
                identity=f"quota-info:{parent}:{token}",
                dispatch=lambda timeout, page_token=token: self._client.quota_infos(
                    parent=parent,
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
                    values.extend(_map_quota_info(item, request))
                except (TypeError, ValueError, OverflowError):
                    diagnostics.append(
                        schema_diagnostic("effective-quota-read", "cloud-quotas")
                    )
            token = page.next_page_token
            if not token:
                break
        else:
            cap = bool(token)
        if cap:
            diagnostics.append(
                page_cap_diagnostic("effective-quota-read", "cloud-quotas")
            )
        return ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=self._now(),
            diagnostics=tuple(diagnostics),
        )


class GoogleQuotaPreferenceReader(_CloudQuotasReader):
    """Read and normalize existing QuotaPreference resources."""

    async def read(
        self, request: QuotaPreferenceReadRequest
    ) -> ProviderRead[QuotaPreferenceEvidence]:
        """List existing preferences separately from effective quota evidence."""
        if not isinstance(request, QuotaPreferenceReadRequest):
            msg = "preference reader requires QuotaPreferenceReadRequest"
            raise TypeError(msg)
        parent = (
            f"{request.context.project.resource_scope.canonical_name}/locations/global"
        )
        token = ""
        attempted = 0
        completed = 0
        values: list[QuotaPreferenceEvidence] = []
        diagnostics = []
        diagnostic_services: list[str | None] = []
        cap = False
        while attempted < self._maximum_pages:
            attempted += 1
            result = await self._policy.call(
                request.context,
                provider="cloud-quotas",
                phase="quota-preference-read",
                identity=f"quota-preference:{parent}:{token}",
                dispatch=lambda timeout, page_token=token: (
                    self._client.quota_preferences(
                        parent=parent,
                        page_size=self._page_size,
                        page_token=page_token,
                        timeout_seconds=timeout,
                    )
                ),
            )
            if result.diagnostic is not None:
                diagnostics.append(result.diagnostic)
                diagnostic_services.append(None)
                break
            page = result.value
            if page is None:
                msg = "successful page call must contain a page"
                raise RuntimeError(msg)
            completed += 1
            for item in page.items:
                mapped, attributed_service, invalid = _selected_preference(
                    item, request
                )
                if mapped is not None:
                    values.append(mapped)
                if invalid:
                    diagnostics.append(
                        schema_diagnostic("quota-preference-read", "cloud-quotas")
                    )
                    diagnostic_services.append(attributed_service)
            token = page.next_page_token
            if not token:
                break
        else:
            cap = bool(token)
        if cap:
            diagnostics.append(
                page_cap_diagnostic("quota-preference-read", "cloud-quotas")
            )
            diagnostic_services.append(None)
        return ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=self._now(),
            diagnostics=tuple(diagnostics),
            diagnostic_services=tuple(diagnostic_services),
        )


class GoogleWatchObservationReader:
    """Read one bound preference and its exact effective quota for Watch."""

    def __init__(
        self,
        client: CloudQuotasObservationClient,
        *,
        timeout_seconds: float = 20.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind exact read calls to finite transport and caller deadlines."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            msg = "Watch read timeout_seconds must be positive and finite"
            raise ValueError(msg)
        self._client = client
        self._timeout_seconds = float(timeout_seconds)
        self._now = now
        self._monotonic = monotonic

    async def observe(self, request: WatchObservationRequest) -> WatchObservation:
        """Return current lifecycle evidence for one exact accepted child."""
        if not isinstance(request, WatchObservationRequest):
            msg = "Watch reader requires WatchObservationRequest"
            raise TypeError(msg)
        child = request.child
        request.cancellation.raise_if_cancelled()
        remaining = self._remaining(request)
        preference = await _await_watch_read(
            self._client.quota_preference(
                name=child.preference_identity,
                timeout_seconds=remaining,
            ),
            request,
            remaining_seconds=remaining,
        )
        request.cancellation.raise_if_cancelled()
        remaining = self._remaining(request)
        info = await _await_watch_read(
            self._client.quota_info(
                name=_quota_info_name(child),
                timeout_seconds=remaining,
            ),
            request,
            remaining_seconds=remaining,
        )
        request.cancellation.raise_if_cancelled()
        return _watch_observation(preference, info, child, self._now())

    def _remaining(self, request: WatchObservationRequest) -> float:
        remaining = request.deadline - self._monotonic()
        if remaining <= 0:
            raise CoordinationDeadlineExceededError
        return min(self._timeout_seconds, remaining)


async def _await_watch_read[ValueT](
    work: Awaitable[ValueT],
    request: WatchObservationRequest,
    *,
    remaining_seconds: float,
) -> ValueT:
    work_task = asyncio.ensure_future(work)
    cancellation_task = asyncio.create_task(request.cancellation.wait())
    tasks = (work_task, cancellation_task)
    try:
        done, _ = await asyncio.wait(
            tasks,
            timeout=remaining_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            raise CoordinationCancelledError
        if work_task not in done:
            raise CoordinationDeadlineExceededError
        try:
            return await work_task
        except (
            google_exceptions.DeadlineExceeded,
            google_exceptions.InternalServerError,
            google_exceptions.ResourceExhausted,
            google_exceptions.ServiceUnavailable,
            google_exceptions.TooManyRequests,
        ) as error:
            raise WatchObservationTransientError(
                _provider_retry_after_seconds(error)
            ) from None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _provider_retry_after_seconds(
    error: google_exceptions.GoogleAPICallError,
) -> float | None:
    """Extract bounded numeric RetryInfo or Retry-After guidance when present."""
    for detail in error.details:
        retry_delay = getattr(detail, "retry_delay", None)
        seconds = getattr(retry_delay, "seconds", None)
        nanos = getattr(retry_delay, "nanos", None)
        if isinstance(seconds, int) and isinstance(nanos, int):
            delay = float(seconds) + (float(nanos) / 1_000_000_000)
            if math.isfinite(delay) and delay >= 0:
                return delay
    response = error.response
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get("Retry-After")
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return None
        if math.isfinite(delay) and delay >= 0:
            return delay
    return None


def _map_quota_info(
    info: cloudquotas_v1.QuotaInfo,
    request: EffectiveQuotaReadRequest,
) -> list[EffectiveQuotaEvidence]:
    scope = request.context.project.resource_scope.canonical_name
    expected_name = (
        f"{scope}/locations/global/services/{info.service}/quotaInfos/{info.quota_id}"
    )
    info_pb = cloudquotas_v1.QuotaInfo.pb(info)
    if (
        info.service != request.service
        or info.name != expected_name
        or not info_pb.HasField("quota_increase_eligibility")
        or not info.dimensions_infos
    ):
        msg = "QuotaInfo identity does not match request"
        raise ValueError(msg)
    reason = _ineligibility_symbol(info.quota_increase_eligibility)
    eligibility = QuotaIncreaseEligibility(
        eligible=info.quota_increase_eligibility.is_eligible,
        reason=reason,
    )
    declared_dimensions = tuple(info.dimensions)
    declared_dimension_set = set(declared_dimensions)
    if any(not dimension for dimension in declared_dimensions) or len(
        declared_dimension_set
    ) != len(declared_dimensions):
        msg = "QuotaInfo declared dimensions are invalid"
        raise ValueError(msg)
    results = []
    for dimensions_info in info.dimensions_infos:
        dimensions_pb = cloudquotas_v1.DimensionsInfo.pb(dimensions_info)
        dimension_keys = set(dimensions_info.dimensions)
        applicable_locations = tuple(dimensions_info.applicable_locations)
        if (
            not dimensions_pb.HasField("details")
            or not applicable_locations
            or any(not location for location in applicable_locations)
            or not dimension_keys.issubset(declared_dimension_set)
        ):
            msg = "QuotaInfo dimension slice requires details"
            raise ValueError(msg)
        dimensions = NormalizedDimensions(dimensions_info.dimensions.items())
        identity = EffectiveQuotaSliceIdentity(
            resource_scope=request.context.project.resource_scope,
            service=info.service,
            quota_id=info.quota_id,
            dimensions=dimensions,
            quota_scope=_quota_scope(dimensions, applicable_locations),
        )
        results.append(
            EffectiveQuotaEvidence(
                identity=identity,
                effective_value=QuotaQuantity(
                    dimensions_info.details.value,
                    QuotaUnit(info.metric_unit),
                ),
                metric=info.metric,
                declared_dimensions=declared_dimensions,
                applicable_locations=applicable_locations,
                eligibility=eligibility,
                fixed=info.is_fixed,
                concurrent=info.is_concurrent,
                precise=info.is_precise,
                refresh_interval=info.refresh_interval or None,
                ongoing_rollout=dimensions_info.details.rollout_info.ongoing_rollout,
                container_type=_container_symbol(info),
                metric_display_name=info.metric_display_name or None,
                quota_display_name=info.quota_display_name or None,
            )
        )
    return results


def _quota_info_name(child: WatchChildIdentity) -> str:
    identity = child.slice_identity
    return (
        f"{identity.resource_scope.canonical_name}/locations/global"
        f"/services/{identity.service}/quotaInfos/{identity.quota_id}"
    )


def _watch_observation(
    preference: cloudquotas_v1.QuotaPreference,
    info: cloudquotas_v1.QuotaInfo,
    child: WatchChildIdentity,
    effective_observed_at: datetime,
) -> WatchObservation:
    preference_pb = cloudquotas_v1.QuotaPreference.pb(preference)
    if (
        preference.name != child.preference_identity
        or preference.service != child.slice_identity.service
        or preference.quota_id != child.slice_identity.quota_id
        or NormalizedDimensions(preference.dimensions.items())
        != child.slice_identity.dimensions
        or not preference_pb.HasField("quota_config")
        or preference.quota_config.preferred_value != child.target.value
    ):
        msg = "Watch QuotaPreference does not match the bound child"
        raise ValueError(msg)
    status_observed_at = _timestamp(preference, "update_time")
    if status_observed_at is None:
        msg = "Watch QuotaPreference requires an update timestamp"
        raise ValueError(msg)
    config_pb = cloudquotas_v1.QuotaConfig.pb(preference.quota_config)
    granted = (
        QuotaQuantity(
            cast("int", preference.quota_config.granted_value),
            child.target.unit,
        )
        if config_pb.HasField("granted_value")
        else None
    )
    status = QuotaRequestStatus.derive(
        reconciliation=(
            Reconciliation.RECONCILING
            if preference.reconciling
            else (
                Reconciliation.SETTLED
                if granted is not None
                else Reconciliation.UNKNOWN
            )
        ),
        baseline=child.baseline,
        desired=child.target,
        granted=granted,
        effective=_watch_effective_value(info, child),
        status_observed_at=status_observed_at,
        effective_observed_at=effective_observed_at,
    )
    return WatchObservation(
        status=status,
        preference_target=child.target,
        etag=preference.etag or None,
        trace_id=preference.quota_config.trace_id or None,
        observed_at=effective_observed_at,
    )


def _watch_effective_value(
    info: cloudquotas_v1.QuotaInfo,
    child: WatchChildIdentity,
) -> QuotaQuantity:
    identity = child.slice_identity
    declared_dimensions = tuple(info.dimensions)
    declared_dimension_set = set(declared_dimensions)
    if (
        info.name != _quota_info_name(child)
        or info.service != identity.service
        or info.quota_id != identity.quota_id
        or info.metric_unit != child.target.unit.symbol
        or any(not dimension for dimension in declared_dimensions)
        or len(declared_dimension_set) != len(declared_dimensions)
        or not {key for key, _ in identity.dimensions.items}.issubset(
            declared_dimension_set
        )
    ):
        msg = "Watch QuotaInfo does not match the bound child"
        raise ValueError(msg)
    matches: list[QuotaQuantity] = []
    for item in info.dimensions_infos:
        item_pb = cloudquotas_v1.DimensionsInfo.pb(item)
        dimensions = NormalizedDimensions(item.dimensions.items())
        locations = tuple(item.applicable_locations)
        if (
            dimensions == identity.dimensions
            and _quota_scope(dimensions, locations) is identity.quota_scope
            and item_pb.HasField("details")
        ):
            matches.append(QuotaQuantity(item.details.value, child.target.unit))
    if len(matches) != 1:
        msg = "Watch requires one exact effective quota slice"
        raise ValueError(msg)
    return matches[0]


def _map_preference(
    preference: cloudquotas_v1.QuotaPreference,
    request: QuotaPreferenceReadRequest,
) -> QuotaPreferenceEvidence:
    scope = request.context.project.resource_scope.canonical_name
    prefix = f"{scope}/locations/global/quotaPreferences/"
    preference_id = preference.name.removeprefix(prefix)
    preference_pb = cloudquotas_v1.QuotaPreference.pb(preference)
    if (
        not preference.name.startswith(prefix)
        or not preference_id
        or "/" in preference_id
        or not preference_pb.HasField("quota_config")
    ):
        msg = "QuotaPreference identity does not match request"
        raise ValueError(msg)
    dimensions = NormalizedDimensions(preference.dimensions.items())
    config_pb = cloudquotas_v1.QuotaConfig.pb(preference.quota_config)
    granted = (
        config_pb.granted_value.value if config_pb.HasField("granted_value") else None
    )
    return QuotaPreferenceEvidence(
        provider_name=preference.name,
        identity=EffectiveQuotaSliceIdentity(
            resource_scope=request.context.project.resource_scope,
            service=preference.service,
            quota_id=preference.quota_id,
            dimensions=dimensions,
            quota_scope=_quota_scope(dimensions),
        ),
        preferred_value=preference.quota_config.preferred_value,
        granted_value=granted,
        etag=preference.etag or None,
        reconciling=preference.reconciling,
        state_detail=preference.quota_config.state_detail or None,
        trace_id=preference.quota_config.trace_id or None,
        create_time=_timestamp(preference, "create_time"),
        update_time=_timestamp(preference, "update_time"),
        request_origin=_origin_symbol(preference.quota_config),
    )


def _selected_preference(
    preference: cloudquotas_v1.QuotaPreference,
    request: QuotaPreferenceReadRequest,
) -> tuple[QuotaPreferenceEvidence | None, str | None, bool]:
    """Map one selected partition or return its attributable schema failure."""
    try:
        _require_service(preference.service)
    except ValueError:
        attributed_service = None
    else:
        attributed_service = preference.service
    if (
        request.services
        and attributed_service is not None
        and attributed_service not in request.services
    ):
        return None, None, False
    try:
        return _map_preference(preference, request), attributed_service, False
    except (TypeError, ValueError, OverflowError):
        return None, attributed_service, True


def _timestamp(
    preference: cloudquotas_v1.QuotaPreference,
    field: str,
) -> datetime | None:
    pb = cloudquotas_v1.QuotaPreference.pb(preference)
    if not pb.HasField(field):
        return None
    timestamp = getattr(pb, field)
    return timestamp.ToDatetime(tzinfo=UTC)


def _ineligibility_symbol(
    message: cloudquotas_v1.QuotaIncreaseEligibility,
) -> ProviderSymbol[QuotaIneligibilityReason]:
    pb = cloudquotas_v1.QuotaIncreaseEligibility.pb(message)
    field = "ineligibility_reason"
    descriptor = pb.DESCRIPTOR.fields_by_name[field].enum_type
    number = pb.ineligibility_reason
    value = descriptor.values_by_number.get(number)
    raw = value.name if value is not None else f"UNRECOGNIZED_{number}"
    return ProviderSymbol(raw, QuotaIneligibilityReason)


def _origin_symbol(
    message: cloudquotas_v1.QuotaConfig,
) -> ProviderSymbol[QuotaPreferenceOrigin]:
    pb = cloudquotas_v1.QuotaConfig.pb(message)
    field = "request_origin"
    descriptor = pb.DESCRIPTOR.fields_by_name[field].enum_type
    number = pb.request_origin
    value = descriptor.values_by_number.get(number)
    raw = value.name if value is not None else f"UNRECOGNIZED_{number}"
    return ProviderSymbol(raw, QuotaPreferenceOrigin)


def _container_symbol(
    message: cloudquotas_v1.QuotaInfo,
) -> ProviderSymbol[QuotaContainerType]:
    pb = cloudquotas_v1.QuotaInfo.pb(message)
    field = "container_type"
    descriptor = pb.DESCRIPTOR.fields_by_name[field].enum_type
    number = pb.container_type
    value = descriptor.values_by_number.get(number)
    raw = value.name if value is not None else f"UNRECOGNIZED_{number}"
    return ProviderSymbol(raw, QuotaContainerType)


def _quota_scope(
    dimensions: NormalizedDimensions,
    applicable_locations: tuple[str, ...] = (),
) -> QuotaScope:
    keys = {key for key, _ in dimensions.items}
    if "zone" in keys:
        return QuotaScope.ZONAL
    if "region" in keys:
        return QuotaScope.REGIONAL
    if applicable_locations == ("global",):
        return QuotaScope.GLOBAL
    return QuotaScope.UNKNOWN


def _require_service(service: object) -> None:
    if (
        not isinstance(service, str)
        or not service.isascii()
        or service != service.lower()
    ):
        msg = "service must be a canonical lowercase service DNS name"
        raise ValueError(msg)
    labels = service.split(".")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    minimum_labels = 2
    if len(labels) < minimum_labels or any(
        not label
        or label.startswith("-")
        or label.endswith("-")
        or any(character not in allowed for character in label)
        for label in labels
    ):
        msg = "service must be a canonical lowercase service DNS name"
        raise ValueError(msg)
