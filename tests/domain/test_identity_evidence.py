"""ADC identity evidence and capability contracts."""

from __future__ import annotations

from dataclasses import fields

import pytest

from cqmgr.domain.diagnostics import DiagnosticCode
from cqmgr.domain.identity import (
    ADCIdentityEvidence,
    ADCQuotaProject,
    CredentialKind,
    PrincipalIdentity,
    PrincipalVerification,
    ProviderIdentityEvidence,
    VerifiedDirectUserEmail,
)


def test_verified_identity_enables_mutation_without_mixing_operation_inputs() -> None:
    """Verified identity evidence remains separate from scope and contact intent."""
    evidence = ADCIdentityEvidence(
        credential_kind=CredentialKind.DIRECT_USER,
        acting_principal=PrincipalIdentity("principal://accounts.google.com/12345"),
        stable_principal=PrincipalIdentity("principal://accounts.google.com/12345"),
        verification=PrincipalVerification.VERIFIED,
        adc_quota_project=ADCQuotaProject("billing-project"),
        direct_user_email=VerifiedDirectUserEmail("fixture.user@example.com"),
    )

    assert evidence.read_capability
    assert evidence.preview_apply_capability
    assert {field.name for field in fields(evidence)} == {
        "credential_kind",
        "acting_principal",
        "stable_principal",
        "impersonation_chain",
        "verification",
        "adc_quota_project",
        "direct_user_email",
        "diagnostics",
    }
    assert evidence.acting_principal == evidence.stable_principal
    assert "fixture.user@example.com" not in repr(evidence)
    assert evidence.transport_budget_identity is not None
    assert evidence.transport_budget_identity.value.startswith("adc-quota-project:")


def test_unverified_principal_keeps_read_capability_and_fails_closed_for_writes() -> (
    None
):
    """An unidentified but refreshed ADC context remains read-only."""
    evidence = ADCIdentityEvidence.principal_unverified(
        credential_kind=CredentialKind.FEDERATED,
        adc_quota_project=None,
    )

    assert evidence.read_capability
    assert not evidence.preview_apply_capability
    assert evidence.stable_principal is None
    assert evidence.transport_budget_identity is None
    assert [item.code for item in evidence.diagnostics] == [
        DiagnosticCode("principal-unverified")
    ]


def test_result_identity_evidence_discards_transport_and_contact_state() -> None:
    """Durable principal evidence excludes ADC quota project and direct-user email."""
    principal = PrincipalIdentity("principal://accounts.google.com/12345")
    adc = ADCIdentityEvidence(
        credential_kind=CredentialKind.DIRECT_USER,
        acting_principal=principal,
        stable_principal=principal,
        verification=PrincipalVerification.VERIFIED,
        adc_quota_project=ADCQuotaProject("billing-project"),
        direct_user_email=VerifiedDirectUserEmail("fixture.user@example.com"),
    )

    retained = ProviderIdentityEvidence.from_adc(adc)

    assert retained == ProviderIdentityEvidence(
        credential_kind=CredentialKind.DIRECT_USER,
        verification=PrincipalVerification.VERIFIED,
        acting_principal=principal,
    )
    assert {field.name for field in fields(retained)} == {
        "credential_kind",
        "verification",
        "acting_principal",
        "impersonation_chain",
    }
    assert "billing-project" not in repr(retained)
    assert "fixture.user@example.com" not in repr(retained)


def test_verified_provider_identity_requires_an_acting_principal() -> None:
    """Durable verified evidence cannot claim proof without its principal."""
    with pytest.raises(ValueError, match="verified provider identity"):
        ProviderIdentityEvidence(
            credential_kind=CredentialKind.SERVICE_ACCOUNT,
            verification=PrincipalVerification.VERIFIED,
            acting_principal=None,
        )


def test_unavailable_adc_disables_all_provider_capabilities() -> None:
    """An ADC load or refresh failure cannot authorize even a provider read."""
    evidence = ADCIdentityEvidence.unavailable(
        credential_kind=CredentialKind.UNKNOWN,
        code="adc-unavailable",
        guidance="Repair Application Default Credentials outside cqmgr, then retry.",
    )

    assert not evidence.read_capability
    assert not evidence.preview_apply_capability
    assert evidence.verification is PrincipalVerification.UNAVAILABLE


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "principal identity"),
        ("contains whitespace", "principal identity"),
        ("user@example.com", "principal identity"),
    ],
)
def test_principal_identity_requires_a_typed_stable_namespace(
    value: str,
    message: str,
) -> None:
    """A bare email or display label is never accepted as identity proof."""
    with pytest.raises(ValueError, match=message):
        PrincipalIdentity(value)


def test_verified_evidence_requires_complete_consistent_identity() -> None:
    """Verified state cannot omit its stable acting principal or chain endpoint."""
    target = PrincipalIdentity("serviceAccount:target@example.iam.gserviceaccount.com")
    source = PrincipalIdentity("principal://accounts.google.com/12345")

    with pytest.raises(ValueError, match="verified identity"):
        ADCIdentityEvidence(
            credential_kind=CredentialKind.IMPERSONATED,
            acting_principal=target,
            stable_principal=None,
            impersonation_chain=(source, target),
            verification=PrincipalVerification.VERIFIED,
        )

    with pytest.raises(ValueError, match="chain must end"):
        ADCIdentityEvidence(
            credential_kind=CredentialKind.IMPERSONATED,
            acting_principal=target,
            stable_principal=target,
            impersonation_chain=(source,),
            verification=PrincipalVerification.VERIFIED,
        )
