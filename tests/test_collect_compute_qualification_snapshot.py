"""Trusted collection of one sanitized exact-zone Compute snapshot."""

from __future__ import annotations

import json
import runpy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override

import pytest
from google.cloud import compute_v1

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "collect_compute_qualification_snapshot.py"
)
HEAD_SHA = "a" * 40
WORKFLOW_SHA = "b" * 40
WORKFLOW_REF = (
    "nisavid/cqmgr/.github/workflows/trusted-live-read-only.yml@" + WORKFLOW_SHA
)
RAW_PROJECT = "private-live-project"
COLLECTED_AT = datetime(2026, 8, 6, 0, 30, tzinfo=UTC)
MAX_RESULTS = 50
TIMEOUT_SECONDS = 10


class _Transport:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Pager:
    def __init__(self, page: object) -> None:
        def pages() -> Iterator[object]:
            yield page
            msg = "collector attempted a second provider page"
            raise AssertionError(msg)

        self.pages = pages()


class _MachineClient:
    def __init__(self) -> None:
        self.transport = _Transport()
        self.calls: list[tuple[object, object, object]] = []

    def list(self, *, request: object, retry: object, timeout: object) -> _Pager:
        self.calls.append((request, retry, timeout))
        page = compute_v1.MachineTypeList(
            items=[
                compute_v1.MachineType(name="unrelated-machine"),
                compute_v1.MachineType(
                    name="a3-highgpu-8g",
                    zone=(
                        "https://www.googleapis.com/compute/v1/projects/"
                        f"{RAW_PROJECT}/zones/us-central1-a"
                    ),
                    self_link=(
                        "https://www.googleapis.com/compute/v1/projects/"
                        f"{RAW_PROJECT}/zones/us-central1-a/"
                        "machineTypes/a3-highgpu-8g"
                    ),
                    accelerators=[
                        compute_v1.Accelerators(
                            guest_accelerator_type="nvidia-h100-80gb",
                            guest_accelerator_count=8,
                        )
                    ],
                ),
            ],
            next_page_token="",
        )
        return _Pager(page)


class _AcceleratorClient:
    def __init__(self) -> None:
        self.transport = _Transport()
        self.calls: list[tuple[object, object, object]] = []

    def list(self, *, request: object, retry: object, timeout: object) -> _Pager:
        self.calls.append((request, retry, timeout))
        page = compute_v1.AcceleratorTypeList(
            items=[
                compute_v1.AcceleratorType(name="unrelated-accelerator"),
                compute_v1.AcceleratorType(
                    name="nvidia-h100-80gb",
                    zone=(
                        "https://www.googleapis.com/compute/v1/projects/"
                        f"{RAW_PROJECT}/zones/us-central1-a"
                    ),
                    self_link=(
                        "https://www.googleapis.com/compute/v1/projects/"
                        f"{RAW_PROJECT}/zones/us-central1-a/"
                        "acceleratorTypes/nvidia-h100-80gb"
                    ),
                ),
            ],
            next_page_token="",
        )
        return _Pager(page)


class _FailingMachineClient(_MachineClient):
    @override
    def list(self, *, request: object, retry: object, timeout: object) -> _Pager:
        del request, retry, timeout
        msg = f"provider warning exposed {RAW_PROJECT} and https://private.invalid"
        raise RuntimeError(msg)


class _PagedMachineClient(_MachineClient):
    @override
    def list(self, *, request: object, retry: object, timeout: object) -> _Pager:
        pager = super().list(request=request, retry=retry, timeout=timeout)
        page = cast("Any", next(pager.pages))
        page.next_page_token = "opaque-provider-token"  # noqa: S105
        return _Pager(page)


def _main() -> Callable[..., int]:
    return cast("Callable[..., int]", runpy.run_path(str(SCRIPT))["main"])


