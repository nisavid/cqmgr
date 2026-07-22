"""Offline local CLI and bootstrap-classification contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import cqmgr.cli as cli_module
from cqmgr.bootstrap import InvocationKind, classify_invocation
from cqmgr.cli import main

REJECTED_PRECONDITION_EXIT = 3
OPERATIONAL_FAILURE_EXIT = 9


@pytest.mark.parametrize(
    ("arguments", "stdin_is_tty", "stdout_is_tty", "expected"),
    [
        ((), False, False, InvocationKind.HELP),
        ((), True, True, InvocationKind.TUI),
        (("--version",), False, False, InvocationKind.HELP),
        (("quo", "--help"), False, False, InvocationKind.HELP),
        (("sco", "sho"), False, False, InvocationKind.LOCAL),
        (("pro", "lis"), False, False, InvocationKind.LOCAL),
        (("con", "get", "interface.no-color"), False, False, InvocationKind.LOCAL),
        (("tui",), True, True, InvocationKind.TUI),
        (("quo", "lis"), False, False, InvocationKind.PROVIDER),
        (("unknown",), False, False, InvocationKind.INVALID),
    ],
)
def test_invocation_is_classified_before_optional_runtime_imports(
    arguments: tuple[str, ...],
    *,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
    expected: InvocationKind,
) -> None:
    """Aliases and metadata are classified without importing integrations."""
    assert (
        classify_invocation(
            arguments,
            stdin_is_tty=stdin_is_tty,
            stdout_is_tty=stdout_is_tty,
        )
        is expected
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ("scope", "--help"),
        ("sco", "show", "--help"),
        ("tui", "--help"),
    ],
)
def test_click_root_classifies_the_complete_raw_invocation(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    """Actual nested help reaches bootstrap classification with its raw argv."""
    observed: list[tuple[str, ...]] = []
    classify = cli_module.classify_invocation

    def recording_classifier(
        raw_arguments: tuple[str, ...],
        *,
        stdin_is_tty: bool,
        stdout_is_tty: bool,
    ) -> InvocationKind:
        observed.append(tuple(raw_arguments))
        return classify(
            raw_arguments,
            stdin_is_tty=stdin_is_tty,
            stdout_is_tty=stdout_is_tty,
        )

    monkeypatch.setattr(cli_module, "classify_invocation", recording_classifier)

    result = CliRunner().invoke(main, list(arguments))

    assert result.exit_code == 0, result.output
    assert observed == [arguments]


def local_environment(tmp_path: Path) -> dict[str, str]:
    """Provide explicit cqmgr-only paths for hermetic CLI state."""
    return {
        "CQMGR_CONFIG_PATH": str(tmp_path / "config.toml"),
        "CQMGR_SELECTION_STATE_PATH": str(tmp_path / "selection.toml"),
    }


def test_scope_commands_and_aliases_preserve_resolution_source(tmp_path: Path) -> None:
    """Canonical and three-letter commands share one atomic local operation."""
    runner = CliRunner()
    environment = local_environment(tmp_path)

    selected = runner.invoke(
        main,
        [
            "sco",
            "sel",
            "--resource-scope",
            "projects/123",
            "--output",
            "json",
        ],
        env=environment,
    )
    shown = runner.invoke(
        main,
        ["scope", "show", "--output", "json"],
        env=environment,
    )

    assert selected.exit_code == 0, selected.output
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert payload["resource_scope"] == {
        "type": "project",
        "name": "projects/123",
    }
    assert payload["data"]["resolution_source"] == "direct-selection"


def test_folder_selection_is_rejected_without_replacing_project(tmp_path: Path) -> None:
    """Unsupported V1 scope input returns class 3 and preserves prior state."""
    runner = CliRunner()
    environment = local_environment(tmp_path)
    assert (
        runner.invoke(
            main,
            ["scope", "select", "--resource-scope", "projects/123"],
            env=environment,
        ).exit_code
        == 0
    )

    rejected = runner.invoke(
        main,
        [
            "scope",
            "select",
            "--resource-scope",
            "folders/456",
            "--output",
            "json",
        ],
        env=environment,
    )
    shown = runner.invoke(
        main,
        ["scope", "show", "--output", "json"],
        env=environment,
    )

    assert rejected.exit_code == REJECTED_PRECONDITION_EXIT
    assert json.loads(rejected.stdout)["outcome"]["code"] == (
        "unsupported-resource-scope"
    )
    assert json.loads(shown.stdout)["resource_scope"]["name"] == "projects/123"


def test_decoded_json_scope_syntax_failure_returns_a_structured_usage_result(
    tmp_path: Path,
) -> None:
    """A recognized JSON invocation preserves its result form on input failure."""
    result = CliRunner().invoke(
        main,
        [
            "scope",
            "select",
            "--resource-scope",
            "not-canonical",
            "--output",
            "json",
        ],
        env=local_environment(tmp_path),
    )

    assert result.exit_code == click.UsageError.exit_code
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["operation"] == "scope.select"
    assert payload["outcome"] == {"code": "invalid-resource-scope", "exit_class": 2}
    assert payload["boundary"] == {
        "condition": "resource-scope-valid",
        "reached": False,
    }


def test_profile_and_config_commands_use_separate_files(tmp_path: Path) -> None:
    """Profile selection never rewrites operator-owned profile configuration."""
    environment = local_environment(tmp_path)
    config_path = Path(environment["CQMGR_CONFIG_PATH"])
    config_path.write_text(
        'schema = "cqmgr.config/v1"\n\n'
        "[profiles.primary]\n"
        'resource_scope = "projects/789"\n'
    )
    original = config_path.read_bytes()
    runner = CliRunner()

    selected = runner.invoke(main, ["pro", "sel", "primary"], env=environment)
    after_profile_selection = config_path.read_bytes()
    scope = runner.invoke(
        main,
        ["sco", "sho", "--output", "json"],
        env=environment,
    )
    configured = runner.invoke(
        main,
        ["con", "set", "interface.nerd-font", "true"],
        env=environment,
    )

    assert selected.exit_code == 0, selected.output
    assert after_profile_selection == original
    assert json.loads(scope.stdout)["data"]["resolution_source"] == "selected-profile"
    assert configured.exit_code == 0, configured.output
    assert b"nerd_font = true" in config_path.read_bytes()
    assert Path(environment["CQMGR_SELECTION_STATE_PATH"]).exists()


def test_profile_json_exposes_only_safe_os_keyring_reference_metadata(
    tmp_path: Path,
) -> None:
    """Structured profile output never emits contact or credential-shaped text."""
    environment = local_environment(tmp_path)
    Path(environment["CQMGR_CONFIG_PATH"]).write_text(
        'schema = "cqmgr.config/v1"\n\n'
        "[profiles.primary]\n"
        'quota_contact_keyring_reference = "cqmgr:quota-contact:primary"\n'
    )

    result = CliRunner().invoke(
        main,
        ["profile", "get", "primary", "--output", "json"],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    reference = json.loads(result.stdout)["data"]["profile"][
        "quota_contact_keyring_reference"
    ]
    assert reference == {
        "account": "quota-contact:primary",
        "backend": "os-keyring",
        "service": "cqmgr",
    }


def test_profile_select_human_output_reports_the_effective_scope_and_source(
    tmp_path: Path,
) -> None:
    """The selected profile remains distinct from the winning direct project."""
    environment = local_environment(tmp_path)
    Path(environment["CQMGR_CONFIG_PATH"]).write_text(
        'schema = "cqmgr.config/v1"\n\n'
        "[profiles.secondary]\n"
        'resource_scope = "projects/789"\n'
    )
    runner = CliRunner()
    assert (
        runner.invoke(
            main,
            ["scope", "select", "--resource-scope", "projects/123"],
            env=environment,
        ).exit_code
        == 0
    )

    result = runner.invoke(
        main,
        ["profile", "select", "secondary"],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    assert "Profile resource scope: projects/789" in result.stdout
    assert "Effective resource scope: projects/123" in result.stdout
    assert "Resolution source: direct-selection" in result.stdout


@pytest.mark.parametrize(
    "command",
    [
        ("scope", "show"),
        ("scope", "select"),
        ("scope", "clear"),
        ("profile", "list"),
        ("profile", "get"),
        ("profile", "select"),
        ("config", "get"),
        ("config", "set"),
    ],
)
def test_every_local_leaf_exposes_shared_presentation_options(
    command: tuple[str, ...],
) -> None:
    """No-color and quiet remain leaf-scoped public compatibility options."""
    result = CliRunner().invoke(main, [*command, "--help"])

    assert result.exit_code == 0, result.output
    assert "--no-color" in result.stdout
    assert "--quiet" in result.stdout


def test_no_color_and_quiet_preserve_required_human_result_facts(
    tmp_path: Path,
) -> None:
    """Presentation controls suppress neither result identity nor safety facts."""
    environment = local_environment(tmp_path)
    selected = CliRunner().invoke(
        main,
        [
            "scope",
            "select",
            "--resource-scope",
            "projects/123",
            "--no-color",
            "--quiet",
        ],
        env=environment,
    )

    assert selected.exit_code == 0, selected.output
    assert selected.stdout == (
        "Resource scope: projects/123\nResolution source: direct-selection\n"
    )

    rejected = CliRunner().invoke(
        main,
        [
            "scope",
            "select",
            "--resource-scope",
            "folders/456",
            "--no-color",
            "--quiet",
        ],
        env=environment,
    )

    assert rejected.exit_code == REJECTED_PRECONDITION_EXIT
    assert "Outcome: unsupported-resource-scope (exit 3)" in rejected.stderr
    assert "Boundary: local-selection-updated (not reached)" in rejected.stderr
    assert "folder resource scopes are reserved but unsupported" in rejected.stderr


def test_local_commands_do_not_import_or_initialize_optional_integrations(
    tmp_path: Path,
) -> None:
    """Local operations succeed when Textual, ADC, provider, and keyring fail."""
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "selection.toml"
    script = r"""
