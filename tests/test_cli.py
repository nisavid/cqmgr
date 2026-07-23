"""Public command-line bootstrap contracts."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cqmgr.adapters.cli.group import CanonicalAliasGroup
from cqmgr.cli import main


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_root_metadata_commands_are_offline(argument: str) -> None:
    """Help and version do not initialize optional runtime integrations."""
    script = """
import json
import sys

forbidden = ("google", "keyring", "textual")

class BlockForbiddenImports:
    def find_spec(self, fullname, path, target=None):
        if fullname in forbidden or fullname.startswith(
            tuple(f"{item}." for item in forbidden)
        ):
            raise AssertionError(f"forbidden metadata-command import: {fullname}")
        return None

sys.meta_path.insert(0, BlockForbiddenImports())

from cqmgr.cli import main

try:
    main([sys.argv[1]], prog_name="cqmgr", standalone_mode=False)
except SystemExit as error:
    if error.code not in (0, None):
        raise

print(json.dumps(sorted(
    name for name in sys.modules
    if name in forbidden or name.startswith(tuple(f"{item}." for item in forbidden))
)))
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, argument],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    output_lines = completed.stdout.splitlines()
    assert json.loads(output_lines[-1]) == []
    if argument == "--help":
        assert output_lines[0].startswith("Usage: ")
        assert "--version" in completed.stdout
    else:
        assert completed.stdout.startswith(f"cqmgr, version {version('cqmgr')}")


def test_python_module_exposes_root_help() -> None:
    """The module entry point routes to the same Click command."""
    completed = subprocess.run(
        [sys.executable, "-m", "cqmgr", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("Usage: ")
    assert "--version" in completed.stdout


def test_python_module_dispatch_is_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module guard dispatches only when executed as the main module."""
    module_path = Path(__file__).parents[1] / "src" / "cqmgr" / "__main__.py"
    runpy.run_path(str(module_path), run_name="cqmgr.not_main")
    monkeypatch.setattr(sys, "argv", ["cqmgr", "--version"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(module_path), run_name="__main__")

    assert exit_info.value.code == 0


def test_canonical_alias_group_resolves_only_reserved_alias() -> None:
    """Canonical names retain only their explicitly registered aliases."""

    @click.group(cls=CanonicalAliasGroup, aliases={"quota": "q"})
    def command_group() -> None:
        """Test command group."""

    @command_group.command(name="quota")
    def quota_command() -> None:
        """Test quota command."""
        click.echo("quota")

    runner = CliRunner()

    assert runner.invoke(command_group, ["quota"]).output == "quota\n"
    assert runner.invoke(command_group, ["q"]).output == "quota\n"
    assert runner.invoke(command_group, ["qu"]).exit_code == click.UsageError.exit_code
    assert runner.invoke(command_group, ["quo"]).exit_code == click.UsageError.exit_code


def test_alias_invocation_renders_only_the_canonical_help_path() -> None:
    """Help and usage diagnostics never publish invocation-only aliases."""
    runner = CliRunner()

    help_result = runner.invoke(main, ["q", "l", "--help"])
    invalid_result = runner.invoke(main, ["q", "lis", "--help"])

    assert help_result.exit_code == 0
    assert help_result.stdout.startswith("Usage: cqmgr quota list [OPTIONS]\n")
    assert "Usage: cqmgr q l " not in help_result.output
    assert invalid_result.exit_code == click.UsageError.exit_code
    assert invalid_result.stderr.startswith(
        "Usage: cqmgr quota [OPTIONS] COMMAND [ARGS]...\n"
    )
    assert "Usage: cqmgr q " not in invalid_result.output


def test_canonical_alias_group_rejects_invalid_registrations() -> None:
    """Every command requires one exact non-colliding sibling alias."""
    command_group = CanonicalAliasGroup(aliases={})

    with pytest.raises(TypeError, match="canonical command name"):
        command_group.add_command(click.Command(name=None))

    with pytest.raises(TypeError, match="missing explicit alias"):
        command_group.add_command(click.Command(name="quota"))

    with pytest.raises(TypeError, match="duplicate alias"):
        CanonicalAliasGroup(aliases={"quota": "q", "query": "q"})

    with pytest.raises(TypeError, match="collides with canonical command"):
        CanonicalAliasGroup(aliases={"quota": "q", "query": "quota"})


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_root_metadata_commands_succeed(argument: str) -> None:
    """Click accepts both root metadata options."""
    result = CliRunner().invoke(main, [argument])

    assert result.exit_code == 0
    if argument == "--help":
        assert result.output.startswith("Usage: ")
    else:
        assert result.output == f"cqmgr, version {version('cqmgr')}\n"
