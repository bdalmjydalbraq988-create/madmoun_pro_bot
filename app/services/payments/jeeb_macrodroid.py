from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.services.payments.jeeb_relay import JeebRelayAuthError, VerifiedJeebRelay

JEEB_ANDROID_PACKAGE = "com.ahd.jaib"

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_TRANSACTION_RE = re.compile(
    r"(?:رقم\s*(?:العملية|المرجع)|مرجع\s*العملية|transaction\s*(?:id|no\.?))"
    r"\s*[:：#-]?\s*([A-Z0-9_-]{3,200})",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    r"(?:المبلغ|amount)\s*[:：]?\s*([0-9٠-٩۰-۹٬,.]+)"
    r"\s*(?:ر\.?\s*ي|ريال(?:\s*يمني)?|YER)",
    re.IGNORECASE,
)
_SENDER_RE = re.compile(
    r"(?:من\s*(?:حساب|رقم)?|المرسل|sender)\s*[:：]?"
    r"\s*([0-9٠-٩۰-۹ +()\-]{6,40})",
    re.IGNORECASE,
)
_SUCCESS_RE = re.compile(
    r"(?:تم\s+(?:استلام|استقبال)|استلمت|حوالة\s+واردة|"
    r"تحويل\s+وارد|received|credited)",
    re.IGNORECASE,
)
_DEBIT_RE = re.compile(
    r"(?:تم\s+(?:الإرسال|الدفع|السحب)|أرسلت|دفعت|سحب|خصم|"
    r"sent|paid|withdraw)",
    re.IGNORECASE,
)
_DIGIT_TRANSLATION = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class JeebNotificationParseError(ValueError):
    """Raised when a notification cannot be proven to be an incoming Jeeb transfer."""


@dataclass(frozen=True, slots=True)
class ParsedJeebNotification:
    transaction_id: str
    amount: Decimal
    sender_account: str


def verify_macrodroid_relay_request(
    *,
    token: str,
    expected_device_id: str,
    authorization: str | None,
    device_id: str | None,
    body: bytes,
) -> VerifiedJeebRelay:
    """Authenticate the Play-Store relay and derive a stable replay key.

    TLS protects the bearer token in transit. The stable body digest, Jeeb's
    transaction ID uniqueness and the wallet ledger idempotency key provide
    three independent replay barriers.
    """

    if len(token) < 48:
        raise JeebRelayAuthError("MacroDroid relay is not configured")
    if not device_id or not _DEVICE_ID_RE.fullmatch(device_id):
        raise JeebRelayAuthError("Invalid relay device ID")
    if not hmac.compare_digest(device_id, expected_device_id):
        raise JeebRelayAuthError("Unknown relay device")
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not supplied_token:
        raise JeebRelayAuthError("Missing relay bearer token")
    if not hmac.compare_digest(supplied_token, token):
        raise JeebRelayAuthError("Invalid relay bearer token")

    body_sha256 = hashlib.sha256(body).hexdigest()
    nonce_digest = hashlib.sha256(device_id.encode("utf-8") + b"\n" + body).digest()[:24]
    nonce = base64.urlsafe_b64encode(nonce_digest).decode("ascii").rstrip("=")
    return VerifiedJeebRelay(
        device_id=device_id,
        timestamp=0,
        nonce=nonce,
        body_sha256=body_sha256,
    )


def unpack_macrodroid_notification(body: bytes) -> tuple[str, str]:
    """Read the fixed plain-text envelope emitted by the supplied macro."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JeebNotificationParseError("ترميز إشعار جيب غير صالح") from exc
    package, separator, notification = text.partition("\n")
    if separator != "\n" or package.strip() != JEEB_ANDROID_PACKAGE:
        raise JeebNotificationParseError("مصدر الإشعار ليس تطبيق جيب الرسمي")
    notification = notification.replace("\x00", " ").strip()
    if not 8 <= len(notification) <= 8000:
        raise JeebNotificationParseError("طول إشعار جيب غير صالح")
    return package.strip(), notification


def parse_jeeb_notification(raw: str) -> ParsedJeebNotification:
    """Extract only an unambiguous successful incoming Jeeb transfer."""

    compact = raw.replace("\x00", " ").strip()
    if not 8 <= len(compact) <= 8000:
        raise JeebNotificationParseError("طول إشعار جيب غير صالح")
    if _SUCCESS_RE.search(compact) is None:
        raise JeebNotificationParseError("الإشعار ليس تحويل جيب واردًا ناجحًا")
    if _DEBIT_RE.search(compact) is not None:
        raise JeebNotificationParseError("تم رفض إشعار خصم أو دفع صادر")

    transaction_id = _capture(_TRANSACTION_RE, compact, "رقم العملية").upper()
    if re.fullmatch(r"[A-Z0-9_-]{3,200}", transaction_id) is None:
        raise JeebNotificationParseError("رقم عملية جيب غير صالح")

    amount_text = (
        _capture(_AMOUNT_RE, compact, "المبلغ")
        .translate(_DIGIT_TRANSLATION)
        .replace("٬", "")
        .replace(",", "")
    )
    try:
        amount = Decimal(amount_text)
    except InvalidOperation as exc:
        raise JeebNotificationParseError("مبلغ جيب غير صالح") from exc
    if amount <= 0 or amount > Decimal("1000000000000"):
        raise JeebNotificationParseError("مبلغ جيب خارج النطاق المسموح")

    sender_raw = _capture(_SENDER_RE, compact, "حساب المرسل")
    sender_account = "".join(
        character for character in sender_raw.translate(_DIGIT_TRANSLATION) if character.isdigit()
    )
    if not 6 <= len(sender_account) <= 20:
        raise JeebNotificationParseError("حساب مرسل جيب غير صالح")
    return ParsedJeebNotification(
        transaction_id=transaction_id,
        amount=amount,
        sender_account=sender_account,
    )


def _capture(pattern: re.Pattern[str], raw: str, label: str) -> str:
    match = pattern.search(raw)
    if match is None or not match.group(1):
        raise JeebNotificationParseError(f"لم يتم العثور على {label}")
    return match.group(1).strip()
