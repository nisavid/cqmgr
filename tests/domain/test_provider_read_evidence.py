"""Provider-neutral read evidence contracts."""

from datetime import UTC, datetime

import pytest

from cqmgr.domain.quotas import (
    MonitoringPoint,
    MonitoringValue,
    MonitoringValueKind,
    ProviderRead,
    ProviderReadCoverage,
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
