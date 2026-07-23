"""Create secret-safe local read-only fixtures through an installed artifact."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cqmgr
from cqmgr.adapters.persistence.audit import FilesystemAuditJournal
from cqmgr.adapters.persistence.quota_snapshots import (
    FilesystemQuotaQuerySnapshots,
)
from cqmgr.domain.audit import (
    AuditFact,
    AuditFactName,
    AuditRecordDraft,
    AuditRecordKind,
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
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import OperationName, StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

SENSITIVE_VALUE = "private.operator@example.com"


def _audit_fixture(root: Path, now: datetime) -> str:
    journal = FilesystemAuditJournal(root)
    record = journal.append(
        AuditRecordDraft(
            kind=AuditRecordKind.PREVIEW_EVIDENCE,
            operation=OperationName("quota.preview"),
            resource_scope=ResourceScope(
                ResourceScopeKind.PROJECT,
                "projects/123",
            ),
            occurred_at=now,
            outcome=StableSymbol("succeeded"),
            correlation_id=RedactedText("ya29.private-access-token"),
            facts=(
                AuditFact(
                    AuditFactName.SOURCE,
                    RedactedText(SENSITIVE_VALUE),
                ),
                AuditFact(
                    AuditFactName.PROVIDER_BODY,
                    RedactedText('{"private":"provider-body"}'),
                ),
            ),
        ),
        sensitive_values=(SENSITIVE_VALUE,),
    )
    return record.record_id


def _quota_cursor_fixture(root: Path, now: datetime) -> str:
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
            snapshot_id="snapshot-installed-smoke",
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


def main() -> None:
    """Create installed-package fixtures and return their public identities."""
    parser = argparse.ArgumentParser()
    parser.add_argument("audit_root", type=Path)
    parser.add_argument("snapshot_root", type=Path)
    parser.add_argument("checkout", type=Path)
    arguments = parser.parse_args()
    package_path = Path(cqmgr.__file__).resolve()
    assert not package_path.is_relative_to(arguments.checkout.resolve())
    now = datetime.now(UTC)
    sys.stdout.write(
        json.dumps(
            {
                "audit_record_id": _audit_fixture(arguments.audit_root, now),
                "cursor": _quota_cursor_fixture(arguments.snapshot_root, now),
            },
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
