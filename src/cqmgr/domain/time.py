"""Shared domain timestamp validation."""

from datetime import UTC, datetime


def require_utc(value: object, field_name: str) -> None:
    """Require an aware UTC datetime with a field-specific error."""
    if not isinstance(value, datetime):
        msg = f"{field_name} must be a datetime"
        raise TypeError(msg)
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        msg = f"{field_name} must be an aware UTC timestamp"
        raise ValueError(msg)
