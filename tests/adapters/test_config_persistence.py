"""Real-filesystem configuration and selection-state contracts."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from inspect import iscoroutinefunction
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cqmgr.adapters.persistence.configuration import (
    InvalidStoredDataError,
    StoredDataOperationalError,
    TomlConfigRepository,
    TomlSelectionStateRepository,
    UnsupportedStoredSchemaError,
)
from cqmgr.application.configuration import (
    ConfigSnapshot,
    InterfaceSettingKey,
    Profile,
    QuotaContactKeyringReference,
    SelectionState,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import Coroutine


def project(identifier: str) -> ResourceScope:
    """Build one canonical project resource scope."""
    return ResourceScope(ResourceScopeKind.PROJECT, f"projects/{identifier}")


def run[ResultT](awaitable: Coroutine[object, object, ResultT]) -> ResultT:
    """Run one repository coroutine at the public persistence boundary."""
    return asyncio.run(awaitable)


def test_persistence_ports_are_async_first() -> None:
    """Filesystem repositories expose coroutine read and update boundaries."""
    assert iscoroutinefunction(TomlConfigRepository.read)
    assert iscoroutinefunction(TomlConfigRepository.update)
    assert iscoroutinefunction(TomlSelectionStateRepository.read)
    assert iscoroutinefunction(TomlSelectionStateRepository.update)


def test_missing_files_load_independent_empty_snapshots(tmp_path: Path) -> None:
    """A first run performs no implicit write and has no ambient selection."""
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "selection.toml"

    assert run(TomlConfigRepository(config_path).read()) == ConfigSnapshot()
    assert run(TomlSelectionStateRepository(state_path).read()) == SelectionState()
    assert not config_path.exists()
    assert not state_path.exists()


def test_v0_configuration_is_migrated_in_memory_and_written_as_v1(
    tmp_path: Path,
) -> None:
    """The supported forward migration renames legacy project profile fields."""
    path = tmp_path / "config.toml"
    path.write_text(
        'schema = "cqmgr.config/v0"\n\n[profiles.primary]\nproject = "projects/123"\n'
    )
    repository = TomlConfigRepository(path)

    migrated = run(repository.read())
    run(repository.update(lambda snapshot: snapshot))

    assert migrated.profile("primary").resource_scope == project("123")
    assert 'schema = "cqmgr.config/v1"' in path.read_text()
    assert 'resource_scope = "projects/123"' in path.read_text()
    assert "project =" not in path.read_text()


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        ('schema = "cqmgr.config/v2"\n', UnsupportedStoredSchemaError),
        ('schema = "cqmgr.config/v1"\nsecret = "value"\n', InvalidStoredDataError),
        (
            'schema = "cqmgr.config/v1"\n[interface]\nno_color = "yes"\n',
            InvalidStoredDataError,
        ),
        ('schema = "not-cqmgr"\n', InvalidStoredDataError),
    ],
)
def test_configuration_rejects_invalid_or_newer_versions(
    tmp_path: Path,
    contents: str,
    error: type[Exception],
) -> None:
    """Stored data fails closed instead of guessing or silently downgrading."""
    path = tmp_path / "config.toml"
    path.write_text(contents)

    with pytest.raises(error):
        run(TomlConfigRepository(path).read())


def test_atomic_updates_recover_stale_temporary_files(tmp_path: Path) -> None:
    """A pre-replace crash artifact never replaces the last valid snapshot."""
    path = tmp_path / "config.toml"
    repository = TomlConfigRepository(path)
    run(
        repository.update(
            lambda snapshot: ConfigSnapshot(
                profiles=snapshot.profiles,
                interface=snapshot.interface.replace(
                    InterfaceSettingKey.NO_COLOR,
                    value=True,
                ),
            )
        )
    )
    stale = tmp_path / ".config.toml.crashed.tmp"
    stale.write_text("truncated")

    loaded = run(repository.read())

    assert loaded.interface.no_color is True
    assert not stale.exists()


def test_concurrent_selection_writers_preserve_both_fields(tmp_path: Path) -> None:
    """Serialized read-modify-write updates do not lose an independent selection."""
    repository = TomlSelectionStateRepository(tmp_path / "selection.toml")

    def select_profile() -> None:
        run(
            repository.update(
                lambda state: SelectionState(
                    selected_profile="primary",
                    direct_resource_scope=state.direct_resource_scope,
                )
            )
        )

    def select_scope() -> None:
        run(
            repository.update(
                lambda state: SelectionState(
                    selected_profile=state.selected_profile,
                    direct_resource_scope=project("123"),
                )
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(select_profile), executor.submit(select_scope)]
        for future in futures:
            future.result(timeout=10)

    assert run(repository.read()) == SelectionState(
        selected_profile="primary",
        direct_resource_scope=project("123"),
    )


def test_configuration_round_trip_preserves_safe_profile_fields(tmp_path: Path) -> None:
    """Profiles persist references and settings without storing secret values."""
    repository = TomlConfigRepository(tmp_path / "config.toml")
    expected = ConfigSnapshot(
        profiles=(
            Profile(
                name="primary",
                resource_scope=project("123"),
                adc_quota_project=project("456"),
                quota_contact_keyring_reference=QuotaContactKeyringReference("primary"),
            ),
        ),
    )

    run(repository.update(lambda _: expected))

    assert run(repository.read()) == expected


@pytest.mark.parametrize(
    "stored_value",
    [
        "operator@example.com",
        "raw quota contact",
        "credential-json",
        "cqmgr:quota-contact:secondary",
    ],
)
def test_configuration_rejects_unsafe_or_cross_profile_keyring_references(
    tmp_path: Path,
    stored_value: str,
) -> None:
    """TOML cannot disguise contact data or another profile as a reference."""
    path = tmp_path / "config.toml"
    path.write_text(
        'schema = "cqmgr.config/v1"\n\n'
        "[profiles.primary]\n"
        f'quota_contact_keyring_reference = "{stored_value}"\n'
    )

    with pytest.raises(InvalidStoredDataError, match="keyring reference"):
        run(TomlConfigRepository(path).read())


def test_v0_selection_state_migrates_without_rewriting_configuration(
    tmp_path: Path,
) -> None:
    """Legacy direct-project state migrates independently to current selection."""
    path = tmp_path / "selection.toml"
    path.write_text(
        'schema = "cqmgr.selection-state/v0"\n'
        'selected_profile = "primary"\n'
        'direct_project = "projects/123"\n'
    )
    repository = TomlSelectionStateRepository(path)

    migrated = run(repository.read())
    run(repository.update(lambda state: state))

    assert migrated == SelectionState(
        selected_profile="primary",
        direct_resource_scope=project("123"),
    )
    assert 'schema = "cqmgr.selection-state/v1"' in path.read_text()
    assert 'direct_resource_scope = "projects/123"' in path.read_text()
    assert "direct_project =" not in path.read_text()


def test_selection_state_rejects_newer_schema(tmp_path: Path) -> None:
    """Mutable state never guesses how to downgrade a newer writer's schema."""
    path = tmp_path / "selection.toml"
    path.write_text('schema = "cqmgr.selection-state/v2"\n')

    with pytest.raises(UnsupportedStoredSchemaError):
        run(TomlSelectionStateRepository(path).read())


