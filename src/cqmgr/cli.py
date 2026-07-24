"""TTY-aware, offline-safe command-line entry point."""

# Click supplies declared boolean flags positionally to leaf callbacks.
# ruff: noqa: FBT001

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override

import click

from cqmgr.adapters.cli.audit import (
    AuditPresentation,
    emit_audit_result,
    parse_audit_query,
)
from cqmgr.adapters.cli.group import CanonicalAliasGroup
from cqmgr.adapters.cli.lifecycle import (
    DEFAULT_TARGET_STRATEGY,
    MANUAL_TARGET_STRATEGY,
    TARGET_STRATEGY_CHOICES,
    WATCH_CONDITION_CHOICES,
    LifecycleCliRuntime,
    LifecyclePresentation,
    PlanReferenceInput,
    RequestCompositionInput,
    WatchCliInput,
    WatchPresentation,
    emit_composition,
    emit_lifecycle_result,
    emit_watch_event,
    parse_absolute_rfc3339,
    parse_target_strategy,
    parse_watch_condition,
    read_quota_contact,
)
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
from cqmgr.application.operations.watch import WatchStartError
from cqmgr.application.ports.configuration import ConfigurationRepositoryError
from cqmgr.bootstrap import (
    InvocationKind,
    build_audit_operations,
    build_local_operations,
    build_quota_cursor_operations,
    build_read_only_operations,
    build_trust_initialization_operations,
    classify_invocation,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence
    from typing import BinaryIO

    from cqmgr.application.operations.lifecycle_requests import (
        PreparedLifecycleRequests,
    )
    from cqmgr.application.operations.local import LocalOperations
    from cqmgr.application.ports.secrets import SecretValue

_OUTPUT_OPTION = click.option(
    "--output",
    type=click.Choice(("human", "json"), case_sensitive=True),
    default="human",
    show_default=True,
)
_PROVIDER_OPERATION_SECONDS = 60.0
_ACKNOWLEDGEMENT_CODES = (
    "decrease-below-usage",
    "decrease-over-ten-percent",
    "unlimited-transition",
)


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


def build_lifecycle_cli_runtime() -> LifecycleCliRuntime:
    """Fail closed until the production bootstrap supplies protected inputs."""
    message = "lifecycle operations are unavailable in this installation"
    raise click.ClickException(message)


def _prepare_lifecycle_requests(
    runtime: LifecycleCliRuntime,
    value: RequestCompositionInput,
) -> PreparedLifecycleRequests | None:
    """Resolve fresh async evidence when the production preparation seam exists."""
    if runtime.preparation is None:
        return None
    try:
        return asyncio.run(
            runtime.preparation.prepare(
                value.to_intent(),
                deadline=_provider_deadline(),
            )
        )
    except (TypeError, ValueError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error


def _quota_contact_from_stdin(*, enabled: bool) -> SecretValue | None:
    """Read protected contact bytes only when the operator selected stdin."""
    if not enabled:
        return None
    binary = getattr(sys.stdin, "buffer", None)
    if binary is not None:
        return read_quota_contact(cast("BinaryIO", binary))
    return read_quota_contact(BytesIO(sys.stdin.read().encode("utf-8")))


def _plan_reference(digest: str | None, path: Path | None) -> PlanReferenceInput:
    """Bind exactly one public Plan reference before runtime construction."""
    try:
        return PlanReferenceInput(digest, path)
    except (TypeError, ValueError) as error:
        raise click.UsageError(str(error)) from error


def _manual_targets(values: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Parse one absolute target for every explicitly named workload child."""
    targets: list[tuple[str, str]] = []
    for value in values:
        child_id, separator, target = value.partition("=")
        if not separator or not child_id or not target:
            message = "manual workload targets must use CHILD_ID=VALUE"
            raise click.UsageError(message)
        targets.append((child_id, target))
    if len({child_id for child_id, _ in targets}) != len(targets):
        message = "manual workload target child IDs must be unique"
        raise click.UsageError(message)
    return tuple(targets)


def _request_composition_input(  # noqa: C901, PLR0912, PLR0913
    *,
    resource_scope: str | None,
    profile: str | None,
    service: str | None,
    quota_id: str | None,
    location: str | None,
    dimensions: tuple[str, ...],
    targets: tuple[str, ...],
    target_strategy: str | None,
    acknowledgements: tuple[str, ...],
    expert: bool,
    quota_contact_stdin: bool,
    plan_out: Path | None,
    machine_type: str | None,
    instance_count: str | None,
    attached_accelerator_type: str | None,
    attached_accelerator_count: str | None,
    accelerator_type: str | None,
    topology: str | None,
    runtime_version: str | None,
    slice_count: str | None,
    provisioning_model: str | None,
    candidates: tuple[str, ...],
    all_compatible_locations: bool,
) -> RequestCompositionInput:
    """Decode the mutually exclusive exact, Compute, and Cloud TPU grammars."""
    try:
        scope_input = _read_only_scope_input(resource_scope, profile)
        exact_values = (service, quota_id, location, dimensions)
        compute_values = (
            machine_type,
            instance_count,
            attached_accelerator_type,
            attached_accelerator_count,
        )
        tpu_values = (accelerator_type, topology, runtime_version, slice_count)
        exact_selected = any(exact_values)
        compute_selected = any(compute_values)
        tpu_selected = any(tpu_values)
        if sum((exact_selected, compute_selected, tpu_selected)) != 1:
            message = "select exactly one exact-slice, Compute, or Cloud TPU request"
            raise ValueError(message)  # noqa: TRY301
        selector = None
        workload = None
        if exact_selected:
            if (
                service is None
                or quota_id is None
                or location is None
                or len(targets) != 1
                or "=" in targets[0]
                or target_strategy not in {None, MANUAL_TARGET_STRATEGY}
                or any(compute_values)
                or any(tpu_values)
                or candidates
                or all_compatible_locations
                or provisioning_model is not None
            ):
                message = "exact request requires complete selectors and one target"
                raise ValueError(message)  # noqa: TRY301
            selector = QuotaInspectSelector(
                service,
                quota_id,
                location,
                parse_dimensions(dimensions),
            )
            strategy = parse_target_strategy(MANUAL_TARGET_STRATEGY)
            parsed_targets: tuple[tuple[str | None, str], ...] = ((None, targets[0]),)
        else:
            if service is not None or quota_id is not None or location is not None:
                message = "workload request cannot include exact-slice selectors"
                raise ValueError(message)  # noqa: TRY301
            if provisioning_model is None:
                message = "workload request requires provisioning model"
                raise ValueError(message)  # noqa: TRY301
            if compute_selected:
                if machine_type is None or instance_count is None:
                    message = "Compute request requires machine type and instance count"
                    raise ValueError(message)  # noqa: TRY301
                workload = parse_compute_instance_requirement(
                    machine_type=machine_type,
                    instance_count=instance_count,
                    provisioning_model=provisioning_model,
                    locations=candidates,
                    all_compatible=all_compatible_locations,
                    attached_accelerator_type=attached_accelerator_type,
                    attached_accelerator_count=attached_accelerator_count,
                )
            else:
                if (
                    accelerator_type is None
                    or topology is None
                    or runtime_version is None
                    or slice_count is None
                ):
                    message = (
                        "Cloud TPU request requires accelerator type, topology, "
                        "runtime version, and slice count"
                    )
                    raise ValueError(message)  # noqa: TRY301
                workload = parse_cloud_tpu_slice_requirement(
                    accelerator_type=accelerator_type,
                    topology=topology,
                    runtime_version=runtime_version,
                    slice_count=slice_count,
                    provisioning_model=provisioning_model,
                    locations=candidates,
                    all_compatible=all_compatible_locations,
                )
            selected_strategy = target_strategy or DEFAULT_TARGET_STRATEGY
            strategy = parse_target_strategy(selected_strategy)
            manual_strategy = selected_strategy == MANUAL_TARGET_STRATEGY
            parsed_targets = _manual_targets(targets) if manual_strategy else ()
            if manual_strategy and not parsed_targets:
                message = "manual workload strategy requires child targets"
                raise ValueError(message)  # noqa: TRY301
            if not manual_strategy and targets:
                message = "derived workload strategies do not accept targets"
                raise ValueError(message)  # noqa: TRY301
        return RequestCompositionInput(
            scope_input=scope_input,
            selector=selector,
            workload=workload,
            target_strategy=strategy,
            targets=parsed_targets,
            acknowledgements=acknowledgements,
            expert=expert,
            quota_contact=_quota_contact_from_stdin(enabled=quota_contact_stdin),
            plan_out=plan_out,
        )
    except (TypeError, ValueError) as error:
        raise click.UsageError(str(error)) from error


def _emit_lifecycle(
    result: object,
    presentation: LifecyclePresentation,
) -> None:
    """Emit one facade result and convert its stable exit class to Click."""
    exit_class = emit_lifecycle_result(result, presentation)  # type: ignore[arg-type]
    if exit_class:
        raise click.exceptions.Exit(exit_class)


async def _apply_lifecycle_async(
    runtime: LifecycleCliRuntime,
    request: object,
    presentation: LifecyclePresentation,
) -> None:
    result = await runtime.operations.apply(request)  # type: ignore[arg-type]
    _emit_lifecycle(result, presentation)


async def _watch_lifecycle_async(
    runtime: LifecycleCliRuntime,
    request: object,
    presentation: WatchPresentation,
) -> None:
    """Stream every material event and preserve the terminal result exit class."""
    exit_class = 0
    try:
        async for event in runtime.operations.watch(request):  # type: ignore[arg-type]
            emit_watch_event(event, presentation)
            result = getattr(event, "result", None)
            if result is not None:
                exit_class = int(result.outcome.exit_class)
    except WatchStartError as error:
        click.echo(
            f"Watch: {error.code.value} (exit {int(error.exit_class)})",
            err=True,
        )
        raise click.exceptions.Exit(int(error.exit_class)) from None
    if exit_class:
        raise click.exceptions.Exit(exit_class)


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


@main.group(cls=CanonicalAliasGroup)
def trust() -> None:
    """Manage explicit installation-local signing trust."""


@trust.command(name="init")
@click.option(
    "--output",
    type=click.Choice(("human", "json"), case_sensitive=True),
    default="human",
    show_default=True,
)
def trust_init(output: str) -> None:
    """Initialize installation signing trust once in the native keyring."""
    operations = build_trust_initialization_operations()
    result = operations.initialize()
    if output == "json":
        click.echo(
            json.dumps(
                {
                    "initialized": result.initialized,
                    "reason": result.reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif result.initialized:
        click.echo("Installation trust initialized.")
    if not result.initialized:
        message = result.reason or "installation trust initialization failed"
        raise click.ClickException(message)


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
@click.option("--attached-accelerator-count")
@click.option("--attached-accelerator-type")
@_scope_options
@_OUTPUT_OPTION
def resolve_compute_instance(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    machine_type: str,
    instance_count: str,
    attached_accelerator_type: str | None,
    attached_accelerator_count: str | None,
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
            attached_accelerator_type=attached_accelerator_type,
            attached_accelerator_count=attached_accelerator_count,
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
            attached_accelerator_type=(
                None if machine.gpu is None else machine.gpu.accelerator_type
            ),
            attached_accelerator_count=(
                None if machine.gpu is None else str(machine.gpu.count)
            ),
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


def _request_input_options[CommandT: Callable[..., Any]](
    function: CommandT,
) -> CommandT:
    """Add the mutually exclusive exact and workload request vocabularies."""
    decorated = click.option("--all-compatible-locations", is_flag=True)(function)
    decorated = click.option("--candidate", multiple=True)(decorated)
    decorated = click.option(
        "--provisioning-model",
        type=click.Choice(("standard", "spot", "flex-start", "reservation-bound")),
    )(decorated)
    decorated = click.option("--slice-count")(decorated)
    decorated = click.option("--runtime-version")(decorated)
    decorated = click.option("--topology")(decorated)
    decorated = click.option("--accelerator-type")(decorated)
    decorated = click.option("--attached-accelerator-count")(decorated)
    decorated = click.option("--attached-accelerator-type")(decorated)
    decorated = click.option("--instance-count")(decorated)
    decorated = click.option("--machine-type")(decorated)
    decorated = click.option(
        "--quota-contact-stdin",
        is_flag=True,
    )(decorated)
    decorated = click.option(
        "--expert",
        is_flag=True,
        help="Enable expert-only request paths without bypassing safety gates.",
    )(decorated)
    decorated = click.option(
        "--acknowledge",
        multiple=True,
        type=click.Choice(_ACKNOWLEDGEMENT_CODES),
    )(decorated)
    decorated = click.option(
        "--target-strategy",
        type=click.Choice(TARGET_STRATEGY_CHOICES),
        default=None,
        show_default=DEFAULT_TARGET_STRATEGY,
    )(decorated)
    decorated = click.option("--target", "targets", multiple=True)(decorated)
    decorated = click.option("--dimension", "dimensions", multiple=True)(decorated)
    decorated = click.option("--location")(decorated)
    decorated = click.option("--quota-id")(decorated)
    decorated = click.option("--service")(decorated)
    decorated = _scope_options(decorated)
    return cast("CommandT", decorated)


def _composition_from_options(  # noqa: PLR0913
    *,
    resource_scope: str | None,
    profile: str | None,
    service: str | None,
    quota_id: str | None,
    location: str | None,
    dimensions: tuple[str, ...],
    targets: tuple[str, ...],
    target_strategy: str | None,
    acknowledge: tuple[str, ...],
    expert: bool,
    quota_contact_stdin: bool,
    machine_type: str | None,
    instance_count: str | None,
    attached_accelerator_type: str | None,
    attached_accelerator_count: str | None,
    accelerator_type: str | None,
    topology: str | None,
    runtime_version: str | None,
    slice_count: str | None,
    provisioning_model: str | None,
    candidate: tuple[str, ...],
    all_compatible_locations: bool,
    plan_out: Path | None,
) -> RequestCompositionInput:
    return _request_composition_input(
        resource_scope=resource_scope,
        profile=profile,
        service=service,
        quota_id=quota_id,
        location=location,
        dimensions=dimensions,
        targets=targets,
        target_strategy=target_strategy,
        acknowledgements=acknowledge,
        expert=expert,
        quota_contact_stdin=quota_contact_stdin,
        plan_out=plan_out,
        machine_type=machine_type,
        instance_count=instance_count,
        attached_accelerator_type=attached_accelerator_type,
        attached_accelerator_count=attached_accelerator_count,
        accelerator_type=accelerator_type,
        topology=topology,
        runtime_version=runtime_version,
        slice_count=slice_count,
        provisioning_model=provisioning_model,
        candidates=candidate,
        all_compatible_locations=all_compatible_locations,
    )


@main.group(cls=CanonicalAliasGroup)
def request() -> None:
    """Compose, Preview, or Watch exact quota requests."""


@request.command(name="compose")
@_presentation_options
@_request_input_options
@_OUTPUT_OPTION
def request_compose(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    service: str | None,
    quota_id: str | None,
    location: str | None,
    dimensions: tuple[str, ...],
    targets: tuple[str, ...],
    target_strategy: str | None,
    acknowledge: tuple[str, ...],
    expert: bool,
    quota_contact_stdin: bool,
    machine_type: str | None,
    instance_count: str | None,
    attached_accelerator_type: str | None,
    attached_accelerator_count: str | None,
    accelerator_type: str | None,
    topology: str | None,
    runtime_version: str | None,
    slice_count: str | None,
    provisioning_model: str | None,
    candidate: tuple[str, ...],
    all_compatible_locations: bool,
    no_color: bool,
    quiet: bool,
) -> None:
    """Compose exact absolute targets without issuing a Plan."""
    value = _composition_from_options(
        resource_scope=resource_scope,
        profile=profile,
        service=service,
        quota_id=quota_id,
        location=location,
        dimensions=dimensions,
        targets=targets,
        target_strategy=target_strategy,
        acknowledge=acknowledge,
        expert=expert,
        quota_contact_stdin=quota_contact_stdin,
        machine_type=machine_type,
        instance_count=instance_count,
        attached_accelerator_type=attached_accelerator_type,
        attached_accelerator_count=attached_accelerator_count,
        accelerator_type=accelerator_type,
        topology=topology,
        runtime_version=runtime_version,
        slice_count=slice_count,
        provisioning_model=provisioning_model,
        candidate=candidate,
        all_compatible_locations=all_compatible_locations,
        plan_out=None,
    )
    runtime = build_lifecycle_cli_runtime()
    prepared = _prepare_lifecycle_requests(runtime, value)
    request_value = (
        runtime.requests.compose(value) if prepared is None else prepared.composition
    )
    composition = runtime.operations.compose(request_value)
    exit_class = emit_composition(
        composition,
        LifecyclePresentation(output, no_color, quiet),
    )
    if exit_class:
        raise click.exceptions.Exit(exit_class)


@request.command(name="preview")
@_presentation_options
@click.option("--plan-out", type=click.Path(path_type=Path))
@_request_input_options
@_OUTPUT_OPTION
def request_preview(  # noqa: PLR0913
    output: str,
    resource_scope: str | None,
    profile: str | None,
    service: str | None,
    quota_id: str | None,
    location: str | None,
    dimensions: tuple[str, ...],
    targets: tuple[str, ...],
    target_strategy: str | None,
    acknowledge: tuple[str, ...],
    expert: bool,
    quota_contact_stdin: bool,
    machine_type: str | None,
    instance_count: str | None,
    attached_accelerator_type: str | None,
    attached_accelerator_count: str | None,
    accelerator_type: str | None,
    topology: str | None,
    runtime_version: str | None,
    slice_count: str | None,
    provisioning_model: str | None,
    candidate: tuple[str, ...],
    all_compatible_locations: bool,
    plan_out: Path | None,
    no_color: bool,
    quiet: bool,
) -> None:
    """Preview one request and optionally export its portable Plan."""
    value = _composition_from_options(
        resource_scope=resource_scope,
        profile=profile,
        service=service,
        quota_id=quota_id,
        location=location,
        dimensions=dimensions,
        targets=targets,
        target_strategy=target_strategy,
        acknowledge=acknowledge,
        expert=expert,
        quota_contact_stdin=quota_contact_stdin,
        machine_type=machine_type,
        instance_count=instance_count,
        attached_accelerator_type=attached_accelerator_type,
        attached_accelerator_count=attached_accelerator_count,
        accelerator_type=accelerator_type,
        topology=topology,
        runtime_version=runtime_version,
        slice_count=slice_count,
        provisioning_model=provisioning_model,
        candidate=candidate,
        all_compatible_locations=all_compatible_locations,
        plan_out=plan_out,
    )
    runtime = build_lifecycle_cli_runtime()
    prepared = _prepare_lifecycle_requests(runtime, value)
    if prepared is None:
        request_value = runtime.requests.preview(value)
    else:
        request_value = prepared.preview
        if request_value is None:
            message = "Preview requires a resolvable protected quota contact"
            raise click.ClickException(message)
    _emit_lifecycle(
        runtime.operations.preview(request_value),
        LifecyclePresentation(output, no_color, quiet),
    )


@request.command(name="watch")
@_presentation_options
@click.option(
    "--output",
    type=click.Choice(("human", "jsonl"), case_sensitive=True),
    default=None,
)
@click.option("--deadline", required=True)
@click.option("--condition", type=click.Choice(WATCH_CONDITION_CHOICES))
@click.option("--resume")
@click.option("--intent-id")
def request_watch(  # noqa: PLR0913
    intent_id: str | None,
    resume: str | None,
    condition: str | None,
    deadline: str,
    output: str | None,
    no_color: bool,
    quiet: bool,
) -> None:
    """Watch one durable Apply intent until an explicit condition or deadline."""
    selected_output = output or ("human" if _interactive_streams()[1] else "jsonl")
    try:
        value = WatchCliInput(
            intent_id=intent_id,
            condition=None if condition is None else parse_watch_condition(condition),
            resume=resume,
            deadline=parse_absolute_rfc3339(deadline),
        )
        runtime = build_lifecycle_cli_runtime()
        request_value = runtime.requests.watch(value)
    except (TypeError, ValueError) as error:
        raise click.UsageError(str(error)) from error
    asyncio.run(
        _watch_lifecycle_async(
            runtime,
            request_value,
            WatchPresentation(selected_output, no_color, quiet),
        )
    )


@main.group(cls=CanonicalAliasGroup)
def plan() -> None:
    """Review or Apply one local or portable quota request Plan."""


@plan.command(name="review")
@_presentation_options
@click.option("--plan-file", type=click.Path(path_type=Path))
@click.option("--plan", "digest")
@_OUTPUT_OPTION
def plan_review(
    output: str,
    digest: str | None,
    plan_file: Path | None,
    no_color: bool,
    quiet: bool,
) -> None:
    """Review one Plan without provider mutation."""
    value = _plan_reference(digest, plan_file)
    runtime = build_lifecycle_cli_runtime()
    _emit_lifecycle(
        runtime.operations.review(runtime.requests.review(value)),
        LifecyclePresentation(output, no_color, quiet),
    )


@plan.command(name="apply")
@_presentation_options
@click.option("--acknowledge-resource-scope", required=True)
@click.option("--plan-file", type=click.Path(path_type=Path))
@click.option("--plan", "digest")
@_OUTPUT_OPTION
def plan_apply(  # noqa: PLR0913
    output: str,
    digest: str | None,
    plan_file: Path | None,
    acknowledge_resource_scope: str,
    no_color: bool,
    quiet: bool,
) -> None:
    """Apply one reviewed Plan in its bound non-atomic child order."""
    value = _plan_reference(digest, plan_file)
    runtime = build_lifecycle_cli_runtime()
    request_value = runtime.requests.apply(value, acknowledge_resource_scope)
    asyncio.run(
        _apply_lifecycle_async(
            runtime,
            request_value,
            LifecyclePresentation(output, no_color, quiet),
        )
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