import json
import os
import sys

forbidden = ("google", "keyring", "textual")

class BlockForbiddenImports:
    def find_spec(self, fullname, path, target=None):
        if fullname in forbidden or fullname.startswith(
            tuple(f"{item}." for item in forbidden)
        ):
            raise AssertionError(f"forbidden local-command import: {fullname}")
        return None

sys.meta_path.insert(0, BlockForbiddenImports())
os.environ["CQMGR_CONFIG_PATH"] = sys.argv[1]
os.environ["CQMGR_SELECTION_STATE_PATH"] = sys.argv[2]
os.environ["CLOUDSDK_CORE_PROJECT"] = "ambient-must-not-be-read"
os.environ["GOOGLE_CLOUD_PROJECT"] = "ambient-must-not-be-read"

from cqmgr.cli import main

for arguments in (
    ["scope", "select", "--resource-scope", "projects/123"],
    ["scope", "show"],
    ["profile", "list"],
    ["config", "get", "interface.no-color"],
):
    try:
        main(arguments, prog_name="cqmgr", standalone_mode=False)
    except SystemExit as error:
        if error.code not in (0, None):
            raise

print(json.dumps(sorted(
    name for name in sys.modules
    if name in forbidden or name.startswith(tuple(f"{item}." for item in forbidden))
)))
"""

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(config_path), str(state_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == []


@pytest.mark.parametrize(
    ("schema", "expected_exit", "expected_outcome"),
    [
        (
            "cqmgr.config/v2",
            REJECTED_PRECONDITION_EXIT,
            "unsupported-configuration-schema",
        ),
        ("not-cqmgr", OPERATIONAL_FAILURE_EXIT, "invalid-local-state"),
    ],
)
def test_invalid_configuration_returns_a_typed_result(
    tmp_path: Path,
    schema: str,
    expected_exit: int,
    expected_outcome: str,
) -> None:
    """Local bootstrap classifies unsafe state without a traceback or fallback."""
    environment = local_environment(tmp_path)
    Path(environment["CQMGR_CONFIG_PATH"]).write_text(f'schema = "{schema}"\n')

    result = CliRunner().invoke(
        main,
        ["config", "get", "interface.no-color", "--output", "json"],
        env=environment,
    )

    assert result.exit_code == expected_exit
    payload = json.loads(result.stdout)
    assert payload["outcome"]["code"] == expected_outcome
    assert payload["boundary"] == {"condition": "local-state-valid", "reached": False}
    assert payload["complete"] is False
    assert [item["code"] for item in payload["diagnostics"]] == [expected_outcome]
    assert payload["diagnostics"][0]["phase"] == "local-state-read"
    assert payload["diagnostics"][0]["source"] == "local-state"
    assert payload["data"]["guidance"] == payload["diagnostics"][0]["message"]


def test_human_repository_failure_has_common_envelope_and_safe_guidance(
    tmp_path: Path,
) -> None:
    """A local-state failure stays actionable without exposing stored contents."""
    environment = local_environment(tmp_path)
    Path(environment["CQMGR_CONFIG_PATH"]).write_text("not valid TOML = [")

    result = CliRunner().invoke(
        main,
        ["config", "get", "interface.no-color"],
        env=environment,
    )

    assert result.exit_code == OPERATIONAL_FAILURE_EXIT
    assert result.stdout == ""
    assert "Operation: config.get" in result.stderr
    assert "Outcome: invalid-local-state (exit 9)" in result.stderr
    assert "Boundary: local-state-valid (not reached)" in result.stderr
    assert "Complete: false" in result.stderr
    assert "Repair or restore the cqmgr local-state file, then retry." in result.stderr
    assert "not valid TOML" not in result.stderr


def test_noninteractive_entry_points_fail_before_textual_import() -> None:
    """Bare and explicit TUI invocation return usage without terminal startup."""
    runner = CliRunner()

    bare = runner.invoke(main, [])
    explicit = runner.invoke(main, ["tui"])

    assert bare.exit_code == click.UsageError.exit_code
    assert bare.stdout.startswith("Usage: ")
    assert explicit.exit_code == click.UsageError.exit_code
    assert "requires interactive input and output" in explicit.output
