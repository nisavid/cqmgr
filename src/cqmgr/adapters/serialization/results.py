"""JSON-compatible serialization for surface-neutral operation results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from cqmgr.application.configuration import QuotaContactKeyringReference
from cqmgr.application.operations.quotas import QuotaInspectData
from cqmgr.domain.diagnostics import DiagnosticCode, DiagnosticPhase, DiagnosticSource
from cqmgr.domain.identity import PrincipalIdentity
from cqmgr.domain.quotas import (
    MonitoringValue,
    MonitoringValueKind,
    QuotaPreferenceEvidence,
    QuotaQuantity,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.scopes import ResourceScope

if TYPE_CHECKING:
    from cqmgr.domain.results import OperationResult


def _value(value: object) -> object:  # noqa: C901, PLR0911, PLR0912
    if isinstance(value, ResourceScope):
        return {"type": value.kind.value, "name": value.canonical_name}
    if isinstance(value, QuotaQuantity):
        return {"value": value.base10, "unit": value.unit.symbol}
    if isinstance(value, MonitoringValue):
        provider_value = (
            str(value.value) if value.kind is MonitoringValueKind.INT64 else value.value
        )
        return {"kind": value.kind.value, "value": provider_value}
    if isinstance(value, (QuotaInspectData, QuotaPreferenceEvidence)):
        return _quota_evidence_value(value)
    if isinstance(value, QuotaContactKeyringReference):
        return {
            "backend": value.backend,
            "service": value.service,
            "account": value.account,
        }
    if isinstance(value, (PrincipalIdentity, RedactedText)):
        return value.value
    if isinstance(value, (DiagnosticCode, DiagnosticPhase, DiagnosticSource)):
        return value.value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value


def _quota_evidence_value(
    value: QuotaInspectData | QuotaPreferenceEvidence,
) -> dict[str, object]:
    if isinstance(value, QuotaPreferenceEvidence):
        return _preference_value(value, None)
    unit = (
        _inspect_native_unit(value)
        if value.preference is None or value.preference.identity == value.identity
        else None
    )
    return {
        field.name: (
            _preference_value(value.preference, unit)
            if field.name == "preference" and value.preference is not None
            else _value(getattr(value, field.name))
        )
        for field in fields(value)
    }


def _inspect_native_unit(value: QuotaInspectData) -> str | None:
    """Return one authoritative exact-slice unit or explicit unavailability."""
    units = {
        quantity.unit.symbol
        for identity, quantity in (
            (
                None if value.evidence is None else value.evidence.identity,
                None if value.evidence is None else value.evidence.effective_value,
            ),
            (
                None if value.item is None else value.item.identity,
                None if value.item is None else value.item.effective_value,
            ),
        )
        if identity == value.identity and quantity is not None
    }
    return next(iter(units)) if len(units) == 1 else None


def _preference_value(
    value: QuotaPreferenceEvidence,
    unit: str | None,
) -> dict[str, object]:
    mapping = {
        field.name: _value(getattr(value, field.name)) for field in fields(value)
    }
    mapping["preferred_value"] = {
        "value": str(value.preferred_value),
        "unit": unit,
    }
    mapping["granted_value"] = (
        None
        if value.granted_value is None
        else {"value": str(value.granted_value), "unit": unit}
    )
    return mapping


def operation_result_mapping(result: OperationResult[Any]) -> dict[str, object]:
    """Serialize one result with the exact stable top-level envelope."""
    mapping = {
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
    if result.identity_evidence is not None:
        mapping["identity_evidence"] = _value(result.identity_evidence)
    return mapping
