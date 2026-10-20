"""Production read-only composition without ambient provider activity."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import google.auth.exceptions
import pytest
from google.cloud import compute_v1

from cqmgr.adapters.google.compute_catalog import (
    OfficialComputeMachineTypesPageClient,
)
from cqmgr.adapters.google.identity import ADCCredentialSnapshot
from cqmgr.application.operations.quotas import WorkloadChoiceRequest
from cqmgr.application.operations.read_only import (
    ReadOnlyOperations,
    ReadOnlyQuotaQuery,
    ReadOnlyScopeInput,
)
from cqmgr.application.ports.coordination import (
    BudgetRequest,
    CancellationToken,
    CoordinationUnavailableError,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.bootstrap import build_read_only_operations
from cqmgr.domain.accelerator_overlay import WorkloadKind
from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject, CredentialKind
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.results import ExitClass
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.google_read_only import (
    CachedADCRuntime,
    LazyClientProxy,
    OwnedClientPool,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Protocol

    class _HasClient(Protocol):
        _client: object


class _RecordingADCRuntime:
    def __init__(self, credential: object) -> None:
        self.snapshot = ADCCredentialSnapshot(CredentialKind.UNKNOWN, credential)
        self.loads: list[tuple[tuple[str, ...], str | None]] = []

    def load(
        self,
        *,
        scopes: Sequence[str],
        quota_project_id: str | None,
        timeout_seconds: float = 10.0,
    ) -> ADCCredentialSnapshot:
        del timeout_seconds
        self.loads.append((tuple(scopes), quota_project_id))
        return self.snapshot

    def refresh(
        self,
        snapshot: ADCCredentialSnapshot,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        assert snapshot is self.snapshot
        assert timeout_seconds > 0

    def fetch_user_info(
        self,
        snapshot: ADCCredentialSnapshot,
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, object]:
        del snapshot, timeout_seconds
        return {}


def test_lazy_clients_share_one_cached_adc_credential() -> None:
    """Independent official-client factories reuse one lazily loaded ADC object."""
    credential = object()
    delegate = _RecordingADCRuntime(credential)
    runtime = CachedADCRuntime(
        delegate,
        default_scopes=("scope-a", "scope-b"),
    )
    first = LazyClientProxy(lambda: SimpleNamespace(credential=runtime.credential()))
    second = LazyClientProxy(lambda: SimpleNamespace(credential=runtime.credential()))

    assert first.credential is credential
    assert second.credential is credential
    assert runtime.load(scopes=("scope-a", "scope-b"), quota_project_id=None) is (
        delegate.snapshot
    )
    runtime.refresh(delegate.snapshot)
    assert runtime.fetch_user_info(delegate.snapshot) == {}
    assert delegate.loads == [(("scope-a", "scope-b"), None)]


def test_lazy_client_shutdown_does_not_construct_an_unused_client() -> None:
    """Closing the composition root preserves lazy client construction."""
    constructed: list[object] = []
    proxy = LazyClientProxy(lambda: constructed.append(object()))

    asyncio.run(proxy.aclose())

    assert constructed == []


def test_lazy_client_shutdown_uses_its_configured_closer_once() -> None:
    """A generated client with no public close can own transport shutdown."""
    client = SimpleNamespace(marker=True)
    closed: list[object] = []
    proxy = LazyClientProxy(
        lambda: client,
        closer=closed.append,
    )

    assert proxy.marker
    asyncio.run(proxy.aclose())
    asyncio.run(proxy.aclose())

    assert closed == [client]


def test_lazy_client_shutdown_accepts_a_client_without_close() -> None:
    """A constructed non-closing client remains safe at the shared boundary."""
    proxy = LazyClientProxy(lambda: SimpleNamespace(marker=True))

    assert proxy.marker
    asyncio.run(proxy.aclose())


def test_owned_client_pool_closes_every_constructed_client() -> None:
    """Owned clients all receive shutdown even when one close fails."""
    closed: list[str] = []

    class _Client:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self._name = name
            self._fail = fail

        async def close(self) -> None:
            closed.append(self._name)
            if self._fail:
                msg = "close failed"
                raise RuntimeError(msg)

    first = LazyClientProxy(lambda: _Client("first", fail=True))
    second = LazyClientProxy(lambda: _Client("second"))
    assert callable(first.close)
    assert callable(second.close)

    asyncio.run(OwnedClientPool(first, second).aclose())

    assert closed == ["first", "second"]


def test_owned_client_pool_closes_an_unused_official_compute_wrapper() -> None:
    """The composition root can own a lazy Compute wrapper without constructing it."""
    constructed: list[object] = []
    wrapper = OfficialComputeMachineTypesPageClient(
        lambda: constructed.append(object()),  # type: ignore[arg-type,return-value]
    )

    asyncio.run(OwnedClientPool(wrapper).aclose())

    assert constructed == []


def test_build_read_only_operations_is_lazy_about_adc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Building provider operations neither discovers ADC nor opens a client."""
    attempted: list[object] = []

    def forbid_adc(**kwargs: object) -> object:
        attempted.append(kwargs)
        msg = "ADC must remain lazy"
        raise AssertionError(msg)

    monkeypatch.setattr("google.auth.default", forbid_adc)
    monkeypatch.setattr(
        "google.cloud.compute_v1.AcceleratorTypesClient",
        forbid_adc,
    )
    monkeypatch.setattr(
        "google.cloud.compute_v1.MachineTypesClient",
        forbid_adc,
    )
    operations = build_read_only_operations(
        {
            "CQMGR_CONFIG_PATH": str(tmp_path / "config.toml"),
            "CQMGR_SELECTION_STATE_PATH": str(tmp_path / "selection.toml"),
            "CQMGR_QUOTA_SNAPSHOT_PATH": str(tmp_path / "snapshots"),
            "CQMGR_BUDGET_PATH": str(tmp_path / "budgets"),
        }
    )

    assert isinstance(operations, ReadOnlyOperations)
    assert attempted == []

    quotas = operations._quotas  # noqa: SLF001  # composition-root wiring proof
    workloads = operations._workloads  # noqa: SLF001  # composition-root wiring proof
    assert quotas._effective is workloads._effective  # noqa: SLF001
    assert quotas._usage is workloads._usage  # noqa: SLF001
    assert (
        cast("_HasClient", quotas._effective)._client  # noqa: SLF001
        is cast(  # noqa: SLF001
            "_HasClient",
            quotas._preferences,  # noqa: SLF001
        )._client
    )
    assert cast("_HasClient", workloads._tpu_locations)._client is (  # noqa: SLF001
        cast("_HasClient", workloads._tpu_accelerator_types)._client  # noqa: SLF001
    )
    assert cast("_HasClient", workloads._tpu_locations)._client is (  # noqa: SLF001
        cast("_HasClient", workloads._tpu_runtime_versions)._client  # noqa: SLF001
    )
    asyncio.run(operations.aclose())
    assert attempted == []


