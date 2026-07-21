from __future__ import annotations

import json

import pytest

from app.services.payments.jeeb_relay import (
    JeebRelayAuthError,
    sign_jeeb_relay_request,
    verify_jeeb_relay_request,
)

SECRET = "b6f0f80b7c82f247bb86ff5de0a9197f2a562605e0b26f3c"
DEVICE_ID = "owner-phone-01"
NOW = 1_800_000_000
NONCE = "uM5hC2vQ9eA7kT4zP8xR"


def signed_request(body: bytes, **overrides: object) -> dict[str, object]:
    timestamp = int(overrides.pop("timestamp", NOW))
    nonce = str(overrides.pop("nonce", NONCE))
    device_id = str(overrides.pop("device_id", DEVICE_ID))
    signature = sign_jeeb_relay_request(
        secret=SECRET,
        body=body,
        device_id=device_id,
        timestamp=timestamp,
        nonce=nonce,
    )
    return {
        "secret": SECRET,
        "expected_device_id": DEVICE_ID,
        "body": body,
        "version": "1",
        "device_id": device_id,
        "timestamp": str(timestamp),
        "nonce": nonce,
        "signature": signature,
        "tolerance_seconds": 300,
        "now": NOW,
        **overrides,
    }


def test_valid_jeeb_relay_signature_is_accepted() -> None:
    body = json.dumps(
        {"transaction_id": "TX-100", "amount": 25000},
        separators=(",", ":"),
    ).encode()
    verified = verify_jeeb_relay_request(**signed_request(body))
    assert verified.device_id == DEVICE_ID
    assert verified.nonce == NONCE
    assert len(verified.body_sha256) == 64


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"body": b'{"amount":25001}'}, "signature"),
        ({"version": "2"}, "version"),
        ({"expected_device_id": "another-phone"}, "device"),
        ({"nonce": "short"}, "nonce"),
        ({"timestamp": str(NOW - 301)}, "Expired"),
    ],
)
def test_jeeb_relay_rejects_tampering_and_replay_window(
    change: dict[str, object],
    message: str,
) -> None:
    body = b'{"transaction_id":"TX-100","amount":25000}'
    arguments = signed_request(body)
    arguments.update(change)
    with pytest.raises(JeebRelayAuthError, match=message):
        verify_jeeb_relay_request(**arguments)
