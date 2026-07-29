"""Installed release candidates qualify production read adapters without leakage."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import pytest

SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "installed_live_adapter_qualification.py"
)
PROJECT = "private-qualification-project"
EXPECTED_CALL_COUNT = 3
QUOTA_LIST_INVOCATION = 2


def _module() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def _operation_result(*, schema: str, exit_class: int) -> bytes:
    return json.dumps(
        {
            "schema": schema,
            "outcome": {"code": "qualified", "exit_class": exit_class},
            "data": {
                "provider_response": "private-provider-data",
                "principal": "principal@example.invalid",
            },
        }
    ).encode()


def _assert_evidence_digest(record: dict[str, object]) -> None:
    safe_fields = {key: value for key, value in record.items() if key != "digest"}
    expected = hashlib.sha256(
        (
            json.dumps(
                safe_fields,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
    ).hexdigest()
    assert record["digest"] == f"sha256:{expected}"


def test_cli_installs_and_qualifies_exact_candidate_without_retaining_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI owns installation, exact commands, and aggregate-only evidence."""
    main = cast("Any", _module()["main"])
    wheel = tmp_path / "cqmgr-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"immutable candidate")
    output = tmp_path / "qualification.json"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(
        command: list[str],
        *,
        env: dict[str, str],
        capture_output: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        assert capture_output is True
        assert check is False
        assert timeout > 0
        calls.append((command, env))
        if command[:3] == ["uv", "tool", "install"]:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(
            command,
            0,
            _operation_result(
                schema="cqmgr.operation-result/v1",
                exit_class=0,
            ),
            b"private provider diagnostic",
        )

    times = iter((1.0, 1.1, 2.0, 2.2, 3.0, 3.3))
    exit_code = main(
        [
            str(wheel),
            "--project-env",
            "QUALIFICATION_PROJECT",
            "--output",
            str(output),
        ],
        runner=runner,
        environ={
            "QUALIFICATION_PROJECT": PROJECT,
            "GOOGLE_APPLICATION_CREDENTIALS": "/private/credential.json",
        },
        monotonic=lambda: next(times),
    )

    assert exit_code == 0
    assert len(calls) == EXPECTED_CALL_COUNT
    install, quota_list, quota_resolve = calls
    assert install[0] == [
        "uv",
        "tool",
        "install",
        "--no-build",
        "--python",
        "3.14",
        str(wheel),
    ]
    executable = quota_list[0][0]
    assert executable != "cqmgr"
    assert quota_list[0][1:] == [
        "quota",
        "list",
        "--resource-scope",
        f"projects/{PROJECT}",
        "--service",
        "compute",
        "--limit",
        "20",
        "--output",
        "json",
        "--no-color",
        "--quiet",
    ]
    assert quota_resolve[0] == [
        executable,
        "quota",
        "resolve",
        "compute-instance",
        "--resource-scope",
        f"projects/{PROJECT}",
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
    ]
    for _command, child_environment in calls:
        assert "QUALIFICATION_PROJECT" not in child_environment
        assert child_environment["GOOGLE_APPLICATION_CREDENTIALS"] == (
            "/private/credential.json"
        )
        for key in (
            "UV_TOOL_BIN_DIR",
            "UV_TOOL_DIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ):
            assert child_environment[key].startswith(str(tmp_path.parent))

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert [record["check"] for record in evidence] == [
        "candidate-install",
        "quota-list",
        "quota-resolve",
    ]
    for record in evidence:
        assert set(record) == {
            "check",
            "digest",
            "elapsed_ms",
            "exit_code",
            "outcome",
            "schema",
        }
        _assert_evidence_digest(record)
    assert evidence[0]["schema"] == "not-applicable"
    assert evidence[0]["outcome"] == "successful"
    assert evidence[1]["schema"] == "supported"
    assert evidence[1]["outcome"] == "successful"
    assert evidence[2]["schema"] == "supported"
    assert evidence[2]["outcome"] == "successful"
    retained = output.read_text(encoding="utf-8")
    assert PROJECT not in retained
    assert "private-provider-data" not in retained
    assert "principal@example.invalid" not in retained
    assert "credential" not in retained
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_schema_or_outcome_failure_blocks_after_retaining_sanitized_evidence(
    tmp_path: Path,
) -> None:
    """Both installed checks run, while either invalid result blocks release."""
    main = cast("Any", _module()["main"])
    wheel = tmp_path / "cqmgr-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"immutable candidate")
    output = tmp_path / "qualification.json"
    responses = iter(
        (
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess(
                [],
                0,
                _operation_result(schema="cqmgr.operation-result/v2", exit_class=0),
                b"private failure",
            ),
            subprocess.CompletedProcess(
                [],
                9,
                _operation_result(
                    schema="cqmgr.operation-result/v1",
                    exit_class=9,
                ),
                b"private failure",
            ),
        )
    )

    def runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del command, kwargs
        return next(responses)

    times = iter((1.0, 1.1, 2.0, 2.1, 3.0, 3.1))
    exit_code = main(
        [
            str(wheel),
            "--project-env",
            "QUALIFICATION_PROJECT",
            "--output",
            str(output),
        ],
        runner=runner,
        environ={"QUALIFICATION_PROJECT": PROJECT},
        monotonic=lambda: next(times),
    )

    assert exit_code == 1
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence[1]["schema"] == "unsupported"
    assert evidence[1]["outcome"] == "successful"
    assert evidence[2]["schema"] == "supported"
    assert evidence[2]["outcome"] == "unsuccessful"
    assert PROJECT not in output.read_text(encoding="utf-8")


def test_child_failures_always_emit_only_sanitized_evidence(
    tmp_path: Path,
) -> None:
    """Timeout and malformed-output details never escape the in-memory boundary."""
    main = cast("Any", _module()["main"])
    wheel = tmp_path / "cqmgr-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"immutable candidate")
    output = tmp_path / "qualification.json"
    invocation = 0

    def runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal invocation
        del kwargs
        invocation += 1
        if invocation == 1:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if invocation == QUOTA_LIST_INVOCATION:
            raise subprocess.TimeoutExpired(
                command,
                timeout=1,
                output=b"private timed-out payload",
                stderr=b"principal@example.invalid",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            b"private malformed payload",
            b"private provider diagnostic",
        )

    times = iter((1.0, 1.1, 2.0, 2.1, 3.0, 3.1))
    exit_code = main(
        [
            str(wheel),
            "--project-env",
            "QUALIFICATION_PROJECT",
            "--output",
            str(output),
        ],
        runner=runner,
        environ={"QUALIFICATION_PROJECT": PROJECT},
        monotonic=lambda: next(times),
    )

    assert exit_code == 1
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert [record["exit_code"] for record in evidence] == [0, 124, 0]
    assert [record["schema"] for record in evidence] == [
        "not-applicable",
        "unsupported",
        "unsupported",
    ]
    assert [record["outcome"] for record in evidence] == [
        "successful",
        "unsuccessful",
        "unsuccessful",
    ]
    for record in evidence:
        _assert_evidence_digest(record)
    retained = output.read_text(encoding="utf-8")
    assert PROJECT not in retained
    assert "private" not in retained
    assert "principal@example.invalid" not in retained
