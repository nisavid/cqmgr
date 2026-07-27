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
    read_pages = cast("Any", _module()["read_pages"])
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
    read_pages = cast("Any", _module()["read_pages"])

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
        )


def test_compute_aggregated_pages_count_nested_records_not_scope_wrappers() -> None:
    """Warnings and scope wrappers never inflate specialized-hardware evidence."""
    read_pages = cast("Any", _module()["read_pages"])

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
    )

    assert records == ({"name": "nvidia-b200"},)
    assert evidence["records"] == 1
