"""Machine-readable performance evidence remains baseline-only until review."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "measure_performance.py"
BASELINE = (
    Path(__file__).parents[1]
    / "docs"
    / "release"
    / "performance-baseline-macos-arm64-python314.json"
)


def test_performance_report_records_every_required_axis_without_invented_budgets() -> (
    None
):
    """Executable measurements are evidence; operator-reviewed limits are separate."""
    measurement_report = cast(
        "Any",
        runpy.run_path(str(SCRIPT))["measurement_report"],
    )

    report = measurement_report(
        cold_start_seconds=(0.4, 0.3, 0.5),
        resident_memory_bytes=64_000_000,
        peak_python_memory_bytes=12_000_000,
        first_tui_render_seconds=1.2,
        steady_refresh_seconds=0.8,
        platform_name="test-platform",
        python_version="3.14.0",
    )

    assert report["schema"] == "cqmgr.performance-baseline/v1"
    assert report["environment"] == {
        "platform": "test-platform",
        "python": "3.14.0",
    }
    assert report["measurements"]["cold_start_seconds"] == {
        "maximum": 0.5,
        "median": 0.4,
        "runs": 3,
    }
    resident = 64_000_000
    peak = 12_000_000
    first_render = 1.2
    steady_refresh = 0.8
    assert report["measurements"]["resident_memory_bytes"] == resident
    assert report["measurements"]["peak_python_memory_bytes"] == peak
    assert report["measurements"]["first_tui_render_seconds"] == first_render
    assert report["measurements"]["steady_refresh_seconds"] == steady_refresh
    assert "budgets" not in report


@pytest.mark.parametrize(
    "cold_starts",
    [(), (0.0,), (-0.1, 0.2)],
)
def test_performance_report_rejects_missing_or_nonpositive_measurements(
    cold_starts: tuple[float, ...],
) -> None:
    """Incomplete evidence cannot be mistaken for a reviewed baseline."""
    measurement_report = cast(
        "Any",
        runpy.run_path(str(SCRIPT))["measurement_report"],
    )

    with pytest.raises(ValueError, match="positive"):
        measurement_report(
            cold_start_seconds=cold_starts,
            resident_memory_bytes=1,
            peak_python_memory_bytes=1,
            first_tui_render_seconds=1.0,
            steady_refresh_seconds=1.0,
            platform_name="test",
            python_version="3.14",
        )


def test_measure_uses_dedicated_tui_boundaries_not_pytest_process_time() -> None:
    """TUI evidence times actual render and refresh after probe startup."""
    module = runpy.run_path(str(SCRIPT))
    measure = cast("Any", module["measure"])
    commands: list[tuple[str, ...]] = []
    runs = 2
    first_render = 0.2
    steady_refresh = 0.03

    def timed(command: tuple[str, ...]) -> float:
        commands.append(command)
        return 0.1

    measure.__globals__["_run_timed"] = timed
    measure.__globals__["_memory_measurements"] = lambda: (100, 50)
    measure.__globals__["_tui_measurements"] = lambda: (
        first_render,
        steady_refresh,
    )

    report = measure(runs)

    assert len(commands) == runs
    assert commands == [(sys.executable, "-m", "cqmgr", "--help")] * runs
    assert all("pytest" not in argument for command in commands for argument in command)
    assert report["measurements"]["first_tui_render_seconds"] == first_render
    assert report["measurements"]["steady_refresh_seconds"] == steady_refresh


def test_committed_performance_baseline_is_loaded_and_validated() -> None:
    """The retained local artifact satisfies the executable baseline schema."""
    module = runpy.run_path(str(SCRIPT))
    validate = cast("Any", module["validate_measurement_report"])
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert validate(baseline) == baseline
