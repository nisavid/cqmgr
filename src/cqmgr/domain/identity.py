"""Provider-neutral ADC identity evidence and capability boundaries."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum

from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.redaction import RedactedText

_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
_PROJECT_NUMBER = re.compile(r"(?:projects/)?[0-9]+\Z")
_PRINCIPAL = re.compile(
    r"(?:serviceAccount:[^\s:@]+@[^\s:@]+|"
    r"principal://[^\s]+|federated://sha256/[0-9a-f]{64})\Z"
)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\Z")
_BUDGET_IDENTITY = re.compile(r"adc-quota-project:[0-9a-f]{64}\Z")
_MAXIMUM_EMAIL_LENGTH = 254


class CredentialKind(StrEnum):
    """Closed ADC credential shapes understood by the identity adapter."""

    DIRECT_USER = "direct-user"
    SERVICE_ACCOUNT = "service-account"
    IMPERSONATED = "impersonated"
    FEDERATED = "federated"
    UNKNOWN = "unknown"


class PrincipalVerification(StrEnum):
    """Whether refreshed ADC provides mutation-gating principal proof."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PrincipalIdentity:
    """One stable, explicitly namespaced principal identity."""

    value: str

    def __post_init__(self) -> None:
        """Reject bare emails, labels, paths, and untyped identities."""
        if not isinstance(self.value, str) or _PRINCIPAL.fullmatch(self.value) is None:
            msg = "principal identity must use a supported stable namespace"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class VerifiedDirectUserEmail:
    """Transient verified UserInfo email, separate from principal and contact."""

    value: str

    def __post_init__(self) -> None:
        """Require a bounded single-line email-shaped UserInfo value."""
        if (
            not isinstance(self.value, str)
            or len(self.value) > _MAXIMUM_EMAIL_LENGTH
            or _EMAIL.fullmatch(self.value) is None
        ):
            msg = "verified direct-user email must be a valid bounded email"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ADCQuotaProjectBudgetIdentity:
    """Non-secret local budget identity for one ADC transport quota project."""

    value: str

    def __post_init__(self) -> None:
        """Require the closed hashed budget-identity representation."""
        if (
            not isinstance(self.value, str)
            or _BUDGET_IDENTITY.fullmatch(self.value) is None
        ):
            msg = "ADC quota-project budget identity must be a lowercase SHA-256 name"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ADCQuotaProject:
    """A transport quota-project reference that is never a resource scope."""

    value: str

    def __post_init__(self) -> None:
        """Accept a project ID, number, or canonical numeric project name."""
        if not isinstance(self.value, str) or (
            _PROJECT_ID.fullmatch(self.value) is None
            and _PROJECT_NUMBER.fullmatch(self.value) is None
        ):
            msg = "ADC quota project must be a project ID, number, or canonical name"
            raise ValueError(msg)

    @property
    def google_auth_value(self) -> str:
        """Return the identifier accepted by google-auth quota-project override."""
        return self.value.removeprefix("projects/")

    @property
    def budget_identity(self) -> ADCQuotaProjectBudgetIdentity:
        """Return a stable non-secret identity without exposing the project value."""
        digest = hashlib.sha256(self.google_auth_value.encode()).hexdigest()
        return ADCQuotaProjectBudgetIdentity(f"adc-quota-project:{digest}")


