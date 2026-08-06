"""Collect one trusted, sanitized exact-zone Compute qualification snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from google.cloud import compute_v1

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

SCHEMA = "cqmgr.live-compute-exact-zone-snapshot/v1"
CANONICAL_REPOSITORY = "nisavid/cqmgr"
PROJECT_ALIAS = "protected-project"
ZONE = "us-central1-a"
MACHINE_NAME = "a3-highgpu-8g"
ACCELERATOR_NAME = "nvidia-h100-80gb"
ACCELERATOR_COUNT = 8
MAX_RESULTS = 50
TIMEOUT_SECONDS = 10
OPERATIONS = (
    "compute.machineTypes.list",
    "compute.acceleratorTypes.list",
)
MACHINE_FILTER = f'(name = "{MACHINE_NAME}")'
ACCELERATOR_FILTER = f'(name = "{ACCELERATOR_NAME}")'

_PR_PATTERN = re.compile(r"[1-9][0-9]*\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


class _Transport(Protocol):
    def close(self) -> None: ...


class _Client(Protocol):
    transport: _Transport

    def list(
        self,
        *,
        request: object,
        retry: object,
        timeout: float,
    ) -> object: ...


class _CollectionError(Exception):
    """A collection result could not satisfy the trusted snapshot contract."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect the fixed cqmgr exact-zone Compute snapshot.",
    )
    parser.add_argument("--project-env", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--output", required=True, type=Path)
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


def _validated_identity(
    arguments: argparse.Namespace,
    *,
    collected_at: datetime,
) -> dict[str, str]:
    repository = cast("str", arguments.repository)
    pull_request = cast("str", arguments.pull_request)
    head_sha = cast("str", arguments.head_sha)
    workflow_ref = cast("str", arguments.workflow_ref)
    workflow_sha = cast("str", arguments.workflow_sha)
    run_id = cast("str", arguments.run_id)
    run_attempt = cast("str", arguments.run_attempt)
    if repository != CANONICAL_REPOSITORY:
        raise _CollectionError
    if _PR_PATTERN.fullmatch(pull_request) is None:
        raise _CollectionError
    if _COMMIT_PATTERN.fullmatch(head_sha) is None:
        raise _CollectionError
    if _COMMIT_PATTERN.fullmatch(workflow_sha) is None:
        raise _CollectionError
    expected_workflow_ref = (
        f"{CANONICAL_REPOSITORY}/.github/workflows/"
        f"trusted-live-read-only.yml@{workflow_sha}"
    )
    if workflow_ref != expected_workflow_ref:
        raise _CollectionError
    if _PR_PATTERN.fullmatch(run_id) is None:
        raise _CollectionError
    if _PR_PATTERN.fullmatch(run_attempt) is None:
        raise _CollectionError
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise _CollectionError
    timestamp = collected_at.astimezone(UTC).replace(microsecond=0)
    return {
        "collected_at": timestamp.isoformat().replace("+00:00", "Z"),
        "head_sha": head_sha,
        "pull_request": pull_request,
        "repository": repository,
        "run_attempt": run_attempt,
        "run_id": run_id,
        "workflow_ref": workflow_ref,
        "workflow_sha": workflow_sha,
    }


def _only_page(pager: object) -> object:
    pages = iter(cast("Any", pager).pages)
    try:
        page = next(pages)
    except StopIteration as error:
        raise _CollectionError from error
    if cast("Any", page).next_page_token:
        raise _CollectionError
    return page


def _one_named(items: object, name: str) -> Any:  # noqa: ANN401
    matches = [item for item in cast("Any", items) if item.name == name]
    if len(matches) != 1:
        raise _CollectionError
    return matches[0]


def _machine_record(machine: object) -> dict[str, object]:
    accelerators = list(cast("Any", machine).accelerators)
    if len(accelerators) != 1:
        raise _CollectionError
    attachment = accelerators[0]
    if (
        attachment.guest_accelerator_type != ACCELERATOR_NAME
        or attachment.guest_accelerator_count != ACCELERATOR_COUNT
    ):
        raise _CollectionError
    return {
        "guest_accelerator_count": ACCELERATOR_COUNT,
        "guest_accelerator_type": ACCELERATOR_NAME,
        "name": MACHINE_NAME,
        "zone": ZONE,
    }


def _collect(
    *,
    project: str,
    machine_client: _Client,
    accelerator_client: _Client,
) -> tuple[dict[str, object], dict[str, str]]:
    machine_request = compute_v1.ListMachineTypesRequest(
        filter=MACHINE_FILTER,
        max_results=MAX_RESULTS,
        page_token="",
        project=project,
        zone=ZONE,
    )
    machine_page = _only_page(
        machine_client.list(
            request=machine_request,
            retry=None,
            timeout=TIMEOUT_SECONDS,
        )
    )
    machine = _one_named(cast("Any", machine_page).items, MACHINE_NAME)

    accelerator_request = compute_v1.ListAcceleratorTypesRequest(
        filter=ACCELERATOR_FILTER,
        max_results=MAX_RESULTS,
        page_token="",
        project=project,
        zone=ZONE,
    )
    accelerator_page = _only_page(
        accelerator_client.list(
            request=accelerator_request,
            retry=None,
            timeout=TIMEOUT_SECONDS,
        )
    )
    _one_named(cast("Any", accelerator_page).items, ACCELERATOR_NAME)

    return _machine_record(machine), {
        "name": ACCELERATOR_NAME,
        "zone": ZONE,
    }


def _snapshot(
    *,
    identity: dict[str, str],
    machine: dict[str, object],
    accelerator: dict[str, str],
) -> dict[str, object]:
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
        "profile": {
            "max_results": MAX_RESULTS,
            "operations": list(OPERATIONS),
            "project_alias": PROJECT_ALIAS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "zone": ZONE,
        },
        "schema": SCHEMA,
    }
    # Bind the document while only the three component digests are present;
    # the replay verifier reconstructs this exact pre-digest shape.
    digests["snapshot"] = _digest(snapshot)
    return snapshot


def _close(client: _Client | None) -> None:
    if client is not None:
        client.transport.close()


def _project(environ: Mapping[str, str], key: str) -> str:
    project = environ.get(key)
    if not project:
        raise _CollectionError
    return project


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    machine_client_factory: Callable[[], object] = compute_v1.MachineTypesClient,
    accelerator_client_factory: Callable[[], object] = (
        compute_v1.AcceleratorTypesClient
    ),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Collect and write the fixed snapshot without retaining provider data."""
    arguments = _parser().parse_args(argv)
    machine_client: _Client | None = None
    accelerator_client: _Client | None = None
    try:
        project = _project(environ, cast("str", arguments.project_env))
        identity = _validated_identity(
            arguments,
            collected_at=now(),
        )
        machine_client = cast("_Client", machine_client_factory())
        accelerator_client = cast("_Client", accelerator_client_factory())
        machine, accelerator = _collect(
            project=project,
            machine_client=machine_client,
            accelerator_client=accelerator_client,
        )
        arguments.output.write_bytes(
            _canonical_bytes(
                _snapshot(
                    identity=identity,
                    machine=machine,
                    accelerator=accelerator,
                )
            )
            + b"\n"
        )
    except Exception:  # noqa: BLE001
        arguments.output.unlink(missing_ok=True)
        return 1
    finally:
        _close(accelerator_client)
        _close(machine_client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
