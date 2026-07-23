"""Human and structured Spot obtainability presentation contracts."""

from __future__ import annotations

# ruff: noqa: PLR2004
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from cqmgr.adapters.cli.read_only import Presentation, emit_read_only_result
from cqmgr.domain.obtainability import (
    CapacityAdvice,
    CapacityHistory,
    DistributionShape,
    ObtainabilityCandidate,
    ObtainabilityComparison,
    PreemptionInterval,
    PriceInterval,
    SpotMachineConfiguration,
    rank_candidates,
)
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")


def _result() -> OperationResult[ObtainabilityComparison]:
    candidate = ObtainabilityCandidate(
        "us-central1",
        ("us-central1-a",),
        SpotMachineConfiguration("a3-highgpu-8g"),
        4,
        DistributionShape.ANY_SINGLE_ZONE,
    )
    history = CapacityHistory(
        "a3-highgpu-8g",
        "us-central1-a",
        tuple(
            PreemptionInterval(
                NOW - timedelta(days=31 - day),
                NOW - timedelta(days=30 - day),
                Decimal(day) / Decimal(100),
            )
            for day in range(1, 31)
        ),
        (
            PriceInterval(
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                Decimal("1.25"),
            ),
        ),
        NOW,
    )
    comparison = ObtainabilityComparison(
        rank_candidates(
            (
                (
                    candidate,
                    CapacityAdvice(Decimal("0.8"), "3600s", (), NOW),
                    history,
                ),
            )
        )
    )
    return OperationResult(
        OperationName("obtainability.compare"),
        SCOPE,
        OperationBoundary(StableSymbol("spot-advice-assessed"), reached=True),
        Outcome(StableSymbol("spot-advice-assessed"), ExitClass.SUCCESS),
        Completeness.complete(),
        NOW,
        NOW,
        comparison,
    )


def test_obtainability_human_output_keeps_identity_derivations_and_disclaimer(
    capsys: CaptureFixture[str],
) -> None:
    """Human output exposes every rank component and avoids a capacity claim."""
    emit_read_only_result(
        _result(),
        Presentation("human", no_color=True, quiet=False),
    )

    output = capsys.readouterr().out
    assert "Candidate endpoint region: us-central1" in output
    assert "Candidate zones: us-central1-a" in output
    assert "Machine type: a3-highgpu-8g" in output
    assert "Provider obtainability: 0.8 (high)" in output
    assert "30-day p90 preemption rate: 0.27" in output
    assert "Total-request hourly price: USD 5.00" in output
    assert "Capacity guarantee: no" in output
    assert "Provider status: Preview" in output


def test_obtainability_json_output_preserves_exact_decimal_and_request_identity(
    capsys: CaptureFixture[str],
) -> None:
    """Structured output derives from the same typed result without float coercion."""
    emit_read_only_result(
        _result(),
        Presentation("json", no_color=True, quiet=False),
    )

    output = capsys.readouterr().out
    mapping = json.loads(output)
    candidate = mapping["data"]["candidates"][0]
    assert candidate["candidate"]["vm_count"] == 4
    assert candidate["candidate"]["zones"] == ["us-central1-a"]
    assert candidate["advice"]["obtainability"] == "0.8"
    assert candidate["preemption_p90"] == "0.27"
    assert candidate["total_request_hourly_price_usd"] == "5.00"
    assert mapping["data"]["no_capacity_guarantee"] is True
