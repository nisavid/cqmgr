"""Properties for explicitly supplied diagnostic redaction terms."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cqmgr.domain.redaction import REDACTION_MARKER, RedactedText


def test_redacted_text_replaces_sensitive_values_and_machine_paths_longest_first() -> (
    None
):
    """Explicit values and paths are replaced without leaking overlapping terms."""
    text = "token=abc; config=/Users/ivan/.config/cqmgr; owner=ivan"

    redacted = RedactedText(
        text,
        sensitive_values=("abc", "ivan"),
        machine_paths=("/Users/ivan/.config/cqmgr", "/Users/ivan"),
    )

    assert str(redacted) == (
        f"token={REDACTION_MARKER}; config={REDACTION_MARKER}; owner={REDACTION_MARKER}"
    )
    assert redacted.value == str(redacted)


@pytest.mark.parametrize("argument", ["sensitive_values", "machine_paths"])
def test_redacted_text_rejects_empty_redaction_terms(argument: str) -> None:
    """An empty term cannot turn every string boundary into a redaction marker."""
    kwargs = {argument: ("",)}

    with pytest.raises(ValueError, match="must not be empty"):
        RedactedText("safe", **kwargs)


def test_redacted_text_rejects_non_string_inputs() -> None:
    """Untyped values cannot bypass the explicit safe-text boundary."""
    with pytest.raises(TypeError, match="must be strings"):
        RedactedText(7)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be strings"):
        RedactedText("safe", sensitive_values=(7,))  # type: ignore[arg-type]


@given(
    prefix=st.text(),
    secret=st.text(min_size=1),
    suffix=st.text(),
)
def test_redacted_text_is_idempotent_for_explicit_terms(
    prefix: str,
    secret: str,
    suffix: str,
) -> None:
    """Applying the same explicit redaction set repeatedly is stable."""
    first = RedactedText(
        prefix + secret + suffix,
        sensitive_values=(secret,),
    )

    second = RedactedText(str(first), sensitive_values=(secret,))

    assert second == first


@given(
    shorter=st.text(
        alphabet=st.characters(min_codepoint=97, max_codepoint=122),
        min_size=1,
    ),
    final=st.characters(min_codepoint=97, max_codepoint=122),
)
def test_redacted_text_prefers_longer_overlapping_terms(
    shorter: str,
    final: str,
) -> None:
    """Overlapping redaction terms cannot expose a suffix of the longer term."""
    longer = shorter + final
    text = longer + "|"

    redacted = RedactedText(text, sensitive_values=(shorter, longer))

    assert str(redacted) == REDACTION_MARKER + "|"


def test_redacted_text_does_not_claim_unknown_secret_discovery() -> None:
    """Text stays unchanged when no explicit redaction term is supplied."""
    text = "token-shaped-but-not-declared"

    assert str(RedactedText(text)) == text


def test_redacted_text_never_redacts_inside_its_stable_marker() -> None:
    """A supplied term that overlaps the marker cannot corrupt prior redaction."""
    terms = ("secret", "RED")

    first = RedactedText("secret", sensitive_values=terms)
    second = RedactedText(str(first), sensitive_values=terms)

    assert str(first) == REDACTION_MARKER
    assert second == first


@pytest.mark.parametrize(
    ("text", "terms"),
    [
        ("abcde", ("ab", "bcde")),
        ("abcd", ("ab", "cd")),
    ],
)
def test_redacted_text_merges_overlapping_or_adjacent_sensitive_ranges(
    text: str,
    terms: tuple[str, ...],
) -> None:
    """Every character covered by connected sensitive terms is redacted once."""
    assert str(RedactedText(text, sensitive_values=terms)) == REDACTION_MARKER


@pytest.mark.parametrize(
    "secret",
    [
        REDACTION_MARKER,
        f"{REDACTION_MARKER}:token",
        f"token:{REDACTION_MARKER}:tail",
    ],
)
def test_redacted_text_redacts_explicit_terms_containing_the_marker(
    secret: str,
) -> None:
    """Marker-shaped explicit secrets are redacted as complete terms."""
    redacted = RedactedText(f"before|{secret}|after", sensitive_values=(secret,))

    assert str(redacted) == f"before|{REDACTION_MARKER}|after"
    assert RedactedText(str(redacted), sensitive_values=(secret,)) == redacted
