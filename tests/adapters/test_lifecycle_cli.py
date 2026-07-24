"""CLI lifecycle parsing, presentation, and Copy CLI contracts."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from cqmgr.adapters.cli.copy_cli import (
    CopyCliPresentation,
    WatchCopyCliPresentation,
    plan_apply_copy_cli,
    plan_review_copy_cli,
    request_exact_copy_cli,
    request_watch_copy_cli,
    request_workload_copy_cli,
)
from cqmgr.adapters.cli.lifecycle import (
    LifecyclePresentation,
    PlanReferenceInput,
    RequestCompositionInput,
    WatchCliInput,
    WatchPresentation,
    emit_composition,
    emit_lifecycle_result,
    emit_watch_event,
    read_quota_contact,
)
from cqmgr.application.operations.plans import ComposeRequest, Composition
from cqmgr.application.operations.read_only import (
    QuotaInspectSelector,
    ReadOnlyScopeInput,
)
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.accelerator_overlay import (
    AllCompatibleLocations,
    CandidateLocations,
    CloudTpuSliceRequirement,
    ComputeInstanceRequirement,
    ProvisioningModel,
)
from cqmgr.domain.apply_records import ApplyChildDisposition
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.plans import PlanKind, TargetStrategy
from cqmgr.domain.quotas import (
    EffectiveQuotaSliceIdentity,
    NormalizedDimensions,
    QuotaQuantity,
    QuotaScope,
    QuotaUnit,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import (
    Completeness,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)
from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind
from cqmgr.domain.status import WatchCondition, WatchDisposition
from cqmgr.domain.watch import (
    WatchAggregate,
    WatchChildIdentity,
    WatchChildSummary,
    WatchEventKind,
    WatchStreamEvent,
    WatchSubject,
)

SCOPE = ResourceScope(ResourceScopeKind.PROJECT, "projects/123")
NOW = datetime(2026, 7, 24, 11, tzinfo=UTC)
EXPECTED_MANUAL_TARGET_COUNT = 2
REJECTED_COMPOSITION_EXIT = 3


def _exact_input() -> RequestCompositionInput:
    return RequestCompositionInput(
        scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        selector=QuotaInspectSelector(
            "compute.googleapis.com",
            "GPU-DIRECT",
            "us-central1",
            NormalizedDimensions((("region", "us-central1"),)),
        ),
        workload=None,
        target_strategy=TargetStrategy.MANUAL,
        targets=((None, "8"),),
    )


def _workload_input() -> RequestCompositionInput:
    return RequestCompositionInput(
        scope_input=ReadOnlyScopeInput(explicit_resource_scope=SCOPE),
        selector=None,
        workload=ComputeInstanceRequirement(
            machine_type="a3-highgpu-8g",
            instance_count=2,
            provisioning_model=ProvisioningModel.SPOT,
            locations=CandidateLocations(("us-central1-a",)),
            attached_accelerator_type="nvidia-h100-80gb",
            attached_accelerator_count=8,
        ),
        target_strategy=TargetStrategy.MINIMUM,
        targets=(),
    )


def test_exact_preview_copy_cli_retains_safe_inputs_and_contact_stdin() -> None:
    """A copied Preview is canonical and never places the contact value in argv."""
    command = request_exact_copy_cli(
        "preview",
        SCOPE,
        service="compute.googleapis.com",
        quota_id="GPUS-PER-GPU-FAMILY-per-project-region",
        location="us-central1",
        dimensions=NormalizedDimensions(
            (("gpu_family", "NVIDIA_H100"), ("region", "us-central1"))
        ),
        target="8",
        acknowledgements=("decrease-over-ten-percent",),
        quota_contact_stdin=True,
        plan_out=Path("request.plan"),
        presentation=CopyCliPresentation(output="json", no_color=True, quiet=True),
    )
    arguments = shlex.split(command)

    assert arguments[:3] == ["cqmgr", "request", "preview"]
    assert arguments.count("--quota-contact-stdin") == 1
    assert "operator@example.com" not in command
    assert arguments[-6:] == [
        "--plan-out",
        "request.plan",
        "--output",
        "json",
        "--no-color",
        "--quiet",
    ]
    assert "--expert" not in arguments


def test_plan_copy_cli_preserves_reference_and_incomplete_apply_acknowledgement() -> (
    None
):
    """Review round-trips a plan while Apply keeps operator acknowledgement blank."""
    review = shlex.split(
        plan_review_copy_cli(
            digest="sha256:" + ("a" * 64),
            presentation=CopyCliPresentation(output="json"),
        )
    )
    apply = shlex.split(
        plan_apply_copy_cli(
            path=Path("request.plan"),
            presentation=CopyCliPresentation(no_color=True),
        )
    )

    assert review[:3] == ["cqmgr", "plan", "review"]
    assert review[3:] == ["--plan", "sha256:" + ("a" * 64), "--output", "json"]
    assert apply[:3] == ["cqmgr", "plan", "apply"]
    acknowledgement = apply.index("--acknowledge-resource-scope")
    assert apply[acknowledgement + 1] == "<RESOURCE_SCOPE>"
    assert "projects/123" not in apply


def test_watch_copy_cli_keeps_initial_and_resume_forms_disjoint() -> None:
    """Copied Watch commands retain one selector, a deadline, and stream output."""
    initial = shlex.split(
        request_watch_copy_cli(
            intent_id="sha256:" + ("b" * 64),
            condition=WatchCondition.FULFILLED,
            deadline="2026-07-25T00:00:00Z",
            presentation=WatchCopyCliPresentation(output="jsonl", quiet=True),
        )
    )
    resumed = shlex.split(
        request_watch_copy_cli(
            resume="cqmgr.watch-resume/v1:opaque",
            deadline="2026-07-25T01:00:00Z",
        )
    )

    assert initial[:3] == ["cqmgr", "request", "watch"]
    assert "--intent-id" in initial
    assert "--condition" in initial
    assert initial[-3:] == ["--output", "jsonl", "--quiet"]
    assert "--resume" not in initial
    assert "--resume" in resumed
    assert "--intent-id" not in resumed
    assert "--condition" not in resumed


@pytest.mark.parametrize(
    "deadline",
    [
        "",
        "2026-07-25 00:00:00+00:00",
        "2026-07-25T00:00:00",
        "2026-07-25T00:00:00+0000",
        "2026-02-30T00:00:00Z",
    ],
)
def test_watch_copy_cli_rejects_non_rfc3339_deadlines(deadline: str) -> None:
    """Copy CLI accepts the same strict absolute timestamp grammar as execution."""
    with pytest.raises(ValueError, match="RFC 3339"):
        request_watch_copy_cli(deadline=deadline, resume="resume")


def test_watch_copy_cli_normalizes_absolute_deadline_to_utc() -> None:
    """Copied Watch commands use one canonical UTC RFC 3339 timestamp."""
    command = request_watch_copy_cli(
        deadline="2026-07-25T01:30:00+01:30",
        resume="resume",
    )

    arguments = shlex.split(command)
    deadline_index = arguments.index("--deadline")
    assert arguments[deadline_index + 1] == "2026-07-25T00:00:00Z"


def test_workload_preview_copy_cli_retains_shape_strategy_and_manual_targets() -> None:
    """A copied bundle Preview keeps its complete typed workload request."""
    command = request_workload_copy_cli(
        "preview",
        SCOPE,
        ComputeInstanceRequirement(
            machine_type="a3-highgpu-8g",
            instance_count=2,
            provisioning_model=ProvisioningModel.SPOT,
            locations=CandidateLocations(("us-central1-a",)),
            attached_accelerator_type="nvidia-h100-80gb",
            attached_accelerator_count=8,
        ),
        target_strategy=TargetStrategy.MANUAL,
        targets=(("accelerator", "16"), ("all-regions", "32")),
        quota_contact_stdin=True,
        presentation=CopyCliPresentation(output="json"),
    )
    arguments = shlex.split(command)

    assert arguments[:3] == ["cqmgr", "request", "preview"]
    assert arguments.count("--target") == EXPECTED_MANUAL_TARGET_COUNT
    assert "accelerator=16" in arguments
    assert "all-regions=32" in arguments
    assert "--quota-contact-stdin" in arguments
    assert "--expert" not in arguments


def test_copy_cli_presentations_reject_cross_wired_controls() -> None:
    """Copy CLI one-shot and stream controls retain disjoint vocabularies."""
    with pytest.raises(ValueError, match="human or json"):
        CopyCliPresentation(output="jsonl")
    with pytest.raises(TypeError, match="boolean"):
        CopyCliPresentation(no_color="false")  # type: ignore[bad-argument-type]
    with pytest.raises(ValueError, match="human or jsonl"):
        WatchCopyCliPresentation(output="json")
    with pytest.raises(TypeError, match="boolean"):
        WatchCopyCliPresentation(quiet="false")  # type: ignore[bad-argument-type]


def test_exact_copy_cli_rejects_invalid_public_inputs() -> None:
    """Exact Copy CLI fails closed before rendering malformed or protected inputs."""
    dimensions = NormalizedDimensions(())
    with pytest.raises(ValueError, match="compose or preview"):
        request_exact_copy_cli(
            "apply",
            SCOPE,
            service="compute.googleapis.com",
            quota_id="GPU-DIRECT",
            location="us-central1",
            dimensions=dimensions,
            target="8",
        )
    with pytest.raises(ValueError, match="non-empty"):
        request_exact_copy_cli(
            "compose",
            SCOPE,
            service="",
            quota_id="GPU-DIRECT",
            location="us-central1",
            dimensions=dimensions,
            target="8",
        )
    with pytest.raises(TypeError, match="NormalizedDimensions"):
        request_exact_copy_cli(
            "compose",
            SCOPE,
            service="compute.googleapis.com",
            quota_id="GPU-DIRECT",
            location="us-central1",
            dimensions=object(),  # type: ignore[bad-argument-type]
            target="8",
        )
    with pytest.raises(ValueError, match="acknowledgements"):
        request_exact_copy_cli(
            "compose",
            SCOPE,
            service="compute.googleapis.com",
            quota_id="GPU-DIRECT",
            location="us-central1",
            dimensions=dimensions,
            target="8",
            acknowledgements=("",),
        )
    with pytest.raises(TypeError, match="contact mode"):
        request_exact_copy_cli(
            "compose",
            SCOPE,
            service="compute.googleapis.com",
            quota_id="GPU-DIRECT",
            location="us-central1",
            dimensions=dimensions,
            target="8",
            quota_contact_stdin="true",  # type: ignore[bad-argument-type]
        )
    with pytest.raises(ValueError, match="only for Preview"):
        request_exact_copy_cli(
            "compose",
            SCOPE,
            service="compute.googleapis.com",
            quota_id="GPU-DIRECT",
            location="us-central1",
            dimensions=dimensions,
            target="8",
            plan_out=Path("request.plan"),
        )


def test_workload_copy_cli_rejects_cross_wired_target_controls() -> None:
    """Workload Copy CLI keeps typed requirements and target strategies coherent."""
    requirement = _workload_input().workload
    assert isinstance(requirement, ComputeInstanceRequirement)
    with pytest.raises(ValueError, match="compose or preview"):
        request_workload_copy_cli(
            "apply",
            SCOPE,
            requirement,
            target_strategy=TargetStrategy.MINIMUM,
        )
    with pytest.raises(TypeError, match="typed requirement"):
        request_workload_copy_cli(
            "compose",
            SCOPE,
            object(),  # type: ignore[bad-argument-type]
            target_strategy=TargetStrategy.MINIMUM,
        )
    with pytest.raises(TypeError, match="TargetStrategy"):
        request_workload_copy_cli(
            "compose",
            SCOPE,
            requirement,
            target_strategy="minimum",  # type: ignore[bad-argument-type]
        )
    with pytest.raises(ValueError, match="unique"):
        request_workload_copy_cli(
            "compose",
            SCOPE,
            requirement,
            target_strategy=TargetStrategy.MANUAL,
            targets=(("child", "8"), ("child", "16")),
        )
    with pytest.raises(ValueError, match="requires child targets"):
        request_workload_copy_cli(
            "compose",
            SCOPE,
            requirement,
            target_strategy=TargetStrategy.MANUAL,
        )
    with pytest.raises(ValueError, match="do not accept"):
        request_workload_copy_cli(
            "compose",
            SCOPE,
            requirement,
            target_strategy=TargetStrategy.MINIMUM,
            targets=(("child", "8"),),
        )
    with pytest.raises(TypeError, match="contact mode"):
        request_workload_copy_cli(
            "compose",
            SCOPE,
            requirement,
            target_strategy=TargetStrategy.MINIMUM,
            quota_contact_stdin="true",  # type: ignore[bad-argument-type]
        )
    with pytest.raises(ValueError, match="only for Preview"):
        request_workload_copy_cli(
            "compose",
            SCOPE,
            requirement,
            target_strategy=TargetStrategy.MINIMUM,
            plan_out=Path("request.plan"),
        )


def test_workload_copy_cli_renders_cloud_tpu_and_all_compatible_forms() -> None:
    """Copy CLI retains the other typed workload and location branches."""
    command = request_workload_copy_cli(
        "preview",
        SCOPE,
        CloudTpuSliceRequirement(
            accelerator_type="v5p",
            topology="2x2x2",
            runtime_version="tpu-vm-v4-base",
            slice_count=2,
            provisioning_model=ProvisioningModel.SPOT,
            locations=AllCompatibleLocations(),
        ),
        target_strategy=TargetStrategy.MINIMUM,
        plan_out=Path("request.plan"),
    )

    arguments = shlex.split(command)
    assert "--accelerator-type" in arguments
    assert "--slice-count" in arguments
    assert "--all-compatible-locations" in arguments
    assert arguments[-2:] == ["--output", "human"]


def test_plan_and_watch_copy_cli_reject_cross_wired_controls() -> None:
    """Plan and Watch copies never guess references, selectors, or acknowledgements."""
    with pytest.raises(ValueError, match="exactly one"):
        plan_review_copy_cli()
    with pytest.raises(ValueError, match="digest"):
        plan_review_copy_cli(digest="")
    with pytest.raises(TypeError, match="path"):
        plan_review_copy_cli(path="request.plan")  # type: ignore[bad-argument-type]
    with pytest.raises(ValueError, match="acknowledgement"):
        plan_apply_copy_cli(
            path=Path("request.plan"),
            acknowledge_resource_scope="",
        )
    with pytest.raises(ValueError, match="deadline"):
        request_watch_copy_cli(deadline="", resume="resume")
    with pytest.raises(ValueError, match="exactly one"):
        request_watch_copy_cli(deadline="2026-07-25T00:00:00Z")
    with pytest.raises(ValueError, match="intent ID and condition"):
        request_watch_copy_cli(
            deadline="2026-07-25T00:00:00Z",
            intent_id="intent",
        )
    with pytest.raises(ValueError, match="recovers"):
        request_watch_copy_cli(
            deadline="2026-07-25T00:00:00Z",
            resume="resume",
            condition=WatchCondition.GRANTED,
        )
    with pytest.raises(TypeError, match="Watch presentation"):
        request_watch_copy_cli(
            deadline="2026-07-25T00:00:00Z",
            resume="resume",
            presentation=object(),  # type: ignore[bad-argument-type]
        )


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"\n",
        b"operator@example.com\nextra",
        b"operator@example.com\x00\n",
        b"\xff\n",
    ],
)
def test_quota_contact_stdin_rejects_non_exact_lines(value: bytes) -> None:
    """Protected contact input accepts one nonempty UTF-8 line and nothing else."""
    with pytest.raises(ValueError, match="quota contact"):
        read_quota_contact(BytesIO(value))


def test_quota_contact_stdin_returns_only_a_redacted_secret() -> None:
    """The adapter does not retain a printable contact value."""
    value = read_quota_contact(BytesIO(b"operator@example.com\r\n"))

    assert value.reveal() == b"operator@example.com"
    assert "operator@example.com" not in repr(value)


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("scope_input", object(), TypeError),
        ("workload", _workload_input().workload, ValueError),
        ("target_strategy", "manual", TypeError),
        ("targets", (), ValueError),
        ("targets", (("child", "8"),), ValueError),
        ("acknowledgements", ("",), ValueError),
        ("quota_contact", object(), TypeError),
        ("plan_out", "request.plan", TypeError),
    ],
)
def test_exact_composition_input_rejects_cross_wired_values(
    field_name: str,
    value: object,
    error: type[Exception],
) -> None:
    """The injected builder receives one complete safe public grammar."""
    with pytest.raises(error):
        replace(
            _exact_input(),
            **{field_name: value},  # type: ignore[bad-argument-type]
        )


def test_composition_input_rejects_missing_and_cross_wired_workload_targets() -> None:
    """Derived and manual workload strategies keep disjoint target forms."""
    with pytest.raises(ValueError, match="exactly one"):
        replace(_workload_input(), workload=None)
    with pytest.raises(ValueError, match="manual workload"):
        replace(
            _workload_input(),
            target_strategy=TargetStrategy.MANUAL,
            targets=(),
        )
    with pytest.raises(ValueError, match="name selected children"):
        replace(
            _workload_input(),
            target_strategy=TargetStrategy.MANUAL,
            targets=((None, "8"),),
        )
    with pytest.raises(ValueError, match="do not accept targets"):
        replace(_workload_input(), targets=(("child", "8"),))


@pytest.mark.parametrize(
    ("digest", "path", "error"),
    [
        (None, None, ValueError),
        ("digest", Path("plan"), ValueError),
        ("", None, ValueError),
        (None, "plan", TypeError),
    ],
)
def test_plan_reference_requires_exactly_one_typed_reference(
    digest: str | None,
    path: object,
    error: type[Exception],
) -> None:
    """Plan Review and Apply never guess between local and portable plans."""
    with pytest.raises(error):
        PlanReferenceInput(digest, path)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        (None, None, None),
        ("intent", WatchCondition.GRANTED, "resume"),
        ("", WatchCondition.GRANTED, None),
        (None, WatchCondition.GRANTED, "resume"),
        (None, None, ""),
    ],
)
def test_watch_cli_input_rejects_cross_wired_selectors(
    values: tuple[str | None, object, str | None],
) -> None:
    """Initial and resumed Watch controls are disjoint before construction."""
    intent_id, condition, resume = values
    with pytest.raises(ValueError, match=r"Watch CLI|initial|resumed"):
        WatchCliInput(
            intent_id,
            condition,  # type: ignore[arg-type]
            resume,
            NOW,
        )
    with pytest.raises(ValueError, match="absolute"):
        WatchCliInput(
            "intent",
            WatchCondition.GRANTED,
            None,
            NOW.replace(tzinfo=None),
        )


@pytest.mark.parametrize(
    "presentation",
    [
        lambda: LifecyclePresentation(output="jsonl", no_color=False, quiet=False),
        lambda: LifecyclePresentation(
            output="json",
            no_color="false",  # type: ignore[bad-argument-type]
            quiet=False,
        ),
        lambda: WatchPresentation(output="json", no_color=False, quiet=False),
        lambda: WatchPresentation(
            output="jsonl",
            no_color=False,
            quiet="false",  # type: ignore[bad-argument-type]
        ),
    ],
)
def test_lifecycle_presentations_reject_cross_wired_controls(
    presentation: object,
) -> None:
    """One-shot and stream vocabularies cannot be interchanged."""
    with pytest.raises((TypeError, ValueError)):
        presentation()  # type: ignore[operator]


def test_presenter_rejects_secret_bearing_or_untyped_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No protected value or untyped result crosses the presentation boundary."""
    with pytest.raises(TypeError, match="OperationResult"):
        emit_lifecycle_result(
            object(),  # type: ignore[bad-argument-type]
            LifecyclePresentation(output="json", no_color=False, quiet=False),
        )
    with pytest.raises(TypeError, match="LifecyclePresentation"):
        emit_lifecycle_result(  # type: ignore[arg-type]
            _result_with_data(SecretValue(b"secret")),
            object(),  # type: ignore[bad-argument-type]
        )
    with pytest.raises(TypeError, match="secret"):
        emit_lifecycle_result(
            _result_with_data(SecretValue(b"secret")),
            LifecyclePresentation(output="json", no_color=False, quiet=False),
        )
    assert capsys.readouterr().out == ""


