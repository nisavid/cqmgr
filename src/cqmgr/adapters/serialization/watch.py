"""Authenticated opaque Watch resume-token serialization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import TYPE_CHECKING, cast

from cqmgr.domain.status import WatchCondition
from cqmgr.domain.watch import WatchResumeClaims

if TYPE_CHECKING:
    from cqmgr.application.ports.secrets import SecretValue

_PREFIX = "cqmgr.watch-resume/v1:"
_FIELDS = frozenset(
    {
        "installation_id",
        "checkpoint_id",
        "intent_id",
        "subject_digest",
        "condition",
        "resolution_checkpoint",
        "sequence",
    }
)


class HmacWatchResumeCodec:
    """Encode authenticated V1 resume claims with no secret-bearing fields."""

    def encode(self, claims: WatchResumeClaims, key: SecretValue) -> str:
        """Return a URL-safe canonical payload plus SHA-256 authentication tag."""
        if not isinstance(claims, WatchResumeClaims):
            msg = "Watch resume claims must be typed"
            raise TypeError(msg)
        payload = json.dumps(
            {
                "installation_id": claims.installation_id,
                "checkpoint_id": claims.checkpoint_id,
                "intent_id": claims.intent_id,
                "subject_digest": claims.subject_digest,
                "condition": claims.condition.value,
                "resolution_checkpoint": claims.resolution_checkpoint,
                "sequence": claims.sequence,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        tag = hmac.digest(key.reveal(), payload, "sha256")
        return _PREFIX + base64.urlsafe_b64encode(payload + tag).decode().rstrip("=")

    def decode(self, token: str, key: SecretValue) -> WatchResumeClaims:
        """Authenticate exact canonical V1 claims and reject malformed tokens."""
        if not isinstance(token, str) or not token.startswith(_PREFIX):
            msg = "unsupported Watch resume token"
            raise ValueError(msg)
        encoded = token.removeprefix(_PREFIX)
        try:
            raw = base64.urlsafe_b64decode(encoded + ("=" * (-len(encoded) % 4)))
        except (ValueError, TypeError) as error:
            msg = "invalid Watch resume encoding"
            raise ValueError(msg) from error
        tag_size = hashlib.sha256().digest_size
        if len(raw) <= tag_size:
            msg = "invalid Watch resume payload"
            raise ValueError(msg)
        payload, supplied_tag = raw[:-tag_size], raw[-tag_size:]
        expected_tag = hmac.digest(key.reveal(), payload, "sha256")
        if not hmac.compare_digest(supplied_tag, expected_tag):
            msg = "Watch resume authentication failed"
            raise ValueError(msg)
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            msg = "invalid Watch resume payload"
            raise ValueError(msg) from error
        if not isinstance(decoded, dict) or frozenset(decoded) != _FIELDS:
            msg = "invalid Watch resume claims"
            raise ValueError(msg)
        canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
        if canonical != payload:
            msg = "Watch resume payload must be canonical"
            raise ValueError(msg)
        try:
            return WatchResumeClaims(
                installation_id=cast("str", decoded["installation_id"]),
                checkpoint_id=cast("str", decoded["checkpoint_id"]),
                intent_id=cast("str", decoded["intent_id"]),
                subject_digest=cast("str", decoded["subject_digest"]),
                condition=WatchCondition(cast("str", decoded["condition"])),
                resolution_checkpoint=cast("int", decoded["resolution_checkpoint"]),
                sequence=cast("int", decoded["sequence"]),
            )
        except (TypeError, ValueError) as error:
            msg = "invalid Watch resume claims"
            raise ValueError(msg) from error
