"""The live canary cannot escape its bounded read-only request allowlist."""

from __future__ import annotations

import hashlib
import json
import runpy
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "live_read_only_canary.py"
OVERLAY = files("cqmgr.resources").joinpath("accelerator-overlay.json")


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

    selection = {
        "filter_digest": f"sha256:{'0' * 64}",
        "kind": "accelerator-relevant-machine-types/v1",
        "overlay_content_digest": f"sha256:{'1' * 64}",
        "overlay_machine_type_terms": 1,
        "return_partial_success": True,
    }
    with pytest.raises(RuntimeError, match="page limit") as raised:
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
            selection=selection,
        )
    assert cast("Any", raised.value).evidence["selection"] == selection


def test_page_limit_retains_sanitized_incomplete_evidence(tmp_path: Path) -> None:
    """A source bound stops fanout and preserves safe partial evidence."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])
    write_evidence = cast("Any", module["_write_evidence"])
    project = "dedicated-canary"

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
                return Response({"name": f"projects/{project}"})
            return Response(
                {
                    "quotaInfos": [{"name": f"projects/{project}/quotaInfos/private"}],
                    "nextPageToken": "private-token",
                }
            )

    session = Session()
    evidence = run_canary(
        session,
        project,
        max_pages=1,
        timeout=1.0,
        max_requests=10,
        max_seconds=30.0,
        max_locations=10,
    )

    expected_requests = 2
    assert session.calls == expected_requests
    assert evidence["complete"] is False
    assert evidence["failure"] == "page-limit"
    assert evidence["total_requests"] == expected_requests
    sources = cast("list[dict[str, object]]", evidence["sources"])
    terminal = sources[-1]
    assert terminal["complete"] is False
    assert terminal["method"] == "GET"
    assert terminal["pages"] == 1
    assert terminal["path"] == (
        "/v1/projects/{project}/locations/global/services/{service}/quotaInfos"
    )
    assert terminal["reason"] == "page-limit"
    assert terminal["records"] == 1
    assert str(terminal["digest"]).startswith("sha256:")
    encoded = json.dumps(evidence, sort_keys=True)
    assert project not in encoded
    assert "private-token" not in encoded
    assert "quotaInfos/private" not in encoded
    output = tmp_path / "evidence.json"
    with pytest.raises(SystemExit) as raised:
        write_evidence(output, evidence)
    assert raised.value.code == 1
    assert json.loads(output.read_text()) == evidence


def test_selected_pagination_keeps_filter_and_hides_page_tokens() -> None:
    """Every selected page keeps its public query while opaque tokens stay private."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])
    selection = cast("dict[str, object]", module["MACHINE_TYPE_SELECTION"])
    filter_value = cast("str", module["MACHINE_TYPE_FILTER"])
    opaque_page_value = "private-provider-page-value"

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self.payload

    class Session:
        def __init__(self) -> None:
            self.params: list[dict[str, str]] = []

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url
            self.params.append(dict(cast("dict[str, str]", kwargs["params"])))
            if len(self.params) == 1:
                return Response({"items": {}, "nextPageToken": opaque_page_value})
            return Response({"items": {}})

    session = Session()
    base_params = {
        "filter": filter_value,
        "maxResults": "500",
        "returnPartialSuccess": "true",
    }
    evidence, _records_value = read_pages(
        session,
        method="GET",
        path_template="/compute/v1/projects/{project}/aggregated/machineTypes",
        url="https://compute.googleapis.com/compute/v1/projects/private/"
        "aggregated/machineTypes",
        item_key="items",
        params=base_params,
        max_pages=2,
        timeout=1.0,
        budget=request_budget_type(max_requests=2, max_seconds=5.0),
        nested_key="machineTypes",
        selection=selection,
    )

    assert session.params == [
        base_params,
        {**base_params, "pageToken": opaque_page_value},
    ]
    expected_pages = 2
    assert evidence["pages"] == expected_pages
    assert evidence["selection"] == selection
    assert opaque_page_value not in json.dumps(evidence)


