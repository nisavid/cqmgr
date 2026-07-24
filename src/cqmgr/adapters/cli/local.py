"""Human and structured rendering for offline local operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import click

from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.application.operations.local import (
    ConfigOperationData,
    ProfileOperationData,
    RepositoryFailureData,
    ScopeOperationData,
)

if TYPE_CHECKING:
    from cqmgr.domain.results import OperationResult


@dataclass(frozen=True, slots=True)
class LocalPresentation:
    """Shared controls for an ANSI-free renderer with no optional progress prose.

    Local results currently contain no color and no suppressible progress text, so
    ``no_color`` and ``quiet`` preserve the same required result and safety facts.
    """

    output: str
    no_color: bool
    quiet: bool


def _scope_name(value: object) -> str:
    return "none" if value is None else value.canonical_name  # type: ignore[union-attr]


def _human_data_lines(result: OperationResult[Any]) -> list[str]:  # noqa: PLR0911
    data = result.data
    if isinstance(data, ScopeOperationData):
        lines = [
            f"Resource scope: {_scope_name(result.resource_scope)}",
            f"Resolution source: {data.resolution_source or 'none'}",
            "Authenticated principal: deferred (offline)",
        ]
        if data.reason is not None:
            lines.append(f"Reason: {data.reason}")
        return lines
    if isinstance(data, ConfigOperationData):
        return [f"{data.key} = {str(data.value).lower()}"]
    if isinstance(data, RepositoryFailureData):
        return [f"Guidance: {data.guidance}"]
    if isinstance(data, ProfileOperationData):
        if data.profile is not None:
            profile = data.profile
            lines = [
                f"Profile: {profile.name}",
                f"Selected: {str(data.selected_profile == profile.name).lower()}",
                f"Profile resource scope: {_scope_name(profile.resource_scope)}",
                f"ADC quota project: {_scope_name(profile.adc_quota_project)}",
                "Quota contact: "
                + (
                    "OS keyring reference configured for "
                    f"{profile.quota_contact_keyring_reference.service}/"
                    f"{profile.quota_contact_keyring_reference.account}"
                    if profile.quota_contact_keyring_reference is not None
                    else "none"
                ),
            ]
            if data.resolution_source is not None:
                lines.extend(
                    (
                        "Effective resource scope: "
                        f"{_scope_name(result.resource_scope)}",
                        f"Resolution source: {data.resolution_source}",
                    )
                )
            if data.reason is not None:
                lines.append(f"Reason: {data.reason}")
            return lines
        if not data.profiles:
            return (
                [f"Reason: {data.reason}"]
                if data.reason
                else ["No profiles configured."]
            )
        return [
            f"{'*' if profile.name == data.selected_profile else ' '} {profile.name}: "
            f"{_scope_name(profile.resource_scope)}"
            for profile in data.profiles
        ]
    return [json.dumps(operation_result_mapping(result), sort_keys=True)]


def _human_lines(result: OperationResult[Any]) -> list[str]:
    data_lines = _human_data_lines(result)
    if int(result.outcome.exit_class) == 0:
        return data_lines
    reached = "reached" if result.boundary.reached else "not reached"
    envelope = [
        f"Operation: {result.operation.value}",
        (
            f"Outcome: {result.outcome.code.value} "
            f"(exit {int(result.outcome.exit_class)})"
        ),
        f"Boundary: {result.boundary.condition.value} ({reached})",
        f"Complete: {str(result.completeness.is_complete).lower()}",
        f"Resource scope: {_scope_name(result.resource_scope)}",
    ]
    return [*envelope, *data_lines]


def emit_local_result(
    result: OperationResult[Any],
    presentation: LocalPresentation,
) -> int:
    """Write exactly one selected result form and return its global exit class."""
    exit_class = int(result.outcome.exit_class)
    if presentation.output == "json":
        click.echo(
            json.dumps(
                operation_result_mapping(result),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        for line in _human_lines(result):
            click.echo(line, err=exit_class != 0)
    return exit_class