@dataclass(frozen=True, slots=True)
class ADCIdentityEvidence:
    """Safe refreshed identity evidence with explicit provider capabilities."""

    credential_kind: CredentialKind
    acting_principal: PrincipalIdentity | None
    stable_principal: PrincipalIdentity | None
    impersonation_chain: tuple[PrincipalIdentity, ...] = ()
    verification: PrincipalVerification = PrincipalVerification.UNVERIFIED
    adc_quota_project: ADCQuotaProject | None = None
    direct_user_email: VerifiedDirectUserEmail | None = field(
        default=None,
        repr=False,
    )
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:  # noqa: C901
        """Keep verification, principal proof, chain, and diagnostics coherent."""
        if not isinstance(self.credential_kind, CredentialKind):
            msg = "credential_kind must be a CredentialKind"
            raise TypeError(msg)
        if not isinstance(self.verification, PrincipalVerification):
            msg = "verification must be a PrincipalVerification"
            raise TypeError(msg)
        if self.acting_principal is not None and not isinstance(
            self.acting_principal, PrincipalIdentity
        ):
            msg = "acting_principal must use PrincipalIdentity"
            raise TypeError(msg)
        if self.stable_principal is not None and not isinstance(
            self.stable_principal, PrincipalIdentity
        ):
            msg = "stable_principal must use PrincipalIdentity"
            raise TypeError(msg)
        if not isinstance(self.impersonation_chain, tuple) or any(
            not isinstance(item, PrincipalIdentity) for item in self.impersonation_chain
        ):
            msg = "impersonation_chain must contain PrincipalIdentity values"
            raise TypeError(msg)
        if self.adc_quota_project is not None and not isinstance(
            self.adc_quota_project, ADCQuotaProject
        ):
            msg = "adc_quota_project must use ADCQuotaProject"
            raise TypeError(msg)
        if self.direct_user_email is not None and not isinstance(
            self.direct_user_email, VerifiedDirectUserEmail
        ):
            msg = "direct_user_email must use VerifiedDirectUserEmail"
            raise TypeError(msg)
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            msg = "identity diagnostics must contain Diagnostic values"
            raise TypeError(msg)

        if self.verification is PrincipalVerification.VERIFIED and (
            self.acting_principal is None or self.stable_principal is None
        ):
            msg = "verified identity requires acting and stable principal proof"
            raise ValueError(msg)
        if self.verification is not PrincipalVerification.VERIFIED and (
            self.stable_principal is not None
        ):
            msg = "unverified identity cannot claim a stable principal"
            raise ValueError(msg)
        if self.direct_user_email is not None and (
            self.credential_kind is not CredentialKind.DIRECT_USER
            or self.verification is not PrincipalVerification.VERIFIED
        ):
            msg = "verified direct-user email requires verified direct-user ADC"
            raise ValueError(msg)
        if self.impersonation_chain and (
            self.acting_principal is None
            or self.impersonation_chain[-1] != self.acting_principal
        ):
            msg = "impersonation chain must end at the acting principal"
            raise ValueError(msg)

    @property
    def read_capability(self) -> bool:
        """Whether refreshed credentials remain usable for read-only providers."""
        return self.verification is not PrincipalVerification.UNAVAILABLE

    @property
    def preview_apply_capability(self) -> bool:
        """Whether exact stable principal proof can gate Preview and Apply."""
        return self.verification is PrincipalVerification.VERIFIED

    @property
    def transport_budget_identity(self) -> ADCQuotaProjectBudgetIdentity | None:
        """Return the optional transport-budget axis without a sentinel fallback."""
        if self.adc_quota_project is None:
            return None
        return self.adc_quota_project.budget_identity

    @classmethod
    def principal_unverified(
        cls,
        *,
        credential_kind: CredentialKind,
        adc_quota_project: ADCQuotaProject | None,
        guidance: str = (
            "Configure ADC with a stable verifiable principal outside cqmgr, "
            "then retry Preview or Apply."
        ),
    ) -> ADCIdentityEvidence:
        """Return read-capable evidence with the required fail-closed diagnostic."""
        return cls(
            credential_kind=credential_kind,
            acting_principal=None,
            stable_principal=None,
            verification=PrincipalVerification.UNVERIFIED,
            adc_quota_project=adc_quota_project,
            diagnostics=(
                _identity_diagnostic(
                    "principal-unverified",
                    guidance,
                    severity=Severity.WARNING,
                    retry=RetryDisposition.NEVER,
                ),
            ),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        credential_kind: CredentialKind,
        code: str,
        guidance: str,
    ) -> ADCIdentityEvidence:
        """Return safe evidence for an ADC load or refresh failure."""
        return cls(
            credential_kind=credential_kind,
            acting_principal=None,
            stable_principal=None,
            verification=PrincipalVerification.UNAVAILABLE,
            diagnostics=(
                _identity_diagnostic(
                    code,
                    guidance,
                    severity=Severity.ERROR,
                    retry=RetryDisposition.AFTER_REFRESH,
                ),
            ),
        )


def _identity_diagnostic(
    code: str,
    guidance: str,
    *,
    severity: Severity,
    retry: RetryDisposition,
) -> Diagnostic:
    """Build one static diagnostic that cannot retain provider failure text."""
    return Diagnostic(
        code=DiagnosticCode(code),
        severity=severity,
        phase=DiagnosticPhase("identity-resolution"),
        source=DiagnosticSource("application-default-credentials"),
        retry=retry,
        message=RedactedText(guidance),
    )
