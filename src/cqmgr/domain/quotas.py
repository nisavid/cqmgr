"""Provider-neutral effective-quota identities and quantities."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from unicodedata import normalize

from cqmgr.domain.diagnostics import Diagnostic
from cqmgr.domain.schemas import ProviderSymbol
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.time import require_utc

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

SIGNED_64_MIN = -(2**63)
SIGNED_64_MAX = (2**63) - 1


@dataclass(frozen=True, slots=True, init=False)
class NormalizedDimensions:
    """An immutable, NFC-normalized dimension map in canonical key order."""

    items: tuple[tuple[str, str], ...]

    def __init__(self, pairs: Iterable[tuple[str, str]] = ()) -> None:
        """Normalize Unicode and reject keys that would lose information."""
        normalized_pairs: list[tuple[str, str]] = []
        seen_keys: set[str] = set()
        for key, value in pairs:
            if not isinstance(key, str) or not isinstance(value, str):
                msg = "dimension keys and values must be strings"
                raise TypeError(msg)
            normalized_key = normalize("NFC", key)
            normalized_value = normalize("NFC", value)
            if not normalized_key:
                msg = "dimension key must not be empty"
                raise ValueError(msg)
            if normalized_key in seen_keys:
                msg = (
                    "duplicate dimension key after NFC normalization: "
                    f"{normalized_key!r}"
                )
                raise ValueError(msg)
            seen_keys.add(normalized_key)
            normalized_pairs.append((normalized_key, normalized_value))

        object.__setattr__(self, "items", tuple(sorted(normalized_pairs)))


class QuotaScope(StrEnum):
    """Product classification of a quota's applicable location scope."""

    GLOBAL = "global"
    REGIONAL = "regional"
    ZONAL = "zonal"
    UNKNOWN = "unknown"


class MonitoringValueKind(StrEnum):
    """Closed protobuf scalar shapes accepted from Monitoring."""

    BOOL = "bool"
    INT64 = "int64"
    DOUBLE = "double"
    STRING = "string"


class QuotaIneligibilityReason(StrEnum):
    """Known Cloud Quotas increase-ineligibility reasons."""

    UNSPECIFIED = "INELIGIBILITY_REASON_UNSPECIFIED"
    NO_VALID_BILLING_ACCOUNT = "NO_VALID_BILLING_ACCOUNT"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    NOT_ENOUGH_USAGE_HISTORY = "NOT_ENOUGH_USAGE_HISTORY"
    OTHER = "OTHER"


class QuotaPreferenceOrigin(StrEnum):
    """Known provider origins for a quota preference."""

    UNSPECIFIED = "ORIGIN_UNSPECIFIED"
    CLOUD_CONSOLE = "CLOUD_CONSOLE"
    AUTO_ADJUSTER = "AUTO_ADJUSTER"


class QuotaContainerType(StrEnum):
    """Known provider container classifications for QuotaInfo."""

    UNSPECIFIED = "CONTAINER_TYPE_UNSPECIFIED"
    PROJECT = "PROJECT"
    FOLDER = "FOLDER"
    ORGANIZATION = "ORGANIZATION"


