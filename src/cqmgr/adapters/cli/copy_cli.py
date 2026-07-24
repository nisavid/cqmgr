"""Canonical, shell-safe Copy CLI serialization for read-only operations."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cqmgr.adapters.cli.group import canonical_command_path
from cqmgr.adapters.cli.lifecycle import canonical_absolute_rfc3339

if TYPE_CHECKING:
    from cqmgr.application.operations.read_only import (
        QuotaInspectSelector,
        ReadOnlyQuotaQuery,
    )
    from cqmgr.domain.accelerator_overlay import (
        CloudTpuSliceRequirement,
        ComputeInstanceRequirement,
    )
    from cqmgr.domain.obtainability import (
        DistributionShape,
        ObtainabilityCandidate,
        SpotMachineConfiguration,
    )
    from cqmgr.domain.plans import TargetStrategy
    from cqmgr.domain.quotas import NormalizedDimensions
    from cqmgr.domain.scopes import ResourceScope
    from cqmgr.domain.status import WatchCondition

_MAXIMUM_LIMIT = 1000
_QUOTA_LIST_COMMAND = canonical_command_path("cqmgr", "quota", "list")
_QUOTA_INSPECT_COMMAND = canonical_command_path("cqmgr", "quota", "inspect")
_COMPUTE_RESOLVE_COMMAND = canonical_command_path(
    "cqmgr",
    "quota",
    "resolve",
    "compute-instance",
)
_CLOUD_TPU_RESOLVE_COMMAND = canonical_command_path(
    "cqmgr",
    "quota",
    "resolve",
    "cloud-tpu-slice",
)
_OBTAINABILITY_COMPARE_COMMAND = canonical_command_path(
    "cqmgr",
    "obtainability",
    "compare",
)
_REQUEST_COMPOSE_COMMAND = canonical_command_path("cqmgr", "request", "compose")
_REQUEST_PREVIEW_COMMAND = canonical_command_path("cqmgr", "request", "preview")
_REQUEST_WATCH_COMMAND = canonical_command_path("cqmgr", "request", "watch")
_PLAN_REVIEW_COMMAND = canonical_command_path("cqmgr", "plan", "review")
_PLAN_APPLY_COMMAND = canonical_command_path("cqmgr", "plan", "apply")
_RESOURCE_SCOPE_ACKNOWLEDGEMENT_PLACEHOLDER = "<RESOURCE_SCOPE>"


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


@dataclass(frozen=True, slots=True)
class WatchCopyCliPresentation:
    """Explicit stream controls retained by a copied Watch command."""

    output: str = "human"
    no_color: bool = False
    quiet: bool = False

    def __post_init__(self) -> None:
        """Reject presentation controls outside the Watch vocabulary."""
        if self.output not in {"human", "jsonl"}:
            msg = "Copy CLI Watch output must be human or jsonl"
            raise ValueError(msg)
        if not isinstance(self.no_color, bool) or not isinstance(self.quiet, bool):
            msg = "Copy CLI Watch terminal controls must be boolean"
            raise TypeError(msg)


def request_exact_copy_cli(  # noqa: PLR0913
    command: str,
    resource_scope: ResourceScope,
    *,
    service: str,
    quota_id: str,
    location: str,
    dimensions: NormalizedDimensions,
    target: str,
    acknowledgements: tuple[str, ...] = (),
    expert: bool = False,
    quota_contact_stdin: bool = False,
    plan_out: Path | None = None,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one exact-slice Compose or Preview without protected values."""
    from cqmgr.domain.quotas import NormalizedDimensions  # noqa: PLC0415

    _require_scope(resource_scope)
    if command not in {"compose", "preview"}:
        msg = "Copy CLI request command must be compose or preview"
        raise ValueError(msg)
    if any(
        not isinstance(value, str) or not value
        for value in (service, quota_id, location, target)
    ):
        msg = "Copy CLI exact request selectors and target must be non-empty"
        raise ValueError(msg)
    if not isinstance(dimensions, NormalizedDimensions):
        msg = "Copy CLI exact request dimensions must be NormalizedDimensions"
        raise TypeError(msg)
    _require_acknowledgements(acknowledgements)
    if not isinstance(expert, bool):
        msg = "Copy CLI expert intent must be boolean"
        raise TypeError(msg)
    if not isinstance(quota_contact_stdin, bool):
        msg = "Copy CLI contact mode must be boolean"
        raise TypeError(msg)
    if plan_out is not None and (
        command != "preview" or not isinstance(plan_out, Path)
    ):
        msg = "Copy CLI plan output is available only for Preview"
        raise ValueError(msg)
    selected_presentation = _copy_presentation(presentation)
    arguments = [
        *(
            _REQUEST_COMPOSE_COMMAND
            if command == "compose"
            else _REQUEST_PREVIEW_COMMAND
        ),
        "--resource-scope",
        resource_scope.canonical_name,
        "--service",
        service,
        "--quota-id",
        quota_id,
        "--location",
        location,
    ]
    _repeat(
        arguments,
        "--dimension",
        tuple(f"{key}={value}" for key, value in dimensions.items),
    )
    arguments.extend(("--target", target))
    _repeat(arguments, "--acknowledge", acknowledgements)
    if expert:
        arguments.append("--expert")
    if quota_contact_stdin:
        arguments.append("--quota-contact-stdin")
    if plan_out is not None:
        arguments.extend(("--plan-out", str(plan_out)))
    _append_presentation(arguments, selected_presentation)
    return shlex.join(arguments)


