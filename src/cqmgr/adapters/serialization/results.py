"""JSON-compatible serialization for surface-neutral operation results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from cqmgr.application.configuration import QuotaContactKeyringReference
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.scopes import ResourceScope

if TYPE_CHECKING:
    from cqmgr.domain.results import OperationResult


def _value(value: object) -> object:  # noqa: PLR0911
    if isinstance(value, ResourceScope):
        return {"type": value.kind.value, "name": value.canonical_name}
    if isinstance(value, QuotaContactKeyringReference):
        return {
            "backend": value.backend,
            "service": value.service,
            "account": value.account,
        }
    if isinstance(value, RedactedText):
        return value.value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value


def operation_result_mapping(result: OperationResult[Any]) -> dict[str, object]:
    """Serialize one result with the exact stable top-level envelope."""
    return {
        "schema": result.schema,
        "operation": result.operation.value,
        "resource_scope": _value(result.resource_scope),
        "boundary": {
            "condition": result.boundary.condition.value,
            "reached": result.boundary.reached,
        },
        "outcome": {
            "code": result.outcome.code.value,
            "exit_class": int(result.outcome.exit_class),
        },
        "complete": result.completeness.is_complete,
        "started_at": _value(result.started_at),
        "finished_at": _value(result.finished_at),
        "data": _value(result.data),
        "diagnostics": _value(result.diagnostics),
        "provenance": _value(result.provenance),
    }
