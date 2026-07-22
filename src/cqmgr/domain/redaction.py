"""Explicit redaction for safe domain text."""

from __future__ import annotations

import re
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
        if not isinstance(value, str) or any(
            not isinstance(term, str) for term in terms
        ):
            msg = "redacted text and redaction terms must be strings"
            raise TypeError(msg)
        if any(not term for term in terms):
            msg = "redaction terms must not be empty"
            raise ValueError(msg)

        ordered_terms = sorted(set(terms), key=lambda item: (-len(item), item))
        ranges = [
            (match.start(), match.start() + len(term))
            for term in (*ordered_terms, REDACTION_MARKER)
            for match in re.finditer(f"(?={re.escape(term)})", value)
        ]
        merged_ranges: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged_ranges and start <= merged_ranges[-1][1]:
                previous_start, previous_end = merged_ranges[-1]
                merged_ranges[-1] = (previous_start, max(previous_end, end))
            else:
                merged_ranges.append((start, end))

        parts: list[str] = []
        cursor = 0
        for start, end in merged_ranges:
            parts.append(value[cursor:start])
            parts.append(REDACTION_MARKER)
            cursor = end
        parts.append(value[cursor:])

        object.__setattr__(self, "_value", "".join(parts))

    @property
    def value(self) -> str:
        """Return the scrubbed text value."""
        return self._value

    def __str__(self) -> str:
        """Return the scrubbed text for presentation."""
        return self._value
