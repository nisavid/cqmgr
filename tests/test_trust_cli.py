"""Explicit trust initialization command behavior."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from cqmgr import cli as cli_module
from cqmgr.application.operations.trust import TrustInitializationResult

if TYPE_CHECKING:
    import pytest


class _Operations:
    def __init__(self, result: TrustInitializationResult) -> None:
        self.result = result
        self.calls = 0

    def initialize(self) -> TrustInitializationResult:
        self.calls += 1
        return self.result


def test_trust_init_is_explicit_and_emits_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one explicit command emits only non-secret initialization status."""
    operations = _Operations(TrustInitializationResult(initialized=True))
    monkeypatch.setattr(
        cli_module,
        "build_trust_initialization_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(
        cli_module.main,
        ["trust", "init", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "initialized": True,
        "reason": None,
    }
    assert operations.calls == 1
    assert "secret" not in result.stdout.casefold()


def test_trust_init_fails_closed_when_state_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeat initialization is rejected without claiming success."""
    operations = _Operations(
        TrustInitializationResult(
            initialized=False,
            reason="already-initialized",
        )
    )
    monkeypatch.setattr(
        cli_module,
        "build_trust_initialization_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(cli_module.main, ["trust", "init"])

    assert result.exit_code == 1
    assert "already-initialized" in result.stderr
    assert operations.calls == 1


def test_trust_init_reports_explicit_incomplete_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human output distinguishes a restarted incomplete bootstrap."""
    operations = _Operations(
        TrustInitializationResult(
            initialized=True,
            reason="incomplete-trust-restarted",
        )
    )
    monkeypatch.setattr(
        cli_module,
        "build_trust_initialization_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(cli_module.main, ["trust", "init"])

    assert result.exit_code == 0
    assert "after restarting an incomplete attempt" in result.stdout
    assert operations.calls == 1


def test_trust_help_never_builds_native_keyring_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trust help remains import- and keyring-operation-safe."""

    def forbidden() -> object:
        message = "help must not initialize keyring"
        raise AssertionError(message)

    monkeypatch.setattr(
        cli_module,
        "build_trust_initialization_operations",
        forbidden,
    )

    result = CliRunner().invoke(cli_module.main, ["trust", "--help"])

    assert result.exit_code == 0
    assert "init" in result.stdout
