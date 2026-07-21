"""Offline-safe command-line entry point."""

import click

from cqmgr.adapters.cli.group import CanonicalAliasGroup


@click.group(cls=CanonicalAliasGroup, name="cqmgr", no_args_is_help=True)
@click.version_option(package_name="cqmgr")
def main() -> None:
    """Inspect effective cloud quota and manage exact quota requests."""
