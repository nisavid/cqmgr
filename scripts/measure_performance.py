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
FIRST_TUI_CONTRACT = (
    "tests/adapters/tui/test_app.py::"
    "test_wide_shell_opens_federated_quota_inspector_with_semantic_evidence"
)
STEADY_REFRESH_CONTRACT = (
    "tests/adapters/tui/test_app.py::"
    "test_completed_inspection_owns_result_and_copy_cli_over_older_refresh"
)


def _positive(value: float, name: str) -> None:
    if value <= 0:
        msg = f"{name} must contain positive executable evidence"
        raise ValueError(msg)


def measurement_report(  # noqa: PLR0913 - stable report input axes
    *,
    cold_start_seconds: tuple[float, ...],
    resident_memory_bytes: int,
    peak_python_memory_bytes: int,
    first_tui_render_contract_seconds: float,
    steady_refresh_contract_seconds: float,
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
            first_tui_render_contract_seconds,
            "first_tui_render_contract_seconds",
        ),
        (steady_refresh_contract_seconds, "steady_refresh_contract_seconds"),
    ):
        _positive(value, name)
    return {
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
            "first_tui_render_contract_seconds": (first_tui_render_contract_seconds),
            "peak_python_memory_bytes": peak_python_memory_bytes,
            "resident_memory_bytes": resident_memory_bytes,
            "steady_refresh_contract_seconds": steady_refresh_contract_seconds,
        },
        "schema": BASELINE_SCHEMA,
    }


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


def measure(runs: int) -> dict[str, object]:
    """Execute cold-start, memory, TUI-render, and steady-refresh probes."""
    if runs < 1:
        msg = "performance run count must be positive"
        raise ValueError(msg)
    cold_starts = tuple(
        _run_timed((sys.executable, "-m", "cqmgr", "--help")) for _ in range(runs)
    )
    resident, peak = _memory_measurements()
    first_render = _run_timed(
        (
            sys.executable,
            "-m",
            "pytest",
            "--no-cov",
            "-q",
            FIRST_TUI_CONTRACT,
        )
    )
    steady_refresh = _run_timed(
        (
            sys.executable,
            "-m",
            "pytest",
            "--no-cov",
            "-q",
            STEADY_REFRESH_CONTRACT,
        )
    )
    return measurement_report(
        cold_start_seconds=cold_starts,
        resident_memory_bytes=resident,
        peak_python_memory_bytes=peak,
        first_tui_render_contract_seconds=first_render,
        steady_refresh_contract_seconds=steady_refresh,
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
