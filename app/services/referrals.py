from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LedgerKind, OrderStatus
from app.models import Order, Referral, ReferralProgramConfig, User
from app.services.audit import add_audit
from app.services.wallet import WalletService, money


@dataclass(frozen=True, slots=True)
class ReferralStats:
    invited: int
    qualified: int
    earned: Decimal


@dataclass(frozen=True, slots=True)
class ReferralAward:
    applied: bool
    referrer_id: int | None = None
    invitee_id: int | None = None
    referrer_amount: Decimal = Decimal("0")
    invitee_amount: Decimal = Decimal("0")


class ReferralService:
    def __init__(self, wallet: WalletService | None = None) -> None:
        self.wallet = wallet or WalletService()

    async def get_config(self, session: AsyncSession) -> ReferralProgramConfig:
        config = await session.get(ReferralProgramConfig, "default")
        if config is None:
            config = ReferralProgramConfig(
                code="default",
                enabled=False,
                referrer_reward=Decimal("0"),
                invitee_reward=Decimal("0"),
                minimum_order_amount=Decimal("0"),
            )
            session.add(config)
            await session.flush()
        return config

    async def register(
        self,
        session: AsyncSession,
        *,
        invitee_id: int,
        referrer_id: int,
    ) -> bool:
        """Attach an inviter exactly once; callers must only invoke this for a new user."""
        if invitee_id == referrer_id or invitee_id <= 0 or referrer_id <= 0:
            return False
        if await session.get(Referral, invitee_id) is not None:
            return False
        if await session.get(User, referrer_id) is None:
            return False
        if await session.get(User, invitee_id) is None:
            return False
        session.add(Referral(invitee_id=invitee_id, referrer_id=referrer_id))
        await session.flush()
        add_audit(
            session,
            actor_user_id=invitee_id,
            action="referral.registered",
            entity_type="referral",
            entity_id=str(invitee_id),
            metadata={"referrer_id": referrer_id},
        )
        return True

    async def stats(self, session: AsyncSession, referrer_id: int) -> ReferralStats:
        invited = (
            await session.scalar(
                select(func.count()).select_from(Referral).where(
                    Referral.referrer_id == referrer_id
                )
            )
            or 0
        )
        qualified = (
            await session.scalar(
                select(func.count()).select_from(Referral).where(
                    Referral.referrer_id == referrer_id,
                    Referral.rewarded_at.is_not(None),
                )
            )
            or 0
        )
        earned = await session.scalar(
            select(func.coalesce(func.sum(Referral.referrer_reward_amount), 0)).where(
                Referral.referrer_id == referrer_id,
                Referral.rewarded_at.is_not(None),
            )
        )
        return ReferralStats(int(invited), int(qualified), money(earned or 0))

    async def award_completed_order(
        self,
        session: AsyncSession,
        *,
        order: Order,
    ) -> ReferralAward:
        """Qualify the first completed order and credit rewards with replay protection."""
        if order.status is not OrderStatus.COMPLETED:
            return ReferralAward(applied=False)
        referral = await session.scalar(
            select(Referral)
            .where(Referral.invitee_id == order.user_id)
            .with_for_update()
        )
        if referral is None or referral.qualified_order_id is not None:
            return ReferralAward(applied=False)

        # autoflush is disabled in production; flush makes this completed order visible
        # to the first-order query before any reward decision is made.
        await session.flush()
        first_completed = await session.scalar(
            select(Order)
            .where(
                Order.user_id == order.user_id,
                Order.status == OrderStatus.COMPLETED,
            )
            .order_by(Order.completed_at, Order.created_at, Order.id)
            .limit(1)
        )
        if first_completed is None:
            return ReferralAward(applied=False)

        # The referral cannot be attached retroactively to an account that had already
        # completed a purchase. Mark it consumed without crediting any reward.
        if first_completed.id != order.id:
            referral.qualified_order_id = first_completed.id
            add_audit(
                session,
                actor_user_id=None,
                action="referral.ineligible_existing_order",
                entity_type="referral",
                entity_id=str(referral.invitee_id),
            )
            return ReferralAward(applied=False)

        config = await self.get_config(session)
        referral.qualified_order_id = order.id
        if not config.enabled or money(order.total_amount) < money(config.minimum_order_amount):
            add_audit(
                session,
                actor_user_id=None,
                action="referral.qualified_without_reward",
                entity_type="referral",
                entity_id=str(referral.invitee_id),
                metadata={"order_id": str(order.id), "program_enabled": config.enabled},
            )
            return ReferralAward(applied=False)

        referrer_amount = money(config.referrer_reward)
        invitee_amount = money(config.invitee_reward)
        referral.referrer_reward_amount = referrer_amount
        referral.invitee_reward_amount = invitee_amount
        referral.rewarded_at = datetime.now(UTC)

        if referrer_amount > 0:
            await self.wallet.credit(
                session,
                user_id=referral.referrer_id,
                amount=referrer_amount,
                kind=LedgerKind.ADMIN_CREDIT,
                idempotency_key=f"referral:referrer:{referral.invitee_id}",
                reference_type="referral",
                reference_id=str(referral.invitee_id),
                note=f"Referral reward for customer {referral.invitee_id}",
            )
        if invitee_amount > 0:
            await self.wallet.credit(
                session,
                user_id=referral.invitee_id,
                amount=invitee_amount,
                kind=LedgerKind.ADMIN_CREDIT,
                idempotency_key=f"referral:invitee:{referral.invitee_id}",
                reference_type="referral",
                reference_id=str(referral.invitee_id),
                note=f"Referral welcome reward from customer {referral.referrer_id}",
            )
        add_audit(
            session,
            actor_user_id=None,
            action="referral.rewarded",
            entity_type="referral",
            entity_id=str(referral.invitee_id),
            metadata={
                "order_id": str(order.id),
                "referrer_id": referral.referrer_id,
                "referrer_reward": format(referrer_amount, "f"),
                "invitee_reward": format(invitee_amount, "f"),
            },
        )
        return ReferralAward(
            applied=referrer_amount > 0 or invitee_amount > 0,
            referrer_id=referral.referrer_id,
            invitee_id=referral.invitee_id,
            referrer_amount=referrer_amount,
            invitee_amount=invitee_amount,
        )
