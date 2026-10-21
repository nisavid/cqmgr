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
EXPECTED_CALL_COUNT = 2
QUOTA_LIST_INVOCATION = 1
SAFE_CHILD_ENVIRONMENT_KEYS = {
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HOME",
    "PATH",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}


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


def test_cli_only_runs_preinstalled_candidate_without_retaining_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The credentialed CLI only runs exact reads and retains aggregate evidence."""
    main = cast("Any", _module()["main"])
    executable = tmp_path / "bin" / "cqmgr"
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
        assert Path(command[0]) == executable
        return subprocess.CompletedProcess(
            command,
            0,
            _operation_result(
                schema="cqmgr.operation-result/v1",
                exit_class=0,
            ),
            b"private provider diagnostic",
        )

    times = iter((1.0, 1.1, 2.0, 2.2))
    exit_code = main(
        [
            str(executable),
            "--project-env",
            "QUALIFICATION_PROJECT",
            "--output",
            str(output),
        ],
        runner=runner,
        environ={
            "QUALIFICATION_PROJECT": PROJECT,
            "PATH": "/usr/local/bin:/usr/bin",
            "HOME": "/private/home",
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": "/private/credential.json",
            "CLOUDSDK_CORE_PROJECT": "credential-project",
            "CLOUDSDK_PROJECT": "credential-project",
            "GCLOUD_PROJECT": "credential-project",
            "GCP_PROJECT": "credential-project",
            "GOOGLE_APPLICATION_CREDENTIALS": "/private/credential.json",
            "GOOGLE_CLOUD_PROJECT": "credential-project",
            "GOOGLE_GHA_CREDS_PATH": "/private/credential.json",
            "GITHUB_TOKEN": "private-github-token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-oidc-token",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://private-oidc.invalid",
            "UNRELATED_SECRET": "private-unrelated-secret",
        },
        monotonic=lambda: next(times),
    )

    assert exit_code == 0
    assert len(calls) == EXPECTED_CALL_COUNT
    quota_list, quota_resolve = calls
    assert quota_list[0][0] == str(executable)
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
        str(executable),
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
        assert set(child_environment) == SAFE_CHILD_ENVIRONMENT_KEYS
        assert "QUALIFICATION_PROJECT" not in child_environment
        assert child_environment["PATH"] == "/usr/local/bin:/usr/bin"
        assert child_environment["GOOGLE_APPLICATION_CREDENTIALS"] == (
            "/private/credential.json"
        )
        for key in (
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ):
            assert Path(child_environment[key]).is_relative_to(tmp_path)

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert [record["check"] for record in evidence] == [
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
    assert all(record["schema"] == "supported" for record in evidence)
    assert all(record["outcome"] == "successful" for record in evidence)
    retained = output.read_text(encoding="utf-8")
    assert PROJECT not in retained
    assert "private-provider-data" not in retained
    assert "principal@example.invalid" not in retained
    assert "credential" not in retained
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_missing_project_blocks_with_one_sanitized_input_failure(
    tmp_path: Path,
) -> None:
    """Missing qualification input fails before install with attributable evidence."""
    main = cast("Any", _module()["main"])
    executable = tmp_path / "bin" / "cqmgr"
    output = tmp_path / "qualification.json"
    calls: list[list[str]] = []

    def runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    exit_code = main(
        [
            str(executable),
            "--project-env",
            "QUALIFICATION_PROJECT",
            "--output",
            str(output),
        ],
        runner=runner,
        environ={},
    )

    assert exit_code == 1
    assert calls == []
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert len(evidence) == 1
    record = evidence[0]
    assert set(record) == {
        "check",
        "digest",
        "elapsed_ms",
        "exit_code",
        "outcome",
        "schema",
    }
    assert record["check"] == "qualification-input"
    assert record["elapsed_ms"] == 0
    assert record["exit_code"] == 1
    assert record["outcome"] == "unsuccessful"
    assert record["schema"] == "not-applicable"
    _assert_evidence_digest(record)
    assert PROJECT not in output.read_text(encoding="utf-8")


def test_missing_preinstalled_executable_retains_only_sanitized_failures(
    tmp_path: Path,
) -> None:
    """A missing executable fails both reads without trying a fallback install."""
    main = cast("Any", _module()["main"])
    output = tmp_path / "qualification.json"

    exit_code = main(
        [
            str(tmp_path / "missing-cqmgr"),
            "--project-env",
            "QUALIFICATION_PROJECT",
            "--output",
            str(output),
        ],
        environ={"QUALIFICATION_PROJECT": PROJECT},
    )

    assert exit_code == 1
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert [record["check"] for record in evidence] == ["quota-list", "quota-resolve"]
    assert all(record["exit_code"] == 1 for record in evidence)
    assert all(record["outcome"] == "unsuccessful" for record in evidence)
    assert all(record["schema"] == "unsupported" for record in evidence)
    for record in evidence:
        _assert_evidence_digest(record)
    retained = output.read_text(encoding="utf-8")
    assert PROJECT not in retained
    assert "missing-cqmgr" not in retained


def test_schema_or_outcome_failure_blocks_after_retaining_sanitized_evidence(
    tmp_path: Path,
) -> None:
    """Both installed checks run, while either invalid result blocks release."""
    main = cast("Any", _module()["main"])
    executable = tmp_path / "bin" / "cqmgr"
    output = tmp_path / "qualification.json"
    responses = iter(
        (
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

    times = iter((1.0, 1.1, 2.0, 2.1))
    exit_code = main(
        [
            str(executable),
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
    assert evidence[0]["schema"] == "unsupported"
    assert evidence[0]["outcome"] == "successful"
    assert evidence[1]["schema"] == "supported"
    assert evidence[1]["outcome"] == "unsuccessful"
    assert PROJECT not in output.read_text(encoding="utf-8")


def test_child_failures_always_emit_only_sanitized_evidence(
    tmp_path: Path,
) -> None:
    """Timeout and malformed-output details never escape the in-memory boundary."""
    main = cast("Any", _module()["main"])
    executable = tmp_path / "bin" / "cqmgr"
    output = tmp_path / "qualification.json"
    invocation = 0

    def runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal invocation
        del kwargs
        invocation += 1
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

    times = iter((1.0, 1.1, 2.0, 2.1))
    exit_code = main(
        [
            str(executable),
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
    assert [record["exit_code"] for record in evidence] == [124, 0]
    assert [record["schema"] for record in evidence] == [
        "unsupported",
        "unsupported",
    ]
    assert [record["outcome"] for record in evidence] == [
        "unsuccessful",
        "unsuccessful",
    ]
    for record in evidence:
        _assert_evidence_digest(record)
    retained = output.read_text(encoding="utf-8")
    assert PROJECT not in retained
    assert "private" not in retained
    assert "principal@example.invalid" not in retained
