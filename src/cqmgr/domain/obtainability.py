"""Exact Spot VM request evidence and transparent obtainability ranking."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from cqmgr.domain.accelerator_overlay import ResolvedWorkloadRequirement
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

_DAY_COUNT = 30
_P90_NEAREST_RANK_INDEX = 26
_CANONICAL_LOCATION_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class DistributionShape(StrEnum):
    """Provider-defined distribution intent for one immutable candidate."""

    ANY = "any"
    ANY_SINGLE_ZONE = "any-single-zone"
    BALANCED = "balanced"

    @property
    def provider_value(self) -> str:
        """Return the exact Compute API enum spelling."""
        return self.value.replace("-", "_").upper()


class ObtainabilityBand(StrEnum):
    """Documented provider obtainability bands used by the first rank component."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class GpuAttachment:
    """One exact N1 guest-accelerator attachment."""

    accelerator_type: str
    count: int

    def __post_init__(self) -> None:
        """Require a complete provider accelerator identity and positive count."""
        _require_nonempty(self.accelerator_type, "accelerator_type")
        _require_positive_int(self.count, "gpu count")


@dataclass(frozen=True, slots=True)
class SpotMachineConfiguration:
    """One exact Spot VM machine selection used by both advice sources."""

    machine_type: str
    gpu: GpuAttachment | None = None
    local_ssd_count: int = 0

    def __post_init__(self) -> None:
        """Keep optional attachments exact and reject unsupported partial shapes."""
        _require_nonempty(self.machine_type, "machine_type")
        if self.gpu is not None and not isinstance(self.gpu, GpuAttachment):
            msg = "gpu must be a GpuAttachment or None"
            raise TypeError(msg)
        if (
            isinstance(self.local_ssd_count, bool)
            or not isinstance(self.local_ssd_count, int)
            or self.local_ssd_count < 0
        ):
            msg = "local_ssd_count must be a non-negative integer"
            raise ValueError(msg)

    @property
    def is_n1_attached_gpu(self) -> bool:
        """Whether history is explicitly unsupported for this exact shape."""
        return self.machine_type.startswith("n1-") and self.gpu is not None


