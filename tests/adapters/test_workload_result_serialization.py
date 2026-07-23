"""Stable structured discriminators for workload-first quota resolution."""

from datetime import UTC, datetime

from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
)
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _mapping(data: object) -> dict[str, object]:
    result = OperationResult(
        operation=OperationName("quota.resolve"),
        resource_scope=None,
        boundary=OperationBoundary(
            StableSymbol("workload-requirement-resolved"),
            reached=True,
        ),
        outcome=Outcome(StableSymbol("requirement-resolved"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=data,
    )
    return operation_result_mapping(result)


def test_compute_instance_candidate_shape_has_stable_discriminators() -> None:
    """Structured output identifies both workload and location-selection shapes."""
    requirement = ComputeInstanceRequirement(
        machine_type="a4-highgpu-8g",
        instance_count=2,
        provisioning_model=ProvisioningModel.STANDARD,
        locations=CandidateLocations(("us-central1-a", "us-east1-b")),
    )

    assert _mapping(requirement)["data"] == {
        "kind": "compute-instance",
        "machine_type": "a4-highgpu-8g",
        "instance_count": 2,
        "provisioning_model": "standard",
        "locations": {
            "mode": "candidates",
            "values": ["us-central1-a", "us-east1-b"],
        },
    }


def test_cloud_tpu_slice_all_compatible_shape_has_stable_discriminators() -> None:
    """All-compatible is explicit and never confused with an empty candidate list."""
    requirement = CloudTpuSliceRequirement(
        accelerator_type="v6e-8",
        topology="2x4",
        runtime_version="tpu-vm-base",
        slice_count=1,
        provisioning_model=ProvisioningModel.SPOT,
        locations=AllCompatibleLocations(),
    )

    assert _mapping(requirement)["data"] == {
        "kind": "cloud-tpu-slice",
        "accelerator_type": "v6e-8",
        "topology": "2x4",
        "runtime_version": "tpu-vm-base",
        "slice_count": 1,
        "provisioning_model": "spot",
        "locations": {"mode": "all-compatible"},
    }
