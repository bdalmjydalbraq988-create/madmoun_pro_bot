from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services.payments.binance import BinancePayClient, BinancePayError


def test_binance_request_signature_uses_exact_payload() -> None:
    client = BinancePayClient(api_key="api", secret_key="secret")
    timestamp = "1670000000000"
    nonce = "A" * 32
    body = '{"merchantTradeNo":"P123","orderAmount":"1.00"}'
    payload = f"{timestamp}\n{nonce}\n{body}\n"
    expected = hmac.new(b"secret", payload.encode(), hashlib.sha512).hexdigest().upper()
    assert client.sign(timestamp, nonce, body) == expected


@pytest.mark.asyncio
async def test_binance_webhook_rsa_signature_and_payload_are_verified() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    client = BinancePayClient(api_key="api", secret_key="secret", webhook_tolerance_seconds=60)
    client._certificates["certificate-1"] = public_pem
    body = json.dumps(
        {
            "bizType": "PAY",
            "bizStatus": "PAY_SUCCESS",
            "data": json.dumps(
                {
                    "merchantTradeNo": "P123",
                    "transactionId": "TX1",
                    "totalFee": "2.50",
                    "currency": "USDT",
                },
                separators=(",", ":"),
            ),
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time() * 1000))
    nonce = "B" * 32
    payload = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    signature = base64.b64encode(
        private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    ).decode()

    event = await client.parse_and_verify_webhook(
        raw_body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        certificate_sn="certificate-1",
    )
    assert event.merchant_trade_no == "P123"
    assert event.transaction_id == "TX1"
    assert str(event.total_fee) == "2.50"

    with pytest.raises(BinancePayError):
        await client.parse_and_verify_webhook(
            raw_body=body + b" ",
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
            certificate_sn="certificate-1",
        )
    await client.close()
