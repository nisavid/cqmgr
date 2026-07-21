"""Supported public record schema identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

OPERATION_RESULT_SCHEMA = "cqmgr.operation-result/v1"
WATCH_EVENT_SCHEMA = "cqmgr.watch-event/v1"


class UnsupportedSchemaError(ValueError):
    """Raised when a record does not use an exactly supported schema."""


@dataclass(frozen=True, slots=True, init=False)
class ProviderSymbol[KnownT: StrEnum]:
    """Exact provider enum text with an optional known product projection."""

    raw: str
    enum_type: type[KnownT]
    known: KnownT | None

    def __init__(self, raw: str, enum_type: type[KnownT]) -> None:
        """Preserve the raw text and classify only an exact known value."""
        if not isinstance(raw, str):
            msg = "provider symbol raw must be a string"
            raise TypeError(msg)
        if not raw:
            msg = "provider symbol must be non-empty"
            raise ValueError(msg)
        if not isinstance(enum_type, type) or not issubclass(enum_type, StrEnum):
            msg = "provider symbol enum_type must be a StrEnum type"
            raise TypeError(msg)
        try:
            known = enum_type(raw)
        except ValueError:
            known = None
        object.__setattr__(self, "raw", raw)
        object.__setattr__(self, "enum_type", enum_type)
        object.__setattr__(self, "known", known)


def _require_schema(value: str, expected: str) -> str:
    if value != expected:
        msg = f"unsupported schema: {value!r}; expected {expected!r}"
        raise UnsupportedSchemaError(msg)
    return value


def require_operation_result_schema(value: str) -> str:
    """Accept only the exact V1 operation-result discriminator."""
    return _require_schema(value, OPERATION_RESULT_SCHEMA)


def require_watch_event_schema(value: str) -> str:
    """Accept only the exact V1 Watch-event discriminator."""
    return _require_schema(value, WATCH_EVENT_SCHEMA)
