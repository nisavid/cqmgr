"""Google Application Default Credentials identity adapter."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import google.auth
from google.auth import external_account, impersonated_credentials
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2 import credentials as user_credentials
from google.oauth2 import service_account

from cqmgr.domain.identity import (
    ADCIdentityEvidence,
    ADCQuotaProject,
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
    VerifiedDirectUserEmail,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ADC_IDENTITY_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)
_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
_SUBJECT = re.compile(r"[A-Za-z0-9._~-]+\Z")


@dataclass(frozen=True, slots=True)
class ADCCredentialSnapshot:
    """Transient credential shape with no token or credential-path field."""

    kind: CredentialKind
    credential: object = field(repr=False, compare=False)
    discovered_project_id: str | None = field(default=None, repr=False)
    quota_project_id: str | None = None
    service_account_email: str | None = None
    source: ADCCredentialSnapshot | None = field(default=None, repr=False)
    delegates: tuple[str, ...] = ()
    federated_audience: str | None = field(default=None, repr=False)
    federated_subject: str | None = field(default=None, repr=False)


class ADCRuntime(Protocol):
    """Sync google-auth mechanics kept inside a bounded worker call."""

    def load(
        self,
        *,
        scopes: Sequence[str],
        quota_project_id: str | None,
    ) -> ADCCredentialSnapshot:
        """Load one ADC context with the requested scopes and override."""
        ...

    def refresh(self, snapshot: ADCCredentialSnapshot) -> None:
        """Refresh the exact loaded credential without retaining its token."""
        ...

    def fetch_user_info(
        self,
        snapshot: ADCCredentialSnapshot,
    ) -> Mapping[str, object]:
        """Fetch authoritative OpenID UserInfo for direct-user credentials."""
        ...


class FederatedSubjectResolver(Protocol):
    """Resolve provider-mapped google.subject without retaining a subject token."""

    def resolve(self, credential: external_account.Credentials) -> str | None:
        """Return an authoritative mapped subject, or None when unsupported."""
        ...


class GoogleAuthRuntime:
    """Production google-auth loader, refresher, and UserInfo client."""

    def __init__(
        self,
        federated_subject_resolver: FederatedSubjectResolver,
    ) -> None:
        """Require authoritative provider-specific federation composition."""
        self._federated_subject_resolver = federated_subject_resolver

    def load(
        self,
        *,
        scopes: Sequence[str],
        quota_project_id: str | None,
    ) -> ADCCredentialSnapshot:
        """Load ADC once without reading or reporting an ambient active project."""
        credential, discovered_project_id = google.auth.default(
            scopes=scopes,
            quota_project_id=quota_project_id,
        )
        return self._snapshot(credential, discovered_project_id)

    def refresh(self, snapshot: ADCCredentialSnapshot) -> None:
        """Refresh in memory through google-auth's supported transport."""
        refresh = getattr(snapshot.credential, "refresh", None)
        if not callable(refresh):
            msg = "loaded ADC credential has no refresh operation"
            raise TypeError(msg)
        refresh(Request())

    def fetch_user_info(
        self,
        snapshot: ADCCredentialSnapshot,
    ) -> Mapping[str, object]:
        """Read the OpenID UserInfo response without exposing the access token."""
        session = AuthorizedSession(snapshot.credential)  # type: ignore[arg-type]
        try:
            response = session.get(_USERINFO_ENDPOINT, timeout=10.0)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                msg = "OpenID UserInfo response must be an object"
                raise TypeError(msg)
            return payload
        finally:
            session.close()

    def _snapshot(
        self,
        credential: object,
        discovered_project_id: str | None,
    ) -> ADCCredentialSnapshot:
        """Classify only supported public credential types and safe properties."""
        quota_project_id = _optional_string(
            getattr(credential, "quota_project_id", None)
        )
        if isinstance(credential, user_credentials.Credentials):
            return ADCCredentialSnapshot(
                CredentialKind.DIRECT_USER,
                credential,
                discovered_project_id,
                quota_project_id,
            )
        if isinstance(credential, service_account.Credentials):
            return ADCCredentialSnapshot(
                CredentialKind.SERVICE_ACCOUNT,
                credential,
                discovered_project_id,
                quota_project_id,
                _optional_string(credential.service_account_email),
            )
        if isinstance(credential, impersonated_credentials.Credentials):
            source = getattr(credential, "_source_credentials", None)
            delegates = getattr(credential, "_delegates", None)
            return ADCCredentialSnapshot(
                CredentialKind.IMPERSONATED,
                credential,
                discovered_project_id,
                quota_project_id,
                _optional_string(credential.service_account_email),
                self._snapshot(source, None) if source is not None else None,
                _string_tuple(delegates),
            )
        if isinstance(credential, external_account.Credentials):
            subject = self._federated_subject_resolver.resolve(credential)
            return ADCCredentialSnapshot(
                CredentialKind.FEDERATED,
                credential,
                discovered_project_id,
                quota_project_id,
                _optional_string(credential.service_account_email),
                federated_audience=_optional_string(
                    getattr(credential, "_audience", None)
                ),
                federated_subject=_optional_string(subject),
            )
        return ADCCredentialSnapshot(
            CredentialKind.UNKNOWN,
            credential,
            discovered_project_id,
            quota_project_id,
        )