def test_location_limit_retains_sanitized_incomplete_evidence() -> None:
    """A TPU fanout bound stops before zone reads and preserves safe evidence."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self.payload

    class Session:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, kwargs
            self.urls.append(url)
            hostname = urlsplit(url).hostname
            if hostname == "cloudquotas.googleapis.com":
                item_key = (
                    "quotaPreferences" if "quotaPreferences" in url else "quotaInfos"
                )
                return Response({item_key: []})
            if hostname == "monitoring.googleapis.com":
                return Response({"timeSeries": []})
            if hostname == "compute.googleapis.com":
                return Response({"items": {}})
            if url.endswith("/locations"):
                return Response(
                    {
                        "locations": [
                            {"locationId": "us-central1"},
                            {"locationId": "us-east1"},
                        ]
                    }
                )
            return Response({})

    session = Session()
    evidence = run_canary(
        session,
        "dedicated-canary",
        max_pages=1,
        timeout=1.0,
        max_requests=20,
        max_seconds=30.0,
        max_locations=1,
    )

    assert evidence["complete"] is False
    assert evidence["failure"] == "location-limit"
    terminal = cast("list[dict[str, object]]", evidence["sources"])[-1]
    assert terminal["method"] is None
    assert terminal["path"] == "/v2/projects/{project}/locations"
    assert terminal["reason"] == "location-limit"
    expected_locations = 2
    assert terminal["records"] == expected_locations
    encoded = json.dumps(evidence, sort_keys=True)
    assert "us-central1" not in encoded
    assert "us-east1" not in encoded
    assert all(
        f"/locations/{location}/" not in url
        for url in session.urls
        for location in ("us-central1", "us-east1")
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


def test_transport_budget_errors_are_not_reclassified_as_provider_failures() -> None:
    """A shared budget failure keeps its terminal reason across read boundaries."""
    module = _module()
    read_once = cast("Any", module["read_once"])
    read_pages = cast("Any", module["read_pages"])
    request_budget_error = cast("Any", module["RequestBudgetError"])
    request_budget_type = cast("Any", module["RequestBudget"])
    reason = "request-limit"

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            del method, url, kwargs
            raise request_budget_error(reason, "bounded transport stopped")

    with pytest.raises(
        request_budget_error,
        match="bounded transport stopped",
    ) as raised_once:
        read_once(
            Session(),
            path_template="/v3/projects/{project}",
            url="https://cloudresourcemanager.googleapis.com/v3/projects/private",
            params={},
            timeout=1.0,
            budget=request_budget_type(max_requests=1, max_seconds=5.0),
        )
    assert raised_once.value.reason == reason

    with pytest.raises(
        request_budget_error,
        match="bounded transport stopped",
    ) as raised_pages:
        read_pages(
            Session(),
            method="GET",
            path_template=(
                "/v1/projects/{project}/locations/global/services/{service}/quotaInfos"
            ),
            url="https://cloudquotas.googleapis.com/v1/projects/private/quotaInfos",
            item_key="quotaInfos",
            params={"pageSize": "200"},
            max_pages=1,
            timeout=1.0,
            budget=request_budget_type(max_requests=1, max_seconds=5.0),
        )
    assert raised_pages.value.reason == reason


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
            hostname = urlsplit(url).hostname
            if hostname == "monitoring.googleapis.com":
                self.monitoring_filters.append(params["filter"])
                return Response({"timeSeries": []})
            if hostname == "cloudquotas.googleapis.com":
                item_key = (
                    "quotaPreferences" if "quotaPreferences" in url else "quotaInfos"
                )
                return Response({item_key: []})
            if hostname == "compute.googleapis.com":
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


def test_canary_bounds_compute_inventory_to_specialized_machine_shapes() -> None:
    """Live qualification selects assigned accelerators and release-overlay shapes."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])
    machine_type_filter = cast("str", module["MACHINE_TYPE_FILTER"])
    machine_type_selection = cast("dict[str, object]", module["MACHINE_TYPE_SELECTION"])
    overlay = cast(
        "dict[str, object]",
        json.loads(OVERLAY.read_text(encoding="utf-8")),
    )
    mappings = cast("list[dict[str, object]]", overlay["mappings"])
    machine_types = sorted(
        {
            machine_type
            for mapping in mappings
            for machine_type in cast("list[str]", mapping["machine_types"])
        }
    )
    filter_terms = machine_type_filter.split(" OR ")
    assert filter_terms[0] == "(accelerators.guestAcceleratorType:*)"
    assert set(filter_terms[1:]) == {
        f'(name = "{machine_type}")' for machine_type in machine_types
    }
    assert len(filter_terms) == len(machine_types) + 1

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self.payload

    class Session:
        def __init__(self) -> None:
            self.compute_params: dict[str, dict[str, str]] = {}

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            assert method == "GET"
            params = cast("dict[str, str]", kwargs["params"])
            hostname = urlsplit(url).hostname
            if hostname == "cloudquotas.googleapis.com":
                item_key = (
                    "quotaPreferences" if "quotaPreferences" in url else "quotaInfos"
                )
                return Response({item_key: []})
            if hostname == "monitoring.googleapis.com":
                return Response({"timeSeries": []})
            if hostname == "compute.googleapis.com":
                resource = url.rsplit("/", maxsplit=1)[-1]
                self.compute_params[resource] = dict(params)
                return Response({"items": {}})
            if url.endswith("/locations"):
                return Response({"locations": []})
            return Response({})

    session = Session()
    evidence = run_canary(
        session,
        "dedicated-canary",
        max_pages=50,
        timeout=1.0,
        max_requests=100,
        max_seconds=30.0,
        max_locations=100,
    )

    assert session.compute_params == {
        "acceleratorTypes": {
            "maxResults": "500",
            "returnPartialSuccess": "true",
        },
        "machineTypes": {
            "filter": machine_type_filter,
            "maxResults": "500",
            "returnPartialSuccess": "true",
        },
    }
    machine_evidence = next(
        source
        for source in cast("list[dict[str, object]]", evidence["sources"])
        if source["path"] == "/compute/v1/projects/{project}/aggregated/machineTypes"
    )
    assert machine_type_selection == {
        "filter_digest": "sha256:"
        + hashlib.sha256(machine_type_filter.encode()).hexdigest(),
        "kind": "accelerator-relevant-machine-types/v1",
        "overlay_content_digest": overlay["content_digest"],
        "overlay_machine_type_terms": len(machine_types),
        "return_partial_success": True,
    }
    assert machine_evidence["selection"] == machine_type_selection


