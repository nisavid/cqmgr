"""Click group with stable canonical command aliases."""

from typing import override

import click

ALIAS_LENGTH = 3


class CanonicalAliasGroup(click.Group):
    """Resolve only each canonical command's reserved three-letter alias."""

    @override
    def add_command(self, cmd: click.Command, name: str | None = None) -> None:
        """Reject canonical names whose reserved aliases collide."""
        canonical_name = name or cmd.name
        if canonical_name is None:
            msg = "A canonical command name is required."
            raise TypeError(msg)
        alias = canonical_name[:ALIAS_LENGTH]
        collisions = [
            existing
            for existing in self.commands
            if existing != canonical_name and existing[:ALIAS_LENGTH] == alias
        ]
        if collisions:
            msg = f"Reserved command alias {alias!r} is already in use."
            raise TypeError(msg)
        super().add_command(cmd, canonical_name)

    @override
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Resolve a canonical name or its exact reserved alias."""
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        matches = [
            candidate
            for canonical_name, candidate in self.commands.items()
            if cmd_name == canonical_name[:ALIAS_LENGTH]
        ]
        return matches[0] if len(matches) == 1 else None
