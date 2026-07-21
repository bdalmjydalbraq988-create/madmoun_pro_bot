from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.crypto import PayloadCipher
from app.enums import FulfillmentMode, LedgerKind, OrderStatus
from app.models import (
    Category,
    LedgerEntry,
    Product,
    Referral,
    ReferralProgramConfig,
    User,
    Wallet,
)
from app.services.orders import OrderService
from app.services.referrals import ReferralService
from app.services.wallet import WalletService


@pytest.mark.asyncio
async def test_referral_registration_is_new_user_only_and_immutable(session_factory) -> None:
    service = ReferralService()
    async with session_factory() as session:
        session.add_all(
            [
                User(telegram_id=100, display_name="Inviter"),
                Wallet(user_id=100),
                User(telegram_id=200, display_name="New customer"),
                Wallet(user_id=200),
                User(telegram_id=300, display_name="Other inviter"),
                Wallet(user_id=300),
            ]
        )
        await session.flush()

        assert await service.register(session, invitee_id=200, referrer_id=100)
        assert not await service.register(session, invitee_id=200, referrer_id=300)
        assert not await service.register(session, invitee_id=100, referrer_id=100)
        assert not await service.register(session, invitee_id=400, referrer_id=100)
        await session.commit()

        referral = await session.get(Referral, 200)
        assert referral.referrer_id == 100
        assert await session.scalar(select(func.count()).select_from(Referral)) == 1


@pytest.mark.asyncio
async def test_referral_rewards_first_completed_order_exactly_once(session_factory) -> None:
    cipher = PayloadCipher(PayloadCipher.generate_key())
    wallet_service = WalletService()
    referral_service = ReferralService(wallet_service)
    order_service = OrderService(cipher, wallet_service, referral_service)

    async with session_factory() as session:
        session.add_all(
            [
                User(telegram_id=101, display_name="Inviter"),
                Wallet(user_id=101),
                User(telegram_id=202, display_name="Invitee"),
                Wallet(user_id=202),
                Category(name_ar="AI"),
                ReferralProgramConfig(
                    code="default",
                    enabled=True,
                    referrer_reward=Decimal("0.50"),
                    invitee_reward=Decimal("0.25"),
                    minimum_order_amount=Decimal("3"),
                ),
            ]
        )
        await session.flush()
        category = await session.scalar(select(Category).where(Category.name_ar == "AI"))
        product = Product(
            category_id=category.id,
            name_ar="Gemini Pro",
            sale_price=Decimal("5"),
            fulfillment_mode=FulfillmentMode.MANUAL,
            is_active=True,
        )
        session.add(product)
        await session.flush()
        assert await referral_service.register(session, invitee_id=202, referrer_id=101)
        await wallet_service.credit(
            session,
            user_id=202,
            amount=Decimal("10"),
            kind=LedgerKind.ADMIN_CREDIT,
            idempotency_key="invitee-initial-credit",
            reference_type="test",
            reference_id="referral",
        )
        order = await order_service.place_order(
            session,
            user_id=202,
            product_id=product.id,
            customer_input="buyer@example.com",
            idempotency_key="referral-first-order",
        )
        await session.commit()

        completed = await order_service.complete_manual(
            session,
            order_id=order.id,
            admin_id=999,
            delivery="delivered",
        )
        assert completed.status is OrderStatus.COMPLETED
        replay = await referral_service.award_completed_order(session, order=completed)
        assert not replay.applied
        await session.commit()

        referral = await session.get(Referral, 202)
        assert referral.qualified_order_id == order.id
        assert referral.referrer_reward_amount == Decimal("0.50000000")
        assert referral.invitee_reward_amount == Decimal("0.25000000")
        assert (await session.get(Wallet, 101)).balance == Decimal("0.50000000")
        assert (await session.get(Wallet, 202)).balance == Decimal("5.25000000")
        referral_entries = await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.reference_type == "referral")
        )
        assert referral_entries == 2
