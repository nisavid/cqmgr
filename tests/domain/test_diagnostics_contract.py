"""Contract tests for provider-neutral ordered diagnostics."""

from __future__ import annotations

from dataclasses import fields
from typing import cast

import pytest

from cqmgr.domain.diagnostics import (
    Diagnostic,
    DiagnosticCode,
    DiagnosticPhase,
    Diagnostics,
    DiagnosticSource,
    FieldPath,
    ProviderMetadata,
    RetryDisposition,
    Severity,
)
from cqmgr.domain.redaction import REDACTION_MARKER, RedactedText


def _diagnostic(code: str, *, severity: Severity = Severity.ERROR) -> Diagnostic:
    return Diagnostic(
        code=DiagnosticCode(code),
        severity=severity,
        phase=DiagnosticPhase("effective-quota-read"),
        source=DiagnosticSource("cloud-quotas-api"),
        retry=RetryDisposition.AFTER_BACKOFF,
        message=RedactedText(
            "request req-123 failed for /Users/ivan/project",
            sensitive_values=("req-123",),
            machine_paths=("/Users/ivan/project",),
        ),
        field_paths=(FieldPath(("data", "effective-quota-slices", "0")),),
        provider_metadata=ProviderMetadata(
            http_status=429,
            grpc_status=RedactedText("RESOURCE_EXHAUSTED"),
            reason=RedactedText("rate-limit"),
            preference_identity=RedactedText("quotaPreferences/abc"),
            etag=RedactedText("etag-value", sensitive_values=("etag-value",)),
            trace_identity=RedactedText("trace-123", sensitive_values=("trace-123",)),
            request_identity=RedactedText(
                "request-123",
                sensitive_values=("request-123",),
            ),
        ),
    )


def test_diagnostic_carries_typed_safe_ordered_facts() -> None:
    """A diagnostic exposes control facts separately from scrubbed prose."""
    diagnostic = _diagnostic("provider-rate-limited")

    assert diagnostic.code.value == "provider-rate-limited"
    assert diagnostic.severity is Severity.ERROR
    assert diagnostic.phase.value == "effective-quota-read"
    assert diagnostic.source.value == "cloud-quotas-api"
    assert diagnostic.retry is RetryDisposition.AFTER_BACKOFF
    assert str(diagnostic.message) == (
        f"request {REDACTION_MARKER} failed for {REDACTION_MARKER}"
    )
    assert diagnostic.field_paths == (
        FieldPath(("data", "effective-quota-slices", "0")),
    )
    assert diagnostic.provider_metadata == ProviderMetadata(
        http_status=429,
        grpc_status=RedactedText("RESOURCE_EXHAUSTED"),
        reason=RedactedText("rate-limit"),
        preference_identity=RedactedText("quotaPreferences/abc"),
        etag=RedactedText(REDACTION_MARKER),
        trace_identity=RedactedText(REDACTION_MARKER),
        request_identity=RedactedText(REDACTION_MARKER),
    )


def test_diagnostic_codes_are_open_stable_symbols() -> None:
    """A new valid code needs no central enum update, while unstable forms fail."""
    assert DiagnosticCode("future-provider-condition").value == (
        "future-provider-condition"
    )

    for invalid in ("", "ProviderFailure", "provider_failure", "provider--failure"):
        with pytest.raises(ValueError, match="lowercase kebab case"):
            DiagnosticCode(invalid)


def test_phase_and_source_are_distinct_open_symbolic_types() -> None:
    """Phase and source remain extensible without becoming interchangeable strings."""
    assert DiagnosticPhase("plan-validation").value == "plan-validation"
    assert DiagnosticSource("local-keyring").value == "local-keyring"

    with pytest.raises(TypeError, match="DiagnosticPhase"):
        Diagnostic(
            code=DiagnosticCode("invalid-phase-type"),
            severity=Severity.ERROR,
            phase=cast("DiagnosticPhase", DiagnosticSource("provider")),
            source=DiagnosticSource("provider"),
            retry=RetryDisposition.NEVER,
            message=RedactedText("safe"),
        )


