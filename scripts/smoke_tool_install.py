"""Install each local artifact with uv and smoke-test its executable."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

EXPECTED_ARTIFACT_COUNT = 2
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_VERSION = tomllib.loads(Path("pyproject.toml").read_text())["project"][
    "version"
]
RUNTIME_GUARD_MARKER = "CQMGR-RUNTIME-GUARD-ACTIVE"
READ_ONLY_HELP_INVOCATIONS = (
    ("tui", "--help"),
    ("scope", "--help"),
    ("sc", "--help"),
    ("scope", "show", "--help"),
    ("sc", "sh", "--help"),
    ("scope", "select", "--help"),
    ("sc", "se", "--help"),
    ("scope", "clear", "--help"),
    ("sc", "cl", "--help"),
    ("profile", "--help"),
    ("pf", "--help"),
    ("profile", "list", "--help"),
    ("pf", "l", "--help"),
    ("profile", "get", "--help"),
    ("pf", "g", "--help"),
    ("profile", "select", "--help"),
    ("pf", "s", "--help"),
    ("config", "--help"),
    ("cfg", "--help"),
    ("config", "get", "--help"),
    ("cfg", "g", "--help"),
    ("config", "set", "--help"),
    ("cfg", "s", "--help"),
    ("quota", "--help"),
    ("q", "--help"),
    ("quota", "list", "--help"),
    ("q", "l", "--help"),
    ("quota", "inspect", "--help"),
    ("q", "i", "--help"),
    ("quota", "resolve", "--help"),
    ("q", "r", "--help"),
    ("quota", "resolve", "compute-instance", "--help"),
    ("q", "r", "ci", "--help"),
    ("quota", "resolve", "cloud-tpu-slice", "--help"),
    ("q", "r", "ct", "--help"),
    ("audit", "--help"),
    ("aud", "--help"),
    ("audit", "list", "--help"),
    ("aud", "l", "--help"),
    ("audit", "inspect", "--help"),
    ("aud", "i", "--help"),
    ("audit", "verify", "--help"),
    ("aud", "v", "--help"),
)
REJECTED_ALIAS_INVOCATIONS = (
    ("sco", "--help"),
    ("pro", "--help"),
    ("con", "--help"),
    ("quo", "--help"),
    ("scope", "sho", "--help"),
    ("profile", "lis", "--help"),
    ("quota", "lis", "--help"),
    ("q", "r", "com", "--help"),
    ("audit", "lis", "--help"),
)
CANONICAL_ALIASES_BY_PARENT: Mapping[
    tuple[str, ...],
    Mapping[str, str],
] = {
    (): {
        "sc": "scope",
        "pf": "profile",
        "cfg": "config",
        "q": "quota",
        "aud": "audit",
    },
    ("scope",): {"sh": "show", "se": "select", "cl": "clear"},
    ("profile",): {"l": "list", "g": "get", "s": "select"},
    ("config",): {"g": "get", "s": "set"},
    ("quota",): {"l": "list", "i": "inspect", "r": "resolve"},
    ("quota", "resolve"): {"ci": "compute-instance", "ct": "cloud-tpu-slice"},
    ("audit",): {"l": "list", "i": "inspect", "v": "verify"},
}
OFFLINE_FUNCTIONAL_CASES = (
    ((("scope", "show"), ("sc", "sh")), 3, "scope.show"),
    (
        (
            ("scope", "select", "--resource-scope", "projects/123"),
            ("sc", "se", "--resource-scope", "projects/123"),
        ),
        0,
        "scope.select",
    ),
    ((("scope", "clear"), ("sc", "cl")), 0, "scope.clear"),
    ((("profile", "list"), ("pf", "l")), 0, "profile.list"),
    (
        ((("profile", "get", "missing"), ("pf", "g", "missing"))),
        3,
        "profile.get",
    ),
    (
        ((("profile", "select", "missing"), ("pf", "s", "missing"))),
        3,
        "profile.select",
    ),
    (
        (
            ("config", "get", "interface.no-color"),
            ("cfg", "g", "interface.no-color"),
        ),
        0,
        "config.get",
    ),
    (
        (
            ("config", "set", "interface.no-color", "true"),
            ("cfg", "s", "interface.no-color", "false"),
        ),
        0,
        "config.set",
    ),
    (
        (
            ("quota", "list", "--resource-scope", "folders/456"),
            ("q", "l", "--resource-scope", "folders/456"),
        ),
        3,
        "quota.list",
    ),
    (
        (
            (
                "quota",
                "inspect",
                "--resource-scope",
                "folders/456",
                "--service",
                "compute",
                "--quota-id",
                "GPUS-ALL-REGIONS-per-project",
                "--location",
                "global",
            ),
            (
                "q",
                "i",
                "--resource-scope",
                "folders/456",
                "--service",
                "compute",
                "--quota-id",
                "GPUS-ALL-REGIONS-per-project",
                "--location",
                "global",
            ),
        ),
        3,
        "quota.inspect",
    ),
    (
        (
            (
                "quota",
                "resolve",
                "compute-instance",
                "--resource-scope",
                "folders/456",
                "--machine-type",
                "a3-highgpu-8g",
                "--instance-count",
                "1",
                "--provisioning-model",
                "standard",
                "--candidate",
                "us-central1-a",
            ),
            (
                "q",
                "r",
                "ci",
                "--resource-scope",
                "folders/456",
                "--machine-type",
                "a3-highgpu-8g",
                "--instance-count",
                "1",
                "--provisioning-model",
                "standard",
                "--candidate",
                "us-central1-a",
            ),
        ),
        3,
        "quota.resolve",
    ),
    (
        (
            (
                "quota",
                "resolve",
                "cloud-tpu-slice",
                "--resource-scope",
                "folders/456",
                "--accelerator-type",
                "v6e-8",
                "--topology",
                "2x4",
                "--runtime-version",
                "tpu-vm-base",
                "--slice-count",
                "1",
                "--provisioning-model",
                "standard",
                "--candidate",
                "us-central1-b",
            ),
            (
                "q",
                "r",
                "ct",
                "--resource-scope",
                "folders/456",
                "--accelerator-type",
                "v6e-8",
                "--topology",
                "2x4",
                "--runtime-version",
                "tpu-vm-base",
                "--slice-count",
                "1",
                "--provisioning-model",
                "standard",
                "--candidate",
                "us-central1-b",
            ),
        ),
        3,
        "quota.resolve",
    ),
    ((("audit", "list"), ("aud", "l")), 0, "audit.list"),
    (
        (
            ("audit", "inspect", "audit-00000000000000000001"),
            ("aud", "i", "audit-00000000000000000001"),
        ),
        3,
        "audit.inspect",
    ),
    ((("audit", "verify"), ("aud", "v")), 0, "audit.verify"),
)
RUNTIME_GUARD = """
import os
import sys

