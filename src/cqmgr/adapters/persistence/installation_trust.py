"""Private create-once persistence for non-secret installation trust."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import cast

from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock
from cqmgr.application.operations.trust import (
    InstallationTrust,
    InstallationTrustPhase,
)
from cqmgr.application.ports.secrets import (
    SecretPurpose,
    SecretStoreReference,
)

_SCHEMA = "cqmgr.installation-trust/v2"
_FIELDS = frozenset(
    (
        "schema",
        "installation_id",
        "key_service",
        "key_item_id",
        "key_commitment",
        "phase",
    )
)


class InstallationTrustPersistenceError(RuntimeError):
    """Retained installation trust is unavailable or internally inconsistent."""


class TomlInstallationTrustRepository:
    """Atomically retain one non-secret installation signing-key reference."""

    def __init__(
        self,
        path: Path,
        *,
        lock: NativePlanInterprocessLock | None = None,
    ) -> None:
        """Bind one trust record and its bounded sibling lock."""
        self._path = path
        self._lock = lock or NativePlanInterprocessLock(
            path.with_name(f".{path.name}.lock")
        )

    def load(self) -> InstallationTrust | None:
        """Read the exact retained state under its bounded interprocess lock."""
        try:
            with self._lock:
                return self._load_unlocked()
        except InstallationTrustPersistenceError:
            raise
        except OSError as error:
            msg = f"installation trust read failed: {type(error).__name__}"
            raise InstallationTrustPersistenceError(msg) from error

    def create(self, value: InstallationTrust) -> None:
        """Create prepared trust without replacing any retained state."""
        if not isinstance(value, InstallationTrust):
            msg = "installation trust create requires typed state"
            raise TypeError(msg)
        try:
            with self._lock:
                if self._path.exists():
                    msg = "installation trust already exists"
                    raise InstallationTrustPersistenceError(msg)  # noqa: TRY301
                self._write_unlocked(value)
        except InstallationTrustPersistenceError:
            raise
        except OSError as error:
            msg = f"installation trust create failed: {type(error).__name__}"
            raise InstallationTrustPersistenceError(msg) from error

    def transition(
        self,
        expected: InstallationTrustPhase,
        replacement: InstallationTrust,
    ) -> None:
        """Atomically move the exact retained identity between bootstrap phases."""
        if not isinstance(expected, InstallationTrustPhase):
            msg = "installation trust expected phase must be typed"
            raise TypeError(msg)
        if not isinstance(replacement, InstallationTrust):
            msg = "installation trust replacement must be typed"
            raise TypeError(msg)
        try:
            with self._lock:
                current = self._load_unlocked()
                if current is None:
                    msg = "installation trust is missing"
                    raise InstallationTrustPersistenceError(msg)  # noqa: TRY301
                if current.phase is not expected:
                    msg = "installation trust phase changed concurrently"
                    raise InstallationTrustPersistenceError(msg)  # noqa: TRY301
                if (
                    current.installation_id != replacement.installation_id
                    or current.authentication_key_reference
                    != replacement.authentication_key_reference
                    or current.authentication_key_commitment
                    != replacement.authentication_key_commitment
                ):
                    msg = "installation trust identity cannot change"
                    raise InstallationTrustPersistenceError(msg)  # noqa: TRY301
                self._write_unlocked(replacement)
        except InstallationTrustPersistenceError:
            raise
        except OSError as error:
            msg = f"installation trust transition failed: {type(error).__name__}"
            raise InstallationTrustPersistenceError(msg) from error

    def restart_incomplete(
        self,
        expected: InstallationTrust,
        replacement: InstallationTrust,
    ) -> None:
        """Replace one exact incomplete candidate without touching active trust."""
        if not isinstance(expected, InstallationTrust):
            msg = "installation trust restart requires typed expected state"
            raise TypeError(msg)
        if not isinstance(replacement, InstallationTrust):
            msg = "installation trust restart requires typed replacement state"
            raise TypeError(msg)
        if expected.phase is InstallationTrustPhase.ACTIVE:
            msg = "active installation trust cannot be restarted"
            raise InstallationTrustPersistenceError(msg)
        if replacement.phase is not InstallationTrustPhase.PREPARED:
            msg = "installation trust restart must prepare fresh authority"
            raise InstallationTrustPersistenceError(msg)
        if (
            replacement.installation_id == expected.installation_id
            or replacement.authentication_key_reference
            == expected.authentication_key_reference
            or replacement.authentication_key_commitment
            == expected.authentication_key_commitment
        ):
            msg = "installation trust restart requires fresh authority"
            raise InstallationTrustPersistenceError(msg)
        try:
            with self._lock:
                current = self._load_unlocked()
                if current != expected:
                    msg = "installation trust changed before incomplete restart"
                    raise InstallationTrustPersistenceError(msg)  # noqa: TRY301
                self._write_unlocked(replacement)
        except InstallationTrustPersistenceError:
            raise
        except OSError as error:
            msg = f"installation trust restart failed: {type(error).__name__}"
            raise InstallationTrustPersistenceError(msg) from error

    def _load_unlocked(self) -> InstallationTrust | None:
        if not self._path.exists():
            return None
        try:
            with self._path.open("rb") as stream:
                raw = tomllib.load(stream)
        except tomllib.TOMLDecodeError as error:
            msg = "installation trust is malformed"
            raise InstallationTrustPersistenceError(msg) from error
        schema = raw.get("schema")
        if schema != _SCHEMA:
            msg = f"unsupported installation trust schema {schema!r}"
            raise InstallationTrustPersistenceError(msg)
        if set(raw) != _FIELDS:
            msg = "installation trust fields are inconsistent"
            raise InstallationTrustPersistenceError(msg)
        try:
            installation_id = cast("str", raw["installation_id"])
            reference = SecretStoreReference(
                installation_id,
                SecretPurpose.PLAN_AUTHENTICATION,
                cast("str", raw["key_item_id"]),
            )
            if raw["key_service"] != reference.service:
                msg = "installation trust key service must match its reference"
                raise ValueError(msg)  # noqa: TRY301
            key_commitment_value = raw["key_commitment"]
            if not isinstance(key_commitment_value, str):
                msg = "installation trust key commitment must be text"
                raise TypeError(msg)  # noqa: TRY301
            try:
                key_commitment = bytes.fromhex(key_commitment_value)
            except ValueError as error:
                msg = "installation trust key commitment must be hexadecimal"
                raise ValueError(msg) from error
            if key_commitment.hex() != key_commitment_value:
                msg = "installation trust key commitment must be canonical hex"
                raise ValueError(msg)  # noqa: TRY301
            phase = InstallationTrustPhase(cast("str", raw["phase"]))
            return InstallationTrust(
                installation_id,
                reference,
                key_commitment,
                phase,
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = f"installation trust is inconsistent: {error}"
            raise InstallationTrustPersistenceError(msg) from error

    def _write_unlocked(self, value: InstallationTrust) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self._path.parent.chmod(0o700)
        contents = "\n".join(
            (
                f"schema = {json.dumps(_SCHEMA)}",
                f"installation_id = {json.dumps(value.installation_id)}",
                "key_service = "
                + json.dumps(value.authentication_key_reference.service),
                "key_item_id = "
                + json.dumps(value.authentication_key_reference.item_id),
                "key_commitment = "
                + json.dumps(value.authentication_key_commitment.hex()),
                f"phase = {json.dumps(value.phase.value)}",
                "",
            )
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            if os.name != "nt":
                temporary.chmod(0o600)
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
            if os.name != "nt":
                self._path.chmod(0o600)
                directory = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
