from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.crypto import PayloadCipher
from app.enums import FulfillmentMode, LedgerKind, OrderStatus, ProviderResultStatus
from app.models import Category, LedgerEntry, Product, User, Wallet
from app.services.orders import OrderProcessor, OrderService
from app.services.providers.base import ProvisionResult
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


class PendingThenCompletedProvider:
    code = "ventebot"

    def __init__(self) -> None:
        self.create_calls = 0
        self.get_calls = 0

    async def create_order(self, request) -> ProvisionResult:
        self.create_calls += 1
        return ProvisionResult(
            status=ProviderResultStatus.PENDING,
            external_order_id="supplier-77",
            provider_status="awaiting_activation",
        )

    async def get_order(self, external_order_id: str) -> ProvisionResult:
        self.get_calls += 1
        assert external_order_id == "supplier-77"
        return ProvisionResult(
            status=ProviderResultStatus.COMPLETED,
            external_order_id=external_order_id,
            provider_status="completed",
            delivery="تم التفعيل بنجاح",
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_pending_supplier_order_is_polled_without_duplicate_purchase(
    session_factory,
) -> None:
    cipher = PayloadCipher(PayloadCipher.generate_key())
    wallet_service = WalletService()
    order_service = OrderService(cipher, wallet_service)
    provider = PendingThenCompletedProvider()

    async with session_factory() as session:
        session.add_all(
            [
                User(telegram_id=301, display_name="Buyer"),
                Wallet(user_id=301),
                Category(name_ar="AI"),
            ]
        )
        await session.flush()
        category = await session.scalar(select(Category).where(Category.name_ar == "AI"))
        product = Product(
            category_id=category.id,
            name_ar="Gemini Pro",
            sale_price=Decimal("5"),
            fulfillment_mode=FulfillmentMode.AUTO,
            provider_code="ventebot",
            provider_product_id="12",
            customer_input_label="Email",
            is_active=True,
        )
        session.add(product)
        await session.flush()
        await wallet_service.credit(
            session,
            user_id=301,
            amount=Decimal("20"),
            kind=LedgerKind.ADMIN_CREDIT,
            idempotency_key="pending-initial-credit",
            reference_type="test",
            reference_id="2",
        )
        order = await order_service.place_order(
            session,
            user_id=301,
            product_id=product.id,
            customer_input="buyer@example.com",
            idempotency_key="pending-purchase",
        )
        await session.commit()

    processor = OrderProcessor(
        session_factory=session_factory,
        order_service=order_service,
        providers={"ventebot": provider},
    )
    assert await processor.process_next()
    async with session_factory() as session:
        order = await session.get(type(order), order.id)
        assert order.status is OrderStatus.PROVIDER_PENDING
        order.next_retry_at = None
        await session.commit()

    assert await processor.process_next()
    async with session_factory() as session:
        order = await session.get(type(order), order.id)
        assert order.status is OrderStatus.COMPLETED
        assert cipher.decrypt(order.delivery_encrypted) == "تم التفعيل بنجاح"
        assert (await session.get(Wallet, 301)).balance == Decimal("15.00000000")

    assert provider.create_calls == 1
    assert provider.get_calls == 1
