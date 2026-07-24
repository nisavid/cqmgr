"""Official Cloud Quotas mutation adapter with deterministic reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import cloudquotas_v1
from google.protobuf import field_mask_pb2

from cqmgr.application.ports.provider_writes import (
    QuotaPreferenceWrite,
    QuotaPreferenceWriteAction,
    QuotaPreferenceWriteResult,
    UnknownWriteResolution,
)
from cqmgr.domain.results import StableSymbol

if TYPE_CHECKING:
    from google.api_core.gapic_v1.method import _MethodDefault
    from google.api_core.retry import AsyncRetry


class CloudQuotasMutationClient(Protocol):
    """Narrow generated-client seam with retry control visible to tests."""

    async def create_quota_preference(
        self,
        request: cloudquotas_v1.CreateQuotaPreferenceRequest,
        *,
        retry: AsyncRetry | _MethodDefault | None,
        timeout: float,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaPreference:
        """Create exactly one deterministic preference."""
        ...

    async def update_quota_preference(
        self,
        request: cloudquotas_v1.UpdateQuotaPreferenceRequest,
        *,
        retry: AsyncRetry | _MethodDefault | None,
        timeout: float,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaPreference:
        """Amend exactly one current preference."""
        ...


class CloudQuotasQuotaPreferenceReadClient(Protocol):
    """Narrow generated-client seam for read-after-unknown reconciliation."""

    async def get_quota_preference(
        self,
        request: cloudquotas_v1.GetQuotaPreferenceRequest,
        *,
        retry: AsyncRetry | _MethodDefault | None,
        timeout: float,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaPreference:
        """Read one deterministic reconciliation identity."""
        ...


class OfficialQuotaPreferenceWriter:
    """Create or amend exact quota preferences without generic retry."""

    def __init__(
        self,
        client: CloudQuotasMutationClient,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Bind one official client with a positive per-call timeout."""
        if timeout_seconds <= 0:
            msg = "quota preference write timeout must be positive"
            raise ValueError(msg)
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def dispatch(
        self,
        request: QuotaPreferenceWrite,
    ) -> QuotaPreferenceWriteResult:
        """Perform exactly one create or amend with generic retry disabled."""
        preference = _preference(request)
        try:
            if request.action is QuotaPreferenceWriteAction.CREATE:
                parent, preference_id = request.preference_identity.rsplit(
                    "/quotaPreferences/",
                    maxsplit=1,
                )
                response = await self._client.create_quota_preference(
                    cloudquotas_v1.CreateQuotaPreferenceRequest(
                        parent=parent,
                        quota_preference_id=preference_id,
                        quota_preference=preference,
                        ignore_safety_checks=_safety_checks(request),
                    ),
                    retry=None,
                    timeout=self._timeout_seconds,
                )
            else:
                response = await self._client.update_quota_preference(
                    cloudquotas_v1.UpdateQuotaPreferenceRequest(
                        quota_preference=preference,
                        update_mask=field_mask_pb2.FieldMask(
                            paths=(
                                "quota_config.preferred_value",
                                "contact_email",
                            )
                        ),
                        allow_missing=False,
                        validate_only=False,
                        ignore_safety_checks=_safety_checks(request),
                    ),
                    retry=None,
                    timeout=self._timeout_seconds,
                )
        except (
            google_exceptions.Aborted,
            google_exceptions.AlreadyExists,
        ):
            return QuotaPreferenceWriteResult(
                accepted=False,
                outcome=StableSymbol("conflicting"),
            )
        except (
            google_exceptions.BadRequest,
            google_exceptions.Forbidden,
            google_exceptions.PermissionDenied,
            google_exceptions.FailedPrecondition,
        ):
            return QuotaPreferenceWriteResult(
                accepted=False,
                outcome=StableSymbol("provider-rejected"),
            )
        if not _matches(response, request):
            return QuotaPreferenceWriteResult(
                accepted=False,
                outcome=StableSymbol("conflicting"),
            )
        return QuotaPreferenceWriteResult(
            accepted=True,
            outcome=StableSymbol("submitted"),
        )


class OfficialQuotaPreferenceUnknownResolver:
    """Read one exact quota preference after dispatch uncertainty."""

    def __init__(
        self,
        client: CloudQuotasQuotaPreferenceReadClient,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Bind one official read client with a positive per-call timeout."""
        if timeout_seconds <= 0:
            msg = "quota preference read timeout must be positive"
            raise ValueError(msg)
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def resolve_unknown(
        self,
        request: QuotaPreferenceWrite,
    ) -> UnknownWriteResolution:
        """Read the exact identity once; never turn uncertainty into a write."""
        try:
            response = await self._client.get_quota_preference(
                cloudquotas_v1.GetQuotaPreferenceRequest(
                    name=request.preference_identity
                ),
                retry=None,
                timeout=self._timeout_seconds,
            )
        except google_exceptions.NotFound:
            return UnknownWriteResolution.FAILED
        return (
            UnknownWriteResolution.ACCEPTED
            if _matches(response, request)
            else UnknownWriteResolution.FAILED
        )


def _preference(
    request: QuotaPreferenceWrite,
) -> cloudquotas_v1.QuotaPreference:
    return cloudquotas_v1.QuotaPreference(
        name=request.preference_identity,
        service=request.slice_identity.service,
        quota_id=request.slice_identity.quota_id,
        dimensions=dict(request.slice_identity.dimensions.items),
        quota_config=cloudquotas_v1.QuotaConfig(preferred_value=request.target.value),
        etag=request.current_etag or "",
        contact_email=request.contact_value,
    )


def _safety_checks(
    request: QuotaPreferenceWrite,
) -> tuple[cloudquotas_v1.QuotaSafetyCheck, ...]:
    codes = {item.value for item in request.acknowledgements}
    checks: list[cloudquotas_v1.QuotaSafetyCheck] = []
    if "decrease-below-usage" in codes:
        checks.append(cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_BELOW_USAGE)
    if "decrease-over-ten-percent" in codes:
        checks.append(
            cloudquotas_v1.QuotaSafetyCheck.QUOTA_DECREASE_PERCENTAGE_TOO_HIGH
        )
    return tuple(checks)


def _matches(
    response: cloudquotas_v1.QuotaPreference,
    request: QuotaPreferenceWrite,
) -> bool:
    return (
        response.name == request.preference_identity
        and response.service == request.slice_identity.service
        and response.quota_id == request.slice_identity.quota_id
        and dict(response.dimensions) == dict(request.slice_identity.dimensions.items)
        and response.quota_config.preferred_value == request.target.value
        and response.contact_email == request.contact_value
    )
