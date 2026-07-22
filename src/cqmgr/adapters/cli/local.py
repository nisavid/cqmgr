"""Human and structured rendering for offline local operations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import click

from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.application.operations.local import (
    ConfigOperationData,
    ProfileOperationData,
    ScopeOperationData,
)

if TYPE_CHECKING:
    from cqmgr.domain.results import OperationResult


def _scope_name(value: object) -> str:
    return "none" if value is None else value.canonical_name  # type: ignore[union-attr]


def _human_lines(result: OperationResult[Any]) -> list[str]:
    data = result.data
    if isinstance(data, ScopeOperationData):
        lines = [
            f"Resource scope: {_scope_name(result.resource_scope)}",
            f"Resolution source: {data.resolution_source or 'none'}",
        ]
        if data.reason is not None:
            lines.append(f"Reason: {data.reason}")
        return lines
    if isinstance(data, ConfigOperationData):
        return [f"{data.key} = {str(data.value).lower()}"]
    if isinstance(data, ProfileOperationData):
        if data.profile is not None:
            profile = data.profile
            lines = [
                f"Profile: {profile.name}",
                f"Selected: {str(data.selected_profile == profile.name).lower()}",
                f"Resource scope: {_scope_name(profile.resource_scope)}",
                f"ADC quota project: {_scope_name(profile.adc_quota_project)}",
                "Quota contact: "
                + (
                    "keyring reference configured"
                    if profile.quota_contact_keyring_reference is not None
                    else "none"
                ),
            ]
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


def emit_local_result(result: OperationResult[Any], output: str) -> int:
    """Write exactly one selected result form and return its global exit class."""
    exit_class = int(result.outcome.exit_class)
    if output == "json":
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
