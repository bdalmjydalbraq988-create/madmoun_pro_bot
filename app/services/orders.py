from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.crypto import PayloadCipher
from app.enums import FulfillmentMode, LedgerKind, OrderStatus, ProviderResultStatus
from app.models import Order, Product
from app.services.audit import add_audit
from app.services.providers.base import (
    ProviderRejectedError,
    ProviderTemporaryError,
    ProviderUncertainError,
    ProvisionRequest,
    SupplierProvider,
)
from app.services.referrals import ReferralService
from app.services.wallet import InsufficientBalance, WalletService, money

logger = logging.getLogger(__name__)


class OrderError(Exception):
    pass


def order_code() -> str:
    return "O" + secrets.token_hex(7).upper()


class OrderService:
    def __init__(
        self,
        cipher: PayloadCipher,
        wallet: WalletService | None = None,
        referrals: ReferralService | None = None,
    ) -> None:
        self.cipher = cipher
        self.wallet = wallet or WalletService()
        self.referrals = referrals or ReferralService(self.wallet)

    async def place_order(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        product_id: uuid.UUID,
        customer_input: str,
        idempotency_key: str,
    ) -> Order:
        existing = await session.scalar(
            select(Order).where(Order.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing

        product = await session.scalar(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        if product is None or not product.is_active:
            raise OrderError("الخدمة غير متاحة حاليًا")
        total = money(product.sale_price)
        if total <= 0:
            raise OrderError("سعر الخدمة غير مضبوط")

        order = Order(
            id=uuid.uuid4(),
            public_code=order_code(),
            idempotency_key=idempotency_key,
            user_id=user_id,
            product_id=product.id,
            product_name_snapshot=product.name_ar,
            quantity=1,
            unit_price=total,
            total_amount=total,
            currency=product.currency,
            customer_input_encrypted=self.cipher.encrypt(customer_input),
            status=(
                OrderStatus.QUEUED
                if product.fulfillment_mode is FulfillmentMode.AUTO
                else OrderStatus.REVIEW_REQUIRED
            ),
            provider_code=product.provider_code,
        )
        session.add(order)
        await session.flush()
        try:
            await self.wallet.debit(
                session,
                user_id=user_id,
                amount=total,
                kind=LedgerKind.PURCHASE,
                idempotency_key=f"order:purchase:{order.id}",
                reference_type="order",
                reference_id=str(order.id),
                note=product.name_ar,
            )
        except InsufficientBalance:
            raise
        add_audit(
            session,
            actor_user_id=user_id,
            action="order.placed",
            entity_type="order",
            entity_id=str(order.id),
            metadata={"public_code": order.public_code, "product": product.name_ar},
        )
        return order

    async def refund(
        self,
        session: AsyncSession,
        *,
        order: Order,
        actor_user_id: int | None,
        reason: str,
    ) -> Order:
        if order.status is OrderStatus.COMPLETED:
            raise OrderError("لا يمكن رد طلب مكتمل تلقائيًا")
        await self.wallet.credit(
            session,
            user_id=order.user_id,
            amount=order.total_amount,
            kind=LedgerKind.REFUND,
            idempotency_key=f"order:refund:{order.id}",
            reference_type="order",
            reference_id=str(order.id),
            actor_user_id=actor_user_id,
            note=reason,
        )
        order.status = OrderStatus.REFUNDED
        order.last_error_message = reason[:500]
        add_audit(
            session,
            actor_user_id=actor_user_id,
            action="order.refunded",
            entity_type="order",
            entity_id=str(order.id),
            metadata={"reason": reason[:300]},
        )
        return order

    async def complete_manual(
        self,
        session: AsyncSession,
        *,
        order_id: uuid.UUID,
        admin_id: int,
        delivery: str,
    ) -> Order:
        order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
        if order is None:
            raise OrderError("الطلب غير موجود")
        if order.status not in {OrderStatus.REVIEW_REQUIRED, OrderStatus.PROVIDER_PENDING}:
            raise OrderError("حالة الطلب لا تسمح بالتسليم اليدوي")
        order.delivery_encrypted = self.cipher.encrypt(delivery)
        order.status = OrderStatus.COMPLETED
        order.completed_at = datetime.now(UTC)
        await self.referrals.award_completed_order(session, order=order)
        add_audit(
            session,
            actor_user_id=admin_id,
            action="order.manual_completed",
            entity_type="order",
            entity_id=str(order.id),
        )
        return order


Notifier = Callable[[Order, str], Awaitable[None]]


class OrderProcessor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        order_service: OrderService,
        providers: dict[str, SupplierProvider],
        max_retries: int = 3,
        notifier: Notifier | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.order_service = order_service
        self.providers = providers
        self.max_retries = max_retries
        self.notifier = notifier

    async def recover_stale_processing(self, max_age: timedelta = timedelta(minutes=10)) -> int:
        """Quarantine interrupted supplier calls instead of retrying them blindly."""
        cutoff = datetime.now(UTC) - max_age
        async with self.session_factory() as session:
            orders = list(
                await session.scalars(
                    select(Order)
                    .where(
                        Order.status == OrderStatus.PROCESSING,
                        Order.updated_at < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for order in orders:
                order.status = OrderStatus.REVIEW_REQUIRED
                order.last_error_code = "interrupted_provider_call"
                order.last_error_message = (
                    "The process stopped during supplier provisioning; reconcile before retrying"
                )
                add_audit(
                    session,
                    actor_user_id=None,
                    action="order.interrupted_review",
                    entity_type="order",
                    entity_id=str(order.id),
                )
            await session.commit()
            return len(orders)

    async def process_next(self) -> bool:
        async with self.session_factory() as session:
            order = await session.scalar(
                select(Order)
                .options(selectinload(Order.product))
                .where(
                    Order.status.in_([OrderStatus.QUEUED, OrderStatus.PROVIDER_PENDING]),
                    (Order.next_retry_at.is_(None) | (Order.next_retry_at <= datetime.now(UTC))),
                )
                .order_by(Order.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if order is None:
                return False
            previous_status = order.status
            order.status = OrderStatus.PROCESSING
            if previous_status is OrderStatus.QUEUED:
                order.retry_count += 1
            order_id = order.id
            provider_code = order.provider_code
            provider_order_id = order.provider_order_id
            product_id = order.product.provider_product_id
            customer_input = self.order_service.cipher.decrypt(order.customer_input_encrypted)
            request = ProvisionRequest(
                client_order_id=order.public_code,
                product_id=product_id or "",
                quantity=order.quantity,
                customer_input=customer_input,
            )
            await session.commit()

        provider = self.providers.get(provider_code or "")
        if provider is None or not product_id:
            await self._mark_review(order_id, "provider_not_configured", "المورد غير مضبوط")
            return True

        try:
            if previous_status is OrderStatus.PROVIDER_PENDING:
                if not provider_order_id:
                    await self._mark_review(
                        order_id,
                        "provider_order_id_missing",
                        "معرّف طلب المورد غير موجود",
                    )
                    return True
                result = await provider.get_order(provider_order_id)
            else:
                result = await provider.create_order(request)
        except ProviderUncertainError as exc:
            await self._mark_review(order_id, exc.code, str(exc))
        except ProviderTemporaryError as exc:
            if previous_status is OrderStatus.PROVIDER_PENDING:
                await self._retry_pending(order_id, exc.code, str(exc))
            else:
                await self._retry_or_review(order_id, exc.code, str(exc))
        except ProviderRejectedError as exc:
            await self._refund_failure(order_id, exc.code, str(exc))
        except Exception:
            logger.exception("Unexpected provider exception for order %s", order_id)
            await self._mark_review(order_id, "unexpected_provider_error", "خطأ غير متوقع")
        else:
            async with self.session_factory() as session:
                order = await session.scalar(
                    select(Order).where(Order.id == order_id).with_for_update()
                )
                if order is None or order.status is not OrderStatus.PROCESSING:
                    return True
                order.provider_order_id = result.external_order_id
                order.provider_status = result.provider_status
                if result.status is ProviderResultStatus.COMPLETED and result.delivery:
                    order.delivery_encrypted = self.order_service.cipher.encrypt(result.delivery)
                    order.status = OrderStatus.COMPLETED
                    order.completed_at = datetime.now(UTC)
                    await self.order_service.referrals.award_completed_order(
                        session,
                        order=order,
                    )
                    message = "completed"
                else:
                    order.status = OrderStatus.PROVIDER_PENDING
                    order.next_retry_at = datetime.now(UTC) + timedelta(seconds=60)
                    message = "provider_pending"
                await session.commit()
                if self.notifier:
                    await self.notifier(order, message)
        return True

    async def _mark_review(self, order_id: uuid.UUID, code: str, message: str) -> None:
        async with self.session_factory() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if order is None:
                return
            order.status = OrderStatus.REVIEW_REQUIRED
            order.last_error_code = code
            order.last_error_message = message[:500]
            add_audit(
                session,
                actor_user_id=None,
                action="order.review_required",
                entity_type="order",
                entity_id=str(order.id),
                metadata={"code": code},
            )
            await session.commit()
            if self.notifier:
                await self.notifier(order, "review_required")

    async def _retry_or_review(self, order_id: uuid.UUID, code: str, message: str) -> None:
        async with self.session_factory() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if order is None:
                return
            order.last_error_code = code
            order.last_error_message = message[:500]
            if order.retry_count >= self.max_retries:
                order.status = OrderStatus.REVIEW_REQUIRED
            else:
                order.status = OrderStatus.QUEUED
                order.next_retry_at = datetime.now(UTC) + timedelta(seconds=30 * order.retry_count)
            await session.commit()

    async def _retry_pending(self, order_id: uuid.UUID, code: str, message: str) -> None:
        async with self.session_factory() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if order is None:
                return
            order.last_error_code = code
            order.last_error_message = message[:500]
            order.status = OrderStatus.PROVIDER_PENDING
            order.next_retry_at = datetime.now(UTC) + timedelta(seconds=60)
            await session.commit()

    async def _refund_failure(self, order_id: uuid.UUID, code: str, message: str) -> None:
        async with self.session_factory() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if order is None:
                return
            order.last_error_code = code
            order.last_error_message = message[:500]
            order.status = OrderStatus.FAILED
            await self.order_service.refund(
                session,
                order=order,
                actor_user_id=None,
                reason="رفض المورد الطلب قبل التسليم",
            )
            await session.commit()
            if self.notifier:
                await self.notifier(order, "refunded")
