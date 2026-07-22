"""Installation-local persistence and opaque quota-query cursors."""

import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

import cqmgr.adapters.persistence.quota_snapshots as persistence
from cqmgr.adapters.persistence.quota_snapshots import FilesystemQuotaQuerySnapshots
from cqmgr.application.ports.quota_snapshots import (
    ExpiredQuotaCursorError,
    ExpiredQuotaSnapshotError,
    MalformedQuotaCursorError,
    QuotaCursorQueryMismatchError,
    QuotaSnapshotConflictError,
    QuotaSnapshotNotFoundError,
    QuotaSnapshotOperationalError,
    QuotaSnapshotStoredDataError,
    ResolvedQuotaQueryCursor,
    UnknownQuotaCursorError,
    UnsupportedQuotaSnapshotSchemaError,
)
from cqmgr.domain.catalog import (
    ACCELERATOR_CATALOG_SCHEMA,
    CatalogMetadata,
    CatalogPredicates,
)
from cqmgr.domain.quota_queries import (
    QUOTA_QUERY_EVIDENCE_CONTRACT,
    QuerySnapshotMetadata,
    QuotaQuery,
    QuotaQueryItem,
    QuotaQuerySnapshot,
    ServiceSource,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

NOW = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)
TOKEN = "A" * 43
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _snapshot(*, expires_at: datetime | None = None) -> QuotaQuerySnapshot:
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    query = QuotaQuery(scope, ServiceSource("compute.googleapis.com"))
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
    return QuotaQuerySnapshot(
        QuerySnapshotMetadata(
            snapshot_id="snapshot-public-1",
            query=query,
            catalog=CatalogMetadata(
                ACCELERATOR_CATALOG_SCHEMA,
                "2026-07-22",
                "sha256:" + "c" * 64,
            ),
            evidence_contract=QUOTA_QUERY_EVIDENCE_CONTRACT,
            observed_at=datetime(2026, 7, 22, 8, tzinfo=UTC),
            expires_at=expires_at or datetime(2026, 7, 22, 9, tzinfo=UTC),
            complete=True,
        ),
        (item,),
    )


def test_repository_atomically_round_trips_private_canonical_snapshot(
    tmp_path: Path,
) -> None:
    """Snapshot bytes use private owner-only paths and leave no temp files."""
    root = tmp_path / "quota-query-snapshots"
    repository = FilesystemQuotaQuerySnapshots(root, token_factory=lambda: TOKEN)
    snapshot = _snapshot()

    repository.save(snapshot)
    loaded = repository.load(snapshot.metadata.snapshot_id, now=NOW)

    assert loaded == snapshot
    snapshot_files = list((root / "snapshots").glob("*.json"))
    assert len(snapshot_files) == 1
    assert json.loads(snapshot_files[0].read_bytes())["schema"] == (
        "cqmgr.quota-query-snapshot/v2"
    )
    assert list(root.rglob("*.tmp")) == []
    if stat.S_IMODE(root.stat().st_mode):
        assert stat.S_IMODE(root.stat().st_mode) == PRIVATE_DIRECTORY_MODE
        assert (
            stat.S_IMODE((root / "snapshots").stat().st_mode) == PRIVATE_DIRECTORY_MODE
        )
        assert stat.S_IMODE(snapshot_files[0].stat().st_mode) == PRIVATE_FILE_MODE


def test_repository_snapshot_id_is_immutable_and_idempotent(tmp_path: Path) -> None:
    """A snapshot ID may repeat identical bytes but can never change meaning."""
    repository = FilesystemQuotaQuerySnapshots(tmp_path / "snapshots")
    snapshot = _snapshot()
    repository.save(snapshot)
    repository.save(snapshot)

    conflicting = replace(
        snapshot,
        metadata=replace(snapshot.metadata, complete=False),
    )
    with pytest.raises(QuotaSnapshotConflictError):
        repository.save(conflicting)

    assert repository.load(snapshot.metadata.snapshot_id, now=NOW) == snapshot