@pytest.mark.parametrize(
    ("locations", "expected_method"),
    [
        ((), "aggregated"),
        (("us-central1-a",), "exact-zone"),
    ],
)
def test_build_read_only_operations_dispatches_complete_compute_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    locations: tuple[str, ...],
    expected_method: str,
) -> None:
    """The real graph selects accelerator and machine reads from request shape."""
    calls: list[tuple[str, str]] = []
    closed: list[str] = []

    class _Pager:
        def __init__(self, page: object) -> None:
            self.pages = iter((page,))

    class _Accelerators:
        transport = SimpleNamespace(close=lambda: closed.append("accelerators"))

        def aggregated_list(self, **kwargs: object) -> _Pager:
            del kwargs
            calls.append(("accelerators", "aggregated"))
            return _Pager(compute_v1.AcceleratorTypeAggregatedList())

        def list(self, **kwargs: object) -> _Pager:
            del kwargs
            calls.append(("accelerators", "exact-zone"))
            return _Pager(compute_v1.AcceleratorTypeList())

    class _Machines:
        transport = SimpleNamespace(close=lambda: closed.append("machines"))

        def aggregated_list(self, **kwargs: object) -> _Pager:
            del kwargs
            calls.append(("machines", "aggregated"))
            return _Pager(compute_v1.MachineTypeAggregatedList())

        def list(self, **kwargs: object) -> _Pager:
            del kwargs
            calls.append(("machines", "exact-zone"))
            return _Pager(compute_v1.MachineTypeList())

    monkeypatch.setattr(
        "google.auth.default",
        lambda **_: (SimpleNamespace(quota_project_id=None), None),
    )
    monkeypatch.setattr(
        "google.cloud.compute_v1.AcceleratorTypesClient",
        lambda **_: _Accelerators(),
    )
    monkeypatch.setattr(
        "google.cloud.compute_v1.MachineTypesClient",
        lambda **_: _Machines(),
    )
    operations = build_read_only_operations(
        {
            "CQMGR_CONFIG_PATH": str(tmp_path / "config.toml"),
            "CQMGR_SELECTION_STATE_PATH": str(tmp_path / "selection.toml"),
            "CQMGR_QUOTA_SNAPSHOT_PATH": str(tmp_path / "snapshots"),
            "CQMGR_BUDGET_PATH": str(tmp_path / "budgets"),
        }
    )
    context = ProviderReadContext(
        project=CanonicalProject(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
            "fixture-project",
            "Fixture Project",
        ),
        identity=ADCIdentityEvidence.principal_unverified(
            credential_kind=CredentialKind.UNKNOWN,
            adc_quota_project=ADCQuotaProject("fixture-quota-project"),
        ),
        deadline=time.monotonic() + 30.0,
        cancellation=CancellationToken(),
    )

    asyncio.run(
        operations._workloads.read_catalog(  # noqa: SLF001 - composition proof
            WorkloadChoiceRequest(context, WorkloadKind.COMPUTE_INSTANCE, locations)
        )
    )
    asyncio.run(operations.aclose())

    assert sorted(calls) == [
        ("accelerators", expected_method),
        ("machines", expected_method),
    ]
    assert sorted(closed) == ["accelerators", "machines"]


