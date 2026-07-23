"""Import-safe invocation classification and composition root."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cqmgr.application.operations.audit import AuditOperations
    from cqmgr.application.operations.local import LocalOperations
    from cqmgr.application.operations.quotas import QuotaOperations
    from cqmgr.application.operations.read_only import ReadOnlyOperations

_LOCAL_GROUPS = frozenset(
    ("scope", "sc", "profile", "pf", "config", "cfg", "audit", "aud")
)
_PROVIDER_GROUPS = frozenset(
    (
        "quota",
        "q",
        "obtainability",
        "ob",
        "request",
        "req",
        "plan",
        "pl",
    )
)


class InvocationKind(StrEnum):
    """Startup dependency class selected before optional runtime imports."""

    HELP = "help"
    LOCAL = "local"
    TUI = "tui"
    PROVIDER = "provider"
    INVALID = "invalid"


def classify_invocation(
    arguments: Sequence[str],
    *,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> InvocationKind:
    """Classify raw argv without importing Textual, ADC, providers, or keyring."""
    if not arguments:
        return (
            InvocationKind.TUI
            if stdin_is_tty and stdout_is_tty
            else InvocationKind.HELP
        )
    command = arguments[0]
    if command in {"--help", "--version"}:
        return InvocationKind.HELP
    if command == "tui":
        return InvocationKind.HELP if "--help" in arguments else InvocationKind.TUI
    if command in _LOCAL_GROUPS:
        return InvocationKind.HELP if "--help" in arguments else InvocationKind.LOCAL
    if command in _PROVIDER_GROUPS:
        return InvocationKind.HELP if "--help" in arguments else InvocationKind.PROVIDER
    return InvocationKind.INVALID


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Explicit platform-native paths for local configuration and evidence."""

    configuration: Path
    selection_state: Path
    audit: Path
    quota_snapshots: Path
    budgets: Path


def _platform_paths(environment: Mapping[str, str]) -> RuntimePaths:
    home = Path.home()
    if sys.platform == "win32":
        config_home = Path(environment.get("APPDATA", home / "AppData/Roaming"))
        state_home = Path(environment.get("LOCALAPPDATA", home / "AppData/Local"))
    elif sys.platform == "darwin":
        config_home = home / "Library/Application Support"
        state_home = config_home
    else:
        config_home = Path(environment.get("XDG_CONFIG_HOME", home / ".config"))
        state_home = Path(environment.get("XDG_STATE_HOME", home / ".local/state"))
    return RuntimePaths(
        configuration=config_home / "cqmgr/config.toml",
        selection_state=state_home / "cqmgr/selection.toml",
        audit=state_home / "cqmgr/audit",
        quota_snapshots=state_home / "cqmgr/quota-snapshots",
        budgets=state_home / "cqmgr/budgets",
    )


def runtime_paths(environment: Mapping[str, str] | None = None) -> RuntimePaths:
    """Resolve only cqmgr path overrides and platform-native defaults."""
    source = os.environ if environment is None else environment
    defaults = _platform_paths(source)
    return RuntimePaths(
        configuration=Path(
            source.get("CQMGR_CONFIG_PATH", str(defaults.configuration))
        ).expanduser(),
        selection_state=Path(
            source.get("CQMGR_SELECTION_STATE_PATH", str(defaults.selection_state))
        ).expanduser(),
        audit=Path(source.get("CQMGR_AUDIT_PATH", str(defaults.audit))).expanduser(),
        quota_snapshots=Path(
            source.get(
                "CQMGR_QUOTA_SNAPSHOT_PATH",
                str(defaults.quota_snapshots),
            )
        ).expanduser(),
        budgets=Path(
            source.get("CQMGR_BUDGET_PATH", str(defaults.budgets))
        ).expanduser(),
    )


def build_local_operations(
    environment: Mapping[str, str] | None = None,
) -> LocalOperations:
    """Compose the complete local-only application without optional integrations."""
    from cqmgr.adapters.clock import SystemClock  # noqa: PLC0415
    from cqmgr.adapters.persistence.configuration import (  # noqa: PLC0415
        TomlConfigRepository,
        TomlSelectionStateRepository,
    )
    from cqmgr.application.operations.local import LocalOperations  # noqa: PLC0415

    paths = runtime_paths(environment)
    return LocalOperations(
        TomlConfigRepository(paths.configuration),
        TomlSelectionStateRepository(paths.selection_state),
        SystemClock(),
    )


def build_audit_operations(
    environment: Mapping[str, str] | None = None,
) -> AuditOperations:
    """Compose local audit reads without importing provider integrations."""
    from cqmgr.adapters.clock import SystemClock  # noqa: PLC0415
    from cqmgr.adapters.persistence.audit import (  # noqa: PLC0415
        EmptyAuditJournal,
        FilesystemAuditJournal,
        UnavailableAuditJournal,
    )
    from cqmgr.application.operations.audit import AuditOperations  # noqa: PLC0415

    path = runtime_paths(environment).audit
    if not path.exists() and path.parent.exists() and not path.parent.is_dir():
        journal = UnavailableAuditJournal()
    elif not path.exists() or (
        path.is_dir()
        and not (path / "manifest.json").exists()
        and not next(path.glob("audit-*.jsonl"), None)
    ):
        journal = EmptyAuditJournal()
    else:
        try:
            journal = FilesystemAuditJournal(path)
        except OSError:
            journal = UnavailableAuditJournal()
    return AuditOperations(
        journal,
        SystemClock(),
    )


