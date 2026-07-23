"""Click group with stable canonical command aliases."""

from collections.abc import Mapping
from typing import Any, override

import click

_DEFAULT_ALIAS_REGISTRIES: Mapping[str, Mapping[str, str | None]] = {
    "cqmgr": {
        "tui": None,
        "scope": "sc",
        "profile": "pf",
        "config": "cfg",
        "quota": "q",
        "obtainability": "ob",
        "request": "req",
        "plan": "pl",
        "audit": "aud",
    },
    "scope": {"show": "sh", "select": "se", "clear": "cl"},
    "profile": {"list": "l", "get": "g", "select": "s"},
    "config": {"get": "g", "set": "s"},
    "quota": {"list": "l", "inspect": "i", "resolve": "r"},
    "resolve": {"compute-instance": "ci", "cloud-tpu-slice": "ct"},
    "obtainability": {"compare": "c"},
    "request": {"compose": "c", "preview": "p", "watch": "w"},
    "plan": {"review": "r", "apply": "a"},
    "audit": {"list": "l", "inspect": "i", "verify": "v"},
}


class CanonicalAliasGroup(click.Group):
    """Resolve only a group's explicitly registered canonical aliases."""

    def __init__(
        self,
        *args: Any,  # noqa: ANN401 - forwards Click's extensible constructor.
        aliases: Mapping[str, str | None] | None = None,
        **kwargs: Any,  # noqa: ANN401 - forwards Click's extensible constructor.
    ) -> None:
        """Bind one sibling-level alias registry before commands are registered."""
        name = kwargs.get("name")
        if name is None and args:
            name = args[0]
        configured_aliases = (
            _DEFAULT_ALIAS_REGISTRIES.get(
                name if isinstance(name, str) else "",
                {},
            )
            if aliases is None
            else aliases
        )
        self._aliases = dict(configured_aliases)
        self._validate_aliases()
        super().__init__(*args, **kwargs)

    def _validate_aliases(self) -> None:
        if any(
            not isinstance(canonical, str)
            or not canonical
            or (alias is not None and (not isinstance(alias, str) or not alias))
            for canonical, alias in self._aliases.items()
        ):
            msg = "canonical command names and aliases must be non-empty strings."
            raise TypeError(msg)
        aliases = tuple(alias for alias in self._aliases.values() if alias is not None)
        if len(set(aliases)) != len(aliases):
            msg = "A sibling alias registry cannot contain a duplicate alias."
            raise TypeError(msg)
        collisions = set(self._aliases).intersection(aliases)
        if collisions:
            msg = "An alias collides with canonical command names: " + ", ".join(
                sorted(collisions)
            )
            raise TypeError(msg)

    @override
    def add_command(self, cmd: click.Command, name: str | None = None) -> None:
        """Reject a command that lacks an explicit sibling-level alias."""
        canonical_name = name or cmd.name
        if canonical_name is None:
            msg = "A canonical command name is required."
            raise TypeError(msg)
        if canonical_name not in self._aliases:
            msg = f"Command {canonical_name!r} is missing explicit alias registration."
            raise TypeError(msg)
        super().add_command(cmd, canonical_name)

    @override
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Resolve only a canonical command name or its exact registered alias."""
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        canonical_name = next(
            (
                canonical
                for canonical, alias in self._aliases.items()
                if alias is not None and cmd_name == alias
            ),
            None,
        )
        return self.commands.get(canonical_name) if canonical_name is not None else None

    @override
    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """Return the canonical name so Click renders canonical help and usage."""
        resolved_name, command, remaining = super().resolve_command(ctx, args)
        if command is None:
            return resolved_name, command, remaining
        return command.name, command, remaining
