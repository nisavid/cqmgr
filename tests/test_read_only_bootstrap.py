"""Composition-root paths for the read-only vertical slice."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cqmgr.application.operations.audit import AuditOperations
from cqmgr.bootstrap import build_audit_operations, runtime_paths
from cqmgr.domain.audit import AuditQuery
from cqmgr.domain.results import ExitClass

if TYPE_CHECKING:
    from pathlib import Path


def test_read_only_state_paths_accept_explicit_environment_overrides(
    tmp_path: Path,
) -> None:
    """Tests and operators can isolate every installation-local read store."""
    paths = runtime_paths(
        {
            "CQMGR_CONFIG_PATH": str(tmp_path / "config.toml"),
            "CQMGR_SELECTION_STATE_PATH": str(tmp_path / "selection.toml"),
            "CQMGR_AUDIT_PATH": str(tmp_path / "audit"),
            "CQMGR_QUOTA_SNAPSHOT_PATH": str(tmp_path / "quota-snapshots"),
            "CQMGR_BUDGET_PATH": str(tmp_path / "budgets"),
        }
    )

    assert paths.configuration == tmp_path / "config.toml"
    assert paths.selection_state == tmp_path / "selection.toml"
    assert paths.audit == tmp_path / "audit"
    assert paths.quota_snapshots == tmp_path / "quota-snapshots"
    assert paths.budgets == tmp_path / "budgets"


def test_audit_operations_are_composed_without_provider_dependencies(
    tmp_path: Path,
) -> None:
    """The offline audit command constructs only local persistence and clock."""
    operations = build_audit_operations(
        {
            "CQMGR_AUDIT_PATH": str(tmp_path / "audit"),
        }
    )

    assert isinstance(operations, AuditOperations)
    assert not (tmp_path / "audit").exists()


def test_unavailable_audit_storage_becomes_a_typed_operation_failure(
    tmp_path: Path,
) -> None:
    """Composition keeps an unusable audit path behind the result boundary."""
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked")

    operations = build_audit_operations(
        {
            "CQMGR_AUDIT_PATH": str(blocked_parent / "audit"),
        }
    )
    listed = asyncio.run(operations.list(AuditQuery()))
    inspected = asyncio.run(operations.inspect("record-1"))
    verified = asyncio.run(operations.verify())

    assert {result.outcome.code.value for result in (listed, inspected, verified)} == {
        "audit-journal-unavailable"
    }
    assert {result.outcome.exit_class for result in (listed, inspected, verified)} == {
        ExitClass.OPERATIONAL_FAILURE
    }
    assert listed.data.reason == "audit-journal-unavailable"
