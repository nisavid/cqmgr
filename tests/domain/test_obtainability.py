"""Behavioral contract for transparent Spot obtainability comparisons."""

from __future__ import annotations

# ruff: noqa: FBT003, PLR2004, PT007
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.domain.obtainability import (
    AdviceShard,
    CapacityAdvice,
    CapacityHistory,
    DistributionShape,
    GpuAttachment,
    ObtainabilityCandidate,
    ObtainabilityComparison,
    ObtainabilityProductCoverage,
    PreemptionInterval,
    PriceInterval,
    RankedCandidate,
    SpotMachineConfiguration,
    UnrankedReason,
    rank_candidates,
)

if TYPE_CHECKING:
    from cqmgr.domain.accelerator_overlay import ResolvedWorkloadRequirement

_OBSERVED_AT = datetime(2026, 7, 23, 12, tzinfo=UTC)


def _invalid_ranked_candidates() -> tuple[RankedCandidate, ...]:
    return cast("tuple[RankedCandidate, ...]", ("bad",))


def _invalid_resolver_provenance() -> ResolvedWorkloadRequirement:
    return cast("ResolvedWorkloadRequirement", "bad")


def test_rank_uses_band_nearest_rank_p90_and_current_total_request_price() -> None:
    """All three independently attributable components determine transparent rank."""
    candidate = ObtainabilityCandidate(
        endpoint_region="us-central1",
        zones=("us-central1-a", "us-central1-b"),
        machine=SpotMachineConfiguration("a3-highgpu-8g"),
        vm_count=4,
        distribution_shape=DistributionShape.BALANCED,
    )
    rates = tuple(
        PreemptionInterval(
            started_at=_OBSERVED_AT - timedelta(days=31 - day),
            finished_at=_OBSERVED_AT - timedelta(days=30 - day),
            rate=Decimal(day) / Decimal(100),
        )
        for day in range(1, 31)
    )
    history = CapacityHistory(
        machine_type="a3-highgpu-8g",
        location="us-central1",
        preemption=rates,
        prices=(
            PriceInterval(
                started_at=_OBSERVED_AT - timedelta(days=1),
                finished_at=_OBSERVED_AT + timedelta(days=1),
                usd_per_vm_hour=Decimal("1.25"),
            ),
        ),
        retrieved_at=_OBSERVED_AT,
    )

    ranked = rank_candidates(
        (
            (
                candidate,
                CapacityAdvice(Decimal("0.8"), "3600s", (), _OBSERVED_AT),
                history,
            ),
        )
    )

    assert ranked[0].rank == 1
    assert ranked[0].band is not None
    assert ranked[0].band.value == "high"
    assert ranked[0].preemption_p90 == Decimal("0.27")
    assert ranked[0].total_request_hourly_price_usd == Decimal("5.00")
    assert ranked[0].unranked_reasons == ()


def test_exact_rank_tie_uses_canonical_candidate_identity() -> None:
    """Canonical immutable identity is the final deterministic tie-breaker."""
    candidates = tuple(
        ObtainabilityCandidate(
            endpoint_region=region,
            zones=(),
            machine=SpotMachineConfiguration("a3-highgpu-8g"),
            vm_count=4,
            distribution_shape=DistributionShape.ANY,
        )
        for region in ("us-central1", "us-east1")
    )
    history = CapacityHistory(
        "a3-highgpu-8g",
        "us-central1",
        tuple(
            PreemptionInterval(
                _OBSERVED_AT - timedelta(days=31 - day),
                _OBSERVED_AT - timedelta(days=30 - day),
                Decimal(day) / Decimal(100),
            )
            for day in range(1, 31)
        ),
        (
            PriceInterval(
                _OBSERVED_AT - timedelta(days=1),
                _OBSERVED_AT + timedelta(days=1),
                Decimal("1.25"),
            ),
        ),
        _OBSERVED_AT,
    )
    advice = CapacityAdvice(Decimal("0.8"), "3600s", (), _OBSERVED_AT)

    ranked = rank_candidates(
        tuple((candidate, advice, history) for candidate in candidates)
    )
    comparison = ObtainabilityComparison(ranked)

    assert tuple(item.candidate.candidate_id for item in ranked) == tuple(
        sorted(candidate.candidate_id for candidate in candidates)
    )
    assert tuple(item.rank for item in ranked) == (1, 2)
    assert comparison.tied_candidate_ids == frozenset(
        candidate.candidate_id for candidate in candidates
    )
    for assessment in comparison.candidates:
        price = assessment.price_derivation
        assert price is not None
        assert price.interval.usd_per_vm_hour == Decimal("1.25")
        assert price.vm_count == 4
        assert price.total_request_hourly_price_usd == Decimal("5.00")
        preemption = assessment.preemption_derivation
        assert preemption is not None
        assert preemption.nearest_rank == 27
        assert preemption.selected_rate == assessment.preemption_p90


