"""Canonical project resolution domain contracts."""

from __future__ import annotations

import pytest

from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.projects import CanonicalProject, ProjectReference, ProjectResolution
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind


@pytest.mark.parametrize(
    ("value", "lookup_name"),
    [
        ("projects/415104041262", "projects/415104041262"),
        ("415104041262", "projects/415104041262"),
        ("tokyo-rain-123", "projects/tokyo-rain-123"),
    ],
)
def test_project_reference_builds_the_official_get_name(
    value: str,
    lookup_name: str,
) -> None:
    """Accepted project inputs map only to Resource Manager get names."""
    assert ProjectReference(value).lookup_name == lookup_name


@pytest.mark.parametrize(
    "value",
    [
        "",
        "projects/",
        "projects/tokyo-rain-123",
        "folders/123",
        "UPPERCASE",
        "short",
        "a/../b",
    ],
)
def test_project_reference_rejects_non_project_or_ambiguous_inputs(value: str) -> None:
    """A project resolver never infers from another resource kind or path."""
    with pytest.raises(ValueError, match="project reference"):
        ProjectReference(value)


def test_canonical_project_binds_numeric_scope_and_project_id() -> None:
    """Resource Manager evidence preserves both canonical number and project ID."""
    project = CanonicalProject(
        resource_scope=ResourceScope(
            ResourceScopeKind.PROJECT,
            "projects/415104041262",
        ),
        project_id="tokyo-rain-123",
        display_name="Tokyo Rain",
    )

    assert project.resource_scope.canonical_name == "projects/415104041262"
    assert project.project_id == "tokyo-rain-123"


def test_canonical_project_rejects_non_project_scope() -> None:
    """Canonical project evidence cannot carry a folder or organization."""
    with pytest.raises(ValueError, match="project resource scope"):
        CanonicalProject(
            resource_scope=ResourceScope(ResourceScopeKind.FOLDER, "folders/123"),
            project_id="tokyo-rain-123",
            display_name=None,
        )


def test_project_resolution_requires_exactly_one_success_or_failure_shape() -> None:
    """A resolution cannot be empty or combine a project with failure diagnostics."""
    reference = ProjectReference("tokyo-rain-123")
    project = CanonicalProject(
        resource_scope=ResourceScope(
            ResourceScopeKind.PROJECT,
            "projects/415104041262",
        ),
        project_id="tokyo-rain-123",
        display_name=None,
    )
    diagnostic = Diagnostic(
        code=DiagnosticCode("project-resolution-failed"),
        severity=Severity.ERROR,
        phase=DiagnosticPhase("project-resolution"),
        source=DiagnosticSource("resource-manager"),
        retry=RetryDisposition.AFTER_REFRESH,
        message=RedactedText("Retry."),
    )

    with pytest.raises(ValueError, match="either project evidence or diagnostics"):
        ProjectResolution(reference, None)
    with pytest.raises(ValueError, match="either project evidence or diagnostics"):
        ProjectResolution(reference, project, (diagnostic,))
