"""Provider-neutral diagnostic facts and safe presentation text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from cqmgr.domain.redaction import RedactedText

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_MIN_HTTP_STATUS: Final = 100
_MAX_HTTP_STATUS: Final = 599


def _require_symbol(value: str, type_name: str) -> None:
    segments = value.split("-")
    valid = bool(value) and all(
        bool(segment)
        and "a" <= segment[0] <= "z"
        and all("a" <= char <= "z" or "0" <= char <= "9" for char in segment)
        for segment in segments
    )
    if not valid:
        msg = f"{type_name} must use lowercase kebab case"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DiagnosticCode:
    """An open, stable symbolic diagnostic code."""

    value: str

    def __post_init__(self) -> None:
        """Validate the public symbolic-code representation."""
        _require_symbol(self.value, type(self).__name__)


@dataclass(frozen=True, slots=True)
class DiagnosticPhase:
    """An open symbolic operation phase that produced a diagnostic."""

    value: str

    def __post_init__(self) -> None:
        """Validate the public phase representation."""
        _require_symbol(self.value, type(self).__name__)


@dataclass(frozen=True, slots=True)
class DiagnosticSource:
    """An open symbolic authoritative or local diagnostic source."""

    value: str

    def __post_init__(self) -> None:
        """Validate the public source representation."""
        _require_symbol(self.value, type(self).__name__)


class Severity(StrEnum):
    """Closed diagnostic severity vocabulary."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class RetryDisposition(StrEnum):
    """Closed guidance for whether and when an operation may be retried."""

    NEVER = "never"
    AFTER_REFRESH = "after-refresh"
    AFTER_NEW_PREVIEW = "after-new-preview"
    AFTER_BACKOFF = "after-backoff"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FieldPath:
    """A structured path to a field in an operation input or result."""

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject paths that cannot identify a field."""
        if not isinstance(self.segments, tuple) or any(
            not isinstance(segment, str) for segment in self.segments
        ):
            msg = "field path segments must be a tuple of strings"
            raise TypeError(msg)
        if not self.segments:
            msg = "a field path must contain at least one segment"
            raise ValueError(msg)
        if any(not segment for segment in self.segments):
            msg = "field path segments must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Closed allowlist of safe provider diagnostic metadata."""

    http_status: int | None = None
    grpc_status: RedactedText | None = None
    reason: RedactedText | None = None
    preference_identity: RedactedText | None = None
    etag: RedactedText | None = None
    trace_identity: RedactedText | None = None
    request_identity: RedactedText | None = None

    def __post_init__(self) -> None:
        """Require status-shaped integers and explicitly scrubbed text."""
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not _MIN_HTTP_STATUS <= self.http_status <= _MAX_HTTP_STATUS
        ):
            msg = "HTTP status must be an integer from 100 through 599"
            raise ValueError(msg)

        text_values = (
            self.grpc_status,
            self.reason,
            self.preference_identity,
            self.etag,
            self.trace_identity,
            self.request_identity,
        )
        if any(
            value is not None and not isinstance(value, RedactedText)
            for value in text_values
        ):
            msg = "provider text metadata must use RedactedText"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One ordered, typed, provider-neutral operation diagnostic."""

    code: DiagnosticCode
    severity: Severity
    phase: DiagnosticPhase
    source: DiagnosticSource
    retry: RetryDisposition
    message: RedactedText
    field_paths: tuple[FieldPath, ...] = ()
    provider_metadata: ProviderMetadata | None = None

    def __post_init__(self) -> None:
        """Reject raw or cross-wired values at the safe diagnostic boundary."""
        required_types = (
            (self.code, DiagnosticCode),
            (self.severity, Severity),
            (self.phase, DiagnosticPhase),
            (self.source, DiagnosticSource),
            (self.retry, RetryDisposition),
            (self.message, RedactedText),
        )
        for value, expected_type in required_types:
            if not isinstance(value, expected_type):
                msg = f"diagnostic value must use {expected_type.__name__}"
                raise TypeError(msg)

        if not isinstance(self.field_paths, tuple) or any(
            not isinstance(path, FieldPath) for path in self.field_paths
        ):
            msg = "diagnostic field paths must be a tuple of FieldPath values"
            raise TypeError(msg)
        if self.provider_metadata is not None and not isinstance(
            self.provider_metadata,
            ProviderMetadata,
        ):
            msg = "diagnostic provider metadata must use ProviderMetadata"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True, init=False)
class Diagnostics:
    """An immutable, insertion-ordered diagnostic collection."""

    _items: tuple[Diagnostic, ...]

    def __init__(self, items: Iterable[Diagnostic] = ()) -> None:
        """Capture diagnostics in the caller-supplied order."""
        captured = tuple(items)
        if any(not isinstance(item, Diagnostic) for item in captured):
            msg = "diagnostic collections may contain only Diagnostic values"
            raise TypeError(msg)
        object.__setattr__(self, "_items", captured)

    def append(self, diagnostic: Diagnostic) -> Diagnostics:
        """Return a new collection with one diagnostic appended."""
        return Diagnostics((*self._items, diagnostic))

    def __iter__(self) -> Iterator[Diagnostic]:
        """Iterate in stable insertion order."""
        return iter(self._items)

    def __len__(self) -> int:
        """Return the number of diagnostics."""
        return len(self._items)

    def __getitem__(self, index: int) -> Diagnostic:
        """Return a diagnostic at its stable position."""
        return self._items[index]
