"""Public Click command tree for the V1 read-only vertical slice."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

import cqmgr.cli as cli_module
from cqmgr.adapters.cli.copy_cli import (
    CopyCliPresentation,
    obtainability_all_compatible_copy_cli,
    quota_inspect_copy_cli,
    quota_list_copy_cli,
    quota_resolve_copy_cli,
)
from cqmgr.adapters.cli.group import canonical_command_path
from cqmgr.application.operations.read_only import (
    QuotaInspectSelector,
    ReadOnlyQuotaQuery,
)
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
)
from cqmgr.domain.catalog import CatalogGroupId
from cqmgr.domain.obtainability import (
    DistributionShape,
    GpuAttachment,
    SpotMachineConfiguration,
)
from cqmgr.domain.quota_queries import QuotaQueryFilters, QuotaSort, QuotaSortField
from cqmgr.domain.quotas import NormalizedDimensions
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import Reconciliation

if TYPE_CHECKING:
    from pathlib import Path

USAGE_EXIT = 2


def test_copy_cli_canonical_paths_are_derived_from_the_alias_registry() -> None:
    """Generated commands use registered canonical siblings, never aliases."""
    assert canonical_command_path("cqmgr", "quota", "list") == (
        "cqmgr",
        "quota",
        "list",
    )
    assert canonical_command_path(
        "cqmgr",
        "quota",
        "resolve",
        "cloud-tpu-slice",
    ) == ("cqmgr", "quota", "resolve", "cloud-tpu-slice")

    with pytest.raises(
        ValueError,
        match="not registered",
    ):
        canonical_command_path("cqmgr", "q", "list")


REJECTED_PRECONDITION_EXIT = 3
QUERY_LIMIT = 20
INSTANCE_COUNT = 2
main = cli_module.main


def _result(
    operation: str,
    data: object = None,
    *,
    exit_class: ExitClass = ExitClass.SUCCESS,
) -> OperationResult[object]:
    now = datetime(2026, 7, 23, 18, tzinfo=UTC)
    return OperationResult(
        operation=OperationName(operation),
        resource_scope=None,
        boundary=OperationBoundary(
            StableSymbol("completed"),
            exit_class is ExitClass.SUCCESS,
        ),
        outcome=Outcome(
            StableSymbol(
                "succeeded" if exit_class is ExitClass.SUCCESS else "invalid-input"
            ),
            exit_class,
        ),
        completeness=Completeness.complete(),
        started_at=now,
        finished_at=now,
        data=data,
    )


class RecordingReadOnlyOperations:
    """Record the typed application request received from each Click leaf."""

    def __init__(self) -> None:
        """Initialize empty operation and usage call ledgers."""
        self.browse_calls: list[tuple[object, dict[str, object]]] = []
        self.inspect_calls: list[tuple[object, dict[str, object]]] = []
        self.resolve_calls: list[tuple[object, dict[str, object]]] = []
        self.obtainability_calls: list[tuple[object, dict[str, object]]] = []
        self.usage_calls: list[tuple[str, str]] = []
        self.close_calls = 0

    async def aclose(self) -> None:
        """Record one composition-root shutdown."""
        self.close_calls += 1

    async def browse(self, query: object, **kwargs: object) -> OperationResult[object]:
        """Record one quota browse call."""
        self.browse_calls.append((query, kwargs))
        return _result("quota.list")

    async def inspect(
        self,
        selector: object,
        **kwargs: object,
    ) -> OperationResult[object]:
        """Record one exact-slice inspection call."""
        self.inspect_calls.append((selector, kwargs))
        return _result("quota.inspect")

    async def resolve(
        self,
        requirement: object,
        **kwargs: object,
    ) -> OperationResult[object]:
        """Record one workload resolution call."""
        self.resolve_calls.append((requirement, kwargs))
        return _result("quota.resolve")

    async def compare_obtainability(
        self,
        candidates: object,
        **kwargs: object,
    ) -> OperationResult[object]:
        """Record one exact Spot advice comparison."""
        self.obtainability_calls.append((candidates, kwargs))
        return _result("obtainability.compare")

    async def compare_obtainability_all_compatible(
        self,
        requirement: object,
        **kwargs: object,
    ) -> OperationResult[object]:
        """Record one resolver-backed Spot advice comparison."""
        self.obtainability_calls.append((requirement, kwargs))
        return _result("obtainability.compare")

    async def browse_usage_failure(self, reason: str) -> OperationResult[object]:
        """Record one typed quota-list usage failure."""
        self.usage_calls.append(("quota.list", reason))
        return _result("quota.list", exit_class=ExitClass.USAGE)

    async def inspect_usage_failure(self, reason: str) -> OperationResult[object]:
        """Record one typed quota-inspect usage failure."""
        self.usage_calls.append(("quota.inspect", reason))
        return _result("quota.inspect", exit_class=ExitClass.USAGE)

    async def resolve_usage_failure(self, reason: str) -> OperationResult[object]:
        """Record one typed quota-resolve usage failure."""
        self.usage_calls.append(("quota.resolve", reason))
        return _result("quota.resolve", exit_class=ExitClass.USAGE)

    async def compare_obtainability_usage_failure(
        self,
        reason: str,
    ) -> OperationResult[object]:
        """Record one typed obtainability usage failure."""
        self.usage_calls.append(("obtainability.compare", reason))
        return _result("obtainability.compare", exit_class=ExitClass.USAGE)


def test_quota_command_tree_and_exact_aliases_are_registered() -> None:
    """Quota commands expose only the approved sibling-level aliases."""
    runner = CliRunner()

    canonical = runner.invoke(main, ["quota", "--help"])
    alias = runner.invoke(main, ["q", "--help"])
    resolve = runner.invoke(main, ["q", "r", "--help"])

    assert canonical.exit_code == 0
    assert alias.exit_code == 0
    assert "list" in canonical.output
    assert "inspect" in canonical.output
    assert "resolve" in canonical.output
    assert "list" in alias.output
    assert "inspect" in alias.output
    assert "resolve" in alias.output
    assert resolve.exit_code == 0
    assert "compute-instance" in resolve.output
    assert "cloud-tpu-slice" in resolve.output

    assert runner.invoke(main, ["quo", "--help"]).exit_code == USAGE_EXIT
    assert runner.invoke(main, ["q", "lis", "--help"]).exit_code == USAGE_EXIT
    assert runner.invoke(main, ["q", "r", "com", "--help"]).exit_code == USAGE_EXIT


def test_obtainability_command_preserves_exact_candidates_and_request_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical command and alias delegate one full immutable comparison."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(
        main,
        [
            "ob",
            "c",
            "--resource-scope",
            "projects/123",
            "--machine-type",
            "n1-standard-16",
            "--gpu-type",
            "nvidia-tesla-t4",
            "--gpu-count",
            "2",
            "--vm-count",
            "3",
            "--distribution-shape",
            "any-single-zone",
            "--candidate",
            "us-central1=us-central1-a,us-central1-b",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    candidates, options = operations.obtainability_calls[0]
    candidate = candidates[0]  # type: ignore[index]
    assert candidate.endpoint_region == "us-central1"
    assert candidate.zones == ("us-central1-a", "us-central1-b")
    assert candidate.machine.gpu == GpuAttachment("nvidia-tesla-t4", 2)
    assert candidate.distribution_shape is DistributionShape.ANY_SINGLE_ZONE
    assert "support" not in options
    assert operations.close_calls == 1


def test_obtainability_all_compatible_delegates_a_spot_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exhaustive mode delegates a normalized Spot compute workload."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(
        main,
        [
            "ob",
            "c",
            "--resource-scope",
            "projects/123",
            "--machine-type",
            "n1-standard-16",
            "--gpu-type",
            "nvidia-tesla-t4",
            "--gpu-count",
            "2",
            "--vm-count",
            "2",
            "--distribution-shape",
            "any-single-zone",
            "--all-compatible-locations",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    requirement, options = operations.obtainability_calls[0]
    assert requirement == ComputeInstanceRequirement(
        "n1-standard-16",
        2,
        ProvisioningModel.SPOT,
        AllCompatibleLocations(),
        attached_accelerator_type="nvidia-tesla-t4",
        attached_accelerator_count=2,
    )
    assert options["machine"].machine_type == "n1-standard-16"  # type: ignore[union-attr]
    assert options["distribution_shape"] is DistributionShape.ANY_SINGLE_ZONE
    assert "support" not in options


def test_quota_list_help_exposes_the_complete_v1_query_surface() -> None:
    """Every public query facet remains leaf-scoped and unabbreviated."""
    result = CliRunner().invoke(main, ["quota", "list", "--help"])

    assert result.exit_code == 0
    for option in (
        "--resource-scope",
        "--profile",
        "--text",
        "--service",
        "--catalog-group",
        "--accelerator",
        "--location",
        "--quota-scope",
        "--quota-pool",
        "--cataloged",
        "--guided",
        "--mutable",
        "--reconciliation",
        "--grant-satisfaction",
        "--effective-confirmation",
        "--sort",
        "--limit",
        "--cursor",
        "--output",
        "--no-color",
        "--quiet",
    ):
        assert option in result.output


def test_quota_inspect_help_exposes_exact_slice_selectors() -> None:
    """Exact-slice inspection accepts only canonical selector inputs."""
    result = CliRunner().invoke(main, ["quota", "inspect", "--help"])

    assert result.exit_code == 0
    for option in (
        "--resource-scope",
        "--profile",
        "--service",
        "--quota-id",
        "--location",
        "--dimension",
        "--output",
        "--no-color",
        "--quiet",
    ):
        assert option in result.output


def test_workload_resolution_help_exposes_both_complete_shapes() -> None:
    """Each workload leaf owns its shape and mutually exclusive location mode."""
    runner = CliRunner()
    compute = runner.invoke(main, ["quota", "resolve", "compute-instance", "--help"])
    tpu = runner.invoke(main, ["q", "r", "ct", "--help"])

    assert compute.exit_code == 0
    for option in (
        "--machine-type",
        "--instance-count",
        "--provisioning-model",
        "--candidate",
        "--all-compatible-locations",
    ):
        assert option in compute.output
    assert "--workload-consumer" not in compute.output

    assert tpu.exit_code == 0
    for option in (
        "--accelerator-type",
        "--topology",
        "--runtime-version",
        "--slice-count",
        "--provisioning-model",
        "--candidate",
        "--all-compatible-locations",
    ):
        assert option in tpu.output


def test_audit_command_tree_and_exact_aliases_are_registered() -> None:
    """Offline audit operations expose their exact canonical aliases."""
    runner = CliRunner()

    canonical = runner.invoke(main, ["audit", "--help"])
    alias = runner.invoke(main, ["aud", "--help"])

    assert canonical.exit_code == 0
    assert alias.exit_code == 0
    assert "list" in canonical.output
    assert "inspect" in canonical.output
    assert "verify" in canonical.output
    assert "list" in alias.output
    assert "inspect" in alias.output
    assert "verify" in alias.output
    assert runner.invoke(main, ["audit", "lis", "--help"]).exit_code == USAGE_EXIT


def test_audit_leaf_help_exposes_bounded_local_query_contract() -> None:
    """Audit list, inspect, and verify expose only their documented inputs."""
    runner = CliRunner()
    listing = runner.invoke(main, ["audit", "list", "--help"])
    inspection = runner.invoke(main, ["audit", "inspect", "--help"])
    verification = runner.invoke(main, ["audit", "verify", "--help"])

    assert listing.exit_code == 0
    for option in (
        "--operation",
        "--outcome",
        "--since",
        "--until",
        "--limit",
        "--cursor",
        "--output",
        "--no-color",
        "--quiet",
    ):
        assert option in listing.output
    assert "RECORD_ID" in inspection.output
    assert "--from" in verification.output
    assert "--through" in verification.output


def test_audit_commands_run_offline_with_typed_results(tmp_path: Path) -> None:
    """The real local journal is available without configuring ADC or providers."""
    runner = CliRunner()
    environment = {"CQMGR_AUDIT_PATH": str(tmp_path / "audit")}

    listing = runner.invoke(
        main,
        ["aud", "l", "--output", "json"],
        env=environment,
    )
    verification = runner.invoke(
        main,
        ["audit", "verify", "--output", "json"],
        env=environment,
    )
    missing = runner.invoke(
        main,
        ["audit", "inspect", "audit-00000000000000000001"],
        env=environment,
    )

    assert listing.exit_code == 0, listing.output
    assert json.loads(listing.stdout)["operation"] == "audit.list"
    assert verification.exit_code == 0, verification.output
    assert json.loads(verification.stdout)["data"]["verification"]["valid"] is True
    assert missing.exit_code == REJECTED_PRECONDITION_EXIT
    assert missing.stdout == ""
    assert "Outcome: audit-record-not-found (exit 3)" in missing.stderr
    assert not (tmp_path / "audit").exists()


def test_invalid_audit_query_returns_usage_without_a_traceback(tmp_path: Path) -> None:
    """Invalid RFC3339 input remains a typed CLI usage result."""
    result = CliRunner().invoke(
        main,
        ["audit", "list", "--since", "yesterday", "--output", "json"],
        env={"CQMGR_AUDIT_PATH": str(tmp_path / "audit")},
    )

    assert result.exit_code == USAGE_EXIT
    payload = json.loads(result.stdout)
    assert payload["outcome"]["code"] == "invalid-audit-query"
    assert "Traceback" not in result.output
    assert not (tmp_path / "audit").exists()


def test_installed_package_subprocess_exposes_every_read_only_command_and_alias() -> (
    None
):
    """The installed import path serves all read-only help without integrations."""
    invocations = (
        ("quota", "--help"),
        ("q", "--help"),
        ("quota", "list", "--help"),
        ("q", "l", "--help"),
        ("quota", "inspect", "--help"),
        ("q", "i", "--help"),
        ("quota", "resolve", "--help"),
        ("q", "r", "--help"),
        ("quota", "resolve", "compute-instance", "--help"),
        ("q", "r", "ci", "--help"),
        ("quota", "resolve", "cloud-tpu-slice", "--help"),
        ("q", "r", "ct", "--help"),
        ("audit", "--help"),
        ("aud", "--help"),
        ("audit", "list", "--help"),
        ("aud", "l", "--help"),
        ("audit", "inspect", "--help"),
        ("aud", "i", "--help"),
        ("audit", "verify", "--help"),
        ("aud", "v", "--help"),
    )
    script = """