@dataclass(frozen=True, slots=True)
class ObtainabilityCandidate:
    """One immutable provider-request snapshot within a fixed comparison."""

    endpoint_region: str
    zones: tuple[str, ...]
    machine: SpotMachineConfiguration
    vm_count: int
    distribution_shape: DistributionShape
    candidate_id: str = field(init=False)

    def __post_init__(self) -> None:
        """Require region-consistent zones and derive canonical immutable identity."""
        _require_region(self.endpoint_region)
        if (
            not isinstance(self.zones, tuple)
            or any(not _is_zone(zone) for zone in self.zones)
            or len(set(self.zones)) != len(self.zones)
            or any(
                _region_for_zone(zone) != self.endpoint_region for zone in self.zones
            )
        ):
            msg = "candidate zones must be unique canonical zones in endpoint region"
            raise ValueError(msg)
        if not isinstance(self.machine, SpotMachineConfiguration):
            msg = "candidate machine must be a SpotMachineConfiguration"
            raise TypeError(msg)
        _require_positive_int(self.vm_count, "vm_count")
        if not isinstance(self.distribution_shape, DistributionShape):
            msg = "distribution_shape must be a DistributionShape"
            raise TypeError(msg)
        identity = "\x1f".join(
            (
                self.endpoint_region,
                ",".join(self.zones),
                self.machine.machine_type,
                "" if self.machine.gpu is None else self.machine.gpu.accelerator_type,
                "" if self.machine.gpu is None else str(self.machine.gpu.count),
                str(self.machine.local_ssd_count),
                str(self.vm_count),
                self.distribution_shape.value,
            )
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        object.__setattr__(self, "candidate_id", f"sha256:{digest}")


@dataclass(frozen=True, slots=True)
class AdviceShard:
    """Provider-returned uniform placement shard without a derived zone score."""

    zone: str
    machine_type: str
    vm_count: int
    provisioning_model: str

    def __post_init__(self) -> None:
        """Preserve the exact shard allocation as provider evidence."""
        _require_zone(self.zone)
        _require_nonempty(self.machine_type, "shard machine_type")
        _require_positive_int(self.vm_count, "shard vm_count")
        _require_nonempty(self.provisioning_model, "shard provisioning_model")


@dataclass(frozen=True, slots=True)
class CapacityAdvice:
    """Normalized current provider advice for one complete candidate."""

    obtainability: Decimal
    estimated_uptime: str
    shards: tuple[AdviceShard, ...]
    retrieved_at: datetime
    preview_status: str = "Preview"
    source: str = (
        "https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacity"
    )

    def __post_init__(self) -> None:
        """Require one exact score and client-observed retrieval time."""
        if (
            not isinstance(self.obtainability, Decimal)
            or self.obtainability < 0
            or self.obtainability > 1
        ):
            msg = "obtainability must be a Decimal from 0 through 1"
            raise ValueError(msg)
        _require_nonempty(self.estimated_uptime, "estimated_uptime")
        if not isinstance(self.shards, tuple) or any(
            not isinstance(item, AdviceShard) for item in self.shards
        ):
            msg = "shards must contain AdviceShard values"
            raise TypeError(msg)
        require_utc(self.retrieved_at, "retrieved_at")
        _require_nonempty(self.preview_status, "preview_status")
        _require_nonempty(self.source, "source")

    @property
    def band(self) -> ObtainabilityBand:
        """Classify only the provider's documented non-overlapping bands."""
        if self.obtainability >= Decimal("0.7"):
            return ObtainabilityBand.HIGH
        if self.obtainability >= Decimal("0.4"):
            return ObtainabilityBand.MEDIUM
        return ObtainabilityBand.LOW


@dataclass(frozen=True, slots=True)
class PreemptionInterval:
    """One candidate-attributable provider daily preemption bucket."""

    started_at: datetime
    finished_at: datetime
    rate: Decimal

    def __post_init__(self) -> None:
        """Preserve inclusive start, exclusive end, and exact provider rate."""
        _require_interval(self.started_at, self.finished_at)
        if not isinstance(self.rate, Decimal) or self.rate < 0 or self.rate > 1:
            msg = "preemption rate must be a Decimal from 0 through 1"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """One regional USD per-VM-hour interval for a complete machine request."""

    started_at: datetime
    finished_at: datetime
    usd_per_vm_hour: Decimal

    def __post_init__(self) -> None:
        """Preserve inclusive start, exclusive end, and exact USD price."""
        _require_interval(self.started_at, self.finished_at)
        if not isinstance(self.usd_per_vm_hour, Decimal) or self.usd_per_vm_hour < 0:
            msg = "USD hourly price must be a non-negative Decimal"
            raise ValueError(msg)

    def contains(self, when: datetime) -> bool:
        """Apply the provider's inclusive-start, exclusive-end interval contract."""
        require_utc(when, "price retrieval time")
        return self.started_at <= when < self.finished_at


@dataclass(frozen=True, slots=True)
class CapacityHistory:
    """Normalized regional or zonal Spot history with explicit attribution."""

    machine_type: str
    location: str
    preemption: tuple[PreemptionInterval, ...]
    prices: tuple[PriceInterval, ...]
    retrieved_at: datetime
    preemption_attributable: bool = True
    price_attributable: bool = True
    price_covers_complete_machine: bool = True
    source: str = (
        "https://cloud.google.com/compute/docs/reference/rest/beta/"
        "advice/capacityHistory"
    )

    def __post_init__(self) -> None:
        """Keep location, intervals, coverage, and attribution independent."""
        _require_nonempty(self.machine_type, "history machine_type")
        if not (_is_region(self.location) or _is_zone(self.location)):
            msg = "history location must be a canonical region or zone"
            raise ValueError(msg)
        if not isinstance(self.preemption, tuple) or any(
            not isinstance(item, PreemptionInterval) for item in self.preemption
        ):
            msg = "preemption must contain PreemptionInterval values"
            raise TypeError(msg)
        if not isinstance(self.prices, tuple) or any(
            not isinstance(item, PriceInterval) for item in self.prices
        ):
            msg = "prices must contain PriceInterval values"
            raise TypeError(msg)
        require_utc(self.retrieved_at, "retrieved_at")
        if any(
            not isinstance(value, bool)
            for value in (
                self.preemption_attributable,
                self.price_attributable,
                self.price_covers_complete_machine,
            )
        ):
            msg = "history attribution flags must be boolean"
            raise TypeError(msg)
        _require_nonempty(self.source, "source")


class UnrankedReason(StrEnum):
    """Exact reason one independently required ranking component is unavailable."""

    CURRENT_ADVICE_UNSUPPORTED = "current-advice-unsupported"
    ADVICE_UNAVAILABLE = "current-advice-unavailable"
    ADVICE_INCOMPLETE = "current-advice-incomplete"
    HISTORY_UNSUPPORTED = "history-unsupported"
    HISTORY_UNSUPPORTED_N1_GPU = "history-unsupported-n1-attached-gpu"
    HISTORY_UNAVAILABLE = "history-unavailable"
    HISTORY_INCOMPLETE = "history-incomplete"
    PREEMPTION_NON_ATTRIBUTABLE = "preemption-non-attributable"
    PREEMPTION_WINDOW_INCOMPLETE = "preemption-window-incomplete"
    PRICE_NON_ATTRIBUTABLE = "price-non-attributable"
    PRICE_INCOMPLETE_MACHINE = "price-incomplete-machine-request"
    CURRENT_PRICE_UNAVAILABLE = "current-price-unavailable"
    CATALOG_UNSUPPORTED = "configuration-not-cataloged"
    SPOT_UNSUPPORTED = "spot-unsupported"
    NON_COMPUTE_MANAGEMENT_PLANE = "non-compute-management-plane"


@dataclass(frozen=True, slots=True)
class PreemptionP90Derivation:
    """The exact complete interval set and nearest-rank selection."""

    intervals: tuple[PreemptionInterval, ...]
    nearest_rank: int
    selected_rate: Decimal


@dataclass(frozen=True, slots=True)
class CurrentPriceDerivation:
    """The exact applicable per-VM interval and request multiplication."""

    interval: PriceInterval
    vm_count: int
    total_request_hourly_price_usd: Decimal


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One comparison candidate with every visible derivation and exact reasons."""

    candidate: ObtainabilityCandidate
    advice: CapacityAdvice | None
    history: CapacityHistory | None
    band: ObtainabilityBand | None
    preemption_p90: Decimal | None
    total_request_hourly_price_usd: Decimal | None
    rank: int | None
    unranked_reasons: tuple[UnrankedReason, ...]
    no_capacity_guarantee: bool = field(default=True, init=False)

    @property
    def preemption_derivation(self) -> PreemptionP90Derivation | None:
        """Return the retained 30-bucket nearest-rank derivation when complete."""
        if self.preemption_p90 is None or self.history is None:
            return None
        return PreemptionP90Derivation(
            self.history.preemption,
            _P90_NEAREST_RANK_INDEX + 1,
            self.preemption_p90,
        )

    @property
    def price_derivation(self) -> CurrentPriceDerivation | None:
        """Return the one interval used for exact total-request price."""
        if (
            self.total_request_hourly_price_usd is None
            or self.history is None
            or self.advice is None
        ):
            return None
        applicable = tuple(
            interval
            for interval in self.history.prices
            if interval.contains(self.advice.retrieved_at)
        )
        if len(applicable) != 1:
            return None
        return CurrentPriceDerivation(
            applicable[0],
            self.candidate.vm_count,
            self.total_request_hourly_price_usd,
        )


@dataclass(frozen=True, slots=True)
class ObtainabilityComparison:
    """One fixed configuration compared across exact immutable candidates."""

    candidates: tuple[RankedCandidate, ...]
    catalog_coverage: tuple[ObtainabilityProductCoverage, ...] = ()
    resolver_provenance: ResolvedWorkloadRequirement | None = None
    preview_status: str = "Preview"
    no_capacity_guarantee: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        """Require unique candidate identity and explicit Preview labeling."""
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(item, RankedCandidate) for item in self.candidates
        ):
            msg = "comparison candidates must contain RankedCandidate values"
            raise TypeError(msg)
        if len({item.candidate.candidate_id for item in self.candidates}) != len(
            self.candidates
        ):
            msg = "comparison candidate identities must be unique"
            raise ValueError(msg)
        if not isinstance(self.catalog_coverage, tuple) or any(
            not isinstance(item, ObtainabilityProductCoverage)
            for item in self.catalog_coverage
        ):
            msg = "catalog coverage must contain ObtainabilityProductCoverage values"
            raise TypeError(msg)
        if self.resolver_provenance is not None and not isinstance(
            self.resolver_provenance,
            ResolvedWorkloadRequirement,
        ):
            msg = "resolver provenance must be a ResolvedWorkloadRequirement or None"
            raise TypeError(msg)
        _require_nonempty(self.preview_status, "preview_status")

    @property
    def tied_candidate_ids(self) -> frozenset[str]:
        """Identify exact rank-component ties before canonical identity ordering."""
        groups: dict[
            tuple[ObtainabilityBand | None, Decimal | None, Decimal | None],
            list[str],
        ] = {}
        for assessment in self.candidates:
            if assessment.unranked_reasons:
                continue
            components = (
                assessment.band,
                assessment.preemption_p90,
                assessment.total_request_hourly_price_usd,
            )
            groups.setdefault(components, []).append(assessment.candidate.candidate_id)
        return frozenset(
            candidate_id
            for identities in groups.values()
            if len(identities) > 1
            for candidate_id in identities
        )


@dataclass(frozen=True, slots=True)
class ObtainabilityProductCoverage:
    """Advice support kept separate from catalog presence for one product."""

    product_id: str
    service: str
    cataloged: bool
    current_advice_supported: bool
    history_supported: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Require explicit support facts and exact non-empty coverage reasons."""
        _require_nonempty(self.product_id, "product_id")
        _require_nonempty(self.service, "service")
        if any(
            not isinstance(value, bool)
            for value in (
                self.cataloged,
                self.current_advice_supported,
                self.history_supported,
            )
        ):
            msg = "product coverage flags must be boolean"
            raise TypeError(msg)
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.reasons
        ):
            msg = "product coverage reasons must be non-empty strings"
            raise TypeError(msg)
        unsupported = not self.current_advice_supported or not self.history_supported
        if self.cataloged and unsupported and not self.reasons:
            msg = "unsupported cataloged products require an exact coverage reason"
            raise ValueError(msg)


