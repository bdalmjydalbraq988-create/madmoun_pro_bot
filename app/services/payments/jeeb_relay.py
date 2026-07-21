from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass


class JeebRelayAuthError(ValueError):
    """Raised when a Jeeb relay request cannot be trusted."""


_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_HEX_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class VerifiedJeebRelay:
    device_id: str
    timestamp: int
    nonce: str
    body_sha256: str


def jeeb_relay_canonical_message(
    *,
    device_id: str,
    timestamp: int,
    nonce: str,
    body_sha256: str,
) -> bytes:
    """Return the byte-for-byte v1 message signed by the phone relay."""

    return (
        f"jeeb-relay-v1\n{device_id}\n{timestamp}\n{nonce}\n{body_sha256}"
    ).encode("utf-8")


def sign_jeeb_relay_request(
    *,
    secret: str,
    body: bytes,
    device_id: str,
    timestamp: int,
    nonce: str,
) -> str:
    """Create the lowercase SHA-256 HMAC used by the Android relay."""

    body_sha256 = hashlib.sha256(body).hexdigest()
    message = jeeb_relay_canonical_message(
        device_id=device_id,
        timestamp=timestamp,
        nonce=nonce,
        body_sha256=body_sha256,
    )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_jeeb_relay_request(
    *,
    secret: str,
    expected_device_id: str,
    body: bytes,
    version: str | None,
    device_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    tolerance_seconds: int,
    now: int | None = None,
) -> VerifiedJeebRelay:
    """Authenticate one relay request without logging or exposing its secret."""

    if version != "1":
        raise JeebRelayAuthError("Unsupported relay protocol version")
    if not device_id or not _DEVICE_ID_RE.fullmatch(device_id):
        raise JeebRelayAuthError("Invalid relay device ID")
    if not hmac.compare_digest(device_id, expected_device_id):
        raise JeebRelayAuthError("Unknown relay device")
    if not nonce or not _NONCE_RE.fullmatch(nonce):
        raise JeebRelayAuthError("Invalid relay nonce")
    if not timestamp or not timestamp.isascii() or not timestamp.isdecimal():
        raise JeebRelayAuthError("Invalid relay timestamp")
    request_time = int(timestamp)
    current_time = int(time.time()) if now is None else int(now)
    if abs(current_time - request_time) > tolerance_seconds:
        raise JeebRelayAuthError("Expired relay request")
    normalized_signature = (signature or "").removeprefix("sha256=").lower()
    if not _HEX_SIGNATURE_RE.fullmatch(normalized_signature):
        raise JeebRelayAuthError("Invalid relay signature")
    expected_signature = sign_jeeb_relay_request(
        secret=secret,
        body=body,
        device_id=device_id,
        timestamp=request_time,
        nonce=nonce,
    )
    if not hmac.compare_digest(normalized_signature, expected_signature):
        raise JeebRelayAuthError("Invalid relay signature")
    return VerifiedJeebRelay(
        device_id=device_id,
        timestamp=request_time,
        nonce=nonce,
        body_sha256=hashlib.sha256(body).hexdigest(),
    )