def test_machine_type_filter_is_canonical_and_rejects_unsafe_growth() -> None:
    """Overlay order cannot change the query and unsafe terms fail before transport."""
    module = _module()
    build_query = cast("Any", module["_machine_type_query"])
    digest = f"sha256:{'a' * 64}"
    payload = {
        "content_digest": digest,
        "mappings": [
            {"machine_types": ["z9-highgpu-1g", "a3-highgpu-8g"]},
            {"machine_types": ["a3-highgpu-8g"]},
        ],
    }

    filter_value, selection = build_query(
        payload,
        expected_content_digest=digest,
        expected_machine_types=frozenset({"a3-highgpu-8g", "z9-highgpu-1g"}),
    )

    assert filter_value == (
        "(accelerators.guestAcceleratorType:*)"
        ' OR (name = "a3-highgpu-8g")'
        ' OR (name = "z9-highgpu-1g")'
    )
    assert selection["overlay_content_digest"] == digest
    expected_terms = 2
    assert selection["overlay_machine_type_terms"] == expected_terms

    unsafe = {
        "content_digest": digest,
        "mappings": [{"machine_types": ['a3") OR (name = "private']}],
    }
    with pytest.raises(ValueError, match="invalid machine type"):
        build_query(
            unsafe,
            expected_content_digest=digest,
            expected_machine_types=frozenset(),
        )

    too_many = {
        "content_digest": digest,
        "mappings": [
            {
                "machine_types": [
                    f"a{index}-highgpu-1g"
                    for index in range(
                        cast("int", module["MAX_MACHINE_TYPE_FILTER_TERMS"]) + 1
                    )
                ]
            }
        ],
    }
    with pytest.raises(ValueError, match="too many machine types"):
        build_query(
            too_many,
            expected_content_digest=digest,
            expected_machine_types=frozenset(),
        )

    oversized = {
        "content_digest": digest,
        "mappings": [
            {
                "machine_types": [
                    f"a{index}-{'x' * 200}-1g"
                    for index in range(
                        cast("int", module["MAX_MACHINE_TYPE_FILTER_TERMS"])
                    )
                ]
            }
        ],
    }
    oversized_machine_types = frozenset(
        cast("list[str]", oversized["mappings"][0]["machine_types"])
    )
    with pytest.raises(ValueError, match="filter is too large"):
        build_query(
            oversized,
            expected_content_digest=digest,
            expected_machine_types=oversized_machine_types,
        )


