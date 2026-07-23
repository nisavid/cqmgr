"""Google Compute Preview Spot advice REST adapters."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Protocol, cast

from google.api_core import exceptions as google_exceptions
from google.auth.transport.requests import AuthorizedSession
from requests import HTTPError

from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.obtainability import (
    AdviceShard,
    CapacityAdvice,
    CapacityHistory,
    PreemptionInterval,
    PriceInterval,
)
from cqmgr.domain.quotas import ProviderRead, ProviderReadCoverage
from cqmgr.domain.redaction import RedactedText

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from cqmgr.adapters.google.read_policy import GoogleReadPolicy
    from cqmgr.application.ports.obtainability import (
        CapacityAdviceReadRequest,
        CapacityHistoryReadRequest,
    )

_PROVIDER = "compute-spot-advice"
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_CAPACITY_SOURCE = (
    "https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacity"
)
_HISTORY_SOURCE = (
    "https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacityHistory"
)


class CapacityAdviceJsonClient(Protocol):
    """Narrow beta REST surface because the generated v1 client omits these methods."""

    async def capacity(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        """POST one current advice request."""
        ...

    async def capacity_history(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        """POST one history request."""
        ...


class JsonCapacityAdviceClient:
    """Shared JSON validation helper used by production and public-schema fixtures."""

    @staticmethod
    def load_fixture(path: Path) -> dict[str, object]:
        """Load one checked-in secret-free public-schema fixture."""
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            msg = "Spot advice fixture must contain a JSON object"
            raise TypeError(msg)
        return cast("dict[str, object]", value)


class OfficialCapacityAdviceJsonClient:
    """Call the official beta REST surface through Google Auth transport."""

    def __init__(self, credential: object) -> None:
        """Bind one already resolved shared credential without retaining responses."""
        self._session = AuthorizedSession(credential)  # type: ignore[arg-type]

    async def capacity(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        """POST one capacity request with a finite timeout."""
        return await self._post(
            f"https://compute.googleapis.com/compute/beta/projects/{project}/"
            f"regions/{region}/advice/capacity",
            body,
            timeout_seconds,
        )

    async def capacity_history(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        """POST one capacity-history request with a finite timeout."""
        return await self._post(
            f"https://compute.googleapis.com/compute/beta/projects/{project}/"
            f"regions/{region}/advice/capacityHistory",
            body,
            timeout_seconds,
        )

    async def _post(
        self,
        url: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        def dispatch() -> Mapping[str, object]:
            response = self._session.post(url, json=body, timeout=timeout_seconds)
            try:
                response.raise_for_status()
            except HTTPError as error:
                error_response = (
                    error.response if error.response is not None else response
                )
                status_code = error_response.status_code
                msg = f"Compute advice request failed with HTTP {status_code}."
                if status_code == _HTTP_UNAUTHORIZED:
                    translated = google_exceptions.Unauthenticated(
                        msg,
                        response=error_response,
                    )
                elif status_code == _HTTP_FORBIDDEN:
                    translated = google_exceptions.PermissionDenied(
                        msg,
                        response=error_response,
                    )
                else:
                    translated = google_exceptions.from_http_status(
                        status_code,
                        msg,
                        response=error_response,
                    )
                raise translated from error
            value = response.json()
            if not isinstance(value, dict):
                msg = "Compute advice response must contain a JSON object"
                raise TypeError(msg)
            return cast("Mapping[str, object]", value)

        return await asyncio.to_thread(dispatch)

    def close(self) -> None:
        """Close the owned authenticated HTTP session."""
        self._session.close()


class GoogleCapacityAdviceReader:
    """Normalize one current capacity recommendation at the read port."""

    def __init__(
        self,
        client: CapacityAdviceJsonClient,
        policy: GoogleReadPolicy,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the beta REST client, shared read policy, and retrieval clock."""
        self._client = client
        self._policy = policy
        self._clock = clock

    async def read(
        self,
        request: CapacityAdviceReadRequest,
    ) -> ProviderRead[CapacityAdvice]:
        """Preserve exact request identity and normalize one provider recommendation."""
        candidate = request.candidate
        body = _capacity_body(candidate)
        result = await self._policy.call(
            request.context,
            provider=_PROVIDER,
            phase="capacity-advice",
            identity=candidate.candidate_id,
            dispatch=lambda timeout: self._client.capacity(
                project=request.context.project.project_id,
                region=candidate.endpoint_region,
                body=body,
                timeout_seconds=timeout,
            ),
        )
        observed_at = self._clock()
        if result.value is None:
            diagnostic = _required_diagnostic(result.diagnostic)
            return ProviderRead(
                (),
                ProviderReadCoverage(1, 0),
                observed_at,
                (diagnostic,),
                ("compute.googleapis.com",),
            )
        try:
            advice = _parse_capacity(result.value, observed_at)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            diagnostic = _invalid_evidence(
                "capacity-advice",
                "invalid-capacity-advice-evidence",
            )
            return ProviderRead(
                (),
                ProviderReadCoverage(1, 0),
                observed_at,
                (diagnostic,),
                ("compute.googleapis.com",),
            )
        return ProviderRead(
            (advice,),
            ProviderReadCoverage(1, 1),
            observed_at,
        )


