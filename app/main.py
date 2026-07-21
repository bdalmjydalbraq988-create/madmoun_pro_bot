from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import __version__
from app.bootstrap import seed_defaults
from app.bot import build_bot, build_dispatcher
from app.config import get_settings
from app.crypto import PayloadCipher
from app.db import create_engine, create_session_factory
from app.formatting import money_label
from app.keyboards import delivery_keyboard
from app.models import Base, LedgerEntry, Order, Referral
from app.services.delivery import delivery_html, is_placeholder_delivery
from app.services.orders import OrderProcessor, OrderService
from app.services.payments.binance import BinancePayClient
from app.services.payments.jeeb_relay import JeebRelayAuthError, verify_jeeb_relay_request
from app.services.payments.service import JeebPaymentEvent, PaymentError, PaymentService
from app.services.providers.quantumvault import VenteBotProvider
from app.services.supplier_catalog import sync_supplier_catalog
from app.web.routes import router as web_router

logging.basicConfig(
    level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


async def worker_loop(processor: OrderProcessor, interval: float) -> None:
    while True:
        try:
            processed = await processor.process_next()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Order worker iteration failed")
            processed = False
        if not processed:
            await asyncio.sleep(interval)


async def supplier_catalog_loop(
    *,
    session_factory,
    provider: VenteBotProvider,
    interval_minutes: int,
) -> None:
    """Refresh prices, stock and images without blocking order processing."""
    interval = max(5, interval_minutes) * 60
    while True:
        try:
            async with session_factory() as session:
                result = await sync_supplier_catalog(
                    session,
                    provider=provider,
                    actor_user_id=None,
                )
                await session.commit()
                logger.info(
                    "Automatic supplier catalog sync completed: "
                    "received=%s created=%s updated=%s deactivated=%s skipped=%s",
                    result.received,
                    result.created,
                    result.updated,
                    result.deactivated,
                    result.skipped,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic supplier catalog sync failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "[MADMOUN RELEASE %s] Starting with ADMIN_IDS=%s",
        __version__,
        settings.admin_ids,
    )
    if not settings.data_encryption_key.get_secret_value():
        raise RuntimeError(
            "DATA_ENCRYPTION_KEY is required. Run `python -m app.cli generate-key` "
            "and add it to .env."
        )

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    # Initial schema bootstrap. Alembic owns subsequent production migrations.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        await seed_defaults(session, settings)

    cipher = PayloadCipher(settings.data_encryption_key.get_secret_value())
    payment_service = PaymentService(
        jeeb_intent_ttl_minutes=settings.jeeb_intent_ttl_minutes,
    )
    order_service = OrderService(cipher)
    binance = None
    if settings.binance_pay_enabled:
        binance = BinancePayClient(
            api_key=settings.binance_pay_api_key.get_secret_value(),
            secret_key=settings.binance_pay_secret_key.get_secret_value(),
            base_url=settings.binance_pay_base_url,
            webhook_tolerance_seconds=settings.binance_webhook_tolerance_seconds,
        )

    providers = {}
    if settings.supplier_enabled:
        providers["ventebot"] = VenteBotProvider(
            base_url=settings.supplier_base_url,
            api_key=settings.supplier_api_key.get_secret_value(),
            me_path=settings.supplier_me_path,
            products_path=settings.supplier_products_path,
            quote_path=settings.supplier_quote_path,
            create_order_path=settings.supplier_create_order_path,
            status_path=settings.supplier_order_status_path,
            activation_identifier_path=settings.supplier_activation_identifier_path,
        )

    bot = None
    dispatcher = None
    polling_task = None
    token = settings.bot_token.get_secret_value()
    if token:
        bot = build_bot(settings)
        dispatcher = build_dispatcher(
            settings=settings,
            session_factory=session_factory,
            cipher=cipher,
            order_service=order_service,
            payment_service=payment_service,
            binance=binance,
        )

    async def notify_order(order: Order, event: str) -> None:
        if not bot:
            return
        if event == "completed":
            async with session_factory() as session:
                purchase_entry = await session.scalar(
                    select(LedgerEntry).where(
                        LedgerEntry.idempotency_key == f"order:purchase:{order.id}"
                    )
                )
                referral = await session.scalar(
                    select(Referral).where(
                        Referral.qualified_order_id == order.id,
                        Referral.rewarded_at.is_not(None),
                    )
                )
            paid = abs(purchase_entry.amount) if purchase_entry is not None else order.total_amount
            remaining = (
                money_label(purchase_entry.balance_after, order.currency)
                if purchase_entry is not None
                else "راجع محفظتك"
            )
            before_line = (
                f"الرصيد قبل: <b>{money_label(purchase_entry.balance_before, order.currency)}</b>\n"
                if purchase_entry is not None
                else ""
            )
            text = (
                f"✅ <b>اكتمل طلبك</b> <code>{order.public_code}</code>\n"
                f"الخدمة: {html.escape(order.product_name_snapshot)}\n\n"
                f"{before_line}"
                f"تم الخصم: <b>{money_label(paid, order.currency)}</b>\n"
                f"الرصيد المتبقي: <b>{remaining}</b>"
            )
            await bot.send_message(order.user_id, text)
            if referral is not None:
                if referral.invitee_reward_amount > 0:
                    await bot.send_message(
                        order.user_id,
                        "🎉 أُضيفت هدية الإحالة إلى رصيدك: "
                        f"<b>{money_label(referral.invitee_reward_amount)}</b>",
                    )
                if referral.referrer_reward_amount > 0:
                    await bot.send_message(
                        referral.referrer_id,
                        "🎁 اكتملت أول عملية شراء لأحد المدعوين، وأُضيفت مكافأتك: "
                        f"<b>{money_label(referral.referrer_reward_amount)}</b>",
                    )
            delivery = (
                cipher.decrypt(order.delivery_encrypted) if order.delivery_encrypted else None
            )
            if not is_placeholder_delivery(delivery):
                await bot.send_message(
                    order.user_id,
                    f"📨 <b>بيانات التسليم</b>\n\n{delivery_html(delivery)}\n"
                    "اضغط على النص لنسخه أو افتح الرابط من الزر.",
                    reply_markup=delivery_keyboard(delivery),
                )
            return
        elif event == "refunded":
            text = f"↩️ تعذر تنفيذ الطلب <code>{order.public_code}</code> وأُعيد المبلغ إلى رصيدك."
        elif event == "review_required":
            text = (
                f"طلبك <code>{order.public_code}</code> تحت مراجعة الإدارة، ولا يلزمك الدفع مجددًا."
            )
        else:
            text = f"طلبك <code>{order.public_code}</code> قيد التنفيذ لدى المورد."
        await bot.send_message(order.user_id, text)
        if event == "review_required":
            for admin_id in settings.admin_ids:
                await bot.send_message(
                    admin_id,
                    f"⚠️ الطلب <code>{order.public_code}</code> يحتاج مراجعة. افتح لوحة الأدمن.",
                )

    processor = OrderProcessor(
        session_factory=session_factory,
        order_service=order_service,
        providers=providers,
        max_retries=settings.order_max_retries,
        notifier=notify_order,
    )
    recovered = await processor.recover_stale_processing()
    if recovered:
        logger.warning("Moved %s interrupted supplier orders to manual review", recovered)
    worker_task = asyncio.create_task(
        worker_loop(processor, settings.order_worker_interval_seconds),
        name="order-worker",
    )
    supplier_sync_task = None
    supplier_provider = providers.get("ventebot")
    if isinstance(supplier_provider, VenteBotProvider):
        supplier_sync_task = asyncio.create_task(
            supplier_catalog_loop(
                session_factory=session_factory,
                provider=supplier_provider,
                interval_minutes=settings.supplier_catalog_sync_minutes,
            ),
            name="supplier-catalog-sync",
        )
    if bot and dispatcher and settings.is_production:
        webhook_secret = settings.webhook_secret_path.get_secret_value()
        await bot.set_webhook(
            f"{settings.public_base_url}/telegram/{webhook_secret}",
            secret_token=webhook_secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    elif bot and dispatcher:
        polling_task = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False), name="telegram-polling"
        )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.cipher = cipher
    app.state.payment_service = payment_service
    app.state.order_service = order_service
    app.state.binance = binance
    app.state.bot = bot
    app.state.dispatcher = dispatcher

    try:
        yield
    finally:
        worker_task.cancel()
        if supplier_sync_task:
            supplier_sync_task.cancel()
        if polling_task:
            polling_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        if supplier_sync_task:
            with contextlib.suppress(asyncio.CancelledError):
                await supplier_sync_task
        if polling_task:
            with contextlib.suppress(asyncio.CancelledError):
                await polling_task
        if dispatcher:
            await dispatcher.storage.close()
        if bot:
            await bot.session.close()
        if binance:
            await binance.close()
        for provider in providers.values():
            await provider.close()
        await engine.dispose()


