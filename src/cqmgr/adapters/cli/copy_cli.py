"""Canonical, shell-safe Copy CLI serialization for read-only operations."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cqmgr.application.operations.read_only import (
        QuotaInspectSelector,
        ReadOnlyQuotaQuery,
    )
    from cqmgr.domain.accelerator_overlay import (
        CloudTpuSliceRequirement,
        ComputeInstanceRequirement,
    )
    from cqmgr.domain.scopes import ResourceScope

_MAXIMUM_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class CopyCliPresentation:
    """Explicit one-shot presentation inputs retained by a copied command."""

    output: str = "human"
    no_color: bool = False
    quiet: bool = False

    def __post_init__(self) -> None:
        """Reject output or terminal controls outside the public CLI vocabulary."""
        if self.output not in {"human", "json"}:
            msg = "Copy CLI output must be human or json"
            raise ValueError(msg)
        if not isinstance(self.no_color, bool) or not isinstance(self.quiet, bool):
            msg = "Copy CLI terminal controls must be boolean"
            raise TypeError(msg)


def quota_list_copy_cli(
    resource_scope: ResourceScope,
    query: ReadOnlyQuotaQuery,
    *,
    limit: int = 100,
    cursor: str | None = None,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one complete canonical quota-list command from typed public input."""
    _require_scope(resource_scope)
    _require_query(query)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAXIMUM_LIMIT
    ):
        msg = "Copy CLI quota-list limit must be from 1 through 1000"
        raise ValueError(msg)
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        msg = "Copy CLI cursor must be non-empty text or None"
        raise ValueError(msg)
    selected_presentation = presentation or CopyCliPresentation()
    if not isinstance(selected_presentation, CopyCliPresentation):
        msg = "Copy CLI presentation must use CopyCliPresentation"
        raise TypeError(msg)

    filters = query.filters
    arguments = [
        "cqmgr",
        "quota",
        "list",
        "--resource-scope",
        resource_scope.canonical_name,
    ]
    _repeat(arguments, "--service", filters.services)
    _repeat(
        arguments,
        "--catalog-group",
        tuple(item.value for item in filters.catalog_groups),
    )
    _repeat(
        arguments,
        "--accelerator",
        tuple(item.value for item in filters.accelerators),
    )
    _repeat(arguments, "--location", filters.locations)
    _repeat(
        arguments,
        "--quota-scope",
        tuple(item.value for item in filters.quota_scopes),
    )
    _repeat(arguments, "--quota-pool", filters.quota_pools)
    _optional_boolean(arguments, "--cataloged", value=filters.cataloged)
    _optional_boolean(arguments, "--guided", value=filters.guided)
    _optional_boolean(arguments, "--mutable", value=filters.mutable)
    _repeat(
        arguments,
        "--reconciliation",
        tuple(item.value for item in filters.reconciliations),
    )
    _repeat(
        arguments,
        "--grant-satisfaction",
        tuple(item.value for item in filters.grant_satisfactions),
    )
    _repeat(
        arguments,
        "--effective-confirmation",
        tuple(item.value for item in filters.effective_confirmations),
    )
    if filters.text is not None:
        arguments.extend(("--text", filters.text))
    _repeat(
        arguments,
        "--sort",
        tuple(f"{item.field.value}:{item.direction.value}" for item in query.sort),
    )
    arguments.extend(("--limit", str(limit)))
    if cursor is not None:
        arguments.extend(("--cursor", cursor))
    _append_presentation(arguments, selected_presentation)
    return shlex.join(arguments)


