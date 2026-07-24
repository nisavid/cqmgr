"""Private, create-once installation-trust persistence."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from cqmgr.adapters.persistence.installation_trust import (
    InstallationTrustPersistenceError,
    TomlInstallationTrustRepository,
)
from cqmgr.application.operations.trust import (
    InstallationTrust,
    InstallationTrustPhase,
)
from cqmgr.application.ports.secrets import (
    SecretPurpose,
    SecretStoreReference,
)

if TYPE_CHECKING:
    from pathlib import Path

INSTALLATION_ID = "installation-test"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
REFERENCE = SecretStoreReference.generate(
    INSTALLATION_ID,
    SecretPurpose.PLAN_AUTHENTICATION,
)
RECOVERY_INSTALLATION_ID = "installation-recovery"
RECOVERY_REFERENCE = SecretStoreReference.generate(
    RECOVERY_INSTALLATION_ID,
    SecretPurpose.PLAN_AUTHENTICATION,
)
KEY_COMMITMENT = bytes.fromhex(
    "5e318f8cf9cbe249a30812b8ca132d691ded7a91991413558db5758575f5e01f"
)


def _trust(phase: InstallationTrustPhase) -> InstallationTrust:
    return InstallationTrust(INSTALLATION_ID, REFERENCE, KEY_COMMITMENT, phase)


def _recovery_trust(phase: InstallationTrustPhase) -> InstallationTrust:
    return InstallationTrust(
        RECOVERY_INSTALLATION_ID,
        RECOVERY_REFERENCE,
        b"r" * 32,
        phase,
    )


def test_trust_repository_creates_and_transitions_private_state(tmp_path: Path) -> None:
    """Trust state is private, exact, and phase-transitioned atomically."""
    path = tmp_path / "trust.toml"
    repository = TomlInstallationTrustRepository(path)

    repository.create(_trust(InstallationTrustPhase.PREPARED))
    repository.transition(
        InstallationTrustPhase.PREPARED,
        _trust(InstallationTrustPhase.CREATE_INTENT),
    )
    repository.transition(
        InstallationTrustPhase.CREATE_INTENT,
        _trust(InstallationTrustPhase.ACTIVE),
    )

    assert repository.load() == _trust(InstallationTrustPhase.ACTIVE)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE
        assert path.parent.stat().st_mode & 0o777 == PRIVATE_DIRECTORY_MODE
    contents = path.read_text()
    assert "installation-test" in contents
    assert REFERENCE.item_id in contents
    assert KEY_COMMITMENT.hex() in contents
    assert "kkkk" not in contents


def test_trust_repository_rejects_recreation_and_wrong_phase(tmp_path: Path) -> None:
    """Existing state and stale compare-and-swap transitions fail closed."""
    repository = TomlInstallationTrustRepository(tmp_path / "trust.toml")
    repository.create(_trust(InstallationTrustPhase.PREPARED))

    with pytest.raises(InstallationTrustPersistenceError, match="already exists"):
        repository.create(_trust(InstallationTrustPhase.PREPARED))
    with pytest.raises(InstallationTrustPersistenceError, match="phase changed"):
        repository.transition(
            InstallationTrustPhase.ACTIVE,
            _trust(InstallationTrustPhase.ACTIVE),
        )


def test_trust_repository_restarts_only_the_exact_incomplete_candidate(
    tmp_path: Path,
) -> None:
    """Explicit recovery cannot replace changed or active installation trust."""
    path = tmp_path / "trust.toml"
    repository = TomlInstallationTrustRepository(path)
    prepared = _trust(InstallationTrustPhase.PREPARED)
    recovery = _recovery_trust(InstallationTrustPhase.PREPARED)
    repository.create(prepared)

    repository.restart_incomplete(prepared, recovery)

    assert repository.load() == recovery
    with pytest.raises(InstallationTrustPersistenceError, match="changed"):
        repository.restart_incomplete(
            prepared,
            _recovery_trust(InstallationTrustPhase.PREPARED),
        )
    with pytest.raises(InstallationTrustPersistenceError, match="fresh"):
        repository.restart_incomplete(recovery, recovery)
    with pytest.raises(InstallationTrustPersistenceError, match="prepare"):
        repository.restart_incomplete(
            recovery,
            _trust(InstallationTrustPhase.CREATE_INTENT),
        )
    active_repository = TomlInstallationTrustRepository(tmp_path / "active.toml")
    active_repository.create(_trust(InstallationTrustPhase.ACTIVE))
    with pytest.raises(InstallationTrustPersistenceError, match="changed"):
        active_repository.restart_incomplete(prepared, recovery)
    with pytest.raises(InstallationTrustPersistenceError, match="active"):
        repository.restart_incomplete(
            _trust(InstallationTrustPhase.ACTIVE),
            recovery,
        )


def test_trust_repository_rejects_tampered_or_newer_state(tmp_path: Path) -> None:
    """Unknown schemas and inconsistent key references are never interpreted."""
    path = tmp_path / "trust.toml"
    path.write_text('schema = "cqmgr.installation-trust/v3"\n')
    repository = TomlInstallationTrustRepository(path)

    with pytest.raises(InstallationTrustPersistenceError, match="unsupported"):
        repository.load()

    path.write_text(
        "\n".join(
            (
                'schema = "cqmgr.installation-trust/v2"',
                'installation_id = "other-installation"',
                f'key_service = "{REFERENCE.service}"',
                f'key_item_id = "{REFERENCE.item_id}"',
                f'key_commitment = "{KEY_COMMITMENT.hex()}"',
                'phase = "active"',
            )
        )
    )
    with pytest.raises(InstallationTrustPersistenceError, match="match"):
        repository.load()


def test_trust_repository_rejects_uncommitted_v1_state(tmp_path: Path) -> None:
    """A pre-commitment record has no safe implicit migration authority."""
    path = tmp_path / "trust.toml"
    path.write_text(
        "\n".join(
            (
                'schema = "cqmgr.installation-trust/v1"',
                f'installation_id = "{INSTALLATION_ID}"',
                f'key_service = "{REFERENCE.service}"',
                f'key_item_id = "{REFERENCE.item_id}"',
                'phase = "active"',
            )
        )
    )

    with pytest.raises(InstallationTrustPersistenceError, match="unsupported"):
        TomlInstallationTrustRepository(path).load()


def test_trust_repository_rejects_invalid_key_commitment(tmp_path: Path) -> None:
    """Malformed commitments cannot become retained installation authority."""
    path = tmp_path / "trust.toml"
    path.write_text(
        "\n".join(
            (
                'schema = "cqmgr.installation-trust/v2"',
                f'installation_id = "{INSTALLATION_ID}"',
                f'key_service = "{REFERENCE.service}"',
                f'key_item_id = "{REFERENCE.item_id}"',
                'key_commitment = "not-a-sha256-digest"',
                'phase = "active"',
            )
        )
    )

    with pytest.raises(InstallationTrustPersistenceError, match="commitment"):
        TomlInstallationTrustRepository(path).load()