def test_cursor_is_random_opaque_and_resolves_bound_query_and_offset(
    tmp_path: Path,
) -> None:
    """The public handle reveals neither snapshot identity nor logical offset."""
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: TOKEN,
    )
    snapshot = _snapshot()
    repository.save(snapshot)

    cursor = repository.issue(snapshot.metadata.snapshot_id, 1, now=NOW)
    resolved = repository.resolve(
        cursor.value,
        now=NOW,
        expected_query=snapshot.metadata.query,
    )

    assert cursor.value == TOKEN
    assert snapshot.metadata.snapshot_id not in cursor.value
    assert resolved.snapshot == snapshot
    assert resolved.offset == 1
    cursor_file = next((tmp_path / "snapshots" / "cursors").glob("*.json"))
    assert TOKEN not in cursor_file.read_text()
    assert stat.S_IMODE(cursor_file.stat().st_mode) == PRIVATE_FILE_MODE


def test_cursor_collision_retries_without_overwriting_existing_binding(
    tmp_path: Path,
) -> None:
    """Exclusive cursor publication preserves an existing random-token binding."""
    tokens = iter(("A" * 43, "A" * 43, "B" * 43))
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: next(tokens),
    )
    snapshot = _snapshot()
    repository.save(snapshot)
    first = repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)
    second = repository.issue(snapshot.metadata.snapshot_id, 1, now=NOW)

    assert first.value == "A" * 43
    assert second.value == "B" * 43
    assert repository.resolve(first.value, now=NOW).offset == 0
    assert repository.resolve(second.value, now=NOW).offset == 1


def test_cursor_rejects_query_mismatch_before_resolution(tmp_path: Path) -> None:
    """Explicit options must equal the installation-local cursor-bound query."""
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: TOKEN,
    )
    snapshot = _snapshot()
    repository.save(snapshot)
    cursor = repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)
    mismatch = replace(
        snapshot.metadata.query,
        source=ServiceSource("storage.googleapis.com"),
    )

    with pytest.raises(QuotaCursorQueryMismatchError):
        repository.resolve(cursor.value, now=NOW, expected_query=mismatch)


