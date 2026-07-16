from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class BinancePayError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BinanceCheckout:
    prepay_id: str
    checkout_url: str
    expire_time_ms: int
    currency: str
    total_fee: Decimal


@dataclass(frozen=True, slots=True)
class BinancePaymentEvent:
    merchant_trade_no: str
    transaction_id: str | None
    status: str
    total_fee: Decimal
    currency: str


class BinancePayClient:
    """Official Binance Pay Merchant API client.

    Request signing uses HMAC-SHA512. Incoming webhooks use Binance's RSA
    certificate and are verified before any database write.
    """

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str = "https://bpay.binanceapi.com",
        webhook_tolerance_seconds: int = 300,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.webhook_tolerance_seconds = webhook_tolerance_seconds
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0), trust_env=False
        )
        self._owns_client = client is None
        self._certificates: dict[str, str] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _nonce() -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(32))

    def sign(self, timestamp_ms: str, nonce: str, body: str) -> str:
        payload = f"{timestamp_ms}\n{nonce}\n{body}\n"
        return (
            hmac.new(
                self.secret_key.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha512,
            )
            .hexdigest()
            .upper()
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        nonce = self._nonce()
        headers = {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": timestamp,
            "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": self.api_key,
            "BinancePay-Signature": self.sign(timestamp, nonce, body),
        }
        try:
            response = await self._client.post(
                f"{self.base_url}{path}", content=body, headers=headers
            )
            response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BinancePayError("Binance Pay request outcome is uncertain") from exc
        except httpx.HTTPStatusError as exc:
            raise BinancePayError(f"Binance Pay HTTP error: {exc.response.status_code}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise BinancePayError("Binance Pay returned invalid JSON") from exc
        if data.get("status") != "SUCCESS":
            code = str(data.get("code", "UNKNOWN"))
            message = str(data.get("errorMessage", "Binance Pay rejected the request"))
            raise BinancePayError(f"{code}: {message}")
        return data

    async def create_order(
        self,
        *,
        merchant_trade_no: str,
        amount: Decimal,
        currency: str,
        description: str,
        product_name: str,
        webhook_url: str,
    ) -> BinanceCheckout:
        data = await self._post(
            "/binancepay/openapi/v3/order",
            {
                "env": {"terminalType": "APP"},
                "merchantTradeNo": merchant_trade_no,
                "orderAmount": format(amount, "f"),
                "currency": currency.upper(),
                "description": description[:256],
                "goodsDetails": [
                    {
                        "goodsType": "02",
                        "goodsCategory": "6000",
                        "referenceGoodsId": "WALLET_TOPUP",
                        "goodsName": product_name[:256],
                    }
                ],
                "passThroughInfo": merchant_trade_no,
                "webhookUrl": webhook_url,
            },
        )
        result = data["data"]
        return BinanceCheckout(
            prepay_id=str(result["prepayId"]),
            checkout_url=str(result["checkoutUrl"]),
            expire_time_ms=int(result["expireTime"]),
            currency=str(result["currency"]),
            total_fee=Decimal(str(result["totalFee"])),
        )

    async def query_order(self, merchant_trade_no: str) -> dict[str, Any]:
        data = await self._post(
            "/binancepay/openapi/v2/order/query",
            {"merchantTradeNo": merchant_trade_no},
        )
        return dict(data["data"])

    async def _load_certificate(self, certificate_sn: str) -> str:
        cached = self._certificates.get(certificate_sn)
        if cached:
            return cached
        data = await self._post("/binancepay/openapi/certificates", {})
        for item in data.get("data", []):
            sn = str(item.get("certSerial"))
            cert_public = str(item.get("certPublic", ""))
            if sn and cert_public:
                self._certificates[sn] = cert_public
        certificate = self._certificates.get(certificate_sn)
        if not certificate:
            raise BinancePayError("Webhook certificate was not returned by Binance Pay")
        return certificate

    async def parse_and_verify_webhook(
        self,
        *,
        raw_body: bytes,
        timestamp: str,
        nonce: str,
        signature: str,
        certificate_sn: str,
    ) -> BinancePaymentEvent:
        try:
            timestamp_ms = int(timestamp)
        except ValueError as exc:
            raise BinancePayError("Invalid webhook timestamp") from exc
        age_ms = abs(int(time.time() * 1000) - timestamp_ms)
        if age_ms > self.webhook_tolerance_seconds * 1000:
            raise BinancePayError("Webhook timestamp is outside the accepted window")

        certificate = await self._load_certificate(certificate_sn)
        try:
            public_key = serialization.load_pem_public_key(certificate.encode("utf-8"))
            payload = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + raw_body + b"\n"
            public_key.verify(
                base64.b64decode(signature, validate=True),
                payload,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise BinancePayError("Webhook signature verification failed") from exc

        try:
            envelope = json.loads(raw_body)
            details = json.loads(envelope["data"])
        except (ValueError, KeyError, TypeError) as exc:
            raise BinancePayError("Invalid Binance Pay webhook payload") from exc
        if envelope.get("bizType") != "PAY":
            raise BinancePayError("Unsupported Binance Pay webhook type")
        return BinancePaymentEvent(
            merchant_trade_no=str(details["merchantTradeNo"]),
            transaction_id=(
                str(details["transactionId"]) if details.get("transactionId") else None
            ),
            status=str(envelope.get("bizStatus", "")),
            total_fee=Decimal(str(details["totalFee"])),
            currency=str(details["currency"]).upper(),
        )
