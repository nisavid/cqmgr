"""Canonical JSON serialization and local authentication for quota request plans."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cqmgr.application.ports.plans import EncodedPlan
from cqmgr.domain.plans import (
    PLAN_SCHEMA,
    ContactBinding,
    EvidenceBinding,
    PlanPrincipal,
    QuotaRequestPlan,
)
from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

_MINIMUM_AUTHENTICATION_KEY_BYTES = 32


class PlanDecodeError(ValueError):
    """Raised when untrusted plan bytes cannot be safely reviewed."""


@dataclass(frozen=True, slots=True)
class DecodedPlan:
    """Digest-valid canonical plan with an untrusted issuer authenticator."""

    plan: QuotaRequestPlan
    digest: str
    authentication: str
    bytes: bytes

    def authenticate(self, key: bytes) -> bool:
        """Verify that the issuing installation held the supplied key."""
        expected = _authentication(_canonical(_plan_mapping(self.plan)), key)
        return hmac.compare_digest(self.authentication, expected)


class PlanCodec:
    """Encode and decode the exact V1 portable plan representation."""

    @staticmethod
    def encode(plan: QuotaRequestPlan, key: bytes) -> EncodedPlan:
        """Return deterministic authenticated bytes for one valid plan."""
        if not isinstance(plan, QuotaRequestPlan):
            msg = "plan must be a QuotaRequestPlan"
            raise TypeError(msg)
        _require_key(key)
        plan_mapping = _plan_mapping(plan)
        content = _canonical(plan_mapping)
        digest = _digest(content)
        envelope = {
            "authentication": _authentication(content, key),
            "digest": digest,
            "plan": plan_mapping,
        }
        return EncodedPlan(bytes=_canonical(envelope) + b"\n", digest=digest)

    @staticmethod
    def decode(data: bytes) -> DecodedPlan:
        """Validate exact encoding and digest before exposing plan contents."""
        if not isinstance(data, bytes):
            msg = "plan data must be bytes"
            raise TypeError(msg)
        try:
            envelope = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
        except (UnicodeDecodeError, json.JSONDecodeError, PlanDecodeError) as error:
            msg = "plan is not valid canonical JSON"
            raise PlanDecodeError(msg) from error
        if not isinstance(envelope, dict) or set(envelope) != {
            "authentication",
            "digest",
            "plan",
        }:
            msg = "plan envelope has unsupported fields"
            raise PlanDecodeError(msg)
        plan_value = envelope["plan"]
        if not isinstance(plan_value, dict) or plan_value.get("schema") != PLAN_SCHEMA:
            msg = "plan uses an unsupported schema"
            raise PlanDecodeError(msg)
        if data != _canonical(envelope) + b"\n":
            msg = "plan bytes are not canonical"
            raise PlanDecodeError(msg)
        digest = envelope["digest"]
        authentication = envelope["authentication"]
        if not isinstance(digest, str) or not isinstance(authentication, str):
            msg = "plan digest and authentication must be strings"
            raise PlanDecodeError(msg)
        expected_digest = _digest(_canonical(plan_value))
        if not hmac.compare_digest(digest, expected_digest):
            msg = "plan content digest does not match canonical bytes"
            raise PlanDecodeError(msg)
        if not authentication.startswith("hmac-sha256:"):
            msg = "plan authentication algorithm is unsupported"
            raise PlanDecodeError(msg)
        try:
            plan = _parse_plan(plan_value)
        except (KeyError, TypeError, ValueError) as error:
            msg = "plan content is invalid"
            raise PlanDecodeError(msg) from error
        return DecodedPlan(plan, digest, authentication, data)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            msg = f"duplicate plan field: {key}"
            raise PlanDecodeError(msg)
        result[key] = value
    return result


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        msg = "plan contains a value that cannot be canonically encoded"
        raise PlanDecodeError(msg) from error


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _authentication(content: bytes, key: bytes) -> str:
    _require_key(key)
    return f"hmac-sha256:{hmac.digest(key, content, 'sha256').hex()}"


def _require_key(key: object) -> None:
    if not isinstance(key, bytes) or len(key) < _MINIMUM_AUTHENTICATION_KEY_BYTES:
        msg = "plan authentication key must contain at least 32 bytes"
        raise ValueError(msg)


def _time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _quantity(value: QuotaQuantity) -> dict[str, str]:
    return {"unit": value.unit.symbol, "value": value.base10}


def _scope(value: ResourceScope) -> dict[str, str]:
    return {"name": value.canonical_name, "type": value.kind.value}


def _slice(value: EffectiveQuotaSliceIdentity) -> dict[str, object]:
    return {
        "dimensions": dict(value.dimensions.items),
        "quota_id": value.quota_id,
        "quota_scope": value.quota_scope.value,
        "resource_scope": _scope(value.resource_scope),
        "service": value.service,
    }


def _plan_mapping(plan: QuotaRequestPlan) -> dict[str, object]:
    return {
        "acknowledgements": [item.value for item in plan.acknowledgements],
        "constraints": [_slice(item.slice_identity) for item in plan.constraints],
        "contact_binding": {
            "source": plan.contact_binding.source.value,
            "source_identity": plan.contact_binding.source_identity,
            "value_digest": plan.contact_binding.value_digest,
        },
        "effective": _quantity(plan.effective),
        "effective_observed_at": _time(plan.effective_observed_at),
        "evidence": [
            {
                "name": item.name.value,
                "observed_at": _time(item.observed_at),
                "value_digest": item.value_digest,
            }
            for item in plan.evidence
        ],
        "expires_at": _time(plan.expires_at),
        "installation_id": plan.installation_id,
        "issued_at": _time(plan.issued_at),
        "preference": {
            "etag": plan.preference_etag,
            "name": plan.preference_name,
        },
        "principal": {
            "impersonation_chain": list(plan.principal.impersonation_chain),
            "stable_identity": plan.principal.stable_identity,
        },
        "required_acknowledgements": [
            item.value for item in plan.required_acknowledgements
        ],
        "resource_scope": _scope(plan.resource_scope),
        "schema": plan.schema,
        "slice": _slice(plan.slice_identity),
        "target": _quantity(plan.target),
        "warnings": [item.value for item in plan.warnings],
    }


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        msg = "plan timestamp must be canonical UTC RFC 3339"
        raise ValueError(msg)
    return datetime.fromisoformat(f"{value[:-1]}+00:00")


def _parse_scope(value: object) -> ResourceScope:
    mapping = _exact_mapping(value, {"name", "type"})
    return ResourceScope(
        ResourceScopeKind(_string(mapping["type"])),
        _string(mapping["name"]),
    )


def _parse_quantity(value: object) -> QuotaQuantity:
    mapping = _exact_mapping(value, {"unit", "value"})
    raw_value = _string(mapping["value"])
    if raw_value != str(int(raw_value)):
        msg = "quantity value must use canonical base-10 encoding"
        raise ValueError(msg)
    return QuotaQuantity(int(raw_value), QuotaUnit(_string(mapping["unit"])))


def _parse_slice(value: object) -> EffectiveQuotaSliceIdentity:
    mapping = _exact_mapping(
        value,
        {"dimensions", "quota_id", "quota_scope", "resource_scope", "service"},
    )
    dimensions = mapping["dimensions"]
    if not isinstance(dimensions, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in dimensions.items()
    ):
        msg = "slice dimensions must be a string map"
        raise TypeError(msg)
    return EffectiveQuotaSliceIdentity(
        resource_scope=_parse_scope(mapping["resource_scope"]),
        service=_string(mapping["service"]),
        quota_id=_string(mapping["quota_id"]),
        dimensions=NormalizedDimensions(dimensions.items()),
        quota_scope=QuotaScope(_string(mapping["quota_scope"])),
    )


def _parse_plan(value: dict[str, Any]) -> QuotaRequestPlan:
    expected = {
        "acknowledgements",
        "constraints",
        "contact_binding",
        "effective",
        "effective_observed_at",
        "evidence",
        "expires_at",
        "installation_id",
        "issued_at",
        "preference",
        "principal",
        "required_acknowledgements",
        "resource_scope",
        "schema",
        "slice",
        "target",
        "warnings",
    }
    mapping = _exact_mapping(value, expected)
    preference = _exact_mapping(mapping["preference"], {"etag", "name"})
    principal = _exact_mapping(
        mapping["principal"], {"impersonation_chain", "stable_identity"}
    )
    contact = _exact_mapping(
        mapping["contact_binding"],
        {"source", "source_identity", "value_digest"},
    )
    return QuotaRequestPlan(
        resource_scope=_parse_scope(mapping["resource_scope"]),
        slice_identity=_parse_slice(mapping["slice"]),
        target=_parse_quantity(mapping["target"]),
        effective=_parse_quantity(mapping["effective"]),
        effective_observed_at=_parse_time(mapping["effective_observed_at"]),
        preference_name=_optional_string(preference["name"]),
        preference_etag=_optional_string(preference["etag"]),
        principal=PlanPrincipal(
            stable_identity=_string(principal["stable_identity"]),
            impersonation_chain=tuple(_string_list(principal["impersonation_chain"])),
        ),
        contact_binding=ContactBinding(
            source=StableSymbol(_string(contact["source"])),
            source_identity=_string(contact["source_identity"]),
            value_digest=_string(contact["value_digest"]),
        ),
        warnings=tuple(
            StableSymbol(item) for item in _string_list(mapping["warnings"])
        ),
        required_acknowledgements=tuple(
            StableSymbol(item)
            for item in _string_list(mapping["required_acknowledgements"])
        ),
        acknowledgements=tuple(
            StableSymbol(item) for item in _string_list(mapping["acknowledgements"])
        ),
        constraints=tuple(
            ConstraintReference(_parse_slice(item))
            for item in _list(mapping["constraints"])
        ),
        evidence=tuple(
            EvidenceBinding(
                name=StableSymbol(_string(item_mapping["name"])),
                value_digest=_string(item_mapping["value_digest"]),
                observed_at=_parse_time(item_mapping["observed_at"]),
            )
            for item_mapping in (
                _exact_mapping(item, {"name", "observed_at", "value_digest"})
                for item in _list(mapping["evidence"])
            )
        ),
        installation_id=_string(mapping["installation_id"]),
        issued_at=_parse_time(mapping["issued_at"]),
        expires_at=_parse_time(mapping["expires_at"]),
    )


def _exact_mapping(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        msg = "plan object has missing or unsupported fields"
        raise ValueError(msg)
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        msg = "plan field must be a list"
        raise TypeError(msg)
    return value


def _string_list(value: object) -> list[str]:
    items = _list(value)
    if any(not isinstance(item, str) for item in items):
        msg = "plan list must contain strings"
        raise TypeError(msg)
    return items


def _string(value: object) -> str:
    if not isinstance(value, str):
        msg = "plan field must be a string"
        raise TypeError(msg)
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)