def test_cursor_rejects_malformed_unknown_and_expired_handles(tmp_path: Path) -> None:
    """Invalid local continuation state fails closed with typed outcomes."""
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: TOKEN,
    )
    snapshot = _snapshot()
    repository.save(snapshot)
    cursor = repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)

    with pytest.raises(MalformedQuotaCursorError):
        repository.resolve("../not-opaque", now=NOW)
    with pytest.raises(UnknownQuotaCursorError):
        repository.resolve("B" * 43, now=NOW)
    with pytest.raises(ExpiredQuotaCursorError):
        repository.resolve(
            cursor.value,
            now=datetime(2026, 7, 22, 9, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "now",
    [
        NOW.replace(tzinfo=None),
        datetime(2026, 7, 22, 4, 30, tzinfo=timezone(-timedelta(hours=4))),
    ],
)
def test_repository_rejects_non_utc_clock_values(tmp_path: Path, now: datetime) -> None:
    """Expiry decisions require explicit UTC rather than ambiguous local time."""
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: TOKEN,
    )
    snapshot = _snapshot()
    repository.save(snapshot)

    with pytest.raises(ValueError, match="now must be UTC"):
        repository.load(snapshot.metadata.snapshot_id, now=now)
    with pytest.raises(ValueError, match="now must be UTC"):
        repository.issue(snapshot.metadata.snapshot_id, 0, now=now)
    with pytest.raises(ValueError, match="now must be UTC"):
        repository.resolve(TOKEN, now=now)


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_repository_rejects_symlinked_snapshot_and_cursor_files(
    tmp_path: Path,
) -> None:
    """Reads fail closed when local state files are replaced by symlinks."""
    root = tmp_path / "snapshots"
    repository = FilesystemQuotaQuerySnapshots(root, token_factory=lambda: TOKEN)
    snapshot = _snapshot()
    repository.save(snapshot)
    cursor = repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)

    snapshot_path = next((root / "snapshots").glob("*.json"))
    snapshot_target = root / "snapshot-target.json"
    snapshot_path.rename(snapshot_target)
    snapshot_path.symlink_to(snapshot_target)
    with pytest.raises(QuotaSnapshotOperationalError, match="symlink"):
        repository.load(snapshot.metadata.snapshot_id, now=NOW)

    snapshot_path.unlink()
    snapshot_target.rename(snapshot_path)
    cursor_path = next((root / "cursors").glob("*.json"))
    cursor_target = root / "cursor-target.json"
    cursor_path.rename(cursor_target)
    cursor_path.symlink_to(cursor_target)
    with pytest.raises(QuotaSnapshotOperationalError, match="symlink"):
        repository.resolve(cursor.value, now=NOW)


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
@pytest.mark.parametrize("directory", ["snapshots", "cursors"])
def test_repository_rejects_symlinked_state_directories(
    tmp_path: Path,
    directory: str,
) -> None:
    """Descriptor-relative reads never follow replaced parent directories."""
    root = tmp_path / "state"
    repository = FilesystemQuotaQuerySnapshots(root, token_factory=lambda: TOKEN)
    snapshot = _snapshot()
    repository.save(snapshot)
    cursor = repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)
    state_directory = root / directory
    target = root / f"{directory}-target"
    state_directory.rename(target)
    state_directory.symlink_to(target, target_is_directory=True)

    if directory == "snapshots":
        with pytest.raises(QuotaSnapshotOperationalError):
            repository.load(snapshot.metadata.snapshot_id, now=NOW)
    else:
        with pytest.raises(QuotaSnapshotOperationalError):
            repository.resolve(cursor.value, now=NOW)


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_repository_rejects_symlinked_root_during_reads(tmp_path: Path) -> None:
    """The bound storage root itself cannot be replaced by a directory symlink."""
    root = tmp_path / "state"
    repository = FilesystemQuotaQuerySnapshots(root)
    snapshot = _snapshot()
    repository.save(snapshot)
    target = tmp_path / "state-target"
    root.rename(target)
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(QuotaSnapshotOperationalError):
        repository.load(snapshot.metadata.snapshot_id, now=NOW)


def test_repository_rejects_newer_and_corrupt_snapshot_state(tmp_path: Path) -> None:
    """Schema-newer and malformed persisted bytes never become partial evidence."""
    root = tmp_path / "snapshots"
    repository = FilesystemQuotaQuerySnapshots(root, token_factory=lambda: TOKEN)
    snapshot = _snapshot()
    repository.save(snapshot)
    path = next((root / "snapshots").glob("*.json"))

    document = json.loads(path.read_bytes())
    document["schema"] = "cqmgr.quota-query-snapshot/v3"
    path.write_text(json.dumps(document))
    with pytest.raises(UnsupportedQuotaSnapshotSchemaError):
        repository.load(snapshot.metadata.snapshot_id, now=NOW)

    path.write_text("not-json")
    with pytest.raises(QuotaSnapshotStoredDataError):
        repository.load(snapshot.metadata.snapshot_id, now=NOW)


def test_repository_boundaries_reject_wrong_types_and_unknown_snapshots(
    tmp_path: Path,
) -> None:
    """Application inputs cannot be coerced into repository identities or records."""
    with pytest.raises(TypeError, match="root must be a Path"):
        FilesystemQuotaQuerySnapshots(cast("Path", str(tmp_path)))
    repository = FilesystemQuotaQuerySnapshots(tmp_path / "snapshots")
    with pytest.raises(TypeError, match="QuotaQuerySnapshot"):
        repository.save(cast("QuotaQuerySnapshot", object()))
    for snapshot_id in ("", cast("str", 1)):
        with pytest.raises(ValueError, match="snapshot_id"):
            repository.load(snapshot_id, now=NOW)
    with pytest.raises(TypeError, match="now must be a datetime"):
        repository.load("snapshot-1", now=cast("datetime", "now"))
    with pytest.raises(QuotaSnapshotNotFoundError):
        repository.load("snapshot-unknown", now=NOW)


