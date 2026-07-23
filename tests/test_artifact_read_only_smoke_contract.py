"""The artifact smoke owns the complete V1 read-only command matrix."""

import runpy
from pathlib import Path
from typing import cast


def _smoke_contract() -> dict[str, object]:
    return runpy.run_path(str(Path("scripts/smoke_tool_install.py")))


def _invocations(name: str) -> tuple[tuple[str, ...], ...]:
    return cast("tuple[tuple[str, ...], ...]", _smoke_contract()[name])


def test_artifact_smoke_covers_every_canonical_and_alias_command() -> None:
    """Every command identity is exercised from each built executable."""
    expected = {
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
    }

    assert set(_invocations("READ_ONLY_HELP_INVOCATIONS")) == expected


def test_artifact_smoke_rejects_fuzzy_and_retired_aliases() -> None:
    """Built executables reject aliases outside the exact public registry."""
    expected = {
        ("sco", "--help"),
        ("pro", "--help"),
        ("con", "--help"),
        ("quo", "--help"),
        ("scope", "sho", "--help"),
        ("profile", "lis", "--help"),
        ("quota", "lis", "--help"),
        ("q", "r", "com", "--help"),
        ("audit", "lis", "--help"),
    }

    assert set(_invocations("REJECTED_ALIAS_INVOCATIONS")) == expected


def test_artifact_smoke_executes_every_canonical_and_alias_leaf() -> None:
    """Every leaf dispatches through each built executable, not only through help."""
    cases = cast(
        "tuple[tuple[tuple[tuple[str, ...], ...], int, str], ...]",
        _smoke_contract()["OFFLINE_FUNCTIONAL_CASES"],
    )
    actual = {
        arguments[: (3 if operation == "quota.resolve" else 2)]
        for invocations, _exit_class, operation in cases
        for arguments in invocations
    }
    expected = {
        ("scope", "show"),
        ("sc", "sh"),
        ("scope", "select"),
        ("sc", "se"),
        ("scope", "clear"),
        ("sc", "cl"),
        ("profile", "list"),
        ("pf", "l"),
        ("profile", "get"),
        ("pf", "g"),
        ("profile", "select"),
        ("pf", "s"),
        ("config", "get"),
        ("cfg", "g"),
        ("config", "set"),
        ("cfg", "s"),
        ("quota", "list"),
        ("q", "l"),
        ("quota", "inspect"),
        ("q", "i"),
        ("quota", "resolve", "compute-instance"),
        ("q", "r", "ci"),
        ("quota", "resolve", "cloud-tpu-slice"),
        ("q", "r", "ct"),
        ("audit", "list"),
        ("aud", "l"),
        ("audit", "inspect"),
        ("aud", "i"),
        ("audit", "verify"),
        ("aud", "v"),
    }

    assert actual == expected
