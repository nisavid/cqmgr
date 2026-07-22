"""Resource Manager project canonicalization adapter contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from google.api_core import exceptions as google_exceptions
from google.cloud import resourcemanager_v3

from cqmgr.adapters.google.projects import ResourceManagerProjectResolver
from cqmgr.domain.projects import ProjectReference

if TYPE_CHECKING:
    from collections.abc import Mapping

FIXTURES = Path(__file__).parents[1] / "fixtures" / "google"


class FakeProjectsClient:
    """One-method no-network Resource Manager client."""

    def __init__(
        self,
        response: resourcemanager_v3.Project | BaseException,
    ) -> None:
        """Configure one GetProject response or failure."""
        self.response = response
        self.calls: list[tuple[str, float]] = []

    async def get_project(
        self,
        *,
        name: str,
        timeout: float,  # noqa: ASYNC109
    ) -> resourcemanager_v3.Project:
        """Record the exact official request surface."""
        self.calls.append((name, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _project_fixture() -> resourcemanager_v3.Project:
    mapping = cast(
        "Mapping[str, object]",
        json.loads((FIXTURES / "resource-manager-project.json").read_text()),
    )
    return resourcemanager_v3.Project(mapping)


def test_resource_manager_get_canonicalizes_a_project_id() -> None:
    """The official GetProject response becomes one canonical numeric scope."""
    client = FakeProjectsClient(_project_fixture())
    resolver = ResourceManagerProjectResolver(client, timeout_seconds=7.5)

    result = asyncio.run(resolver.resolve(ProjectReference("tokyo-rain-123")))

    assert client.calls == [("projects/tokyo-rain-123", 7.5)]
    assert result.succeeded
    assert result.project is not None
    assert result.project.resource_scope.canonical_name == "projects/415104041262"
    assert result.project.project_id == "tokyo-rain-123"
    assert result.project.display_name == "Tokyo Rain"
    assert result.diagnostics == ()
    assert "public-schema-fixture" not in repr(result)


def test_resource_manager_failure_returns_a_static_redacted_diagnostic() -> None:
    """Transport details never retain tokens, contacts, or credential paths."""
    private = (
        "token=ya29.public-fixture-only "
        "contact=fixture.user@example.com "
        "path=/Users/example/private/adc.json"
    )
    client = FakeProjectsClient(RuntimeError(private))

    result = asyncio.run(
        ResourceManagerProjectResolver(client).resolve(ProjectReference("123456"))
    )

    assert not result.succeeded
    assert result.project is None
    assert result.diagnostics[0].code.value == "project-resolution-failed"
    rendered = repr(result)
    assert "ya29" not in rendered
    assert "fixture.user@example.com" not in rendered
    assert "/Users/example" not in rendered


@pytest.mark.parametrize(
    ("error", "code", "retry"),
    [
        (
            google_exceptions.PermissionDenied("fixture"),
            "project-authorization-failed",
            "never",
        ),
        (google_exceptions.NotFound("fixture"), "project-not-found", "never"),
        (
            google_exceptions.InvalidArgument("fixture"),
            "invalid-project-reference",
            "never",
        ),
        (
            google_exceptions.ServiceUnavailable("fixture"),
            "project-resolution-transient",
            "after-backoff",
        ),
    ],
)
def test_resource_manager_classifies_retryable_and_permanent_failures(
    error: Exception,
    code: str,
    retry: str,
) -> None:
    """Only documented transient provider failures recommend backoff."""
    result = asyncio.run(
        ResourceManagerProjectResolver(FakeProjectsClient(error)).resolve(
            ProjectReference("123456")
        )
    )

    assert result.diagnostics[0].code.value == code
    assert result.diagnostics[0].retry.value == retry


def test_resource_manager_rejects_non_active_project_evidence() -> None:
    """A delete-requested project is not selectable as a canonical V1 scope."""
    response = _project_fixture()
    response.state = resourcemanager_v3.Project.State.DELETE_REQUESTED

    result = asyncio.run(
        ResourceManagerProjectResolver(FakeProjectsClient(response)).resolve(
            ProjectReference("tokyo-rain-123")
        )
    )

    assert not result.succeeded
    assert result.diagnostics[0].code.value == "project-not-active"


def test_resource_manager_rejects_malformed_or_mismatched_project_evidence() -> None:
    """Provider evidence cannot silently resolve a different requested project ID."""
    response = _project_fixture()
    response.project_id = "different-project"

    result = asyncio.run(
        ResourceManagerProjectResolver(FakeProjectsClient(response)).resolve(
            ProjectReference("tokyo-rain-123")
        )
    )

    assert not result.succeeded
    assert result.diagnostics[0].code.value == "invalid-project-evidence"


def test_resource_manager_rejects_malformed_canonical_response() -> None:
    """A malformed numeric resource name is safe invalid provider evidence."""
    response = _project_fixture()
    response.name = "projects/not-a-number"

    result = asyncio.run(
        ResourceManagerProjectResolver(FakeProjectsClient(response)).resolve(
            ProjectReference("tokyo-rain-123")
        )
    )

    assert not result.succeeded
    assert result.diagnostics[0].code.value == "invalid-project-evidence"


def test_resource_manager_numeric_lookup_requires_exact_numeric_response() -> None:
    """A numeric input cannot resolve to a different canonical project number."""
    result = asyncio.run(
        ResourceManagerProjectResolver(FakeProjectsClient(_project_fixture())).resolve(
            ProjectReference("123456")
        )
    )

    assert not result.succeeded
    assert result.diagnostics[0].code.value == "invalid-project-evidence"


def test_resource_manager_omits_empty_optional_display_name() -> None:
    """An absent provider display name remains None instead of an invented label."""
    response = _project_fixture()
    response.display_name = ""

    result = asyncio.run(
        ResourceManagerProjectResolver(FakeProjectsClient(response)).resolve(
            ProjectReference("tokyo-rain-123")
        )
    )

    assert result.project is not None
    assert result.project.display_name is None


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        "20",
        True,
        10**309,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_resource_manager_requires_a_positive_numeric_timeout(timeout: object) -> None:
    """The adapter refuses unbounded or mistyped transport timeout policy."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        ResourceManagerProjectResolver(
            FakeProjectsClient(_project_fixture()),
            timeout_seconds=cast("float", timeout),
        )


def test_resource_manager_requires_typed_project_reference() -> None:
    """The adapter boundary rejects raw project strings before provider access."""
    client = FakeProjectsClient(_project_fixture())

    with pytest.raises(TypeError, match="ProjectReference"):
        asyncio.run(
            ResourceManagerProjectResolver(client).resolve(
                cast("ProjectReference", "projects/415104041262")
            )
        )
    assert client.calls == []