def test_missing_and_non_attributable_components_have_exact_unranked_reasons() -> None:
    """Required evidence is never converted to a numeric worst value."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        1,
        DistributionShape.ANY,
    )
    advice = CapacityAdvice(Decimal("0.8"), "3600s", (), _OBSERVED_AT)
    incomplete = CapacityHistory(
        "a3-highgpu-8g",
        "us-central1",
        tuple(
            PreemptionInterval(
                _OBSERVED_AT - timedelta(days=day + 1),
                _OBSERVED_AT - timedelta(days=day),
                Decimal("0.1"),
            )
            for day in range(29)
        ),
        (),
        _OBSERVED_AT,
        price_attributable=False,
    )

    assessed = rank_candidates(((candidate, advice, incomplete),))[0]

    assert assessed.rank is None
    assert assessed.preemption_p90 is None
    assert assessed.total_request_hourly_price_usd is None
    assert assessed.unranked_reasons == (
        UnrankedReason.PREEMPTION_WINDOW_INCOMPLETE,
        UnrankedReason.PRICE_NON_ATTRIBUTABLE,
    )


@pytest.mark.parametrize(
    ("factory", "exception"),
    (
        (
            lambda: SpotMachineConfiguration(
                "n2-standard-4",
                gpu=cast("GpuAttachment", "bad"),
            ),
            TypeError,
        ),
        (
            lambda: SpotMachineConfiguration("n2-standard-4", local_ssd_count=-1),
            ValueError,
        ),
        (
            lambda: ObtainabilityCandidate(
                "us-central1",
                ("us-east1-a",),
                SpotMachineConfiguration("n2-standard-4"),
                1,
                DistributionShape.ANY,
            ),
            ValueError,
        ),
        (
            lambda: ObtainabilityCandidate(
                "us-central1",
                (),
                cast("SpotMachineConfiguration", "bad"),
                1,
                DistributionShape.ANY,
            ),
            TypeError,
        ),
        (lambda: AdviceShard("region", "n2-standard-4", 1, "SPOT"), ValueError),
        (
            lambda: CapacityAdvice(Decimal(2), "3600s", (), _OBSERVED_AT),
            ValueError,
        ),
        (
            lambda: CapacityAdvice(
                Decimal("0.5"),
                "3600s",
                cast("tuple[AdviceShard, ...]", ("bad",)),
                _OBSERVED_AT,
            ),
            TypeError,
        ),
        (
            lambda: PreemptionInterval(
                _OBSERVED_AT,
                _OBSERVED_AT,
                Decimal("0.5"),
            ),
            ValueError,
        ),
        (
            lambda: PreemptionInterval(
                _OBSERVED_AT,
                _OBSERVED_AT + timedelta(days=1),
                Decimal(2),
            ),
            ValueError,
        ),
        (
            lambda: PriceInterval(
                _OBSERVED_AT,
                _OBSERVED_AT + timedelta(days=1),
                Decimal(-1),
            ),
            ValueError,
        ),
        (
            lambda: CapacityHistory(
                "n2-standard-4",
                "invalid",
                (),
                (),
                _OBSERVED_AT,
            ),
            ValueError,
        ),
        (
            lambda: CapacityHistory(
                "n2-standard-4",
                "us-central1",
                cast("tuple[PreemptionInterval, ...]", ("bad",)),
                (),
                _OBSERVED_AT,
            ),
            TypeError,
        ),
        (
            lambda: CapacityHistory(
                "n2-standard-4",
                "us-central1",
                (),
                cast("tuple[PriceInterval, ...]", ("bad",)),
                _OBSERVED_AT,
            ),
            TypeError,
        ),
        (
            lambda: CapacityHistory(
                "n2-standard-4",
                "us-central1",
                (),
                (),
                _OBSERVED_AT,
                preemption_attributable=cast("bool", 1),
            ),
            TypeError,
        ),
        (
            lambda: ObtainabilityComparison(_invalid_ranked_candidates()),
            TypeError,
        ),
        (
            lambda: ObtainabilityComparison(
                (),
                resolver_provenance=_invalid_resolver_provenance(),
            ),
            TypeError,
        ),
        (
            lambda: ObtainabilityProductCoverage(
                "tpu-v6e",
                "compute.googleapis.com",
                cast("bool", 1),
                False,
                False,
                ("unsupported",),
            ),
            TypeError,
        ),
        (
            lambda: ObtainabilityProductCoverage(
                "tpu-v6e",
                "compute.googleapis.com",
                True,
                False,
                False,
            ),
            ValueError,
        ),
    ),
)
def test_obtainability_domain_rejects_ambiguous_or_untyped_evidence(
    factory: object,
    exception: type[Exception],
) -> None:
    """Public constructors reject values that could create invented evidence."""
    with pytest.raises(exception):
        factory()  # type: ignore[operator]


def test_rank_retains_distinct_non_attributable_and_incomplete_price_reasons() -> None:
    """Unranked evidence identifies which independent ranking component failed."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("n2-standard-4"),
        1,
        DistributionShape.ANY,
    )
    advice = CapacityAdvice(Decimal("0.2"), "3600s", (), _OBSERVED_AT)
    history = CapacityHistory(
        "n2-standard-4",
        "us-central1",
        (),
        (),
        _OBSERVED_AT,
        preemption_attributable=False,
        price_covers_complete_machine=False,
    )

    assessed = rank_candidates(((candidate, advice, history),))[0]

    assert advice.band.value == "low"
    assert assessed.unranked_reasons == (
        UnrankedReason.PREEMPTION_NON_ATTRIBUTABLE,
        UnrankedReason.PRICE_INCOMPLETE_MACHINE,
    )


