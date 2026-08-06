"""Credential-free replay of a sanitized Compute qualification snapshot."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import runpy
import sys
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
from google.cloud import compute_v1

from cqmgr.adapters.google import compute_catalog

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping

SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "replay_compute_qualification_snapshot.py"
)
HEAD_SHA = "a" * 40
WORKFLOW_SHA = "b" * 40
WORKFLOW_REF = (
    "nisavid/cqmgr/.github/workflows/trusted-live-read-only.yml@" + WORKFLOW_SHA
)
EXPECTED_ELAPSED_SECONDS = 0.25
CHILD_CHALLENGE_BYTES = 32
CHILD_RESPONSE_BYTES = hashlib.sha256().digest_size
CHILD_TIMEOUT_SECONDS = 60
EXPECTED_WRAPPER_WORKERS = 2


class _WaitableProcess(Protocol):
    pid: int

    def wait(self, timeout: float | None = None) -> int:
        raise NotImplementedError


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _snapshot() -> dict[str, object]:
    identity = {
        "collected_at": "2026-08-06T00:30:00Z",
        "head_sha": HEAD_SHA,
        "pull_request": "107",
        "repository": "nisavid/cqmgr",
        "run_attempt": "2",
        "run_id": "123456789",
        "workflow_ref": WORKFLOW_REF,
        "workflow_sha": WORKFLOW_SHA,
    }
    profile: dict[str, object] = {
        "max_results": 50,
        "operations": [
            "compute.machineTypes.list",
            "compute.acceleratorTypes.list",
        ],
        "project_alias": "protected-project",
        "timeout_seconds": 10,
        "zone": "us-central1-a",
    }
    machine: dict[str, object] = {
        "guest_accelerator_count": 8,
        "guest_accelerator_type": "nvidia-h100-80gb",
        "name": "a3-highgpu-8g",
        "zone": "us-central1-a",
    }
    accelerator = {
        "name": "nvidia-h100-80gb",
        "zone": "us-central1-a",
    }
    digests = {
        "accelerator": _digest(accelerator),
        "identity": _digest(identity),
        "machine": _digest(machine),
    }
    snapshot: dict[str, object] = {
        "accelerator": accelerator,
        "complete": True,
        "digests": digests,
        "identity": identity,
        "machine": machine,
        "profile": profile,
        "schema": "cqmgr.live-compute-exact-zone-snapshot/v1",
    }
    digests["snapshot"] = _digest(snapshot)
    return snapshot


def _main() -> Callable[..., int]:
    script_globals = _script_globals()
    main = cast("Callable[..., int]", script_globals["main"])
    expected_identity = cast(
        "Callable[[argparse.Namespace], Mapping[str, str]]",
        script_globals["_expected_identity"],
    )
    validate_snapshot = cast(
        "Callable[..., tuple[Mapping[str, object], str]]",
        script_globals["_validate_snapshot"],
    )
    exercise_wrappers = cast(
        "Callable[[Mapping[str, object]], Any]",
        script_globals["_exercise_wrappers"],
    )

    def invoke(argv: list[str], **options: object) -> int:
        if "candidate_runner" not in options:

            def in_process_candidate(arguments: argparse.Namespace) -> None:
                snapshot, _ = validate_snapshot(
                    arguments.snapshot,
                    expected_identity=expected_identity(arguments),
                )
                asyncio.run(exercise_wrappers(snapshot))

            options["candidate_runner"] = in_process_candidate
        return main(argv, **options)

    return invoke


def _script_globals() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT))


def _arguments(snapshot: Path, output: Path) -> list[str]:
    return [
        "--snapshot",
        str(snapshot),
        "--repository",
        "nisavid/cqmgr",
        "--pull-request",
        "107",
        "--head-sha",
        HEAD_SHA,
        "--workflow-sha",
        WORKFLOW_SHA,
        "--run-id",
        "123456789",
        "--run-attempt",
        "2",
        "--output",
        str(output),
    ]


def _child_arguments(snapshot: Path, output: Path) -> list[str]:
    return [
        *_arguments(snapshot, output),
        "--candidate-site-packages",
        str(snapshot.parent / "candidate-env" / "lib" / "python3.14" / "site-packages"),
        "--candidate-home",
        str(snapshot.parent / "candidate-home"),
    ]


def test_replay_runs_candidate_in_an_isolated_bounded_child(
    tmp_path: Path,
) -> None:
    """Trusted evidence writing never shares the candidate UID or interpreter."""
    candidate_site = tmp_path / "candidate-env" / "lib" / "python3.14" / "site-packages"
    candidate_site.mkdir(parents=True)
    (tmp_path / "candidate-home").mkdir()
    script_globals = _script_globals()
    parser = cast("Callable[[], Any]", script_globals["_parser"])()
    arguments = parser.parse_args(
        _child_arguments(tmp_path / "snapshot.json", tmp_path / "evidence.json")
    )
    captured: dict[str, object] = {}
    challenge = b"c" * CHILD_CHALLENGE_BYTES
    child_response = cast("Callable[[bytes], bytes]", script_globals["_child_response"])

    def supervised(
        command: list[str],
        *,
        input_bytes: bytes,
        output_limit: int,
        timeout_seconds: int,
    ) -> bytes:
        captured["command"] = command
        captured["input_bytes"] = input_bytes
        captured["output_limit"] = output_limit
        captured["timeout_seconds"] = timeout_seconds
        return child_response(input_bytes)

    def cleaned() -> None:
        captured["cleaned"] = True

    cast("Callable[..., None]", script_globals["_run_candidate"])(
        arguments,
        supervisor=supervised,
        challenge_factory=lambda _size: challenge,
        candidate_cleanup=cleaned,
    )

    command = cast("list[str]", captured["command"])
    assert command[:7] == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "cqmgr-replay",
        "--",
        "/usr/bin/env",
        "-i",
    ]
    trusted_python = str(Path(sys.executable).resolve())
    assert command.index(trusted_python) > command.index("PYTHONNOUSERSITE=1")
    assert command[command.index(trusted_python) + 1 :][:2] == ["-I", "-S"]
    assert "--candidate-site-packages" in command
    assert not any(
        argument.endswith("candidate-env/bin/python") for argument in command
    )
    assert "--child-only" in command
    assert "--output" not in command
    assert captured["input_bytes"] == challenge
    assert captured["output_limit"] == CHILD_RESPONSE_BYTES
    assert captured["timeout_seconds"] == CHILD_TIMEOUT_SECONDS
    assert captured["cleaned"] is True


@pytest.mark.parametrize(
    "forged_response",
    [b"qualified\n", hashlib.sha256(b"public-static-record").digest()],
)
def test_replay_rejects_forged_or_replayed_child_success(
    tmp_path: Path,
    forged_response: bytes,
) -> None:
    """Exit zero and a public or stale record cannot qualify candidate code."""
    candidate_site = tmp_path / "candidate-env" / "lib" / "python3.14" / "site-packages"
    candidate_site.mkdir(parents=True)
    (tmp_path / "candidate-home").mkdir()
    script_globals = _script_globals()
    parser = cast("Callable[[], Any]", script_globals["_parser"])()
    arguments = parser.parse_args(
        _child_arguments(tmp_path / "snapshot.json", tmp_path / "evidence.json")
    )
    replay_error = cast("type[Exception]", script_globals["_ReplayError"])

    def forge(_command: list[str], **_options: object) -> bytes:
        return forged_response

    with pytest.raises(replay_error):
        cast("Callable[..., None]", script_globals["_run_candidate"])(
            arguments,
            supervisor=forge,
            challenge_factory=lambda _size: b"c" * CHILD_CHALLENGE_BYTES,
            candidate_cleanup=lambda: None,
        )


def test_candidate_uid_cleanup_kills_and_verifies_escaped_sessions() -> None:
    """UID-wide cleanup catches descendants even after they call setsid()."""
    script_globals = _script_globals()
    commands: list[list[str]] = []
    return_codes = iter((0, 0, 0, 1))

    def run(command: list[str], **_options: object) -> object:
        commands.append(command)
        return SimpleNamespace(returncode=next(return_codes))

    cast("Callable[..., None]", script_globals["_reap_candidate_uid"])(runner=run)

    assert commands == [
        [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/pkill",
            "-KILL",
            "-u",
            "cqmgr-replay",
            "--",
            ".*",
        ],
        ["/usr/bin/pgrep", "-u", "cqmgr-replay", "--", ".*"],
        [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/pkill",
            "-KILL",
            "-u",
            "cqmgr-replay",
            "--",
            ".*",
        ],
        ["/usr/bin/pgrep", "-u", "cqmgr-replay", "--", ".*"],
    ]


def test_candidate_uid_cleanup_fails_if_any_process_survives() -> None:
    """Qualification cannot pass while its dedicated UID still owns work."""
    script_globals = _script_globals()
    replay_error = cast("type[Exception]", script_globals["_ReplayError"])

    def run(_command: list[str], **_options: object) -> object:
        return SimpleNamespace(returncode=0)

    with pytest.raises(replay_error):
        cast("Callable[..., None]", script_globals["_reap_candidate_uid"])(runner=run)


def test_process_group_termination_uses_privileged_exact_group() -> None:
    """The trusted runner can terminate every UID in the candidate group."""
    script_globals = _script_globals()
    commands: list[list[str]] = []
    waits: list[float] = []
    process = SimpleNamespace(
        pid=4242,
        wait=lambda *, timeout: waits.append(timeout),
    )

    def run(command: list[str], **_options: object) -> object:
        commands.append(command)
        return SimpleNamespace(returncode=0)

    cast("Callable[..., None]", script_globals["_terminate_process_group"])(
        cast("_WaitableProcess", process),
        runner=run,
    )

    assert commands == [
        [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/pkill",
            "-KILL",
            "-g",
            "4242",
            "--",
            ".*",
        ]
    ]
    assert waits == [5]


def test_bounded_child_supervisor_rejects_timeout_and_kills_process_group() -> None:
    """A candidate cannot retain the trusted controller or its process group."""
    script_globals = _script_globals()
    os_module = cast("Any", script_globals["os"])
    original_killpg = os_module.killpg
    killed_groups: list[int] = []

    def kill_group(process_group: int, signal_number: int) -> None:
        killed_groups.append(process_group)
        original_killpg(process_group, signal_number)

    def terminate(process: _WaitableProcess) -> None:
        kill_group(process.pid, 9)
        process.wait(timeout=5)

    replay_error = cast("type[Exception]", script_globals["_ReplayError"])
    supervise = cast("Callable[..., bytes]", script_globals["_supervise_process"])

    with pytest.raises(replay_error):
        supervise(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            input_bytes=b"x",
            output_limit=1,
            timeout_seconds=0.05,
            group_terminator=terminate,
        )

    assert killed_groups


def test_bounded_child_supervisor_cleans_descendants_after_normal_exit() -> None:
    """A successful direct child cannot leave work alive during uploads."""
    script_globals = _script_globals()
    os_module = cast("Any", script_globals["os"])
    original_killpg = os_module.killpg
    killed_groups: list[int] = []

    def kill_group(process_group: int, signal_number: int) -> None:
        killed_groups.append(process_group)
        original_killpg(process_group, signal_number)

    def terminate(process: _WaitableProcess) -> None:
        with contextlib.suppress(ProcessLookupError):
            kill_group(process.pid, 9)
        process.wait(timeout=5)

    supervise = cast("Callable[..., bytes]", script_globals["_supervise_process"])
    child = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "sys.stdout.buffer.write(b'x')"
    )

    output = supervise(
        [sys.executable, "-c", child],
        input_bytes=b"",
        output_limit=1,
        timeout_seconds=5,
        group_terminator=terminate,
    )

    assert output == b"x"
    assert killed_groups


def test_replay_classifies_empty_machine_scope_as_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing exact-zone scope fails through the replay contract."""

    class EmptyMachineWrapper:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def machine_types_for_zone(self, **_kwargs: object) -> object:
            return SimpleNamespace(scopes=(), next_page_token="")

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        compute_catalog,
        "OfficialComputeMachineTypesPageClient",
        EmptyMachineWrapper,
    )
    script_globals = _script_globals()
    exercise_wrappers = cast(
        "Callable[[Mapping[str, object]], Any]",
        script_globals["_exercise_wrappers"],
    )
    replay_error = cast("type[Exception]", script_globals["_ReplayError"])

    with pytest.raises(replay_error):
        asyncio.run(exercise_wrappers(_snapshot()))


