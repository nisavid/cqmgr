"""Enforce the reviewed targeted-mutation baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

BLOCKING_STATUSES = (
    "check_was_interrupted_by_user",
    "no_tests",
    "segfault",
    "suspicious",
    "timeout",
)
MAXIMUM_SCORE = 100.0


def verify_mutation_results(
    stats: Mapping[str, object],
    minimum_score: float,
) -> float:
    """Require complete execution and a non-regressing killed-mutant score."""
    if not 0 <= minimum_score <= MAXIMUM_SCORE:
        msg = "minimum mutation score must be between 0 and 100"
        raise ValueError(msg)
    values: dict[str, int] = {}
    for name in ("killed", "survived", "total", *BLOCKING_STATUSES):
        value = stats.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            msg = f"mutation statistic {name!r} must be a non-negative integer"
            raise TypeError(msg)
        values[name] = value
    if values["total"] < 1:
        msg = "targeted mutation testing produced no mutants"
        raise ValueError(msg)
    if values["killed"] + values["survived"] != values["total"]:
        msg = "mutation totals do not describe only killed and survived mutants"
        raise ValueError(msg)
    blocking = {name: values[name] for name in BLOCKING_STATUSES if values[name]}
    if blocking:
        msg = f"mutation execution has blocking statuses: {blocking}"
        raise ValueError(msg)
    score = values["killed"] / values["total"] * 100
    if score < minimum_score:
        msg = (
            f"mutation score {score:.2f} is below reviewed minimum {minimum_score:.2f}"
        )
        raise ValueError(msg)
    return score


def main(arguments: Sequence[str] | None = None) -> None:
    """Verify one mutmut CI statistics file."""
    parser = argparse.ArgumentParser()
    parser.add_argument("stats", type=Path)
    parser.add_argument("--minimum-score", type=float, required=True)
    parsed = parser.parse_args(arguments)
    value = json.loads(parsed.stats.read_text())
    if not isinstance(value, dict):
        msg = "mutation statistics must be an object"
        raise TypeError(msg)
    verify_mutation_results(value, parsed.minimum_score)


if __name__ == "__main__":
    main()
