from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import ErrorEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.crypto import PayloadCipher
from app.handlers.admin import build_admin_router
from app.handlers.common import build_common_router
from app.handlers.user import build_user_router
from app.middleware import DatabaseSessionMiddleware
from app.services.orders import OrderService
from app.services.payments.binance import BinancePayClient
from app.services.payments.service import PaymentService

logger = logging.getLogger(__name__)


def build_storage(settings: Settings) -> BaseStorage:
    if settings.is_production:
        return RedisStorage.from_url(settings.redis_url)
    return MemoryStorage()


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cipher: PayloadCipher,
    order_service: OrderService,
    payment_service: PaymentService,
    binance: BinancePayClient | None,
) -> Dispatcher:
    dispatcher = Dispatcher(storage=build_storage(settings))
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware(session_factory))
    dispatcher.include_router(build_common_router(settings))
    dispatcher.include_router(
        build_admin_router(
            settings=settings,
            cipher=cipher,
            order_service=order_service,
            payment_service=payment_service,
        )
    )
    dispatcher.include_router(
        build_user_router(
            settings=settings,
            order_service=order_service,
            payment_service=payment_service,
            cipher=cipher,
            binance=binance,
        )
    )

    @dispatcher.error()
    async def on_error(event: ErrorEvent) -> bool:
        logger.exception("Unhandled Telegram update error", exc_info=event.exception)
        update = event.update
        message = update.message or (update.callback_query and update.callback_query.message)
        if message:
            try:
                await message.answer(
                    "حدث خطأ داخلي ولم تُكرر العملية المالية. حاول لاحقًا أو أرسل رقم الطلب للدعم."
                )
            except Exception:
                logger.exception("Could not send error notice")
        return True

    return dispatcher
