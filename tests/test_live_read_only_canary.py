"""The live canary cannot escape its bounded read-only request allowlist."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "live_read_only_canary.py"


def _module() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/{parent}/quotaInfos"),
        ("PATCH", "/v1/{name}"),
        ("DELETE", "/v1/{name}"),
        ("GET", "/v1/{parent}/services"),
        ("GET", "/v1/{parent}/quotaPreferences:validateOnly"),
    ],
)
def test_canary_rejects_every_nonallowlisted_shape(method: str, path: str) -> None:
    """Mutation-shaped or undeclared requests fail before transport dispatch."""
    require_allowlisted = cast("Any", _module()["require_allowlisted"])

    with pytest.raises(ValueError, match="allowlist"):
        require_allowlisted(method, path)


def test_canary_allows_only_declared_get_paths() -> None:
    """The ordinary canary exposes only GETs for the exact V1 read sources."""
    module = _module()
    require_allowlisted = cast("Any", module["require_allowlisted"])
    allowed_paths = cast("frozenset[str]", module["ALLOWED_GET_PATHS"])

    assert allowed_paths
    for path in allowed_paths:
        require_allowlisted("GET", path)
    assert all(
        token not in path.lower()
        for path in allowed_paths
        for token in ("create", "update", "patch", "delete", "validateonly")
    )


def test_canary_forces_the_explicit_quota_project_header() -> None:
    """Every provider read bills and checks service use against the named project."""
    quota_project_session_type = cast("Any", _module()["QuotaProjectSession"])

    class Session:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def request(self, method: str, url: str, **kwargs: object) -> object:
            del method, url
            self.kwargs = kwargs
            return object()

    session = Session()
    quota_session = quota_project_session_type(session, "dedicated-canary")

    quota_session.request("GET", "https://example.invalid", timeout=1.0)

    assert session.kwargs["headers"] == {
        "x-goog-user-project": "dedicated-canary",
    }
    with pytest.raises(ValueError, match="cannot replace"):
        quota_session.request(
            "GET",
            "https://example.invalid",
            headers={"x-goog-user-project": "other"},
        )


def test_paged_evidence_is_bounded_sanitized_and_content_addressed() -> None:
    """Retained evidence contains shapes and digests, never project data or bodies."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])
    project = "private-project-123"

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self.payload

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            self.calls += 1
            if self.calls == 1:
                return Response(
                    {
                        "quotaInfos": [{"name": f"projects/{project}/quotaInfos/a"}],
                        "nextPageToken": "opaque-next",
                    }
                )
            return Response(
                {
                    "quotaInfos": [{"name": f"projects/{project}/quotaInfos/b"}],
                    "nextPageToken": "",
                }
            )

    session = Session()
    evidence, records = read_pages(
        session,
        method="GET",
        path_template=(
            "/v1/projects/{project}/locations/global/services/{service}/quotaInfos"
        ),
        url=f"https://cloudquotas.googleapis.com/v1/projects/{project}/quotaInfos",
        item_key="quotaInfos",
        params={"pageSize": "2"},
        max_pages=2,
        timeout=1.0,
        budget=request_budget_type(max_requests=2, max_seconds=5.0),
        collect_records=True,
    )

    expected_count = 2
    assert session.calls == expected_count
    assert len(records) == expected_count
    assert evidence["pages"] == expected_count
    assert evidence["records"] == expected_count
    assert evidence["complete"] is True
    assert evidence["method"] == "GET"
    assert evidence["path"] == (
        "/v1/projects/{project}/locations/global/services/{service}/quotaInfos"
    )
    encoded = json.dumps(evidence, sort_keys=True)
    assert project not in encoded
    assert "opaque-next" not in encoded
    assert "quotaInfos/a" not in encoded


