"""TTY-aware, offline-safe command-line entry point."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import click

from cqmgr.adapters.cli.group import CanonicalAliasGroup
from cqmgr.adapters.cli.local import emit_local_result
from cqmgr.application.configuration import (
    InterfaceSettingKey,
    parse_resource_scope_name,
)
from cqmgr.application.ports.configuration import ConfigurationRepositoryError
from cqmgr.bootstrap import InvocationKind, build_local_operations, classify_invocation

if TYPE_CHECKING:
    from collections.abc import Callable

    from cqmgr.application.operations.local import LocalOperations

_OUTPUT_OPTION = click.option(
    "--output",
    type=click.Choice(("human", "json"), case_sensitive=True),
    default="human",
    show_default=True,
)


def _run(
    operations: LocalOperations,
    operation_name: str,
    callback: Callable[[], object],
    output: str,
) -> None:
    try:
        result = callback()
    except ConfigurationRepositoryError as error:
        result = operations.repository_failure(operation_name, error)
    exit_class = emit_local_result(result, output)  # type: ignore[arg-type]
    if exit_class:
        raise click.exceptions.Exit(exit_class)


@click.group(
    cls=CanonicalAliasGroup,
    name="cqmgr",
    invoke_without_command=True,
    no_args_is_help=False,
)
@click.version_option(package_name="cqmgr")
@click.pass_context
def main(context: click.Context) -> None:
    """Inspect effective cloud quota and manage exact quota requests."""
    arguments = (
        (context.invoked_subcommand,)
        if context.invoked_subcommand is not None
        else ()
    )
    invocation_kind = classify_invocation(
        arguments,
        stdin_is_tty=sys.stdin.isatty(),
        stdout_is_tty=sys.stdout.isatty(),
    )
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
@_OUTPUT_OPTION
def scope_show(output: str) -> None:
    """Show the resolved project and its resolution source."""
    operations = build_local_operations()
    _run(operations, "scope.show", operations.scope_show, output)


@scope.command(name="select")
@click.argument("resource_scope")
@_OUTPUT_OPTION
def scope_select(resource_scope: str, output: str) -> None:
    """Select one full canonical project resource scope."""
    try:
        parsed = parse_resource_scope_name(resource_scope)
    except (TypeError, ValueError) as error:
        raise click.BadParameter(str(error), param_hint="RESOURCE_SCOPE") from error
    operations = build_local_operations()
    _run(
        operations,
        "scope.select",
        lambda: operations.scope_select(parsed),
        output,
    )


@scope.command(name="clear")
@_OUTPUT_OPTION
def scope_clear(output: str) -> None:
    """Clear only the direct local resource-scope selection."""
    operations = build_local_operations()
    _run(operations, "scope.clear", operations.scope_clear, output)


@main.group(cls=CanonicalAliasGroup)
def profile() -> None:
    """Inspect or select validated local profiles."""


@profile.command(name="list")
@_OUTPUT_OPTION
def profile_list(output: str) -> None:
    """List validated local profiles."""
    operations = build_local_operations()
    _run(operations, "profile.list", operations.profile_list, output)


@profile.command(name="get")
@click.argument("name")
@_OUTPUT_OPTION
def profile_get(name: str, output: str) -> None:
    """Inspect one explicitly named local profile."""
    operations = build_local_operations()
    _run(
        operations,
        "profile.get",
        lambda: operations.profile_get(name),
        output,
    )


@profile.command(name="select")
@click.argument("name")
@_OUTPUT_OPTION
def profile_select(name: str, output: str) -> None:
    """Select one explicitly named local profile."""
    operations = build_local_operations()
    _run(
        operations,
        "profile.select",
        lambda: operations.profile_select(name),
        output,
    )


@main.group(cls=CanonicalAliasGroup)
def config() -> None:
    """Inspect or change validated local interface settings."""


@config.command(name="get")
@click.argument(
    "key",
    type=click.Choice(tuple(item.value for item in InterfaceSettingKey)),
)
@_OUTPUT_OPTION
def config_get(key: str, output: str) -> None:
    """Inspect one validated interface setting."""
    operations = build_local_operations()
    _run(
        operations,
        "config.get",
        lambda: operations.config_get(InterfaceSettingKey(key)),
        output,
    )


@config.command(name="set")
@click.argument(
    "key",
    type=click.Choice(tuple(item.value for item in InterfaceSettingKey)),
)
@click.argument("value", type=click.Choice(("true", "false")))
@_OUTPUT_OPTION
def config_set(key: str, value: str, output: str) -> None:
    """Atomically change one validated interface setting."""
    operations = build_local_operations()
    _run(
        operations,
        "config.set",
        lambda: operations.config_set(
            InterfaceSettingKey(key),
            value=value == "true",
        ),
        output,
    )