def test_replay_waits_for_deferred_transport_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivered page can precede its worker's transport-close bookkeeping."""
    workers_type = compute_catalog._BoundedDaemonWorkers  # noqa: SLF001
    finish_worker = workers_type._finish_worker  # noqa: SLF001
    finish_lock = Lock()
    finishes_started = 0
    both_finishes_started = Event()
    release_finishes = Event()
    replay_completed = Event()
    failures: list[BaseException] = []

    def delayed_finish(worker: object) -> None:
        nonlocal finishes_started
        with finish_lock:
            finishes_started += 1
            if finishes_started == EXPECTED_WRAPPER_WORKERS:
                both_finishes_started.set()
        release_finishes.wait(5)
        finish_worker(cast("Any", worker))

    monkeypatch.setattr(workers_type, "_finish_worker", delayed_finish)
    exercise_wrappers = cast(
        "Callable[[Mapping[str, object]], Any]",
        _script_globals()["_exercise_wrappers"],
    )

    def replay() -> None:
        try:
            asyncio.run(exercise_wrappers(_snapshot()))
        except BaseException as error:  # noqa: BLE001 - asserted by the test thread
            failures.append(error)
        finally:
            replay_completed.set()

    replay_thread = Thread(target=replay)
    replay_thread.start()
    both_started = both_finishes_started.wait(5)
    completed_before_release = replay_completed.wait(0.05)
    release_finishes.set()
    replay_thread.join(5)

    assert both_started is True
    assert completed_before_release is False
    assert replay_thread.is_alive() is False
    assert failures == []


