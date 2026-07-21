from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.enums import PaymentKind, PaymentStatus
from app.models import LedgerEntry, PaymentChannel, User, Wallet
from app.services.payments.service import JeebPaymentEvent, PaymentError, PaymentService


@pytest.mark.asyncio
async def test_manual_payment_approval_is_idempotent(session_factory) -> None:
    service = PaymentService()
    async with session_factory() as session:
        user = User(telegram_id=301, display_name="Depositor")
        wallet = Wallet(user_id=301)
        channel = PaymentChannel(
            code="jeeb",
            name_ar="جيب",
            kind=PaymentKind.MANUAL,
            settlement_currency="YER",
            units_per_usdt=Decimal("2500"),
            fee_percent=Decimal("3"),
            min_credit=Decimal("1"),
            max_credit=Decimal("100"),
            is_active=True,
        )
        session.add_all([user, wallet, channel])
        await session.commit()

        quote = service.quote(channel, Decimal("10"))
        assert quote.expected_amount == Decimal("25750")
        payment = await service.create_pending(
            session,
            user_id=301,
            channel=channel,
            quote=quote,
            payer_reference="REF-1",
            proof_file_id="telegram-file-id",
        )
        await session.commit()

        with pytest.raises(PaymentError, match="مستخدم"):
            await service.create_pending(
                session,
                user_id=301,
                channel=channel,
                quote=quote,
                payer_reference="REF-1",
                proof_file_id="another-file-id",
            )

        payment, first = await service.approve_manual(session, payment_id=payment.id, admin_id=999)
        payment, replay = await service.approve_manual(session, payment_id=payment.id, admin_id=999)
        await session.commit()
        assert payment.status is PaymentStatus.CONFIRMED
        assert first.balance_after == Decimal("10.00000000")
        assert replay.was_replayed is True
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1

        with pytest.raises(PaymentError):
            await service.reject_manual(
                session, payment_id=payment.id, admin_id=999, reason="too late"
            )


@pytest.mark.asyncio
async def test_trusted_jeeb_event_matches_and_credits_exactly_once(session_factory) -> None:
    service = PaymentService()
    async with session_factory() as session:
        session.add_all(
            [
                User(telegram_id=401, display_name="Depositor"),
                Wallet(user_id=401),
                PaymentChannel(
                    code="jeeb",
                    name_ar="جيب",
                    kind=PaymentKind.MANUAL,
                    settlement_currency="YER",
                    units_per_usdt=Decimal("2500"),
                    fee_percent=Decimal("0"),
                    min_credit=Decimal("1"),
                    max_credit=Decimal("100"),
                    is_active=True,
                ),
            ]
        )
        await session.commit()
        channel = await session.get(PaymentChannel, "jeeb")
        quote = service.quote(channel, Decimal("10"))
        payment, mutation = await service.create_jeeb_pending(
            session,
            user_id=401,
            channel=channel,
            quote=quote,
            transaction_id=" tx-100 ",
            payer_account="777 123 456",
        )
        assert mutation is None
        assert payment.status is PaymentStatus.PENDING

        event = JeebPaymentEvent(
            transaction_id="TX-100",
            amount=Decimal("25000"),
            currency="yer",
            sender_account="777123456",
        )
        _, confirmed, first = await service.receive_jeeb_event(session, event)
        _, replayed, replay = await service.receive_jeeb_event(session, event)
        await session.commit()

        assert confirmed.id == payment.id == replayed.id
        assert confirmed.status is PaymentStatus.CONFIRMED
        assert first.balance_after == Decimal("10.00000000")
        assert replay.was_replayed is True
        assert (await session.get(Wallet, 401)).balance == Decimal("10.00000000")
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1


@pytest.mark.asyncio
async def test_jeeb_event_can_arrive_before_claim_and_mismatch_never_credits(
    session_factory,
) -> None:
    service = PaymentService()
    async with session_factory() as session:
        session.add_all(
            [
                User(telegram_id=402, display_name="Depositor"),
                Wallet(user_id=402),
                PaymentChannel(
                    code="jeeb",
                    name_ar="جيب",
                    kind=PaymentKind.MANUAL,
                    settlement_currency="YER",
                    units_per_usdt=Decimal("2500"),
                    fee_percent=Decimal("0"),
                    min_credit=Decimal("1"),
                    max_credit=Decimal("100"),
                    is_active=True,
                ),
            ]
        )
        await session.commit()
        event = JeebPaymentEvent(
            transaction_id="TX-200",
            amount=Decimal("25000"),
            currency="YER",
            sender_account="777123456",
        )
        _, unmatched, mutation = await service.receive_jeeb_event(session, event)
        assert unmatched is None and mutation is None

        channel = await session.get(PaymentChannel, "jeeb")
        quote = service.quote(channel, Decimal("10"))
        payment, mutation = await service.create_jeeb_pending(
            session,
            user_id=402,
            channel=channel,
            quote=quote,
            transaction_id="TX-200",
            payer_account="777000000",
        )
        await session.commit()
        assert mutation is None
        assert payment.status is PaymentStatus.REVIEW_REQUIRED
        assert (await session.get(Wallet, 402)).balance == Decimal("0E-8")
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
