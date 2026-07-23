"""Provider-neutral read evidence contracts."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.quotas import (
    MonitoringPoint,
    MonitoringValue,
    MonitoringValueKind,
    ProviderRead,
    ProviderReadCoverage,
)
from cqmgr.domain.redaction import RedactedText

NOW = datetime(2026, 7, 22, tzinfo=UTC)


def _diagnostic() -> Diagnostic:
    return Diagnostic(
        DiagnosticCode("provider-schema-invalid"),
        Severity.ERROR,
        DiagnosticPhase("quota-preference-read"),
        DiagnosticSource("cloud-quotas"),
        RetryDisposition.AFTER_UPGRADE,
        RedactedText("The provider returned malformed preference evidence."),
    )


def test_complete_read_requires_all_pages() -> None:
    """A page cap or failed page cannot be reported as complete evidence."""
    coverage = ProviderReadCoverage(
        pages_attempted=2,
        pages_completed=1,
        page_cap_reached=True,
    )

    read = ProviderRead(
        values=("usable-first-page",),
        coverage=coverage,
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    assert not read.complete
    assert read.values == ("usable-first-page",)


def test_read_diagnostics_can_be_attributed_to_one_logical_service() -> None:
    """One global read can retain independent logical service completeness."""
    read = ProviderRead(
        values=(),
        coverage=ProviderReadCoverage(1, 1),
        observed_at=NOW,
        diagnostics=(_diagnostic(),),
        diagnostic_services=("tpu.googleapis.com",),
    )

    assert read.complete_for("compute.googleapis.com")
    assert not read.complete_for("tpu.googleapis.com")
    assert read.diagnostics_for("compute.googleapis.com") == ()
    assert read.diagnostics_for("tpu.googleapis.com") == (_diagnostic(),)


def test_unattributed_read_diagnostic_is_shared_by_every_service() -> None:
    """Pagination and unassignable schema failures remain globally incomplete."""
    read = ProviderRead(
        values=(),
        coverage=ProviderReadCoverage(1, 1),
        observed_at=NOW,
        diagnostics=(_diagnostic(),),
        diagnostic_services=(None,),
    )

    assert not read.complete_for("compute.googleapis.com")
    assert not read.complete_for("tpu.googleapis.com")


def test_read_rejects_misaligned_diagnostic_attribution() -> None:
    """Diagnostic attribution cannot silently omit a retained failure."""
    with pytest.raises(ValueError, match="align"):
        ProviderRead(
            values=(),
            coverage=ProviderReadCoverage(1, 1),
            observed_at=NOW,
            diagnostics=(_diagnostic(),),
            diagnostic_services=(None, "tpu.googleapis.com"),
        )


def test_read_rejects_untyped_evidence_and_diagnostic_attribution() -> None:
    """Provider evidence cannot bypass immutable typed read boundaries."""
    read = ProviderRead(
        values=(),
        coverage=ProviderReadCoverage(1, 1),
        observed_at=NOW,
        diagnostics=(_diagnostic(),),
    )

    with pytest.raises(TypeError, match="values"):
        replace(read, values=cast("tuple[object, ...]", []))
    with pytest.raises(TypeError, match="coverage"):
        replace(read, coverage=cast("ProviderReadCoverage", object()))
    with pytest.raises(TypeError, match="diagnostics"):
        replace(read, diagnostics=cast("tuple[Diagnostic, ...]", (object(),)))
    with pytest.raises(TypeError, match="diagnostic services"):
        replace(read, diagnostic_services=("Compute.GoogleApis.com",))
    with pytest.raises(ValueError, match="diagnostic service"):
        read.diagnostics_for("Compute.GoogleApis.com")
    assert read.diagnostics_for("compute.googleapis.com") == (_diagnostic(),)


def test_monitoring_point_preserves_interval_and_typed_value() -> None:
    """Usage evidence retains provider intervals without inventing freshness."""
    start = datetime(2026, 7, 22, 1, tzinfo=UTC)
    end = datetime(2026, 7, 22, 2, tzinfo=UTC)
    point = MonitoringPoint(
        interval_start=start,
        interval_end=end,
        value=MonitoringValue(MonitoringValueKind.INT64, 2**63 - 1),
    )

    assert point.interval_start == start
    assert point.interval_end == end
    assert point.value.value == 2**63 - 1


def test_monitoring_point_rejects_reversed_intervals() -> None:
    """Malformed provider intervals cannot become trustworthy usage evidence."""
    with pytest.raises(ValueError, match="interval"):
        MonitoringPoint(
            interval_start=datetime(2026, 7, 22, 2, tzinfo=UTC),
            interval_end=datetime(2026, 7, 22, 1, tzinfo=UTC),
            value=MonitoringValue(MonitoringValueKind.DOUBLE, 1.5),
        )