def rank_candidates(
    evidence: tuple[
        tuple[
            ObtainabilityCandidate,
            CapacityAdvice | None,
            CapacityHistory | None,
        ],
        ...,
    ],
    *,
    forced_reasons: Mapping[str, tuple[UnrankedReason, ...]] | None = None,
) -> tuple[RankedCandidate, ...]:
    """Derive exact components, rank complete candidates, and retain input evidence."""
    reason_map = {} if forced_reasons is None else forced_reasons
    assessed = [
        _assess(
            *item,
            forced_reasons=reason_map.get(item[0].candidate_id, ()),
        )
        for item in evidence
    ]
    comparable = sorted(
        (item for item in assessed if not item.unranked_reasons),
        key=lambda item: (
            -_band_weight(item.band),
            item.preemption_p90,
            item.total_request_hourly_price_usd,
            item.candidate.candidate_id,
        ),
    )
    ranks = {
        item.candidate.candidate_id: index for index, item in enumerate(comparable, 1)
    }
    return tuple(
        RankedCandidate(
            candidate=item.candidate,
            advice=item.advice,
            history=item.history,
            band=item.band,
            preemption_p90=item.preemption_p90,
            total_request_hourly_price_usd=item.total_request_hourly_price_usd,
            rank=ranks.get(item.candidate.candidate_id),
            unranked_reasons=item.unranked_reasons,
        )
        for item in sorted(
            assessed,
            key=lambda item: (
                item.unranked_reasons != (),
                ranks.get(item.candidate.candidate_id, 0),
                item.candidate.candidate_id,
            ),
        )
    )


