"""Protected Review, Apply, and Watch CLI request construction."""

from __future__ import annotations

import asyncio
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
from cqmgr.adapters.persistence.plans import LocalPlanRepository
from cqmgr.adapters.serialization.plans import PlanCodec
from cqmgr.application.operations.contacts import (
    ResolvedContact,
    bind_protected_contact,
)
from cqmgr.application.operations.lifecycle_apply import (
    ApplyRefreshError,
)
from cqmgr.application.operations.trust import LoadedInstallationTrust
from cqmgr.application.ports.plans import (
    EncodedPlan,
    PlanRepositoryOutcome,
    PlanRepositoryStatus,
)
from cqmgr.application.ports.secrets import (
    SecretStoreOutcome,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
)
from cqmgr.domain.plans import PLAN_LIFETIME, PlanPrincipal, QuotaRequestPlan
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
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


class _Contacts:
    async def prepare_apply(
        self,
        binding: object,
        *,
        explicit_value: SecretValue | None,
        principal: PlanPrincipal,
        trust: LoadedInstallationTrust,
    ) -> ResolvedContact:
        del principal
        if explicit_value is None:
            message = "Apply requires the Plan-bound per-operation contact"
            raise ApplyRefreshError(message)
        resolved = bind_protected_contact(
            explicit_value,
            trust.authentication_key,
        )
        if resolved != binding:
            message = "Apply quota contact does not match the reviewed Plan"
            raise ApplyRefreshError(message)
        return ResolvedContact(resolved, explicit_value)


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
        self.loaded: list[str] = []
        self.stored: list[EncodedPlan] = []

    def load(
        self,
        digest: str,
        authentication_key: SecretValue,
        now: datetime,
    ) -> PlanRepositoryOutcome:
        assert authentication_key is KEY
        assert now == NOW
        self.loaded.append(digest)
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.AVAILABLE,
            plan_bytes=b"portable-plan",
        )

    def read_export(self, path: Path) -> PlanRepositoryOutcome:
        assert path == Path("request.plan")
        return PlanRepositoryOutcome(
            PlanRepositoryStatus.EXPORTED,
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


class _MissingConsumptionMarkers:
    def get_consumption_marker(
        self,
        reference: SecretStoreReference,
    ) -> SecretStoreOutcome:
        del reference
        return SecretStoreOutcome(SecretStoreStatus.MISSING)

    def create_consumption_marker(
        self,
        reference: SecretStoreReference,
        secret: SecretValue,
    ) -> SecretStoreOutcome:
        del reference, secret
        return SecretStoreOutcome(SecretStoreStatus.CREATED)


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
        contacts=cast("Any", _Contacts()),
        clock=_Clock(),
    )
    return factory, repository


def _real_encoded_plan() -> EncodedPlan:
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    unit = QuotaUnit("1")
    plan = QuotaRequestPlan(
        resource_scope=scope,
        slice_identity=EffectiveQuotaSliceIdentity(
            resource_scope=scope,
            service="compute.googleapis.com",
            quota_id="GPUS-PER-PROJECT",
            dimensions=NormalizedDimensions(),
            quota_scope=QuotaScope.GLOBAL,
        ),
        target=QuotaQuantity(8, unit),
        effective=QuotaQuantity(4, unit),
        effective_observed_at=NOW,
        preference_name=None,
        preference_etag=None,
        principal=PlanPrincipal("principal://accounts/123"),
        contact_binding=bind_protected_contact(CONTACT, KEY),
        warnings=(),
        required_acknowledgements=(),
        acknowledgements=(),
        constraints=(),
        evidence=(),
        installation_id="installation-test",
        issued_at=NOW,
        expires_at=NOW + PLAN_LIFETIME,
    )
    return PlanCodec.encode(plan, KEY.reveal())


def test_apply_imports_authenticated_plan_and_rebinds_contact() -> None:
    """Portable Apply becomes local only after trust and contact both match."""
    factory, repository = _factory()

    request = asyncio.run(
        factory.apply(
            PlanReferenceInput(None, Path("request.plan")),
            "projects/123",
            quota_contact=CONTACT,
        )
    )

    assert request.digest == DIGEST
    assert request.local_installation_id == "installation-test"
    assert request.resource_scope_acknowledgement == ResourceScope(
        ResourceScopeKind.PROJECT,
        "projects/123",
    )
    assert request.contact_value == "operator@example.com"
    assert repository.stored == [EncodedPlan(b"portable-plan", DIGEST)]


def test_apply_loads_available_local_plan_by_digest() -> None:
    """Digest Apply requires the local repository's available status."""
    factory, repository = _factory()

    request = asyncio.run(
        factory.apply(
            PlanReferenceInput(DIGEST, None),
            "projects/123",
            quota_contact=CONTACT,
        )
    )

    assert request.digest == DIGEST
    assert repository.loaded == [DIGEST]
    assert repository.stored == []


def test_apply_imports_exported_plan_from_local_repository(tmp_path: Path) -> None:
    """Portable Apply accepts the repository's authenticated exported status."""
    repository = LocalPlanRepository(
        tmp_path / "repository",
        _MissingConsumptionMarkers(),
    )
    encoded = _real_encoded_plan()
    exported = tmp_path / "request.plan"
    assert repository.export(encoded, exported).status is PlanRepositoryStatus.EXPORTED
    factory = ProtectedLifecycleCliRequestFactory(
        trust=_Trust(),
        repository=repository,
        codec=cast("Any", PlanCodec()),
        contacts=cast("Any", _Contacts()),
        clock=_Clock(),
    )

    request = asyncio.run(
        factory.apply(
            PlanReferenceInput(None, exported),
            "projects/123",
            quota_contact=CONTACT,
        )
    )

    assert request.digest == encoded.digest
    assert repository.load(encoded.digest, KEY, NOW).status is (
        PlanRepositoryStatus.AVAILABLE
    )


def test_apply_rejects_contact_that_does_not_match_plan() -> None:
    """Protected re-entry cannot silently change the Plan-bound contact."""
    factory, _ = _factory()

    with pytest.raises(ApplyRefreshError, match="does not match"):
        asyncio.run(
            factory.apply(
                PlanReferenceInput(None, Path("request.plan")),
                "projects/123",
                quota_contact=SecretValue(b"other@example.com"),
            )
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