def quota_resolve_copy_cli(
    resource_scope: ResourceScope,
    requirement: ComputeInstanceRequirement | CloudTpuSliceRequirement,
    *,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one complete canonical workload-resolution command."""
    from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
        AllCompatibleLocations,
        CandidateLocations,
        CloudTpuSliceRequirement,
        ComputeInstanceRequirement,
    )

    _require_scope(resource_scope)
    if not isinstance(
        requirement,
        (ComputeInstanceRequirement, CloudTpuSliceRequirement),
    ):
        msg = "Copy CLI workload must use a supported typed requirement"
        raise TypeError(msg)
    selected_presentation = presentation or CopyCliPresentation()
    if not isinstance(selected_presentation, CopyCliPresentation):
        msg = "Copy CLI presentation must use CopyCliPresentation"
        raise TypeError(msg)

    leaf = (
        "compute-instance"
        if isinstance(requirement, ComputeInstanceRequirement)
        else "cloud-tpu-slice"
    )
    arguments = [
        "cqmgr",
        "quota",
        "resolve",
        leaf,
        "--resource-scope",
        resource_scope.canonical_name,
    ]
    if isinstance(requirement, ComputeInstanceRequirement):
        arguments.extend(
            (
                "--machine-type",
                requirement.machine_type,
                "--instance-count",
                str(requirement.instance_count),
            )
        )
    else:
        arguments.extend(
            (
                "--accelerator-type",
                requirement.accelerator_type,
                "--topology",
                requirement.topology,
                "--runtime-version",
                requirement.runtime_version,
                "--slice-count",
                str(requirement.slice_count),
            )
        )
    arguments.extend(("--provisioning-model", requirement.provisioning_model.value))
    locations = requirement.locations
    if isinstance(locations, CandidateLocations):
        _repeat(arguments, "--candidate", locations.values)
    elif isinstance(locations, AllCompatibleLocations):
        arguments.append("--all-compatible-locations")
    _append_presentation(arguments, selected_presentation)
    return shlex.join(arguments)


def quota_inspect_copy_cli(
    resource_scope: ResourceScope,
    selector: QuotaInspectSelector,
    *,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one exact-slice inspection using only canonical public selectors."""
    from cqmgr.application.operations.read_only import (  # noqa: PLC0415
        QuotaInspectSelector,
    )

    _require_scope(resource_scope)
    if not isinstance(selector, QuotaInspectSelector):
        msg = "Copy CLI quota inspect must use QuotaInspectSelector"
        raise TypeError(msg)
    selected_presentation = presentation or CopyCliPresentation()
    if not isinstance(selected_presentation, CopyCliPresentation):
        msg = "Copy CLI presentation must use CopyCliPresentation"
        raise TypeError(msg)

    arguments = [
        "cqmgr",
        "quota",
        "inspect",
        "--resource-scope",
        resource_scope.canonical_name,
        "--service",
        selector.service,
        "--quota-id",
        selector.quota_id,
        "--location",
        selector.location,
    ]
    _repeat(
        arguments,
        "--dimension",
        tuple(f"{key}={value}" for key, value in selector.dimensions.items),
    )
    _append_presentation(arguments, selected_presentation)
    return shlex.join(arguments)


def _require_scope(resource_scope: object) -> None:
    """Require the domain scope shape without importing it during module import."""
    from cqmgr.domain.scopes import ResourceScope  # noqa: PLC0415

    if not isinstance(resource_scope, ResourceScope):
        msg = "Copy CLI resource scope must use ResourceScope"
        raise TypeError(msg)


def _require_query(query: object) -> None:
    """Require the application query shape without creating an import cycle."""
    from cqmgr.application.operations.read_only import (  # noqa: PLC0415
        ReadOnlyQuotaQuery,
    )

    if not isinstance(query, ReadOnlyQuotaQuery):
        msg = "Copy CLI quota list must use ReadOnlyQuotaQuery"
        raise TypeError(msg)


def _repeat(arguments: list[str], option: str, values: tuple[str, ...]) -> None:
    """Append one canonical option occurrence per normalized value."""
    for value in values:
        arguments.extend((option, value))


def _optional_boolean(
    arguments: list[str],
    option: str,
    *,
    value: bool | None,
) -> None:
    """Append a present public boolean with its lowercase closed vocabulary."""
    if value is not None:
        arguments.extend((option, str(value).lower()))


def _append_presentation(
    arguments: list[str],
    presentation: CopyCliPresentation,
) -> None:
    """Append complete shared one-shot presentation inputs."""
    arguments.extend(("--output", presentation.output))
    if presentation.no_color:
        arguments.append("--no-color")
    if presentation.quiet:
        arguments.append("--quiet")