def test_repository_reports_direct_snapshot_expiry(tmp_path: Path) -> None:
    """Expired evidence cannot be loaded outside cursor resolution either."""
    repository = FilesystemQuotaQuerySnapshots(tmp_path / "snapshots")
    snapshot = _snapshot()
    repository.save(snapshot)

    with pytest.raises(ExpiredQuotaSnapshotError):
        repository.load(snapshot.metadata.snapshot_id, now=snapshot.metadata.expires_at)


@pytest.mark.parametrize("offset", [True, -1, 2, 1.5])
def test_cursor_issue_rejects_positions_outside_the_snapshot(
    tmp_path: Path,
    offset: object,
) -> None:
    """Continuation positions stay integral and within retained row bounds."""
    repository = FilesystemQuotaQuerySnapshots(tmp_path / "snapshots")
    snapshot = _snapshot()
    repository.save(snapshot)

    with pytest.raises(MalformedQuotaCursorError, match="outside"):
        repository.issue(snapshot.metadata.snapshot_id, cast("int", offset), now=NOW)


@pytest.mark.parametrize("token", ["short", "!" * 43, cast("str", 1)])
def test_cursor_issue_rejects_nonopaque_token_sources(
    tmp_path: Path,
    token: str,
) -> None:
    """A broken randomness source cannot publish guessable or malformed handles."""
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: token,
    )
    snapshot = _snapshot()
    repository.save(snapshot)

    with pytest.raises(MalformedQuotaCursorError, match="token source"):
        repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)


def test_cursor_issue_stops_after_repeated_random_collision(tmp_path: Path) -> None:
    """A stuck randomness source preserves the existing binding and fails closed."""
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: TOKEN,
    )
    snapshot = _snapshot()
    repository.save(snapshot)
    repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)

    with pytest.raises(QuotaSnapshotOperationalError, match="unique"):
        repository.issue(snapshot.metadata.snapshot_id, 1, now=NOW)
    assert repository.resolve(TOKEN, now=NOW).offset == 0


def test_cursor_resolution_rejects_missing_snapshot_and_out_of_range_binding(
    tmp_path: Path,
) -> None:
    """A binding never authorizes missing evidence or a position beyond its rows."""
    root = tmp_path / "snapshots"
    repository = FilesystemQuotaQuerySnapshots(root, token_factory=lambda: TOKEN)
    snapshot = _snapshot()
    repository.save(snapshot)
    cursor = repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)
    snapshot_path = next((root / "snapshots").glob("*.json"))
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_path.unlink()
    with pytest.raises(UnknownQuotaCursorError):
        repository.resolve(cursor.value, now=NOW)

    snapshot_path.write_bytes(snapshot_bytes)
    cursor_path = next((root / "cursors").glob("*.json"))
    cursor_path.write_bytes(
        b'{"offset":2,"schema":"cqmgr.quota-query-cursor/v1",'
        b'"snapshot_id":"snapshot-public-1"}\n'
    )
    with pytest.raises(QuotaSnapshotStoredDataError, match="exceeds"):
        repository.resolve(cursor.value, now=NOW)


def test_repository_rejects_mismatched_snapshot_identity_on_disk(
    tmp_path: Path,
) -> None:
    """The hashed lookup key cannot be rebound by copying another valid record."""
    repository = FilesystemQuotaQuerySnapshots(tmp_path / "snapshots")
    snapshot = _snapshot()
    repository.save(snapshot)
    stored_path = next((tmp_path / "snapshots" / "snapshots").glob("*.json"))
    requested_path = repository._snapshot_path("snapshot-other")  # noqa: SLF001
    requested_path.write_bytes(stored_path.read_bytes())

    with pytest.raises(QuotaSnapshotStoredDataError, match="identity"):
        repository.load("snapshot-other", now=NOW)


