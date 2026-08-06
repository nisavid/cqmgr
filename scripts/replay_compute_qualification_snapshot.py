"""Replay one sanitized Compute snapshot through the installed candidate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import selectors
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

SNAPSHOT_SCHEMA = "cqmgr.live-compute-exact-zone-snapshot/v1"
EVIDENCE_SCHEMA = "cqmgr.qualification-evidence/v1"
PROVIDER_CODEC = "cqmgr.google.compute-exact-zone-replay/v1"
CANONICAL_REPOSITORY = "nisavid/cqmgr"
PROJECT_ALIAS = "protected-project"
ZONE = "us-central1-a"
MACHINE_NAME = "a3-highgpu-8g"
ACCELERATOR_NAME = "nvidia-h100-80gb"
ACCELERATOR_COUNT = 8
MAX_RESULTS = 50
TIMEOUT_SECONDS = 10
CHILD_TIMEOUT_SECONDS = 60
CHILD_CHALLENGE_BYTES = 32
CHILD_RESPONSE_BYTES = hashlib.sha256().digest_size
CHILD_RESPONSE_DOMAIN = b"cqmgr-candidate-replay/v1\0"
PROCESS_REAP_SECONDS = 5
CANDIDATE_USER = "cqmgr-replay"
UID_REAP_ATTEMPTS = 5
OPERATIONS = (
    "compute.machineTypes.list",
    "compute.acceleratorTypes.list",
)

_PR_PATTERN = re.compile(r"[1-9][0-9]*\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
_FORBIDDEN_ENVIRONMENT_PREFIXES = (
    "ACTIONS_ID_TOKEN_",
    "ARM_",
    "AWS_",
    "AZURE_",
    "CLOUDSDK_",
    "GCLOUD_",
    "GCP_",
    "GOOGLE_",
)
_FORBIDDEN_ENVIRONMENT_SUFFIXES = (
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_PASSWORD",
    "_PRIVATE_KEY",
    "_SECRET",
    "_TOKEN",
)


class _SnapshotError(Exception):
    """The snapshot or its trusted binding is invalid."""


class _ReplayError(Exception):
    """The candidate wrapper did not preserve the replay contract."""


class _Transport:
    def __init__(self) -> None:
        self._closed = Event()

    def close(self) -> None:
        self._closed.set()

    def wait_closed(self, timeout_seconds: float) -> bool:
        """Wait a bounded interval for worker-owned close bookkeeping."""
        return self._closed.wait(timeout_seconds)


class _Pager:
    def __init__(self, page: object) -> None:
        self.pages: Iterator[object] = iter((page,))


class _GeneratedClient:
    def __init__(self, *, page: object, request_type: type[object]) -> None:
        self._page = page
        self._request_type = request_type
        self.transport = _Transport()
        self.called = False

    def list(
        self,
        *,
        request: object,
        retry: object,
        timeout: object,
    ) -> _Pager:
        if self.called or not isinstance(request, self._request_type):
            raise _ReplayError
        if (
            getattr(request, "project", None) != PROJECT_ALIAS
            or getattr(request, "zone", None) != ZONE
            or getattr(request, "max_results", None) != MAX_RESULTS
            or getattr(request, "page_token", None) != ""
            or getattr(request, "filter", None) != ""
            or getattr(request, "order_by", None) != ""
            or getattr(request, "return_partial_success", None) is not False
            or retry is not None
            or timeout != TIMEOUT_SECONDS
        ):
            raise _ReplayError
        self.called = True
        return _Pager(self._page)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the fixed Compute snapshot with the installed cqmgr.",
    )
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--candidate-site-packages", type=Path)
    parser.add_argument("--candidate-home", type=Path)
    parser.add_argument("--child-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verify-only", action="store_true")
    return parser


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _expected_identity(arguments: argparse.Namespace) -> dict[str, str]:
    values = {
        "head_sha": cast("str", arguments.head_sha),
        "pull_request": cast("str", arguments.pull_request),
        "repository": cast("str", arguments.repository),
        "run_attempt": cast("str", arguments.run_attempt),
        "run_id": cast("str", arguments.run_id),
        "workflow_sha": cast("str", arguments.workflow_sha),
    }
    if values["repository"] != CANONICAL_REPOSITORY:
        raise _SnapshotError
    if _PR_PATTERN.fullmatch(values["pull_request"]) is None:
        raise _SnapshotError
    if _COMMIT_PATTERN.fullmatch(values["head_sha"]) is None:
        raise _SnapshotError
    if _COMMIT_PATTERN.fullmatch(values["workflow_sha"]) is None:
        raise _SnapshotError
    if _PR_PATTERN.fullmatch(values["run_id"]) is None:
        raise _SnapshotError
    if _PR_PATTERN.fullmatch(values["run_attempt"]) is None:
        raise _SnapshotError
    return values


def _has_forbidden_environment(environ: Mapping[str, str]) -> bool:
    for key, value in environ.items():
        normalized = key.upper()
        if value and (
            normalized.startswith(_FORBIDDEN_ENVIRONMENT_PREFIXES)
            or normalized.endswith(_FORBIDDEN_ENVIRONMENT_SUFFIXES)
        ):
            return True
    return False


def _reject_forbidden_environment(environ: Mapping[str, str]) -> None:
    if _has_forbidden_environment(environ):
        raise _ReplayError


def _matches_exact(value: object, expected: object) -> bool:
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        if not isinstance(value, dict) or value.keys() != expected.keys():
            return False
        return all(_matches_exact(value[key], item) for key, item in expected.items())
    return value == expected


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _SnapshotError
    return cast("dict[str, object]", value)


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise _SnapshotError
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise _SnapshotError from error
    return value


def _validate_snapshot(
    path: Path,
    *,
    expected_identity: Mapping[str, str],
) -> tuple[dict[str, object], str]:
    raw = path.read_bytes()
    document = _mapping(json.loads(raw))
    if raw != _canonical_bytes(document) + b"\n":
        raise _SnapshotError
    if set(document) != {
        "accelerator",
        "complete",
        "digests",
        "identity",
        "machine",
        "profile",
        "schema",
    }:
        raise _SnapshotError
    expected_workflow_ref = (
        f"{CANONICAL_REPOSITORY}/.github/workflows/"
        f"trusted-live-read-only.yml@{expected_identity['workflow_sha']}"
    )
    identity = _mapping(document["identity"])
    expected_snapshot_identity: dict[str, object] = {
        "collected_at": identity.get("collected_at"),
        "head_sha": expected_identity["head_sha"],
        "pull_request": expected_identity["pull_request"],
        "repository": expected_identity["repository"],
        "run_attempt": expected_identity["run_attempt"],
        "run_id": expected_identity["run_id"],
        "workflow_ref": expected_workflow_ref,
        "workflow_sha": expected_identity["workflow_sha"],
    }
    _validate_timestamp(identity.get("collected_at"))
    if not _matches_exact(identity, expected_snapshot_identity):
        raise _SnapshotError

    profile = {
        "max_results": MAX_RESULTS,
        "operations": list(OPERATIONS),
        "project_alias": PROJECT_ALIAS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "zone": ZONE,
    }
    machine = {
        "guest_accelerator_count": ACCELERATOR_COUNT,
        "guest_accelerator_type": ACCELERATOR_NAME,
        "name": MACHINE_NAME,
        "zone": ZONE,
    }
    accelerator = {"name": ACCELERATOR_NAME, "zone": ZONE}
    if document["schema"] != SNAPSHOT_SCHEMA:
        raise _SnapshotError
    if document["complete"] is not True:
        raise _SnapshotError
    if not _matches_exact(document["profile"], profile):
        raise _SnapshotError
    if not _matches_exact(document["machine"], machine):
        raise _SnapshotError
    if not _matches_exact(document["accelerator"], accelerator):
        raise _SnapshotError

    digests = _mapping(document["digests"])
    expected_digests = {
        "accelerator": _digest(accelerator),
        "identity": _digest(identity),
        "machine": _digest(machine),
    }
    snapshot_without_digest = dict(document)
    snapshot_without_digest["digests"] = expected_digests
    expected_digests["snapshot"] = _digest(snapshot_without_digest)
    if not _matches_exact(digests, expected_digests):
        raise _SnapshotError
    return document, expected_digests["snapshot"]


async def _exercise_wrappers(snapshot: Mapping[str, object]) -> None:
    from google.cloud import compute_v1  # noqa: PLC0415

    from cqmgr.adapters.google.compute_catalog import (  # noqa: PLC0415
        OfficialComputeAcceleratorTypesPageClient,
        OfficialComputeMachineTypesPageClient,
    )

    machine = _mapping(snapshot["machine"])
    accelerator = _mapping(snapshot["accelerator"])
    machine_page = compute_v1.MachineTypeList(
        items=(
            compute_v1.MachineType(
                name=cast("str", machine["name"]),
                accelerators=(
                    compute_v1.Accelerators(
                        guest_accelerator_count=cast(
                            "int", machine["guest_accelerator_count"]
                        ),
                        guest_accelerator_type=cast(
                            "str", machine["guest_accelerator_type"]
                        ),
                    ),
                ),
            ),
        ),
        next_page_token="",
    )
    accelerator_page = compute_v1.AcceleratorTypeList(
        items=(compute_v1.AcceleratorType(name=cast("str", accelerator["name"])),),
        next_page_token="",
    )
    machine_client = _GeneratedClient(
        page=machine_page,
        request_type=compute_v1.ListMachineTypesRequest,
    )
    accelerator_client = _GeneratedClient(
        page=accelerator_page,
        request_type=compute_v1.ListAcceleratorTypesRequest,
    )
    machine_wrapper = OfficialComputeMachineTypesPageClient(
        cast("compute_v1.MachineTypesClient", machine_client),
        maximum_workers=1,
    )
    accelerator_wrapper = OfficialComputeAcceleratorTypesPageClient(
        cast("compute_v1.AcceleratorTypesClient", accelerator_client),
        maximum_workers=1,
    )
    try:
        machine_result = await machine_wrapper.machine_types_for_zone(
            project=PROJECT_ALIAS,
            zone=ZONE,
            max_results=MAX_RESULTS,
            page_token="",
            timeout_seconds=TIMEOUT_SECONDS,
        )
        accelerator_result = await accelerator_wrapper.accelerator_types_for_zone(
            project=PROJECT_ALIAS,
            zone=ZONE,
            max_results=MAX_RESULTS,
            page_token="",
            timeout_seconds=TIMEOUT_SECONDS,
        )
    finally:
        await accelerator_wrapper.close()
        await machine_wrapper.close()
    machine_transport_closed = machine_client.transport.wait_closed(
        PROCESS_REAP_SECONDS
    )
    accelerator_transport_closed = accelerator_client.transport.wait_closed(
        PROCESS_REAP_SECONDS
    )
    if len(machine_result.scopes) != 1:
        raise _ReplayError
    machine_types = machine_result.scopes[0].machine_types
    machine_accelerators = machine_types[0].accelerators if machine_types else ()
    if (
        not machine_client.called
        or not accelerator_client.called
        or not machine_transport_closed
        or not accelerator_transport_closed
        or machine_result.next_page_token != ""
        or accelerator_result.next_page_token != ""
        or machine_result.scopes[0].scope != f"zones/{ZONE}"
        or machine_result.scopes[0].warning_code is not None
        or len(machine_types) != 1
        or machine_types[0].name != MACHINE_NAME
        or len(machine_accelerators) != 1
        or machine_accelerators[0].guest_accelerator_type != ACCELERATOR_NAME
        or machine_accelerators[0].guest_accelerator_count != ACCELERATOR_COUNT
        or len(accelerator_result.scopes) != 1
        or accelerator_result.scopes[0].scope != f"zones/{ZONE}"
        or accelerator_result.scopes[0].warning_code is not None
        or len(accelerator_result.scopes[0].accelerator_types) != 1
        or accelerator_result.scopes[0].accelerator_types[0].name != ACCELERATOR_NAME
    ):
        raise _ReplayError


def _candidate_directory(value: Path | None) -> Path:
    if value is None or not value.is_absolute():
        raise _ReplayError
    try:
        resolved = value.resolve(strict=True)
    except OSError as error:
        raise _ReplayError from error
    if not resolved.is_dir():
        raise _ReplayError
    return resolved


def _activate_candidate_site_packages(location: Path) -> None:
    """Expose installed packages without processing candidate startup hooks."""
    path = str(location)
    if path not in sys.path:
        sys.path.append(path)


def _read_child_challenge() -> bytes:
    challenge = sys.stdin.buffer.read(CHILD_CHALLENGE_BYTES + 1)
    if len(challenge) != CHILD_CHALLENGE_BYTES:
        raise _ReplayError
    return challenge


def _child_response(challenge: bytes) -> bytes:
    if len(challenge) != CHILD_CHALLENGE_BYTES:
        raise _ReplayError
    return hashlib.sha256(CHILD_RESPONSE_DOMAIN + challenge).digest()


def _write_child_response(response: bytes) -> None:
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.flush()


def _child_main(
    arguments: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    challenge_reader: Callable[[], bytes] = _read_child_challenge,
    response_writer: Callable[[bytes], None] = _write_child_response,
) -> int:
    """Exercise candidate code without owning trusted evidence output."""
    try:
        identity = _expected_identity(arguments)
        _reject_forbidden_environment(environ)
        snapshot, _ = _validate_snapshot(
            cast("Path", arguments.snapshot),
            expected_identity=identity,
        )
        site_packages = _candidate_directory(
            cast("Path | None", arguments.candidate_site_packages)
        )
        _activate_candidate_site_packages(site_packages)
        asyncio.run(_exercise_wrappers(snapshot))
        response = _child_response(challenge_reader())
    except (Exception, SystemExit):  # noqa: BLE001
        return 1
    response_writer(response)
    return 0


def _child_command(arguments: argparse.Namespace) -> list[str]:
    site_packages = _candidate_directory(
        cast("Path | None", arguments.candidate_site_packages)
    )
    candidate_home = _candidate_directory(cast("Path | None", arguments.candidate_home))
    trusted_python = Path(sys.executable).resolve(strict=True)
    return [
        "/usr/bin/sudo",
        "-n",
        "-u",
        CANDIDATE_USER,
        "--",
        "/usr/bin/env",
        "-i",
        f"HOME={candidate_home}",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONNOUSERSITE=1",
        str(trusted_python),
        "-I",
        "-S",
        str(Path(__file__).resolve(strict=True)),
        "--snapshot",
        str(cast("Path", arguments.snapshot)),
        "--repository",
        cast("str", arguments.repository),
        "--pull-request",
        cast("str", arguments.pull_request),
        "--head-sha",
        cast("str", arguments.head_sha),
        "--workflow-sha",
        cast("str", arguments.workflow_sha),
        "--run-id",
        cast("str", arguments.run_id),
        "--run-attempt",
        cast("str", arguments.run_attempt),
        "--candidate-site-packages",
        str(site_packages),
        "--candidate-home",
        str(candidate_home),
        "--child-only",
    ]


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    """Terminate and reap the candidate's dedicated process group."""
    try:
        runner(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/pkill",
                "-KILL",
                "-g",
                str(process.pid),
                "--",
                ".*",
            ],
            check=False,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        process.wait(timeout=PROCESS_REAP_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _ReplayError from error


def _read_process_output(
    selector: selectors.BaseSelector,
    output: bytearray,
    *,
    output_limit: int,
    timeout_seconds: float,
) -> bool:
    """Read one bounded batch and report whether the selector became ready."""
    ready = selector.select(timeout=timeout_seconds)
    for key, _ in ready:
        chunk = os.read(key.fd, output_limit + 1 - len(output))
        if not chunk:
            selector.unregister(key.fileobj)
            continue
        output.extend(chunk)
        if len(output) > output_limit:
            raise _ReplayError
    return bool(ready)


def _supervise_process(
    command: Sequence[str],
    *,
    input_bytes: bytes,
    output_limit: int,
    timeout_seconds: float,
    group_terminator: Callable[[subprocess.Popen[bytes]], None] = (
        _terminate_process_group
    ),
) -> bytes:
    """Bound output, time, and descendants for one isolated candidate child."""
    process = subprocess.Popen(  # noqa: S603 - exact trusted command construction
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdin is None or process.stdout is None:
        group_terminator(process)
        raise _ReplayError
    selector = selectors.DefaultSelector()
    output = bytearray()
    started_at = time.monotonic()
    group_terminated = False
    try:
        process.stdin.write(input_bytes)
        process.stdin.close()
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while process.poll() is None:
            remaining = timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                raise _ReplayError
            _read_process_output(
                selector,
                output,
                output_limit=output_limit,
                timeout_seconds=min(remaining, 0.05),
            )

        return_code = process.returncode
        group_terminator(process)
        group_terminated = True
        while selector.get_map():
            if not _read_process_output(
                selector,
                output,
                output_limit=output_limit,
                timeout_seconds=0.05,
            ):
                raise _ReplayError
        if return_code != 0:
            raise _ReplayError
        return bytes(output)
    except (BrokenPipeError, OSError) as error:
        raise _ReplayError from error
    finally:
        if not group_terminated:
            group_terminator(process)
        selector.close()
        process.stdout.close()


def _reap_candidate_uid(
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    """Kill and verify all processes owned by the replay-only ephemeral UID."""
    kill_command = [
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/pkill",
        "-KILL",
        "-u",
        CANDIDATE_USER,
        "--",
        ".*",
    ]
    probe_command = ["/usr/bin/pgrep", "-u", CANDIDATE_USER, "--", ".*"]
    options = {
        "check": False,
        "stderr": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
    }
    for _ in range(UID_REAP_ATTEMPTS):
        runner(kill_command, **options)
        probe = runner(probe_command, **options)
        if probe.returncode == 1:
            return
        if probe.returncode != 0:
            raise _ReplayError
    raise _ReplayError


def _run_candidate(
    arguments: argparse.Namespace,
    *,
    supervisor: Callable[..., bytes] = _supervise_process,
    challenge_factory: Callable[[int], bytes] = secrets.token_bytes,
    candidate_cleanup: Callable[[], None] = _reap_candidate_uid,
) -> None:
    """Run the candidate child with bounded time and a fresh challenge."""
    challenge = challenge_factory(CHILD_CHALLENGE_BYTES)
    if len(challenge) != CHILD_CHALLENGE_BYTES:
        raise _ReplayError
    try:
        response = supervisor(
            _child_command(arguments),
            input_bytes=challenge,
            output_limit=CHILD_RESPONSE_BYTES,
            timeout_seconds=CHILD_TIMEOUT_SECONDS,
        )
    finally:
        candidate_cleanup()
    if not secrets.compare_digest(response, _child_response(challenge)):
        raise _ReplayError


def _evidence(  # noqa: PLR0913 - explicit stable evidence-envelope fields
    *,
    command_class: str,
    elapsed_seconds: float,
    exit_status: int,
    identity: Mapping[str, str] | None,
    outcome_class: str,
    failure: str | None = None,
    snapshot_digest: str | None = None,
) -> dict[str, object]:
    if outcome_class == "passed":
        snapshot_check = "passed"
        wrapper_check = "passed"
    elif outcome_class == "verified":
        snapshot_check = "passed"
        wrapper_check = "not-run"
    elif failure in {"credential-environment", "identity-invalid"}:
        snapshot_check = "not-run"
        wrapper_check = "not-run"
    elif failure == "snapshot-invalid":
        snapshot_check = "failed"
        wrapper_check = "not-run"
    else:
        snapshot_check = "passed"
        wrapper_check = "failed"
    evidence: dict[str, object] = {
        "command_class": command_class,
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
        "exit_status": exit_status,
        "identity": dict(identity) if identity is not None else "unavailable",
        "outcome_class": outcome_class,
        "provider": {
            "codec": PROVIDER_CODEC,
            "name": "google",
            "checks": {
                "accelerator_exact_zone_wrapper": wrapper_check,
                "machine_exact_zone_wrapper": wrapper_check,
                "snapshot": snapshot_check,
            },
        },
        "schema": EVIDENCE_SCHEMA,
        "snapshot_digest": snapshot_digest or "unavailable",
    }
    if failure is not None:
        evidence["failure"] = failure
    evidence["digest"] = _digest(evidence)
    return evidence


def _write(path: Path, evidence: Mapping[str, object]) -> None:
    path.write_bytes(_canonical_bytes(evidence) + b"\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    candidate_runner: Callable[[argparse.Namespace], None] = _run_candidate,
    environ: Mapping[str, str] = os.environ,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Validate and replay the snapshot without accepting cloud capability."""
    arguments = _parser().parse_args(argv)
    if arguments.child_only:
        return _child_main(arguments, environ=environ)
    output = cast("Path | None", arguments.output)
    if output is None:
        return 2
    started_at = monotonic()
    command_class = (
        "snapshot-verification"
        if arguments.verify_only
        else "exact-wheel-snapshot-replay"
    )
    identity: dict[str, str] | None = None
    snapshot_digest: str | None = None
    failure = "identity-invalid"
    try:
        identity = _expected_identity(arguments)
        failure = "credential-environment"
        _reject_forbidden_environment(environ)
        failure = "snapshot-invalid"
        _, snapshot_digest = _validate_snapshot(
            cast("Path", arguments.snapshot),
            expected_identity=identity,
        )
        if arguments.verify_only:
            _write(
                output,
                _evidence(
                    command_class=command_class,
                    elapsed_seconds=monotonic() - started_at,
                    exit_status=0,
                    identity=identity,
                    outcome_class="verified",
                    snapshot_digest=snapshot_digest,
                ),
            )
            return 0
        failure = "candidate-replay"
        candidate_runner(arguments)
    except BaseException as error:
        _write(
            output,
            _evidence(
                command_class=command_class,
                elapsed_seconds=monotonic() - started_at,
                exit_status=1,
                identity=identity,
                outcome_class="failed",
                failure=failure,
                snapshot_digest=snapshot_digest,
            ),
        )
        if isinstance(error, (Exception, SystemExit)):
            return 1
        raise
    _write(
        output,
        _evidence(
            command_class=command_class,
            elapsed_seconds=monotonic() - started_at,
            exit_status=0,
            identity=identity,
            outcome_class="passed",
            snapshot_digest=snapshot_digest,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
