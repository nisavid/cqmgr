"""Record executable release performance evidence without inventing budgets."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

BASELINE_SCHEMA = "cqmgr.performance-baseline/v1"
TUI_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "adapters"
    / "tui"
    / "test_app.py"
)
MEMORY_PROBE = r"""
import contextlib
import io
import json
import sys
import tracemalloc

tracemalloc.start()
with (
    contextlib.redirect_stdout(io.StringIO()),
    contextlib.redirect_stderr(io.StringIO()),
):
    from cqmgr.cli import main
    main(["--help"], prog_name="cqmgr", standalone_mode=False)
_current, peak = tracemalloc.get_traced_memory()
try:
    import resource
    resident = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        resident *= 1024
except ImportError:
    resident = peak
print(json.dumps({"peak_python_memory_bytes": peak, "resident_memory_bytes": resident}))
"""
TUI_PROBE = r"""
import asyncio
import json
import runpy
import sys
import time

from cqmgr.adapters.tui.app import CloudQuotaManagerApp

fixture = runpy.run_path(sys.argv[1])
ScriptedReadOnlyOperations = fixture["ScriptedReadOnlyOperations"]
ScriptedAuditOperations = fixture["ScriptedAuditOperations"]
browse_result = fixture["_browse_result"]

async def measure():
    operations = ScriptedReadOnlyOperations(browse_result())
    started = time.perf_counter()
    app = CloudQuotaManagerApp(operations, ScriptedAuditOperations())
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        first_render = time.perf_counter() - started
        if app.last_result is not operations.result:
            raise AssertionError("first TUI render did not retain the browse result")

        prior_reads = len(operations.browse_calls)
        started = time.perf_counter()
        app.action_refresh()
        for _attempt in range(20):
            await pilot.pause()
            if len(operations.browse_calls) > prior_reads:
                break
        else:
            raise AssertionError("steady TUI refresh did not complete")
        steady_refresh = time.perf_counter() - started
        if app.last_result is not operations.result:
            raise AssertionError("steady TUI refresh did not retain the browse result")
    return first_render, steady_refresh