def test_unavailable_budget_storage_is_a_typed_coordination_failure(
    tmp_path: Path,
) -> None:
    """An unusable budget path remains behind the coordination port."""
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked")

    operations = build_read_only_operations(
        {
            "CQMGR_CONFIG_PATH": str(tmp_path / "config.toml"),
            "CQMGR_SELECTION_STATE_PATH": str(tmp_path / "selection.toml"),
            "CQMGR_QUOTA_SNAPSHOT_PATH": str(tmp_path / "snapshots"),
            "CQMGR_BUDGET_PATH": str(blocked_parent / "budgets"),
        }
    )
    effective = operations._quotas._effective  # noqa: SLF001
    budget = effective._policy._budget  # type: ignore[attr-defined]  # noqa: SLF001

    async def acquire() -> None:
        await budget.acquire(
            BudgetRequest("cloud-quotas", "projects/123", None),
            deadline=1e20,
            cancellation=CancellationToken(),
        )

    with pytest.raises(CoordinationUnavailableError):
        asyncio.run(acquire())


def test_adc_construction_failure_is_a_typed_operation_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing ADC is normalized at the provider boundary, never raised."""

    def unavailable(**kwargs: object) -> object:
        del kwargs
        detail = "private detail"
        raise google.auth.exceptions.DefaultCredentialsError(detail)

    monkeypatch.setattr("google.auth.default", unavailable)
    operations = build_read_only_operations(
        {
            "CQMGR_CONFIG_PATH": str(tmp_path / "config.toml"),
            "CQMGR_SELECTION_STATE_PATH": str(tmp_path / "selection.toml"),
            "CQMGR_QUOTA_SNAPSHOT_PATH": str(tmp_path / "snapshots"),
            "CQMGR_BUDGET_PATH": str(tmp_path / "budgets"),
        }
    )

    result = asyncio.run(
        operations.browse(
            ReadOnlyQuotaQuery(),
            deadline=time.monotonic() + 60.0,
            scope_input=ReadOnlyScopeInput(
                explicit_resource_scope=ResourceScope(
                    ResourceScopeKind.PROJECT,
                    "projects/123456789",
                )
            ),
        )
    )

    assert result.outcome.code.value == "adc-unavailable"
    assert result.outcome.exit_class is ExitClass.AUTHORIZATION
    assert "private detail" not in repr(result)