def test_machine_type_filter_rejects_release_overlay_drift() -> None:
    """Live selection stays bound to the canonical semantic overlay identity."""
    module = _module()
    build_query = cast("Any", module["_machine_type_query"])
    overlay = cast(
        "dict[str, object]",
        json.loads(OVERLAY.read_text(encoding="utf-8")),
    )
    expected_digest = cast("str", module["EXPECTED_OVERLAY_CONTENT_DIGEST"])
    expected_machine_types = cast(
        "frozenset[str]",
        module["EXPECTED_OVERLAY_MACHINE_TYPES"],
    )

    with pytest.raises(ValueError, match="content digest does not match"):
        build_query(
            overlay,
            expected_content_digest=f"sha256:{'0' * 64}",
            expected_machine_types=expected_machine_types,
        )

    drifted = cast("dict[str, object]", json.loads(json.dumps(overlay)))
    mappings = cast("list[dict[str, object]]", drifted["mappings"])
    cast("list[str]", mappings[0]["machine_types"]).append("z9-highgpu-1g")
    with pytest.raises(ValueError, match="machine types do not match"):
        build_query(
            drifted,
            expected_content_digest=expected_digest,
            expected_machine_types=expected_machine_types,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "items": {
                "zones/public-a": {
                    "machineTypes": [],
                    "warning": {"code": "PERMISSION_DENIED"},
                }
            }
        },
        {"items": {}, "unreachables": ["zones/private-a"]},
    ],
)
def test_compute_coverage_failures_retain_only_sanitized_counts(
    payload: dict[str, object],
) -> None:
    """Partial-success warnings and unreachable scopes cannot look complete."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return payload

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    with pytest.raises(RuntimeError, match="coverage") as raised:
        read_pages(
            Session(),
            method="GET",
            path_template="/compute/v1/projects/{project}/aggregated/machineTypes",
            url="https://compute.googleapis.com/compute/v1/projects/private/"
            "aggregated/machineTypes",
            item_key="items",
            params={"returnPartialSuccess": "true"},
            max_pages=1,
            timeout=1.0,
            budget=request_budget_type(max_requests=1, max_seconds=5.0),
            nested_key="machineTypes",
        )

    evidence = cast("Any", raised.value).evidence
    assert evidence["complete"] is False
    assert evidence["reason"] == "coverage-incomplete"
    assert evidence["coverage_failures"] == 1
    encoded = json.dumps(evidence)
    assert "public-a" not in encoded
    assert "private-a" not in encoded
    assert "PERMISSION_DENIED" not in encoded


@pytest.mark.parametrize(
    "payload",
    [
        {"quotaInfos": [], "warning": {"code": "PARTIAL_FAILURE"}},
        {"quotaInfos": [], "unreachables": ["locations/private-a"]},
    ],
)
def test_top_level_coverage_failures_apply_to_every_paged_source(
    payload: dict[str, object],
) -> None:
    """A non-Compute partial-success response cannot bypass the live gate."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return payload

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    with pytest.raises(RuntimeError, match="coverage") as raised:
        read_pages(
            Session(),
            method="GET",
            path_template=(
                "/v1/projects/{project}/locations/global/services/{service}/quotaInfos"
            ),
            url="https://cloudquotas.googleapis.com/v1/projects/private/quotaInfos",
            item_key="quotaInfos",
            params={"pageSize": "200"},
            max_pages=1,
            timeout=1.0,
            budget=request_budget_type(max_requests=1, max_seconds=5.0),
        )

    evidence = cast("Any", raised.value).evidence
    assert evidence["reason"] == "coverage-incomplete"
    assert evidence["coverage_failures"] == 1
    encoded = json.dumps(evidence)
    assert "PARTIAL_FAILURE" not in encoded
    assert "private-a" not in encoded