@dataclass(frozen=True, slots=True)
class MonitoringValue:
    """One lossless supported Monitoring typed value."""

    kind: MonitoringValueKind
    value: bool | int | float | str

    def __post_init__(self) -> None:
        """Keep kind and value coherent without numeric coercion."""
        if not isinstance(self.kind, MonitoringValueKind):
            msg = "monitoring value kind must be MonitoringValueKind"
            raise TypeError(msg)
        expected: dict[MonitoringValueKind, type[object]] = {
            MonitoringValueKind.BOOL: bool,
            MonitoringValueKind.INT64: int,
            MonitoringValueKind.DOUBLE: float,
            MonitoringValueKind.STRING: str,
        }
        if type(self.value) is not expected[self.kind]:
            msg = "monitoring value must match its explicit kind"
            raise TypeError(msg)
        if self.kind is MonitoringValueKind.INT64 and not (
            SIGNED_64_MIN <= self.value <= SIGNED_64_MAX  # type: ignore[operator]
        ):
            msg = "monitoring int64 value must fit a signed 64-bit integer"
            raise ValueError(msg)
        if self.kind is MonitoringValueKind.DOUBLE and not math.isfinite(
            self.value  # type: ignore[arg-type]
        ):
            msg = "monitoring double value must be finite"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MonitoringPoint:
    """One provider point with its exact explicit observation interval."""

    interval_start: datetime | None
    interval_end: datetime
    value: MonitoringValue

    def __post_init__(self) -> None:
        """Require UTC ordered intervals and a typed value."""
        require_utc(self.interval_end, "interval_end")
        if self.interval_start is not None:
            require_utc(self.interval_start, "interval_start")
            if self.interval_start > self.interval_end:
                msg = "monitoring point interval must not be reversed"
                raise ValueError(msg)
        if not isinstance(self.value, MonitoringValue):
            msg = "monitoring point value must be MonitoringValue"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class QuotaIncreaseEligibility:
    """Provider eligibility fact with unknown enum values preserved."""

    eligible: bool
    reason: ProviderSymbol[QuotaIneligibilityReason]

    def __post_init__(self) -> None:
        """Require an explicit boolean and correctly typed provider symbol."""
        if not isinstance(self.eligible, bool):
            msg = "quota eligibility must be boolean"
            raise TypeError(msg)
        if (
            not isinstance(self.reason, ProviderSymbol)
            or self.reason.enum_type is not QuotaIneligibilityReason
        ):
            msg = "quota eligibility reason must use QuotaIneligibilityReason"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class EffectiveQuotaEvidence:
    """One normalized effective QuotaInfo dimension slice."""

    identity: EffectiveQuotaSliceIdentity
    effective_value: QuotaQuantity
    metric: str
    declared_dimensions: tuple[str, ...]
    applicable_locations: tuple[str, ...]
    eligibility: QuotaIncreaseEligibility
    fixed: bool
    concurrent: bool
    precise: bool
    refresh_interval: str | None
    ongoing_rollout: bool
    container_type: ProviderSymbol[QuotaContainerType]
    metric_display_name: str | None = None
    quota_display_name: str | None = None

    def __post_init__(self) -> None:
        """Validate normalized, immutable provider evidence."""
        if not isinstance(self.identity, EffectiveQuotaSliceIdentity):
            msg = "effective quota evidence requires exact slice identity"
            raise TypeError(msg)
        if not isinstance(self.effective_value, QuotaQuantity):
            msg = "effective quota value must be QuotaQuantity"
            raise TypeError(msg)
        if not isinstance(self.metric, str) or not self.metric:
            msg = "effective quota metric must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.eligibility, QuotaIncreaseEligibility):
            msg = "effective quota eligibility must be typed"
            raise TypeError(msg)
        if (
            not isinstance(self.container_type, ProviderSymbol)
            or self.container_type.enum_type is not QuotaContainerType
        ):
            msg = "effective quota container_type must use QuotaContainerType"
            raise TypeError(msg)
        tuple_fields = (self.declared_dimensions, self.applicable_locations)
        if any(
            not isinstance(values, tuple)
            or any(not isinstance(value, str) for value in values)
            for values in tuple_fields
        ):
            msg = "effective quota collections must be tuples of strings"
            raise TypeError(msg)
        if any(
            not isinstance(value, bool)
            for value in (
                self.fixed,
                self.concurrent,
                self.precise,
                self.ongoing_rollout,
            )
        ):
            msg = "effective quota lifecycle facts must be booleans"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class QuotaPreferenceEvidence:
    """One normalized existing provider QuotaPreference."""

    provider_name: str
    identity: EffectiveQuotaSliceIdentity
    preferred_value: int
    granted_value: int | None
    etag: str | None
    reconciling: bool
    state_detail: str | None
    trace_id: str | None
    create_time: datetime | None
    update_time: datetime | None
    request_origin: ProviderSymbol[QuotaPreferenceOrigin]

    def __post_init__(self) -> None:
        """Keep provider lifecycle evidence exact and internally coherent."""
        if not isinstance(self.provider_name, str) or not self.provider_name:
            msg = "quota preference provider_name must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.identity, EffectiveQuotaSliceIdentity):
            msg = "quota preference requires exact slice identity"
            raise TypeError(msg)
        for name, value in (
            ("preferred_value", self.preferred_value),
            ("granted_value", self.granted_value),
        ):
            if value is None and name == "granted_value":
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not SIGNED_64_MIN <= value <= SIGNED_64_MAX
            ):
                msg = f"{name} must be a signed 64-bit integer"
                raise ValueError(msg)
        if not isinstance(self.reconciling, bool):
            msg = "quota preference reconciling must be boolean"
            raise TypeError(msg)
        for field_name, value in (
            ("create_time", self.create_time),
            ("update_time", self.update_time),
        ):
            if value is not None:
                require_utc(value, field_name)
        if (
            not isinstance(self.request_origin, ProviderSymbol)
            or self.request_origin.enum_type is not QuotaPreferenceOrigin
        ):
            msg = "quota preference origin must use QuotaPreferenceOrigin"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """One Monitoring time series with all explicit points and dimensions."""

    resource_scope: ResourceScope
    metric_type: str
    metric_labels: NormalizedDimensions
    resource_type: str
    resource_labels: NormalizedDimensions
    points: tuple[MonitoringPoint, ...]
    unit: str | None

    def __post_init__(self) -> None:
        """Require provider-neutral immutable series evidence."""
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "usage observation requires a ResourceScope"
            raise TypeError(msg)
        if not isinstance(self.metric_type, str) or not self.metric_type:
            msg = "usage metric_type must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.metric_labels, NormalizedDimensions) or not isinstance(
            self.resource_labels, NormalizedDimensions
        ):
            msg = "usage labels must use NormalizedDimensions"
            raise TypeError(msg)
        if not isinstance(self.resource_type, str) or not self.resource_type:
            msg = "usage resource_type must be non-empty"
            raise ValueError(msg)
        if not isinstance(self.points, tuple) or any(
            not isinstance(point, MonitoringPoint) for point in self.points
        ):
            msg = "usage points must be a tuple of MonitoringPoint"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ProviderReadCoverage:
    """Bounded page coverage for one provider read."""

    pages_attempted: int
    pages_completed: int
    page_cap_reached: bool = False

    def __post_init__(self) -> None:
        """Require conservative monotonic page accounting."""
        values = (self.pages_attempted, self.pages_completed)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            msg = "page coverage counts must be integers"
            raise TypeError(msg)
        if not 0 <= self.pages_completed <= self.pages_attempted:
            msg = "page coverage must satisfy completed <= attempted"
            raise ValueError(msg)
        if not isinstance(self.page_cap_reached, bool):
            msg = "page_cap_reached must be a boolean"
            raise TypeError(msg)

    @property
    def complete(self) -> bool:
        """Whether every attempted page completed and no next page was capped."""
        return (
            self.pages_completed == self.pages_attempted and not self.page_cap_reached
        )