import sys

forbidden = ("google", "keyring", "textual")

class BlockForbiddenImports:
    def find_spec(self, fullname, path, target=None):
        if fullname in forbidden or fullname.startswith(
            tuple(f"{item}." for item in forbidden)
        ):
            raise AssertionError(f"forbidden read-only help import: {fullname}")
        return None

sys.meta_path.insert(0, BlockForbiddenImports())

from cqmgr.cli import main

main(sys.argv[1:], prog_name="cqmgr")
"""

    for arguments in invocations:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == 0, (arguments, completed.stderr)
        assert completed.stdout.startswith("Usage: cqmgr")
        assert "Traceback" not in completed.stderr


def test_quota_list_decodes_filters_and_delegates_one_typed_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Click passes a scope-neutral normalized query to the application facade."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(
        main,
        [
            "q",
            "l",
            "--resource-scope",
            "projects/123",
            "--service",
            "compute",
            "--catalog-group",
            "compute-accelerators",
            "--location",
            "us-central1",
            "--sort",
            "quota-id:desc",
            "--limit",
            "20",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    query, options = operations.browse_calls[0]
    assert query.filters.services == ("compute.googleapis.com",)  # type: ignore[attr-defined]
    assert query.filters.locations == ("us-central1",)  # type: ignore[attr-defined]
    assert query.sort[0].direction.value == "desc"  # type: ignore[attr-defined]
    assert options["limit"] == QUERY_LIMIT
    scope_input = options["scope_input"]
    assert scope_input.explicit_resource_scope.canonical_name == "projects/123"  # type: ignore[attr-defined]
    assert operations.close_calls == 1


def test_copy_cli_uses_canonical_names_and_round_trips_through_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied command is complete, canonical, shell-safe, and parser-equivalent."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )
    query = ReadOnlyQuotaQuery(
        filters=QuotaQueryFilters(
            services=("compute",),
            catalog_groups=(CatalogGroupId("compute-accelerators"),),
            locations=("us-central1",),
            reconciliations=(Reconciliation.SETTLED,),
            text="H100 GPU",
        ),
        sort=(QuotaSort(QuotaSortField.QUOTA_ID),),
    )

    copied = quota_list_copy_cli(
        ResourceScope(ResourceScopeKind.PROJECT, "projects/123"),
        query,
        limit=20,
        presentation=CopyCliPresentation(
            output="json",
            no_color=True,
            quiet=True,
        ),
    )
    arguments = __import__("shlex").split(copied)

    assert arguments[:3] == ["cqmgr", "quota", "list"]
    assert "q" not in arguments
    service_index = arguments.index("--service")
    assert arguments[service_index + 1] == "compute.googleapis.com"
    assert "compute" not in arguments
    assert arguments.count("--resource-scope") == 1
    result = CliRunner().invoke(main, arguments[1:])
    assert result.exit_code == 0, result.output
    parsed, options = operations.browse_calls[0]
    assert parsed == query
    assert options["limit"] == QUERY_LIMIT


def test_workload_copy_cli_round_trips_both_typed_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copied workload commands retain every typed input under canonical leaves."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    requirements = (
        ComputeInstanceRequirement(
            machine_type="n1-standard-16",
            instance_count=2,
            provisioning_model=ProvisioningModel.SPOT,
            locations=AllCompatibleLocations(),
            attached_accelerator_type="nvidia-tesla-t4",
            attached_accelerator_count=2,
        ),
        CloudTpuSliceRequirement(
            accelerator_type="v6e-8",
            topology="2x4",
            runtime_version="tpu-vm-base",
            slice_count=3,
            provisioning_model=ProvisioningModel.STANDARD,
            locations=AllCompatibleLocations(),
        ),
    )

    copied = tuple(
        quota_resolve_copy_cli(
            scope,
            requirement,
            presentation=CopyCliPresentation(output="json"),
        )
        for requirement in requirements
    )
    arguments = tuple(__import__("shlex").split(command) for command in copied)

    assert arguments[0][:4] == ["cqmgr", "quota", "resolve", "compute-instance"]
    assert arguments[1][:4] == ["cqmgr", "quota", "resolve", "cloud-tpu-slice"]
    for command_arguments in arguments:
        result = CliRunner().invoke(main, command_arguments[1:])
        assert result.exit_code == 0, result.output
    assert tuple(call[0] for call in operations.resolve_calls) == requirements


@pytest.mark.parametrize(
    "machine",
    [
        SpotMachineConfiguration("n1-standard-16"),
        SpotMachineConfiguration(
            "n1-standard-16",
            gpu=GpuAttachment("nvidia-tesla-t4", 1),
        ),
    ],
)
def test_all_compatible_copy_cli_rejects_attachment_mismatch(
    machine: SpotMachineConfiguration,
) -> None:
    """Copy CLI cannot weaken or alter a resolver-owned attachment request."""
    requirement = ComputeInstanceRequirement(
        machine_type="n1-standard-16",
        instance_count=2,
        provisioning_model=ProvisioningModel.SPOT,
        locations=AllCompatibleLocations(),
        attached_accelerator_type="nvidia-tesla-t4",
        attached_accelerator_count=2,
    )

    with pytest.raises(ValueError, match="shape must match"):
        obtainability_all_compatible_copy_cli(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123"),
            requirement,
            machine=machine,
            distribution_shape=DistributionShape.ANY_SINGLE_ZONE,
        )


def test_exact_slice_copy_cli_round_trips_canonical_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copied inspection retains canonical service, location, and dimensions."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    selector = QuotaInspectSelector(
        "compute",
        "GPUS-PER-GPU-FAMILY-per-project-region",
        "us-central1",
        NormalizedDimensions(
            (("gpu_family", "NVIDIA_H100"), ("region", "us-central1"))
        ),
    )

    copied = quota_inspect_copy_cli(
        scope,
        selector,
        presentation=CopyCliPresentation(output="json"),
    )
    arguments = __import__("shlex").split(copied)

    assert arguments[:3] == ["cqmgr", "quota", "inspect"]
    service_index = arguments.index("--service")
    assert arguments[service_index + 1] == "compute.googleapis.com"
    result = CliRunner().invoke(main, arguments[1:])
    assert result.exit_code == 0, result.output
    assert operations.inspect_calls[0][0] == selector


def test_workload_and_inspect_leaves_preserve_typed_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workload counts, location mode, and exact selector reach the facade."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )
    runner = CliRunner()

    compute = runner.invoke(
        main,
        [
            "q",
            "r",
            "ci",
            "--resource-scope",
            "projects/123",
            "--machine-type",
            "a3-highgpu-8g",
            "--instance-count",
            "2",
            "--provisioning-model",
            "spot",
            "--candidate",
            "us-central1-a",
            "--output",
            "json",
        ],
    )
    inspection = runner.invoke(
        main,
        [
            "q",
            "i",
            "--resource-scope",
            "projects/123",
            "--service",
            "compute",
            "--quota-id",
            "GPUS-PER-GPU-FAMILY-per-project-region",
            "--location",
            "us-central1",
            "--dimension",
            "region=us-central1",
            "--output",
            "json",
        ],
    )

    assert compute.exit_code == 0, compute.output
    requirement, _ = operations.resolve_calls[0]
    assert requirement.instance_count == INSTANCE_COUNT  # type: ignore[attr-defined]
    assert requirement.locations.values == ("us-central1-a",)  # type: ignore[attr-defined]
    assert inspection.exit_code == 0, inspection.output
    selector, _ = operations.inspect_calls[0]
    assert selector.service == "compute.googleapis.com"  # type: ignore[attr-defined]
    assert selector.dimensions.items == (("region", "us-central1"),)  # type: ignore[attr-defined]


def test_invalid_workload_location_mode_returns_typed_usage_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conflicting candidate modes never reach provider operations."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(
        main,
        [
            "quota",
            "resolve",
            "cloud-tpu-slice",
            "--resource-scope",
            "projects/123",
            "--accelerator-type",
            "v6e-8",
            "--topology",
            "2x4",
            "--runtime-version",
            "tpu-vm-base",
            "--slice-count",
            "1",
            "--provisioning-model",
            "standard",
            "--candidate",
            "us-central1-b",
            "--all-compatible-locations",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == USAGE_EXIT, result.output
    assert operations.resolve_calls == []
    assert operations.usage_calls[0][0] == "quota.resolve"
    assert "exactly one location mode" in operations.usage_calls[0][1]


def test_quota_leaf_parse_failures_return_typed_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed list, inspect, and Compute inputs never reach provider methods."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )
    runner = CliRunner()
    invocations = (
        (
            "quota.list",
            [
                "quota",
                "list",
                "--resource-scope",
                "projects/123",
                "--sort",
                "not-a-sort",
                "--output",
                "json",
            ],
        ),
        (
            "quota.inspect",
            [
                "quota",
                "inspect",
                "--resource-scope",
                "projects/123",
                "--service",
                "compute",
                "--quota-id",
                "quota-id",
                "--location",
                "global",
                "--dimension",
                "missing-equals",
                "--output",
                "json",
            ],
        ),
        (
            "quota.resolve",
            [
                "quota",
                "resolve",
                "compute-instance",
                "--resource-scope",
                "projects/123",
                "--machine-type",
                "a3-highgpu-8g",
                "--instance-count",
                "0",
                "--provisioning-model",
                "standard",
                "--candidate",
                "us-central1-a",
                "--output",
                "json",
            ],
        ),
    )

    for expected_operation, arguments in invocations:
        result = runner.invoke(main, arguments)

        assert result.exit_code == USAGE_EXIT, result.output
        assert operations.usage_calls[-1][0] == expected_operation

    assert operations.browse_calls == []
    assert operations.inspect_calls == []
    assert operations.resolve_calls == []


def test_cloud_tpu_all_compatible_shape_delegates_to_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complete TPU shape preserves its exhaustive location mode."""
    operations = RecordingReadOnlyOperations()
    monkeypatch.setattr(
        cli_module,
        "build_read_only_operations",
        lambda: operations,
    )

    result = CliRunner().invoke(
        main,
        [
            "q",
            "r",
            "ct",
            "--resource-scope",
            "projects/123",
            "--accelerator-type",
            "v6e-8",
            "--topology",
            "2x4",
            "--runtime-version",
            "tpu-vm-base",
            "--slice-count",
            "1",
            "--provisioning-model",
            "standard",
            "--all-compatible-locations",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    requirement, _ = operations.resolve_calls[0]
    assert requirement.kind.value == "cloud-tpu-slice"  # type: ignore[attr-defined]
    assert requirement.locations.mode.value == "all-compatible"  # type: ignore[attr-defined]
