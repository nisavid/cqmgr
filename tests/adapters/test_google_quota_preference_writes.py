"""Hermetic official Cloud Quotas write and reconciliation adapter tests."""

import asyncio
from dataclasses import replace

import pytest
from google.api_core import exceptions as google_exceptions
from google.cloud import cloudquotas_v1

from cqmgr.adapters.google.quota_preference_writes import (
    OfficialQuotaPreferenceUnknownResolver,
    OfficialQuotaPreferenceWriter,
)
from cqmgr.application.ports.provider_writes import (
    QuotaPreferenceWrite,
    QuotaPreferenceWriteAction,
    UnknownWriteResolution,
)
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.results import StableSymbol
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
IDENTITY = "projects/123456789/locations/global/quotaPreferences/cqmgr-deterministic"
DEFAULT_TIMEOUT_SECONDS = 30.0


def _write(action: QuotaPreferenceWriteAction) -> QuotaPreferenceWrite:
    return QuotaPreferenceWrite(
        child_id="direct",
        slice_identity=EffectiveQuotaSliceIdentity(
            SCOPE,
            "compute.googleapis.com",
            "GPU-DIRECT",
            NormalizedDimensions((("region", "us-central1"),)),
            QuotaScope.REGIONAL,
        ),
        target=QuotaQuantity(8, QuotaUnit("1")),
        preference_identity=IDENTITY,
        action=action,
        current_etag=(
            "current-etag" if action is QuotaPreferenceWriteAction.AMEND else None
        ),
        contact_value="resolved@example.com",
        acknowledgements=(
            StableSymbol("decrease-below-usage"),
            StableSymbol("decrease-over-ten-percent"),
        ),
    )


def _response(
    request: QuotaPreferenceWrite,
    *,
    contact_value: str = "resolved@example.com",
) -> cloudquotas_v1.QuotaPreference:
    return cloudquotas_v1.QuotaPreference(
        name=request.preference_identity,
        service=request.slice_identity.service,
        quota_id=request.slice_identity.quota_id,
        dimensions=dict(request.slice_identity.dimensions.items),
        quota_config=cloudquotas_v1.QuotaConfig(preferred_value=request.target.value),
        contact_email=contact_value,
    )


