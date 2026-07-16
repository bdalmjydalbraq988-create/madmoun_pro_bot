from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.crypto import PayloadCipher
from app.enums import FulfillmentMode, LedgerKind, OrderStatus
from app.models import Category, LedgerEntry, Product, User, Wallet
from app.services.orders import OrderService
from app.services.wallet import WalletService


@pytest.mark.asyncio
async def test_order_purchase_and_refund_are_each_applied_once(session_factory) -> None:
    cipher = PayloadCipher(PayloadCipher.generate_key())
    wallet_service = WalletService()
    order_service = OrderService(cipher, wallet_service)

    async with session_factory() as session:
        user = User(telegram_id=201, display_name="Buyer")
        wallet = Wallet(user_id=201)
        category = Category(name_ar="AI")
        session.add_all([user, wallet, category])
        await session.flush()
        product = Product(
            category_id=category.id,
            name_ar="Gemini Pro",
            sale_price=Decimal("5"),
            fulfillment_mode=FulfillmentMode.MANUAL,
            customer_input_label="Email",
            is_active=True,
        )
        session.add(product)
        await session.flush()
        await wallet_service.credit(
            session,
            user_id=201,
            amount=Decimal("20"),
            kind=LedgerKind.ADMIN_CREDIT,
            idempotency_key="initial-credit",
            reference_type="test",
            reference_id="1",
        )
        await session.commit()

        order = await order_service.place_order(
            session,
            user_id=201,
            product_id=product.id,
            customer_input="buyer@example.com",
            idempotency_key="same-purchase",
        )
        replay = await order_service.place_order(
            session,
            user_id=201,
            product_id=product.id,
            customer_input="buyer@example.com",
            idempotency_key="same-purchase",
        )
        await session.commit()
        assert replay.id == order.id
        assert order.status is OrderStatus.REVIEW_REQUIRED
        assert cipher.decrypt(order.customer_input_encrypted) == "buyer@example.com"
        assert (await session.get(Wallet, 201)).balance == Decimal("15.00000000")

        await order_service.refund(session, order=order, actor_user_id=999, reason="test refund")
        await order_service.refund(
            session, order=order, actor_user_id=999, reason="test refund replay"
        )
        await session.commit()
        assert order.status is OrderStatus.REFUNDED
        assert (await session.get(Wallet, 201)).balance == Decimal("20.00000000")
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 3