def test_failed_atomic_replace_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure before replace leaves the last complete configuration readable."""
    path = tmp_path / "config.toml"
    repository = TomlConfigRepository(path)
    expected = ConfigSnapshot(profiles=(Profile(name="primary"),))
    run(repository.update(lambda _: expected))
    original_replace = Path.replace

    def fail_replace(source: Path, target: Path) -> Path:
        message = f"injected replace failure for {source.name} -> {target.name}"
        raise OSError(message)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(StoredDataOperationalError, match=r"config\.toml"):
        run(
            repository.update(
                lambda snapshot: ConfigSnapshot(
                    profiles=snapshot.profiles,
                    interface=snapshot.interface.replace(
                        InterfaceSettingKey.NERD_FONT,
                        value=True,
                    ),
                )
            )
        )
    monkeypatch.setattr(Path, "replace", original_replace)

    assert run(repository.read()) == expected
    assert not tuple(tmp_path.glob(".config.toml.*.tmp"))


def test_concurrent_processes_preserve_independent_selection_fields(
    tmp_path: Path,
) -> None:
    """The file lock serializes separate cqmgr processes without lost updates."""
    path = tmp_path / "selection.toml"
    start = tmp_path / "start"
    script = r"""
import asyncio
import sys
import time
from pathlib import Path

from cqmgr.adapters.persistence.configuration import TomlSelectionStateRepository
from cqmgr.application.configuration import SelectionState
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

path = Path(sys.argv[1])
mode = sys.argv[2]
ready = Path(sys.argv[3])
start = Path(sys.argv[4])
ready.touch()
deadline = time.monotonic() + 10
while not start.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("start barrier was not released")
    time.sleep(0.01)

repository = TomlSelectionStateRepository(path)
if mode == "profile":
    asyncio.run(repository.update(lambda state: SelectionState(
        selected_profile="primary",
        direct_resource_scope=state.direct_resource_scope,
    )))
else:
    asyncio.run(repository.update(lambda state: SelectionState(
        selected_profile=state.selected_profile,
        direct_resource_scope=ResourceScope(
            ResourceScopeKind.PROJECT,
            "projects/123",
        ),
    )))
"""
    processes = [
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                script,
                str(path),
                mode,
                str(tmp_path / f"ready-{mode}"),
                str(start),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for mode in ("profile", "scope")
    ]
    deadline = time.monotonic() + 10
    while not all(
        (tmp_path / f"ready-{mode}").exists() for mode in ("profile", "scope")
    ):
        if time.monotonic() >= deadline:
            pytest.fail("concurrent writers did not reach the start barrier")
        time.sleep(0.01)
    start.touch()

    for process in processes:
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr

    assert run(TomlSelectionStateRepository(path).read()) == SelectionState(
        selected_profile="primary",
        direct_resource_scope=project("123"),
    )
