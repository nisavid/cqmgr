"""Installed-style cursor continuation without provider runtime dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cqmgr.adapters.persistence.quota_snapshots import (
    FilesystemQuotaQuerySnapshots,
)
from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.quota_queries import (
    QUOTA_QUERY_EVIDENCE_CONTRACT,
    ProviderSourceCoverage,
    QuerySnapshotMetadata,
    QuotaQuery,
    QuotaQueryFilters,
    QuotaQueryItem,
    QuotaQuerySnapshot,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from pathlib import Path


def _retained_cursor(root: Path) -> str:
    now = datetime.now(UTC)
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    query = QuotaQuery(
        scope,
        filters=QuotaQueryFilters(services=("compute",)),
    )
    item = QuotaQueryItem(
        identity=EffectiveQuotaSliceIdentity(
            scope,
            "compute.googleapis.com",
            "GPUS-ALL-REGIONS-per-project",
            NormalizedDimensions(),
            QuotaScope.UNKNOWN,
        ),
        display_name="GPUs all regions",
        accelerator_id=None,
        location="global",
        quota_pool="standard",
        predicates=CatalogPredicates(
            discovered=True,
            cataloged=False,
            guided=False,
            mutable=True,
        ),
        effective_value=QuotaQuantity(128, QuotaUnit("1")),
    )
    snapshot = QuotaQuerySnapshot(
        QuerySnapshotMetadata(
            snapshot_id="snapshot-offline-cursor",
            query=query,
            catalog=CatalogMetadata(
                ACCELERATOR_CATALOG_SCHEMA,
                "2026-07-23",
                "sha256:" + ("c" * 64),
            ),
            evidence_contract=QUOTA_QUERY_EVIDENCE_CONTRACT,
            observed_at=now,
            expires_at=now + timedelta(minutes=15),
            complete=True,
            source_coverage=(
                ProviderSourceCoverage.complete(
                    "compute.googleapis.com",
                    pages_attempted=1,
                    pages_completed=1,
                    observed_at=now,
                ),
                ProviderSourceCoverage.intentionally_unqueried("tpu.googleapis.com"),
            ),
        ),
        (item,),
    )
    repository = FilesystemQuotaQuerySnapshots(
        root,
        token_factory=lambda: "A" * 43,
    )
    repository.save(snapshot)
    return repository.issue(snapshot.metadata.snapshot_id, 0, now=now).value


def test_cursor_only_cli_continues_without_google_or_budget_state(
    tmp_path: Path,
) -> None:
    """A retained cursor remains usable when provider bootstrap is unavailable."""
    snapshot_root = tmp_path / "quota-snapshots"
    cursor = _retained_cursor(snapshot_root)
    blocked_budget_path = tmp_path / "budget-is-not-a-directory"
    blocked_budget_path.write_text("must remain untouched")
    script = """
import sys

class BlockGoogleImports:
    def find_spec(self, fullname, path, target=None):
        if fullname == "google" or fullname.startswith("google."):
            raise AssertionError(f"cursor continuation imported {fullname}")
        return None

sys.meta_path.insert(0, BlockGoogleImports())

from cqmgr.cli import main

main(sys.argv[1:], prog_name="cqmgr")
"""
    environment = os.environ.copy()
    environment.update(
        {
            "CQMGR_BUDGET_PATH": str(blocked_budget_path),
            "CQMGR_QUOTA_SNAPSHOT_PATH": str(snapshot_root),
        }
    )

    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            script,
            "quota",
            "list",
            "--cursor",
            cursor,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema"] == "cqmgr.operation-result/v1"
    assert payload["outcome"] == {"code": "page-read", "exit_class": 0}
    assert payload["data"]["items"][0]["identity"]["service"] == (
        "compute.googleapis.com"
    )
    assert completed.stderr == ""
    assert blocked_budget_path.read_text() == "must remain untouched"