@dataclass(frozen=True, slots=True)
class ProviderRead[ReadT]:
    """Usable normalized values with explicit fail-closed read coverage."""

    values: tuple[ReadT, ...]
    coverage: ProviderReadCoverage
    observed_at: datetime
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        """Require immutable values, typed coverage, and UTC observation time."""
        if not isinstance(self.values, tuple):
            msg = "provider read values must be a tuple"
            raise TypeError(msg)
        if not isinstance(self.coverage, ProviderReadCoverage):
            msg = "provider read coverage must be ProviderReadCoverage"
            raise TypeError(msg)
        require_utc(self.observed_at, "observed_at")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            msg = "provider read diagnostics must be a tuple of Diagnostic"
            raise TypeError(msg)

    @property
    def complete(self) -> bool:
        """Whether evidence can participate in a later mutation gate."""
        return self.coverage.complete and not self.diagnostics


@dataclass(frozen=True, slots=True)
class QuotaUnit:
    """An explicit provider-native quota unit symbol."""

    symbol: str

    def __post_init__(self) -> None:
        """Preserve every non-empty provider unit exactly."""
        if not isinstance(self.symbol, str):
            msg = "quota unit symbol must be a string"
            raise TypeError(msg)
        if not self.symbol:
            msg = "quota unit symbol must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class QuotaQuantity:
    """A signed 64-bit quota amount paired with its native unit."""

    value: int
    unit: QuotaUnit

    def __post_init__(self) -> None:
        """Reject lossy numeric types, booleans, and out-of-range values."""
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            msg = "quota quantity value must be an integer, not bool"
            raise TypeError(msg)
        if not SIGNED_64_MIN <= self.value <= SIGNED_64_MAX:
            msg = "quota quantity value must fit a signed 64-bit integer"
            raise ValueError(msg)
        if not isinstance(self.unit, QuotaUnit):
            msg = "quota quantity unit must be a QuotaUnit"
            raise TypeError(msg)

    @property
    def base10(self) -> str:
        """Return the canonical base-10 representation of the quantity."""
        return str(self.value)


@dataclass(frozen=True, slots=True)
class EffectiveQuotaSliceIdentity:
    """The complete stable identity of one effective quota slice."""

    resource_scope: ResourceScope
    service: str
    quota_id: str
    dimensions: NormalizedDimensions
    quota_scope: QuotaScope

    def __post_init__(self) -> None:
        """Require canonical values without rewriting provider identity."""
        if not isinstance(self.resource_scope, ResourceScope):
            msg = "resource_scope must be a ResourceScope"
            raise TypeError(msg)
        if not _is_canonical_service_dns(self.service):
            msg = "service must be a lowercase canonical service DNS name"
            raise ValueError(msg)
        if not isinstance(self.quota_id, str):
            msg = "quota_id must be a string"
            raise TypeError(msg)
        if not self.quota_id:
            msg = "quota_id must not be empty"
            raise ValueError(msg)
        if not isinstance(self.dimensions, NormalizedDimensions):
            msg = "dimensions must be NormalizedDimensions"
            raise TypeError(msg)
        if not isinstance(self.quota_scope, QuotaScope):
            msg = "quota_scope must be a QuotaScope"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class ConstraintReference:
    """A reference to one independently limiting exact quota slice."""

    slice_identity: EffectiveQuotaSliceIdentity

    def __post_init__(self) -> None:
        """Keep constraint references bound to complete slice identities."""
        if not isinstance(self.slice_identity, EffectiveQuotaSliceIdentity):
            msg = "slice_identity must be an EffectiveQuotaSliceIdentity"
            raise TypeError(msg)


def _is_canonical_service_dns(service: object) -> bool:
    """Return whether a value is an ASCII lowercase DNS service name."""
    if (
        not isinstance(service, str)
        or not service.isascii()
        or service != service.lower()
    ):
        return False
    labels = service.split(".")
    minimum_dns_labels = 2
    if len(labels) < minimum_dns_labels:
        return False
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
    return all(
        label
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character in allowed for character in label)
        for label in labels
    )
