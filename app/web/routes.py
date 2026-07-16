from __future__ import annotations

import logging
import secrets

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.services.payments.binance import BinancePayError
from app.services.payments.service import PaymentError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.post("/webhooks/binance/{secret_path}")
async def binance_webhook(secret_path: str, request: Request) -> JSONResponse:
    settings = request.app.state.settings
    expected_secret = settings.webhook_secret_path.get_secret_value()
    if not secrets.compare_digest(secret_path, expected_secret):
        raise HTTPException(status_code=404, detail="Not found")
    client = request.app.state.binance
    if client is None:
        raise HTTPException(status_code=503, detail="Binance Pay is disabled")

    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise HTTPException(status_code=413, detail="Webhook body is too large")
    headers = request.headers
    required = {
        "timestamp": headers.get("BinancePay-Timestamp"),
        "nonce": headers.get("BinancePay-Nonce"),
        "signature": headers.get("BinancePay-Signature"),
        "certificate_sn": headers.get("BinancePay-Certificate-SN"),
    }
    if not all(required.values()):
        raise HTTPException(status_code=400, detail="Missing Binance Pay headers")
    try:
        event = await client.parse_and_verify_webhook(raw_body=raw_body, **required)
    except BinancePayError as exc:
        logger.warning("Rejected Binance Pay webhook: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid webhook") from exc

    payment = None
    mutation = None
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            payment, mutation = await request.app.state.payment_service.confirm_binance_event(
                session, event
            )
        except PaymentError as exc:
            # A verified amount/currency mismatch is persisted for manual review.
            await session.commit()
            logger.error("Verified Binance Pay event needs review: %s", exc)
            return JSONResponse({"returnCode": "SUCCESS", "returnMessage": None})
        await session.commit()

    if payment and mutation:
        bot: Bot | None = request.app.state.bot
        if bot:
            try:
                await bot.send_message(
                    payment.user_id,
                    f"✅ تم شحن <b>{payment.credit_amount:g} USDT</b> تلقائيًا عبر Binance Pay.\n"
                    f"رصيدك الجديد: <b>{mutation.balance_after:g} USDT</b>",
                )
            except Exception:
                logger.exception("Could not notify user about Binance Pay credit")
    return JSONResponse({"returnCode": "SUCCESS", "returnMessage": None})
