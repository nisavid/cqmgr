"""Read-only application operations for local audit evidence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cqmgr.domain.audit import AuditQuery, AuditRecord, AuditVerification
from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    DiagnosticSource,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.redaction import RedactedText
from cqmgr.domain.results import (
    Completeness,
    EvidenceGap,
    ExitClass,
    OperationBoundary,
    OperationName,
    OperationResult,
    Outcome,
    StableSymbol,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cqmgr.application.ports.audit import AuditJournal
    from cqmgr.application.ports.clock import Clock


@dataclass(frozen=True, slots=True)
class AuditListData:
    """One bounded local audit query result."""

    query: AuditQuery | None
    records: tuple[AuditRecord, ...]
    next_cursor: str | None
    reason: str | None = None
    guidance: str | None = None


@dataclass(frozen=True, slots=True)
class AuditInspectData:
    """One exact retained audit record, when found."""

    record_id: str | None
    record: AuditRecord | None
    reason: str | None = None
    guidance: str | None = None


@dataclass(frozen=True, slots=True)
class AuditVerifyData:
    """The validity result for one requested audit-chain range."""

    from_record_id: str | None
    through_record_id: str | None
    verification: AuditVerification | None
    reason: str | None = None
    guidance: str | None = None


class AuditOperations:
    """Run async read-only audit operations over the local journal port."""

    def __init__(self, journal: AuditJournal, clock: Clock) -> None:
        """Inject only the local journal and observation clock."""
        self._journal = journal
        self._clock = clock

    async def list(self, query: AuditQuery) -> OperationResult[AuditListData]:
        """Read one bounded local audit query page."""
        if not isinstance(query, AuditQuery):
            return await self.list_usage_failure(
                "audit query must be a valid AuditQuery"
            )
        started_at = self._clock.now()
        try:
            page = await asyncio.to_thread(self._journal.query, query)
        except ValueError:
            return await self.list_usage_failure("audit query is invalid")
        except Exception:  # noqa: BLE001
            return self._journal_failure(
                operation="audit.list",
                boundary="audit-query-read",
                data=AuditListData(
                    query=query,
                    records=(),
                    next_cursor=None,
                    reason="audit-journal-unavailable",
                    guidance=_JOURNAL_GUIDANCE,
                ),
                started_at=started_at,
            )
        return self._result(
            operation="audit.list",
            boundary="audit-query-read",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            completeness=Completeness.complete(),
            data=AuditListData(query, page.records, page.next_cursor),
            started_at=started_at,
        )

    async def list_usage_failure(self, reason: str) -> OperationResult[AuditListData]:
        """Return usage after the caller decoded an invalid audit query."""
        return self._usage_result(
            operation="audit.list",
            boundary="audit-query-valid",
            outcome="invalid-audit-query",
            data=AuditListData(None, (), None, reason=reason),
        )

    async def inspect(self, record_id: str) -> OperationResult[AuditInspectData]:
        """Read one exact retained audit record."""
        if not isinstance(record_id, str) or not record_id:
            return await self.inspect_usage_failure(
                "audit record identity must be non-empty"
            )
        started_at = self._clock.now()
        try:
            record = await asyncio.to_thread(self._journal.inspect, record_id)
        except Exception:  # noqa: BLE001
            return self._journal_failure(
                operation="audit.inspect",
                boundary="audit-record-read",
                data=AuditInspectData(
                    record_id,
                    None,
                    reason="audit-journal-unavailable",
                    guidance=_JOURNAL_GUIDANCE,
                ),
                started_at=started_at,
            )
        if record is None:
            return self._result(
                operation="audit.inspect",
                boundary="audit-record-read",
                reached=False,
                outcome="audit-record-not-found",
                exit_class=ExitClass.REJECTED_PRECONDITION,
                completeness=Completeness.complete(),
                data=AuditInspectData(record_id, None, reason="audit-record-not-found"),
                started_at=started_at,
            )
        return self._result(
            operation="audit.inspect",
            boundary="audit-record-read",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            completeness=Completeness.complete(),
            data=AuditInspectData(record_id, record),
            started_at=started_at,
        )

    async def inspect_usage_failure(
        self, reason: str
    ) -> OperationResult[AuditInspectData]:
        """Return usage after the caller decoded an invalid record identity."""
        return self._usage_result(
            operation="audit.inspect",
            boundary="audit-record-identity-valid",
            outcome="invalid-audit-record-identity",
            data=AuditInspectData(None, None, reason=reason),
        )

    async def verify(
        self,
        *,
        from_record_id: str | None = None,
        through_record_id: str | None = None,
    ) -> OperationResult[AuditVerifyData]:
        """Verify one complete retained audit-chain range."""
        if not (
            _valid_optional_record_id(from_record_id)
            and _valid_optional_record_id(through_record_id)
        ):
            return await self.verify_usage_failure(
                "audit verification identities must be non-empty strings"
            )
        started_at = self._clock.now()
        try:
            verification = await asyncio.to_thread(
                self._journal.verify,
                from_record_id=from_record_id,
                through_record_id=through_record_id,
            )
        except Exception:  # noqa: BLE001
            return self._journal_failure(
                operation="audit.verify",
                boundary="audit-chain-valid",
                data=AuditVerifyData(
                    from_record_id,
                    through_record_id,
                    None,
                    reason="audit-journal-unavailable",
                    guidance=_JOURNAL_GUIDANCE,
                ),
                started_at=started_at,
            )
        data = AuditVerifyData(from_record_id, through_record_id, verification)
        if not verification.valid:
            return self._result(
                operation="audit.verify",
                boundary="audit-chain-valid",
                reached=False,
                outcome="audit-chain-invalid",
                exit_class=ExitClass.REQUESTED_OUTCOME_UNMET,
                completeness=Completeness.complete(),
                data=data,
                started_at=started_at,
            )
        return self._result(
            operation="audit.verify",
            boundary="audit-chain-valid",
            reached=True,
            outcome="succeeded",
            exit_class=ExitClass.SUCCESS,
            completeness=Completeness.complete(),
            data=data,
            started_at=started_at,
        )

    async def verify_usage_failure(
        self, reason: str
    ) -> OperationResult[AuditVerifyData]:
        """Return usage after the caller decoded invalid range input."""
        return self._usage_result(
            operation="audit.verify",
            boundary="audit-verification-range-valid",
            outcome="invalid-audit-verification-range",
            data=AuditVerifyData(None, None, None, reason=reason),
        )

    def _usage_result[DataT](
        self,
        *,
        operation: str,
        boundary: str,
        outcome: str,
        data: DataT,
    ) -> OperationResult[DataT]:
        return self._result(
            operation=operation,
            boundary=boundary,
            reached=False,
            outcome=outcome,
            exit_class=ExitClass.USAGE,
            completeness=Completeness.complete(),
            data=data,
            started_at=self._clock.now(),
        )

    def _journal_failure[DataT](
        self,
        *,
        operation: str,
        boundary: str,
        data: DataT,
        started_at: datetime,
    ) -> OperationResult[DataT]:
        diagnostic = Diagnostic(
            code=DiagnosticCode("audit-journal-unavailable"),
            severity=Severity.ERROR,
            phase=DiagnosticPhase("audit-read"),
            source=DiagnosticSource("local-audit"),
            retry=RetryDisposition.AFTER_REFRESH,
            message=RedactedText(_JOURNAL_GUIDANCE),
        )
        return self._result(
            operation=operation,
            boundary=boundary,
            reached=False,
            outcome="audit-journal-unavailable",
            exit_class=ExitClass.OPERATIONAL_FAILURE,
            completeness=Completeness.unavailable(
                EvidenceGap(
                    StableSymbol("local-audit"),
                    StableSymbol("audit-journal-unavailable"),
                )
            ),
            data=data,
            started_at=started_at,
            diagnostics=(diagnostic,),
        )

    def _result[DataT](  # noqa: PLR0913
        self,
        *,
        operation: str,
        boundary: str,
        reached: bool,
        outcome: str,
        exit_class: ExitClass,
        completeness: Completeness,
        data: DataT,
        started_at: datetime,
        diagnostics: tuple[Diagnostic, ...] = (),
    ) -> OperationResult[DataT]:
        return OperationResult(
            operation=OperationName(operation),
            resource_scope=None,
            boundary=OperationBoundary(StableSymbol(boundary), reached),
            outcome=Outcome(StableSymbol(outcome), exit_class),
            completeness=completeness,
            started_at=started_at,
            finished_at=self._clock.now(),
            data=data,
            diagnostics=diagnostics,
        )


def _valid_optional_record_id(value: object) -> bool:
    """Accept no range bound or one non-empty opaque retained identity."""
    return value is None or (isinstance(value, str) and bool(value))


_JOURNAL_GUIDANCE = "Check cqmgr local audit storage, then retry."
