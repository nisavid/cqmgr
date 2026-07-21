"""Explicit redaction for safe domain text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

REDACTION_MARKER: Final = "[REDACTED]"


@dataclass(frozen=True, slots=True, init=False)
class RedactedText:
    """Text scrubbed using only explicitly supplied sensitive values and paths."""

    _value: str

    def __init__(
        self,
        value: str,
        *,
        sensitive_values: Iterable[str] = (),
        machine_paths: Iterable[str] = (),
    ) -> None:
        """Redact explicit terms longest-first using one stable marker."""
        terms = tuple(sensitive_values) + tuple(machine_paths)
        if any(not term for term in terms):
            msg = "redaction terms must not be empty"
            raise ValueError(msg)

        ordered_terms = sorted(set(terms), key=lambda item: (-len(item), item))
        parts: list[str] = []
        index = 0
        while index < len(value):
            matched = next(
                (term for term in ordered_terms if value.startswith(term, index)),
                None,
            )
            marker_starts_here = value.startswith(REDACTION_MARKER, index)
            if marker_starts_here and (
                matched is None or len(matched) < len(REDACTION_MARKER)
            ):
                parts.append(REDACTION_MARKER)
                index += len(REDACTION_MARKER)
                continue

            if matched is None:
                parts.append(value[index])
                index += 1
                continue

            parts.append(REDACTION_MARKER)
            index += len(matched)

        object.__setattr__(self, "_value", "".join(parts))

    @property
    def value(self) -> str:
        """Return the scrubbed text value."""
        return self._value

    def __str__(self) -> str:
        """Return the scrubbed text for presentation."""
        return self._value