def test_replay_exercises_installed_exact_zone_wrappers_and_writes_pass(
    tmp_path: Path,
) -> None:
    """A strict snapshot drives only the reviewed exact-zone wrapper requests."""
    snapshot_path = tmp_path / "snapshot.json"
    output = tmp_path / "evidence.json"
    snapshot_path.write_bytes(_canonical_bytes(_snapshot()) + b"\n")

    times = iter((100.0, 100.25))
    exit_code = _main()(
        _arguments(snapshot_path, output),
        environ={"GCP_PROJECT_ID": ""},
        monotonic=times.__next__,
    )

    assert exit_code == 0
    document = output.read_text(encoding="utf-8")
    evidence = cast("Mapping[str, Any]", json.loads(document))
    assert document == _canonical_bytes(evidence).decode() + "\n"
    assert evidence["schema"] == "cqmgr.qualification-evidence/v1"
    assert evidence["command_class"] == "exact-wheel-snapshot-replay"
    assert evidence["outcome_class"] == "passed"
    assert evidence["exit_status"] == 0
    assert evidence["elapsed_seconds"] == EXPECTED_ELAPSED_SECONDS
    assert evidence["identity"] == {
        "head_sha": HEAD_SHA,
        "pull_request": "107",
        "repository": "nisavid/cqmgr",
        "run_attempt": "2",
        "run_id": "123456789",
        "workflow_sha": WORKFLOW_SHA,
    }
    assert evidence["provider"] == {
        "codec": "cqmgr.google.compute-exact-zone-replay/v1",
        "name": "google",
        "checks": {
            "accelerator_exact_zone_wrapper": "passed",
            "machine_exact_zone_wrapper": "passed",
            "snapshot": "passed",
        },
    }
    assert (
        evidence["snapshot_digest"]
        == cast("Mapping[str, str]", _snapshot()["digests"])["snapshot"]
    )
    assert cast("str", evidence["digest"]).startswith("sha256:")
    assert str(tmp_path) not in document
    assert "https://" not in document