class GoogleCapacityHistoryReader:
    """Normalize one regional or zonal capacity-history response."""

    def __init__(
        self,
        client: CapacityAdviceJsonClient,
        policy: GoogleReadPolicy,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the independently injectable history port."""
        self._client = client
        self._policy = policy
        self._clock = clock

    async def read(
        self,
        request: CapacityHistoryReadRequest,
    ) -> ProviderRead[CapacityHistory]:
        """Request only documented history categories for one exact location."""
        candidate = request.candidate
        body = _history_body(request)
        result = await self._policy.call(
            request.context,
            provider=_PROVIDER,
            phase="capacity-history",
            identity=f"{candidate.candidate_id}:{request.location}",
            dispatch=lambda timeout: self._client.capacity_history(
                project=request.context.project.project_id,
                region=candidate.endpoint_region,
                body=body,
                timeout_seconds=timeout,
            ),
        )
        observed_at = self._clock()
        if result.value is None:
            diagnostic = _required_diagnostic(result.diagnostic)
            return ProviderRead(
                (),
                ProviderReadCoverage(1, 0),
                observed_at,
                (diagnostic,),
                ("compute.googleapis.com",),
            )
        try:
            history = _parse_history(result.value, observed_at)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            diagnostic = _invalid_evidence(
                "capacity-history",
                "invalid-capacity-history-evidence",
            )
            return ProviderRead(
                (),
                ProviderReadCoverage(1, 0),
                observed_at,
                (diagnostic,),
                ("compute.googleapis.com",),
            )
        return ProviderRead(
            (history,),
            ProviderReadCoverage(1, 1),
            observed_at,
        )


def _capacity_body(candidate: object) -> dict[str, object]:
    machine = candidate.machine  # type: ignore[union-attr]
    selection: dict[str, object] = {
        "machineTypes": [machine.machine_type],
        "guestAccelerators": (
            []
            if machine.gpu is None
            else [
                {
                    "acceleratorType": machine.gpu.accelerator_type,
                    "acceleratorCount": machine.gpu.count,
                }
            ]
        ),
        "disks": [{"type": "SCRATCH"}] * machine.local_ssd_count,
    }
    return {
        "instanceProperties": {"scheduling": {"provisioningModel": "SPOT"}},
        "instanceFlexibilityPolicy": {
            "instanceSelections": {"cqmgr-selection": selection}
        },
        "size": candidate.vm_count,  # type: ignore[union-attr]
        "distributionPolicy": {
            "zones": [
                {"zone": f"zones/{zone}"}
                for zone in candidate.zones  # type: ignore[union-attr]
            ],
            "targetShape": candidate.distribution_shape.provider_value,  # type: ignore[union-attr]
        },
    }


def _history_body(request: CapacityHistoryReadRequest) -> dict[str, object]:
    regional = request.location == request.candidate.endpoint_region
    body: dict[str, object] = {
        "instanceProperties": {
            "machineType": request.candidate.machine.machine_type,
            "scheduling": {"provisioningModel": "SPOT"},
        },
        "types": ["PREEMPTION", *(("PRICE",) if request.include_price else ())],
    }
    if not regional:
        body["locationPolicy"] = {"location": f"zones/{request.location}"}
    return body


def _parse_capacity(
    value: Mapping[str, object],
    observed_at: datetime,
) -> CapacityAdvice:
    recommendations = _list(value["recommendations"])
    if len(recommendations) != 1:
        msg = "capacity advice must contain exactly one initial recommendation"
        raise ValueError(msg)
    recommendation = _mapping(recommendations[0])
    scores = _mapping(recommendation["scores"])
    shards = tuple(
        AdviceShard(
            zone=_basename(_str(item, "zone")),
            machine_type=_basename(_str(item, "machineType")),
            vm_count=_int(item, "instanceCount"),
            provisioning_model=_str(item, "provisioningModel"),
        )
        for item in map(_mapping, _list(recommendation.get("shards", [])))
    )
    return CapacityAdvice(
        Decimal(str(scores["obtainability"])),
        _str(scores, "estimatedUptime"),
        shards,
        observed_at,
        source=_CAPACITY_SOURCE,
    )


def _parse_history(
    value: Mapping[str, object],
    observed_at: datetime,
) -> CapacityHistory:
    preemption = tuple(
        PreemptionInterval(
            _timestamp(_mapping(item["interval"])["startTime"]),
            _timestamp(_mapping(item["interval"])["endTime"]),
            Decimal(str(item["preemptionRate"])),
        )
        for item in map(_mapping, _list(value.get("preemptionHistory", [])))
    )
    prices = tuple(
        PriceInterval(
            _timestamp(_mapping(item["interval"])["startTime"]),
            _timestamp(_mapping(item["interval"])["endTime"]),
            _money(_mapping(item["listPrice"])),
        )
        for item in map(_mapping, _list(value.get("priceHistory", [])))
    )
    return CapacityHistory(
        machine_type=_basename(_str(value, "machineType")),
        location=_basename(_str(value, "location")),
        preemption=preemption,
        prices=prices,
        retrieved_at=observed_at,
        source=_HISTORY_SOURCE,
    )


def _money(value: Mapping[str, object]) -> Decimal:
    if _str(value, "currencyCode") != "USD":
        msg = "capacity history price must use USD"
        raise ValueError(msg)
    units = Decimal(_str(value, "units"))
    nanos = _int(value, "nanos")
    return units + (Decimal(nanos) / Decimal(1_000_000_000))


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        msg = "provider timestamp must be a string"
        raise TypeError(msg)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        msg = "provider timestamp must include an offset"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        msg = "provider JSON field must be an object"
        raise TypeError(msg)
    return cast("Mapping[str, object]", value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        msg = "provider JSON field must be an array"
        raise TypeError(msg)
    return value


def _str(value: Mapping[str, object], key: str) -> str:
    result = value[key]
    if not isinstance(result, str) or not result:
        msg = f"provider {key} must be a non-empty string"
        raise TypeError(msg)
    return result


def _int(value: Mapping[str, object], key: str) -> int:
    result = value[key]
    if isinstance(result, bool) or not isinstance(result, int):
        msg = f"provider {key} must be an integer"
        raise TypeError(msg)
    return result


def _basename(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def _required_diagnostic(value: Diagnostic | None) -> Diagnostic:
    if value is None:
        msg = "failed provider call must contain one normalized diagnostic"
        raise AssertionError(msg)
    return value


def _invalid_evidence(phase: str, code: str) -> Diagnostic:
    return Diagnostic(
        DiagnosticCode(code),
        Severity.ERROR,
        DiagnosticPhase(phase),
        DiagnosticSource(_PROVIDER),
        RetryDisposition.AFTER_REFRESH,
        RedactedText("Compute returned invalid Spot advice evidence; retry later."),
    )