def test_page_limit_blocks_an_unexhausted_source() -> None:
    """A terminal coverage claim is impossible when another page remains."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {"items": [], "nextPageToken": "still-more"}

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    with pytest.raises(RuntimeError, match="page limit"):
        read_pages(
            Session(),
            method="GET",
            path_template="/compute/v1/projects/{project}/aggregated/machineTypes",
            url="https://compute.googleapis.com/compute/v1/projects/example/"
            "aggregated/machineTypes",
            item_key="items",
            params={},
            max_pages=1,
            timeout=1.0,
            budget=request_budget_type(max_requests=1, max_seconds=5.0),
        )


def test_compute_aggregated_pages_count_nested_records_not_scope_wrappers() -> None:
    """Warnings and scope wrappers never inflate specialized-hardware evidence."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "items": {
                    "zones/us-central1-a": {
                        "acceleratorTypes": [{"name": "nvidia-b200"}],
                    },
                    "zones/us-east1-d": {
                        "warning": {"code": "NO_RESULTS_ON_PAGE"},
                    },
                }
            }

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    evidence, records = read_pages(
        Session(),
        method="GET",
        path_template="/compute/v1/projects/{project}/aggregated/acceleratorTypes",
        url="https://compute.googleapis.com/compute/v1/projects/example/"
        "aggregated/acceleratorTypes",
        item_key="items",
        params={},
        max_pages=1,
        timeout=1.0,
        nested_key="acceleratorTypes",
        budget=request_budget_type(max_requests=1, max_seconds=5.0),
        collect_records=True,
    )

    assert records == ({"name": "nvidia-b200"},)
    assert evidence["records"] == 1


def test_paged_evidence_counts_records_without_retaining_provider_values() -> None:
    """Sources retain counts and digests unless a caller explicitly needs records."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {"quotaInfos": [{"private": "one"}, {"private": "two"}]}

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    evidence, records = read_pages(
        Session(),
        method="GET",
        path_template=(
            "/v1/projects/{project}/locations/global/services/{service}/quotaInfos"
        ),
        url="https://cloudquotas.googleapis.com/v1/projects/example/quotaInfos",
        item_key="quotaInfos",
        params={},
        max_pages=1,
        timeout=1.0,
        budget=request_budget_type(max_requests=1, max_seconds=5.0),
    )

    expected_count = 2
    assert evidence["records"] == expected_count
    assert records == ()


def test_shared_request_budget_bounds_fanout_across_sources() -> None:
    """Per-source page limits cannot multiply beyond one global request budget."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {"items": []}

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    budget = request_budget_type(max_requests=1, max_seconds=5.0)
    read_pages(
        Session(),
        method="GET",
        path_template="/compute/v1/projects/{project}/aggregated/machineTypes",
        url="https://compute.googleapis.com/compute/v1/projects/example/"
        "aggregated/machineTypes",
        item_key="items",
        params={},
        max_pages=1,
        timeout=1.0,
        budget=budget,
    )

    with pytest.raises(RuntimeError, match="global request limit"):
        read_pages(
            Session(),
            method="GET",
            path_template="/compute/v1/projects/{project}/aggregated/machineTypes",
            url="https://compute.googleapis.com/compute/v1/projects/example/"
            "aggregated/machineTypes",
            item_key="items",
            params={},
            max_pages=1,
            timeout=1.0,
            budget=budget,
        )


def test_shared_request_budget_bounds_wall_clock_and_request_timeout() -> None:
    """Every request timeout is capped by one global monotonic deadline."""
    module = _module()
    request_budget_type = cast("Any", module["RequestBudget"])
    observed = iter((10.0, 12.5, 15.1))
    budget = request_budget_type(
        max_requests=2,
        max_seconds=5.0,
        monotonic=lambda: next(observed),
    )

    expected_remaining = 2.5
    assert budget.claim_timeout(10.0) == expected_remaining
    with pytest.raises(RuntimeError, match="wall-clock deadline"):
        budget.claim_timeout(10.0)


def test_shared_request_budget_rejects_unusable_remaining_timeout() -> None:
    """A near-zero remainder is classified as budget exhaustion before transport."""
    module = _module()
    request_budget_type = cast("Any", module["RequestBudget"])
    threshold_budget = request_budget_type(
        max_requests=1,
        max_seconds=0.1,
        monotonic=lambda: 10.0,
    )

    with pytest.raises(ValueError, match="request timeout must be at least"):
        threshold_budget.claim_timeout(0.099)
    assert threshold_budget.requests == 0

    assert threshold_budget.claim_timeout(10.0) == pytest.approx(0.1)
    assert threshold_budget.requests == 1

    observed = iter((10.0, 14.95))
    budget = request_budget_type(
        max_requests=1,
        max_seconds=5.0,
        monotonic=lambda: next(observed),
    )

    with pytest.raises(RuntimeError, match="wall-clock deadline"):
        budget.claim_timeout(10.0)
    assert budget.requests == 0


