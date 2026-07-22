"""Exercise persistence through an installed cqmgr artifact, outside its checkout."""

from __future__ import annotations

import argparse
import secrets
from datetime import UTC, datetime
from pathlib import Path

import keyring

import cqmgr
from cqmgr.adapters.persistence.native_plan_lock import NativePlanInterprocessLock
from cqmgr.adapters.persistence.plans import LocalPlanRepository
from cqmgr.adapters.persistence.secrets import NativeSecretStore
from cqmgr.adapters.serialization.plans import PlanCodec
from cqmgr.application.ports.plans import PlanRepositoryStatus
from cqmgr.application.ports.secrets import (
    SecretPurpose,
    SecretStoreOutcome,
    SecretStoreReference,
    SecretStoreStatus,
    SecretValue,
)
from cqmgr.domain.plans import (
    PLAN_LIFETIME,
    ContactBinding,
    PlanPrincipal,
    QuotaRequestPlan,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind


class _MemoryConsumptionStore:
    """Create/get-only marker seam for artifact-local filesystem checks."""

    def __init__(self) -> None:
        self._values: dict[SecretStoreReference, SecretValue] = {}

    def get_consumption_marker(
        self, reference: SecretStoreReference
    ) -> SecretStoreOutcome:
        value = self._values.get(reference)
        if value is None:
            return SecretStoreOutcome(SecretStoreStatus.MISSING)
        return SecretStoreOutcome.available(value)

    def create_consumption_marker(
        self,
        reference: SecretStoreReference,
        secret: SecretValue,
    ) -> SecretStoreOutcome:
        if reference in self._values:
            return SecretStoreOutcome(SecretStoreStatus.CONFLICT)
        self._values[reference] = secret
        return SecretStoreOutcome(SecretStoreStatus.CREATED)


def _encoded_plan(key: bytes):  # noqa: ANN202
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
    plan = QuotaRequestPlan(
        resource_scope=scope,
        slice_identity=EffectiveQuotaSliceIdentity(
            resource_scope=scope,
            service="compute.googleapis.com",
            quota_id="GPUS-PER-PROJECT",
            dimensions=NormalizedDimensions(),
            quota_scope=QuotaScope.GLOBAL,
        ),
        target=QuotaQuantity(8, QuotaUnit("1")),
        effective=QuotaQuantity(4, QuotaUnit("1")),
        effective_observed_at=now,
        preference_name=None,
        preference_etag=None,
        principal=PlanPrincipal("principal://accounts/123"),
        contact_binding=ContactBinding(
            StableSymbol("direct-user"),
            "principal://accounts/123",
            "hmac-sha256:" + ("c" * 64),
        ),
        warnings=(),
        required_acknowledgements=(),
        acknowledgements=(),
        constraints=(),
        evidence=(),
        installation_id="artifact-smoke",
        issued_at=now,
        expires_at=now + PLAN_LIFETIME,
    )
    return PlanCodec.encode(plan, key), now


def _exercise_local_persistence(root: Path) -> None:
    key = b"k" * 32
    encoded, now = _encoded_plan(key)
    repository = LocalPlanRepository(root, _MemoryConsumptionStore())
    authentication_key = SecretValue(key)

    stored = repository.store(encoded, authentication_key)
    assert stored.status is PlanRepositoryStatus.STORED
    loaded = repository.load(encoded.digest, authentication_key, now)
    assert loaded.status is PlanRepositoryStatus.AVAILABLE
    assert loaded.plan_bytes == encoded.bytes
    destination = root.parent / "export" / "request.plan"
    exported = repository.export(encoded, destination)
    assert exported.status is PlanRepositoryStatus.EXPORTED
    assert repository.read_export(destination).plan_bytes == encoded.bytes


def _exercise_native_keyring(root: Path, *, require_round_trip: bool) -> None:
    store = NativeSecretStore(
        keyring.get_keyring(),
        NativePlanInterprocessLock(root / "native-keyring.lock"),
    )
    probe = store.probe()
    if not require_round_trip:
        assert probe.backend_identity
        return
    assert probe.mutation_capable
    reference = SecretStoreReference.generate(
        f"artifact-ci-{secrets.token_urlsafe(12)}",
        SecretPurpose.PLAN_AUTHENTICATION,
    )
    value = SecretValue(secrets.token_bytes(32))
    try:
        created = store.create(reference, value)
        assert created.status is SecretStoreStatus.CREATED
        loaded = store.get(reference)
        assert loaded.status is SecretStoreStatus.AVAILABLE
        assert loaded.secret == value
        deleted = store.delete(reference)
        assert deleted.status is SecretStoreStatus.DELETED
        missing = store.get(reference)
        assert missing.status is SecretStoreStatus.MISSING
    finally:
        if store.get(reference).status is SecretStoreStatus.AVAILABLE:
            store.delete(reference)


def main() -> None:
    """Parse paths and prove that the installed package owns every import."""
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--native-keyring-round-trip", action="store_true")
    arguments = parser.parse_args()
    package_path = Path(cqmgr.__file__).resolve()
    assert not package_path.is_relative_to(arguments.checkout.resolve())
    arguments.root.mkdir(parents=True)
    _exercise_local_persistence(arguments.root / "plans")
    _exercise_native_keyring(
        arguments.root,
        require_round_trip=arguments.native_keyring_round_trip,
    )


if __name__ == "__main__":
    main()