FORBIDDEN = (
    ("keyring", "textual")
    if os.environ.get("CQMGR_SMOKE_ALLOW_GOOGLE_IMPORTS") == "1"
    else ("google", "keyring", "textual")
)

class BlockForbiddenImports:
    def find_spec(self, fullname, path, target=None):
        if fullname in FORBIDDEN or fullname.startswith(
            tuple(f"{item}." for item in FORBIDDEN)
        ):
            raise AssertionError(f"forbidden metadata-command import: {fullname}")
        return None

def is_windows_asyncio_socketpair(event, arguments):
    if os.name != "nt" or event != "socket.connect" or len(arguments) < 2:
        return False
    address = arguments[1]
    if (
        not isinstance(address, tuple)
        or not address
        or address[0] not in {"127.0.0.1", "::1"}
    ):
        return False
    frame = sys._getframe()
    while frame is not None:
        if (
            frame.f_code.co_name == "_fallback_socketpair"
            and frame.f_globals.get("__name__") == "socket"
        ):
            return True
        frame = frame.f_back
    return False

def block_network(event, arguments):
    forbidden = {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
    }
    if event in forbidden and not is_windows_asyncio_socketpair(event, arguments):
        raise AssertionError(f"forbidden metadata-command network access: {event}")

sys.meta_path.insert(0, BlockForbiddenImports())
sys.addaudithook(block_network)
print("CQMGR-RUNTIME-GUARD-ACTIVE", file=sys.stderr)
"""
TTY_DISPATCH_SCRIPT = """
import json
import sys
from pathlib import Path

import cqmgr
import cqmgr.cli
import cqmgr.tui

