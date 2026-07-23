"""Hermetic Compute Preview Spot advice adapter contracts."""

# ruff: noqa: PLR2004

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import override

import pytest
from google.api_core import exceptions as google_exceptions

from cqmgr.adapters.google.read_policy import GoogleReadPolicy
from cqmgr.adapters.google.spot_advice import (
    GoogleCapacityAdviceReader,
    GoogleCapacityHistoryReader,
    JsonCapacityAdviceClient,
    OfficialCapacityAdviceJsonClient,
)
from cqmgr.application.ports.coordination import (
    BudgetGrant,
    BudgetRequest,
    CancellationToken,
)
from cqmgr.application.ports.obtainability import (
    CapacityAdviceReadRequest,
    CapacityHistoryReadRequest,
)
from cqmgr.application.ports.provider_reads import ProviderReadContext
from cqmgr.domain.identity import ADCIdentityEvidence, CredentialKind
from cqmgr.domain.obtainability import (
    DistributionShape,
    GpuAttachment,
    ObtainabilityCandidate,
    SpotMachineConfiguration,
)
from cqmgr.domain.projects import CanonicalProject
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

FIXTURES = Path(__file__).parents[1] / "fixtures" / "google"
NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


class RecordingBudget:
    """Always grant a hermetic provider-read budget."""

    async def acquire(
        self,
        request: BudgetRequest,
        *,
        deadline: float,
        cancellation: CancellationToken,
    ) -> BudgetGrant:
        """Grant the exact requested budget."""
        cancellation.raise_if_cancelled()
        return BudgetGrant(deadline - 1, request)


class NoJitter:
    """Return deterministic zero retry delay."""

    def apply(self, delay: float, *, attempt: int, identity: str) -> float:
        """Return no delay after verifying a stable retry identity."""
        del delay, attempt
        assert identity
        return 0.0


def _context() -> ProviderReadContext:
    return ProviderReadContext(
        CanonicalProject(
            ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789"),
            "public-schema-project",
            "Public schema project",
        ),
        ADCIdentityEvidence(
            credential_kind=CredentialKind.SERVICE_ACCOUNT,
            acting_principal=None,
            stable_principal=None,
            impersonation_chain=(),
            adc_quota_project=None,
        ),
        100.0,
        CancellationToken(),
    )


