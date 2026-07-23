"""CLI parsing and presentation for read-only local audit operations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import click

from cqmgr.adapters.serialization.results import operation_result_mapping
from cqmgr.application.operations.audit import (
    AuditInspectData,
    AuditListData,
    AuditVerifyData,
)
from cqmgr.domain.audit import AuditQuery, AuditRecord
from cqmgr.domain.results import OperationName, StableSymbol

if TYPE_CHECKING:
    from cqmgr.domain.results import OperationResult

_RFC3339_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)


@dataclass(frozen=True, slots=True)
class AuditPresentation:
    """One-shot presentation controls for audit operation results."""

    output: str
    no_color: bool
    quiet: bool

    def __post_init__(self) -> None:
        """Require the public one-shot output and terminal controls."""
        if self.output not in {"human", "json"}:
            msg = "audit output must be human or json"
            raise ValueError(msg)
        if not isinstance(self.no_color, bool) or not isinstance(self.quiet, bool):
            msg = "audit presentation flags must be boolean"
            raise TypeError(msg)


def parse_audit_query(  # noqa: PLR0913
    *,
    operations: tuple[str, ...] = (),
    outcomes: tuple[str, ...] = (),
    since: str | None = None,
    until: str | None = None,
    limit: str | int = 100,
    cursor: str | None = None,
) -> AuditQuery:
    """Build one typed bounded audit query from decoded CLI primitives."""
    return AuditQuery(
        operations=tuple(OperationName(value) for value in operations),
        outcomes=tuple(StableSymbol(value) for value in outcomes),
        since=_parse_rfc3339(since, "since"),
        until=_parse_rfc3339(until, "until"),
        limit=_parse_limit(limit),
        cursor=cursor,
    )


def _parse_rfc3339(value: str | None, option: str) -> datetime | None:
    """Parse one absolute RFC 3339 time and normalize it to UTC."""
    if value is None:
        return None
    if not isinstance(value, str) or _RFC3339_PATTERN.fullmatch(value) is None:
        msg = f"{option} must be an RFC 3339 timestamp"
        raise ValueError(msg)
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as error:
        msg = f"{option} must be an RFC 3339 timestamp"
        raise ValueError(msg) from error
    return parsed.astimezone(UTC)


def _parse_limit(value: str | int) -> int:
    """Decode the bounded audit page size without coercing booleans."""
    if isinstance(value, bool):
        msg = "audit query limit must be an integer"
        raise TypeError(msg)
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        msg = "audit query limit must be an integer"
        raise TypeError(msg)
    try:
        return int(value)
    except ValueError as error:
        msg = "audit query limit must be an integer"
        raise ValueError(msg) from error


def emit_audit_result(
    result: OperationResult[Any],
    presentation: AuditPresentation,
) -> int:
    """Write exactly one audit result form and return its global exit class."""
    exit_class = int(result.outcome.exit_class)
    if presentation.output == "json":
        click.echo(
            json.dumps(
                operation_result_mapping(result),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        for line in _human_lines(result):
            click.echo(line, err=exit_class != 0)
    return exit_class


def _human_lines(result: OperationResult[Any]) -> list[str]:
    """Build an ANSI-free complete audit result with a failure envelope."""
    data_lines = _data_lines(result.data)
    resource_scope = (
        result.resource_scope.canonical_name
        if result.resource_scope is not None
        else "none"
    )
    if int(result.outcome.exit_class) == 0:
        return [
            f"Operation: {result.operation.value}",
            f"Resource scope: {resource_scope}",
            f"Complete: {str(result.completeness.is_complete).lower()}",
            *data_lines,
        ]
    reached = "reached" if result.boundary.reached else "not reached"
    return [
        f"Operation: {result.operation.value}",
        f"Outcome: {result.outcome.code.value} (exit {int(result.outcome.exit_class)})",
        f"Boundary: {result.boundary.condition.value} ({reached})",
        f"Complete: {str(result.completeness.is_complete).lower()}",
        f"Resource scope: {resource_scope}",
        *data_lines,
    ]


def _data_lines(data: object) -> list[str]:
    """Render only the explicit typed audit operation payloads."""
    if isinstance(data, AuditListData):
        return _list_lines(data)
    if isinstance(data, AuditInspectData):
        return _inspect_lines(data)
    if isinstance(data, AuditVerifyData):
        return _verify_lines(data)
    return ["Result data: unavailable"]


def _list_lines(data: AuditListData) -> list[str]:
    """Render the bounded query and each complete retained record."""
    lines = _query_lines(data.query)
    lines.append(f"Next cursor: {data.next_cursor or 'none'}")
    if data.reason is not None:
        lines.append(f"Reason: {data.reason}")
    if data.guidance is not None:
        lines.append(f"Guidance: {data.guidance}")
    for record in data.records:
        lines.extend(_record_lines(record))
    return lines


def _inspect_lines(data: AuditInspectData) -> list[str]:
    """Render an exact audit identity and its retained record when present."""
    lines = [f"Record ID: {data.record_id or 'none'}"]
    if data.record is not None:
        lines.extend(_record_lines(data.record, include_id=False))
    if data.reason is not None:
        lines.append(f"Reason: {data.reason}")
    if data.guidance is not None:
        lines.append(f"Guidance: {data.guidance}")
    return lines


def _verify_lines(data: AuditVerifyData) -> list[str]:
    """Render requested bounds and the exact verification disposition."""
    lines = [
        f"Verify from: {data.from_record_id or 'start'}",
        f"Verify through: {data.through_record_id or 'end'}",
    ]
    if data.verification is not None:
        verification = data.verification
        lines.extend(
            (
                f"Chain valid: {str(verification.valid).lower()}",
                f"Verified from: {verification.verified_from or 'none'}",
                f"Verified through: {verification.verified_through or 'none'}",
            )
        )
        if verification.failure is not None:
            failure = verification.failure
            lines.extend(
                (
                    f"Failure code: {failure.code.value}",
                    f"Failure segment: {failure.segment}",
                    "Failure sequence: "
                    f"{failure.sequence if failure.sequence is not None else 'none'}",
                    f"Failure record ID: {failure.record_id or 'none'}",
                )
            )
    if data.reason is not None:
        lines.append(f"Reason: {data.reason}")
    if data.guidance is not None:
        lines.append(f"Guidance: {data.guidance}")
    return lines


def _query_lines(query: AuditQuery | None) -> list[str]:
    """Render audit filters without inventing a missing decoded query."""
    if query is None:
        return ["Audit query: unavailable"]
    return [
        "Query operations: "
        + (", ".join(item.value for item in query.operations) or "all"),
        "Query outcomes: "
        + (", ".join(item.value for item in query.outcomes) or "all"),
        f"Query since: {_timestamp(query.since)}",
        f"Query until: {_timestamp(query.until)}",
        f"Query limit: {query.limit}",
        f"Query cursor: {query.cursor or 'none'}",
    ]


def _record_lines(record: AuditRecord, *, include_id: bool = True) -> list[str]:
    """Render the explicit retained facts from one typed audit record."""
    draft = record.draft
    scope = draft.resource_scope.canonical_name if draft.resource_scope else "none"
    lines = [
        *([f"Record ID: {record.record_id}"] if include_id else []),
        f"Record sequence: {record.sequence}",
        f"Record segment: {record.segment}",
        f"Record kind: {draft.kind.value}",
        f"Record operation: {draft.operation.value}",
        f"Record resource scope: {scope}",
        f"Record occurred at: {_timestamp(draft.occurred_at)}",
        f"Record outcome: {draft.outcome.value if draft.outcome else 'none'}",
        "Record diagnostics: "
        + (", ".join(item.value for item in draft.diagnostic_codes) or "none"),
        f"Previous hash: {record.previous_hash}",
        f"Record hash: {record.record_hash}",
    ]
    lines.extend(f"Fact {fact.name.value}: {fact.value}" for fact in draft.facts)
    return lines


def _timestamp(value: datetime | None) -> str:
    """Render a nullable UTC timestamp without providing a parser contract."""
    return "none" if value is None else value.isoformat().replace("+00:00", "Z")