class GoogleADCIdentityProvider:
    """Resolve ADC identity without mutation, switching, or token retention."""

    def __init__(self, runtime: ADCRuntime) -> None:
        """Require explicit ADC composition, including federation support policy."""
        self._runtime = runtime

    async def resolve(
        self,
        *,
        adc_quota_project: ADCQuotaProject | None = None,
    ) -> ADCIdentityEvidence:
        """Return safe refreshed identity and explicit operation capabilities."""
        override = (
            adc_quota_project.google_auth_value
            if adc_quota_project is not None
            else None
        )
        try:
            snapshot = await asyncio.to_thread(
                self._runtime.load,
                scopes=ADC_IDENTITY_SCOPES,
                quota_project_id=override,
            )
        except Exception:  # noqa: BLE001  # text is intentionally discarded
            return ADCIdentityEvidence.unavailable(
                credential_kind=CredentialKind.UNKNOWN,
                code="adc-unavailable",
                guidance=(
                    "Repair Application Default Credentials outside cqmgr, then retry."
                ),
            )

        quota_project = adc_quota_project
        if quota_project is None and snapshot.quota_project_id is not None:
            try:
                quota_project = ADCQuotaProject(snapshot.quota_project_id)
            except (TypeError, ValueError):
                return ADCIdentityEvidence.unavailable(
                    credential_kind=snapshot.kind,
                    code="adc-quota-project-invalid",
                    guidance=(
                        "Configure a valid ADC quota project outside cqmgr, then retry."
                    ),
                )

        try:
            await asyncio.to_thread(self._runtime.refresh, snapshot)
        except Exception:  # noqa: BLE001  # text is intentionally discarded
            return ADCIdentityEvidence.unavailable(
                credential_kind=snapshot.kind,
                code="adc-refresh-failed",
                guidance=(
                    "Refresh or repair Application Default Credentials outside cqmgr, "
                    "then retry."
                ),
            )

        return await self._resolve_snapshot(snapshot, quota_project=quota_project)

    async def _resolve_snapshot(
        self,
        snapshot: ADCCredentialSnapshot,
        *,
        quota_project: ADCQuotaProject | None,
    ) -> ADCIdentityEvidence:
        if snapshot.kind is CredentialKind.DIRECT_USER:
            return await self._resolve_direct_user(snapshot, quota_project)
        if snapshot.kind is CredentialKind.SERVICE_ACCOUNT:
            principal = _service_account_principal(snapshot.service_account_email)
            if principal is None:
                return _principal_unverified(snapshot.kind, quota_project)
            return _verified(snapshot.kind, principal, quota_project=quota_project)
        if snapshot.kind is CredentialKind.IMPERSONATED:
            return await self._resolve_impersonated(snapshot, quota_project)
        if snapshot.kind is CredentialKind.FEDERATED:
            return _resolve_federated(snapshot, quota_project)
        return _principal_unverified(snapshot.kind, quota_project)

    async def _resolve_direct_user(
        self,
        snapshot: ADCCredentialSnapshot,
        quota_project: ADCQuotaProject | None,
    ) -> ADCIdentityEvidence:
        try:
            user_info = await asyncio.to_thread(
                self._runtime.fetch_user_info,
                snapshot,
            )
        except Exception:  # noqa: BLE001  # text is intentionally discarded
            return _principal_unverified(
                snapshot.kind,
                quota_project,
                direct_user=True,
            )

        subject = user_info.get("sub")
        email = user_info.get("email")
        if (
            not isinstance(subject, str)
            or _SUBJECT.fullmatch(subject) is None
            or not isinstance(email, str)
            or user_info.get("email_verified") is not True
        ):
            return _principal_unverified(
                snapshot.kind,
                quota_project,
                direct_user=True,
            )
        try:
            verified_email = VerifiedDirectUserEmail(email)
        except ValueError:
            return _principal_unverified(
                snapshot.kind,
                quota_project,
                direct_user=True,
            )
        principal = PrincipalIdentity(f"principal://accounts.google.com/{subject}")
        return _verified(
            snapshot.kind,
            principal,
            quota_project=quota_project,
            direct_user_email=verified_email,
        )

    async def _resolve_impersonated(
        self,
        snapshot: ADCCredentialSnapshot,
        quota_project: ADCQuotaProject | None,
    ) -> ADCIdentityEvidence:
        target = _service_account_principal(snapshot.service_account_email)
        if snapshot.source is None or target is None:
            return _principal_unverified(snapshot.kind, quota_project)
        source = await self._resolve_snapshot(snapshot.source, quota_project=None)
        if not source.preview_apply_capability or source.stable_principal is None:
            return _principal_unverified(snapshot.kind, quota_project)
        delegates = tuple(
            _service_account_principal(value) for value in snapshot.delegates
        )
        if any(item is None for item in delegates):
            return _principal_unverified(snapshot.kind, quota_project)
        source_chain = source.impersonation_chain or (source.stable_principal,)
        chain = (
            *source_chain,
            *(item for item in delegates if item is not None),
            target,
        )
        return _verified(
            snapshot.kind,
            target,
            quota_project=quota_project,
            impersonation_chain=chain,
        )


