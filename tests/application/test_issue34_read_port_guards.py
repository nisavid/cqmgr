"""Typed application read-boundary contracts introduced for issue 34."""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.application.ports.catalog_reads import (
    CatalogRead,
    ComputeMachineTypeReadRequest,
    TpuAcceleratorTypeReadRequest,
    TpuLocationReadRequest,
    TpuRuntimeVersionReadRequest,
)
from cqmgr.application.ports.coordination import CancellationToken
from cqmgr.application.ports.provider_reads import ProviderReadContext, UsageReadRequest
from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject, CredentialKind
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.quotas import ProviderRead, ProviderReadCoverage
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from cqmgr.domain.catalog import CatalogLocationCoverage

NOW = datetime(2026, 7, 22, 8, tzinfo=UTC)


def _project() -> CanonicalProject:
    return CanonicalProject(
        ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
        "public-schema-project",
        "Public Schema Project",
    )


def _identity() -> ADCIdentityEvidence:
    return ADCIdentityEvidence.principal_unverified(
        credential_kind=CredentialKind.UNKNOWN,
        adc_quota_project=ADCQuotaProject("public-quota-project"),
    )


def _context() -> ProviderReadContext:
    return ProviderReadContext(_project(), _identity(), 30.0, CancellationToken())


def test_provider_context_requires_explicit_typed_coordination() -> None:
    """Provider reads cannot fall back to ambient project, identity, or cancellation."""
    context = _context()
    invalid_values = (
        ("project", object(), TypeError),
        ("identity", object(), TypeError),
        ("deadline", True, ValueError),
        ("deadline", math.inf, ValueError),
        ("cancellation", object(), TypeError),
    )
    for field_name, value, error_type in invalid_values:
        with pytest.raises(error_type):
            replace(context, **{field_name: value})  # type: ignore[bad-argument-type]


@pytest.mark.parametrize(
    "service",
    ["Compute.GoogleApis.com", "compute", "-compute.googleapis.com", "compute_.com"],
)
def test_usage_reads_require_canonical_service_and_bounded_utc_interval(
    service: str,
) -> None:
    """Monitoring evidence always names one canonical service and UTC interval."""
    with pytest.raises(ValueError, match="canonical lowercase DNS"):
        UsageReadRequest(_context(), service, NOW, NOW + timedelta(minutes=5))

    valid = UsageReadRequest(
        _context(),
        "compute.googleapis.com",
        NOW,
        NOW + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="aware UTC"):
        replace(valid, interval_start=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="start before end"):
        replace(valid, interval_start=valid.interval_end)


def test_catalog_read_preserves_values_and_explicit_completeness() -> None:
    """Catalog values never hide page or location coverage from callers."""
    provider_read = ProviderRead(
        values=("machine",),
        coverage=ProviderReadCoverage(1, 1),
        observed_at=NOW,
    )
    catalog_read = CatalogRead(provider_read, ())

    assert catalog_read.values == ("machine",)
    assert catalog_read.complete
    with pytest.raises(TypeError, match="ProviderRead"):
        replace(catalog_read, read=cast("ProviderRead[str]", object()))
    with pytest.raises(TypeError, match="CatalogLocationCoverage"):
        replace(
            catalog_read,
            location_coverage=cast(
                "tuple[CatalogLocationCoverage, ...]",
                ("us-central1-a",),
            ),
        )


def test_catalog_requests_require_context_and_canonical_zone() -> None:
    """Each catalog read is bound to an explicit context and exact location ID."""
    context = _context()
    assert ComputeMachineTypeReadRequest(context).context is context
    assert TpuLocationReadRequest(context).context is context
    with pytest.raises(TypeError, match="Compute catalog"):
        ComputeMachineTypeReadRequest(cast("ProviderReadContext", object()))
    with pytest.raises(TypeError, match="TPU location"):
        TpuLocationReadRequest(cast("ProviderReadContext", object()))

    for request_type in (TpuAcceleratorTypeReadRequest, TpuRuntimeVersionReadRequest):
        with pytest.raises(TypeError, match="ProviderReadContext"):
            request_type(cast("ProviderReadContext", object()), "us-central1-a")
        for zone in (
            "",
            "US-CENTRAL1-A",
            "-us-central1-a",
            "zones/us-central1-a",
            "us-central1",
            "a",
        ):
            with pytest.raises(ValueError, match="canonical location ID"):
                request_type(context, zone)
