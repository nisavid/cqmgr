"""Provider-neutral effective-quota identities and quantities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from unicodedata import normalize

from cqmgr.domain.scopes import ResourceScope

if TYPE_CHECKING:
    from collections.abc import Iterable

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
