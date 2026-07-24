"""Authenticated durable Watch observation checkpoints."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cqmgr.adapters.persistence.watch import LocalWatchCheckpointRepository
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.application.ports.watch import WatchCheckpointRepositoryStatus
from cqmgr.domain.apply_records import ApplyChildDisposition
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import QuotaRequestStatus, Reconciliation, WatchCondition
from cqmgr.domain.watch import (
    WatchAggregate,
    WatchCheckpoint,
    WatchChildIdentity,
    WatchChildLineage,
    WatchChildSummary,
    WatchSubject,
)

NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
KEY = SecretValue(b"w" * 32)
UNIT = QuotaUnit("1")


def _checkpoint() -> WatchCheckpoint:
    child = WatchChildIdentity(
        child_id="direct",
        order=0,
        slice_identity=EffectiveQuotaSliceIdentity(
            SCOPE,
            "compute.googleapis.com",
            "GPU-DIRECT",
            NormalizedDimensions((("region", "us-central1"),)),
            QuotaScope.REGIONAL,
        ),
        target=QuotaQuantity(8, UNIT),
        disposition=ApplyChildDisposition.ACCEPTED,
        preference_identity=(
            "projects/123456789/locations/global/quotaPreferences/direct"
        ),
        lineage_etag="apply-etag",
        lineage_trace_id=None,
        baseline=QuotaQuantity(4, UNIT),
    )
    subject = WatchSubject(
        kind=PlanKind.SINGLE,
        resource_scope=SCOPE,
        condition=WatchCondition.FULFILLED,
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        children=(child,),
    )
    status = QuotaRequestStatus.derive(
        reconciliation=Reconciliation.RECONCILING,
        baseline=QuotaQuantity(4, UNIT),
        desired=QuotaQuantity(8, UNIT),
        granted=None,
        effective=None,
        status_observed_at=NOW,
        effective_observed_at=None,
    )
    aggregate = WatchAggregate.derive(
        subject,
        (WatchChildSummary(child, status),),
    )
    return WatchCheckpoint(
        checkpoint_id="sha256:" + ("c" * 64),
        installation_id="installation-123",
        subject=subject,
        aggregate=aggregate,
        lineages=(WatchChildLineage("direct", "observed-etag", None),),
        sequence=4,
        saved_at=NOW,
    )


def test_checkpoint_round_trips_authentically_and_is_immutable(
    tmp_path: Path,
) -> None:
    """Resume can recover the exact durable aggregate but cannot replace it."""
    repository = LocalWatchCheckpointRepository(tmp_path)
    checkpoint = _checkpoint()

    stored = repository.save(checkpoint, KEY)
    idempotent = repository.save(checkpoint, KEY)
    loaded = repository.load(checkpoint.checkpoint_id, KEY)
    conflict = repository.save(replace(checkpoint, sequence=5), KEY)

    assert stored.status is WatchCheckpointRepositoryStatus.STORED
    assert idempotent.status is WatchCheckpointRepositoryStatus.STORED
    assert loaded.status is WatchCheckpointRepositoryStatus.AVAILABLE
    assert loaded.checkpoint == checkpoint
    assert conflict.status is WatchCheckpointRepositoryStatus.CONFLICT


def test_checkpoint_rejects_foreign_keys_tampering_and_addresses(
    tmp_path: Path,
) -> None:
    """Only canonical, locally authenticated checkpoints are resumable."""
    repository = LocalWatchCheckpointRepository(tmp_path)
    checkpoint = _checkpoint()
    assert repository.save(checkpoint, KEY).status is (
        WatchCheckpointRepositoryStatus.STORED
    )
    path = (
        tmp_path
        / "watch-checkpoints"
        / f"{checkpoint.checkpoint_id.removeprefix('sha256:')}.json"
    )
    original = path.read_text()

    assert (
        repository.load(
            checkpoint.checkpoint_id,
            SecretValue(b"x" * 32),
        ).status
        is WatchCheckpointRepositoryStatus.CONFLICT
    )
    assert repository.load("invalid", KEY).status is (
        WatchCheckpointRepositoryStatus.FAILED
    )

    envelope = json.loads(original)
    envelope["checkpoint"]["sequence"] = 99
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)
    assert repository.load(checkpoint.checkpoint_id, KEY).status is (
        WatchCheckpointRepositoryStatus.CONFLICT
    )

    path.write_text("{")
    path.chmod(0o600)
    assert repository.load(checkpoint.checkpoint_id, KEY).status is (
        WatchCheckpointRepositoryStatus.FAILED
    )


@pytest.mark.skipif(
    os.name == "nt", reason="Windows privacy uses ACLs, not POSIX modes"
)
def test_checkpoint_rejects_posix_mode_drift(tmp_path: Path) -> None:
    """A checkpoint with widened POSIX permissions is not resumable."""
    repository = LocalWatchCheckpointRepository(tmp_path)
    checkpoint = _checkpoint()
    assert repository.save(checkpoint, KEY).status is (
        WatchCheckpointRepositoryStatus.STORED
    )
    path = (
        tmp_path
        / "watch-checkpoints"
        / f"{checkpoint.checkpoint_id.removeprefix('sha256:')}.json"
    )

    path.chmod(0o644)

    assert repository.load(checkpoint.checkpoint_id, KEY).status is (
        WatchCheckpointRepositoryStatus.CONFLICT
    )