def _assess(  # noqa: C901, PLR0912 - evidence gaps remain independently visible
    candidate: ObtainabilityCandidate,
    advice: CapacityAdvice | None,
    history: CapacityHistory | None,
    *,
    forced_reasons: tuple[UnrankedReason, ...] = (),
) -> RankedCandidate:
    reasons = list(forced_reasons)
    band = advice.band if advice is not None else None
    if advice is None and UnrankedReason.CURRENT_ADVICE_UNSUPPORTED not in reasons:
        reasons.append(UnrankedReason.ADVICE_UNAVAILABLE)
    p90: Decimal | None = None
    current_total: Decimal | None = None
    if candidate.machine.is_n1_attached_gpu:
        if UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU not in reasons:
            reasons.append(UnrankedReason.HISTORY_UNSUPPORTED_N1_GPU)
    elif history is None:
        if UnrankedReason.HISTORY_UNSUPPORTED not in reasons:
            reasons.append(UnrankedReason.HISTORY_UNAVAILABLE)
    else:
        if not history.preemption_attributable:
            reasons.append(UnrankedReason.PREEMPTION_NON_ATTRIBUTABLE)
        elif not _complete_preemption_window(history.preemption):
            reasons.append(UnrankedReason.PREEMPTION_WINDOW_INCOMPLETE)
        else:
            p90 = sorted(item.rate for item in history.preemption)[
                _P90_NEAREST_RANK_INDEX
            ]
        if not history.price_attributable:
            reasons.append(UnrankedReason.PRICE_NON_ATTRIBUTABLE)
        elif not history.price_covers_complete_machine:
            reasons.append(UnrankedReason.PRICE_INCOMPLETE_MACHINE)
        else:
            containing = (
                tuple(
                    item
                    for item in history.prices
                    if item.contains(advice.retrieved_at)
                )
                if advice is not None
                else ()
            )
            if len(containing) != 1:
                reasons.append(UnrankedReason.CURRENT_PRICE_UNAVAILABLE)
            else:
                current_total = containing[0].usd_per_vm_hour * candidate.vm_count
    return RankedCandidate(
        candidate,
        advice,
        history,
        band,
        p90,
        current_total,
        None,
        tuple(reasons),
    )