def test_budget_exhaustion_stops_fanout_and_retains_incomplete_evidence() -> None:
    """A global bound stops before transport and preserves partial safe evidence."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {}

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            self.calls += 1
            return Response()

    session = Session()
    evidence = run_canary(
        session,
        "dedicated-canary",
        max_pages=1,
        timeout=1.0,
        max_requests=1,
        max_seconds=30.0,
        max_locations=10,
    )

    assert session.calls == 1
    assert evidence["complete"] is False
    assert evidence["failure"] == "budget-exhausted"
    assert evidence["total_requests"] == 1
    sources = cast("list[dict[str, object]]", evidence["sources"])
    assert sources[0]["path"] == "/v3/projects/{project}"
    terminal = sources[-1]
    elapsed_ms = terminal.pop("elapsed_ms")
    assert isinstance(elapsed_ms, int)
    assert elapsed_ms >= 0
    assert terminal == {
        "complete": False,
        "method": None,
        "pages": 0,
        "path": None,
        "reason": "budget-exhausted",
        "records": 0,
        "scope": "request-limit",
    }


def test_incomplete_evidence_is_written_before_the_process_fails(
    tmp_path: Path,
) -> None:
    """The workflow can upload retained evidence after a controlled failure."""
    write_evidence = cast("Any", _module()["_write_evidence"])
    output = tmp_path / "evidence.json"
    evidence = {
        "complete": False,
        "failure": "budget-exhausted",
        "schema": "cqmgr.live-read-only-evidence/v1",
    }

    with pytest.raises(SystemExit) as raised:
        write_evidence(output, evidence)

    assert raised.value.code == 1
    assert json.loads(output.read_text()) == evidence


@pytest.mark.parametrize(
    "records",
    [
        (None,),
        ({},),
        ({"locationId": ""},),
        ({"locationId": "US-CENTRAL1"},),
        ({"locationId": "projects/private/locations/us-central1"},),
    ],
)
def test_tpu_location_ids_fail_closed_on_unusable_provider_records(
    records: tuple[object, ...],
) -> None:
    """Malformed locations cannot expand provider fanout or exhaustive claims."""
    location_ids = cast("Any", _module()["_location_ids"])

    with pytest.raises((TypeError, RuntimeError), match="location"):
        location_ids(records, max_locations=10)


def test_tpu_location_ids_are_unique_sorted_and_globally_bounded() -> None:
    """Duplicate location rows do not multiply reads and excess scope fails."""
    location_ids = cast("Any", _module()["_location_ids"])
    records = (
        {"locationId": "us-central1"},
        {"locationId": "europe-west4"},
        {"locationId": "us-central1"},
    )

    assert location_ids(records, max_locations=2) == (
        "europe-west4",
        "us-central1",
    )
    with pytest.raises(RuntimeError, match="location limit"):
        location_ids(records, max_locations=1)


def test_canary_uses_exact_single_metric_monitoring_filters() -> None:
    """Time-series reads select each supported quota usage metric exactly."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])
    monitoring_metrics = cast("tuple[str, ...]", module["MONITORING_METRICS"])

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self.payload

    class Session:
        def __init__(self) -> None:
            self.monitoring_filters: list[str] = []

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            assert method == "GET"
            params = cast("dict[str, str]", kwargs["params"])
            if "monitoring.googleapis.com" in url:
                self.monitoring_filters.append(params["filter"])
                return Response({"timeSeries": []})
            if "cloudquotas.googleapis.com" in url:
                item_key = (
                    "quotaPreferences" if "quotaPreferences" in url else "quotaInfos"
                )
                return Response({item_key: []})
            if "compute.googleapis.com" in url:
                return Response({"items": {}})
            if url.endswith("/locations"):
                return Response({"locations": []})
            return Response({})

    session = Session()
    run_canary(
        session,
        "dedicated-canary",
        max_pages=1,
        timeout=1.0,
        max_requests=20,
        max_seconds=30.0,
        max_locations=10,
    )

    assert monitoring_metrics == (
        "serviceruntime.googleapis.com/quota/allocation/usage",
        "serviceruntime.googleapis.com/quota/rate/net_usage",
    )
    assert session.monitoring_filters == [
        f'metric.type = "{metric}" AND resource.type = "consumer_quota"'
        for metric in monitoring_metrics
    ]
    assert all("starts_with" not in value for value in session.monitoring_filters)