@dataclass(frozen=True, slots=True)
class _LifecycleData:
    plan_digest: str
    ordered_children: tuple[str, ...]


def _result_with_data(data: object) -> OperationResult[object]:
    return OperationResult(
        operation=OperationName("plan.review"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("reviewed"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=data,
    )


def test_lifecycle_result_uses_canonical_json_and_required_human_facts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One-shot lifecycle output preserves the result envelope and child order."""
    result = OperationResult(
        operation=OperationName("plan.review"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("reviewed"), reached=True),
        outcome=Outcome(StableSymbol("succeeded"), ExitClass.SUCCESS),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=_LifecycleData(
            "sha256:" + ("a" * 64),
            ("accelerator", "companion"),
        ),
    )

    json_exit = emit_lifecycle_result(
        result,
        LifecyclePresentation(output="json", no_color=True, quiet=True),
    )
    structured = capsys.readouterr()
    human_exit = emit_lifecycle_result(
        result,
        LifecyclePresentation(output="human", no_color=True, quiet=True),
    )
    human = capsys.readouterr()

    payload = json.loads(structured.out)
    assert json_exit == human_exit == 0
    assert structured.err == ""
    assert payload["operation"] == "plan.review"
    assert payload["data"]["ordered_children"] == ["accelerator", "companion"]
    assert "Operation: plan.review" in human.out
    assert "Resource scope: projects/123" in human.out
    assert "Ordered children: accelerator, companion" in human.out


def test_rejected_structured_result_stays_valid_json_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rejected structured output retains diagnostics in-band on stdout."""
    result = OperationResult(
        operation=OperationName("plan.review"),
        resource_scope=SCOPE,
        boundary=OperationBoundary(StableSymbol("plan-reviewed"), reached=False),
        outcome=Outcome(
            StableSymbol("rejected-precondition"),
            ExitClass.REJECTED_PRECONDITION,
        ),
        completeness=Completeness.complete(),
        started_at=NOW,
        finished_at=NOW,
        data=None,
        diagnostics=(
            Diagnostic(
                code=DiagnosticCode("plan-expired"),
                severity=Severity.ERROR,
                phase=DiagnosticPhase("plan-review"),
                source=DiagnosticSource("local-plan"),
                retry=RetryDisposition.AFTER_NEW_PREVIEW,
                message=RedactedText("Plan expiry has elapsed."),
            ),
        ),
    )

    exit_class = emit_lifecycle_result(
        result,
        LifecyclePresentation(output="json", no_color=True, quiet=True),
    )
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert exit_class == int(ExitClass.REJECTED_PRECONDITION)
    assert captured.err == ""
    assert payload["outcome"]["exit_class"] == int(ExitClass.REJECTED_PRECONDITION)
    assert payload["diagnostics"][0]["code"] == "plan-expired"


def test_composition_presenter_emits_reached_and_rejected_results(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Compose presentation exposes deterministic JSON and fail-closed human output."""
    request = ComposeRequest(
        kind=PlanKind.SINGLE,
        strategy=TargetStrategy.MANUAL,
        resource_scope=SCOPE,
        children=(),
    )

    json_exit = emit_composition(
        Composition(request=request, reached=True),
        LifecyclePresentation(output="json", no_color=True, quiet=True),
    )
    structured = capsys.readouterr()
    human_exit = emit_composition(
        Composition(
            request=request,
            reached=False,
            incapability_reasons=("missing-current-evidence",),
        ),
        LifecyclePresentation(output="human", no_color=True, quiet=True),
    )
    human = capsys.readouterr()

    assert json_exit == 0
    assert json.loads(structured.out)["reached"] is True
    assert human_exit == REJECTED_COMPOSITION_EXIT
    assert human.out == ""
    assert "Reached: false" in human.err
    assert "Incapability reasons: missing-current-evidence" in human.err


def test_rejected_structured_composition_stays_valid_json_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rejected Compose JSON stays machine-readable without stderr fragments."""
    request = ComposeRequest(
        kind=PlanKind.SINGLE,
        strategy=TargetStrategy.MANUAL,
        resource_scope=SCOPE,
        children=(),
    )

    exit_class = emit_composition(
        Composition(
            request=request,
            reached=False,
            incapability_reasons=("missing-current-evidence",),
        ),
        LifecyclePresentation(output="json", no_color=True, quiet=True),
    )
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert exit_class == REJECTED_COMPOSITION_EXIT
    assert captured.err == ""
    assert payload["reached"] is False
    assert payload["incapability_reasons"] == ["missing-current-evidence"]


def test_composition_presenter_rejects_untyped_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Compose output accepts only typed compositions and presentation controls."""
    presentation = LifecyclePresentation(
        output="json",
        no_color=False,
        quiet=False,
    )
    request = ComposeRequest(
        kind=PlanKind.SINGLE,
        strategy=TargetStrategy.MANUAL,
        resource_scope=SCOPE,
        children=(),
    )
    with pytest.raises(TypeError, match="Composition"):
        emit_composition(object(), presentation)
    with pytest.raises(TypeError, match="LifecyclePresentation"):
        emit_composition(
            Composition(request=request, reached=True),
            object(),  # type: ignore[bad-argument-type]
        )
    assert capsys.readouterr().out == ""


def test_watch_jsonl_is_one_self_contained_record_and_human_names_condition(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each Watch presentation preserves sequence, resume, subject, and aggregate."""
    unit = QuotaUnit("1")
    child = WatchChildIdentity(
        child_id="single",
        order=0,
        slice_identity=EffectiveQuotaSliceIdentity(
            SCOPE,
            "compute.googleapis.com",
            "GPU-DIRECT",
            NormalizedDimensions((("region", "us-central1"),)),
            QuotaScope.REGIONAL,
        ),
        target=QuotaQuantity(8, unit),
        disposition=ApplyChildDisposition.ACCEPTED,
        preference_identity=(
            "projects/123/locations/global/quotaPreferences/cqmgr-opaque"
        ),
        lineage_etag="etag-1",
        lineage_trace_id=None,
    )
    subject = WatchSubject(
        PlanKind.SINGLE,
        SCOPE,
        WatchCondition.GRANTED,
        "sha256:" + ("b" * 64),
        "sha256:" + ("a" * 64),
        (child,),
    )
    aggregate = WatchAggregate(
        condition=WatchCondition.GRANTED,
        disposition=WatchDisposition.PENDING,
        accepted_children=1,
        children=(WatchChildSummary(child, None),),
    )
    event = WatchStreamEvent(
        stream_id="stream-1",
        sequence=0,
        event=WatchEventKind.INITIAL,
        resume="cqmgr.watch-resume/v1:opaque",
        observed_at=NOW,
        subject=subject,
        aggregate=aggregate,
    )

    emit_watch_event(
        event,
        WatchPresentation(output="jsonl", no_color=True, quiet=True),
    )
    structured = capsys.readouterr()
    emit_watch_event(
        event,
        WatchPresentation(output="human", no_color=True, quiet=True),
    )
    human = capsys.readouterr()

    payload = json.loads(structured.out)
    assert structured.out.count("\n") == 1
    assert payload["event"] == "initial"
    assert payload["sequence"] == 0
    assert payload["resume"] == "cqmgr.watch-resume/v1:opaque"
    assert payload["aggregate"]["accepted_children"] == 1
    assert "Condition: granted" in human.out
    assert "Disposition: pending" in human.out
