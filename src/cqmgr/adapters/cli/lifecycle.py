"""Fail-closed parsing and presentation seams for lifecycle CLI routes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol

import click

from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.diagnostics import (
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
)
from cqmgr.domain.identity import PrincipalIdentity
from cqmgr.domain.plans import TargetStrategy
from cqmgr.domain.quotas import MonitoringValue, MonitoringValueKind, QuotaQuantity
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import OperationResult, StableSymbol
from cqmgr.domain.scopes import ResourceScope
from cqmgr.domain.status import WatchCondition

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cqmgr.application.operations.apply import ApplyRequest
    from cqmgr.application.operations.contacts import ProtectedContactResolver
    from cqmgr.application.operations.lifecycle import LifecycleOperations
    from cqmgr.application.operations.lifecycle_requests import (
        InstallationTrustSource,
        LifecycleCompositionIntent,
        LifecycleRequestOperations,
    )
    from cqmgr.application.operations.plans import (
        ComposeRequest,
        PlanReviewRequest,
        PreviewRequest,
    )
    from cqmgr.application.operations.read_only import (
        QuotaInspectSelector,
        ReadOnlyOperations,
        ReadOnlyScopeInput,
    )
    from cqmgr.application.operations.trust import LoadedInstallationTrust
    from cqmgr.application.operations.watch import WatchRequest
    from cqmgr.application.ports.plans import DecodedPlan, PlanCodec, PlanRepository
    from cqmgr.domain.accelerator_overlay import (
        CloudTpuSliceRequirement,
        ComputeInstanceRequirement,
    )
    from cqmgr.domain.watch import WatchStreamEvent

_RFC3339_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})\Z"
)
TARGET_STRATEGY_CHOICES = tuple(item.value for item in TargetStrategy)
DEFAULT_TARGET_STRATEGY = TargetStrategy.MINIMUM.value
MANUAL_TARGET_STRATEGY = TargetStrategy.MANUAL.value
WATCH_CONDITION_CHOICES = tuple(item.value for item in WatchCondition)


def parse_target_strategy(value: str) -> TargetStrategy:
    """Decode one public target strategy behind the CLI adapter boundary."""
    return TargetStrategy(value)


def parse_watch_condition(value: str) -> WatchCondition:
    """Decode one public Watch condition behind the CLI adapter boundary."""
    return WatchCondition(value)


def parse_absolute_rfc3339(value: str) -> datetime:
    """Parse one strict absolute RFC 3339 timestamp and normalize it to UTC."""
    if not isinstance(value, str) or _RFC3339_TIMESTAMP.fullmatch(value) is None:
        msg = "Watch deadline must be an absolute RFC 3339 timestamp"
        raise ValueError(msg)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        msg = "Watch deadline must be an absolute RFC 3339 timestamp"
        raise ValueError(msg) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "Watch deadline must be an absolute RFC 3339 timestamp"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def canonical_absolute_rfc3339(value: str) -> str:
    """Return one strict absolute RFC 3339 input in canonical UTC form."""
    return parse_absolute_rfc3339(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class LifecyclePresentation:
    """One-shot lifecycle result controls."""

    output: str
    no_color: bool
    quiet: bool

    def __post_init__(self) -> None:
        """Require the stable one-shot presentation vocabulary."""
        if self.output not in {"human", "json"}:
            msg = "lifecycle output must be human or json"
            raise ValueError(msg)
        if not isinstance(self.no_color, bool) or not isinstance(self.quiet, bool):
            msg = "lifecycle presentation flags must be boolean"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class WatchPresentation:
    """Watch stream controls."""

    output: str
    no_color: bool
    quiet: bool

    def __post_init__(self) -> None:
        """Require the stable Watch presentation vocabulary."""
        if self.output not in {"human", "jsonl"}:
            msg = "Watch output must be human or jsonl"
            raise ValueError(msg)
        if not isinstance(self.no_color, bool) or not isinstance(self.quiet, bool):
            msg = "Watch presentation flags must be boolean"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class RequestCompositionInput:
    """Complete public Compose or Preview input before protected resolution."""

    scope_input: ReadOnlyScopeInput
    selector: QuotaInspectSelector | None
    workload: ComputeInstanceRequirement | CloudTpuSliceRequirement | None
    target_strategy: TargetStrategy
    targets: tuple[tuple[str | None, str], ...]
    acknowledgements: tuple[str, ...] = ()
    expert: bool = False
    quota_contact: SecretValue | None = dataclass_field(
        default=None,
        repr=False,
    )
    plan_out: Path | None = None

    def __post_init__(self) -> None:  # noqa: C901
        """Require one exact-slice or workload grammar without secret defaults."""
        from cqmgr.application.operations.read_only import (  # noqa: PLC0415
            QuotaInspectSelector,
            ReadOnlyScopeInput,
        )
        from cqmgr.domain.accelerator_overlay import (  # noqa: PLC0415
            CloudTpuSliceRequirement,
            ComputeInstanceRequirement,
        )
        from cqmgr.domain.plans import TargetStrategy  # noqa: PLC0415

        if not isinstance(self.scope_input, ReadOnlyScopeInput):
            msg = "lifecycle scope input must use ReadOnlyScopeInput"
            raise TypeError(msg)
        exact = isinstance(self.selector, QuotaInspectSelector)
        workload = isinstance(
            self.workload,
            (ComputeInstanceRequirement, CloudTpuSliceRequirement),
        )
        if exact == workload:
            msg = "lifecycle request requires exactly one exact slice or workload"
            raise ValueError(msg)
        if not isinstance(self.target_strategy, TargetStrategy):
            msg = "lifecycle target strategy must use TargetStrategy"
            raise TypeError(msg)
        if not isinstance(self.targets, tuple) or any(
            (child_id is not None and (not isinstance(child_id, str) or not child_id))
            or not isinstance(value, str)
            or not value
            for child_id, value in self.targets
        ):
            msg = "lifecycle targets must be nonempty public values"
            raise ValueError(msg)
        if not isinstance(self.expert, bool):
            msg = "lifecycle expert intent must be boolean"
            raise TypeError(msg)
        if exact and (
            self.target_strategy is not TargetStrategy.MANUAL
            or len(self.targets) != 1
            or self.targets[0][0] is not None
        ):
            msg = "exact lifecycle request requires one manual absolute target"
            raise ValueError(msg)
        if workload:
            if self.target_strategy is TargetStrategy.MANUAL and (
                not self.targets
                or any(child_id is None for child_id, _ in self.targets)
            ):
                msg = "manual workload lifecycle targets must name selected children"
                raise ValueError(msg)
            if self.target_strategy is not TargetStrategy.MANUAL and self.targets:
                msg = "derived workload lifecycle strategies do not accept targets"
                raise ValueError(msg)
        if not isinstance(self.acknowledgements, tuple) or any(
            not isinstance(value, str) or not value for value in self.acknowledgements
        ):
            msg = "lifecycle acknowledgements must be nonempty text"
            raise ValueError(msg)
        if self.quota_contact is not None and not isinstance(
            self.quota_contact,
            SecretValue,
        ):
            msg = "lifecycle quota contact must be a SecretValue or None"
            raise TypeError(msg)
        if self.plan_out is not None and not isinstance(self.plan_out, Path):
            msg = "lifecycle plan output must use Path or None"
            raise TypeError(msg)

    def to_intent(self) -> LifecycleCompositionIntent:
        """Translate the validated public adapter shape to the shared async input."""
        from cqmgr.application.operations.lifecycle_requests import (  # noqa: PLC0415
            LifecycleCompositionIntent,
        )

        return LifecycleCompositionIntent(
            scope_input=self.scope_input,
            selector=self.selector,
            workload=self.workload,
            target_strategy=self.target_strategy,
            targets=self.targets,
            acknowledgements=self.acknowledgements,
            expert=self.expert,
            quota_contact=self.quota_contact,
            plan_out=self.plan_out,
        )


@dataclass(frozen=True, slots=True)
class PlanReferenceInput:
    """One local digest or portable Plan file selected by the operator."""

    digest: str | None
    path: Path | None

    def __post_init__(self) -> None:
        """Require exactly one nonempty Plan reference."""
        if (self.digest is None) == (self.path is None):
            msg = "Plan reference requires exactly one digest or path"
            raise ValueError(msg)
        if self.digest is not None and (
            not isinstance(self.digest, str) or not self.digest
        ):
            msg = "Plan digest must be nonempty"
            raise ValueError(msg)
        if self.path is not None and not isinstance(self.path, Path):
            msg = "Plan path must use Path"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class WatchCliInput:
    """Initial or resumed Watch selection with one absolute deadline."""

    intent_id: str | None
    condition: WatchCondition | None
    resume: str | None
    deadline: datetime

    def __post_init__(self) -> None:
        """Keep initial and resumed selectors disjoint before runtime conversion."""
        from cqmgr.domain.status import WatchCondition  # noqa: PLC0415

        initial = self.intent_id is not None
        resumed = self.resume is not None
        if initial == resumed:
            msg = "Watch CLI requires exactly one intent ID or resume token"
            raise ValueError(msg)
        if initial and (
            not isinstance(self.intent_id, str)
            or not self.intent_id
            or not isinstance(self.condition, WatchCondition)
        ):
            msg = "initial Watch CLI requires intent ID and condition"
            raise ValueError(msg)
        if resumed and (
            not isinstance(self.resume, str)
            or not self.resume
            or self.condition is not None
        ):
            msg = "resumed Watch CLI recovers its condition from the token"
            raise ValueError(msg)
        if (
            not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            msg = "Watch CLI deadline must be an absolute timestamp"
            raise ValueError(msg)


class LifecycleCliRequestFactory(Protocol):
    """Convert public adapter inputs into protected typed operation requests."""

    def compose(self, value: RequestCompositionInput) -> ComposeRequest:
        """Resolve a public Compose input into its protected typed request."""
        ...

    def preview(self, value: RequestCompositionInput) -> PreviewRequest:
        """Resolve a public Preview input into its protected typed request."""
        ...

    def review(self, value: PlanReferenceInput) -> PlanReviewRequest:
        """Resolve one public Plan reference for Review."""
        ...

    async def apply(
        self,
        value: PlanReferenceInput,
        acknowledgement: str,
        *,
        quota_contact: SecretValue | None = None,
    ) -> ApplyRequest:
        """Resolve one Plan reference, exact scope, and protected contact input."""
        ...

    def watch(self, value: WatchCliInput) -> WatchRequest:
        """Resolve public Watch controls into protected runtime inputs."""
        ...


class LifecycleCliClock(Protocol):
    """Clock surface required for one-shot and monotonic lifecycle inputs."""

    def now(self) -> datetime:
        """Return current aware UTC time."""
        ...

    def monotonic(self) -> float:
        """Return current process-local monotonic seconds."""
        ...


@dataclass(slots=True)
class LifecycleCliRuntime:
    """Injected lifecycle graph with one idempotent async shutdown owner."""

    operations: LifecycleOperations
    requests: LifecycleCliRequestFactory
    preparation: LifecycleRequestOperations | None = None
    read_only: ReadOnlyOperations | None = None
    shutdown: Callable[[], Awaitable[None]] | None = dataclass_field(
        default=None,
        repr=False,
    )

    async def aclose(self) -> None:
        """Release the invocation-scoped read and client graph at most once."""
        shutdown = self.shutdown
        self.shutdown = None
        if shutdown is not None:
            await shutdown()


class ProtectedLifecycleCliRequestFactory:
    """Build Review, Apply, and Watch requests from active local authority."""

    def __init__(
        self,
        *,
        trust: InstallationTrustSource,
        repository: PlanRepository,
        codec: PlanCodec,
        contacts: ProtectedContactResolver,
        clock: LifecycleCliClock,
    ) -> None:
        """Bind read-only plan lookup and explicit protected runtime inputs."""
        self._trust = trust
        self._repository = repository
        self._codec = codec
        self._contacts = contacts
        self._clock = clock

    def compose(self, value: RequestCompositionInput) -> ComposeRequest:
        """Reject the obsolete synchronous evidence path."""
        del value
        message = "Compose requires async lifecycle preparation"
        raise RuntimeError(message)

    def preview(self, value: RequestCompositionInput) -> PreviewRequest:
        """Reject the obsolete synchronous evidence path."""
        del value
        message = "Preview requires async lifecycle preparation"
        raise RuntimeError(message)

    def review(self, value: PlanReferenceInput) -> PlanReviewRequest:
        """Review local digests with authority and exports without requiring it."""
        from cqmgr.application.operations.plans import (  # noqa: PLC0415
            PlanReviewRequest,
        )
        from cqmgr.application.operations.trust import TrustLoadError  # noqa: PLC0415

        try:
            trust = self._trust.load()
        except TrustLoadError:
            if value.digest is not None:
                raise
            return PlanReviewRequest(
                value.digest,
                value.path,
                None,
                "installation-authority-unavailable",
                self._clock.now(),
            )
        return PlanReviewRequest(
            value.digest,
            value.path,
            trust.authentication_key if value.digest is not None else None,
            trust.installation_id,
            self._clock.now(),
        )

    async def apply(
        self,
        value: PlanReferenceInput,
        acknowledgement: str,
        *,
        quota_contact: SecretValue | None = None,
    ) -> ApplyRequest:
        """Authenticate one local/exported Plan and rebind protected input."""
        from cqmgr.application.configuration import (  # noqa: PLC0415
            parse_resource_scope_name,
        )
        from cqmgr.application.operations.apply import ApplyRequest  # noqa: PLC0415

        trust = self._trust.load()
        decoded = self._load_apply_plan(value, trust)
        plan = decoded.plan
        if plan.installation_id != trust.installation_id or not decoded.authenticate(
            trust.authentication_key.reveal()
        ):
            message = "Apply Plan is not authenticated by this installation"
            raise RuntimeError(message)
        contact = await self._contacts.prepare_apply(
            plan.contact_binding,
            explicit_value=quota_contact,
            principal=plan.principal,
            trust=trust,
        )
        return ApplyRequest(
            digest=decoded.digest,
            authentication_key=trust.authentication_key,
            local_installation_id=trust.installation_id,
            resource_scope_acknowledgement=parse_resource_scope_name(acknowledgement),
            principal=plan.principal,
            contact_binding=plan.contact_binding,
            contact_value=contact.value.reveal().decode("utf-8"),
            now=self._clock.now(),
        )

    def watch(self, value: WatchCliInput) -> WatchRequest:
        """Bind active trust and convert an absolute deadline to monotonic time."""
        from cqmgr.application.operations.watch import WatchRequest  # noqa: PLC0415
        from cqmgr.application.ports.coordination import (  # noqa: PLC0415
            CancellationToken,
        )

        trust = self._trust.load()
        remaining = (value.deadline - self._clock.now()).total_seconds()
        return WatchRequest(
            intent_id=value.intent_id,
            condition=value.condition,
            resume=value.resume,
            authentication_key=trust.authentication_key,
            installation_id=trust.installation_id,
            deadline=self._clock.monotonic() + remaining,
            cancellation=CancellationToken(),
        )

    def _load_apply_plan(
        self,
        value: PlanReferenceInput,
        trust: LoadedInstallationTrust,
    ) -> DecodedPlan:
        """Load and locally import one exact authenticated Apply plan."""
        from cqmgr.application.ports.plans import (  # noqa: PLC0415
            EncodedPlan,
            PlanRepositoryStatus,
        )

        if value.digest is not None:
            loaded = self._repository.load(
                value.digest,
                trust.authentication_key,
                self._clock.now(),
            )
            expected_status = PlanRepositoryStatus.AVAILABLE
        else:
            if value.path is None:  # pragma: no cover - validated input
                message = "Apply requires one Plan reference"
                raise RuntimeError(message)
            loaded = self._repository.read_export(value.path)
            expected_status = PlanRepositoryStatus.EXPORTED
        if loaded.status is not expected_status or loaded.plan_bytes is None:
            message = "Apply Plan is unavailable"
            raise RuntimeError(message)
        decoded = self._codec.decode(loaded.plan_bytes)
        if value.digest is None:
            imported = self._repository.store(
                EncodedPlan(loaded.plan_bytes, decoded.digest),
                trust.authentication_key,
            )
            if imported.status not in {
                PlanRepositoryStatus.STORED,
                PlanRepositoryStatus.CONFLICT,
            }:
                message = "Apply Plan could not be imported"
                raise RuntimeError(message)
        return decoded


def read_quota_contact(stream: BinaryIO) -> SecretValue:
    """Read exactly one protected UTF-8 line and retain only redacted bytes."""
    raw = stream.read()
    if not isinstance(raw, bytes):
        msg = "quota contact input must be bytes"
        raise TypeError(msg)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    if not raw or b"\x00" in raw or b"\n" in raw or b"\r" in raw:
        msg = "quota contact must be exactly one nonempty UTF-8 line"
        raise ValueError(msg)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        msg = "quota contact must be exactly one nonempty UTF-8 line"
        raise ValueError(msg) from error
    return SecretValue(raw)


def emit_lifecycle_result(
    result: OperationResult[Any],
    presentation: LifecyclePresentation,
) -> int:
    """Emit one complete lifecycle result and return its stable exit class."""
    if not isinstance(result, OperationResult):
        msg = "lifecycle presentation requires OperationResult"
        raise TypeError(msg)
    if not isinstance(presentation, LifecyclePresentation):
        msg = "lifecycle presentation must use LifecyclePresentation"
        raise TypeError(msg)
    _require_no_secrets(result)
    mapping = operation_result_mapping(result)
    destination_error = int(result.outcome.exit_class) != 0
    if presentation.output == "json":
        click.echo(
            json.dumps(
                mapping,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        for line in _result_lines(mapping):
            click.echo(line, err=destination_error)
        for line in _diagnostic_lines(mapping["diagnostics"]):
            click.echo(line, err=True)
    return int(result.outcome.exit_class)


def emit_composition(
    composition: object,
    presentation: LifecyclePresentation,
) -> int:
    """Emit one complete pre-Preview composition without inventing a plan."""
    from cqmgr.application.operations.plans import Composition  # noqa: PLC0415

    if not isinstance(composition, Composition):
        msg = "Compose presentation requires Composition"
        raise TypeError(msg)
    if not isinstance(presentation, LifecyclePresentation):
        msg = "Compose presentation must use LifecyclePresentation"
        raise TypeError(msg)
    _require_no_secrets(composition)
    mapping = _json_value(composition)
    exit_class = 0 if composition.reached else 3
    if presentation.output == "json":
        click.echo(
            json.dumps(
                mapping,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        for line in _human_lines("composition", mapping):
            click.echo(line, err=exit_class != 0)
    return exit_class


def emit_watch_event(
    event: WatchStreamEvent,
    presentation: WatchPresentation,
) -> None:
    """Emit one self-contained initial, material, or terminal Watch record."""
    from cqmgr.domain.watch import WatchStreamEvent  # noqa: PLC0415

    if not isinstance(event, WatchStreamEvent):
        msg = "Watch presentation requires WatchStreamEvent"
        raise TypeError(msg)
    if not isinstance(presentation, WatchPresentation):
        msg = "Watch presentation must use WatchPresentation"
        raise TypeError(msg)
    mapping = _json_value(event)
    if presentation.output == "jsonl":
        click.echo(
            json.dumps(
                mapping,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    for line in _watch_lines(mapping):
        click.echo(line)
    for line in _diagnostic_lines(mapping["diagnostics"]):
        click.echo(line, err=True)


def _result_lines(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    boundary = _as_mapping(mapping["boundary"])
    outcome = _as_mapping(mapping["outcome"])
    scope = mapping["resource_scope"]
    scope_mapping = None if scope is None else _as_mapping(scope)
    scope_line = (
        "Resource scope: unavailable"
        if scope_mapping is None
        else f"Resource scope: {scope_mapping['name']}"
    )
    lines = [
        f"Operation: {mapping['operation']}",
        (f"Outcome: {outcome['code']} (exit {outcome['exit_class']})"),
        (
            f"Boundary: {boundary['condition']} "
            f"({'reached' if boundary['reached'] else 'not reached'})"
        ),
        f"Complete: {str(mapping['complete']).lower()}",
        scope_line,
    ]
    identity_evidence = mapping.get("identity_evidence")
    if identity_evidence is not None:
        lines.extend(_human_lines("identity_evidence", identity_evidence))
    lines.extend(_human_lines("data", mapping["data"]))
    return tuple(lines)


def _diagnostic_lines(value: object) -> tuple[str, ...]:
    """Render ordered safe diagnostic facts and recovery guidance."""
    if not isinstance(value, list):
        msg = "lifecycle diagnostics must be a list"
        raise TypeError(msg)
    lines: list[str] = []
    for item in value:
        diagnostic = _as_mapping(item)
        lines.extend(
            (
                f"Diagnostic {diagnostic['code']} ({diagnostic['severity']})",
                "Diagnostic context: "
                f"{diagnostic['source']}; {diagnostic['phase']}; "
                f"retry {diagnostic['retry']}",
                f"Guidance: {diagnostic['message']}",
            )
        )
    return tuple(lines)


def _watch_lines(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    subject = _as_mapping(mapping["subject"])
    aggregate = _as_mapping(mapping["aggregate"])
    lines = [
        f"Event: {mapping['event']}",
        f"Stream: {mapping['stream_id']}",
        f"Sequence: {mapping['sequence']}",
        f"Observed at: {mapping['observed_at']}",
        f"Intent: {subject['intent_id']}",
        f"Plan kind: {subject['kind']}",
        f"Condition: {aggregate['condition']}",
        f"Disposition: {aggregate['disposition']}",
        f"Accepted children: {aggregate['accepted_children']}",
        f"Resume: {mapping['resume']}",
    ]
    if mapping["child_id"] is not None:
        lines.append(f"Changed child: {mapping['child_id']}")
    children = aggregate["children"]
    if not isinstance(children, list):
        msg = "Watch aggregate children must be a list"
        raise TypeError(msg)
    for index, child_value in enumerate(children, start=1):
        child = _as_mapping(child_value)
        child_identity = _as_mapping(child["child"])
        lines.append(f"Child {index}: {child_identity['child_id']}")
        status = child["status"]
        if status is None:
            lines.append("Child status: unavailable")
        else:
            lines.extend(_human_lines("status", status))
    if mapping["result"] is not None:
        lines.extend(_result_lines(_as_mapping(mapping["result"])))
    return tuple(lines)


def _as_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = "lifecycle presentation value must be a mapping"
        raise TypeError(msg)
    return value


def _require_no_secrets(value: object) -> None:
    """Reject any secret recursively before a serializer can inspect its bytes."""
    if isinstance(value, SecretValue):
        msg = "secret values cannot cross the lifecycle presentation boundary"
        raise TypeError(msg)
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _require_no_secrets(getattr(value, field.name))
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _require_no_secrets(item)
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            _require_no_secrets(item)


def _human_lines(prefix: str, value: object) -> list[str]:  # noqa: PLR0911
    label = (
        "Authenticated principal"
        if prefix == "acting_principal"
        else prefix.replace("_", " ").capitalize()
    )
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            lines.extend(_human_lines(str(key), item))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{label}: none"]
        if all(not isinstance(item, (Mapping, list)) for item in value):
            return [f"{label}: {', '.join(str(item) for item in value)}"]
        lines = []
        for index, item in enumerate(value, start=1):
            lines.extend(_human_lines(f"{prefix} {index}", item))
        return lines
    if value is None:
        return [f"{label}: unavailable"]
    if isinstance(value, bool):
        return [f"{label}: {str(value).lower()}"]
    return [f"{label}: {value}"]


def _json_value(value: object) -> Any:  # noqa: ANN401, C901, PLR0911
    if isinstance(value, OperationResult):
        return operation_result_mapping(value)
    if isinstance(value, ResourceScope):
        return {"type": value.kind.value, "name": value.canonical_name}
    if isinstance(value, QuotaQuantity):
        return {"value": value.base10, "unit": value.unit.symbol}
    if isinstance(value, MonitoringValue):
        provider_value = (
            str(value.value) if value.kind is MonitoringValueKind.INT64 else value.value
        )
        return {"kind": value.kind.value, "value": provider_value}
    if isinstance(
        value,
        (
            StableSymbol,
            DiagnosticCode,
            DiagnosticPhase,
            DiagnosticSource,
            PrincipalIdentity,
            RedactedText,
        ),
    ):
        return value.value
    if isinstance(value, SecretValue):
        msg = "secret values cannot cross the lifecycle presentation boundary"
        raise TypeError(msg)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