def test_replay_can_verify_snapshot_before_candidate_install(tmp_path: Path) -> None:
    """Trusted code validates exact snapshot bytes before installing the wheel."""
    snapshot_path = tmp_path / "snapshot.json"
    output = tmp_path / "verification.json"
    snapshot_path.write_bytes(_canonical_bytes(_snapshot()) + b"\n")

    exit_code = _main()(
        [*_arguments(snapshot_path, output), "--verify-only"],
        environ={},
    )

    assert exit_code == 0
    evidence = cast("Mapping[str, Any]", json.loads(output.read_bytes()))
    assert evidence["command_class"] == "snapshot-verification"
    assert evidence["outcome_class"] == "verified"
    assert evidence["exit_status"] == 0
    assert cast("Mapping[str, object]", evidence["provider"])["checks"] == {
        "accelerator_exact_zone_wrapper": "not-run",
        "machine_exact_zone_wrapper": "not-run",
        "snapshot": "passed",
    }


def test_replay_turns_candidate_system_exit_zero_into_failure(tmp_path: Path) -> None:
    """Candidate import cannot bypass the trusted result with a clean exit."""
    snapshot_path = tmp_path / "snapshot.json"
    output = tmp_path / "evidence.json"
    snapshot_path.write_bytes(_canonical_bytes(_snapshot()) + b"\n")

    def exit_cleanly(_arguments: object) -> None:
        raise SystemExit(0)

    exit_code = _main()(
        _arguments(snapshot_path, output),
        candidate_runner=exit_cleanly,
        environ={},
    )

    assert exit_code == 1
    evidence = cast("Mapping[str, Any]", json.loads(output.read_bytes()))
    assert evidence["outcome_class"] == "failed"
    assert evidence["exit_status"] == 1
    assert evidence["failure"] == "candidate-replay"


