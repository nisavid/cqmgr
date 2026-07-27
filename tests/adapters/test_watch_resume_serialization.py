"""Authenticated Watch resume tokens fail closed at the public codec seam."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, cast

import pytest

from cqmgr.adapters.serialization.watch import HmacWatchResumeCodec
from cqmgr.application.ports.secrets import SecretValue
from cqmgr.domain.status import WatchCondition
from cqmgr.domain.watch import WatchResumeClaims

PREFIX = "cqmgr.watch-resume/v1:"
KEY = SecretValue(b"k" * 32)


def _claims() -> WatchResumeClaims:
    return WatchResumeClaims(
        installation_id="installation-123",
        checkpoint_id="checkpoint-123",
        intent_id="sha256:" + ("a" * 64),
        subject_digest="sha256:" + ("b" * 64),
        condition=WatchCondition.FULFILLED,
        resolution_checkpoint=2,
        sequence=7,
    )


def _token(payload: bytes, *, tag: bytes | None = None) -> str:
    authentication_tag = (
        hmac.digest(KEY.reveal(), payload, "sha256") if tag is None else tag
    )
    encoded = base64.urlsafe_b64encode(payload + authentication_tag).decode()
    return PREFIX + encoded.rstrip("=")


def _mapping() -> dict[str, object]:
    claims = _claims()
    return {
        "installation_id": claims.installation_id,
        "checkpoint_id": claims.checkpoint_id,
        "intent_id": claims.intent_id,
        "subject_digest": claims.subject_digest,
        "condition": claims.condition.value,
        "resolution_checkpoint": claims.resolution_checkpoint,
        "sequence": claims.sequence,
    }


def test_resume_codec_round_trips_canonical_authenticated_claims() -> None:
    """Canonical claims retain every subject and checkpoint binding."""
    codec = HmacWatchResumeCodec()

    token = codec.encode(_claims(), KEY)

    assert codec.decode(token, KEY) == _claims()
    assert token.startswith(PREFIX)


def test_resume_codec_rejects_untyped_encode_input() -> None:
    """Encoding accepts only the typed claims boundary."""
    with pytest.raises(TypeError, match="claims must be typed"):
        HmacWatchResumeCodec().encode(cast("Any", _mapping()), KEY)


@pytest.mark.parametrize("token", [cast("Any", None), "unsupported:token"])
def test_resume_codec_rejects_unsupported_token_shapes(token: object) -> None:
    """Unknown prefixes and non-string tokens fail before decoding."""
    with pytest.raises(ValueError, match="unsupported Watch resume token"):
        HmacWatchResumeCodec().decode(cast("Any", token), KEY)


def test_resume_codec_rejects_invalid_base64() -> None:
    """Malformed URL-safe encoding fails before authentication."""
    with pytest.raises(ValueError, match="invalid Watch resume encoding"):
        HmacWatchResumeCodec().decode(PREFIX + "A", KEY)


def test_resume_codec_rejects_payload_without_an_authentication_tag() -> None:
    """A token shorter than the SHA-256 tag cannot carry claims."""
    encoded = base64.urlsafe_b64encode(b"short").decode().rstrip("=")

    with pytest.raises(ValueError, match="invalid Watch resume payload"):
        HmacWatchResumeCodec().decode(PREFIX + encoded, KEY)


def test_resume_codec_rejects_wrong_authentication_tag() -> None:
    """Authenticated claims cannot be replaced under a foreign tag."""
    payload = json.dumps(
        _mapping(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(ValueError, match="authentication failed"):
        HmacWatchResumeCodec().decode(
            _token(payload, tag=b"\0" * hashlib.sha256().digest_size),
            KEY,
        )


def test_resume_codec_rejects_authenticated_non_json_payload() -> None:
    """A valid tag does not make malformed claim bytes acceptable."""
    with pytest.raises(ValueError, match="invalid Watch resume payload"):
        HmacWatchResumeCodec().decode(_token(b"{"), KEY)


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps(["not", "claims"], separators=(",", ":")).encode(),
        json.dumps(
            {**_mapping(), "unexpected": True},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    ],
)
def test_resume_codec_rejects_authenticated_wrong_claim_shapes(payload: bytes) -> None:
    """Claims must be one exact closed object even when authenticated."""
    with pytest.raises(ValueError, match="invalid Watch resume claims"):
        HmacWatchResumeCodec().decode(_token(payload), KEY)


def test_resume_codec_rejects_authenticated_noncanonical_json() -> None:
    """Equivalent but noncanonical JSON cannot acquire another token identity."""
    payload = json.dumps(_mapping(), sort_keys=True).encode()

    with pytest.raises(ValueError, match="payload must be canonical"):
        HmacWatchResumeCodec().decode(_token(payload), KEY)


def test_resume_codec_rejects_authenticated_invalid_claim_values() -> None:
    """Closed claim fields retain their domain types after authentication."""
    payload = json.dumps(
        {**_mapping(), "condition": "unsupported"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(ValueError, match="invalid Watch resume claims"):
        HmacWatchResumeCodec().decode(_token(payload), KEY)
