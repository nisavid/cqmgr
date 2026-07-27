"""Targeted mutation evidence has a stable fail-closed quality gate."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping

SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_mutation_results.py"
EXPECTED_BASELINE_FLOOR = 64.0


class MutationVerifier(Protocol):
    """Typed dynamically loaded mutation verifier."""

    def __call__(
        self,
        stats: Mapping[str, object],
        minimum_score: float,
    ) -> float:
        """Verify one mutation result mapping."""
        ...


def _verify() -> MutationVerifier:
    return cast(
        "MutationVerifier",
        runpy.run_path(str(SCRIPT))["verify_mutation_results"],
    )


def test_reviewed_mutation_baseline_passes() -> None:
    """The initial critical-core evidence establishes an explicit floor."""
    stats = {
        "check_was_interrupted_by_user": 0,
        "killed": 78,
        "no_tests": 0,
        "segfault": 0,
        "survived": 43,
        "suspicious": 0,
        "timeout": 0,
        "total": 121,
    }

    assert _verify()(stats, 60.0) > EXPECTED_BASELINE_FLOOR


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"killed": 60, "survived": 61}, "below reviewed minimum"),
        ({"timeout": 1}, "blocking statuses"),
        ({"suspicious": 1}, "blocking statuses"),
        ({"no_tests": 1}, "blocking statuses"),
        (
            {"total": 0, "killed": 0, "survived": 0},
            "produced no mutants",
        ),
    ],
)
def test_mutation_gate_rejects_regression_or_incomplete_execution(
    change: dict[str, int],
    match: str,
) -> None:
    """Survivor regression and uncertain executions cannot pass the release gate."""
    stats = {
        "check_was_interrupted_by_user": 0,
        "killed": 78,
        "no_tests": 0,
        "segfault": 0,
        "survived": 43,
        "suspicious": 0,
        "timeout": 0,
        "total": 121,
    }
    stats.update(change)

    with pytest.raises(ValueError, match=match):
        _verify()(stats, 60.0)