def _arguments(output: Path) -> list[str]:
    return [
        "--project-env",
        "GCP_PROJECT_ID",
        "--repository",
        "nisavid/cqmgr",
        "--pull-request",
        "107",
        "--head-sha",
        HEAD_SHA,
        "--workflow-ref",
        WORKFLOW_REF,
        "--workflow-sha",
        WORKFLOW_SHA,
        "--run-id",
        "123456789",
        "--run-attempt",
        "2",
        "--output",
        str(output),
    ]


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--repository", "other/repository"),
        ("--pull-request", "0"),
        ("--head-sha", "not-a-commit"),
        ("--workflow-ref", "nisavid/cqmgr/.github/workflows/other.yml@" + WORKFLOW_SHA),
        ("--workflow-sha", "not-a-commit"),
        ("--run-id", "0"),
        ("--run-attempt", "0"),
    ],
)
def test_collector_rejects_invalid_identity_before_provider_clients(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    """Every caller and workflow binding fails before cloud client creation."""
    output = tmp_path / "snapshot.json"
    arguments = _arguments(output)
    arguments[arguments.index(flag) + 1] = value
    constructed: list[str] = []

    def machine_factory() -> object:
        constructed.append("machine")
        return _MachineClient()

    def accelerator_factory() -> object:
        constructed.append("accelerator")
        return _AcceleratorClient()

    exit_code = _main()(
        arguments,
        environ={"GCP_PROJECT_ID": RAW_PROJECT},
        machine_client_factory=machine_factory,
        accelerator_client_factory=accelerator_factory,
        now=lambda: COLLECTED_AT,
    )

    assert exit_code == 1
    assert not output.exists()
    assert constructed == []


def test_collector_rejects_missing_project_before_provider_clients(
    tmp_path: Path,
) -> None:
    """A missing protected identifier never reaches a cloud client factory."""
    output = tmp_path / "snapshot.json"
    constructed: list[str] = []

    def machine_factory() -> object:
        constructed.append("machine")
        return _MachineClient()

    exit_code = _main()(
        _arguments(output),
        environ={},
        machine_client_factory=machine_factory,
        accelerator_client_factory=_AcceleratorClient,
        now=lambda: COLLECTED_AT,
    )

    assert exit_code == 1
    assert not output.exists()
    assert constructed == []


def test_collector_rejects_an_incomplete_provider_page(tmp_path: Path) -> None:
    """A provider continuation token cannot be mistaken for complete evidence."""
    output = tmp_path / "snapshot.json"
    machine_client = _PagedMachineClient()
    accelerator_client = _AcceleratorClient()

    exit_code = _main()(
        _arguments(output),
        environ={"GCP_PROJECT_ID": RAW_PROJECT},
        machine_client_factory=lambda: machine_client,
        accelerator_client_factory=lambda: accelerator_client,
        now=lambda: COLLECTED_AT,
    )

    assert exit_code == 1
    assert not output.exists()
    assert machine_client.transport.closed is True
    assert accelerator_client.transport.closed is True


def test_collector_writes_only_the_canonical_project_aliased_snapshot(
    tmp_path: Path,
) -> None:
    """Exact live calls retain only reviewed identities and digested bindings."""
    output = tmp_path / "snapshot.json"
    machine_client = _MachineClient()
    accelerator_client = _AcceleratorClient()

    exit_code = _main()(
        [
            "--project-env",
            "GCP_PROJECT_ID",
            "--repository",
            "nisavid/cqmgr",
            "--pull-request",
            "107",
            "--head-sha",
            HEAD_SHA,
            "--workflow-ref",
            WORKFLOW_REF,
            "--workflow-sha",
            WORKFLOW_SHA,
            "--run-id",
            "123456789",
            "--run-attempt",
            "2",
            "--output",
            str(output),
        ],
        environ={"GCP_PROJECT_ID": RAW_PROJECT},
        machine_client_factory=lambda: machine_client,
        accelerator_client_factory=lambda: accelerator_client,
        now=lambda: COLLECTED_AT,
    )

    assert exit_code == 0
    document = output.read_text(encoding="utf-8")
    snapshot = cast("Mapping[str, Any]", json.loads(document))
    assert (
        document
        == json.dumps(
            snapshot,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    assert snapshot["schema"] == "cqmgr.live-compute-exact-zone-snapshot/v1"
    assert snapshot["complete"] is True
    assert snapshot["identity"] == {
        "collected_at": "2026-08-06T00:30:00Z",
        "head_sha": HEAD_SHA,
        "pull_request": "107",
        "repository": "nisavid/cqmgr",
        "run_attempt": "2",
        "run_id": "123456789",
        "workflow_ref": WORKFLOW_REF,
        "workflow_sha": WORKFLOW_SHA,
    }
    assert snapshot["profile"] == {
        "max_results": 50,
        "operations": [
            "compute.machineTypes.list",
            "compute.acceleratorTypes.list",
        ],
        "project_alias": "protected-project",
        "timeout_seconds": 10,
        "zone": "us-central1-a",
    }
    assert snapshot["machine"] == {
        "guest_accelerator_count": 8,
        "guest_accelerator_type": "nvidia-h100-80gb",
        "name": "a3-highgpu-8g",
        "zone": "us-central1-a",
    }
    assert snapshot["accelerator"] == {
        "name": "nvidia-h100-80gb",
        "zone": "us-central1-a",
    }
    assert set(cast("Mapping[str, str]", snapshot["digests"])) == {
        "accelerator",
        "identity",
        "machine",
        "snapshot",
    }
    assert cast("Mapping[str, str]", snapshot["digests"])["snapshot"] == (
        "sha256:7291502ce0be1dabc7c3cb6fb661a5612471ee93f2efebdb02afb2594825890d"
    )
    assert RAW_PROJECT not in document
    assert "https://" not in document
    assert "unrelated" not in document

    machine_request, machine_retry, machine_timeout = machine_client.calls[0]
    assert isinstance(machine_request, compute_v1.ListMachineTypesRequest)
    assert machine_request.project == RAW_PROJECT
    assert machine_request.zone == "us-central1-a"
    assert machine_request.max_results == MAX_RESULTS
    assert machine_request.page_token == ""
    assert machine_request.filter == '(name = "a3-highgpu-8g")'
    assert machine_retry is None
    assert machine_timeout == TIMEOUT_SECONDS
    accelerator_request, accelerator_retry, accelerator_timeout = (
        accelerator_client.calls[0]
    )
    assert isinstance(accelerator_request, compute_v1.ListAcceleratorTypesRequest)
    assert accelerator_request.project == RAW_PROJECT
    assert accelerator_request.zone == "us-central1-a"
    assert accelerator_request.max_results == MAX_RESULTS
    assert accelerator_request.page_token == ""
    assert accelerator_request.filter == '(name = "nvidia-h100-80gb")'
    assert accelerator_retry is None
    assert accelerator_timeout == TIMEOUT_SECONDS
    assert machine_client.transport.closed is True
    assert accelerator_client.transport.closed is True


def test_collector_drops_provider_failures_without_retaining_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Provider failures close transports and retain no project or warning text."""
    output = tmp_path / "snapshot.json"
    machine_client = _FailingMachineClient()
    accelerator_client = _AcceleratorClient()

    exit_code = _main()(
        [
            "--project-env",
            "GCP_PROJECT_ID",
            "--repository",
            "nisavid/cqmgr",
            "--pull-request",
            "107",
            "--head-sha",
            HEAD_SHA,
            "--workflow-ref",
            WORKFLOW_REF,
            "--workflow-sha",
            WORKFLOW_SHA,
            "--run-id",
            "123456789",
            "--run-attempt",
            "2",
            "--output",
            str(output),
        ],
        environ={"GCP_PROJECT_ID": RAW_PROJECT},
        machine_client_factory=lambda: machine_client,
        accelerator_client_factory=lambda: accelerator_client,
        now=lambda: COLLECTED_AT,
    )

    assert exit_code == 1
    assert not output.exists()
    assert capsys.readouterr() == ("", "")
    assert machine_client.transport.closed is True
    assert accelerator_client.transport.closed is True