def _verified(
    kind: CredentialKind,
    principal: PrincipalIdentity,
    *,
    quota_project: ADCQuotaProject | None,
    impersonation_chain: tuple[PrincipalIdentity, ...] = (),
    direct_user_email: VerifiedDirectUserEmail | None = None,
) -> ADCIdentityEvidence:
    return ADCIdentityEvidence(
        credential_kind=kind,
        acting_principal=principal,
        stable_principal=principal,
        impersonation_chain=impersonation_chain,
        verification=PrincipalVerification.VERIFIED,
        adc_quota_project=quota_project,
        direct_user_email=direct_user_email,
    )


def _resolve_federated(
    snapshot: ADCCredentialSnapshot,
    quota_project: ADCQuotaProject | None,
) -> ADCIdentityEvidence:
    if not snapshot.federated_audience or not snapshot.federated_subject:
        return _principal_unverified(snapshot.kind, quota_project)
    material = f"{snapshot.federated_audience}\0{snapshot.federated_subject}".encode()
    source = PrincipalIdentity(
        f"federated://sha256/{hashlib.sha256(material).hexdigest()}"
    )
    target = _service_account_principal(snapshot.service_account_email)
    if snapshot.service_account_email is not None and target is None:
        return _principal_unverified(snapshot.kind, quota_project)
    if target is None:
        return _verified(snapshot.kind, source, quota_project=quota_project)
    return _verified(
        snapshot.kind,
        target,
        quota_project=quota_project,
        impersonation_chain=(source, target),
    )


def _principal_unverified(
    kind: CredentialKind,
    quota_project: ADCQuotaProject | None,
    *,
    direct_user: bool = False,
) -> ADCIdentityEvidence:
    guidance = (
        "Run gcloud auth application-default login with the required identity "
        "scopes outside cqmgr, then retry Preview or Apply."
        if direct_user
        else (
            "Configure ADC with explicit verifiable workload or service-account "
            "identity outside cqmgr, then retry Preview or Apply."
        )
    )
    return ADCIdentityEvidence.principal_unverified(
        credential_kind=kind,
        adc_quota_project=quota_project,
        guidance=guidance,
    )


def _service_account_principal(value: str | None) -> PrincipalIdentity | None:
    if value is None:
        return None
    try:
        return PrincipalIdentity(f"serviceAccount:{value}")
    except ValueError:
        return None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        return ()
    return tuple(value)
