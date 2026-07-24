"""Hermetic production lifecycle composition-root contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from cqmgr import bootstrap
from cqmgr.adapters.cli.lifecycle import ProtectedLifecycleCliRequestFactory
from cqmgr.application.operations.lifecycle_requests import LifecycleCompositionIntent
from cqmgr.application.operations.read_only import (
    QuotaInspectSelector,
    ReadOnlyScopeInput,
)
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.plans import TargetStrategy
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch


class _NoKeyringAccess:
    def get_password(self, service: str, username: str) -> str | None:
        del service, username
        message = "bootstrap must not read keyring"
        raise AssertionError(message)

    def set_password(self, service: str, username: str, password: str) -> None:
        del service, username, password
        message = "bootstrap must not write keyring"
        raise AssertionError(message)

    def delete_password(self, service: str, username: str) -> None:
        del service, username
        message = "bootstrap must not delete keyring"
        raise AssertionError(message)


class _NoProviderAccess:
    def __init__(self) -> None:
        self.calls = 0

    async def inspect(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        self.calls += 1
        message = "missing trust must stop before provider reads"
        raise AssertionError(message)


def _environment(root: Path) -> dict[str, str]:
    return {
        "CQMGR_CONFIG_PATH": str(root / "config.toml"),
        "CQMGR_SELECTION_STATE_PATH": str(root / "selection.toml"),
        "CQMGR_AUDIT_PATH": str(root / "audit"),
        "CQMGR_QUOTA_SNAPSHOT_PATH": str(root / "snapshots"),
        "CQMGR_BUDGET_PATH": str(root / "budgets"),
        "CQMGR_TRUST_PATH": str(root / "trust.toml"),
        "CQMGR_PLAN_PATH": str(root / "plans"),
        "CQMGR_APPLY_RECORD_PATH": str(root / "apply"),
        "CQMGR_WATCH_PATH": str(root / "watch"),
    }


def test_lifecycle_bootstrap_is_lazy_and_missing_trust_stops_before_reads(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Composition creates no authority and Preview probes trust before providers."""
    read_only = _NoProviderAccess()
    monkeypatch.setattr(
        bootstrap,
        "build_read_only_operations",
        lambda _environment: read_only,
    )
    monkeypatch.setattr("keyring.get_keyring", _NoKeyringAccess)

    runtime = bootstrap.build_lifecycle_runtime(_environment(tmp_path))

    assert runtime.preparation is not None
    assert isinstance(runtime.requests, ProtectedLifecycleCliRequestFactory)
    intent = LifecycleCompositionIntent(
        scope_input=ReadOnlyScopeInput(
            explicit_resource_scope=ResourceScope(
                ResourceScopeKind.PROJECT,
                "projects/123",
            )
        ),
        selector=QuotaInspectSelector(
            "compute.googleapis.com",
            "GPU-DIRECT",
            "us-central1",
        ),
        workload=None,
        target_strategy=TargetStrategy.MANUAL,
        targets=((None, "8"),),
        quota_contact=SecretValue(b"operator@example.com"),
    )

    with pytest.raises(RuntimeError, match="trust is missing"):
        asyncio.run(
            runtime.preparation.prepare(
                intent,
                deadline=100.0,
                require_preview=True,
            )
        )

    assert read_only.calls == 0
    assert not (tmp_path / "trust.toml").exists()