def test_repository_translates_filesystem_failures_and_cleans_temporary_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed publication returns an operational outcome without partial files."""
    repository = FilesystemQuotaQuerySnapshots(tmp_path / "snapshots")
    snapshot = _snapshot()

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(persistence.os, "link", fail_link)
    with pytest.raises(QuotaSnapshotOperationalError, match="PermissionError"):
        repository.save(snapshot)
    assert list((tmp_path / "snapshots").rglob("*.tmp")) == []


def test_repository_rejects_symlinked_storage_directory(tmp_path: Path) -> None:
    """Installation-local state cannot be redirected through a directory symlink."""
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "snapshots"
    root.symlink_to(target, target_is_directory=True)
    repository = FilesystemQuotaQuerySnapshots(root)

    with pytest.raises(QuotaSnapshotOperationalError, match="symlink"):
        repository.save(_snapshot())


def test_repository_translates_read_and_cursor_publication_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem denial remains a typed local failure at every public operation."""
    repository = FilesystemQuotaQuerySnapshots(
        tmp_path / "snapshots",
        token_factory=lambda: TOKEN,
    )
    snapshot = _snapshot()
    repository.save(snapshot)

    def deny_read(_root: Path, _directory: str, _filename: str) -> bytes:
        raise PermissionError

    monkeypatch.setattr(persistence, "_read_repository_file", deny_read)
    with pytest.raises(QuotaSnapshotOperationalError, match="PermissionError"):
        repository.load(snapshot.metadata.snapshot_id, now=NOW)
    with pytest.raises(QuotaSnapshotOperationalError, match="PermissionError"):
        repository.resolve(TOKEN, now=NOW)
    with pytest.raises(QuotaSnapshotOperationalError, match="PermissionError"):
        repository.save(snapshot)

    monkeypatch.undo()

    original_publish = persistence._publish_exclusive  # noqa: SLF001

    def selectively_deny_cursor(path: Path, data: bytes) -> None:
        if path.parent.name == "cursors":
            raise PermissionError
        original_publish(path, data)

    monkeypatch.setattr(persistence, "_publish_exclusive", selectively_deny_cursor)
    with pytest.raises(QuotaSnapshotOperationalError, match="PermissionError"):
        repository.issue(snapshot.metadata.snapshot_id, 0, now=NOW)


def test_repository_rejects_nonregular_state_and_unusable_root(tmp_path: Path) -> None:
    """State reads and private-directory preparation reject filesystem aliases."""
    root = tmp_path / "snapshots"
    repository = FilesystemQuotaQuerySnapshots(root)
    snapshot = _snapshot()
    repository.save(snapshot)
    snapshot_path = next((root / "snapshots").glob("*.json"))
    snapshot_path.unlink()
    snapshot_path.mkdir()
    with pytest.raises(QuotaSnapshotOperationalError, match="regular file"):
        repository.load(snapshot.metadata.snapshot_id, now=NOW)

    unusable = tmp_path / "not-a-directory"
    unusable.write_text("occupied")
    with pytest.raises(QuotaSnapshotOperationalError, match="FileExistsError"):
        FilesystemQuotaQuerySnapshots(unusable).save(snapshot)


def test_resolved_cursor_requires_a_snapshot_and_nonnegative_offset() -> None:
    """The application cursor port exposes only typed validated resolutions."""
    snapshot = _snapshot()
    assert ResolvedQuotaQueryCursor(snapshot, 0).snapshot == snapshot
    with pytest.raises(TypeError, match="snapshot"):
        ResolvedQuotaQueryCursor(cast("QuotaQuerySnapshot", object()), 0)
    for offset in (True, -1, 1.5):
        with pytest.raises(ValueError, match="offset"):
            ResolvedQuotaQueryCursor(snapshot, cast("int", offset))
