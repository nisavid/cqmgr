"""Qualify installed live-read adapters while retaining only safe evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

EXPECTED_SCHEMA = "cqmgr.operation-result/v1"
INSTALL_TIMEOUT_SECONDS = 300.0
CHECK_TIMEOUT_SECONDS = 600.0
TIMEOUT_EXIT_CODE = 124
FAILED_EXECUTION_EXIT_CODE = 1
EXPECTED_EVIDENCE_COUNT = 3
ALLOWED_PARENT_ENVIRONMENT = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "PATH",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify an installed cqmgr wheel with live read-only operations.",
    )
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--project-env", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _safe_record(
    *,
    check: str,
    exit_code: int,
    elapsed_ms: int,
    schema: str,
    outcome: str,
) -> dict[str, object]:
    record: dict[str, object] = {
        "check": check,
        "elapsed_ms": elapsed_ms,
        "exit_code": exit_code,
        "outcome": outcome,
        "schema": schema,
    }
    canonical = (
        json.dumps(
            record,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    record["digest"] = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return record


def _elapsed_ms(started: float, monotonic: Callable[[], float]) -> int:
    return max(0, round((monotonic() - started) * 1000))


def _classify_operation(
    completed: subprocess.CompletedProcess[bytes],
) -> tuple[str, str]:
    schema = "unsupported"
    successful_outcome = False
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        if payload.get("schema") == EXPECTED_SCHEMA:
            schema = "supported"
        outcome = payload.get("outcome")
        if isinstance(outcome, dict):
            exit_class = outcome.get("exit_class")
            successful_outcome = (
                isinstance(exit_class, int)
                and not isinstance(exit_class, bool)
                and exit_class == 0
            )
    outcome_classification = (
        "successful"
        if completed.returncode == 0 and successful_outcome
        else "unsuccessful"
    )
    return schema, outcome_classification


def _run(
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
    command: list[str],
    *,
    environment: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    return runner(
        command,
        env=environment,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _execute_check(
    *,
    label: str,
    command: list[str],
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
    environment: dict[str, str],
    monotonic: Callable[[], float],
) -> dict[str, object]:
    started = monotonic()
    try:
        completed = _run(
            runner,
            command,
            environment=environment,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _safe_record(
            check=label,
            exit_code=TIMEOUT_EXIT_CODE,
            elapsed_ms=_elapsed_ms(started, monotonic),
            schema="unsupported",
            outcome="unsuccessful",
        )
    except OSError:
        return _safe_record(
            check=label,
            exit_code=FAILED_EXECUTION_EXIT_CODE,
            elapsed_ms=_elapsed_ms(started, monotonic),
            schema="unsupported",
            outcome="unsuccessful",
        )
    schema, outcome = _classify_operation(completed)
    return _safe_record(
        check=label,
        exit_code=completed.returncode,
        elapsed_ms=_elapsed_ms(started, monotonic),
        schema=schema,
        outcome=outcome,
    )


def _write_evidence(path: Path, evidence: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            evidence,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    environ: Mapping[str, str] = os.environ,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Install one wheel and qualify its public live-read commands."""
    arguments = _parser().parse_args(argv)
    project_id = environ.get(arguments.project_env)
    if not project_id:
        _write_evidence(
            arguments.output,
            [
                _safe_record(
                    check="qualification-input",
                    exit_code=FAILED_EXECUTION_EXIT_CODE,
                    elapsed_ms=0,
                    schema="not-applicable",
                    outcome="unsuccessful",
                ),
            ],
        )
        return FAILED_EXECUTION_EXIT_CODE

    evidence: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(
        prefix="cqmgr-live-adapter-qualification-",
        dir=arguments.output.parent,
    ) as temporary:
        root = Path(temporary)
        tool_bin = root / "bin"
        child_environment = {
            key: value
            for key in ALLOWED_PARENT_ENVIRONMENT
            if (value := environ.get(key)) is not None
        }
        child_environment.pop(arguments.project_env, None)
        child_environment.update(
            {
                "HOME": str(root / "home"),
                "UV_TOOL_BIN_DIR": str(tool_bin),
                "UV_TOOL_DIR": str(root / "tools"),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "XDG_DATA_HOME": str(root / "xdg-data"),
                "XDG_STATE_HOME": str(root / "xdg-state"),
            }
        )

        install_started = monotonic()
        try:
            installed = _run(
                runner,
                [
                    "uv",
                    "tool",
                    "install",
                    "--no-build",
                    "--python",
                    "3.14",
                    str(arguments.wheel),
                ],
                environment=child_environment,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
            install_exit_code = installed.returncode
        except subprocess.TimeoutExpired:
            install_exit_code = TIMEOUT_EXIT_CODE
        except OSError:
            install_exit_code = FAILED_EXECUTION_EXIT_CODE
        evidence.append(
            _safe_record(
                check="candidate-install",
                exit_code=install_exit_code,
                elapsed_ms=_elapsed_ms(install_started, monotonic),
                schema="not-applicable",
                outcome=("successful" if install_exit_code == 0 else "unsuccessful"),
            )
        )

        if install_exit_code == 0:
            executable = str(tool_bin / "cqmgr")
            resource_scope = f"projects/{project_id}"
            commands = (
                (
                    "quota-list",
                    [
                        executable,
                        "quota",
                        "list",
                        "--resource-scope",
                        resource_scope,
                        "--service",
                        "compute",
                        "--limit",
                        "20",
                        "--output",
                        "json",
                        "--no-color",
                        "--quiet",
                    ],
                ),
                (
                    "quota-resolve",
                    [
                        executable,
                        "quota",
                        "resolve",
                        "compute-instance",
                        "--resource-scope",
                        resource_scope,
                        "--machine-type",
                        "a3-highgpu-8g",
                        "--instance-count",
                        "1",
                        "--provisioning-model",
                        "standard",
                        "--candidate",
                        "us-central1-a",
                        "--output",
                        "json",
                        "--no-color",
                        "--quiet",
                    ],
                ),
            )
            for label, command in commands:
                evidence.append(
                    _execute_check(
                        label=label,
                        command=command,
                        runner=runner,
                        environment=child_environment,
                        monotonic=monotonic,
                    )
                )

    _write_evidence(arguments.output, evidence)
    qualified = (
        len(evidence) == EXPECTED_EVIDENCE_COUNT
        and all(record["outcome"] == "successful" for record in evidence)
        and all(
            record["schema"] in {"not-applicable", "supported"} for record in evidence
        )
    )
    return 0 if qualified else FAILED_EXECUTION_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
