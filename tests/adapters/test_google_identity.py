"""Hermetic ADC identity adapter contracts from public identity schemas."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from google.auth import external_account, identity_pool, impersonated_credentials
from google.oauth2 import credentials as user_credentials
from google.oauth2 import service_account

from cqmgr.adapters.google.identity import (
    ADC_IDENTITY_SCOPES,
    ADCCredentialSnapshot,
    GoogleADCIdentityProvider,
    GoogleAuthRuntime,
)
from cqmgr.domain.identity import (
    ADCQuotaProject,
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

FIXTURES = Path(__file__).parents[1] / "fixtures" / "google"
PUBLIC_FIXTURE_TOKEN = "public-fixture-token"  # noqa: S105
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"  # noqa: S105
STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"  # noqa: S105
USERINFO_TIMEOUT_SECONDS = 10.0


class FakeADCRuntime:
    """Scripted no-network ADC runtime that retains no token or credential path."""

    def __init__(
        self,
        snapshot: ADCCredentialSnapshot | BaseException,
        *,
        user_info: Mapping[str, object] | BaseException | None = None,
        refresh_error: BaseException | None = None,
    ) -> None:
        """Configure one scripted ADC execution."""
        self.snapshot = snapshot
        self.user_info = user_info
        self.refresh_error = refresh_error
        self.load_calls: list[tuple[tuple[str, ...], str | None]] = []
        self.refreshed: list[ADCCredentialSnapshot] = []

    def load(
        self,
        *,
        scopes: Sequence[str],
        quota_project_id: str | None,
    ) -> ADCCredentialSnapshot:
        """Record exact scope and quota-project inputs without ambient state."""
        self.load_calls.append((tuple(scopes), quota_project_id))
        if isinstance(self.snapshot, BaseException):
            raise self.snapshot
        return self.snapshot

    def refresh(self, snapshot: ADCCredentialSnapshot) -> None:
        """Record one refresh or inject a safe test failure."""
        self.refreshed.append(snapshot)
        if self.refresh_error is not None:
            raise self.refresh_error

    def fetch_user_info(
        self,
        snapshot: ADCCredentialSnapshot,
    ) -> Mapping[str, object]:
        """Return a public OpenID UserInfo fixture without making a request."""
        assert snapshot.kind is CredentialKind.DIRECT_USER
        if isinstance(self.user_info, BaseException):
            raise self.user_info
        assert self.user_info is not None
        return self.user_info


def _userinfo() -> Mapping[str, object]:
    return cast(
        "Mapping[str, object]",
        json.loads((FIXTURES / "openid-userinfo.json").read_text()),
    )


def test_direct_user_refreshes_with_exact_scopes_and_quota_override() -> None:
    """Direct-user ADC uses UserInfo subject proof and keeps quota project separate."""
    snapshot = ADCCredentialSnapshot(
        kind=CredentialKind.DIRECT_USER,
        credential=object(),
        discovered_project_id="must-not-become-resource-scope",
        quota_project_id="embedded-project",
    )
    runtime = FakeADCRuntime(snapshot, user_info=_userinfo())
    provider = GoogleADCIdentityProvider(runtime)

    evidence = asyncio.run(
        provider.resolve(adc_quota_project=ADCQuotaProject("projects/415104041262"))
    )

    principal = PrincipalIdentity(
        "principal://accounts.google.com/110169484474386276334"
    )
    assert runtime.load_calls == [(ADC_IDENTITY_SCOPES, "415104041262")]
    assert runtime.refreshed == [snapshot]
    assert evidence.stable_principal == principal
    assert evidence.acting_principal == principal
    assert evidence.adc_quota_project == ADCQuotaProject("projects/415104041262")
    assert evidence.direct_user_email is not None
    assert evidence.direct_user_email.value == "fixture.user@example.com"
    assert evidence.transport_budget_identity is not None
    assert evidence.preview_apply_capability
    assert "must-not-become-resource-scope" not in repr(evidence)
    assert "fixture.user@example.com" not in repr(evidence)


def test_service_account_identity_uses_canonical_principal() -> None:
    """A refreshed service-account credential supplies stable principal proof."""
    runtime = FakeADCRuntime(
        ADCCredentialSnapshot(
            kind=CredentialKind.SERVICE_ACCOUNT,
            credential=object(),
            service_account_email="worker@example.iam.gserviceaccount.com",
            quota_project_id="billing-project",
        )
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    expected = PrincipalIdentity(
        "serviceAccount:worker@example.iam.gserviceaccount.com"
    )
    assert evidence.acting_principal == expected
    assert evidence.stable_principal == expected
    assert evidence.adc_quota_project == ADCQuotaProject("billing-project")
    assert evidence.impersonation_chain == ()


def test_impersonated_identity_preserves_complete_source_delegate_chain() -> None:
    """Impersonation evidence orders verified source, delegates, and acting target."""
    source = ADCCredentialSnapshot(
        kind=CredentialKind.SERVICE_ACCOUNT,
        credential=object(),
        service_account_email="source@example.iam.gserviceaccount.com",
    )
    runtime = FakeADCRuntime(
        ADCCredentialSnapshot(
            kind=CredentialKind.IMPERSONATED,
            credential=object(),
            source=source,
            delegates=("delegate@example.iam.gserviceaccount.com",),
            service_account_email="target@example.iam.gserviceaccount.com",
        )
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    assert tuple(item.value for item in evidence.impersonation_chain) == (
        "serviceAccount:source@example.iam.gserviceaccount.com",
        "serviceAccount:delegate@example.iam.gserviceaccount.com",
        "serviceAccount:target@example.iam.gserviceaccount.com",
    )
    assert evidence.stable_principal == evidence.impersonation_chain[-1]
    assert evidence.preview_apply_capability


def test_federated_identity_binds_audience_subject_and_impersonation() -> None:
    """Federation uses a non-reversible stable subject identity and exact target."""
    runtime = FakeADCRuntime(
        ADCCredentialSnapshot(
            kind=CredentialKind.FEDERATED,
            credential=object(),
            federated_audience=(
                "//iam.googleapis.com/projects/123/locations/global/"
                "workloadIdentityPools/public/providers/example"
            ),
            federated_subject="repo:example/public:ref:refs/heads/main",
            service_account_email="target@example.iam.gserviceaccount.com",
        )
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    assert evidence.credential_kind is CredentialKind.FEDERATED
    assert evidence.impersonation_chain[0].value.startswith("federated://sha256/")
    assert evidence.impersonation_chain[-1].value == (
        "serviceAccount:target@example.iam.gserviceaccount.com"
    )
    assert "repo:example/public" not in repr(evidence)
    assert evidence.preview_apply_capability


def test_unverified_principal_keeps_reads_and_blocks_preview_apply() -> None:
    """Missing authoritative subject proof produces principal-unverified."""
    runtime = FakeADCRuntime(
        ADCCredentialSnapshot(
            kind=CredentialKind.FEDERATED,
            credential=object(),
            federated_audience="//iam.googleapis.com/public",
        )
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    assert evidence.verification is PrincipalVerification.UNVERIFIED
    assert evidence.read_capability
    assert not evidence.preview_apply_capability
    assert evidence.diagnostics[0].code.value == "principal-unverified"


@pytest.mark.parametrize("stage", ["load", "refresh", "userinfo"])
def test_identity_failures_return_static_redacted_diagnostics(stage: str) -> None:
    """Failure messages never retain tokens, contacts, or credential paths."""
    sensitive = (
        "token=ya29.public-fixture-only "
        "contact=fixture.user@example.com "
        "path=/Users/example/private/adc.json"
    )
    snapshot = ADCCredentialSnapshot(
        kind=CredentialKind.DIRECT_USER,
        credential=object(),
    )
    runtime = FakeADCRuntime(
        RuntimeError(sensitive) if stage == "load" else snapshot,
        user_info=RuntimeError(sensitive) if stage == "userinfo" else _userinfo(),
        refresh_error=RuntimeError(sensitive) if stage == "refresh" else None,
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    rendered = repr(evidence)
    assert "ya29" not in rendered
    assert "fixture.user@example.com" not in rendered
    assert "/Users/example" not in rendered
    if stage == "userinfo":
        assert evidence.read_capability
        assert evidence.diagnostics[0].code.value == "principal-unverified"
    else:
        assert not evidence.read_capability


def test_direct_user_requires_authoritative_subject_and_boolean_email_flag() -> None:
    """Typed email text alone is never accepted as principal proof."""
    runtime = FakeADCRuntime(
        ADCCredentialSnapshot(
            kind=CredentialKind.DIRECT_USER,
            credential=object(),
        ),
        user_info={"email": "typed@example.com", "email_verified": "true"},
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    assert evidence.verification is PrincipalVerification.UNVERIFIED
    assert "typed@example.com" not in repr(evidence)


@pytest.mark.parametrize(
    "snapshot",
    [
        ADCCredentialSnapshot(
            kind=CredentialKind.SERVICE_ACCOUNT,
            credential=object(),
            service_account_email=None,
        ),
        ADCCredentialSnapshot(
            kind=CredentialKind.IMPERSONATED,
            credential=object(),
            service_account_email="target@example.iam.gserviceaccount.com",
            source=None,
        ),
        ADCCredentialSnapshot(
            kind=CredentialKind.IMPERSONATED,
            credential=object(),
            service_account_email="target@example.iam.gserviceaccount.com",
            source=ADCCredentialSnapshot(
                kind=CredentialKind.SERVICE_ACCOUNT,
                credential=object(),
                service_account_email="source@example.iam.gserviceaccount.com",
            ),
            delegates=("not-an-email",),
        ),
        ADCCredentialSnapshot(
            kind=CredentialKind.UNKNOWN,
            credential=object(),
        ),
    ],
)
def test_incomplete_or_unknown_credential_shapes_remain_read_only(
    snapshot: ADCCredentialSnapshot,
) -> None:
    """No supported shape guesses missing principal or chain components."""
    evidence = asyncio.run(
        GoogleADCIdentityProvider(FakeADCRuntime(snapshot)).resolve()
    )

    assert evidence.read_capability
    assert not evidence.preview_apply_capability
    assert evidence.diagnostics[0].code.value == "principal-unverified"


def test_invalid_embedded_quota_project_fails_before_refresh() -> None:
    """An invalid transport quota-project identity cannot become another axis."""
    runtime = FakeADCRuntime(
        ADCCredentialSnapshot(
            kind=CredentialKind.SERVICE_ACCOUNT,
            credential=object(),
            quota_project_id="projects/not-canonical",
            service_account_email="worker@example.iam.gserviceaccount.com",
        )
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    assert not evidence.read_capability
    assert runtime.refreshed == []
    assert evidence.diagnostics[0].code.value == "adc-quota-project-invalid"


def test_direct_federated_subject_is_verified_without_impersonation() -> None:
    """An authoritative federated subject can itself be the acting principal."""
    runtime = FakeADCRuntime(
        ADCCredentialSnapshot(
            kind=CredentialKind.FEDERATED,
            credential=object(),
            federated_audience="//iam.googleapis.com/public",
            federated_subject="public-subject",
        )
    )

    evidence = asyncio.run(GoogleADCIdentityProvider(runtime).resolve())

    assert evidence.preview_apply_capability
    assert evidence.stable_principal is not None
    assert evidence.stable_principal.value.startswith("federated://sha256/")
    assert evidence.impersonation_chain == ()


class FakeSigner:
    """Non-secret signer sufficient to construct public service-account shape."""

    @property
    def key_id(self) -> str:
        """Return a public fixture key ID."""
        return "public-fixture-key"

    @property
    def signer_email(self) -> str:
        """Return the public fixture service-account email."""
        return "worker@example.iam.gserviceaccount.com"

    def sign(self, message: bytes) -> bytes:
        """Return deterministic fixture bytes without a private key."""
        return b"public-signature:" + message


class FakeFederatedSubjectResolver:
    """Provider-specific authoritative mapped-subject fixture."""

    def resolve(self, credential: external_account.Credentials) -> str | None:
        """Return a public mapped google.subject fixture."""
        del credential
        return "public-mapped-subject"


def test_google_auth_runtime_classifies_supported_public_credential_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production loader maps google-auth classes without credential info paths."""
    direct = user_credentials.Credentials(token=PUBLIC_FIXTURE_TOKEN)
    service = service_account.Credentials(
        signer=FakeSigner(),
        service_account_email="worker@example.iam.gserviceaccount.com",
        token_uri=TOKEN_URI,
        scopes=ADC_IDENTITY_SCOPES,
    )
    impersonated = impersonated_credentials.Credentials(
        source_credentials=service,
        target_principal="target@example.iam.gserviceaccount.com",
        target_scopes=ADC_IDENTITY_SCOPES,
        delegates=["delegate@example.iam.gserviceaccount.com"],
    )
    federated = identity_pool.Credentials(
        audience=(
            "//iam.googleapis.com/projects/123/locations/global/"
            "workloadIdentityPools/public/providers/example"
        ),
        subject_token_type=SUBJECT_TOKEN_TYPE,
        token_url=STS_TOKEN_URL,
        credential_source={"url": "https://metadata.example.test/token"},
        service_account_impersonation_url=(
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            "target@example.iam.gserviceaccount.com:generateAccessToken"
        ),
    )
    runtime = GoogleAuthRuntime(FakeFederatedSubjectResolver())

    expected = [
        (direct, CredentialKind.DIRECT_USER),
        (service, CredentialKind.SERVICE_ACCOUNT),
        (impersonated, CredentialKind.IMPERSONATED),
        (federated, CredentialKind.FEDERATED),
        (object(), CredentialKind.UNKNOWN),
    ]
    for credential, kind in expected:
        monkeypatch.setattr(
            "cqmgr.adapters.google.identity.google.auth.default",
            lambda credential=credential, **_kwargs: (
                credential,
                "ambient-project-must-be-ignored",
            ),
        )
        snapshot = runtime.load(scopes=ADC_IDENTITY_SCOPES, quota_project_id=None)
        assert snapshot.kind is kind
        assert "ambient-project-must-be-ignored" not in repr(snapshot)

    monkeypatch.setattr(
        "cqmgr.adapters.google.identity.google.auth.default",
        lambda **_kwargs: (federated, None),
    )
    resolved = GoogleAuthRuntime(FakeFederatedSubjectResolver()).load(
        scopes=ADC_IDENTITY_SCOPES,
        quota_project_id=None,
    )
    assert resolved.federated_subject == "public-mapped-subject"
    assert "public-mapped-subject" not in repr(resolved)


def test_google_auth_runtime_refresh_requires_a_credential_operation() -> None:
    """Malformed loaded credentials fail locally without a provider request."""
    runtime = GoogleAuthRuntime(FakeFederatedSubjectResolver())
    snapshot = ADCCredentialSnapshot(CredentialKind.UNKNOWN, object())

    with pytest.raises(TypeError, match="refresh operation"):
        runtime.refresh(snapshot)


def test_google_auth_runtime_userinfo_closes_session_and_validates_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UserInfo mechanics close their transport and reject non-object JSON."""
    closed: list[bool] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[object]:
            return []

    class FakeSession:
        def __init__(self, _credential: object) -> None:
            return None

        def get(self, url: str, *, timeout: float) -> FakeResponse:
            assert url.endswith("/v1/userinfo")
            assert timeout == USERINFO_TIMEOUT_SECONDS
            return FakeResponse()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        "cqmgr.adapters.google.identity.AuthorizedSession",
        FakeSession,
    )
    snapshot = ADCCredentialSnapshot(CredentialKind.DIRECT_USER, object())

    with pytest.raises(TypeError, match="must be an object"):
        GoogleAuthRuntime(FakeFederatedSubjectResolver()).fetch_user_info(snapshot)
    assert closed == [True]
