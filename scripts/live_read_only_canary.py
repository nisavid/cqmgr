"""Run bounded, allowlisted provider reads and retain only sanitized evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

CANARY_SCHEMA = "cqmgr.live-read-only-evidence/v1"
PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
SERVICES = ("compute.googleapis.com", "tpu.googleapis.com")
MONITORING_METRICS = (
    "serviceruntime.googleapis.com/quota/allocation/usage",
    "serviceruntime.googleapis.com/quota/rate/net_usage",
)
MINIMUM_REQUEST_TIMEOUT_SECONDS = 0.1
ALLOWED_GET_PATHS = frozenset(
    {
        "/v3/projects/{project}",
        "/v1/projects/{project}/locations/global/services/{service}/quotaInfos",
        "/v1/projects/{project}/locations/global/quotaPreferences",
        "/v3/projects/{project}/timeSeries",
        "/compute/v1/projects/{project}/aggregated/acceleratorTypes",
        "/compute/v1/projects/{project}/aggregated/machineTypes",
        "/v2/projects/{project}/locations",
        "/v2/projects/{project}/locations/{location}/acceleratorTypes",
        "/v2/projects/{project}/locations/{location}/runtimeVersions",
    }
)


class ResponseLike(Protocol):
    """Minimal requests response contract used by the canary."""

    def raise_for_status(self) -> None:
        """Raise when the provider did not accept the read."""

    def json(self) -> object:
        """Return one decoded provider response."""


class SessionLike(Protocol):
    """Minimal authorized transport contract used by the canary."""

    def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> ResponseLike:
        """Dispatch one allowlisted request."""


class QuotaProjectSession:
    """Attach the explicit quota project without changing request authority."""

    def __init__(self, session: SessionLike, project: str) -> None:
        """Retain one transport and its explicit quota-project header."""
        self._session = session
        self._headers = {"x-goog-user-project": project}

    def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> ResponseLike:
        """Delegate one request with the exact quota-project header."""
        if "headers" in kwargs:
            msg = "canary callers cannot replace the quota-project header"
            raise ValueError(msg)
        return self._session.request(method, url, headers=self._headers, **kwargs)


class RequestBudgetError(RuntimeError):
    """One global request or wall-clock bound stopped canary fanout."""

    def __init__(self, reason: str, message: str) -> None:
        """Retain a public reason code without provider or project values."""
        super().__init__(message)
        self.reason = reason


class SourceLimitError(RuntimeError):
    """One per-source bound stopped canary fanout with sanitized evidence."""

    def __init__(self, message: str, evidence: dict[str, object]) -> None:
        """Retain only public path shape, counts, and safe aggregate evidence."""
        super().__init__(message)
        self.evidence = evidence


@dataclass(slots=True)
class RequestBudget:
    """Enforce one request and wall-clock budget across the complete canary."""

    max_requests: int
    max_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    requests: int = field(init=False, default=0)
    _started: float = field(init=False)

    def __post_init__(self) -> None:
        """Validate limits and bind the global monotonic deadline."""
        if self.max_requests < 1:
            msg = "global request limit must be positive"
            raise ValueError(msg)
        if self.max_seconds <= 0:
            msg = "global wall-clock limit must be positive"
            raise ValueError(msg)
        self._started = self.monotonic()

    def claim_timeout(self, request_timeout: float) -> float:
        """Claim one request and cap its timeout at the remaining deadline."""
        if request_timeout < MINIMUM_REQUEST_TIMEOUT_SECONDS:
            msg = (
                "request timeout must be at least "
                f"{MINIMUM_REQUEST_TIMEOUT_SECONDS} seconds"
            )
            raise ValueError(msg)
        remaining = self.max_seconds - (self.monotonic() - self._started)
        if remaining < MINIMUM_REQUEST_TIMEOUT_SECONDS:
            msg = "canary exceeded its global wall-clock deadline"
            reason = "wall-clock-deadline"
            raise RequestBudgetError(reason, msg)
        if self.requests >= self.max_requests:
            msg = "canary exceeded its global request limit"
            reason = "request-limit"
            raise RequestBudgetError(reason, msg)
        self.requests += 1
        return min(request_timeout, remaining)

    def observed_elapsed_ms(self) -> int:
        """Return sanitized elapsed time even when reporting an exceeded bound."""
        return round((self.monotonic() - self._started) * 1000)

    def elapsed_ms(self) -> int:
        """Return elapsed time only while the global deadline remains valid."""
        elapsed = self.monotonic() - self._started
        if elapsed > self.max_seconds:
            msg = "canary exceeded its global wall-clock deadline"
            reason = "wall-clock-deadline"
            raise RequestBudgetError(reason, msg)
        return round(elapsed * 1000)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def require_allowlisted(method: str, path_template: str) -> None:
    """Reject any request that is not one exact ordinary-canary GET."""
    if method != "GET" or path_template not in ALLOWED_GET_PATHS:
        msg = (
            f"request is outside the live read-only allowlist: {method} {path_template}"
        )
        raise ValueError(msg)


def _response_mapping(response: ResponseLike) -> dict[str, object]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        msg = "provider read returned a non-object response"
        raise TypeError(msg)
    return cast("dict[str, object]", payload)


def _records(
    value: object,
    item_key: str,
    *,
    nested_key: str | None = None,
) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        if nested_key is not None:
            records: list[object] = []
            for wrapper in value.values():
                if not isinstance(wrapper, dict):
                    continue
                nested = wrapper.get(nested_key)
                if isinstance(nested, list):
                    records.extend(nested)
            return tuple(records)
        return tuple(value.values())
    msg = f"provider field {item_key!r} is neither a list nor an object"
    raise RuntimeError(msg)


def read_pages(  # noqa: PLR0913 - one explicit bounded request contract
    session: SessionLike,
    *,
    method: str,
    path_template: str,
    url: str,
    item_key: str,
    params: Mapping[str, str],
    max_pages: int,
    timeout: float,
    budget: RequestBudget,
    nested_key: str | None = None,
    collect_records: bool = False,
) -> tuple[dict[str, object], tuple[object, ...]]:
    """Return evidence and only records explicitly requested by the caller."""
    require_allowlisted(method, path_template)
    if max_pages < 1:
        msg = "page limit must be positive"
        raise ValueError(msg)
    request_params = dict(params)
    records: list[object] = []
    record_count = 0
    digests: list[str] = []
    started = time.monotonic()
    for page in range(1, max_pages + 1):
        response = session.request(
            method,
            url,
            params=request_params,
            timeout=budget.claim_timeout(timeout),
        )
        payload = _response_mapping(response)
        page_records = _records(
            payload.get(item_key),
            item_key,
            nested_key=nested_key,
        )
        record_count += len(page_records)
        if collect_records:
            records.extend(page_records)
        digests.append(hashlib.sha256(_canonical_json(payload)).hexdigest())
        token = payload.get("nextPageToken")
        if token in (None, ""):
            elapsed = time.monotonic() - started
            evidence: dict[str, object] = {
                "complete": True,
                "digest": "sha256:"
                + hashlib.sha256("".join(digests).encode()).hexdigest(),
                "elapsed_ms": round(elapsed * 1000),
                "method": method,
                "pages": page,
                "path": path_template,
                "records": record_count,
            }
            return evidence, tuple(records)
        if not isinstance(token, str):
            msg = "provider page token must be a string"
            raise TypeError(msg)
        request_params["pageToken"] = token
    elapsed = time.monotonic() - started
    evidence = {
        "complete": False,
        "digest": "sha256:" + hashlib.sha256("".join(digests).encode()).hexdigest(),
        "elapsed_ms": round(elapsed * 1000),
        "method": method,
        "pages": max_pages,
        "path": path_template,
        "reason": "page-limit",
        "records": record_count,
    }
    msg = f"provider source exceeded page limit {max_pages}: {path_template}"
    raise SourceLimitError(msg, evidence)


def read_once(  # noqa: PLR0913 - one explicit bounded request contract
    session: SessionLike,
    *,
    path_template: str,
    url: str,
    params: Mapping[str, str],
    timeout: float,
    budget: RequestBudget,
) -> dict[str, object]:
    """Read one non-pageable source and retain only shape and digest evidence."""
    require_allowlisted("GET", path_template)
    started = time.monotonic()
    payload = _response_mapping(
        session.request(
            "GET",
            url,
            params=dict(params),
            timeout=budget.claim_timeout(timeout),
        )
    )
    return {
        "complete": True,
        "digest": "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest(),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "method": "GET",
        "pages": 1,
        "path": path_template,
        "records": 1,
    }


def _location_ids(
    records: tuple[object, ...],
    *,
    max_locations: int,
) -> tuple[str, ...]:
    if max_locations < 1:
        msg = "TPU location limit must be positive"
        raise ValueError(msg)
    locations: set[str] = set()
    for value in records:
        if not isinstance(value, dict):
            msg = "TPU location record must be an object"
            raise TypeError(msg)
        location = value.get("locationId")
        if not isinstance(location, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)+",
            location,
        ):
            msg = "TPU location record has an invalid location ID"
            raise RuntimeError(msg)
        locations.add(location)
        if len(locations) > max_locations:
            evidence: dict[str, object] = {
                "complete": False,
                "method": None,
                "pages": 0,
                "path": "/v2/projects/{project}/locations",
                "reason": "location-limit",
                "records": len(locations),
            }
            msg = f"TPU location source exceeded location limit {max_locations}"
            raise SourceLimitError(msg, evidence)
    return tuple(sorted(locations))


def _run_canary_sources(  # noqa: PLR0913 - explicit bounded source composition
    session: SessionLike,
    project: str,
    *,
    max_pages: int,
    timeout: float,
    max_locations: int,
    budget: RequestBudget,
    sources: list[dict[str, object]],
    state: dict[str, int],
) -> dict[str, object]:
    """Read every ordinary-canary source under one shared budget."""
    quota_session = QuotaProjectSession(session, project)
    project_path = "/v3/projects/{project}"
    sources.append(
        read_once(
            quota_session,
            path_template=project_path,
            url=f"https://cloudresourcemanager.googleapis.com/v3/projects/{project}",
            params={},
            timeout=timeout,
            budget=budget,
        )
    )
    for service in SERVICES:
        quota_path = (
            "/v1/projects/{project}/locations/global/services/{service}/quotaInfos"
        )
        evidence, _records_value = read_pages(
            quota_session,
            method="GET",
            path_template=quota_path,
            url=(
                "https://cloudquotas.googleapis.com/v1/"
                f"projects/{project}/locations/global/services/{service}/quotaInfos"
            ),
            item_key="quotaInfos",
            params={"pageSize": "200"},
            max_pages=max_pages,
            timeout=timeout,
            budget=budget,
        )
        sources.append(evidence)
        preference_path = "/v1/projects/{project}/locations/global/quotaPreferences"
        evidence, _records_value = read_pages(
            quota_session,
            method="GET",
            path_template=preference_path,
            url=(
                "https://cloudquotas.googleapis.com/v1/"
                f"projects/{project}/locations/global/quotaPreferences"
            ),
            item_key="quotaPreferences",
            params={"filter": f'service="{service}"', "pageSize": "200"},
            max_pages=max_pages,
            timeout=timeout,
            budget=budget,
        )
        sources.append(evidence)
    now = datetime.now(UTC)
    monitoring_path = "/v3/projects/{project}/timeSeries"
    for metric in MONITORING_METRICS:
        evidence, _records_value = read_pages(
            quota_session,
            method="GET",
            path_template=monitoring_path,
            url=f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
            item_key="timeSeries",
            params={
                "filter": (
                    f'metric.type = "{metric}" AND resource.type = "consumer_quota"'
                ),
                "interval.endTime": now.isoformat().replace("+00:00", "Z"),
                "interval.startTime": (now - timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "pageSize": "200",
                "view": "HEADERS",
            },
            max_pages=max_pages,
            timeout=timeout,
            budget=budget,
        )
        sources.append(evidence)
    compute_sources = ("acceleratorTypes", "machineTypes")
    for resource in compute_sources:
        path = f"/compute/v1/projects/{{project}}/aggregated/{resource}"
        evidence, _records_value = read_pages(
            quota_session,
            method="GET",
            path_template=path,
            url=(
                f"https://compute.googleapis.com/compute/v1/projects/{project}/"
                f"aggregated/{resource}"
            ),
            item_key="items",
            params={"maxResults": "500"},
            max_pages=max_pages,
            timeout=timeout,
            nested_key=resource,
            budget=budget,
        )
        sources.append(evidence)
    location_path = "/v2/projects/{project}/locations"
    location_evidence, location_records = read_pages(
        quota_session,
        method="GET",
        path_template=location_path,
        url=f"https://tpu.googleapis.com/v2/projects/{project}/locations",
        item_key="locations",
        params={"pageSize": "100"},
        max_pages=max_pages,
        timeout=timeout,
        budget=budget,
        collect_records=True,
    )
    sources.append(location_evidence)
    locations = _location_ids(location_records, max_locations=max_locations)
    state["tpu_locations"] = len(locations)
    for location in locations:
        for resource, item_key in (
            ("acceleratorTypes", "acceleratorTypes"),
            ("runtimeVersions", "runtimeVersions"),
        ):
            path = f"/v2/projects/{{project}}/locations/{{location}}/{resource}"
            evidence, _records_value = read_pages(
                quota_session,
                method="GET",
                path_template=path,
                url=(
                    f"https://tpu.googleapis.com/v2/projects/{project}/"
                    f"locations/{location}/{resource}"
                ),
                item_key=item_key,
                params={"pageSize": "100"},
                max_pages=max_pages,
                timeout=timeout,
                budget=budget,
            )
            sources.append(evidence)
    return {
        "claims": {
            "physical_capacity": False,
            "universal_availability": False,
        },
        "complete": all(source["complete"] is True for source in sources),
        "schema": CANARY_SCHEMA,
        "services": list(SERVICES),
        "sources": sources,
        "tpu_locations": state["tpu_locations"],
        "total_elapsed_ms": budget.elapsed_ms(),
        "total_requests": budget.requests,
    }


def _budget_failure_evidence(
    error: RequestBudgetError,
    *,
    budget: RequestBudget,
    sources: list[dict[str, object]],
    state: dict[str, int],
) -> dict[str, object]:
    """Retain partial sanitized evidence after the global budget stops fanout."""
    failure = {
        "complete": False,
        "elapsed_ms": budget.observed_elapsed_ms(),
        "method": None,
        "pages": 0,
        "path": None,
        "reason": "budget-exhausted",
        "records": 0,
        "scope": error.reason,
    }
    return {
        "claims": {
            "physical_capacity": False,
            "universal_availability": False,
        },
        "complete": False,
        "failure": "budget-exhausted",
        "schema": CANARY_SCHEMA,
        "services": list(SERVICES),
        "sources": [*sources, failure],
        "tpu_locations": state["tpu_locations"],
        "total_elapsed_ms": budget.observed_elapsed_ms(),
        "total_requests": budget.requests,
    }


def _source_limit_failure_evidence(
    error: SourceLimitError,
    *,
    budget: RequestBudget,
    sources: list[dict[str, object]],
    state: dict[str, int],
) -> dict[str, object]:
    """Retain sanitized partial evidence after one source reaches its bound."""
    terminal = {
        "elapsed_ms": budget.observed_elapsed_ms(),
        **error.evidence,
    }
    return {
        "claims": {
            "physical_capacity": False,
            "universal_availability": False,
        },
        "complete": False,
        "failure": error.evidence["reason"],
        "schema": CANARY_SCHEMA,
        "services": list(SERVICES),
        "sources": [*sources, terminal],
        "tpu_locations": state["tpu_locations"],
        "total_elapsed_ms": budget.observed_elapsed_ms(),
        "total_requests": budget.requests,
    }


def run_canary(  # noqa: PLR0913 - explicit global and per-request bounds
    session: SessionLike,
    project: str,
    *,
    max_pages: int,
    timeout: float,
    max_requests: int,
    max_seconds: float,
    max_locations: int,
) -> dict[str, object]:
    """Run one bounded canary and retain budget-exhaustion evidence."""
    if not PROJECT_PATTERN.fullmatch(project):
        msg = "project identifier has an invalid canonical shape"
        raise ValueError(msg)
    if max_pages < 1:
        msg = "page limit must be positive"
        raise ValueError(msg)
    if max_locations < 1:
        msg = "TPU location limit must be positive"
        raise ValueError(msg)
    budget = RequestBudget(
        max_requests=max_requests,
        max_seconds=max_seconds,
    )
    sources: list[dict[str, object]] = []
    state = {"tpu_locations": 0}
    try:
        return _run_canary_sources(
            session,
            project,
            max_pages=max_pages,
            timeout=timeout,
            max_locations=max_locations,
            budget=budget,
            sources=sources,
            state=state,
        )
    except RequestBudgetError as error:
        return _budget_failure_evidence(
            error,
            budget=budget,
            sources=sources,
            state=state,
        )
    except SourceLimitError as error:
        return _source_limit_failure_evidence(
            error,
            budget=budget,
            sources=sources,
            state=state,
        )


def _write_evidence(output: Path, evidence: dict[str, object]) -> None:
    """Write retained evidence before failing an incomplete canary process."""
    output.write_bytes(_canonical_json(evidence))
    if evidence.get("complete") is not True:
        raise SystemExit(1)


def main(arguments: Sequence[str] | None = None) -> None:
    """Authenticate keylessly, run the bounded reads, and write sanitized evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-env", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--max-seconds", type=float, default=120.0)
    parser.add_argument("--max-locations", type=int, default=100)
    parsed = parser.parse_args(arguments)
    project = os.environ.get(parsed.project_env)
    if project is None:
        msg = f"required project environment variable is unset: {parsed.project_env}"
        raise RuntimeError(msg)
    import google.auth  # noqa: PLC0415
    from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415

    credentials, _project = google.auth.default(
        scopes=("https://www.googleapis.com/auth/cloud-platform",)
    )
    session = AuthorizedSession(credentials)
    evidence = run_canary(
        session,
        project,
        max_pages=parsed.max_pages,
        timeout=parsed.timeout,
        max_requests=parsed.max_requests,
        max_seconds=parsed.max_seconds,
        max_locations=parsed.max_locations,
    )
    _write_evidence(parsed.output, evidence)


if __name__ == "__main__":
    main()
