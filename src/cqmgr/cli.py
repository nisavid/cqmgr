"""TTY-aware, offline-safe command-line entry point."""

# Click supplies declared boolean flags positionally to leaf callbacks.
# ruff: noqa: FBT001

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, override

import click

from cqmgr.adapters.cli.audit import (
    AuditPresentation,
    emit_audit_result,
    parse_audit_query,
)
from cqmgr.adapters.cli.group import CanonicalAliasGroup
from cqmgr.adapters.cli.local import LocalPresentation, emit_local_result
from cqmgr.adapters.cli.read_only import Presentation, emit_read_only_result
from cqmgr.adapters.cli.read_only_requests import (
    parse_cloud_tpu_slice_requirement,
    parse_compute_instance_requirement,
    parse_dimensions,
    parse_obtainability_candidates,
    parse_obtainability_shape,
    parse_read_only_quota_query,
)
from cqmgr.application.configuration import (
    InterfaceSettingKey,
    parse_resource_scope_name,
)
from cqmgr.application.operations.quotas import QuotaBrowseRequest
from cqmgr.application.operations.read_only import (
    QuotaInspectSelector,
    ReadOnlyScopeInput,
)
from cqmgr.application.ports.configuration import ConfigurationRepositoryError
from cqmgr.bootstrap import (
    InvocationKind,
    build_audit_operations,
    build_local_operations,
    build_quota_cursor_operations,
    build_read_only_operations,
    classify_invocation,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from cqmgr.application.operations.local import LocalOperations

_OUTPUT_OPTION = click.option(
    "--output",
    type=click.Choice(("human", "json"), case_sensitive=True),
    default="human",
    show_default=True,
)
_PROVIDER_OPERATION_SECONDS = 60.0


def _require_obtainability_location_mode(
    candidates: tuple[str, ...],
    *,
    all_compatible: bool,
) -> None:
    """Require exactly one explicit or resolver-expanded candidate mode."""
    if all_compatible == bool(candidates):
        msg = "select exactly one obtainability location mode"
        raise ValueError(msg)


def _presentation_options[CommandT: Callable[..., Any]](
    function: CommandT,
) -> CommandT:
    """Add shared options to one leaf command in canonical order."""
    decorated = click.option("--quiet", is_flag=True)(function)
    return click.option("--no-color", is_flag=True)(decorated)


def _scope_options[CommandT: Callable[..., Any]](
    function: CommandT,
) -> CommandT:
    """Add explicit project/profile selection to one provider leaf."""
    decorated = click.option("--profile")(function)
    return click.option("--resource-scope")(decorated)


_CURRENT_INVOCATION_KIND: ContextVar[InvocationKind | None] = ContextVar(
    "cqmgr_invocation_kind",
    default=None,
)


def _interactive_streams() -> tuple[bool, bool]:
    """Expose one deterministic seam for terminal dispatch policy."""
    return sys.stdin.isatty(), sys.stdout.isatty()


class ClassifiedRootGroup(CanonicalAliasGroup):
    """Classify the complete invocation before Click parses nested metadata."""

    @override
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        """Preserve raw argv for bootstrap policy across Click dispatch."""
        raw_arguments = tuple(sys.argv[1:] if args is None else args)
        stdin_is_tty, stdout_is_tty = _interactive_streams()
        invocation_kind = classify_invocation(
            raw_arguments,
            stdin_is_tty=stdin_is_tty,
            stdout_is_tty=stdout_is_tty,
        )
        token = _CURRENT_INVOCATION_KIND.set(invocation_kind)
        try:
            return super().main(
                raw_arguments,
                prog_name,
                complete_var,
                standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        finally:
            _CURRENT_INVOCATION_KIND.reset(token)


async def _run_async(
    operations: LocalOperations,
    operation_name: str,
    callback: Callable[[], Awaitable[Any]],
    presentation: LocalPresentation,
) -> None:
    try:
        result = await callback()
    except ConfigurationRepositoryError as error:
        result = await operations.repository_failure(operation_name, error)
    exit_class = emit_local_result(result, presentation)
    if exit_class:
        raise click.exceptions.Exit(exit_class)


def _run(
    operations: LocalOperations,
    operation_name: str,
    callback: Callable[[], Awaitable[Any]],
    presentation: LocalPresentation,
) -> None:
    """Enter the shared async application boundary from Click."""
    asyncio.run(_run_async(operations, operation_name, callback, presentation))


async def _run_audit_async(
    callback: Callable[[], Awaitable[Any]],
    presentation: AuditPresentation,
) -> None:
    """Emit one audit result at the shared async application boundary."""
    result = await callback()
    exit_class = emit_audit_result(result, presentation)
    if exit_class:
        raise click.exceptions.Exit(exit_class)


def _run_audit(
    callback: Callable[[], Awaitable[Any]],
    presentation: AuditPresentation,
) -> None:
    """Enter the local audit application boundary from Click."""
    asyncio.run(_run_audit_async(callback, presentation))


async def _run_read_only_async(
    callback: Callable[[], Awaitable[Any]],
    presentation: Presentation,
    shutdown: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Emit one read-only result at the shared async application boundary."""
    try:
        result = await callback()
    finally:
        if shutdown is not None:
            await shutdown()
    exit_class = emit_read_only_result(result, presentation)
    if exit_class:
        raise click.exceptions.Exit(exit_class)


def _run_read_only(
    callback: Callable[[], Awaitable[Any]],
    presentation: Presentation,
    shutdown: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Enter the provider-scoped read-only application boundary from Click."""
    asyncio.run(_run_read_only_async(callback, presentation, shutdown))


def _read_only_scope_input(
    resource_scope: str | None,
    profile: str | None,
) -> ReadOnlyScopeInput:
    """Decode only explicit scope inputs without consulting ambient state."""
    return ReadOnlyScopeInput(
        explicit_resource_scope=(
            parse_resource_scope_name(resource_scope)
            if resource_scope is not None
            else None
        ),
        explicit_profile=profile,
    )


def _provider_deadline() -> float:
    """Return one finite caller-controlled monotonic provider deadline."""
    return time.monotonic() + _PROVIDER_OPERATION_SECONDS


@click.group(
    cls=ClassifiedRootGroup,
    name="cqmgr",
    invoke_without_command=True,
    no_args_is_help=False,
)
@click.version_option(package_name="cqmgr")
@click.pass_context
def main(context: click.Context) -> None:
    """Inspect effective cloud quota and manage exact quota requests."""
    invocation_kind = _CURRENT_INVOCATION_KIND.get()
    if invocation_kind is None:  # pragma: no cover - Click always enters group.main
        msg = "invocation classification is unavailable"
        raise RuntimeError(msg)
    context.meta["cqmgr_invocation_kind"] = invocation_kind
    if context.invoked_subcommand is not None:
        return
    if invocation_kind is InvocationKind.TUI:
        from cqmgr.tui import run  # noqa: PLC0415

        run()
        return
    click.echo(context.get_help())
    raise click.exceptions.Exit(click.UsageError.exit_code)


@main.command()
def tui() -> None:
    """Open the interactive quota inspector."""
    stdin_is_tty, stdout_is_tty = _interactive_streams()
    if not stdin_is_tty or not stdout_is_tty:
        message = "tui requires interactive input and output"
        raise click.UsageError(message)
    from cqmgr.tui import run  # noqa: PLC0415

    run()


@main.group(cls=CanonicalAliasGroup)
def scope() -> None:
    """Inspect or change the local resource-scope selection."""


@scope.command(name="show")
@_presentation_options
@_OUTPUT_OPTION
def scope_show(output: str, no_color: bool, quiet: bool) -> None:
    """Show the resolved project and its resolution source."""
    operations = build_local_operations()
    _run(
        operations,
        "scope.show",
        operations.scope_show,
        LocalPresentation(output, no_color, quiet),
    )


@scope.command(name="select")
@_presentation_options
@click.option("--resource-scope", required=True)
@_OUTPUT_OPTION
def scope_select(
    resource_scope: str,
    output: str,
    no_color: bool,
    quiet: bool,
) -> None:
    """Select one full canonical project resource scope."""
    try:
        parsed = parse_resource_scope_name(resource_scope)
    except (TypeError, ValueError) as error:
        reason = str(error)
        operations = build_local_operations()
        _run(
            operations,
            "scope.select",
            lambda: operations.scope_selection_usage_failure(reason),
            LocalPresentation(output, no_color, quiet),
        )
        return
    operations = build_local_operations()
    _run(
        operations,
        "scope.select",
        lambda: operations.scope_select(parsed),
        LocalPresentation(output, no_color, quiet),
    )


@scope.command(name="clear")
@_presentation_options
@_OUTPUT_OPTION
def scope_clear(output: str, no_color: bool, quiet: bool) -> None:
    """Clear only the direct local resource-scope selection."""
    operations = build_local_operations()
    _run(
        operations,
        "scope.clear",
        operations.scope_clear,
        LocalPresentation(output, no_color, quiet),
    )


@main.group(cls=CanonicalAliasGroup)
def profile() -> None:
    """Inspect or select validated local profiles."""


@profile.command(name="list")
@_presentation_options
@_OUTPUT_OPTION
def profile_list(output: str, no_color: bool, quiet: bool) -> None:
    """List validated local profiles."""
    operations = build_local_operations()
    _run(
        operations,
        "profile.list",
        operations.profile_list,
        LocalPresentation(output, no_color, quiet),
    )


@profile.command(name="get")
@_presentation_options
@click.argument("name")
@_OUTPUT_OPTION
def profile_get(name: str, output: str, no_color: bool, quiet: bool) -> None:
    """Inspect one explicitly named local profile."""
    operations = build_local_operations()
    _run(
        operations,
        "profile.get",
        lambda: operations.profile_get(name),
        LocalPresentation(output, no_color, quiet),
    )


@profile.command(name="select")
@_presentation_options
@click.argument("name")
@_OUTPUT_OPTION
def profile_select(name: str, output: str, no_color: bool, quiet: bool) -> None:
    """Select one explicitly named local profile."""
    operations = build_local_operations()
    _run(
        operations,
        "profile.select",
        lambda: operations.profile_select(name),
        LocalPresentation(output, no_color, quiet),
    )


@main.group(cls=CanonicalAliasGroup)
def config() -> None:
    """Inspect or change validated local interface settings."""


@config.command(name="get")
@_presentation_options
@click.argument(
    "key",
    type=click.Choice(tuple(item.value for item in InterfaceSettingKey)),
)
@_OUTPUT_OPTION
def config_get(key: str, output: str, no_color: bool, quiet: bool) -> None:
    """Inspect one validated interface setting."""
    operations = build_local_operations()
    _run(
        operations,
        "config.get",
        lambda: operations.config_get(InterfaceSettingKey(key)),
        LocalPresentation(output, no_color, quiet),
    )


@config.command(name="set")
@_presentation_options
@click.argument(
    "key",
    type=click.Choice(tuple(item.value for item in InterfaceSettingKey)),
)
@click.argument("value", type=click.Choice(("true", "false")))
@_OUTPUT_OPTION
def config_set(
    key: str,
    value: str,
    output: str,
    no_color: bool,
    quiet: bool,
) -> None:
    """Atomically change one validated interface setting."""
    operations = build_local_operations()
    _run(
        operations,
        "config.set",
        lambda: operations.config_set(
            InterfaceSettingKey(key),
            value=value == "true",
        ),
        LocalPresentation(output, no_color, quiet),
    )


@main.group(cls=CanonicalAliasGroup)
def quota() -> None:
    """Inspect effective quota and resolve workload requirements."""


@quota.command(name="list")
@_presentation_options
@click.option("--cursor")
@click.option("--limit", type=click.IntRange(1, 1000), default=100, show_default=True)
@click.option("--sort", multiple=True)
@click.option("--effective-confirmation", multiple=True)
@click.option("--grant-satisfaction", multiple=True)
@click.option("--reconciliation", multiple=True)
@click.option("--mutable", type=click.Choice(("true", "false")))
@click.option("--guided", type=click.Choice(("true", "false")))
@click.option("--cataloged", type=click.Choice(("true", "false")))
@click.option("--quota-pool", multiple=True)
@click.option("--quota-scope", multiple=True)
@click.option("--location", multiple=True)
@click.option("--accelerator", multiple=True)
@click.option("--catalog-group", multiple=True)
@click.option("--service", multiple=True)
@click.option("--text")
@_scope_options
@_OUTPUT_OPTION
def quota_list(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    text: str | None,
    service: tuple[str, ...],
    catalog_group: tuple[str, ...],
    accelerator: tuple[str, ...],
    location: tuple[str, ...],
    quota_scope: tuple[str, ...],
    quota_pool: tuple[str, ...],
    cataloged: str | None,
    guided: str | None,
    mutable: str | None,
    reconciliation: tuple[str, ...],
    grant_satisfaction: tuple[str, ...],
    effective_confirmation: tuple[str, ...],
    sort: tuple[str, ...],
    limit: int,
    cursor: str | None,
    no_color: bool,
    quiet: bool,
) -> None:
    """List one bounded logical query over the fixed V1 provider inventory."""
    presentation = Presentation(output, no_color, quiet)
    has_explicit_query = any(
        (
            resource_scope,
            profile,
            text,
            service,
            catalog_group,
            accelerator,
            location,
            quota_scope,
            quota_pool,
            cataloged,
            guided,
            mutable,
            reconciliation,
            grant_satisfaction,
            effective_confirmation,
            sort,
        )
    )
    if cursor is not None and not has_explicit_query:
        cursor_operations = build_quota_cursor_operations()
        _run_read_only(
            lambda: cursor_operations.browse(
                QuotaBrowseRequest(cursor=cursor, limit=limit)
            ),
            presentation,
        )
        return
    operations = build_read_only_operations()
    try:
        scope_input = _read_only_scope_input(resource_scope, profile)
        query = parse_read_only_quota_query(
            services=service,
            catalog_groups=catalog_group,
            accelerators=accelerator,
            locations=location,
            quota_scopes=quota_scope,
            quota_pools=quota_pool,
            cataloged=cataloged,
            guided=guided,
            mutable=mutable,
            reconciliations=reconciliation,
            grant_satisfactions=grant_satisfaction,
            effective_confirmations=effective_confirmation,
            text=text,
            sorts=sort,
        )
    except (TypeError, ValueError) as error:
        reason = str(error)
        _run_read_only(
            lambda: operations.browse_usage_failure(reason),
            presentation,
            operations.aclose,
        )
        return
    _run_read_only(
        lambda: operations.browse(
            query,
            cursor=cursor,
            limit=limit,
            deadline=_provider_deadline(),
            scope_input=scope_input,
        ),
        presentation,
        operations.aclose,
    )


@quota.command(name="inspect")
@_presentation_options
@click.option("--dimension", multiple=True)
@click.option("--location", required=True)
@click.option("--quota-id", required=True)
@click.option("--service", required=True)
@_scope_options
@_OUTPUT_OPTION
def quota_inspect(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    service: str,
    quota_id: str,
    location: str,
    dimension: tuple[str, ...],
    no_color: bool,
    quiet: bool,
) -> None:
    """Inspect one exact effective quota slice."""
    operations = build_read_only_operations()
    presentation = Presentation(output, no_color, quiet)
    try:
        scope_input = _read_only_scope_input(resource_scope, profile)
        selector = QuotaInspectSelector(
            service,
            quota_id,
            location,
            parse_dimensions(dimension),
        )
    except (TypeError, ValueError) as error:
        reason = str(error)
        _run_read_only(
            lambda: operations.inspect_usage_failure(reason),
            presentation,
            operations.aclose,
        )
        return
    _run_read_only(
        lambda: operations.inspect(
            selector,
            deadline=_provider_deadline(),
            scope_input=scope_input,
        ),
        presentation,
        operations.aclose,
    )


@quota.group(cls=CanonicalAliasGroup)
def resolve() -> None:
    """Resolve a complete workload shape to per-location quota constraints."""


def _location_options[CommandT: Callable[..., Any]](
    function: CommandT,
) -> CommandT:
    """Add the two mutually exclusive workload location modes."""
    decorated = click.option("--all-compatible-locations", is_flag=True)(function)
    return click.option("--candidate", multiple=True)(decorated)


@resolve.command(name="compute-instance")
@_presentation_options
@_location_options
@click.option(
    "--provisioning-model",
    required=True,
    type=click.Choice(("standard", "spot", "flex-start", "reservation-bound")),
)
@click.option("--instance-count", required=True)
@click.option("--machine-type", required=True)
@_scope_options
@_OUTPUT_OPTION
def resolve_compute_instance(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    machine_type: str,
    instance_count: str,
    provisioning_model: str,
    candidate: tuple[str, ...],
    all_compatible_locations: bool,
    no_color: bool,
    quiet: bool,
) -> None:
    """Resolve one Compute instance shape without making a capacity claim."""
    operations = build_read_only_operations()
    presentation = Presentation(output, no_color, quiet)
    try:
        scope_input = _read_only_scope_input(resource_scope, profile)
        requirement = parse_compute_instance_requirement(
            machine_type=machine_type,
            instance_count=instance_count,
            provisioning_model=provisioning_model,
            locations=candidate,
            all_compatible=all_compatible_locations,
        )
    except (TypeError, ValueError) as error:
        reason = str(error)
        _run_read_only(
            lambda: operations.resolve_usage_failure(reason),
            presentation,
            operations.aclose,
        )
        return
    _run_read_only(
        lambda: operations.resolve(
            requirement,
            deadline=_provider_deadline(),
            scope_input=scope_input,
        ),
        presentation,
        operations.aclose,
    )


@resolve.command(name="cloud-tpu-slice")
@_presentation_options
@_location_options
@click.option(
    "--provisioning-model",
    required=True,
    type=click.Choice(("standard", "spot", "flex-start", "reservation-bound")),
)
@click.option("--slice-count", required=True)
@click.option("--runtime-version", required=True)
@click.option("--topology", required=True)
@click.option("--accelerator-type", required=True)
@_scope_options
@_OUTPUT_OPTION
def resolve_cloud_tpu_slice(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    accelerator_type: str,
    topology: str,
    runtime_version: str,
    slice_count: str,
    provisioning_model: str,
    candidate: tuple[str, ...],
    all_compatible_locations: bool,
    no_color: bool,
    quiet: bool,
) -> None:
    """Resolve one legacy Cloud TPU slice without making a capacity claim."""
    operations = build_read_only_operations()
    presentation = Presentation(output, no_color, quiet)
    try:
        scope_input = _read_only_scope_input(resource_scope, profile)
        requirement = parse_cloud_tpu_slice_requirement(
            accelerator_type=accelerator_type,
            topology=topology,
            runtime_version=runtime_version,
            slice_count=slice_count,
            provisioning_model=provisioning_model,
            locations=candidate,
            all_compatible=all_compatible_locations,
        )
    except (TypeError, ValueError) as error:
        reason = str(error)
        _run_read_only(
            lambda: operations.resolve_usage_failure(reason),
            presentation,
            operations.aclose,
        )
        return
    _run_read_only(
        lambda: operations.resolve(
            requirement,
            deadline=_provider_deadline(),
            scope_input=scope_input,
        ),
        presentation,
        operations.aclose,
    )


@main.group(cls=CanonicalAliasGroup)
def obtainability() -> None:
    """Compare exact Spot VM requests without making a capacity guarantee."""


@obtainability.command(name="compare")
@_presentation_options
@click.option("--all-compatible-locations", is_flag=True)
@click.option("--candidate", multiple=True)
@click.option(
    "--distribution-shape",
    required=True,
    type=click.Choice(("any", "any-single-zone", "balanced")),
)
@click.option("--vm-count", required=True)
@click.option("--gpu-count")
@click.option("--gpu-type")
@click.option("--machine-type", required=True)
@_scope_options
@_OUTPUT_OPTION
def obtainability_compare(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    machine_type: str,
    gpu_type: str | None,
    gpu_count: str | None,
    vm_count: str,
    distribution_shape: str,
    candidate: tuple[str, ...],
    all_compatible_locations: bool,
    no_color: bool,
    quiet: bool,
) -> None:
    """Compare current Preview advice and attributable Spot history."""
    operations = build_read_only_operations()
    presentation = Presentation(output, no_color, quiet)
    try:
        scope_input = _read_only_scope_input(resource_scope, profile)
        machine, count, shape = parse_obtainability_shape(
            machine_type=machine_type,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            vm_count=vm_count,
            distribution_shape=distribution_shape,
        )
        _require_obtainability_location_mode(
            candidate,
            all_compatible=all_compatible_locations,
        )
        candidates = (
            ()
            if all_compatible_locations
            else parse_obtainability_candidates(
                machine_type=machine_type,
                gpu_type=gpu_type,
                gpu_count=gpu_count,
                vm_count=vm_count,
                distribution_shape=distribution_shape,
                candidates=candidate,
            )
        )
    except (TypeError, ValueError) as error:
        reason = str(error)
        _run_read_only(
            lambda: operations.compare_obtainability_usage_failure(reason),
            presentation,
            operations.aclose,
        )
        return
    if all_compatible_locations:
        requirement = parse_compute_instance_requirement(
            machine_type=machine_type,
            instance_count=str(count),
            provisioning_model="spot",
            locations=(),
            all_compatible=True,
        )
        _run_read_only(
            lambda: operations.compare_obtainability_all_compatible(
                requirement,
                machine=machine,
                distribution_shape=shape,
                deadline=_provider_deadline(),
                scope_input=scope_input,
            ),
            presentation,
            operations.aclose,
        )
        return
    _run_read_only(
        lambda: operations.compare_obtainability(
            candidates,
            deadline=_provider_deadline(),
            scope_input=scope_input,
        ),
        presentation,
        operations.aclose,
    )


@main.group(cls=CanonicalAliasGroup)
def audit() -> None:
    """Read and verify installation-local retained audit evidence."""


@audit.command(name="list")
@_presentation_options
@click.option("--cursor")
@click.option("--limit", type=click.IntRange(1, 1000), default=100, show_default=True)
@click.option("--until")
@click.option("--since")
@click.option("--outcome", multiple=True)
@click.option("--operation", multiple=True)
@_OUTPUT_OPTION
def audit_list(  # noqa: PLR0913
    output: str,
    operation: tuple[str, ...],
    outcome: tuple[str, ...],
    since: str | None,
    until: str | None,
    limit: int,
    cursor: str | None,
    no_color: bool,
    quiet: bool,
) -> None:
    """List one bounded page of local audit records."""
    presentation = AuditPresentation(output, no_color, quiet)
    try:
        query = parse_audit_query(
            operations=operation,
            outcomes=outcome,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
        )
    except (TypeError, ValueError) as error:
        operations = build_audit_operations()
        reason = str(error)
        _run_audit(
            lambda: operations.list_usage_failure(reason),
            presentation,
        )
        return
    operations = build_audit_operations()
    _run_audit(lambda: operations.list(query), presentation)


@audit.command(name="inspect")
@_presentation_options
@click.argument("record_id")
@_OUTPUT_OPTION
def audit_inspect(
    output: str,
    record_id: str,
    no_color: bool,
    quiet: bool,
) -> None:
    """Inspect one exact retained local audit record."""
    operations = build_audit_operations()
    _run_audit(
        lambda: operations.inspect(record_id),
        AuditPresentation(output, no_color, quiet),
    )


@audit.command(name="verify")
@_presentation_options
@click.option("--through", "through_record_id")
@click.option("--from", "from_record_id")
@_OUTPUT_OPTION
def audit_verify(
    output: str,
    from_record_id: str | None,
    through_record_id: str | None,
    no_color: bool,
    quiet: bool,
) -> None:
    """Verify the complete retained chain or one explicit range."""
    operations = build_audit_operations()
    _run_audit(
        lambda: operations.verify(
            from_record_id=from_record_id,
            through_record_id=through_record_id,
        ),
        AuditPresentation(output, no_color, quiet),
    )
