"""Hermetic exact Cloud Quotas Watch observation adapter tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast, override

import pytest
from google.api_core import exceptions as google_exceptions
from google.cloud import cloudquotas_v1
from google.protobuf import duration_pb2
from google.rpc import error_details_pb2

from cqmgr.adapters.google.cloud_quotas import (
    GoogleWatchObservationReader,
    OfficialCloudQuotasObservationClient,
)
from cqmgr.application.ports.coordination import (
    CancellationToken,
    CoordinationCancelledError,
    CoordinationDeadlineExceededError,
)
from cqmgr.application.ports.watch import (
    WatchObservationRequest,
    WatchObservationTransientError,
)
from cqmgr.domain.apply_records import ApplyChildDisposition
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import (
    EffectiveConfirmation,
    GrantSatisfaction,
    Reconciliation,
    WatchCondition,
    WatchDisposition,
)
from cqmgr.domain.watch import WatchChildIdentity

NOW = datetime(2026, 7, 24, 9, 30, tzinfo=UTC)
STATUS_TIME = datetime(2026, 7, 24, 9, 29, tzinfo=UTC)
PROVIDER_RETRY_SECONDS = 7.5


class FakeObservationClient:
    """Return exact generated resources while recording bounded read calls."""

    def __init__(
        self,
        preference: cloudquotas_v1.QuotaPreference,
        quota_info: cloudquotas_v1.QuotaInfo,
    ) -> None:
        """Retain one response for each exact read."""
        self.preference = preference
        self.quota_info_value = quota_info
        self.calls: list[tuple[str, str, float]] = []

    async def quota_preference(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaPreference:
        """Return the bound preference."""
        self.calls.append(("preference", name, timeout_seconds))
        return self.preference

    async def quota_info(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaInfo:
        """Return the bound effective quota."""
        self.calls.append(("quota-info", name, timeout_seconds))
        return self.quota_info_value


class FakeOfficialClient:
    """Capture generated GET requests without network access."""

    def __init__(
        self,
        preference: cloudquotas_v1.QuotaPreference,
        quota_info: cloudquotas_v1.QuotaInfo,
    ) -> None:
        """Retain generated responses and an empty call ledger."""
        self.preference = preference
        self.quota_info_value = quota_info
        self.calls: list[tuple[str, object, object, object]] = []

    async def get_quota_preference(
        self,
        *,
        request: object,
        retry: object,
        timeout: object,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaPreference:
        """Return the exact preference."""
        self.calls.append(("preference", request, retry, timeout))
        return self.preference

    async def get_quota_info(
        self,
        *,
        request: object,
        retry: object,
        timeout: object,  # noqa: ASYNC109
    ) -> cloudquotas_v1.QuotaInfo:
        """Return the exact QuotaInfo."""
        self.calls.append(("quota-info", request, retry, timeout))
        return self.quota_info_value


class BlockingObservationClient:
    """Block the first exact read until its caller cancels."""

    def __init__(self) -> None:
        """Create an unread preference gate."""
        self.started = asyncio.Event()

    async def quota_preference(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaPreference:
        """Wait indefinitely after recording dispatch."""
        del name, timeout_seconds
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def quota_info(
        self,
        *,
        name: str,
        timeout_seconds: float,
    ) -> cloudquotas_v1.QuotaInfo:
        """Fail if cancellation allows the second read."""
        del name, timeout_seconds
        raise AssertionError


def _child() -> WatchChildIdentity:
    scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
    return WatchChildIdentity(
        child_id="direct",
        order=0,
        slice_identity=EffectiveQuotaSliceIdentity(
            scope,
            "compute.googleapis.com",
            "GPUS-PER-GPU-FAMILY-per-project-region",
            NormalizedDimensions(
                (("gpu_family", "NVIDIA_H100"), ("region", "us-east5"))
            ),
            QuotaScope.REGIONAL,
        ),
        target=QuotaQuantity(8, QuotaUnit("1")),
        disposition=ApplyChildDisposition.ACCEPTED,
        preference_identity=(
            "projects/123/locations/global/quotaPreferences/cqmgr-direct"
        ),
        lineage_etag="etag-1",
        lineage_trace_id="trace-1",
        baseline=QuotaQuantity(0, QuotaUnit("1")),
    )


def _preference(child: WatchChildIdentity) -> cloudquotas_v1.QuotaPreference:
    return cloudquotas_v1.QuotaPreference(
        name=child.preference_identity,
        service=child.slice_identity.service,
        quota_id=child.slice_identity.quota_id,
        dimensions=dict(child.slice_identity.dimensions.items),
        quota_config=cloudquotas_v1.QuotaConfig(
            preferred_value=child.target.value,
            granted_value=child.target.value,
            trace_id="trace-1",
        ),
        etag="etag-2",
        reconciling=False,
        update_time=STATUS_TIME,
    )


def _quota_info(child: WatchChildIdentity) -> cloudquotas_v1.QuotaInfo:
    return cloudquotas_v1.QuotaInfo(
        name=(
            f"{child.slice_identity.resource_scope.canonical_name}/locations/global"
            f"/services/{child.slice_identity.service}"
            f"/quotaInfos/{child.slice_identity.quota_id}"
        ),
        service=child.slice_identity.service,
        quota_id=child.slice_identity.quota_id,
        metric="compute.googleapis.com/gpus",
        metric_unit=child.target.unit.symbol,
        dimensions=("gpu_family", "region"),
        dimensions_infos=(
            cloudquotas_v1.DimensionsInfo(
                dimensions=dict(child.slice_identity.dimensions.items),
                applicable_locations=("us-east5",),
                details=cloudquotas_v1.QuotaDetails(value=8),
            ),
        ),
    )


def test_watch_observation_reads_bound_preference_and_exact_effective_slice() -> None:
    """One read returns orthogonal lifecycle axes without mutation capability."""
    child = _child()
    client = FakeObservationClient(_preference(child), _quota_info(child))
    ticks = iter((100.0, 101.0, 102.0))
    reader = GoogleWatchObservationReader(
        client,
        now=lambda: NOW,
        monotonic=lambda: next(ticks),
        timeout_seconds=20,
    )

    observation = asyncio.run(
        reader.observe(
            WatchObservationRequest(
                child=child,
                deadline=110,
                cancellation=CancellationToken(),
            )
        )
    )

    assert client.calls == [
        ("preference", child.preference_identity, 10),
        (
            "quota-info",
            (
                "projects/123/locations/global/services/compute.googleapis.com"
                "/quotaInfos/GPUS-PER-GPU-FAMILY-per-project-region"
            ),
            9,
        ),
    ]
    assert observation.preference_target == child.target
    assert observation.status.reconciliation is Reconciliation.SETTLED
    assert observation.status.is_granted
    assert observation.status.effective_confirmation is EffectiveConfirmation.CONFIRMED
    assert observation.status.status_observed_at == STATUS_TIME
    assert observation.status.effective_observed_at == NOW
    assert observation.etag == "etag-2"
    assert observation.trace_id == "trace-1"


def test_watch_observation_without_grant_is_not_settled() -> None:
    """A cleared reconciling flag cannot prove settlement without a grant."""
    child = _child()
    preference = _preference(child)
    cloudquotas_v1.QuotaConfig.pb(preference.quota_config).ClearField("granted_value")
    reader = GoogleWatchObservationReader(
        FakeObservationClient(preference, _quota_info(child)),
        now=lambda: NOW,
        monotonic=iter((100.0, 101.0)).__next__,
    )

    observation = asyncio.run(
        reader.observe(
            WatchObservationRequest(
                child=child,
                deadline=110,
                cancellation=CancellationToken(),
            )
        )
    )

    assert observation.status.reconciliation is Reconciliation.UNKNOWN
    assert observation.status.granted is None
    assert observation.status.watch(WatchCondition.GRANTED) is WatchDisposition.PENDING


@pytest.mark.parametrize(
    ("granted_value", "satisfaction"),
    [
        (0, GrantSatisfaction.NONE),
        (3, GrantSatisfaction.PARTIAL),
    ],
)
def test_watch_observation_retains_explicit_nonmatching_terminal_grant(
    granted_value: int,
    satisfaction: GrantSatisfaction,
) -> None:
    """Explicit zero and partial grants settle while leaving the condition unmet."""
    child = _child()
    preference = _preference(child)
    cloudquotas_v1.QuotaConfig.pb(
        preference.quota_config
    ).granted_value.value = granted_value
    reader = GoogleWatchObservationReader(
        FakeObservationClient(preference, _quota_info(child)),
        now=lambda: NOW,
        monotonic=iter((100.0, 101.0)).__next__,
    )

    observation = asyncio.run(
        reader.observe(
            WatchObservationRequest(
                child=child,
                deadline=110,
                cancellation=CancellationToken(),
            )
        )
    )

    assert observation.status.reconciliation is Reconciliation.SETTLED
    assert observation.status.granted == QuotaQuantity(granted_value, child.target.unit)
    assert observation.status.grant_satisfaction is satisfaction
    assert observation.status.watch(WatchCondition.GRANTED) is WatchDisposition.UNMET


@pytest.mark.parametrize(
    "error",
    [
        google_exceptions.DeadlineExceeded("transient"),
        google_exceptions.InternalServerError("transient"),
        google_exceptions.ResourceExhausted("transient"),
        google_exceptions.ServiceUnavailable("transient"),
        google_exceptions.TooManyRequests("transient"),
    ],
)
def test_watch_observation_translates_documented_transient_reads_without_retry(
    error: BaseException,
) -> None:
    """Documented transient GET failures cross the port once without provider detail."""

    class TransientClient(FakeObservationClient):
        @override
        async def quota_preference(
            self,
            *,
            name: str,
            timeout_seconds: float,
        ) -> cloudquotas_v1.QuotaPreference:
            self.calls.append(("preference", name, timeout_seconds))
            raise error

    child = _child()
    client = TransientClient(_preference(child), _quota_info(child))
    reader = GoogleWatchObservationReader(
        client,
        now=lambda: NOW,
        monotonic=lambda: 100,
    )

    with pytest.raises(WatchObservationTransientError) as captured:
        asyncio.run(
            reader.observe(
                WatchObservationRequest(
                    child=child,
                    deadline=110,
                    cancellation=CancellationToken(),
                )
            )
        )

    assert captured.value.retry_after_seconds is None
    assert client.calls == [("preference", child.preference_identity, 10)]


def test_watch_observation_preserves_provider_retry_guidance() -> None:
    """Structured RetryInfo remains available to the adaptive Watch scheduler."""
    retry_info = error_details_pb2.RetryInfo(
        retry_delay=duration_pb2.Duration(seconds=7, nanos=500_000_000)
    )
    error = google_exceptions.TooManyRequests(
        "transient",
        details=(retry_info,),
    )

    class TransientClient(FakeObservationClient):
        @override
        async def quota_preference(
            self,
            *,
            name: str,
            timeout_seconds: float,
        ) -> cloudquotas_v1.QuotaPreference:
            del name, timeout_seconds
            raise error

    child = _child()
    reader = GoogleWatchObservationReader(
        TransientClient(_preference(child), _quota_info(child)),
        now=lambda: NOW,
        monotonic=lambda: 100,
    )

    with pytest.raises(WatchObservationTransientError) as captured:
        asyncio.run(
            reader.observe(
                WatchObservationRequest(
                    child=child,
                    deadline=110,
                    cancellation=CancellationToken(),
                )
            )
        )
    assert captured.value.retry_after_seconds == PROVIDER_RETRY_SECONDS


def test_official_watch_client_uses_exact_gets_and_disables_retry() -> None:
    """Generated request and retry objects terminate inside the adapter."""
    child = _child()
    generated = FakeOfficialClient(_preference(child), _quota_info(child))
    client = OfficialCloudQuotasObservationClient(
        cast("cloudquotas_v1.CloudQuotasAsyncClient", generated)
    )
    info_name = (
        "projects/123/locations/global/services/compute.googleapis.com"
        "/quotaInfos/GPUS-PER-GPU-FAMILY-per-project-region"
    )

    preference = asyncio.run(
        client.quota_preference(
            name=child.preference_identity,
            timeout_seconds=3.5,
        )
    )
    info = asyncio.run(client.quota_info(name=info_name, timeout_seconds=4.5))

    assert preference.name == child.preference_identity
    assert info.name == info_name
    preference_request = cast(
        "cloudquotas_v1.GetQuotaPreferenceRequest", generated.calls[0][1]
    )
    info_request = cast("cloudquotas_v1.GetQuotaInfoRequest", generated.calls[1][1])
    assert preference_request.name == child.preference_identity
    assert info_request.name == info_name
    assert generated.calls[0][2:] == (None, 3.5)
    assert generated.calls[1][2:] == (None, 4.5)


def test_watch_observation_cancels_an_in_flight_exact_read() -> None:
    """Cancellation ends an active read without starting another provider call."""

    async def run() -> None:
        client = BlockingObservationClient()
        cancellation = CancellationToken()
        reader = GoogleWatchObservationReader(
            client,
            now=lambda: NOW,
            monotonic=lambda: 100,
        )
        task = asyncio.create_task(
            reader.observe(
                WatchObservationRequest(
                    child=_child(),
                    deadline=110,
                    cancellation=cancellation,
                )
            )
        )
        await client.started.wait()
        cancellation.cancel()

        with pytest.raises(CoordinationCancelledError):
            await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(run())


def test_watch_observation_expired_deadline_performs_no_provider_read() -> None:
    """An exhausted caller deadline cannot cross the exact read boundary."""
    child = _child()
    client = FakeObservationClient(_preference(child), _quota_info(child))
    reader = GoogleWatchObservationReader(
        client,
        now=lambda: NOW,
        monotonic=lambda: 110,
    )

    with pytest.raises(CoordinationDeadlineExceededError):
        asyncio.run(
            reader.observe(
                WatchObservationRequest(
                    child=child,
                    deadline=110,
                    cancellation=CancellationToken(),
                )
            )
        )

    assert client.calls == []


def test_watch_observation_rejects_undeclared_effective_dimensions() -> None:
    """A same-name QuotaInfo cannot smuggle an undeclared dimension identity."""
    child = _child()
    info = _quota_info(child)
    info.dimensions = ["region"]
    reader = GoogleWatchObservationReader(
        FakeObservationClient(_preference(child), info),
        now=lambda: NOW,
        monotonic=iter((100.0, 101.0)).__next__,
    )

    with pytest.raises(ValueError, match="QuotaInfo"):
        asyncio.run(
            reader.observe(
                WatchObservationRequest(
                    child=child,
                    deadline=110,
                    cancellation=CancellationToken(),
                )
            )
        )