def test_compute_no_results_warning_is_complete_empty_coverage() -> None:
    """An explicit empty filtered scope is authoritative, not a failed scope."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "items": {
                    "zones/public-a": {
                        "warning": {"code": "NO_RESULTS_ON_PAGE"},
                    }
                }
            }

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    evidence, records = read_pages(
        Session(),
        method="GET",
        path_template="/compute/v1/projects/{project}/aggregated/machineTypes",
        url="https://compute.googleapis.com/compute/v1/projects/private/"
        "aggregated/machineTypes",
        item_key="items",
        params={"returnPartialSuccess": "true"},
        max_pages=1,
        timeout=1.0,
        budget=request_budget_type(max_requests=1, max_seconds=5.0),
        nested_key="machineTypes",
    )

    assert evidence["complete"] is True
    assert evidence["records"] == 0
    assert records == ()


def test_malformed_compute_collection_fails_with_sanitized_evidence() -> None:
    """A schema-skewed nested collection cannot masquerade as an empty scope."""
    module = _module()
    read_pages = cast("Any", module["read_pages"])
    request_budget_type = cast("Any", module["RequestBudget"])

    class Response:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "items": {
                    "zones/private-a": {"machineTypes": {"private-resource": "invalid"}}
                }
            }

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, url, kwargs
            return Response()

    with pytest.raises(RuntimeError, match="failed safely") as raised:
        read_pages(
            Session(),
            method="GET",
            path_template="/compute/v1/projects/{project}/aggregated/machineTypes",
            url="https://compute.googleapis.com/compute/v1/projects/private/"
            "aggregated/machineTypes",
            item_key="items",
            params={"returnPartialSuccess": "true"},
            max_pages=1,
            timeout=1.0,
            budget=request_budget_type(max_requests=1, max_seconds=5.0),
            nested_key="machineTypes",
        )

    evidence = cast("Any", raised.value).evidence
    assert evidence["reason"] == "provider-read-failure"
    encoded = json.dumps(evidence)
    assert "private-a" not in encoded
    assert "private-resource" not in encoded


def test_malformed_page_token_becomes_sanitized_incomplete_evidence() -> None:
    """A non-string provider page cursor cannot bypass retained failure evidence."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])
    private = "private-provider-cursor"

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
                return Response({"name": "projects/dedicated-canary"})
            return Response(
                {
                    "quotaInfos": [],
                    "nextPageToken": {"private": private},
                }
            )

    evidence = run_canary(
        Session(),
        "dedicated-canary",
        max_pages=50,
        timeout=1.0,
        max_requests=100,
        max_seconds=30.0,
        max_locations=100,
    )

    assert evidence["complete"] is False
    assert evidence["failure"] == "provider-read-failure"
    assert private not in json.dumps(evidence)


def test_malformed_tpu_location_becomes_sanitized_incomplete_evidence() -> None:
    """Invalid location records fail the complete canary through the safe boundary."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])
    private = "projects/private/locations/us-central1"

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self.payload

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> Response:
            del method, kwargs
            hostname = urlsplit(url).hostname
            if hostname == "cloudquotas.googleapis.com":
                item_key = (
                    "quotaPreferences" if "quotaPreferences" in url else "quotaInfos"
                )
                return Response({item_key: []})
            if hostname == "monitoring.googleapis.com":
                return Response({"timeSeries": []})
            if hostname == "compute.googleapis.com":
                return Response({"items": {}})
            if url.endswith("/locations"):
                return Response({"locations": [{"locationId": private}]})
            return Response({})

    evidence = run_canary(
        Session(),
        "dedicated-canary",
        max_pages=50,
        timeout=1.0,
        max_requests=100,
        max_seconds=30.0,
        max_locations=100,
    )

    assert evidence["complete"] is False
    assert evidence["failure"] == "provider-read-failure"
    sources = cast("list[dict[str, object]]", evidence["sources"])
    assert sources[-2]["complete"] is True
    assert sources[-2]["path"] == "/v2/projects/{project}/locations"
    assert sources[-2]["pages"] == 1
    assert sources[-2]["records"] == 1
    terminal = sources[-1]
    elapsed_ms = terminal.pop("elapsed_ms")
    assert isinstance(elapsed_ms, int)
    assert elapsed_ms >= 0
    assert terminal == {
        "complete": False,
        "method": "GET",
        "pages": 1,
        "path": "/v2/projects/{project}/locations",
        "reason": "provider-read-failure",
        "records": 1,
    }
    assert private not in json.dumps(evidence)


def test_provider_failure_retains_sanitized_incomplete_evidence(
    tmp_path: Path,
) -> None:
    """Transport or schema failure still leaves an uploadable safe artifact."""
    module = _module()
    run_canary = cast("Any", module["run_canary"])
    write_evidence = cast("Any", module["_write_evidence"])
    private = "private-project-body-and-principal@example.com"

    class Session:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            del method, url, kwargs
            raise RuntimeError(private)

    evidence = run_canary(
        Session(),
        "dedicated-canary",
        max_pages=50,
        timeout=1.0,
        max_requests=100,
        max_seconds=30.0,
        max_locations=100,
    )

    assert evidence["complete"] is False
    assert evidence["failure"] == "provider-read-failure"
    terminal = cast("list[dict[str, object]]", evidence["sources"])[-1]
    elapsed_ms = terminal.pop("elapsed_ms")
    assert isinstance(elapsed_ms, int)
    assert elapsed_ms >= 0
    assert terminal == {
        "complete": False,
        "method": "GET",
        "pages": 0,
        "path": "/v3/projects/{project}",
        "reason": "provider-read-failure",
        "records": 0,
    }
    assert private not in json.dumps(evidence)
    output = tmp_path / "evidence.json"
    with pytest.raises(SystemExit):
        write_evidence(output, evidence)
    assert json.loads(output.read_text()) == evidence