app = FastAPI(
    title="Digital Store Bot",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.include_router(web_router)


class JeebRelayPayload(BaseModel):
    transaction_id: str = Field(min_length=3, max_length=200)
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="YER", min_length=2, max_length=8)
    sender_account: str = Field(min_length=6, max_length=40)
    occurred_at: datetime


@app.post("/webhooks/jeeb-relay", include_in_schema=False)
async def jeeb_notification_relay(
    request: Request,
    payload: JeebRelayPayload,
    x_jeeb_relay_version: str | None = Header(default=None),
    x_jeeb_device_id: str | None = Header(default=None),
    x_jeeb_timestamp: str | None = Header(default=None),
    x_jeeb_nonce: str | None = Header(default=None),
    x_jeeb_signature: str | None = Header(default=None),
) -> dict[str, bool | str]:
    settings = app.state.settings
    if not settings.jeeb_auto_confirm_enabled:
        raise HTTPException(status_code=503, detail="Jeeb auto confirmation is disabled")
    raw_body = await request.body()
    if len(raw_body) > 4096:
        raise HTTPException(status_code=413, detail="Relay payload is too large")
    try:
        verified = verify_jeeb_relay_request(
            secret=settings.jeeb_relay_secret.get_secret_value(),
            expected_device_id=settings.jeeb_relay_device_id,
            body=raw_body,
            version=x_jeeb_relay_version,
            device_id=x_jeeb_device_id,
            timestamp=x_jeeb_timestamp,
            nonce=x_jeeb_nonce,
            signature=x_jeeb_signature,
            tolerance_seconds=settings.jeeb_relay_tolerance_seconds,
        )
    except JeebRelayAuthError as exc:
        logger.warning("Rejected Jeeb relay request: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="Invalid Jeeb relay authentication",
        ) from exc

    if payload.occurred_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="occurred_at must include a timezone")
    now = datetime.now(UTC)
    occurred_at = payload.occurred_at.astimezone(UTC)
    if occurred_at > now + timedelta(minutes=10):
        raise HTTPException(status_code=409, detail="Jeeb event timestamp is in the future")
    if occurred_at < now - timedelta(hours=settings.jeeb_event_max_age_hours):
        raise HTTPException(status_code=409, detail="Jeeb event is too old")

    async with app.state.session_factory() as session:
        try:
            _, payment, mutation = await app.state.payment_service.receive_jeeb_event(
                session,
                JeebPaymentEvent(
                    transaction_id=payload.transaction_id,
                    amount=payload.amount,
                    currency=payload.currency,
                    sender_account=payload.sender_account,
                    occurred_at=occurred_at,
                    source_device_id=verified.device_id,
                    relay_nonce=verified.nonce,
                    payload_sha256=verified.body_sha256,
                ),
            )
            await session.commit()
        except PaymentError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if payment is not None and mutation is not None and not mutation.was_replayed:
        bot = app.state.bot
        if bot is not None:
            try:
                await bot.send_message(
                    payment.user_id,
                    f"✅ تم تأكيد شحن جيب <code>{payment.public_code}</code> تلقائيًا.\n"
                    f"الرصيد الجديد: <b>{money_label(mutation.balance_after)}</b>",
                )
            except Exception:
                logger.exception("Could not notify customer about confirmed Jeeb payment")
    elif payment is not None and payment.status.value == "review_required":
        bot = app.state.bot
        if bot is not None:
            try:
                await bot.send_message(
                    payment.user_id,
                    f"⚠️ شحن جيب <code>{payment.public_code}</code> لم يطابق جميع البيانات. "
                    "لم يُضف أي رصيد وأُرسل الطلب للمراجعة.",
                )
                for admin_id in settings.admin_ids:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ شحن جيب <code>{payment.public_code}</code> يحتاج مراجعة. "
                        "لم تتم إضافة الرصيد تلقائيًا.",
                    )
            except Exception:
                logger.exception("Could not notify about Jeeb payment mismatch")
    return {
        "ok": True,
        "matched": payment is not None,
        "status": payment.status.value if payment is not None else "awaiting_customer_claim",
    }


@app.get("/webhooks/jeeb-relay/health", include_in_schema=False)
async def jeeb_relay_health() -> dict[str, bool | str]:
    """Non-sensitive readiness check used by the owner's relay phone."""

    return {
        "ok": True,
        "enabled": app.state.settings.jeeb_auto_confirm_enabled,
        "protocol": "hmac-sha256-v1",
    }


@app.post("/telegram/{path_secret}", include_in_schema=False)
async def telegram_webhook(
    path_secret: str,
    update: Update,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    settings = app.state.settings
    expected = settings.webhook_secret_path.get_secret_value()
    if path_secret != expected or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
    bot = app.state.bot
    dispatcher = app.state.dispatcher
    if bot is None or dispatcher is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    await dispatcher.feed_update(bot, update)
    return {"ok": True}
