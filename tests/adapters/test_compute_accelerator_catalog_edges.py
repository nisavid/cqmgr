"""Focused failure and coverage edges for Compute accelerator discovery."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from google.cloud import compute_v1

from cqmgr.adapters.google.compute_catalog import (
    ComputeAcceleratorTypesPage,
    ComputeAcceleratorTypesScope,
    GoogleComputeAcceleratorTypeReader,
)
from cqmgr.adapters.google.read_policy import GoogleReadPolicy
from cqmgr.application.ports.catalog_reads import (
    CatalogRead,
    ComputeAcceleratorTypeReadRequest,
    ComputeMachineTypeReadRequest,
)
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    BudgetRequest,
    CancellationToken,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.catalog import LocationCoverageState
from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject, CredentialKind
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from collections.abc import Sequence

NOW = datetime(2026, 7, 22, 9, tzinfo=UTC)


class RecordingBudget:
    """Grant deterministic local reads without provider access."""

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Grant the requested operation within its test deadline."""
        cancellation.raise_if_cancelled()
        return BudgetGrant(deadline - 1, request)


class NoJitter:
    """Keep the one-attempt read policy deterministic."""

    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        """Return an immediate retry delay."""
        del attempt, identity
        return min(delay, 0.0)


class ComputeAcceleratorPages:
    """Script materialized pages at the external Compute boundary."""

    def __init__(
        self,
        pages: Sequence[ComputeAcceleratorTypesPage | BaseException] = (),
    ) -> None:
        """Retain provider outcomes in dispatch order."""
        self.pages = list(pages)

    async def accelerator_types(
        self,
        *,
        project: str,
        max_results: int,
        page_token: str,
        return_partial_success: bool,
        timeout_seconds: float,
    ) -> ComputeAcceleratorTypesPage:
        """Return or raise the next materialized provider outcome."""
        del project, max_results, page_token, return_partial_success, timeout_seconds
        value = self.pages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _policy() -> GoogleReadPolicy:
    """Return one deterministic no-retry read policy."""
    return GoogleReadPolicy(RecordingBudget(), NoJitter(), maximum_attempts=1)


def _context() -> ProviderReadContext:
    """Return one explicit read-only project context."""
    return ProviderReadContext(
        project=CanonicalProject(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
            "public-schema-project",
            "Public Schema Project",
        ),
        identity=ADCIdentityEvidence.principal_unverified(
            credential_kind=CredentialKind.UNKNOWN,
            adc_quota_project=ADCQuotaProject("public-quota-project"),
        ),
        deadline=time.monotonic() + 30,
        cancellation=CancellationToken(),
    )


def _diagnostic_codes[ValueT](result: CatalogRead[ValueT]) -> list[str]:
    """Return the public stable diagnostic codes."""
    return [item.code.value for item in result.read.diagnostics]


def _accelerator(name: str = "nvidia-b200") -> compute_v1.AcceleratorType:
    """Return one provider-attributed accelerator declaration."""
    return compute_v1.AcceleratorType(
        name=name,
        zone="us-central1-a",
        self_link=(
            "https://www.googleapis.com/compute/v1/projects/public-schema-project/"
            f"zones/us-central1-a/acceleratorTypes/{name}"
        ),
    )


def test_accelerator_reader_rejects_another_catalog_request_type() -> None:
    """An accelerator reader cannot consume a machine-type request."""
    request = ComputeMachineTypeReadRequest(_context())

    with pytest.raises(TypeError, match="ComputeAcceleratorTypeReadRequest"):
        asyncio.run(
            GoogleComputeAcceleratorTypeReader(
                ComputeAcceleratorPages(), _policy()
            ).read(cast("ComputeAcceleratorTypeReadRequest", request))
        )


def test_accelerator_reader_reports_provider_failure_without_provider_text() -> None:
    """Provider exceptions become redacted failed coverage without values."""
    provider_text = "discarded-accelerator-provider-detail"

    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages((RuntimeError(provider_text),)),
            _policy(),
            now=lambda: NOW,
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert result.values == ()
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert _diagnostic_codes(result) == ["provider-read-failed"]
    assert provider_text not in repr(result)


@pytest.mark.parametrize(
    ("warning", "state", "diagnostics"),
    [
        ("NO_RESULTS_ON_PAGE", LocationCoverageState.EMPTY, []),
        (
            "PROVIDER_NEW_WARNING",
            LocationCoverageState.FAILED,
            ["compute-accelerator-catalog-page-warning"],
        ),
    ],
)
def test_accelerator_reader_distinguishes_empty_and_failed_page_warnings(
    warning: str,
    state: LocationCoverageState,
    diagnostics: list[str],
) -> None:
    """Page warnings preserve authoritative empty separately from failed evidence."""
    page = ComputeAcceleratorTypesPage((), "", warning_code=warning)

    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert result.values == ()
    assert result.location_coverage[0].state is state
    assert _diagnostic_codes(result) == diagnostics
    assert result.complete is (state is LocationCoverageState.EMPTY)


def test_accelerator_reader_fails_a_malformed_scope_without_relabeling_items() -> None:
    """A non-zone aggregate scope cannot lend its identity to valid-looking items."""
    page = ComputeAcceleratorTypesPage(
        (ComputeAcceleratorTypesScope("regions/us-central1", (_accelerator(),)),),
        "",
    )

    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert result.values == ()
    assert [(item.location, item.state) for item in result.location_coverage] == [
        ("global", LocationCoverageState.FAILED)
    ]
    assert _diagnostic_codes(result) == ["provider-schema-invalid"]


def test_accelerator_reader_fails_unattributable_unreachable_scopes_globally() -> None:
    """Malformed unreachable scope evidence remains failed without invented location."""
    page = ComputeAcceleratorTypesPage(
        (),
        "",
        unreachable_scopes=("regions/us-central1",),
    )

    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert [(item.location, item.state) for item in result.location_coverage] == [
        ("global", LocationCoverageState.FAILED)
    ]
    assert _diagnostic_codes(result) == [
        "compute-accelerator-catalog-location-unreachable"
    ]
    assert not result.complete


def test_accelerator_reader_retains_values_but_fails_a_warned_scope() -> None:
    """A provider scope warning prevents an exhaustive claim without hiding values."""
    page = ComputeAcceleratorTypesPage(
        (
            ComputeAcceleratorTypesScope(
                "zones/us-central1-a",
                (_accelerator(),),
                warning_code="PROVIDER_NEW_WARNING",
            ),
        ),
        "",
    )

    result = asyncio.run(
        GoogleComputeAcceleratorTypeReader(
            ComputeAcceleratorPages((page,)), _policy(), now=lambda: NOW
        ).read(ComputeAcceleratorTypeReadRequest(_context()))
    )

    assert [item.name for item in result.values] == ["nvidia-b200"]
    assert result.location_coverage[0].state is LocationCoverageState.FAILED
    assert _diagnostic_codes(result) == ["compute-accelerator-catalog-scope-warning"]
    assert not result.complete
