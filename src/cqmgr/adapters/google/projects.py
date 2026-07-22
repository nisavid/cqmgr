"""Official Resource Manager client adapter for canonical project evidence."""

from __future__ import annotations

import math
import warnings
from typing import TYPE_CHECKING, Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import resourcemanager_v3

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

if TYPE_CHECKING:
    from google.cloud.resourcemanager_v3.types import Project


class ProjectsClient(Protocol):
    """Narrow official-client surface used for read-only project lookup."""

    async def get_project(
        self,
        *,
        name: str,
        timeout: float,  # noqa: ASYNC109
    ) -> Project:
        """Retrieve exactly one project by its explicit Resource Manager name."""
        ...


class ResourceManagerProjectResolver:
    """Canonicalize explicit projects through Resource Manager GetProject only."""

    def __init__(
        self,
        client: ProjectsClient,
        *,
        timeout_seconds: float = 20.0,
    ) -> None:
        """Bind the shared-credential official client and bounded call timeout."""
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            msg = "project resolution timeout_seconds must be positive"
            raise ValueError(msg)  # noqa: TRY004  # one invalid-policy contract
        try:
            timeout = float(timeout_seconds)
        except OverflowError:
            msg = "project resolution timeout_seconds must be positive"
            raise ValueError(msg) from None
        if not math.isfinite(timeout) or timeout <= 0:
            msg = "project resolution timeout_seconds must be positive"
            raise ValueError(msg)
        self._client = client
        self._timeout_seconds = timeout

    async def resolve(self, reference: ProjectReference) -> ProjectResolution:
        """Return canonical active project evidence without retaining raw DTOs."""
        if not isinstance(reference, ProjectReference):
            msg = "project resolver requires a ProjectReference"
            raise TypeError(msg)
        try:
            response = await self._client.get_project(
                name=reference.lookup_name,
                timeout=self._timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001  # provider text is discarded
            return _provider_failure(reference, error)

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Unrecognized State enum value: [0-9]+\Z",
                category=UserWarning,
                module=r"proto\.marshal\.rules\.enums",
            )
            state = response.state

        if state not in (
            resourcemanager_v3.Project.State.ACTIVE,
            resourcemanager_v3.Project.State.DELETE_REQUESTED,
        ):
            return _failure(
                reference,
                code="invalid-project-evidence",
                guidance=(
                    "Resource Manager returned project evidence without a state; "
                    "refresh and retry."
                ),
                retry=RetryDisposition.AFTER_REFRESH,
            )
        if state == resourcemanager_v3.Project.State.DELETE_REQUESTED:
            return _failure(
                reference,
                code="project-not-active",
                guidance="Select an active Google Cloud project, then retry.",
                retry=RetryDisposition.NEVER,
            )
        try:
            project = _canonical_project(response)
        except (TypeError, ValueError):
            return _failure(
                reference,
                code="invalid-project-evidence",
                guidance=(
                    "Resource Manager returned invalid canonical project evidence; "
                    "refresh and retry."
                ),
                retry=RetryDisposition.AFTER_REFRESH,
            )
        if not _matches_reference(reference, project):
            return _failure(
                reference,
                code="invalid-project-evidence",
                guidance=(
                    "Resource Manager returned project evidence that does not match "
                    "the explicit input."
                ),
                retry=RetryDisposition.NEVER,
            )
        return ProjectResolution(reference=reference, project=project)


def _canonical_project(response: Project) -> CanonicalProject:
    display_name = response.display_name or None
    return CanonicalProject(
        resource_scope=ResourceScope(
            ResourceScopeKind.PROJECT,
            response.name,
        ),
        project_id=response.project_id,
        display_name=display_name,
    )


def _matches_reference(
    reference: ProjectReference,
    project: CanonicalProject,
) -> bool:
    identifier = reference.lookup_name.removeprefix("projects/")
    if identifier.isdigit():
        return project.resource_scope.canonical_name == reference.lookup_name
    return project.project_id == identifier


def _failure(
    reference: ProjectReference,
    *,
    code: str,
    guidance: str,
    retry: RetryDisposition,
) -> ProjectResolution:
    diagnostic = Diagnostic(
        code=DiagnosticCode(code),
        severity=Severity.ERROR,
        phase=DiagnosticPhase("project-resolution"),
        source=DiagnosticSource("resource-manager"),
        retry=retry,
        message=RedactedText(guidance),
    )
    return ProjectResolution(
        reference=reference,
        project=None,
        diagnostics=(diagnostic,),
    )


def _provider_failure(
    reference: ProjectReference,
    error: Exception,
) -> ProjectResolution:
    """Classify only documented provider failure families using static guidance."""
    if isinstance(
        error,
        (google_exceptions.PermissionDenied, google_exceptions.Unauthenticated),
    ):
        return _failure(
            reference,
            code="project-authorization-failed",
            guidance="Grant Resource Manager project read access, then retry.",
            retry=RetryDisposition.NEVER,
        )
    if isinstance(error, google_exceptions.NotFound):
        return _failure(
            reference,
            code="project-not-found",
            guidance="Check the explicit project ID or number, then retry.",
            retry=RetryDisposition.NEVER,
        )
    if isinstance(error, google_exceptions.InvalidArgument):
        return _failure(
            reference,
            code="invalid-project-reference",
            guidance="Supply a valid explicit project ID or number.",
            retry=RetryDisposition.NEVER,
        )
    if isinstance(
        error,
        (
            google_exceptions.DeadlineExceeded,
            google_exceptions.InternalServerError,
            google_exceptions.ResourceExhausted,
            google_exceptions.ServiceUnavailable,
        ),
    ):
        return _failure(
            reference,
            code="project-resolution-transient",
            guidance="Retry project resolution within the operation deadline.",
            retry=RetryDisposition.AFTER_BACKOFF,
        )
    return _failure(
        reference,
        code="project-resolution-failed",
        guidance="Check Resource Manager access and refresh project evidence.",
        retry=RetryDisposition.UNKNOWN,
    )