@pytest.mark.parametrize("mutation", ["gap", "overlap", "duplicate", "out-of-order"])
def test_preemption_window_requires_ordered_exactly_contiguous_daily_intervals(
    mutation: str,
) -> None:
    """Thirty rates are incomplete when their daily interval sequence is invalid."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("n2-standard-4"),
        1,
        DistributionShape.ANY,
    )
    intervals = [
        PreemptionInterval(
            _OBSERVED_AT - timedelta(days=30 - index),
            _OBSERVED_AT - timedelta(days=29 - index),
            Decimal("0.1"),
        )
        for index in range(30)
    ]
    if mutation == "gap":
        intervals[10] = PreemptionInterval(
            intervals[10].started_at + timedelta(hours=1),
            intervals[10].finished_at + timedelta(hours=1),
            intervals[10].rate,
        )
    elif mutation == "overlap":
        intervals[10] = PreemptionInterval(
            intervals[10].started_at - timedelta(hours=1),
            intervals[10].finished_at - timedelta(hours=1),
            intervals[10].rate,
        )
    elif mutation == "duplicate":
        intervals[10] = intervals[9]
    else:
        intervals[9], intervals[10] = intervals[10], intervals[9]
    history = CapacityHistory(
        "n2-standard-4",
        "us-central1",
        tuple(intervals),
        (
            PriceInterval(
                _OBSERVED_AT - timedelta(days=1),
                _OBSERVED_AT + timedelta(days=1),
                Decimal(1),
            ),
        ),
        _OBSERVED_AT,
    )

    ranked = rank_candidates(
        (
            (
                candidate,
                CapacityAdvice(Decimal("0.8"), "3600s", (), _OBSERVED_AT),
                history,
            ),
        )
    )

    assert ranked[0].preemption_p90 is None
    assert UnrankedReason.PREEMPTION_WINDOW_INCOMPLETE in ranked[0].unranked_reasons


def test_cataloged_product_requires_reason_for_history_only_unsupported() -> None:
    """Catalog visibility explains independently unsupported history evidence."""
    with pytest.raises(ValueError, match="exact coverage reason"):
        ObtainabilityProductCoverage(
            "n1-attached-gpu",
            "compute.googleapis.com",
            cataloged=True,
            current_advice_supported=True,
            history_supported=False,
        )


def test_n1_gpu_forced_history_reason_is_not_duplicated() -> None:
    """Automatic N1 coverage logic preserves one stable unranked reason."""
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration(
            "n1-standard-16",
            GpuAttachment("nvidia-tesla-t4", 1),
        ),
        1,
        DistributionShape.ANY,
    )

    ranked = rank_candidates(
        ((candidate, None, None),),
        forced_reasons={
            candidate.candidate_id: (UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU,)
        },
    )

    assert (
        ranked[0].unranked_reasons.count(UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU) == 1
    )