first_render, steady_refresh = asyncio.run(measure())
print(json.dumps({
    "first_tui_render_seconds": first_render,
    "steady_refresh_seconds": steady_refresh,
}))
"""


def _positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{name} must be numeric executable evidence"
        raise TypeError(msg)
    if value <= 0:
        msg = f"{name} must contain positive executable evidence"
        raise ValueError(msg)


def measurement_report(  # noqa: PLR0913 - stable report input axes
    *,
    cold_start_seconds: tuple[float, ...],
    resident_memory_bytes: int,
    peak_python_memory_bytes: int,
    first_tui_render_seconds: float,
    steady_refresh_seconds: float,
    platform_name: str,
    python_version: str,
) -> dict[str, object]:
    """Normalize one platform baseline while keeping budgets separate."""
    if not cold_start_seconds:
        msg = "cold-start evidence must contain positive executable measurements"
        raise ValueError(msg)
    for index, value in enumerate(cold_start_seconds):
        _positive(value, f"cold_start_seconds[{index}]")
    for value, name in (
        (resident_memory_bytes, "resident_memory_bytes"),
        (peak_python_memory_bytes, "peak_python_memory_bytes"),
        (
            first_tui_render_seconds,
            "first_tui_render_seconds",
        ),
        (steady_refresh_seconds, "steady_refresh_seconds"),
    ):
        _positive(value, name)
    report = {
        "environment": {
            "platform": platform_name,
            "python": python_version,
        },
        "measurements": {
            "cold_start_seconds": {
                "maximum": max(cold_start_seconds),
                "median": statistics.median(cold_start_seconds),
                "runs": len(cold_start_seconds),
            },
            "first_tui_render_seconds": first_tui_render_seconds,
            "peak_python_memory_bytes": peak_python_memory_bytes,
            "resident_memory_bytes": resident_memory_bytes,
            "steady_refresh_seconds": steady_refresh_seconds,
        },
        "schema": BASELINE_SCHEMA,
    }
    return validate_measurement_report(report)


def validate_measurement_report(value: object) -> dict[str, object]:  # noqa: C901
    """Validate one committed baseline without accepting budgets or loose fields."""
    if not isinstance(value, dict) or set(value) != {
        "environment",
        "measurements",
        "schema",
    }:
        msg = "performance baseline must contain the exact V1 fields"
        raise TypeError(msg)
    if value["schema"] != BASELINE_SCHEMA:
        msg = "performance baseline schema is unsupported"
        raise ValueError(msg)
    environment = value["environment"]
    if not isinstance(environment, dict) or set(environment) != {
        "platform",
        "python",
    }:
        msg = "performance baseline environment must be exact"
        raise TypeError(msg)
    if any(
        not isinstance(environment[name], str) or not environment[name]
        for name in ("platform", "python")
    ):
        msg = "performance baseline environment values must be non-empty strings"
        raise TypeError(msg)
    measurements = value["measurements"]
    expected_measurements = {
        "cold_start_seconds",
        "first_tui_render_seconds",
        "peak_python_memory_bytes",
        "resident_memory_bytes",
        "steady_refresh_seconds",
    }
    if not isinstance(measurements, dict) or set(measurements) != expected_measurements:
        msg = "performance baseline measurements must be exact"
        raise TypeError(msg)
    cold_start = measurements["cold_start_seconds"]
    if not isinstance(cold_start, dict) or set(cold_start) != {
        "maximum",
        "median",
        "runs",
    }:
        msg = "cold-start baseline must contain maximum, median, and runs"
        raise TypeError(msg)
    runs = cold_start["runs"]
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        msg = "cold-start runs must be a positive integer"
        raise TypeError(msg)
    for name in ("maximum", "median"):
        _positive(cold_start[name], f"cold_start_seconds.{name}")
    if cold_start["maximum"] < cold_start["median"]:
        msg = "cold-start maximum cannot be below its median"
        raise ValueError(msg)
    for name in ("peak_python_memory_bytes", "resident_memory_bytes"):
        measurement = measurements[name]
        if isinstance(measurement, bool) or not isinstance(measurement, int):
            msg = f"{name} must be integer executable evidence"
            raise TypeError(msg)
        _positive(measurement, name)
    for name in ("first_tui_render_seconds", "steady_refresh_seconds"):
        _positive(measurements[name], name)
    return value


def _run_timed(command: Sequence[str]) -> float:
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        msg = (
            f"performance probe failed: {command!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        raise RuntimeError(msg)
    return elapsed


def _memory_measurements() -> tuple[int, int]:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", MEMORY_PROBE],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        msg = f"memory probe failed: {completed.stderr}"
        raise RuntimeError(msg)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        msg = "memory probe did not return an object"
        raise TypeError(msg)
    resident = value.get("resident_memory_bytes")
    peak = value.get("peak_python_memory_bytes")
    if not isinstance(resident, int) or not isinstance(peak, int):
        msg = "memory probe did not return integer measurements"
        raise TypeError(msg)
    return resident, peak


def _tui_measurements() -> tuple[float, float]:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", TUI_PROBE, str(TUI_FIXTURE)],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        msg = f"TUI performance probe failed: {completed.stderr}"
        raise RuntimeError(msg)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        msg = "TUI performance probe did not return an object"
        raise TypeError(msg)
    first_render = value.get("first_tui_render_seconds")
    steady_refresh = value.get("steady_refresh_seconds")
    if isinstance(first_render, bool) or not isinstance(first_render, (int, float)):
        msg = "TUI performance probe did not return numeric measurements"
        raise TypeError(msg)
    if isinstance(steady_refresh, bool) or not isinstance(
        steady_refresh,
        (int, float),
    ):
        msg = "TUI performance probe did not return numeric measurements"
        raise TypeError(msg)
    _positive(first_render, "first_tui_render_seconds")
    _positive(steady_refresh, "steady_refresh_seconds")
    return float(first_render), float(steady_refresh)


def measure(runs: int) -> dict[str, object]:
    """Execute cold-start, memory, TUI-render, and steady-refresh probes."""
    if runs < 1:
        msg = "performance run count must be positive"
        raise ValueError(msg)
    cold_starts = tuple(
        _run_timed((sys.executable, "-m", "cqmgr", "--help")) for _ in range(runs)
    )
    resident, peak = _memory_measurements()
    first_render, steady_refresh = _tui_measurements()
    return measurement_report(
        cold_start_seconds=cold_starts,
        resident_memory_bytes=resident,
        peak_python_memory_bytes=peak,
        first_tui_render_seconds=first_render,
        steady_refresh_seconds=steady_refresh,
        platform_name=platform.platform(),
        python_version=platform.python_version(),
    )


def main(arguments: Sequence[str] | None = None) -> None:
    """Measure the current executable environment and write canonical JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    parsed.output.write_text(
        json.dumps(
            measure(parsed.runs),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
