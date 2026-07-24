"""Authenticated crash-safe Apply record persistence."""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cqmgr.adapters.persistence import apply_records as persistence
from cqmgr.adapters.persistence.apply_records import LocalApplyRecordRepository
from cqmgr.application.ports.apply_records import ApplyRecordRepositoryStatus
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.apply_records import (
    ApplyChildRecord,
    ApplyRecord,
    UnknownDispatchResolution,
)
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 24, 1, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
KEY = SecretValue(b"k" * 32)


def _record() -> ApplyRecord:
    return ApplyRecord(
        intent_id="sha256:" + ("a" * 64),
        plan_digest="sha256:" + ("b" * 64),
        kind=PlanKind.SINGLE,
        resource_scope=SCOPE,
        created_at=NOW,
        children=(
            ApplyChildRecord(
                child_id="single",
                slice_identity=EffectiveQuotaSliceIdentity(
                    SCOPE,
                    "compute.googleapis.com",
                    "GPU-DIRECT",
                    NormalizedDimensions((("region", "us-central1"),)),
                    QuotaScope.REGIONAL,
                ),
                target=QuotaQuantity(8, QuotaUnit("1")),
                preference_identity=(
                    "projects/123456789/locations/global/quotaPreferences/cqmgr-opaque"
                ),
                etag=None,
            ),
        ),
    )


def test_apply_record_round_trips_and_rejects_stale_revision(tmp_path: Path) -> None:
    """Every transition is authenticated, atomic, and monotonic."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()

    created = repository.create(record, KEY)
    loaded = repository.load(record.intent_id, KEY)
    updated_record = record.record_dispatch_intent("single", NOW)
    updated = repository.save(updated_record, KEY)
    stale = repository.save(updated_record, KEY)

    assert created.status is ApplyRecordRepositoryStatus.STORED
    assert loaded.status is ApplyRecordRepositoryStatus.AVAILABLE
    assert loaded.record == record
    assert updated.status is ApplyRecordRepositoryStatus.STORED
    assert stale.status is ApplyRecordRepositoryStatus.CONFLICT
    assert repository.load(record.intent_id, SecretValue(b"x" * 32)).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )


def test_apply_record_repository_rejects_invalid_missing_and_conflicting_inputs(
    tmp_path: Path,
) -> None:
    """Invalid addresses, keys, revisions, and identities fail closed."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    next_record = record.record_dispatch_intent("single", NOW)

    assert repository.load("invalid", KEY).status is (
        ApplyRecordRepositoryStatus.FAILED
    )
    assert repository.load(record.intent_id, SecretValue(b"short")).status is (
        ApplyRecordRepositoryStatus.FAILED
    )
    assert repository.load(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.MISSING
    )
    assert repository.create(next_record, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )
    assert repository.save(next_record, KEY).status is (
        ApplyRecordRepositoryStatus.MISSING
    )

    assert repository.create(record, KEY).status is (ApplyRecordRepositoryStatus.STORED)
    assert repository.create(record, KEY).status is (ApplyRecordRepositoryStatus.STORED)
    conflict = replace(
        record,
        plan_digest="sha256:" + ("c" * 64),
    )
    assert repository.create(conflict, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )
    altered = replace(next_record, plan_digest=conflict.plan_digest)
    assert repository.save(altered, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )
    assert repository.save(next_record, SecretValue(b"short")).status is (
        ApplyRecordRepositoryStatus.FAILED
    )


def test_apply_record_repository_detects_tampering_and_private_mode_drift(
    tmp_path: Path,
) -> None:
    """Envelope, authenticator, and permission changes never yield records."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    assert repository.create(record, KEY).status is (ApplyRecordRepositoryStatus.STORED)
    path = (
        tmp_path / "apply-records" / f"{record.intent_id.removeprefix('sha256:')}.json"
    )
    original = path.read_text()

    path.chmod(0o644)
    assert repository.load(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )
    path.chmod(0o600)

    envelope = json.loads(original)
    envelope["authentication"] = "hmac-sha256:" + ("0" * 64)
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)
    assert repository.load(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )

    path.write_text("{")
    path.chmod(0o600)
    assert repository.load(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.FAILED
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda envelope: envelope.update(schema="unknown"),
        lambda envelope: envelope.update(extra=True),
        lambda envelope: envelope.update(record=[]),
    ],
)
def test_apply_record_repository_rejects_unknown_envelope_shapes(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    """Only the exact authenticated V1 envelope is accepted."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    repository.create(record, KEY)
    path = (
        tmp_path / "apply-records" / f"{record.intent_id.removeprefix('sha256:')}.json"
    )
    envelope = json.loads(path.read_text())
    mutation(envelope)
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)

    assert repository.load(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )


def test_apply_record_repository_classifies_storage_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read and publish failures remain typed and never expose a partial record."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(persistence, "_publish", fail_publish)
    assert repository.create(record, KEY).status is (ApplyRecordRepositoryStatus.FAILED)


def test_unknown_resolution_journal_is_append_only_and_replay_independent(
    tmp_path: Path,
) -> None:
    """Older authentic Apply state cannot erase or replace resolution proof."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    repository.create(record, KEY)
    record_path = (
        tmp_path / "apply-records" / f"{record.intent_id.removeprefix('sha256:')}.json"
    )
    older_authentic_state = record_path.read_bytes()

    appended = repository.append_unknown_resolution(
        record.intent_id,
        "single",
        UnknownDispatchResolution.ACCEPTED,
        NOW,
        KEY,
    )
    record_path.write_bytes(older_authentic_state)
    record_path.chmod(0o600)
    loaded = repository.load_unknown_resolutions(record.intent_id, KEY)
    conflicting = repository.append_unknown_resolution(
        record.intent_id,
        "single",
        UnknownDispatchResolution.FAILED,
        NOW,
        KEY,
    )

    assert appended.status is ApplyRecordRepositoryStatus.STORED
    assert loaded.status is ApplyRecordRepositoryStatus.AVAILABLE
    assert len(loaded.resolutions) == 1
    assert loaded.resolutions[0].resolution is UnknownDispatchResolution.ACCEPTED
    assert conflicting.status is ApplyRecordRepositoryStatus.CONFLICT
