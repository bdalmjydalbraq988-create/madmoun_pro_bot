from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.enums import PaymentKind, PaymentStatus
from app.models import LedgerEntry, PaymentChannel, User, Wallet
from app.services.payments.service import PaymentError, PaymentService


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