def build_quota_cursor_operations(
    environment: Mapping[str, str] | None = None,
) -> QuotaOperations:
    """Compose retained quota-cursor reads without provider integrations."""
    from typing import Any, cast  # noqa: PLC0415

    from cqmgr.adapters.clock import SystemClock  # noqa: PLC0415
    from cqmgr.adapters.persistence.quota_snapshots import (  # noqa: PLC0415
        FilesystemQuotaQuerySnapshots,
    )
    from cqmgr.application.operations.quotas import QuotaOperations  # noqa: PLC0415
    from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
        MAINTAINED_ACCELERATOR_OVERLAY,
    )

    class _ProviderReadUnavailable:
        async def read(self, request: object) -> None:
            del request
            msg = "provider reads are unavailable during cursor continuation"
            raise RuntimeError(msg)

    snapshots = FilesystemQuotaQuerySnapshots(
        runtime_paths(environment).quota_snapshots
    )
    unavailable = _ProviderReadUnavailable()
    return QuotaOperations(
        cast("Any", unavailable),
        cast("Any", unavailable),
        cast("Any", unavailable),
        MAINTAINED_ACCELERATOR_OVERLAY,
        snapshots,
        snapshots,
        SystemClock(),
    )


def build_read_only_operations(  # noqa: PLR0915 - explicit composition root
    environment: Mapping[str, str] | None = None,
) -> ReadOnlyOperations:
    """Compose lazy official Google reads behind provider-neutral operations."""
    from typing import Any, cast  # noqa: PLC0415

    from google.cloud import (  # noqa: PLC0415
        cloudquotas_v1,
        compute_v1,
        monitoring_v3,
        resourcemanager_v3,
        tpu_v2,
    )

    from cqmgr.adapters.clock import SystemClock  # noqa: PLC0415
    from cqmgr.adapters.google.cloud_quotas import (  # noqa: PLC0415
        CloudQuotasPageClient,
        GoogleEffectiveQuotaReader,
        GoogleQuotaPreferenceReader,
        OfficialCloudQuotasPageClient,
    )
    from cqmgr.adapters.google.compute_catalog import (  # noqa: PLC0415
        ComputeAcceleratorTypesPageClient,
        ComputeMachineTypesPageClient,
        GoogleComputeAcceleratorTypeReader,
        GoogleComputeMachineTypeReader,
        OfficialComputeAcceleratorTypesPageClient,
        OfficialComputeMachineTypesPageClient,
    )
    from cqmgr.adapters.google.identity import (  # noqa: PLC0415
        ADC_IDENTITY_SCOPES,
        FederatedSubjectResolver,
        GoogleADCIdentityProvider,
        GoogleAuthRuntime,
    )
    from cqmgr.adapters.google.monitoring import (  # noqa: PLC0415
        GoogleUsageReader,
        MonitoringPageClient,
        OfficialMonitoringPageClient,
    )
    from cqmgr.adapters.google.projects import (  # noqa: PLC0415
        ProjectsClient,
        ResourceManagerProjectResolver,
    )
    from cqmgr.adapters.google.read_policy import GoogleReadPolicy  # noqa: PLC0415
    from cqmgr.adapters.google.tpu_catalog import (  # noqa: PLC0415
        GoogleTpuAcceleratorTypeReader,
        GoogleTpuLocationReader,
        GoogleTpuRuntimeVersionReader,
        OfficialTpuCatalogPageClient,
        TpuCatalogPageClient,
    )
    from cqmgr.adapters.persistence.configuration import (  # noqa: PLC0415
        TomlConfigRepository,
        TomlSelectionStateRepository,
    )
    from cqmgr.adapters.persistence.coordination import (  # noqa: PLC0415
        DeterministicJitter,
        SharedBudgetCoordinator,
        UnavailableBudgetCoordinator,
    )
    from cqmgr.adapters.persistence.quota_snapshots import (  # noqa: PLC0415
        FilesystemQuotaQuerySnapshots,
    )
    from cqmgr.application.operations.quotas import (  # noqa: PLC0415
        QuotaOperations,
        WorkloadResolutionOperations,
    )
    from cqmgr.application.operations.read_only import (  # noqa: PLC0415
        ReadOnlyOperations,
    )
    from cqmgr.application.ports.coordination import (  # noqa: PLC0415
        BudgetLimit,
        BudgetScope,
    )
    from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
        MAINTAINED_ACCELERATOR_OVERLAY,
    )
    from cqmgr.google_read_only import (  # noqa: PLC0415
        CachedADCRuntime,
        LazyClientProxy,
        OwnedClientPool,
    )

    class _UnsupportedFederatedSubjectResolver:
        def resolve(self, credential: object) -> None:  # noqa: ARG002
            return None

    paths = runtime_paths(environment)
    clock = SystemClock()
    configuration = TomlConfigRepository(paths.configuration)
    selection = TomlSelectionStateRepository(paths.selection_state)
    snapshots = FilesystemQuotaQuerySnapshots(paths.quota_snapshots)

    adc = CachedADCRuntime(
        GoogleAuthRuntime(
            cast(
                "FederatedSubjectResolver",
                _UnsupportedFederatedSubjectResolver(),
            )
        ),
        default_scopes=ADC_IDENTITY_SCOPES,
    )

    projects_client = LazyClientProxy(
        lambda: resourcemanager_v3.ProjectsAsyncClient(
            credentials=cast("Any", adc.credential())
        ),
        closer=lambda client: client.transport.close(),
    )
    projects = ResourceManagerProjectResolver(
        cast(
            "ProjectsClient",
            projects_client,
        )
    )
    cloud_quotas_client = LazyClientProxy(
        lambda: OfficialCloudQuotasPageClient(
            cloudquotas_v1.CloudQuotasAsyncClient(
                credentials=cast("Any", adc.credential())
            )
        )
    )
    cloud_quotas = cast(
        "CloudQuotasPageClient",
        cloud_quotas_client,
    )
    monitoring_client = LazyClientProxy(
        lambda: OfficialMonitoringPageClient(
            monitoring_v3.MetricServiceAsyncClient(
                credentials=cast("Any", adc.credential())
            )
        )
    )
    monitoring = cast(
        "MonitoringPageClient",
        monitoring_client,
    )
    compute_accelerators_client = LazyClientProxy(
        lambda: OfficialComputeAcceleratorTypesPageClient(
            compute_v1.AcceleratorTypesClient(credentials=cast("Any", adc.credential()))
        )
    )
    compute_accelerators = cast(
        "ComputeAcceleratorTypesPageClient",
        compute_accelerators_client,
    )
    compute_machine_types_client = LazyClientProxy(
        lambda: OfficialComputeMachineTypesPageClient(
            compute_v1.MachineTypesClient(credentials=cast("Any", adc.credential()))
        )
    )
    compute_machine_types = cast(
        "ComputeMachineTypesPageClient",
        compute_machine_types_client,
    )
    tpu_catalog_client = LazyClientProxy(
        lambda: OfficialTpuCatalogPageClient(
            tpu_v2.TpuAsyncClient(credentials=cast("Any", adc.credential()))
        )
    )
    tpu_catalog = cast(
        "TpuCatalogPageClient",
        tpu_catalog_client,
    )
    owned_clients = OwnedClientPool(
        projects_client,
        cloud_quotas_client,
        monitoring_client,
        compute_accelerators_client,
        compute_machine_types_client,
        tpu_catalog_client,
    )

    # These installation-local ceilings are intentionally lower than common
    # provider defaults: at most 30 read attempts per minute on every identity
    # axis, regardless of how many cqmgr processes share this state directory.
    try:
        budget = SharedBudgetCoordinator(
            paths.budgets,
            {
                scope: BudgetLimit(capacity=30, period_seconds=60.0)
                for scope in BudgetScope
            },
        )
    except OSError:
        budget = UnavailableBudgetCoordinator()
    policy = GoogleReadPolicy(
        budget,
        DeterministicJitter(f"cqmgr-google-read-v1:{paths.budgets}"),
    )

    effective = GoogleEffectiveQuotaReader(cloud_quotas, policy)
    preferences = GoogleQuotaPreferenceReader(cloud_quotas, policy)
    usage = GoogleUsageReader(monitoring, policy)
    compute_accelerator_reader = GoogleComputeAcceleratorTypeReader(
        compute_accelerators,
        policy,
    )
    compute_machine_reader = GoogleComputeMachineTypeReader(
        compute_machine_types,
        policy,
    )
    tpu_locations = GoogleTpuLocationReader(tpu_catalog, policy)
    tpu_accelerators = GoogleTpuAcceleratorTypeReader(tpu_catalog, policy)
    tpu_runtime_versions = GoogleTpuRuntimeVersionReader(tpu_catalog, policy)

    quotas = QuotaOperations(
        effective,
        preferences,
        usage,
        MAINTAINED_ACCELERATOR_OVERLAY,
        snapshots,
        snapshots,
        clock,
    )
    workloads = WorkloadResolutionOperations(
        effective,
        usage,
        compute_accelerator_reader,
        compute_machine_reader,
        tpu_locations,
        tpu_accelerators,
        tpu_runtime_versions,
        MAINTAINED_ACCELERATOR_OVERLAY,
        clock,
    )
    return ReadOnlyOperations(
        configuration,
        selection,
        projects,
        GoogleADCIdentityProvider(adc),
        quotas,
        workloads,
        clock,
        shutdown=owned_clients.aclose,
        budget=budget,
    )