checkout = Path(sys.argv[1]).resolve()
assert not Path(cqmgr.__file__).resolve().is_relative_to(checkout)

calls = []
current = ["bare"]
cqmgr.cli._interactive_streams = lambda: (True, True)
cqmgr.tui.run = lambda: calls.append(current[0])

cqmgr.cli.main([], prog_name="cqmgr", standalone_mode=False)
current[0] = "explicit"
cqmgr.cli.main(["tui"], prog_name="cqmgr", standalone_mode=False)
print(json.dumps(calls))
"""


def _without_runtime_guard(stderr: str) -> str:
    """Remove only the fixture marker emitted before the installed command."""
    return stderr.replace(f"{RUNTIME_GUARD_MARKER}\n", "")


def _canonical_command_path(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Expand one documented alias path independently of Click resolution."""
    canonical: list[str] = []
    for argument in arguments:
        if argument == "--help":
            break
        canonical.append(
            CANONICAL_ALIASES_BY_PARENT.get(tuple(canonical), {}).get(
                argument,
                argument,
            )
        )
    return tuple(canonical)


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    expected_returncode: int = 0,
) -> tuple[str, str]:
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected_returncode, (
        f"command: {command!r}\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed.stdout, completed.stderr


def _exercise_tty_dispatch(
    interpreter: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    """Dispatch both installed interactive entry points through the safe stub."""
    output, errors = _run(
        [
            str(interpreter),
            "-c",
            TTY_DISPATCH_SCRIPT,
            str(CHECKOUT_ROOT),
        ],
        cwd=cwd,
        environment=environment,
    )
    assert json.loads(output) == ["bare", "explicit"]
    assert _without_runtime_guard(errors) == ""


def _exercise_offline_functional_cases(
    executable: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    """Dispatch every installed canonical and alias leaf through an offline path."""
    for (
        invocations,
        expected_returncode,
        expected_operation,
    ) in OFFLINE_FUNCTIONAL_CASES:
        for arguments in invocations:
            command_environment = environment.copy()
            if expected_operation.startswith("quota."):
                command_environment["CQMGR_SMOKE_ALLOW_GOOGLE_IMPORTS"] = "1"
                command_environment["CQMGR_BUDGET_PATH"] = (
                    environment["CQMGR_BUDGET_PATH"] + "-functional"
                )
            output, errors = _run(
                [
                    str(executable),
                    *arguments,
                    "--output",
                    "json",
                    "--no-color",
                    "--quiet",
                ],
                cwd=cwd,
                environment=command_environment,
                expected_returncode=expected_returncode,
            )
            payload = json.loads(output)
            assert payload["schema"] == "cqmgr.operation-result/v1"
            assert payload["operation"] == expected_operation
            assert payload["outcome"]["exit_class"] == expected_returncode
            assert "\x1b" not in output
            assert _without_runtime_guard(errors) == ""


def smoke_artifact(  # noqa: PLR0915 - one installed-artifact acceptance flow
    artifact: Path,
    python: str,
    *,
    native_keyring_round_trip: bool,
) -> None:
    """Install one artifact and run offline-safe metadata commands."""
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        bin_directory = temporary / "bin"
        install_home = temporary / "install-home"
        runtime_home = temporary / "runtime-home"
        install_home.mkdir()
        runtime_home.mkdir()
        install_environment = os.environ.copy()
        install_environment.update(
            {
                "APPDATA": str(install_home / "appdata"),
                "HOME": str(install_home),
                "LOCALAPPDATA": str(install_home / "local-appdata"),
                "UV_CACHE_DIR": str(temporary / "uv-cache"),
                "UV_NO_PROGRESS": "1",
                "UV_PYTHON_INSTALL_DIR": str(temporary / "uv-python"),
                "UV_TOOL_BIN_DIR": str(bin_directory),
                "UV_TOOL_DIR": str(temporary / "uv-tools"),
                "XDG_CACHE_HOME": str(install_home / "xdg-cache"),
                "XDG_CONFIG_HOME": str(install_home / "xdg-config"),
                "XDG_DATA_HOME": str(install_home / "xdg-data"),
                "XDG_STATE_HOME": str(install_home / "xdg-state"),
            }
        )
        install_artifact = artifact.resolve()
        if artifact.name.endswith(".tar.gz"):
            built_directory = temporary / "sdist-wheel"
            _run(
                [
                    "uv",
                    "build",
                    "--wheel",
                    "--no-sources",
                    "--python",
                    python,
                    "--out-dir",
                    str(built_directory),
                    str(artifact.resolve()),
                ],
                cwd=temporary,
                environment=install_environment,
            )
            built_wheels = list(built_directory.glob("*.whl"))
            assert len(built_wheels) == 1
            install_artifact = built_wheels[0]
        _run(
            [
                "uv",
                "tool",
                "install",
                "--no-build",
                "--python",
                python,
                str(install_artifact),
            ],
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
                "APPDATA": str(runtime_home / "appdata"),
                "HOME": str(runtime_home),
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "LOCALAPPDATA": str(runtime_home / "local-appdata"),
                "NO_PROXY": "",
                "PYTHONPATH": str(guard_directory),
                "XDG_CACHE_HOME": str(runtime_home / "xdg-cache"),
                "XDG_CONFIG_HOME": str(runtime_home / "xdg-config"),
                "XDG_DATA_HOME": str(runtime_home / "xdg-data"),
                "XDG_STATE_HOME": str(runtime_home / "xdg-state"),
            }
        )
        executable = bin_directory / ("cqmgr.exe" if os.name == "nt" else "cqmgr")
        tool_environment = temporary / "uv-tools" / "cqmgr"
        interpreter = tool_environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        _exercise_tty_dispatch(
            interpreter,
            cwd=temporary,
            environment=runtime_environment,
        )
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
        for arguments in READ_ONLY_HELP_INVOCATIONS:
            command_output, command_errors = _run(
                [str(executable), *arguments],
                cwd=temporary,
                environment=runtime_environment,
            )
            assert RUNTIME_GUARD_MARKER in command_errors
            canonical_path = " ".join(_canonical_command_path(arguments))
            assert command_output.startswith(f"Usage: cqmgr {canonical_path} ")
        for arguments in REJECTED_ALIAS_INVOCATIONS:
            command_output, command_errors = _run(
                [str(executable), *arguments],
                cwd=temporary,
                environment=runtime_environment,
                expected_returncode=2,
            )
            assert command_output == ""
            assert RUNTIME_GUARD_MARKER in command_errors
            assert "No such command" in command_errors
        assert list(runtime_home.rglob("*")) == []

        bare_output, bare_errors = _run(
            [str(executable)],
            cwd=temporary,
            environment=runtime_environment,
            expected_returncode=2,
        )
        assert bare_output.startswith("Usage: cqmgr")
        assert _without_runtime_guard(bare_errors) == ""

        runtime_environment.update(
            {
                "CQMGR_AUDIT_PATH": str(runtime_home / "audit"),
                "CQMGR_BUDGET_PATH": str(runtime_home / "budgets"),
                "CQMGR_CONFIG_PATH": str(runtime_home / "config.toml"),
                "CQMGR_QUOTA_SNAPSHOT_PATH": str(runtime_home / "quota-snapshots"),
                "CQMGR_SELECTION_STATE_PATH": str(runtime_home / "selection.toml"),
            }
        )
        _exercise_offline_functional_cases(
            executable,
            cwd=temporary,
            environment=runtime_environment,
        )
        fixture_environment = runtime_environment.copy()
        fixture_environment.pop("PYTHONPATH")
        fixture_output, fixture_errors = _run(
            [
                str(interpreter),
                str(CHECKOUT_ROOT / "scripts/artifact_read_only_fixture.py"),
                runtime_environment["CQMGR_AUDIT_PATH"],
                runtime_environment["CQMGR_QUOTA_SNAPSHOT_PATH"],
                str(CHECKOUT_ROOT),
            ],
            cwd=temporary,
            environment=fixture_environment,
        )
        assert fixture_errors == ""
        fixture = json.loads(fixture_output)

        selected_output, selected_errors = _run(
            [
                str(executable),
                "sc",
                "se",
                "--resource-scope",
                "projects/123",
                "--output",
                "json",
            ],
            cwd=temporary,
            environment=runtime_environment,
        )
        selected = json.loads(selected_output)
        assert selected["schema"] == "cqmgr.operation-result/v1"
        assert selected["operation"] == "scope.select"
        assert selected["outcome"] == {"code": "succeeded", "exit_class": 0}
        assert _without_runtime_guard(selected_errors) == ""

        scope_output, scope_errors = _run(
            [str(executable), "scope", "show"],
            cwd=temporary,
            environment=runtime_environment,
        )
        assert "Resource scope: projects/123" in scope_output
        assert "Authenticated principal: deferred (offline)" in scope_output
        assert _without_runtime_guard(scope_errors) == ""

        audit_output, audit_errors = _run(
            [str(executable), "audit", "list", "--output", "json"],
            cwd=temporary,
            environment=runtime_environment,
        )
        audit_result = json.loads(audit_output)
        assert audit_result["schema"] == "cqmgr.operation-result/v1"
        assert audit_result["operation"] == "audit.list"
        assert "[REDACTED]" in audit_output
        assert "private.operator@example.com" not in audit_output
        assert "private-access-token" not in audit_output
        assert '"private":"provider-body"' not in audit_output
        assert _without_runtime_guard(audit_errors) == ""

        missing_output, missing_errors = _run(
            [
                str(executable),
                "aud",
                "i",
                "audit-99999999999999999999",
            ],
            cwd=temporary,
            environment=runtime_environment,
            expected_returncode=3,
        )
        assert missing_output == ""
        missing_error = _without_runtime_guard(missing_errors)
        assert "Operation: audit.inspect" in missing_error
        assert "Outcome: audit-record-not-found (exit 3)" in missing_error

        cursor_output, cursor_errors = _run(
            [
                str(executable),
                "q",
                "l",
                "--cursor",
                fixture["cursor"],
                "--output",
                "json",
            ],
            cwd=temporary,
            environment=runtime_environment,
        )
        cursor_result = json.loads(cursor_output)
        assert cursor_result["schema"] == "cqmgr.operation-result/v1"
        assert cursor_result["outcome"] == {"code": "page-read", "exit_class": 0}
        assert cursor_result["data"]["items"][0]["identity"]["service"] == (
            "compute.googleapis.com"
        )
        assert _without_runtime_guard(cursor_errors) == ""
        assert not Path(runtime_environment["CQMGR_BUDGET_PATH"]).exists()

        rejected_cursor_output, rejected_cursor_errors = _run(
            [
                str(executable),
                "quota",
                "list",
                "--cursor",
                "B" * 43,
            ],
            cwd=temporary,
            environment=runtime_environment,
            expected_returncode=3,
        )
        assert rejected_cursor_output == ""
        rejected_cursor_error = _without_runtime_guard(rejected_cursor_errors)
        assert "Operation: quota.list" in rejected_cursor_error
        assert "Outcome: cursor-rejected (exit 3)" in rejected_cursor_error
        assert not Path(runtime_environment["CQMGR_BUDGET_PATH"]).exists()

        perform_native_round_trip = (
            native_keyring_round_trip and artifact.name.endswith(".whl")
        )
        persistence_environment = runtime_environment.copy()
        persistence_environment.pop("PYTHONPATH")
        if perform_native_round_trip:
            for key in ("APPDATA", "HOME", "LOCALAPPDATA"):
                if key in os.environ:
                    persistence_environment[key] = os.environ[key]
                else:
                    persistence_environment.pop(key, None)
        command = [
            str(interpreter),
            str(CHECKOUT_ROOT / "scripts/artifact_persistence_smoke.py"),
            str(runtime_home / "persistence-smoke"),
            str(CHECKOUT_ROOT),
        ]
        if perform_native_round_trip:
            command.append("--native-keyring-round-trip")
        _run(command, cwd=temporary, environment=persistence_environment)


def main() -> None:
    """Parse arguments and smoke-test every artifact."""
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("--python", default="3.14")
    parser.add_argument("--native-keyring-round-trip", action="store_true")
    arguments = parser.parse_args()
    artifacts = sorted(
        path
        for path in arguments.dist_dir.iterdir()
        if path.name.endswith((".tar.gz", ".whl"))
    )
    assert len(artifacts) == EXPECTED_ARTIFACT_COUNT
    for artifact in artifacts:
        smoke_artifact(
            artifact,
            arguments.python,
            native_keyring_round_trip=arguments.native_keyring_round_trip,
        )


if __name__ == "__main__":
    main()
