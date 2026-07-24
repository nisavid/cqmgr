"""Canonical quota request plan encoding and authentication contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from cqmgr.adapters.serialization.plans import PlanCodec, PlanDecodeError
from cqmgr.domain.plans import (
    PLAN_LIFETIME,
    ContactBinding,
    EvidenceBinding,
    PlanIncapability,
    PlanKind,
    PlanLedgerState,
    PlanPrincipal,
    QuotaRequestBundlePlan,
    QuotaRequestPlan,
    QuotaRequestPlanChild,
    TargetStrategy,
    review_plan,
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

if TYPE_CHECKING:
    from collections.abc import Callable

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123456789")
SLICE = EffectiveQuotaSliceIdentity(
    resource_scope=SCOPE,
    service="compute.googleapis.com",
    quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
    dimensions=NormalizedDimensions(
        (("region", "us-central1"), ("gpu_family", "NVIDIA_H100"))
    ),
    quota_scope=QuotaScope.REGIONAL,
)
UNIT = QuotaUnit("1")
LOCAL_KEY = b"l" * 32
SHA256_A = "sha256:" + ("a" * 64)
SHA256_B = "sha256:" + ("b" * 64)
HMAC_SHA256_C = "hmac-sha256:" + ("c" * 64)


def _plan() -> QuotaRequestPlan:
    return QuotaRequestPlan(
        resource_scope=SCOPE,
        slice_identity=SLICE,
        target=QuotaQuantity(8, UNIT),
        effective=QuotaQuantity(4, UNIT),
        effective_observed_at=NOW,
        preference_name=(
            "projects/123456789/locations/global/quotaPreferences/h100-regional"
        ),
        preference_etag="etag-1",
        principal=PlanPrincipal(
            stable_identity="principal://accounts/123",
            impersonation_chain=("serviceAccount:operator@example.invalid",),
        ),
        contact_binding=ContactBinding(
            source=StableSymbol("selected-profile"),
            source_identity="profile:accelerators",
            value_digest=HMAC_SHA256_C,
        ),
        warnings=(StableSymbol("remaining-companion-bottleneck"),),
        required_acknowledgements=(StableSymbol("decrease-over-ten-percent"),),
        acknowledgements=(StableSymbol("decrease-over-ten-percent"),),
        constraints=(ConstraintReference(SLICE),),
        evidence=(
            EvidenceBinding(
                name=StableSymbol("eligibility"),
                value_digest=SHA256_A,
                observed_at=NOW,
            ),
            EvidenceBinding(
                name=StableSymbol("policy"),
                value_digest=SHA256_B,
                observed_at=NOW,
            ),
        ),
        installation_id="installation-123",
        issued_at=NOW,
        expires_at=NOW + PLAN_LIFETIME,
    )


def _bundle_child() -> QuotaRequestPlanChild:
    """Build one complete bundle child for invariant tests."""
    return QuotaRequestPlanChild(
        child_id="direct",
        slice_identity=SLICE,
        target=QuotaQuantity(8, UNIT),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(5, UNIT),
        prior_desired=None,
        granted=None,
        preference_name=None,
        preference_etag=None,
        target_strategy=TargetStrategy.MINIMUM,
        target_derivation=StableSymbol("usage-plus-workload"),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        warnings=(),
        required_acknowledgements=(),
        acknowledgements=(),
        evidence=(),
    )


def _bundle_plan(
    children: tuple[QuotaRequestPlanChild, ...] | None = None,
) -> QuotaRequestBundlePlan:
    """Build one complete canonical bundle plan."""
    return QuotaRequestBundlePlan(
        resource_scope=SCOPE,
        kind=PlanKind.BUNDLE,
        selected_location="us-central1",
        target_strategy=TargetStrategy.MINIMUM,
        normalized_workload="compute-instance:n1-standard-8:1",
        children=children or (_bundle_child(),),
        constraints=(ConstraintReference(SLICE),),
        principal=_plan().principal,
        contact_binding=_plan().contact_binding,
        installation_id="installation-123",
        issued_at=NOW,
        expires_at=NOW + PLAN_LIFETIME,
    )


def test_plan_encoding_is_canonical_stable_and_authenticated() -> None:
    """One semantic plan has one stable byte encoding and digest handle."""
    encoded = PlanCodec.encode(_plan(), LOCAL_KEY)

    assert encoded.bytes == PlanCodec.encode(_plan(), LOCAL_KEY).bytes
    assert encoded.digest.startswith("sha256:")
    assert encoded.bytes.endswith(b"\n")
    assert b"operator@example.invalid" in encoded.bytes
    assert HMAC_SHA256_C.encode() in encoded.bytes
    assert b"quota-contact" not in encoded.bytes

    decoded = PlanCodec.decode(encoded.bytes)
    assert decoded.plan == _plan()
    assert decoded.digest == encoded.digest
    assert decoded.authenticate(LOCAL_KEY)
    assert not decoded.authenticate(b"f" * 32)


def test_bundle_plan_encoding_binds_kind_order_and_child_derivations() -> None:
    """Portable bundle bytes retain every ordered independently mutable child."""
    child = QuotaRequestPlanChild(
        child_id="direct",
        slice_identity=SLICE,
        target=QuotaQuantity(8, UNIT),
        effective=QuotaQuantity(4, UNIT),
        usage=QuotaQuantity(3, UNIT),
        workload=QuotaQuantity(5, UNIT),
        prior_desired=None,
        granted=None,
        preference_name=None,
        preference_etag=None,
        target_strategy=TargetStrategy.MINIMUM,
        target_derivation=StableSymbol("usage-plus-workload"),
        direct_accelerator_rank=0,
        scope_breadth_rank=1,
        warnings=(),
        required_acknowledgements=(),
        acknowledgements=(),
        evidence=(),
    )
    plan = QuotaRequestBundlePlan(
        resource_scope=SCOPE,
        kind=PlanKind.BUNDLE,
        selected_location="us-central1",
        target_strategy=TargetStrategy.MINIMUM,
        normalized_workload="compute-instance:n1-standard-8:1",
        children=(child,),
        constraints=(ConstraintReference(SLICE),),
        principal=_plan().principal,
        contact_binding=_plan().contact_binding,
        installation_id="installation-123",
        issued_at=NOW,
        expires_at=NOW + PLAN_LIFETIME,
    )

    encoded = PlanCodec.encode(plan, LOCAL_KEY)
    decoded = PlanCodec.decode(encoded.bytes)

    assert decoded.plan == plan
    assert decoded.plan.kind is PlanKind.BUNDLE
    assert decoded.plan.children == (child,)
    assert b'"kind":"bundle"' in encoded.bytes


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        (lambda: replace(_bundle_child(), child_id=""), "child_id"),
        (
            lambda: replace(
                _bundle_child(),
                slice_identity=cast("EffectiveQuotaSliceIdentity", "slice"),
            ),
            "slice identity",
        ),
        (
            lambda: replace(
                _bundle_child(),
                target=cast("QuotaQuantity", 8),
            ),
            "quantities",
        ),
        (
            lambda: replace(
                _bundle_child(),
                usage=QuotaQuantity(3, QuotaUnit("GiBy")),
            ),
            "native unit",
        ),
        (lambda: replace(_bundle_child(), preference_name=""), "preference_name"),
        (
            lambda: replace(
                _bundle_child(),
                target_strategy=cast("TargetStrategy", "minimum"),
            ),
            "target_strategy",
        ),
        (
            lambda: replace(
                _bundle_child(),
                target_derivation=cast("StableSymbol", "formula"),
            ),
            "target_derivation",
        ),
        (
            lambda: replace(_bundle_child(), direct_accelerator_rank=2),
            "direct accelerator",
        ),
        (
            lambda: replace(_bundle_child(), scope_breadth_rank=9),
            "scope breadth",
        ),
        (
            lambda: replace(
                _bundle_child(),
                warnings=cast("tuple[StableSymbol, ...]", ["warning"]),
            ),
            "warnings",
        ),
        (
            lambda: replace(
                _bundle_child(),
                acknowledgements=(StableSymbol("unbound"),),
            ),
            "required",
        ),
        (
            lambda: replace(
                _bundle_child(),
                evidence=(
                    EvidenceBinding(StableSymbol("effective"), SHA256_A, NOW),
                    EvidenceBinding(StableSymbol("effective"), SHA256_B, NOW),
                ),
            ),
            "unique",
        ),
    ],
)
def test_bundle_child_rejects_unreviewable_bindings(
    invalid: Callable[[], object],
    message: str,
) -> None:
    """Every child field remains exact, typed, and internally coherent."""
    with pytest.raises((TypeError, ValueError), match=message):
        invalid()


def test_bundle_plan_rejects_inconsistent_subject_shapes() -> None:
    """A bundle cannot weaken its kind, order, scope, or trust bindings."""
    plan = _bundle_plan()
    other_scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/987654321")
    other_slice = replace(SLICE, resource_scope=other_scope)
    later = replace(
        _bundle_child(),
        child_id="companion",
        direct_accelerator_rank=1,
        scope_breadth_rank=3,
    )
    invalid_cases: tuple[tuple[Callable[[], object], str], ...] = (
        (
            lambda: replace(
                plan,
                resource_scope=cast("ResourceScope", "project"),
            ),
            "resource_scope",
        ),
        (lambda: replace(plan, kind=PlanKind.SINGLE), "kind"),
        (lambda: replace(plan, selected_location=""), "selected_location"),
        (
            lambda: replace(
                plan,
                target_strategy=cast("TargetStrategy", "minimum"),
            ),
            "target_strategy",
        ),
        (lambda: replace(plan, normalized_workload=""), "normalized_workload"),
        (lambda: replace(plan, children=()), "non-empty"),
        (
            lambda: replace(
                plan,
                children=(_bundle_child(), _bundle_child()),
            ),
            "IDs must be unique",
        ),
        (
            lambda: replace(plan, children=(later, _bundle_child())),
            "deterministic",
        ),
        (
            lambda: replace(
                plan,
                children=(replace(_bundle_child(), slice_identity=other_slice),),
            ),
            "resource scope",
        ),
        (
            lambda: replace(
                plan,
                constraints=cast(
                    "tuple[ConstraintReference, ...]",
                    ["slice"],
                ),
            ),
            "constraints",
        ),
        (
            lambda: replace(
                plan,
                principal=cast("PlanPrincipal", "principal"),
            ),
            "principal",
        ),
        (
            lambda: replace(
                plan,
                contact_binding=cast("ContactBinding", "contact"),
            ),
            "contact_binding",
        ),
        (lambda: replace(plan, installation_id=""), "installation_id"),
        (
            lambda: replace(plan, issued_at=NOW.replace(tzinfo=None)),
            "aware UTC",
        ),
        (
            lambda: replace(plan, expires_at=NOW + timedelta(minutes=16)),
            "15 minutes",
        ),
    )
    for invalid, message in invalid_cases:
        with pytest.raises((TypeError, ValueError), match=message):
            invalid()


def test_plan_decode_rejects_noncanonical_tampered_and_newer_schema_bytes() -> None:
    """Untrusted plan files fail before their contents become reviewable."""
    encoded = PlanCodec.encode(_plan(), LOCAL_KEY)

    with pytest.raises(PlanDecodeError, match="canonical"):
        PlanCodec.decode(encoded.bytes.replace(b'":"', b'": "', 1))
    with pytest.raises(PlanDecodeError, match="digest"):
        PlanCodec.decode(encoded.bytes.replace(b'"8"', b'"9"', 1))
    with pytest.raises(PlanDecodeError, match="schema"):
        PlanCodec.decode(
            encoded.bytes.replace(
                b"cqmgr.quota-request-plan/v1",
                b"cqmgr.quota-request-plan/v2",
            )
        )


def test_plan_lifetime_is_exactly_fifteen_minutes() -> None:
    """Plan construction cannot weaken or extend the fixed expiry window."""
    plan = _plan()
    assert plan.expires_at - plan.issued_at == timedelta(minutes=15)

    with pytest.raises(ValueError, match="15 minutes"):
        replace(plan, expires_at=plan.issued_at + timedelta(minutes=16))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _mutated_plan_bytes(
    path: tuple[str | int, ...],
    value: object,
    *,
    plan: QuotaRequestPlan | QuotaRequestBundlePlan | None = None,
) -> bytes:
    envelope = json.loads(PlanCodec.encode(plan or _plan(), LOCAL_KEY).bytes)
    target = envelope["plan"]
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    content = _canonical(envelope["plan"])
    envelope["digest"] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return _canonical(envelope) + b"\n"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("issued_at",), "2026-07-21T12:00:00+00:00"),
        (("target", "value"), "08"),
        (("slice", "dimensions"), []),
        (("preference",), []),
        (("principal", "impersonation_chain"), [7]),
        (("warnings",), {}),
        (("warnings",), [7]),
        (("preference", "name"), 7),
        (("resource_scope", "name"), 7),
        (("constraints",), {}),
        (("principal", "stable_identity"), 7),
    ],
)
def test_digest_valid_malformed_content_still_fails_closed(
    path: tuple[str, ...], value: object
) -> None:
    """Authentication syntax cannot make structurally invalid content reviewable."""
    with pytest.raises(PlanDecodeError, match="content is invalid"):
        PlanCodec.decode(_mutated_plan_bytes(path, value))


def test_digest_valid_semantically_noncanonical_plan_fails_closed() -> None:
    """Canonical JSON cannot hide a noncanonical semantic timestamp spelling."""
    with pytest.raises(PlanDecodeError, match="semantically canonical"):
        PlanCodec.decode(
            _mutated_plan_bytes(("issued_at",), "2026-07-21T12:00:00.000000Z")
        )


def test_codec_rejects_invalid_boundary_types_and_envelope_shapes() -> None:
    """The public codec never guesses across malformed untrusted boundaries."""
    with pytest.raises(TypeError, match="QuotaRequestPlan"):
        PlanCodec.encode(cast("QuotaRequestPlan", None), LOCAL_KEY)
    with pytest.raises(ValueError, match="32 bytes"):
        PlanCodec.encode(_plan(), b"short")
    with pytest.raises(TypeError, match="bytes"):
        PlanCodec.decode(cast("bytes", "not-bytes"))
    with pytest.raises(PlanDecodeError, match="canonical JSON"):
        PlanCodec.decode(b"\xff")
    with pytest.raises(PlanDecodeError, match="canonical JSON"):
        PlanCodec.decode(b'{"authentication":"a","authentication":"b"}\n')
    with pytest.raises(PlanDecodeError, match="unsupported fields"):
        PlanCodec.decode(b"[]\n")
    with pytest.raises(PlanDecodeError, match="canonically encoded"):
        PlanCodec.decode(
            b'{"authentication":"a","digest":"sha256:x",'
            b'"plan":{"schema":"cqmgr.quota-request-plan/v1","value":NaN}}\n'
        )


def test_codec_rejects_non_string_controls_and_unknown_authentication_algorithm() -> (
    None
):
    """Digest and authenticator controls have one closed wire shape."""
    envelope = json.loads(PlanCodec.encode(_plan(), LOCAL_KEY).bytes)
    envelope["digest"] = 7
    with pytest.raises(PlanDecodeError, match="must be strings"):
        PlanCodec.decode(_canonical(envelope) + b"\n")

    envelope = json.loads(PlanCodec.encode(_plan(), LOCAL_KEY).bytes)
    envelope["authentication"] = "sha256:not-keyed"
    with pytest.raises(PlanDecodeError, match="algorithm"):
        PlanCodec.decode(_canonical(envelope) + b"\n")


def test_plan_value_types_reject_invalid_and_secret_bearing_shapes() -> None:
    """Every plan binding is explicit, typed, and non-secret before serialization."""
    with pytest.raises(ValueError, match="stable_identity"):
        PlanPrincipal("")
    with pytest.raises(TypeError, match="impersonation_chain"):
        PlanPrincipal("principal", cast("tuple[str, ...]", ["delegate"]))
    with pytest.raises(TypeError, match="contact source"):
        ContactBinding(
            cast("StableSymbol", "profile"), "profile:name", "hmac-sha256:value"
        )
    with pytest.raises(ValueError, match="source_identity"):
        ContactBinding(StableSymbol("selected-profile"), "", HMAC_SHA256_C)
    with pytest.raises(ValueError, match="source"):
        ContactBinding(
            StableSymbol("unknown-source"),
            "profile:accelerators",
            HMAC_SHA256_C,
        )
    with pytest.raises(ValueError, match="source_identity"):
        ContactBinding(
            StableSymbol("per-operation-input"),
            "person@example.test",
            HMAC_SHA256_C,
        )
    with pytest.raises(ValueError, match="source_identity"):
        ContactBinding(
            StableSymbol("direct-user"),
            "principal://accounts/" + ("a" * 256),
            HMAC_SHA256_C,
        )
    with pytest.raises(ValueError, match="value_digest"):
        ContactBinding(
            StableSymbol("selected-profile"),
            "profile:name",
            "raw@example.invalid",
        )
    with pytest.raises(ValueError, match="value_digest"):
        ContactBinding(
            StableSymbol("selected-profile"),
            "profile:name",
            "hmac-sha256:" + ("A" * 64),
        )
    with pytest.raises(TypeError, match="evidence name"):
        EvidenceBinding(cast("StableSymbol", "policy"), "sha256:value", NOW)
    with pytest.raises(ValueError, match="value_digest"):
        EvidenceBinding(StableSymbol("policy"), "value", NOW)


@pytest.mark.parametrize(
    ("source", "source_identity"),
    [
        ("named-profile", "profile:accelerators"),
        ("selected-profile", "profile:accelerators"),
        ("direct-user", "principal://accounts/123"),
        ("per-operation-input", "input:hmac-sha256:" + ("d" * 64)),
    ],
)
def test_contact_binding_accepts_only_bounded_non_secret_source_identities(
    source: str,
    source_identity: str,
) -> None:
    """Canonical contact sources retain identity without retaining the contact."""
    binding = ContactBinding(
        StableSymbol(source),
        source_identity,
        HMAC_SHA256_C,
    )

    assert binding.source_identity == source_identity
    assert "@" not in binding.source_identity


def test_plan_rejects_cross_scope_unit_and_binding_inconsistency() -> None:
    """A plan cannot be constructed with a weakened or internally split intent."""
    plan = _plan()
    other_scope = ResourceScope(ResourceScopeKind.PROJECT, "projects/987654321")
    other_slice = EffectiveQuotaSliceIdentity(
        resource_scope=other_scope,
        service=plan.slice_identity.service,
        quota_id=plan.slice_identity.quota_id,
        dimensions=plan.slice_identity.dimensions,
        quota_scope=plan.slice_identity.quota_scope,
    )
    with pytest.raises(TypeError, match="resource_scope"):
        replace(plan, resource_scope=cast("ResourceScope", "projects/123"))
    with pytest.raises(TypeError, match="slice_identity"):
        replace(plan, slice_identity=cast("EffectiveQuotaSliceIdentity", "slice"))
    with pytest.raises(ValueError, match="plan resource scope"):
        replace(plan, slice_identity=other_slice)
    with pytest.raises(ValueError, match="constraint resource scope"):
        replace(plan, constraints=(ConstraintReference(other_slice),))
    with pytest.raises(ValueError, match="constraints must be unique"):
        replace(plan, constraints=(plan.constraints[0], plan.constraints[0]))
    with pytest.raises(TypeError, match="target and effective"):
        replace(plan, target=cast("QuotaQuantity", 8))
    with pytest.raises(ValueError, match="same unit"):
        replace(plan, effective=QuotaQuantity(4, QuotaUnit("GiBy")))
    with pytest.raises(ValueError, match="aware UTC"):
        replace(plan, effective_observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="preference_name"):
        replace(plan, preference_name="")
    with pytest.raises(ValueError, match="preference_etag"):
        replace(plan, preference_etag=cast("str", 7))
    with pytest.raises(TypeError, match="principal"):
        replace(plan, principal=cast("PlanPrincipal", "principal"))
    with pytest.raises(TypeError, match="contact_binding"):
        replace(plan, contact_binding=cast("ContactBinding", "contact"))
    with pytest.raises(TypeError, match="warnings"):
        replace(plan, warnings=cast("tuple[StableSymbol, ...]", [StableSymbol("x")]))
    with pytest.raises(TypeError, match="constraints"):
        replace(plan, constraints=(cast("ConstraintReference", "slice"),))
    with pytest.raises(TypeError, match="evidence"):
        replace(plan, evidence=(cast("EvidenceBinding", "evidence"),))
    with pytest.raises(ValueError, match="unique"):
        replace(plan, evidence=(plan.evidence[0], plan.evidence[0]))
    with pytest.raises(ValueError, match="required by"):
        replace(plan, acknowledgements=(StableSymbol("unbound"),))
    with pytest.raises(ValueError, match="installation_id"):
        replace(plan, installation_id="")
    with pytest.raises(ValueError, match="aware UTC"):
        replace(plan, issued_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        (
            lambda: replace(
                _plan(),
                target_strategy=TargetStrategy.MINIMUM,
            ),
            "manual target strategy",
        ),
        (
            lambda: replace(
                _plan(),
                target_derivation=cast("StableSymbol", "manual-absolute"),
            ),
            "target_derivation",
        ),
        (lambda: replace(_plan(), child_id=""), "child_id"),
        (
            lambda: replace(
                _plan(),
                usage=QuotaQuantity(3, QuotaUnit("GiBy")),
            ),
            "target unit",
        ),
        (
            lambda: replace(
                _plan(),
                workload=cast("QuotaQuantity | None", "4"),
            ),
            "target unit",
        ),
        (
            lambda: replace(
                _plan(),
                prior_desired=QuotaQuantity(8, QuotaUnit("GiBy")),
            ),
            "target unit",
        ),
        (
            lambda: replace(
                _plan(),
                granted=cast("QuotaQuantity | None", object()),
            ),
            "target unit",
        ),
        (
            lambda: replace(_plan(), direct_accelerator_rank=2),
            "direct accelerator rank",
        ),
        (
            lambda: replace(_plan(), scope_breadth_rank=4),
            "scope breadth rank",
        ),
    ],
)
def test_single_plan_rejects_invalid_child_dispatch_bindings(
    invalid: Callable[[], object],
    message: str,
) -> None:
    """A single plan retains the same exact child dispatch contract as a bundle."""
    with pytest.raises((TypeError, ValueError), match=message):
        invalid()


@pytest.mark.parametrize(
    ("plan", "path", "value"),
    [
        (_plan(), ("kind",), "collection"),
        (_plan(), ("children",), []),
        (_bundle_plan(), ("children", 0, "direct_accelerator_rank"), True),
    ],
)
def test_decode_rejects_invalid_single_shape_and_dispatch_rank(
    plan: QuotaRequestPlan | QuotaRequestBundlePlan,
    path: tuple[str | int, ...],
    value: object,
) -> None:
    """Authenticated bytes cannot weaken plan kind, arity, or dispatch ordering."""
    with pytest.raises(PlanDecodeError, match="content is invalid"):
        PlanCodec.decode(_mutated_plan_bytes(path, value, plan=plan))


def test_review_validation_and_every_ledger_state_fail_closed() -> None:
    """Applicability reasons are complete and independent of safe inspection."""
    plan = replace(
        _plan(),
        acknowledgements=(),
        required_acknowledgements=(StableSymbol("unlimited-transition"),),
    )
    common = {
        "digest": SHA256_A,
        "authenticated": True,
        "local_installation_id": plan.installation_id,
        "now": NOW,
    }
    for state, reason in (
        (PlanLedgerState.LEASED, PlanIncapability.LEASED),
        (PlanLedgerState.DISPATCHED, PlanIncapability.LEASED),
        (PlanLedgerState.CONSUMED, PlanIncapability.CONSUMED),
        (PlanLedgerState.QUARANTINED, PlanIncapability.QUARANTINED),
        (PlanLedgerState.INVALIDATED, PlanIncapability.INVALIDATED),
    ):
        review = review_plan(plan, state=state, **common)
        assert PlanIncapability.UNACKNOWLEDGED in review.incapability_reasons
        assert reason in review.incapability_reasons

    with pytest.raises(TypeError, match="QuotaRequestPlan"):
        review_plan(
            cast("QuotaRequestPlan", None), state=PlanLedgerState.AVAILABLE, **common
        )
    with pytest.raises(ValueError, match="digest"):
        review_plan(
            plan, state=PlanLedgerState.AVAILABLE, **{**common, "digest": "bad"}
        )
    with pytest.raises(ValueError, match="digest"):
        review_plan(
            plan,
            state=PlanLedgerState.AVAILABLE,
            **{**common, "digest": cast("str", 7)},
        )
    with pytest.raises(TypeError, match="authenticated"):
        review_plan(
            plan,
            state=PlanLedgerState.AVAILABLE,
            **{**common, "authenticated": cast("bool", 1)},
        )
    with pytest.raises(ValueError, match="local_installation_id"):
        review_plan(
            plan,
            state=PlanLedgerState.AVAILABLE,
            **{**common, "local_installation_id": ""},
        )
    with pytest.raises(TypeError, match="PlanLedgerState"):
        review_plan(plan, state=cast("PlanLedgerState", "available"), **common)