def test_severity_and_retry_disposition_are_closed() -> None:
    """Severity and retry control values reject values outside the contract."""
    assert tuple(member.value for member in Severity) == (
        "info",
        "warning",
        "error",
        "critical",
    )
    assert tuple(member.value for member in RetryDisposition) == (
        "never",
        "after-upgrade",
        "after-refresh",
        "after-new-preview",
        "after-backoff",
        "unknown",
    )

    with pytest.raises(ValueError, match="debug"):
        Severity("debug")
    with pytest.raises(ValueError, match="immediate"):
        RetryDisposition("immediate")


def test_provider_metadata_is_a_closed_positive_allowlist() -> None:
    """Only reviewed provider facts exist; unsafe generic payload slots do not."""
    assert {field.name for field in fields(ProviderMetadata)} == {
        "etag",
        "grpc_status",
        "http_status",
        "preference_identity",
        "reason",
        "request_identity",
        "trace_identity",
    }

    with pytest.raises(TypeError):
        ProviderMetadata(raw_body=RedactedText("{}"))  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="RedactedText"):
        ProviderMetadata(reason="unsafe raw string")  # type: ignore[arg-type]


@pytest.mark.parametrize("http_status", [99, 600, True, 429.5])
def test_provider_metadata_rejects_invalid_http_status(http_status: object) -> None:
    """HTTP metadata is a status code, not an arbitrary provider integer."""
    with pytest.raises(ValueError, match="HTTP status"):
        ProviderMetadata(http_status=cast("int", http_status))


def test_diagnostic_requires_redacted_text_for_human_and_provider_text() -> None:
    """Raw strings cannot bypass explicit safe-text construction."""
    with pytest.raises(TypeError, match="RedactedText"):
        Diagnostic(
            code=DiagnosticCode("raw-message-rejected"),
            severity=Severity.ERROR,
            phase=DiagnosticPhase("validation"),
            source=DiagnosticSource("local"),
            retry=RetryDisposition.NEVER,
            message="unsafe raw message",  # type: ignore[arg-type]
        )


def test_diagnostic_rejects_untyped_optional_values() -> None:
    """Optional diagnostic collections and metadata retain their domain types."""
    base = {
        "code": DiagnosticCode("invalid-optional-type"),
        "severity": Severity.ERROR,
        "phase": DiagnosticPhase("validation"),
        "source": DiagnosticSource("local"),
        "retry": RetryDisposition.NEVER,
        "message": RedactedText("safe"),
    }

    with pytest.raises(TypeError, match="tuple of FieldPath"):
        Diagnostic(
            **base,
            field_paths=cast("tuple[FieldPath, ...]", (object(),)),
        )
    with pytest.raises(TypeError, match="ProviderMetadata"):
        Diagnostic(
            **base,
            provider_metadata=cast("ProviderMetadata", object()),
        )


def test_field_path_is_nonempty_and_has_no_empty_segment() -> None:
    """Field paths are structured domain values rather than machine paths."""
    with pytest.raises(TypeError, match="tuple of strings"):
        FieldPath(cast("tuple[str, ...]", ["data"]))
    with pytest.raises(TypeError, match="tuple of strings"):
        FieldPath(cast("tuple[str, ...]", ("data", object())))
    with pytest.raises(ValueError, match="at least one"):
        FieldPath(())
    with pytest.raises(ValueError, match="segments must not be empty"):
        FieldPath(("data", ""))


def test_diagnostics_preserves_order_and_returns_new_values_on_append() -> None:
    """Diagnostic order is stable and additions do not mutate prior results."""
    first = _diagnostic("first-warning", severity=Severity.WARNING)
    second = _diagnostic("second-error")

    original = Diagnostics((first,))
    extended = original.append(second)

    assert tuple(original) == (first,)
    assert tuple(extended) == (first, second)
    assert len(extended) == len((first, second))
    assert extended[1] is second


def test_diagnostics_rejects_untyped_items() -> None:
    """The ordered collection cannot become an untyped message list."""
    unsafe_items = cast("tuple[Diagnostic, ...]", ("unsafe raw diagnostic",))

    with pytest.raises(TypeError, match="only Diagnostic"):
        Diagnostics(unsafe_items)
