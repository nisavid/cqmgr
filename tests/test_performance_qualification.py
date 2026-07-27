"""Machine-readable performance evidence remains baseline-only until review."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "measure_performance.py"


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
        first_tui_render_contract_seconds=1.2,
        steady_refresh_contract_seconds=0.8,
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
    assert report["measurements"]["first_tui_render_contract_seconds"] == first_render
    assert report["measurements"]["steady_refresh_contract_seconds"] == steady_refresh
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
            first_tui_render_contract_seconds=1.0,
            steady_refresh_contract_seconds=1.0,
            platform_name="test",
            python_version="3.14",
        )