def request_workload_copy_cli(  # noqa: C901, PLR0912, PLR0913, PLR0915
    command: str,
    resource_scope: ResourceScope,
    requirement: ComputeInstanceRequirement | CloudTpuSliceRequirement,
    *,
    target_strategy: TargetStrategy,
    targets: tuple[tuple[str, str], ...] = (),
    acknowledgements: tuple[str, ...] = (),
    expert: bool = False,
    quota_contact_stdin: bool = False,
    plan_out: Path | None = None,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one workload Compose or Preview without protected values."""
    from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
        AllCompatibleLocations,
        CandidateLocations,
        CloudTpuSliceRequirement,
        ComputeInstanceRequirement,
    )
    from cqmgr.domain.plans import TargetStrategy  # noqa: PLC0415

    _require_scope(resource_scope)
    if command not in {"compose", "preview"}:
        msg = "Copy CLI request command must be compose or preview"
        raise ValueError(msg)
    if not isinstance(
        requirement,
        (ComputeInstanceRequirement, CloudTpuSliceRequirement),
    ):
        msg = "Copy CLI workload request requires one typed requirement"
        raise TypeError(msg)
    if not isinstance(target_strategy, TargetStrategy):
        msg = "Copy CLI target strategy must use TargetStrategy"
        raise TypeError(msg)
    if (
        not isinstance(targets, tuple)
        or any(
            not isinstance(child_id, str)
            or not child_id
            or not isinstance(value, str)
            or not value
            for child_id, value in targets
        )
        or len({child_id for child_id, _ in targets}) != len(targets)
    ):
        msg = "Copy CLI manual targets must be unique non-empty child values"
        raise ValueError(msg)
    if target_strategy is TargetStrategy.MANUAL and not targets:
        msg = "Copy CLI manual strategy requires child targets"
        raise ValueError(msg)
    if target_strategy is not TargetStrategy.MANUAL and targets:
        msg = "Copy CLI derived strategies do not accept child targets"
        raise ValueError(msg)
    _require_acknowledgements(acknowledgements)
    if not isinstance(expert, bool):
        msg = "Copy CLI expert intent must be boolean"
        raise TypeError(msg)
    if not isinstance(quota_contact_stdin, bool):
        msg = "Copy CLI contact mode must be boolean"
        raise TypeError(msg)
    if plan_out is not None and (
        command != "preview" or not isinstance(plan_out, Path)
    ):
        msg = "Copy CLI plan output is available only for Preview"
        raise ValueError(msg)
    arguments = [
        *(
            _REQUEST_COMPOSE_COMMAND
            if command == "compose"
            else _REQUEST_PREVIEW_COMMAND
        ),
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
        if requirement.attached_accelerator_type is not None:
            arguments.extend(
                (
                    "--attached-accelerator-type",
                    requirement.attached_accelerator_type,
                    "--attached-accelerator-count",
                    str(requirement.attached_accelerator_count),
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
    arguments.extend(
        (
            "--provisioning-model",
            requirement.provisioning_model.value,
        )
    )
    if isinstance(requirement.locations, CandidateLocations):
        _repeat(arguments, "--candidate", requirement.locations.values)
    elif isinstance(requirement.locations, AllCompatibleLocations):
        arguments.append("--all-compatible-locations")
    arguments.extend(("--target-strategy", target_strategy.value))
    _repeat(
        arguments,
        "--target",
        tuple(f"{child_id}={value}" for child_id, value in targets),
    )
    _repeat(arguments, "--acknowledge", acknowledgements)
    if expert:
        arguments.append("--expert")
    if quota_contact_stdin:
        arguments.append("--quota-contact-stdin")
    if plan_out is not None:
        arguments.extend(("--plan-out", str(plan_out)))
    _append_presentation(arguments, _copy_presentation(presentation))
    return shlex.join(arguments)


def plan_review_copy_cli(
    *,
    digest: str | None = None,
    path: Path | None = None,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one canonical Plan Review reference."""
    arguments = [*_PLAN_REVIEW_COMMAND]
    _append_plan_reference(arguments, digest=digest, path=path)
    _append_presentation(arguments, _copy_presentation(presentation))
    return shlex.join(arguments)


def plan_apply_copy_cli(
    *,
    digest: str | None = None,
    path: Path | None = None,
    acknowledge_resource_scope: str | None = None,
    quota_contact_stdin: bool = False,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render Apply with an explicitly incomplete acknowledgement by default."""
    if acknowledge_resource_scope is not None and (
        not isinstance(acknowledge_resource_scope, str)
        or not acknowledge_resource_scope
    ):
        msg = "Copy CLI Apply acknowledgement must be non-empty or None"
        raise ValueError(msg)
    if not isinstance(quota_contact_stdin, bool):
        msg = "Copy CLI Apply contact mode must be boolean"
        raise TypeError(msg)
    arguments = [*_PLAN_APPLY_COMMAND]
    _append_plan_reference(arguments, digest=digest, path=path)
    arguments.extend(
        (
            "--acknowledge-resource-scope",
            acknowledge_resource_scope or _RESOURCE_SCOPE_ACKNOWLEDGEMENT_PLACEHOLDER,
        )
    )
    if quota_contact_stdin:
        arguments.append("--quota-contact-stdin")
    _append_presentation(arguments, _copy_presentation(presentation))
    return shlex.join(arguments)


def request_watch_copy_cli(
    *,
    deadline: str,
    intent_id: str | None = None,
    condition: WatchCondition | None = None,
    resume: str | None = None,
    presentation: WatchCopyCliPresentation | None = None,
) -> str:
    """Render one initial or resumed Watch command with explicit deadline."""
    from cqmgr.domain.status import WatchCondition  # noqa: PLC0415

    canonical_deadline = canonical_absolute_rfc3339(deadline)
    initial = intent_id is not None
    resumed = resume is not None
    if initial == resumed:
        msg = "Copy CLI Watch requires exactly one intent ID or resume token"
        raise ValueError(msg)
    if initial and (
        not isinstance(intent_id, str)
        or not intent_id
        or not isinstance(condition, WatchCondition)
    ):
        msg = "Copy CLI initial Watch requires intent ID and condition"
        raise ValueError(msg)
    if resumed and (not isinstance(resume, str) or not resume or condition is not None):
        msg = "Copy CLI resumed Watch recovers its condition from the token"
        raise ValueError(msg)
    selected = presentation or WatchCopyCliPresentation()
    if not isinstance(selected, WatchCopyCliPresentation):
        msg = "Copy CLI Watch presentation must use WatchCopyCliPresentation"
        raise TypeError(msg)
    arguments = [*_REQUEST_WATCH_COMMAND]
    if initial:
        selected_intent = cast("str", intent_id)
        selected_condition = cast("WatchCondition", condition)
        arguments.extend(
            (
                "--intent-id",
                selected_intent,
                "--condition",
                selected_condition.value,
            )
        )
    else:
        arguments.extend(("--resume", cast("str", resume)))
    arguments.extend(("--deadline", canonical_deadline, "--output", selected.output))
    if selected.no_color:
        arguments.append("--no-color")
    if selected.quiet:
        arguments.append("--quiet")
    return shlex.join(arguments)


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
        *_QUOTA_LIST_COMMAND,
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

    command = (
        _COMPUTE_RESOLVE_COMMAND
        if isinstance(requirement, ComputeInstanceRequirement)
        else _CLOUD_TPU_RESOLVE_COMMAND
    )
    arguments = [
        *command,
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
        if requirement.attached_accelerator_type is not None:
            arguments.extend(
                (
                    "--attached-accelerator-type",
                    requirement.attached_accelerator_type,
                    "--attached-accelerator-count",
                    str(requirement.attached_accelerator_count),
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
        *_QUOTA_INSPECT_COMMAND,
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


def obtainability_compare_copy_cli(
    resource_scope: ResourceScope,
    candidates: tuple[ObtainabilityCandidate, ...],
    *,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one exact explicit-candidate Spot comparison."""
    from cqmgr.domain.obtainability import ObtainabilityCandidate  # noqa: PLC0415

    _require_scope(resource_scope)
    if (
        not isinstance(candidates, tuple)
        or not candidates
        or any(not isinstance(item, ObtainabilityCandidate) for item in candidates)
    ):
        msg = "Copy CLI obtainability comparison requires typed candidates"
        raise ValueError(msg)
    if len({item.candidate_id for item in candidates}) != len(candidates):
        msg = "Copy CLI obtainability candidates must be unique"
        raise ValueError(msg)
    if (
        len(
            {
                (item.machine, item.vm_count, item.distribution_shape)
                for item in candidates
            }
        )
        != 1
    ):
        msg = "Copy CLI obtainability comparison must keep one request fixed"
        raise ValueError(msg)
    selected_presentation = presentation or CopyCliPresentation()
    if not isinstance(selected_presentation, CopyCliPresentation):
        msg = "Copy CLI presentation must use CopyCliPresentation"
        raise TypeError(msg)
    first = candidates[0]
    arguments = [
        *_OBTAINABILITY_COMPARE_COMMAND,
        "--resource-scope",
        resource_scope.canonical_name,
        *_obtainability_shape_arguments(first),
    ]
    _repeat(
        arguments,
        "--candidate",
        tuple(
            candidate.endpoint_region
            + ("=" + ",".join(candidate.zones) if candidate.zones else "")
            for candidate in candidates
        ),
    )
    _append_presentation(arguments, selected_presentation)
    return shlex.join(arguments)


def obtainability_all_compatible_copy_cli(
    resource_scope: ResourceScope,
    requirement: ComputeInstanceRequirement,
    *,
    machine: SpotMachineConfiguration,
    distribution_shape: DistributionShape,
    presentation: CopyCliPresentation | None = None,
) -> str:
    """Render one explicit all-compatible Spot comparison."""
    from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
        AllCompatibleLocations,
        ComputeInstanceRequirement,
        ProvisioningModel,
    )
    from cqmgr.domain.obtainability import (  # noqa: PLC0415
        DistributionShape,
        SpotMachineConfiguration,
    )

    _require_scope(resource_scope)
    if (
        not isinstance(requirement, ComputeInstanceRequirement)
        or not isinstance(requirement.locations, AllCompatibleLocations)
        or requirement.provisioning_model is not ProvisioningModel.SPOT
    ):
        msg = "Copy CLI all-compatible comparison requires one Spot requirement"
        raise ValueError(msg)
    machine_is_typed = isinstance(machine, SpotMachineConfiguration)
    gpu = machine.gpu if machine_is_typed else None
    attachment_matches = (
        gpu is None
        and requirement.attached_accelerator_type is None
        and requirement.attached_accelerator_count is None
    ) or (
        gpu is not None
        and gpu.accelerator_type == requirement.attached_accelerator_type
        and gpu.count == requirement.attached_accelerator_count
    )
    if (
        not machine_is_typed
        or machine.machine_type != requirement.machine_type
        or not attachment_matches
        or not isinstance(distribution_shape, DistributionShape)
    ):
        msg = "Copy CLI all-compatible shape must match the resolved requirement"
        raise ValueError(msg)
    selected_presentation = presentation or CopyCliPresentation()
    if not isinstance(selected_presentation, CopyCliPresentation):
        msg = "Copy CLI presentation must use CopyCliPresentation"
        raise TypeError(msg)
    synthetic = _ObtainabilityCopyShape(
        machine,
        requirement.instance_count,
        distribution_shape,
    )
    arguments = [
        *_OBTAINABILITY_COMPARE_COMMAND,
        "--resource-scope",
        resource_scope.canonical_name,
        *_obtainability_shape_arguments(synthetic),
        "--all-compatible-locations",
    ]
    _append_presentation(arguments, selected_presentation)
    return shlex.join(arguments)


@dataclass(frozen=True, slots=True)
class _ObtainabilityCopyShape:
    machine: SpotMachineConfiguration
    vm_count: int
    distribution_shape: DistributionShape


def _obtainability_shape_arguments(
    candidate: ObtainabilityCandidate | _ObtainabilityCopyShape,
) -> tuple[str, ...]:
    machine = candidate.machine
    arguments = [
        "--machine-type",
        machine.machine_type,
    ]
    if machine.gpu is not None:
        arguments.extend(
            (
                "--gpu-type",
                machine.gpu.accelerator_type,
                "--gpu-count",
                str(machine.gpu.count),
            )
        )
    arguments.extend(
        (
            "--vm-count",
            str(candidate.vm_count),
            "--distribution-shape",
            candidate.distribution_shape.value,
        )
    )
    return tuple(arguments)


def _require_scope(resource_scope: object) -> None:
    """Require the domain scope shape without importing it during module import."""
    from cqmgr.domain.scopes import ResourceScope  # noqa: PLC0415

    if not isinstance(resource_scope, ResourceScope):
        msg = "Copy CLI resource scope must use ResourceScope"
        raise TypeError(msg)


def _require_acknowledgements(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) or not value for value in values
    ):
        msg = "Copy CLI acknowledgements must be non-empty text"
        raise ValueError(msg)


def _copy_presentation(
    presentation: CopyCliPresentation | None,
) -> CopyCliPresentation:
    selected = presentation or CopyCliPresentation()
    if not isinstance(selected, CopyCliPresentation):
        msg = "Copy CLI presentation must use CopyCliPresentation"
        raise TypeError(msg)
    return selected


def _append_plan_reference(
    arguments: list[str],
    *,
    digest: str | None,
    path: Path | None,
) -> None:
    if (digest is None) == (path is None):
        msg = "Copy CLI Plan reference requires exactly one digest or path"
        raise ValueError(msg)
    if digest is not None:
        if not isinstance(digest, str) or not digest:
            msg = "Copy CLI Plan digest must be non-empty"
            raise ValueError(msg)
        arguments.extend(("--plan", digest))
        return
    if not isinstance(path, Path):
        msg = "Copy CLI Plan path must use Path"
        raise TypeError(msg)
    arguments.extend(("--plan-file", str(path)))


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
