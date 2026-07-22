"""Read-only official Cloud Quotas adapters with normalized evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from google.cloud import cloudquotas_v1

from cqmgr.adapters.google.read_policy import (
    GoogleReadPolicy,
    page_cap_diagnostic,
    schema_diagnostic,
)
from cqmgr.application.ports.provider_reads import (
    EffectiveQuotaReadRequest,
    QuotaPreferenceReadRequest,
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

if TYPE_CHECKING:
    from collections.abc import Callable


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


class OfficialCloudQuotasPageClient:
    """Keep official requests, pagers, retry objects, and DTOs in the adapter."""

    def __init__(self, client: cloudquotas_v1.CloudQuotasAsyncClient) -> None:
        """Bind one shared-credential official async client."""
        self._client = client

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
                break
            page = result.value
            if page is None:
                msg = "successful page call must contain a page"
                raise RuntimeError(msg)
            completed += 1
            for item in page.items:
                try:
                    values.append(_map_preference(item, request))
                except (TypeError, ValueError, OverflowError):
                    diagnostics.append(
                        schema_diagnostic("quota-preference-read", "cloud-quotas")
                    )
            token = page.next_page_token
            if not token:
                break
        else:
            cap = bool(token)
        if cap:
            diagnostics.append(
                page_cap_diagnostic("quota-preference-read", "cloud-quotas")
            )
        return ProviderRead(
            values=tuple(values),
            coverage=ProviderReadCoverage(attempted, completed, cap),
            observed_at=self._now(),
            diagnostics=tuple(diagnostics),
        )


def _map_quota_info(
    info: cloudquotas_v1.QuotaInfo,
    request: EffectiveQuotaReadRequest,
) -> list[EffectiveQuotaEvidence]:
    if info.service != request.service or not info.name.startswith(
        f"{request.context.project.resource_scope.canonical_name}/locations/global/"
    ):
        msg = "QuotaInfo identity does not match request"
        raise ValueError(msg)
    reason = _ineligibility_symbol(info.quota_increase_eligibility)
    eligibility = QuotaIncreaseEligibility(
        eligible=info.quota_increase_eligibility.is_eligible,
        reason=reason,
    )
    results = []
    for dimensions_info in info.dimensions_infos:
        dimensions = NormalizedDimensions(dimensions_info.dimensions.items())
        identity = EffectiveQuotaSliceIdentity(
            resource_scope=request.context.project.resource_scope,
            service=info.service,
            quota_id=info.quota_id,
            dimensions=dimensions,
            quota_scope=_quota_scope(dimensions),
        )
        results.append(
            EffectiveQuotaEvidence(
                identity=identity,
                effective_value=QuotaQuantity(
                    dimensions_info.details.value,
                    QuotaUnit(info.metric_unit),
                ),
                metric=info.metric,
                declared_dimensions=tuple(info.dimensions),
                applicable_locations=tuple(dimensions_info.applicable_locations),
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


def _map_preference(
    preference: cloudquotas_v1.QuotaPreference,
    request: QuotaPreferenceReadRequest,
) -> QuotaPreferenceEvidence:
    scope = request.context.project.resource_scope.canonical_name
    prefix = f"{scope}/locations/global/"
    if not preference.name.startswith(prefix):
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


def _quota_scope(dimensions: NormalizedDimensions) -> QuotaScope:
    keys = {key for key, _ in dimensions.items}
    if "zone" in keys:
        return QuotaScope.ZONAL
    if "region" in keys:
        return QuotaScope.REGIONAL
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
