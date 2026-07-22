"""Exact effective-quota-slice identity contracts."""

from dataclasses import replace
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cqmgr.domain.quotas import (
    ConstraintReference,
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaScope,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind

PROJECT = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
IDENTITY_VARIANT_COUNT = 6


@given(
    st.dictionaries(
        keys=st.text(
            alphabet=st.characters(min_codepoint=97, max_codepoint=122),
            min_size=1,
            max_size=12,
        ),
        values=st.text(max_size=20),
        max_size=8,
    )
)
def test_dimension_order_does_not_change_identity(values: dict[str, str]) -> None:
    """Provider map iteration order never changes normalized dimensions."""
    pairs = list(values.items())

    assert NormalizedDimensions(pairs) == NormalizedDimensions(reversed(pairs))
    assert hash(NormalizedDimensions(pairs)) == hash(
        NormalizedDimensions(reversed(pairs))
    )


def test_dimensions_use_only_nfc_and_deterministic_key_order() -> None:
    """Normalization composes Unicode without trimming or changing case."""
    dimensions = NormalizedDimensions(
        [(" region ", " US-CENTRAL1 "), ("gpu_famil\N{COMBINING ACUTE ACCENT}", "H100")]
    )

    assert dimensions.items == (
        (" region ", " US-CENTRAL1 "),
        ("gpu_fami\N{LATIN SMALL LETTER L WITH ACUTE}", "H100"),
    )


def test_dimensions_reject_keys_that_collide_after_nfc() -> None:
    """NFC-equivalent provider keys cannot silently overwrite each other."""
    with pytest.raises(ValueError, match="duplicate dimension key"):
        NormalizedDimensions(
            [
                ("caf\N{LATIN SMALL LETTER E WITH ACUTE}", "one"),
                ("cafe\N{COMBINING ACUTE ACCENT}", "two"),
            ]
        )


@pytest.mark.parametrize(
    "pairs",
    [
        [("", "value")],
        [(cast("str", 1), "value")],
        [("key", cast("str", 1))],
    ],
)
def test_dimensions_reject_unusable_components(
    pairs: list[tuple[str, str]],
) -> None:
    """Dimension keys are non-empty strings and values remain strings."""
    with pytest.raises((TypeError, ValueError), match="dimension"):
        NormalizedDimensions(pairs)


def test_exact_slice_identity_contains_every_canonical_component() -> None:
    """Metadata cannot replace any component of an exact slice identity."""
    identity = EffectiveQuotaSliceIdentity(
        resource_scope=PROJECT,
        service="compute.googleapis.com",
        quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
        dimensions=NormalizedDimensions(
            [("region", "us-central1"), ("gpu_family", "NVIDIA_H100")]
        ),
        quota_scope=QuotaScope.REGIONAL,
    )

    assert identity.service == "compute.googleapis.com"
    assert identity.quota_id == "GPUS-PER-GPU-FAMILY-per-project-region"
    assert identity.dimensions.items == (
        ("gpu_family", "NVIDIA_H100"),
        ("region", "us-central1"),
    )
    assert (
        len(
            {
                identity,
                replace(
                    identity,
                    resource_scope=ResourceScope(
                        ResourceScopeKind.PROJECT,
                        "projects/987654321",
                    ),
                ),
                replace(identity, service="tpu.googleapis.com"),
                replace(identity, quota_id="UNKNOWN-provider-quota"),
                replace(
                    identity,
                    dimensions=NormalizedDimensions([("region", "us-east1")]),
                ),
                replace(identity, quota_scope=QuotaScope.GLOBAL),
            }
        )
        == IDENTITY_VARIANT_COUNT
    )


@pytest.mark.parametrize(
    "service",
    [
        "Compute.googleapis.com",
        " compute.googleapis.com",
        "compute",
        "-compute.googleapis.com",
    ],
)
def test_slice_identity_rejects_noncanonical_service_dns(service: str) -> None:
    """Service identity must already be a lowercase canonical DNS name."""
    with pytest.raises(ValueError, match="canonical service DNS"):
        EffectiveQuotaSliceIdentity(
            resource_scope=PROJECT,
            service=service,
            quota_id="provider-quota",
            dimensions=NormalizedDimensions(()),
            quota_scope=QuotaScope.GLOBAL,
        )


def test_constraint_reference_points_to_exact_slice() -> None:
    """A related constraint retains the complete independent slice identity."""
    identity = EffectiveQuotaSliceIdentity(
        resource_scope=PROJECT,
        service="compute.googleapis.com",
        quota_id="GPUS-ALL-REGIONS-per-project",
        dimensions=NormalizedDimensions(()),
        quota_scope=QuotaScope.GLOBAL,
    )

    assert ConstraintReference(slice_identity=identity).slice_identity is identity


def test_unknown_quota_scope_is_explicit() -> None:
    """Provider global resource paths never imply a product quota scope."""
    identity = EffectiveQuotaSliceIdentity(
        resource_scope=PROJECT,
        service="compute.googleapis.com",
        quota_id="future-provider-quota",
        dimensions=NormalizedDimensions(()),
        quota_scope=QuotaScope.UNKNOWN,
    )

    assert identity.quota_scope is QuotaScope.UNKNOWN


def test_slice_identity_rejects_untyped_or_empty_components() -> None:
    """Every exact identity component uses its canonical domain type."""
    dimensions = NormalizedDimensions(())
    with pytest.raises(TypeError):
        EffectiveQuotaSliceIdentity(
            resource_scope=cast("ResourceScope", "projects/123"),
            service="compute.googleapis.com",
            quota_id="provider-quota",
            dimensions=dimensions,
            quota_scope=QuotaScope.GLOBAL,
        )
    with pytest.raises(ValueError, match="canonical service DNS"):
        EffectiveQuotaSliceIdentity(
            resource_scope=PROJECT,
            service=cast("str", 123),
            quota_id="provider-quota",
            dimensions=dimensions,
            quota_scope=QuotaScope.GLOBAL,
        )
    with pytest.raises(TypeError):
        EffectiveQuotaSliceIdentity(
            resource_scope=PROJECT,
            service="compute.googleapis.com",
            quota_id=cast("str", 123),
            dimensions=dimensions,
            quota_scope=QuotaScope.GLOBAL,
        )
    with pytest.raises(ValueError, match="quota_id must not be empty"):
        EffectiveQuotaSliceIdentity(
            resource_scope=PROJECT,
            service="compute.googleapis.com",
            quota_id="",
            dimensions=dimensions,
            quota_scope=QuotaScope.GLOBAL,
        )
    with pytest.raises(TypeError):
        EffectiveQuotaSliceIdentity(
            resource_scope=PROJECT,
            service="compute.googleapis.com",
            quota_id="provider-quota",
            dimensions=cast("NormalizedDimensions", ()),
            quota_scope=QuotaScope.GLOBAL,
        )
    with pytest.raises(TypeError):
        EffectiveQuotaSliceIdentity(
            resource_scope=PROJECT,
            service="compute.googleapis.com",
            quota_id="provider-quota",
            dimensions=dimensions,
            quota_scope=cast("QuotaScope", "global"),
        )


def test_constraint_reference_rejects_incomplete_identity() -> None:
    """Constraint metadata cannot substitute for an exact slice identity."""
    with pytest.raises(TypeError, match="EffectiveQuotaSliceIdentity"):
        ConstraintReference(slice_identity=cast("EffectiveQuotaSliceIdentity", "quota"))