class _ScriptedMutationClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object, float]] = []
        self.error: BaseException | None = None
        self.contact_value = "resolved@example.com"

    async def create_quota_preference(
        self,
        request: cloudquotas_v1.CreateQuotaPreferenceRequest,
        *,
        retry: object,
        timeout: float,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaPreference:
        self.calls.append(("create", request, retry, timeout))
        if self.error is not None:
            raise self.error
        return _response(
            _write(QuotaPreferenceWriteAction.CREATE),
            contact_value=self.contact_value,
        )

    async def update_quota_preference(
        self,
        request: cloudquotas_v1.UpdateQuotaPreferenceRequest,
        *,
        retry: object,
        timeout: float,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaPreference:
        self.calls.append(("update", request, retry, timeout))
        if self.error is not None:
            raise self.error
        return _response(
            _write(QuotaPreferenceWriteAction.AMEND),
            contact_value=self.contact_value,
        )


class _ScriptedReadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object, float]] = []
        self.error: BaseException | None = None
        self.contact_value = "resolved@example.com"

    async def get_quota_preference(
        self,
        request: cloudquotas_v1.GetQuotaPreferenceRequest,
        *,
        retry: object,
        timeout: float,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaPreference:
        self.calls.append(("get", request, retry, timeout))
        if self.error is not None:
            raise self.error
        return _response(
            _write(QuotaPreferenceWriteAction.CREATE),
            contact_value=self.contact_value,
        )


def test_official_create_binds_identity_and_disables_retry() -> None:
    """Create uses one deterministic ID and no generic retry or validation call."""
    client = _ScriptedMutationClient()
    request = _write(QuotaPreferenceWriteAction.CREATE)

    result = asyncio.run(OfficialQuotaPreferenceWriter(client).dispatch(request))  # type: ignore[arg-type]

    method, provider_request, retry, timeout = client.calls[0]
    assert method == "create"
    assert retry is None
    assert timeout == DEFAULT_TIMEOUT_SECONDS
    assert isinstance(provider_request, cloudquotas_v1.CreateQuotaPreferenceRequest)
    assert provider_request.parent == "projects/123456789/locations/global"
    assert provider_request.quota_preference_id == "cqmgr-deterministic"
    assert provider_request.quota_preference.contact_email == "resolved@example.com"
    assert tuple(provider_request.ignore_safety_checks) == (
        cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_BELOW_USAGE,
        cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_PERCENTAGE_TOO_HIGH,
    )
    assert result.accepted


def test_official_amend_uses_current_etag_and_validate_only_false() -> None:
    """Amend sends the current etag once with validation-only disabled."""
    client = _ScriptedMutationClient()
    request = _write(QuotaPreferenceWriteAction.AMEND)

    result = asyncio.run(OfficialQuotaPreferenceWriter(client).dispatch(request))  # type: ignore[arg-type]

    method, provider_request, retry, _timeout = client.calls[0]
    assert method == "update"
    assert retry is None
    assert isinstance(provider_request, cloudquotas_v1.UpdateQuotaPreferenceRequest)
    assert provider_request.quota_preference.etag == "current-etag"
    assert not provider_request.allow_missing
    assert not provider_request.validate_only
    assert result.accepted


def test_official_unknown_resolution_is_read_only_and_retry_free() -> None:
    """Unknown reconciliation performs exactly one bound get."""
    client = _ScriptedReadClient()
    request = _write(QuotaPreferenceWriteAction.CREATE)

    resolution = asyncio.run(
        OfficialQuotaPreferenceUnknownResolver(client).resolve_unknown(request)  # type: ignore[arg-type]
    )

    method, provider_request, retry, _timeout = client.calls[0]
    assert method == "get"
    assert retry is None
    assert isinstance(provider_request, cloudquotas_v1.GetQuotaPreferenceRequest)
    assert provider_request.name == IDENTITY
    assert resolution is UnknownWriteResolution.ACCEPTED


def test_official_conclusive_errors_are_failed_without_retry() -> None:
    """Provider conflicts and not-found reconciliation remain conclusive."""
    client = _ScriptedMutationClient()
    client.error = google_exceptions.Aborted("conflict")
    request = _write(QuotaPreferenceWriteAction.AMEND)

    result = asyncio.run(OfficialQuotaPreferenceWriter(client).dispatch(request))  # type: ignore[arg-type]
    assert not result.accepted
    assert result.outcome == StableSymbol("conflicting")

    resolver_client = _ScriptedReadClient()
    resolver_client.error = google_exceptions.NotFound("missing")
    resolution = asyncio.run(
        OfficialQuotaPreferenceUnknownResolver(resolver_client).resolve_unknown(  # type: ignore[arg-type]
            request
        )
    )
    assert resolution is UnknownWriteResolution.FAILED

    client.error = google_exceptions.BadRequest("invalid")
    rejected = asyncio.run(
        OfficialQuotaPreferenceWriter(client).dispatch(request)  # type: ignore[arg-type]
    )
    assert not rejected.accepted
    assert rejected.outcome == StableSymbol("provider-rejected")


def test_writer_validates_timeout_and_omits_unneeded_safety_overrides() -> None:
    """The production adapter requires a deadline and sends only bound overrides."""
    client = _ScriptedMutationClient()
    with pytest.raises(ValueError, match="timeout"):
        OfficialQuotaPreferenceWriter(client, timeout_seconds=0)  # type: ignore[arg-type]
    request = replace(
        _write(QuotaPreferenceWriteAction.CREATE),
        acknowledgements=(),
    )

    asyncio.run(OfficialQuotaPreferenceWriter(client).dispatch(request))  # type: ignore[arg-type]

    provider_request = client.calls[0][1]
    assert isinstance(provider_request, cloudquotas_v1.CreateQuotaPreferenceRequest)
    assert tuple(provider_request.ignore_safety_checks) == ()


def test_unknown_resolver_requires_a_positive_read_deadline() -> None:
    """The read-after-unknown boundary owns its deadline independently."""
    with pytest.raises(ValueError, match="read timeout"):
        OfficialQuotaPreferenceUnknownResolver(
            _ScriptedReadClient(),  # type: ignore[arg-type]
            timeout_seconds=0,
        )


def test_response_contact_must_match_the_bound_dispatch_contact() -> None:
    """A stale contact cannot prove dispatch acceptance or unknown resolution."""
    mutation_client = _ScriptedMutationClient()
    mutation_client.contact_value = "stale@example.com"
    read_client = _ScriptedReadClient()
    read_client.contact_value = "stale@example.com"
    request = _write(QuotaPreferenceWriteAction.AMEND)

    dispatch = asyncio.run(
        OfficialQuotaPreferenceWriter(mutation_client).dispatch(request)  # type: ignore[arg-type]
    )
    resolution = asyncio.run(
        OfficialQuotaPreferenceUnknownResolver(read_client).resolve_unknown(request)  # type: ignore[arg-type]
    )

    assert not dispatch.accepted
    assert dispatch.outcome == StableSymbol("conflicting")
    assert resolution is UnknownWriteResolution.FAILED


def test_writer_and_resolver_expose_disjoint_adapter_boundaries() -> None:
    """Mutation and read-after-unknown construction remain independent."""
    mutation_client = _ScriptedMutationClient()
    read_client = _ScriptedReadClient()
    writer = OfficialQuotaPreferenceWriter(mutation_client)  # type: ignore[arg-type]
    resolver = OfficialQuotaPreferenceUnknownResolver(read_client)  # type: ignore[arg-type]

    assert not hasattr(writer, "resolve_unknown")
    assert not hasattr(resolver, "dispatch")
    assert not hasattr(mutation_client, "get_quota_preference")
    assert not hasattr(read_client, "create_quota_preference")
    assert not hasattr(read_client, "update_quota_preference")