def test_candidate_child_turns_system_exit_zero_into_failure(
    tmp_path: Path,
) -> None:
    """The isolated child emits no challenge response after candidate SystemExit."""
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_bytes(_canonical_bytes(_snapshot()) + b"\n")
    candidate_site = tmp_path / "candidate-env" / "lib" / "python3.14" / "site-packages"
    candidate_site.mkdir(parents=True)
    (tmp_path / "candidate-home").mkdir()
    script_globals = _script_globals()
    parser = cast("Callable[[], Any]", script_globals["_parser"])()
    arguments = parser.parse_args(
        [*_child_arguments(snapshot_path, tmp_path / "unused.json"), "--child-only"]
    )

    async def exit_cleanly(_snapshot: Mapping[str, object]) -> None:
        raise SystemExit(0)

    child_main = cast("Callable[..., int]", script_globals["_child_main"])
    child_main.__globals__["_exercise_wrappers"] = exit_cleanly
    responses: list[bytes] = []

    exit_code = child_main(
        arguments,
        environ={},
        challenge_reader=lambda: b"c" * CHILD_CHALLENGE_BYTES,
        response_writer=responses.append,
    )

    assert exit_code == 1
    assert responses == []


def test_replay_persists_then_reraises_base_exception_control_flow(
    tmp_path: Path,
) -> None:
    """Trusted cancellation evidence does not convert control flow to a result."""
    snapshot_path = tmp_path / "snapshot.json"
    output = tmp_path / "evidence.json"
    snapshot_path.write_bytes(_canonical_bytes(_snapshot()) + b"\n")

    def interrupt(_arguments: object) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _main()(
            _arguments(snapshot_path, output),
            candidate_runner=interrupt,
            environ={},
        )

    evidence = cast("Mapping[str, Any]", json.loads(output.read_bytes()))
    assert evidence["outcome_class"] == "failed"
    assert evidence["exit_status"] == 1
    assert evidence["failure"] == "candidate-replay"


@pytest.mark.parametrize(
    ("field", "value"),
    [("order_by", "name"), ("return_partial_success", True)],
)
def test_replay_fake_rejects_every_noncanonical_generated_request_field(
    field: str,
    value: object,
) -> None:
    """A wrapper cannot change generated-request semantics outside the profile."""
    script_globals = _script_globals()
    generated_client = cast("Any", script_globals["_GeneratedClient"])(
        page=object(),
        request_type=compute_v1.ListMachineTypesRequest,
    )
    replay_error = cast("type[Exception]", script_globals["_ReplayError"])
    request = compute_v1.ListMachineTypesRequest(
        max_results=50,
        page_token="",
        project="protected-project",
        zone="us-central1-a",
        **{field: value},
    )

    with pytest.raises(replay_error):
        generated_client.list(request=request, retry=None, timeout=10)


