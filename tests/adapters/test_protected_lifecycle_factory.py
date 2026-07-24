"""Protected Review, Apply, and Watch CLI request construction."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cqmgr.adapters.cli.lifecycle import (
    PlanReferenceInput,
    ProtectedLifecycleCliRequestFactory,
    WatchCliInput,
)
from cqmgr.application.operations.lifecycle_apply import (
    ApplyRefreshError,
    EphemeralApplyContactRefresher,
)
from cqmgr.application.operations.lifecycle_requests import bind_protected_contact
from cqmgr.application.operations.trust import LoadedInstallationTrust
from cqmgr.application.ports.plans import (
    EncodedPlan,
    PlanRepositoryOutcome,
    PlanRepositoryStatus,
)
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.plans import PlanPrincipal
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import WatchCondition

NOW = datetime(2026, 7, 24, 16, tzinfo=UTC)
KEY = SecretValue(b"k" * 32)
CONTACT = SecretValue(b"operator@example.com")
DIGEST = "sha256:" + ("a" * 64)
WATCH_DEADLINE = 160.0


class _Trust:
    def load(self) -> LoadedInstallationTrust:
        return LoadedInstallationTrust(
            "installation-test",
            KEY,
            keyring_mutation_capable=True,
        )


class _Clock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 100.0


class _Decoded:
    def __init__(self, plan: object) -> None:
        self.plan = plan
        self.digest = DIGEST

    def authenticate(self, key: bytes) -> bool:
        return key == KEY.reveal()


class _Codec:
    def __init__(self, plan: object) -> None:
        self.plan = plan

    def decode(self, data: bytes) -> _Decoded:
        assert data == b"portable-plan"
        return _Decoded(self.plan)


class _Repository:
    def __init__(self) -> None:
        self.stored: list[EncodedPlan] = []

    def read_export(self, path: Path) -> PlanRepositoryOutcome:
        assert path == Path("request.plan")
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.AVAILABLE,
            plan_bytes=b"portable-plan",
        )

    def store(
        self,
        encoded: EncodedPlan,
        authentication_key: SecretValue,
    ) -> PlanRepositoryOutcome:
        assert authentication_key is KEY
        self.stored.append(encoded)
        return PlanRepositoryOutcome(PlanRepositoryStatus.STORED)


def _factory() -> tuple[ProtectedLifecycleCliRequestFactory, _Repository]:
    binding = bind_protected_contact(CONTACT, KEY)
    plan = SimpleNamespace(
        installation_id="installation-test",
        principal=PlanPrincipal("principal://accounts/123"),
        contact_binding=binding,
    )
    repository = _Repository()
    factory = ProtectedLifecycleCliRequestFactory(
        trust=_Trust(),
        repository=cast("Any", repository),
        codec=cast("Any", _Codec(plan)),
        contacts=EphemeralApplyContactRefresher(),
        clock=_Clock(),
    )
    return factory, repository


def test_apply_imports_authenticated_plan_and_rebinds_contact() -> None:
    """Portable Apply becomes local only after trust and contact both match."""
    factory, repository = _factory()

    request = factory.apply(
        PlanReferenceInput(None, Path("request.plan")),
        "projects/123",
        quota_contact=CONTACT,
    )

    assert request.digest == DIGEST
    assert request.local_installation_id == "installation-test"
    assert request.resource_scope_acknowledgement == ResourceScope(
        ResourceScopeKind.PROJECT,
        "projects/123",
    )
    assert request.contact_value == "operator@example.com"
    assert repository.stored == [EncodedPlan(b"portable-plan", DIGEST)]


def test_apply_rejects_contact_that_does_not_match_plan() -> None:
    """Protected re-entry cannot silently change the Plan-bound contact."""
    factory, _ = _factory()

    with pytest.raises(ApplyRefreshError, match="does not match"):
        factory.apply(
            PlanReferenceInput(None, Path("request.plan")),
            "projects/123",
            quota_contact=SecretValue(b"other@example.com"),
        )


def test_watch_uses_active_trust_and_absolute_deadline() -> None:
    """Watch binds active installation authority and a monotonic deadline."""
    factory, _ = _factory()

    request = factory.watch(
        WatchCliInput(
            intent_id=DIGEST,
            condition=WatchCondition.GRANTED,
            resume=None,
            deadline=datetime(2026, 7, 24, 16, 1, tzinfo=UTC),
        )
    )

    assert request.authentication_key is KEY
    assert request.installation_id == "installation-test"
    assert request.deadline == WATCH_DEADLINE
