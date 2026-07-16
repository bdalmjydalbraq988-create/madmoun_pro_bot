from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.enums import LedgerKind
from app.models import LedgerEntry, User, Wallet
from app.services.wallet import InsufficientBalance, WalletService


@pytest.mark.asyncio
async def test_wallet_mutations_are_idempotent_and_exact(session_factory) -> None:
    service = WalletService()
    async with session_factory() as session:
        session.add(User(telegram_id=101, display_name="Test"))
        session.add(Wallet(user_id=101))
        await session.commit()

        first = await service.credit(
            session,
            user_id=101,
            amount=Decimal("10.123456789"),
            kind=LedgerKind.ADMIN_CREDIT,
            idempotency_key="credit-once",
            reference_type="test",
            reference_id="1",
        )
        replay = await service.credit(
            session,
            user_id=101,
            amount=Decimal("10.123456789"),
            kind=LedgerKind.ADMIN_CREDIT,
            idempotency_key="credit-once",
            reference_type="test",
            reference_id="1",
        )
        debit = await service.debit(
            session,
            user_id=101,
            amount=Decimal("3.12"),
            kind=LedgerKind.PURCHASE,
            idempotency_key="debit-once",
            reference_type="test",
            reference_id="2",
        )
        await session.commit()

        assert first.balance_after == Decimal("10.12345679")
        assert replay.was_replayed is True
        assert replay.entry_id == first.entry_id
        assert debit.balance_after == Decimal("7.00345679")
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 2
        wallet = await session.get(Wallet, 101)
        assert wallet.balance == Decimal("7.00345679")


@pytest.mark.asyncio
async def test_wallet_never_goes_negative(session_factory) -> None:
    service = WalletService()
    async with session_factory() as session:
        session.add(User(telegram_id=102, display_name="Test"))
        session.add(Wallet(user_id=102, balance=Decimal("2")))
        await session.commit()

        with pytest.raises(InsufficientBalance):
            await service.debit(
                session,
                user_id=102,
                amount=Decimal("2.00000001"),
                kind=LedgerKind.PURCHASE,
                idempotency_key="too-much",
                reference_type="test",
                reference_id="3",
            )
        await session.rollback()
        wallet = await session.get(Wallet, 102)
        assert wallet.balance == Decimal("2.00000000")
