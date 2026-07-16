from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LedgerKind
from app.models import LedgerEntry, Wallet

QUANT = Decimal("0.00000001")


class WalletError(Exception):
    pass


class InvalidAmount(WalletError):
    pass


class InsufficientBalance(WalletError):
    def __init__(self, available: Decimal, required: Decimal) -> None:
        self.available = available
        self.required = required
        super().__init__(f"Insufficient balance: available={available}, required={required}")


@dataclass(frozen=True, slots=True)
class WalletMutation:
    entry_id: uuid.UUID
    balance_before: Decimal
    balance_after: Decimal
    amount: Decimal
    was_replayed: bool = False


def money(value: Decimal | str | int) -> Decimal:
    return Decimal(str(value)).quantize(QUANT, rounding=ROUND_HALF_UP)


class WalletService:
    async def balance(self, session: AsyncSession, user_id: int) -> Decimal:
        wallet = await session.get(Wallet, user_id)
        if wallet is None:
            raise WalletError("Wallet does not exist")
        return money(wallet.balance)

    async def credit(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        amount: Decimal,
        kind: LedgerKind,
        idempotency_key: str,
        reference_type: str,
        reference_id: str,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> WalletMutation:
        amount = money(amount)
        if amount <= 0:
            raise InvalidAmount("Credit amount must be positive")
        return await self._mutate(
            session,
            user_id=user_id,
            signed_amount=amount,
            kind=kind,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            actor_user_id=actor_user_id,
            note=note,
        )

    async def debit(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        amount: Decimal,
        kind: LedgerKind,
        idempotency_key: str,
        reference_type: str,
        reference_id: str,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> WalletMutation:
        amount = money(amount)
        if amount <= 0:
            raise InvalidAmount("Debit amount must be positive")
        return await self._mutate(
            session,
            user_id=user_id,
            signed_amount=-amount,
            kind=kind,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            actor_user_id=actor_user_id,
            note=note,
        )

    async def _mutate(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        signed_amount: Decimal,
        kind: LedgerKind,
        idempotency_key: str,
        reference_type: str,
        reference_id: str,
        actor_user_id: int | None,
        note: str | None,
    ) -> WalletMutation:
        existing = await session.scalar(
            select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return WalletMutation(
                entry_id=existing.id,
                balance_before=money(existing.balance_before),
                balance_after=money(existing.balance_after),
                amount=money(existing.amount),
                was_replayed=True,
            )

        wallet = await session.scalar(
            select(Wallet).where(Wallet.user_id == user_id).with_for_update()
        )
        if wallet is None:
            raise WalletError("Wallet does not exist")

        before = money(wallet.balance)
        after = money(before + signed_amount)
        if after < 0:
            raise InsufficientBalance(available=before, required=abs(signed_amount))

        wallet.balance = after
        entry = LedgerEntry(
            wallet_id=user_id,
            kind=kind,
            amount=signed_amount,
            balance_before=before,
            balance_after=after,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            actor_user_id=actor_user_id,
            note=(note or "")[:300] or None,
        )
        session.add(entry)
        await session.flush()
        return WalletMutation(entry.id, before, after, signed_amount)
