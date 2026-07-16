from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException

from app.bootstrap import seed_defaults
from app.bot import build_bot, build_dispatcher
from app.config import get_settings
from app.crypto import PayloadCipher
from app.db import create_engine, create_session_factory
from app.models import Base, Order
from app.services.orders import OrderProcessor, OrderService
from app.services.payments.binance import BinancePayClient
from app.services.payments.service import PaymentService
from app.services.providers.quantumvault import VenteBotProvider
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
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
    payment_service = PaymentService()
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
            text = (
                f"✅ اكتمل طلبك <code>{order.public_code}</code>.\n"
                f"اعرض التسليم بالأمر /delivery_{order.public_code}"
            )
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
        if polling_task:
            polling_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
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
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.include_router(web_router)


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