class FixtureClient:
    """Record one request and return a public-schema fixture."""

    def __init__(self) -> None:
        """Initialize the exact-call ledger."""
        self.calls: list[tuple[str, str, dict[str, object], float]] = []

    async def capacity(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Return the current-advice fixture."""
        self.calls.append((project, region, body, timeout_seconds))
        return JsonCapacityAdviceClient.load_fixture(
            FIXTURES / "spot-capacity-advice.json"
        )

    async def capacity_history(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Return the history fixture."""
        self.calls.append((project, region, body, timeout_seconds))
        return JsonCapacityAdviceClient.load_fixture(
            FIXTURES / "spot-capacity-history.json"
        )


class PermissionDeniedClient(FixtureClient):
    """Return a provider authorization failure without sensitive text."""

    @override
    async def capacity(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Raise the public Google permission-denied type."""
        del project, region, body, timeout_seconds
        msg = "redacted"
        raise google_exceptions.PermissionDenied(msg)


class IncompleteAdviceClient(FixtureClient):
    """Return a checked-in structurally incomplete public-schema response."""

    @override
    async def capacity(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Return the incomplete fixture."""
        del project, region, body, timeout_seconds
        return JsonCapacityAdviceClient.load_fixture(
            FIXTURES / "spot-capacity-advice-incomplete.json"
        )


class StaticPayloadClient(FixtureClient):
    """Return one scripted JSON object through either provider method."""

    def __init__(self, payload: dict[str, object]) -> None:
        """Retain the public-schema payload."""
        super().__init__()
        self.payload = payload

    @override
    async def capacity(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Return the scripted payload as current advice."""
        del project, region, body, timeout_seconds
        return self.payload

    @override
    async def capacity_history(
        self,
        *,
        project: str,
        region: str,
        body: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Return the scripted payload as history."""
        del project, region, body, timeout_seconds
        return self.payload


def _policy() -> GoogleReadPolicy:
    """Build one deterministic single-attempt read policy."""
    return GoogleReadPolicy(
        RecordingBudget(),
        NoJitter(),
        maximum_attempts=1,
        monotonic=lambda: 0.0,
    )


def _candidate() -> ObtainabilityCandidate:
    """Build one supported regional request."""
    return ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        4,
        DistributionShape.ANY,
    )


def test_capacity_advice_preserves_n1_gpu_request_and_regional_shards() -> None:
    """The adapter binds the whole request while keeping shards score-free."""
    client = FixtureClient()
    reader = GoogleCapacityAdviceReader(
        client,
        GoogleReadPolicy(
            RecordingBudget(),
            NoJitter(),
            monotonic=lambda: 0.0,
        ),
        clock=lambda: NOW,
    )
    candidate = ObtainabilityCandidate(
        "us-central1",
        ("us-central1-a",),
        SpotMachineConfiguration(
            "n1-standard-16",
            GpuAttachment("nvidia-tesla-t4", 2),
        ),
        3,
        DistributionShape.ANY_SINGLE_ZONE,
    )

    result = asyncio.run(reader.read(CapacityAdviceReadRequest(_context(), candidate)))

    assert result.complete
    assert result.values[0].obtainability.as_tuple().digits == (8,)
    assert result.values[0].shards[0].zone == "us-central1-a"
    body = client.calls[0][2]
    assert body["instanceFlexibilityPolicy"] == {
        "instanceSelections": {
            "cqmgr-selection": {
                "machineTypes": ["n1-standard-16"],
                "guestAccelerators": [
                    {
                        "acceleratorType": "nvidia-tesla-t4",
                        "acceleratorCount": 2,
                    }
                ],
                "disks": [],
            }
        }
    }
    assert body["distributionPolicy"] == {
        "zones": [{"zone": "zones/us-central1-a"}],
        "targetShape": "ANY_SINGLE_ZONE",
    }


def test_capacity_history_preserves_intervals_and_regional_price_request() -> None:
    """Regional history retains 30 daily buckets and every price interval."""
    client = FixtureClient()
    reader = GoogleCapacityHistoryReader(
        client,
        GoogleReadPolicy(
            RecordingBudget(),
            NoJitter(),
            monotonic=lambda: 0.0,
        ),
        clock=lambda: NOW,
    )
    candidate = ObtainabilityCandidate(
        "us-central1",
        (),
        SpotMachineConfiguration("a3-highgpu-8g"),
        4,
        DistributionShape.ANY,
    )

    result = asyncio.run(
        reader.read(
            CapacityHistoryReadRequest(
                _context(),
                candidate,
                "us-central1",
                include_price=True,
            )
        )
    )

    assert result.complete
    assert len(result.values[0].preemption) == 30
    assert len(result.values[0].prices) == 2
    assert result.values[0].prices[1].usd_per_vm_hour.as_tuple().digits == (1, 5)
    body = client.calls[0][2]
    assert body["types"] == ["PREEMPTION", "PRICE"]
    assert "locationPolicy" not in body


def test_zonal_history_requests_preemption_without_regional_price() -> None:
    """A zone request never claims the separately regional price evidence."""
    client = FixtureClient()
    reader = GoogleCapacityHistoryReader(client, _policy(), clock=lambda: NOW)

    result = asyncio.run(
        reader.read(
            CapacityHistoryReadRequest(
                _context(),
                _candidate(),
                "us-central1-a",
                include_price=False,
            )
        )
    )

    assert result.complete
    assert client.calls[0][2]["types"] == ["PREEMPTION"]
    assert client.calls[0][2]["locationPolicy"] == {"location": "zones/us-central1-a"}


@pytest.mark.parametrize(
    ("client", "diagnostic_code"),
    [
        (PermissionDeniedClient(), "provider-read-authorization-failed"),
        (IncompleteAdviceClient(), "invalid-capacity-advice-evidence"),
    ],
)
def test_current_advice_failures_are_typed_and_secret_safe(
    client: FixtureClient,
    diagnostic_code: str,
) -> None:
    """Permission and malformed evidence produce no invented advice value."""
    reader = GoogleCapacityAdviceReader(client, _policy(), clock=lambda: NOW)

    result = asyncio.run(
        reader.read(CapacityAdviceReadRequest(_context(), _candidate()))
    )

    assert not result.complete
    assert result.values == ()
    assert result.diagnostics[0].code.value == diagnostic_code
    assert "redacted" not in result.diagnostics[0].message.value


@pytest.mark.parametrize(
    "payload",
    [
        {"machineType": "", "location": "regions/us-central1"},
        {
            "machineType": "n2-standard-4",
            "location": "regions/us-central1",
            "preemptionHistory": "invalid",
        },
        {
            "machineType": "n2-standard-4",
            "location": "regions/us-central1",
            "priceHistory": [
                {
                    "interval": {
                        "startTime": 1,
                        "endTime": "2026-07-24T00:00:00Z",
                    },
                    "listPrice": {
                        "currencyCode": "USD",
                        "units": "1",
                        "nanos": 0,
                    },
                }
            ],
        },
        {
            "machineType": "n2-standard-4",
            "location": "regions/us-central1",
            "priceHistory": [
                {
                    "interval": {
                        "startTime": "2026-07-23T00:00:00Z",
                        "endTime": "2026-07-24T00:00:00Z",
                    },
                    "listPrice": {
                        "currencyCode": "EUR",
                        "units": "1",
                        "nanos": 0,
                    },
                }
            ],
        },
    ],
)
def test_invalid_history_field_shapes_return_incomplete_evidence(
    payload: dict[str, object],
) -> None:
    """Malformed provider fields never escape or become normalized history."""
    reader = GoogleCapacityHistoryReader(
        StaticPayloadClient(payload),
        _policy(),
        clock=lambda: NOW,
    )

    result = asyncio.run(
        reader.read(
            CapacityHistoryReadRequest(
                _context(),
                _candidate(),
                "us-central1",
                include_price=True,
            )
        )
    )

    assert not result.complete
    assert result.diagnostics[0].code.value == "invalid-capacity-history-evidence"


class FakeResponse:
    """Expose one successful public JSON response."""

    def raise_for_status(self) -> None:
        """Model a successful HTTP status."""

    def json(self) -> dict[str, object]:
        """Return a minimal object response."""
        return {"ok": True}


class FakeSession:
    """Record official REST transport calls without network access."""

    def __init__(self) -> None:
        """Initialize the call ledger."""
        self.calls: list[tuple[str, dict[str, object], float]] = []
        self.closed = False

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> FakeResponse:
        """Record one finite-timeout POST."""
        self.calls.append((url, json, timeout))
        return FakeResponse()

    def close(self) -> None:
        """Record transport cleanup."""
        self.closed = True


def test_official_json_client_uses_beta_routes_and_closes_session() -> None:
    """The narrow REST client dispatches both documented beta endpoints."""
    client = object.__new__(OfficialCapacityAdviceJsonClient)
    session = FakeSession()
    object.__setattr__(client, "_session", session)

    capacity = asyncio.run(
        client.capacity(
            project="public-schema-project",
            region="us-central1",
            body={"size": 1},
            timeout_seconds=2.0,
        )
    )
    history = asyncio.run(
        client.capacity_history(
            project="public-schema-project",
            region="us-central1",
            body={"types": ["PREEMPTION"]},
            timeout_seconds=3.0,
        )
    )
    client.close()

    assert capacity == history == {"ok": True}
    assert session.calls[0][0].endswith("/advice/capacity")
    assert session.calls[1][0].endswith("/advice/capacityHistory")
    assert session.closed
