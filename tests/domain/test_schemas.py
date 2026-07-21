"""Stable schema and symbolic-value compatibility contracts."""

from enum import Enum
from typing import cast

import pytest

from cqmgr.domain.results import ExitClass, OperationName, StableSymbol
from cqmgr.domain.schemas import (
    OPERATION_RESULT_SCHEMA,
    WATCH_EVENT_SCHEMA,
    ProviderSymbol,
    UnsupportedSchemaError,
    require_operation_result_schema,
    require_watch_event_schema,
)
from cqmgr.domain.status import Reconciliation


class LegacyReconciliation(Enum):
    """A string-valued provider enum without the required StrEnum contract."""

    SETTLED = "settled"


def test_exact_v1_schemas_are_supported() -> None:
    """Each public record accepts only its exact supported discriminator."""
    assert require_operation_result_schema(OPERATION_RESULT_SCHEMA) == (
        OPERATION_RESULT_SCHEMA
    )
    assert require_watch_event_schema(WATCH_EVENT_SCHEMA) == WATCH_EVENT_SCHEMA


@pytest.mark.parametrize(
    "schema",
    [
        "cqmgr.operation-result/v0",
        "cqmgr.operation-result/v2",
        "cqmgr.watch-event/v2",
        "provider.operation-result/v1",
        "",
    ],
)
def test_unsupported_or_newer_schemas_fail_closed(schema: str) -> None:
    """Unknown schema families and versions never inherit V1 semantics."""
    with pytest.raises(UnsupportedSchemaError, match="unsupported schema"):
        require_operation_result_schema(schema)


def test_stable_symbols_and_exit_classes_are_explicit() -> None:
    """Open codes stay symbolic while global numeric classes stay closed."""
    assert OperationName("request.watch").value == "request.watch"
    assert ExitClass(130) is ExitClass.INTERRUPTED
    with pytest.raises(ValueError, match="stable symbol"):
        StableSymbol("Not Stable")
    with pytest.raises(ValueError, match="not a valid ExitClass"):
        ExitClass(1)


def test_provider_symbols_preserve_unknown_values_exactly() -> None:
    """Provider enum expansion never coerces an unknown value into a known one."""
    known = ProviderSymbol("settled", Reconciliation)
    unknown = ProviderSymbol("FUTURE_provider_STATE", Reconciliation)

    assert known.known is Reconciliation.SETTLED
    assert known.enum_type is Reconciliation
    assert known.raw == "settled"
    assert unknown.known is None
    assert unknown.enum_type is Reconciliation
    assert unknown.raw == "FUTURE_provider_STATE"
    with pytest.raises(ValueError, match="non-empty"):
        ProviderSymbol("", Reconciliation)


@pytest.mark.parametrize("raw", [cast("str", 1), cast("str", b"settled")])
def test_provider_symbols_require_raw_text(raw: str) -> None:
    """Provider symbol text cannot be silently accepted from another type."""
    with pytest.raises(TypeError, match="raw must be a string"):
        ProviderSymbol(raw, Reconciliation)


@pytest.mark.parametrize(
    "enum_type",
    [
        cast("type[Reconciliation]", str),
        cast("type[Reconciliation]", LegacyReconciliation),
        cast("type[Reconciliation]", Reconciliation.SETTLED),
    ],
)
def test_provider_symbols_require_a_strenum_type(
    enum_type: type[Reconciliation],
) -> None:
    """Classification requires an enum class whose members are strings."""
    with pytest.raises(TypeError, match="enum_type must be a StrEnum type"):
        ProviderSymbol("settled", enum_type)