def _band_weight(band: ObtainabilityBand | None) -> int:
    return {
        ObtainabilityBand.HIGH: 3,
        ObtainabilityBand.MEDIUM: 2,
        ObtainabilityBand.LOW: 1,
        None: 0,
    }[band]


def _complete_preemption_window(
    intervals: tuple[PreemptionInterval, ...],
) -> bool:
    """Require 30 ordered, exactly contiguous provider daily intervals."""
    if len(intervals) != _DAY_COUNT:
        return False
    return all(
        item.finished_at - item.started_at == timedelta(days=1)
        and (index == 0 or intervals[index - 1].finished_at == item.started_at)
        for index, item in enumerate(intervals)
    )


def _require_interval(started_at: datetime, finished_at: datetime) -> None:
    require_utc(started_at, "started_at")
    require_utc(finished_at, "finished_at")
    if finished_at <= started_at:
        msg = "interval finish must be after start"
        raise ValueError(msg)


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{field_name} must be a positive integer"
        raise ValueError(msg)


def _require_nonempty(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        msg = f"{field_name} must be a non-empty string"
        raise ValueError(msg)


def _is_region(value: object) -> bool:
    return (
        isinstance(value, str)
        and "-" in value
        and all(value.split("-"))
        and all(character in _CANONICAL_LOCATION_CHARACTERS for character in value)
        and value[-1:].isdigit()
    )


def _is_zone(value: object) -> bool:
    if not isinstance(value, str):
        return False
    region, separator, suffix = value.rpartition("-")
    return (
        separator == "-"
        and _is_region(region)
        and len(suffix) == 1
        and suffix.isascii()
        and suffix.isalpha()
        and suffix.islower()
    )


def _require_region(value: object) -> None:
    if not _is_region(value):
        msg = "endpoint_region must be a canonical region"
        raise ValueError(msg)


def _require_zone(value: object) -> None:
    if not _is_zone(value):
        msg = "zone must be a canonical zone"
        raise ValueError(msg)


def _region_for_zone(value: str) -> str:
    return value.rpartition("-")[0]
