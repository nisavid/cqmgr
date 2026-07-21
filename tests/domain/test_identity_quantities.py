"""Signed quota quantity and native-unit contracts."""

from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cqmgr.domain.quotas import QuotaQuantity, QuotaUnit

SIGNED_64_MIN = -(2**63)
SIGNED_64_MAX = (2**63) - 1


@given(st.integers(min_value=SIGNED_64_MIN, max_value=SIGNED_64_MAX))
def test_signed_64_bit_quantity_has_canonical_base10_text(value: int) -> None:
    """Every supported integer round-trips through canonical decimal text."""
    quantity = QuotaQuantity(value=value, unit=QuotaUnit("provider-unknown-unit"))

    assert int(quantity.base10, 10) == value
    if value == 0:
        assert quantity.base10 == "0"
    elif value > 0:
        assert quantity.base10[0] in "123456789"
    else:
        assert quantity.base10[0] == "-"
        assert quantity.base10[1] in "123456789"


@pytest.mark.parametrize("value", [SIGNED_64_MIN - 1, SIGNED_64_MAX + 1])
def test_quantity_rejects_values_outside_signed_64_bit_range(value: int) -> None:
    """Adjacent values beyond either signed 64-bit boundary fail closed."""
    with pytest.raises(ValueError, match="signed 64-bit"):
        QuotaQuantity(value=value, unit=QuotaUnit("1"))


@pytest.mark.parametrize("value", [True, False])
def test_quantity_rejects_boolean_values(value: object) -> None:
    """Boolean values never masquerade as integer quota amounts."""
    with pytest.raises(TypeError, match="integer, not bool"):
        QuotaQuantity(value=cast("int", value), unit=QuotaUnit("1"))


def test_quota_unit_preserves_unknown_provider_symbol() -> None:
    """Unknown native units stay visible instead of being coerced."""
    unit = QuotaUnit("provider.Custom/{unit}")

    assert unit.symbol == "provider.Custom/{unit}"
    assert QuotaQuantity(value=7, unit=unit).unit is unit


def test_quota_unit_must_be_explicit() -> None:
    """A quantity cannot silently omit its provider-native unit."""
    with pytest.raises(ValueError, match="must not be empty"):
        QuotaUnit("")


def test_quantity_components_require_exact_types() -> None:
    """Floats, untyped units, and non-string symbols fail before use."""
    with pytest.raises(TypeError, match="integer, not bool"):
        QuotaQuantity(value=cast("int", 1.0), unit=QuotaUnit("1"))
    with pytest.raises(TypeError, match="must be a QuotaUnit"):
        QuotaQuantity(value=1, unit=cast("QuotaUnit", "1"))
    with pytest.raises(TypeError, match="symbol must be a string"):
        QuotaUnit(cast("str", 1))
