"""Explicit project references and canonical Resource Manager evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from cqmgr.domain.diagnostics import Diagnostic
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
_PROJECT_NUMBER = re.compile(r"[0-9]+\Z")


@dataclass(frozen=True, slots=True)
class ProjectReference:
    """One explicit project ID, number, or canonical numeric resource name."""

    value: str

    def __post_init__(self) -> None:
        """Reject other resource kinds, paths, and invalid project IDs."""
        if not isinstance(self.value, str):
            msg = "project reference must be a string"
            raise TypeError(msg)
        identifier = self.value.removeprefix("projects/")
        if (
            not identifier
            or "/" in identifier
            or (
                _PROJECT_ID.fullmatch(identifier) is None
                and _PROJECT_NUMBER.fullmatch(identifier) is None
            )
        ):
            msg = "project reference must be an explicit project ID or number"
            raise ValueError(msg)
        if (
            self.value.startswith("projects/")
            and _PROJECT_NUMBER.fullmatch(identifier) is None
        ):
            msg = "canonical project reference must contain an ASCII project number"
            raise ValueError(msg)

    @property
    def lookup_name(self) -> str:
        """Return the exact Resource Manager v3 GetProject name."""
        return f"projects/{self.value.removeprefix('projects/')}"


@dataclass(frozen=True, slots=True)
class CanonicalProject:
    """Safe normalized evidence returned by Resource Manager GetProject."""

    resource_scope: ResourceScope
    project_id: str
    display_name: str | None

    def __post_init__(self) -> None:
        """Require one canonical numeric project scope and matching project ID shape."""
        if not isinstance(self.resource_scope, ResourceScope) or (
            self.resource_scope.kind is not ResourceScopeKind.PROJECT
        ):
            msg = "canonical project requires a project resource scope"
            raise ValueError(msg)
        if (
            not isinstance(self.project_id, str)
            or _PROJECT_ID.fullmatch(self.project_id) is None
        ):
            msg = "canonical project_id must use the provider project ID grammar"
            raise ValueError(msg)
        if self.display_name is not None and (
            not isinstance(self.display_name, str) or not self.display_name
        ):
            msg = "project display_name must be a non-empty string or None"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ProjectResolution:
    """Canonical project evidence or one typed fail-closed diagnostic."""

    reference: ProjectReference
    project: CanonicalProject | None
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        """Keep successful and failed resolution evidence unambiguous."""
        if not isinstance(self.reference, ProjectReference):
            msg = "project resolution reference must use ProjectReference"
            raise TypeError(msg)
        if self.project is not None and not isinstance(self.project, CanonicalProject):
            msg = "project resolution project must use CanonicalProject"
            raise TypeError(msg)
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            msg = "project resolution diagnostics must contain Diagnostic values"
            raise TypeError(msg)
        if (self.project is None) == (not self.diagnostics):
            msg = (
                "project resolution must contain either project evidence or diagnostics"
            )
            raise ValueError(msg)

    @property
    def succeeded(self) -> bool:
        """Whether Resource Manager supplied valid canonical project evidence."""
        return self.project is not None
