"""Authenticated crash-safe Apply record persistence."""

import hmac
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

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
INVALID_SCALAR = 42


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
    assert repository.create(record, SecretValue(b"short")).status is (
        ApplyRecordRepositoryStatus.FAILED
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
    assert repository.load_unknown_resolutions("invalid", KEY).status is (
        ApplyRecordRepositoryStatus.FAILED
    )
    assert repository.load_unknown_resolutions(record.intent_id, KEY).resolutions == ()
    assert (
        repository.append_unknown_resolution(
            "invalid",
            "single",
            UnknownDispatchResolution.ACCEPTED,
            NOW,
            KEY,
        ).status
        is ApplyRecordRepositoryStatus.FAILED
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


def test_apply_record_repository_classifies_update_and_resolution_publish_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every failed atomic publication remains a typed local failure."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    repository.create(record, KEY)

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(persistence, "_publish", fail_publish)

    assert (
        repository.save(
            record.record_dispatch_intent("single", NOW),
            KEY,
        ).status
        is ApplyRecordRepositoryStatus.FAILED
    )
    assert (
        repository.append_unknown_resolution(
            record.intent_id,
            "single",
            UnknownDispatchResolution.ACCEPTED,
            NOW,
            KEY,
        ).status
        is ApplyRecordRepositoryStatus.FAILED
    )


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
    idempotent = repository.append_unknown_resolution(
        record.intent_id,
        "single",
        UnknownDispatchResolution.ACCEPTED,
        NOW,
        KEY,
    )

    assert appended.status is ApplyRecordRepositoryStatus.STORED
    assert idempotent.status is ApplyRecordRepositoryStatus.STORED
    assert loaded.status is ApplyRecordRepositoryStatus.AVAILABLE
    assert len(loaded.resolutions) == 1
    assert loaded.resolutions[0].resolution is UnknownDispatchResolution.ACCEPTED
    assert conflicting.status is ApplyRecordRepositoryStatus.CONFLICT


def _authenticate(mapping: dict[str, object]) -> str:
    data = json.dumps(
        mapping,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"hmac-sha256:{hmac.digest(KEY.reveal(), data, 'sha256').hex()}"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update(children={}),
        lambda record: record["children"][0]["slice_identity"].update(dimensions={}),
        lambda record: record["children"][0]["slice_identity"].update(
            dimensions=[["region"]]
        ),
        lambda record: record.update(resource_scope=[]),
        lambda record: record["children"][0]["target"].update(value=True),
        lambda record: record["children"][0].update(preference_existed="false"),
        lambda record: record["children"][0].update(child_id=INVALID_SCALAR),
        lambda record: record.update(created_at="2026-07-24T01:00:00+00:00"),
        lambda record: record["children"][0].update(etag=INVALID_SCALAR),
    ],
)
def test_authenticated_apply_record_schema_corruption_fails_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    """Authenticated but malformed state never crosses the repository boundary."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    repository.create(record, KEY)
    path = (
        tmp_path / "apply-records" / f"{record.intent_id.removeprefix('sha256:')}.json"
    )
    envelope = json.loads(path.read_text())
    mapping = envelope["record"]
    mutation(mapping)
    envelope["authentication"] = _authenticate(mapping)
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)

    assert repository.load(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda envelope: envelope.update(extra=True),
        lambda envelope: envelope.update(schema="unknown"),
        lambda envelope: envelope.update(resolution=[]),
        lambda envelope: envelope.update(authentication=INVALID_SCALAR),
        lambda envelope: envelope["resolution"].update(child_id=INVALID_SCALAR),
        lambda envelope: envelope["resolution"].update(checkpoint=True),
        lambda envelope: envelope["resolution"].update(
            recorded_at="2026-07-24T01:00:00+00:00"
        ),
    ],
)
def test_unknown_resolution_schema_corruption_fails_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    """Malformed append-only reconciliation evidence is never accepted."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    repository.append_unknown_resolution(
        record.intent_id,
        "single",
        UnknownDispatchResolution.ACCEPTED,
        NOW,
        KEY,
    )
    directory = (
        tmp_path / "apply-resolutions" / record.intent_id.removeprefix("sha256:")
    )
    path = next(directory.glob("*.json"))
    envelope = json.loads(path.read_text())
    mutation(envelope)
    mapping = envelope.get("resolution")
    if isinstance(mapping, dict) and envelope.get("authentication") != INVALID_SCALAR:
        envelope["authentication"] = _authenticate(mapping)
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)

    assert repository.load_unknown_resolutions(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )


def test_atomic_publish_cleanup_and_resolution_permissions_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupted publication and non-private evidence leave no accepted state."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    original_link = persistence.os.link

    def fail_link(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError

    monkeypatch.setattr(persistence.os, "link", fail_link)
    assert repository.create(record, KEY).status is ApplyRecordRepositoryStatus.FAILED
    assert list((tmp_path / "apply-records").glob(".*.tmp")) == []
    monkeypatch.setattr(persistence.os, "link", original_link)

    repository.append_unknown_resolution(
        record.intent_id,
        "single",
        UnknownDispatchResolution.ACCEPTED,
        NOW,
        KEY,
    )
    directory = (
        tmp_path / "apply-resolutions" / record.intent_id.removeprefix("sha256:")
    )
    path = next(directory.glob("*.json"))
    path.chmod(0o644)

    assert repository.load_unknown_resolutions(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.CONFLICT
    )


def test_repository_read_failures_and_invalid_runtime_key_remain_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem read loss and wrong runtime key types never escape the port."""
    repository = LocalApplyRecordRepository(tmp_path)
    record = _record()
    repository.create(record, KEY)
    repository.append_unknown_resolution(
        record.intent_id,
        "single",
        UnknownDispatchResolution.ACCEPTED,
        NOW,
        KEY,
    )
    updated = record.record_dispatch_intent("single", NOW)

    assert (
        repository.load(
            record.intent_id,
            cast("SecretValue", object()),
        ).status
        is ApplyRecordRepositoryStatus.FAILED
    )

    original_read_text = Path.read_text

    def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
        del self, args, kwargs
        raise OSError

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    assert repository.load(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.FAILED
    )
    assert repository.save(updated, KEY).status is ApplyRecordRepositoryStatus.FAILED
    assert repository.load_unknown_resolutions(record.intent_id, KEY).status is (
        ApplyRecordRepositoryStatus.FAILED
    )
    monkeypatch.setattr(Path, "read_text", original_read_text)

    path = (
        tmp_path / "apply-records" / f"{record.intent_id.removeprefix('sha256:')}.json"
    )
    path.chmod(0o644)
    assert repository.save(updated, KEY).status is ApplyRecordRepositoryStatus.CONFLICT


def test_windows_directory_sync_is_an_explicit_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows publication does not attempt POSIX directory fsync."""
    monkeypatch.setattr(persistence.os, "name", "nt")

    persistence._fsync_directory(tmp_path)  # noqa: SLF001
