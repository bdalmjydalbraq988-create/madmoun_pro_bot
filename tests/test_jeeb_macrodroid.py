from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.payments.jeeb_macrodroid import (
    JEEB_ANDROID_PACKAGE,
    JeebNotificationParseError,
    parse_jeeb_notification,
    unpack_macrodroid_notification,
    verify_macrodroid_relay_request,
)
from app.services.payments.jeeb_relay import JeebRelayAuthError

TOKEN = "macro-relay-token-" + ("x" * 48)
DEVICE_ID = "owner-phone-play-store"


def incoming_notification() -> str:
    return (
        "تم استلام حوالة واردة بنجاح\n"
        "المبلغ: ٢٥٬٠٠١ ريال يمني\n"
        "من حساب: ٧٧٧١٢٣٤٥٦\n"
        "رقم العملية: JEEB_ABC-123"
    )


def test_arabic_jeeb_notification_is_parsed_server_side() -> None:
    parsed = parse_jeeb_notification(incoming_notification())
    assert parsed.transaction_id == "JEEB_ABC-123"
    assert parsed.amount == Decimal("25001")
    assert parsed.sender_account == "777123456"


@pytest.mark.parametrize(
    "raw",
    [
        "تم الدفع وخصم المبلغ: 25001 ريال يمني رقم العملية: OUT-1 من حساب: 777123456",
        "رسالة عامة المبلغ: 25001 ريال يمني رقم العملية: BAD-1 من حساب: 777123456",
        "تم استلام حوالة واردة المبلغ: 25001 ريال يمني من حساب: 777123456",
    ],
)
def test_outgoing_or_incomplete_notification_is_rejected(raw: str) -> None:
    with pytest.raises(JeebNotificationParseError):
        parse_jeeb_notification(raw)


def test_plain_text_envelope_requires_official_jeeb_package() -> None:
    body = f"{JEEB_ANDROID_PACKAGE}\n{incoming_notification()}".encode()
    package, notification = unpack_macrodroid_notification(body)
    assert package == JEEB_ANDROID_PACKAGE
    assert notification == incoming_notification()

    with pytest.raises(JeebNotificationParseError, match="الرسمي"):
        unpack_macrodroid_notification(b"com.fake.wallet\nreceived money")


def test_macrodroid_bearer_authentication_and_replay_key_are_stable() -> None:
    body = f"{JEEB_ANDROID_PACKAGE}\n{incoming_notification()}".encode()
    first = verify_macrodroid_relay_request(
        token=TOKEN,
        expected_device_id=DEVICE_ID,
        authorization=f"Bearer {TOKEN}",
        device_id=DEVICE_ID,
        body=body,
    )
    replay = verify_macrodroid_relay_request(
        token=TOKEN,
        expected_device_id=DEVICE_ID,
        authorization=f"Bearer {TOKEN}",
        device_id=DEVICE_ID,
        body=body,
    )
    assert first.nonce == replay.nonce
    assert first.body_sha256 == replay.body_sha256
    assert len(first.nonce) == 32


@pytest.mark.parametrize(
    ("authorization", "device_id", "message"),
    [
        ("Bearer wrong-token", DEVICE_ID, "bearer token"),
        (None, DEVICE_ID, "bearer token"),
        (f"Bearer {TOKEN}", "another-phone", "device"),
    ],
)
def test_macrodroid_authentication_rejects_wrong_credentials(
    authorization: str | None,
    device_id: str,
    message: str,
) -> None:
    with pytest.raises(JeebRelayAuthError, match=message):
        verify_macrodroid_relay_request(
            token=TOKEN,
            expected_device_id=DEVICE_ID,
            authorization=authorization,
            device_id=device_id,
            body=b"payload",
        )