def test_replay_rejects_candidate_that_drops_live_machine_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact-wheel replay preserves the live H100 count and type relationship."""
    snapshot_path = tmp_path / "snapshot.json"
    output = tmp_path / "evidence.json"
    snapshot_path.write_bytes(_canonical_bytes(_snapshot()) + b"\n")
    original_machine_type = compute_v1.MachineType

    def machine_type_without_accelerators(**values: object) -> object:
        values["accelerators"] = ()
        return original_machine_type(**values)

    monkeypatch.setattr(compute_v1, "MachineType", machine_type_without_accelerators)

    exit_code = _main()(_arguments(snapshot_path, output), environ={})

    assert exit_code == 1
    evidence = cast("Mapping[str, Any]", json.loads(output.read_bytes()))
    assert evidence["failure"] == "candidate-replay"


@pytest.mark.parametrize(
    "environment",
    [
        {"ACTIONS_ID_TOKEN_REQUEST_TOKEN": "sensitive-credential-value"},
        {"REGISTRY_PASSWORD": "sensitive-credential-value"},
    ],
)
def test_replay_rejects_credentials_before_reading_the_snapshot(
    tmp_path: Path,
    environment: Mapping[str, str],
) -> None:
    """Cloud or OIDC capability fails closed without retaining its value."""
    missing_snapshot = tmp_path / "must-not-be-read.json"
    output = tmp_path / "evidence.json"
    sensitive_value = next(iter(environment.values()))

    exit_code = _main()(
        _arguments(missing_snapshot, output),
        environ=environment,
    )

    assert exit_code == 1
    document = output.read_text(encoding="utf-8")
    evidence = cast("Mapping[str, Any]", json.loads(document))
    assert document == _canonical_bytes(evidence).decode() + "\n"
    assert evidence["outcome_class"] == "failed"
    assert evidence["exit_status"] == 1
    assert evidence["failure"] == "credential-environment"
    assert cast("Mapping[str, object]", evidence["provider"])["checks"] == {
        "accelerator_exact_zone_wrapper": "not-run",
        "machine_exact_zone_wrapper": "not-run",
        "snapshot": "not-run",
    }
    assert evidence["snapshot_digest"] == "unavailable"
    assert sensitive_value not in document
    assert str(tmp_path) not in document


def test_replay_classifies_invalid_caller_identity_before_snapshot(
    tmp_path: Path,
) -> None:
    """Invalid trusted arguments are distinct from retained snapshot defects."""
    missing_snapshot = tmp_path / "must-not-be-read.json"
    output = tmp_path / "evidence.json"
    arguments = _arguments(missing_snapshot, output)
    arguments[arguments.index("--head-sha") + 1] = "not-a-commit"

    exit_code = _main()(arguments, environ={})

    assert exit_code == 1
    evidence = cast("Mapping[str, Any]", json.loads(output.read_bytes()))
    assert evidence["failure"] == "identity-invalid"
    assert cast("Mapping[str, object]", evidence["provider"])["checks"] == {
        "accelerator_exact_zone_wrapper": "not-run",
        "machine_exact_zone_wrapper": "not-run",
        "snapshot": "not-run",
    }


def test_replay_rejects_a_well_digested_but_invalid_collection_time(
    tmp_path: Path,
) -> None:
    """Digest integrity cannot make a malformed identity binding trusted."""
    snapshot = _snapshot()
    identity = cast("dict[str, object]", snapshot["identity"])
    digests = cast("dict[str, str]", snapshot["digests"])
    identity["collected_at"] = "2026-99-99T99:99:99Z"
    digests["identity"] = _digest(identity)
    unsigned = dict(snapshot)
    unsigned["digests"] = {
        "accelerator": digests["accelerator"],
        "identity": digests["identity"],
        "machine": digests["machine"],
    }
    digests["snapshot"] = _digest(unsigned)
    snapshot_path = tmp_path / "snapshot.json"
    output = tmp_path / "evidence.json"
    snapshot_path.write_bytes(_canonical_bytes(snapshot) + b"\n")

    exit_code = _main()(_arguments(snapshot_path, output), environ={})

    assert exit_code == 1
    document = output.read_text(encoding="utf-8")
    evidence = cast("Mapping[str, Any]", json.loads(document))
    assert evidence["failure"] == "snapshot-invalid"
    assert cast("Mapping[str, object]", evidence["provider"])["checks"] == {
        "accelerator_exact_zone_wrapper": "not-run",
        "machine_exact_zone_wrapper": "not-run",
        "snapshot": "failed",
    }
    assert "99:99" not in document


def test_replay_rejects_snapshot_digest_tampering(tmp_path: Path) -> None:
    """A changed retained record cannot reach the installed candidate wrappers."""
    snapshot = _snapshot()
    machine = cast("dict[str, object]", snapshot["machine"])
    machine["guest_accelerator_count"] = 1
    snapshot_path = tmp_path / "snapshot.json"
    output = tmp_path / "evidence.json"
    snapshot_path.write_bytes(_canonical_bytes(snapshot) + b"\n")

    exit_code = _main()(_arguments(snapshot_path, output), environ={})

    assert exit_code == 1
    document = output.read_text(encoding="utf-8")
    evidence = cast("Mapping[str, Any]", json.loads(document))
    assert evidence["failure"] == "snapshot-invalid"
    assert cast("Mapping[str, object]", evidence["provider"])["checks"] == {
        "accelerator_exact_zone_wrapper": "not-run",
        "machine_exact_zone_wrapper": "not-run",
        "snapshot": "failed",
    }
    assert '"guest_accelerator_count"' not in document
