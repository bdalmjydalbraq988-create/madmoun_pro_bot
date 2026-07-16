from __future__ import annotations

import pytest

from app.bot import build_dispatcher
from app.config import Settings
from app.crypto import PayloadCipher
from app.services.orders import OrderService
from app.services.payments.service import PaymentService


@pytest.mark.asyncio
async def test_dispatcher_and_all_routers_build(session_factory) -> None:
    cipher = PayloadCipher(PayloadCipher.generate_key())
    settings = Settings(admin_ids=[999], data_encryption_key=PayloadCipher.generate_key())
    dispatcher = build_dispatcher(
        settings=settings,
        session_factory=session_factory,
        cipher=cipher,
        order_service=OrderService(cipher),
        payment_service=PaymentService(),
        binance=None,
    )
    assert {router.name for router in dispatcher.sub_routers} == {
        "common",
        "admin",
        "user",
    }
    await dispatcher.storage.close()
