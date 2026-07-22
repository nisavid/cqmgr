"""TTY-aware, offline-safe command-line entry point."""

# Click supplies declared boolean flags positionally to leaf callbacks.
# ruff: noqa: FBT001

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, override

import click

from cqmgr.adapters.cli.group import CanonicalAliasGroup
from cqmgr.adapters.cli.local import LocalPresentation, emit_local_result
from cqmgr.application.configuration import (
    InterfaceSettingKey,
    parse_resource_scope_name,
)
from cqmgr.application.ports.configuration import ConfigurationRepositoryError
from cqmgr.bootstrap import InvocationKind, build_local_operations, classify_invocation

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from cqmgr.application.operations.local import LocalOperations

_OUTPUT_OPTION = click.option(
    "--output",
    type=click.Choice(("human", "json"), case_sensitive=True),
    default="human",
    show_default=True,
)


def _presentation_options[CommandT: Callable[..., Any]](
    function: CommandT,
) -> CommandT:
    """Add shared options to one leaf command in canonical order."""
    decorated = click.option("--quiet", is_flag=True)(function)
    return click.option("--no-color", is_flag=True)(decorated)


_CURRENT_INVOCATION_KIND: ContextVar[InvocationKind | None] = ContextVar(
    "cqmgr_invocation_kind",
    default=None,
)


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
        invocation_kind = classify_invocation(
            raw_arguments,
            stdin_is_tty=sys.stdin.isatty(),
            stdout_is_tty=sys.stdout.isatty(),
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
    if not sys.stdin.isatty() or not sys.stdout.isatty():
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
