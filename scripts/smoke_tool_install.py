"""Install each local artifact with uv and smoke-test its executable."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

EXPECTED_ARTIFACT_COUNT = 2
PROJECT_VERSION = tomllib.loads(Path("pyproject.toml").read_text())["project"][
    "version"
]
RUNTIME_GUARD_MARKER = "CQMGR-RUNTIME-GUARD-ACTIVE"
RUNTIME_GUARD = """
import sys

FORBIDDEN = ("google", "keyring", "textual")

class BlockForbiddenImports:
    def find_spec(self, fullname, path, target=None):
        if fullname in FORBIDDEN or fullname.startswith(
            tuple(f"{item}." for item in FORBIDDEN)
        ):
            raise AssertionError(f"forbidden metadata-command import: {fullname}")
        return None

def block_network(event, arguments):
    if event.startswith("socket."):
        raise AssertionError(f"forbidden metadata-command network access: {event}")

sys.meta_path.insert(0, BlockForbiddenImports())
sys.addaudithook(block_network)
print("CQMGR-RUNTIME-GUARD-ACTIVE", file=sys.stderr)
"""


def _run(
    command: list[str], *, cwd: Path, environment: dict[str, str]
) -> tuple[str, str]:
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout, completed.stderr


def smoke_artifact(artifact: Path, python: str) -> None:
    """Install one artifact and run offline-safe metadata commands."""
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        bin_directory = temporary / "bin"
        runtime_home = temporary / "runtime-home"
        runtime_home.mkdir()
        install_environment = os.environ.copy()
        install_environment.update(
            {
                "APPDATA": str(runtime_home / "appdata"),
                "HOME": str(runtime_home),
                "LOCALAPPDATA": str(runtime_home / "local-appdata"),
                "UV_CACHE_DIR": str(temporary / "uv-cache"),
                "UV_NO_PROGRESS": "1",
                "UV_TOOL_BIN_DIR": str(bin_directory),
                "UV_TOOL_DIR": str(temporary / "uv-tools"),
                "XDG_CACHE_HOME": str(runtime_home / "xdg-cache"),
                "XDG_CONFIG_HOME": str(runtime_home / "xdg-config"),
                "XDG_DATA_HOME": str(runtime_home / "xdg-data"),
                "XDG_STATE_HOME": str(runtime_home / "xdg-state"),
            }
        )
        _run(
            ["uv", "tool", "install", "--python", python, str(artifact.resolve())],
            cwd=temporary,
            environment=install_environment,
        )
        guard_directory = temporary / "runtime-guard"
        guard_directory.mkdir()
        (guard_directory / "sitecustomize.py").write_text(RUNTIME_GUARD)
        runtime_environment = install_environment.copy()
        for key in list(runtime_environment):
            if key.upper().startswith(("CLOUDSDK_", "GCP_", "GOOGLE_")):
                runtime_environment.pop(key)
        runtime_environment.update(
            {
                "ALL_PROXY": "http://127.0.0.1:9",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "",
                "PYTHONPATH": str(guard_directory),
            }
        )
        executable = bin_directory / ("cqmgr.exe" if os.name == "nt" else "cqmgr")
        help_output, help_errors = _run(
            [str(executable), "--help"], cwd=temporary, environment=runtime_environment
        )
        version_output, version_errors = _run(
            [str(executable), "--version"],
            cwd=temporary,
            environment=runtime_environment,
        )
        assert RUNTIME_GUARD_MARKER in help_errors
        assert RUNTIME_GUARD_MARKER in version_errors
        assert help_output.startswith("Usage: cqmgr")
        assert "--version" in help_output
        assert version_output == f"cqmgr, version {PROJECT_VERSION}\n"
        assert list(runtime_home.rglob("*")) == []


def main() -> None:
    """Parse arguments and smoke-test every artifact."""
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("--python", default="3.14")
    arguments = parser.parse_args()
    artifacts = sorted(
        path
        for path in arguments.dist_dir.iterdir()
        if path.name.endswith((".tar.gz", ".whl"))
    )
    assert len(artifacts) == EXPECTED_ARTIFACT_COUNT
    for artifact in artifacts:
        smoke_artifact(artifact, arguments.python)


if __name__ == "__main__":
    main()
