"""Repo-installed hooks find the launcher from any session directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ImpeccableHookTests(unittest.TestCase):
    """Run the configured shell commands with harmless fixture launchers."""

    def test_hooks_resolve_the_git_root_and_skip_a_missing_launcher(self) -> None:
        """Both lifecycle hooks work at root and nested CWDs with spaces."""
        hooks = json.loads(
            (Path(__file__).parents[1] / ".codex/hooks.json").read_text()
        )["hooks"]
        with tempfile.TemporaryDirectory(prefix="impeccable hooks ") as temporary:
            root = Path(temporary) / "repo with spaces %IMPECCABLE_TEST_PATH%"
            hook_environment = {**os.environ, "IMPECCABLE_TEST_PATH": "expanded"}
            root.mkdir()
            subprocess.run(  # noqa: S603 - fixed Git operation in a temporary fixture
                [shutil.which("git") or "git", "init", "-q", str(root)], check=True
            )
            nested = root / "src" / "nested"
            nested.mkdir(parents=True)
            launcher = root / ".agents/skills/impeccable/scripts"
            launcher.mkdir(parents=True)
            if os.name == "nt":
                executable = launcher / "impeccable.cmd"
                executable.write_text('@echo off\necho %1> "%~dp0called"\n')
                command_key = "commandWindows"
            else:
                executable = launcher / "impeccable"
                executable.write_text(
                    '#!/bin/sh\nprintf "%s" "$1" > "$(dirname "$0")/called"\n'
                )
                executable.chmod(0o755)
                command_key = "command"
            marker = launcher / "called"
            for event in ("PostToolUse", "Stop"):
                command = hooks[event][0]["hooks"][0][command_key]
                for cwd in (root, nested):
                    with self.subTest(event=event, cwd=cwd):
                        subprocess.run(  # noqa: S602 - execute the reviewed hook as Codex does
                            command,
                            shell=True,
                            cwd=cwd,
                            check=True,
                            env=hook_environment,
                        )
                        assert marker.read_text().strip() == "hook"
                        marker.unlink()
            executable.unlink()
            for event in ("PostToolUse", "Stop"):
                command = hooks[event][0]["hooks"][0][command_key]
                subprocess.run(  # noqa: S602 - execute the reviewed hook as Codex does
                    command, shell=True, cwd=nested, check=True, env=hook_environment
                )
                assert not marker.exists()


if __name__ == "__main__":
    unittest.main()
