"""Machine-readable performance evidence is checked against approved budgets."""

from __future__ import annotations

import json
import runpy
import sys
import typing
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "measure_performance.py"
BASELINES = (
    Path(__file__).parents[1]
    / "docs"
    / "release"
    / "performance-baseline-macos-arm64-python314.json",
    Path(__file__).parents[1]
    / "docs"
    / "release"
    / "performance-baseline-ubuntu-x86_64-python314.json",
    Path(__file__).parents[1]
    / "docs"
    / "release"
    / "performance-baseline-windows-x86_64-python314.json",
)
BUDGETS = Path(__file__).parents[1] / "docs" / "release" / "performance-budgets.json"


def test_performance_report_records_every_required_axis_without_invented_budgets() -> (
    None
):
    """Executable measurements are evidence; operator-reviewed limits are separate."""
    measurement_report = typing.cast(
        "typing.Any",
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
    measurement_report = typing.cast(
        "typing.Any",
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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_performance_report_rejects_non_finite_measurements(value: float) -> None:
    """Non-finite executable evidence cannot bypass ceiling comparisons."""
    measurement_report = typing.cast(
        "typing.Any",
        runpy.run_path(str(SCRIPT))["measurement_report"],
    )

    with pytest.raises(ValueError, match="finite"):
        measurement_report(
            cold_start_seconds=(value,),
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
    measure = typing.cast("typing.Any", module["measure"])
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


def test_main_uses_the_qualification_run_count_by_default(tmp_path: Path) -> None:
    """Default evidence generation cannot drift from qualification policy."""
    module = runpy.run_path(str(SCRIPT))
    main = typing.cast("typing.Any", module["main"])
    captured_runs: list[int] = []

    def measure(runs: int) -> dict[str, object]:
        captured_runs.append(runs)
        return {"schema": "test"}

    main.__globals__["REQUIRED_BUDGET_COLD_START_RUNS"] = 6
    main.__globals__["measure"] = measure

    main(("--output", str(tmp_path / "report.json")))

    assert captured_runs == [6]


def test_memory_probe_records_portable_process_rss() -> None:
    """Resident memory is an OS process metric on every supported platform."""
    module = runpy.run_path(str(SCRIPT))
    probe = typing.cast("str", module["MEMORY_PROBE"])
    measure_memory = typing.cast("typing.Any", module["_memory_measurements"])

    assert "psutil.Process().memory_info().rss" in probe
    assert "resident = peak" not in probe
    resident, peak = measure_memory()
    assert resident > 0
    assert peak > 0


def test_committed_performance_baseline_is_loaded_and_validated() -> None:
    """Retained representative CI artifacts satisfy the executable schema."""
    module = runpy.run_path(str(SCRIPT))
    validate = typing.cast("typing.Any", module["validate_measurement_report"])
    baselines = [
        json.loads(baseline.read_text(encoding="utf-8")) for baseline in BASELINES
    ]

    assert [validate(baseline) for baseline in baselines] == baselines
    platforms = {
        typing.cast("dict[str, str]", baseline["environment"])["platform"]
        for baseline in baselines
    }
    assert any(platform.startswith("macOS-") for platform in platforms)
    assert any(platform.startswith("Linux-") for platform in platforms)
    assert any(platform.startswith("Windows-") for platform in platforms)


def test_committed_performance_budgets_are_exact_and_operator_approved() -> None:
    """The retained policy records exactly the five approved regression ceilings."""
    module = runpy.run_path(str(SCRIPT))
    validate = typing.cast("typing.Any", module["validate_performance_budgets"])
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))

    assert validate(budgets) == budgets
    assert budgets == {
        "budgets": {
            "cold_start_seconds": 2.0,
            "first_tui_render_seconds": 1.0,
            "peak_python_memory_bytes": 48 * 1024 * 1024,
            "resident_memory_bytes": 128 * 1024 * 1024,
            "steady_refresh_seconds": 0.5,
        },
        "schema": "cqmgr.performance-budgets/v1",
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_performance_budgets_reject_non_finite_ceilings(value: float) -> None:
    """Malformed policy values fail before qualification comparisons."""
    module = runpy.run_path(str(SCRIPT))
    validate = typing.cast("typing.Any", module["validate_performance_budgets"])
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    typing.cast("dict[str, object]", budgets["budgets"])["cold_start_seconds"] = value

    with pytest.raises(ValueError, match="finite"):
        validate(budgets)


def test_performance_budgets_accept_exact_thresholds() -> None:
    """A median equal to its ceiling qualifies despite one retained outlier."""
    module = runpy.run_path(str(SCRIPT))
    enforce = typing.cast("typing.Any", module["enforce_performance_budgets"])
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    limits = typing.cast("dict[str, float | int]", budgets["budgets"])
    baseline = {
        "environment": {"platform": "test", "python": "3.14"},
        "measurements": {
            "cold_start_seconds": {
                "maximum": limits["cold_start_seconds"] * 4,
                "median": limits["cold_start_seconds"],
                "runs": 5,
            },
            "first_tui_render_seconds": limits["first_tui_render_seconds"],
            "peak_python_memory_bytes": limits["peak_python_memory_bytes"],
            "resident_memory_bytes": limits["resident_memory_bytes"],
            "steady_refresh_seconds": limits["steady_refresh_seconds"],
        },
        "schema": "cqmgr.performance-baseline/v1",
    }

    assert enforce(baseline, budgets) == baseline


@pytest.mark.parametrize(
    ("cold_starts", "qualifies"),
    [
        ((0.5, 0.5, 0.5, 2.5, 2.5), True),
        ((0.5, 0.5, 2.5, 2.5, 2.5), False),
    ],
)
def test_performance_budgets_enforce_the_five_launch_boundary(
    cold_starts: tuple[float, ...],
    *,
    qualifies: bool,
) -> None:
    """Two slow launches qualify, while three make the exact-five median fail."""
    module = runpy.run_path(str(SCRIPT))
    measurement_report = typing.cast("typing.Any", module["measurement_report"])
    enforce = typing.cast("typing.Any", module["enforce_performance_budgets"])
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    report = measurement_report(
        cold_start_seconds=cold_starts,
        resident_memory_bytes=1,
        peak_python_memory_bytes=1,
        first_tui_render_seconds=0.1,
        steady_refresh_seconds=0.1,
        platform_name="test",
        python_version="3.14",
    )

    if qualifies:
        assert enforce(report, budgets) == report
    else:
        with pytest.raises(ValueError, match="cold_start_seconds"):
            enforce(report, budgets)


@pytest.mark.parametrize("runs", [4, 7])
def test_performance_budgets_require_exactly_five_cold_start_samples(
    runs: int,
) -> None:
    """Qualification uses the exact five-run policy behind its median."""
    module = runpy.run_path(str(SCRIPT))
    enforce = typing.cast("typing.Any", module["enforce_performance_budgets"])
    baseline = json.loads(BASELINES[0].read_text(encoding="utf-8"))
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    measurements = typing.cast("dict[str, object]", baseline["measurements"])
    measurements["cold_start_seconds"] = {
        "maximum": 0.6,
        "median": 0.5,
        "runs": runs,
    }

    with pytest.raises(ValueError, match="exactly 5"):
        enforce(baseline, budgets)


def test_performance_budgets_report_every_exceeded_ceiling() -> None:
    """Qualification fails closed with every actionable regression named."""
    module = runpy.run_path(str(SCRIPT))
    enforce = typing.cast("typing.Any", module["enforce_performance_budgets"])
    baseline = json.loads(BASELINES[0].read_text(encoding="utf-8"))
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    measurements = typing.cast("dict[str, object]", baseline["measurements"])
    measurements["cold_start_seconds"] = {
        "maximum": 6.0,
        "median": 2.001,
        "runs": 5,
    }
    measurements["resident_memory_bytes"] = 128 * 1024 * 1024 + 1

    with pytest.raises(
        ValueError,
        match=r"cold_start_seconds.*resident_memory_bytes",
    ):
        enforce(baseline, budgets)
