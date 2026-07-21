from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import LedgerKind, PaymentKind, PaymentStatus
from app.models import JeebPaymentIntent, JeebTransactionEvent, Payment, PaymentChannel
from app.services.audit import add_audit
from app.services.payments.binance import BinancePaymentEvent
from app.services.wallet import WalletMutation, WalletService, money


class PaymentError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PaymentQuote:
    credit_amount: Decimal
    expected_amount: Decimal
    credit_currency: str
    settlement_currency: str
    fee_percent: Decimal
    rate: Decimal


@dataclass(frozen=True, slots=True)
class JeebPaymentEvent:
    transaction_id: str
    amount: Decimal
    currency: str
    sender_account: str
    occurred_at: datetime | None = None


def normalize_jeeb_transaction_id(value: str) -> str:
    return value.strip().upper()


def normalize_jeeb_account(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def payment_code() -> str:
    return "P" + secrets.token_hex(7).upper()


def settlement_quantum(currency: str) -> Decimal:
    currency = currency.upper()
    if currency in {"USDT", "USDC", "BTC", "BNB"}:
        return Decimal("0.00000001")
    if currency == "YER":
        return Decimal("1")
    return Decimal("0.01")


class PaymentService:
    def __init__(self, wallet: WalletService | None = None) -> None:
        self.wallet = wallet or WalletService()

    def quote(self, channel: PaymentChannel, credit_amount: Decimal) -> PaymentQuote:
        credit = money(credit_amount)
        if credit < money(channel.min_credit) or credit > money(channel.max_credit):
            raise PaymentError(
                f"المبلغ يجب أن يكون بين {channel.min_credit} و {channel.max_credit} USDT"
            )
        rate = money(channel.units_per_usdt)
        fee = Decimal(str(channel.fee_percent))
        multiplier = Decimal("1") + (fee / Decimal("100"))
        expected = (credit * rate * multiplier).quantize(
            settlement_quantum(channel.settlement_currency), ROUND_HALF_UP
        )
        return PaymentQuote(
            credit_amount=credit,
            expected_amount=expected,
            credit_currency="USDT",
            settlement_currency=channel.settlement_currency,
            fee_percent=fee,
            rate=rate,
        )

    async def create_pending(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        channel: PaymentChannel,
        quote: PaymentQuote,
        payer_reference: str | None = None,
        proof_file_id: str | None = None,
        allow_unproved_manual: bool = False,
    ) -> Payment:
        if not channel.is_active:
            raise PaymentError("طريقة الدفع غير متاحة حاليًا")
        if (
            channel.kind is PaymentKind.MANUAL
            and not proof_file_id
            and not allow_unproved_manual
        ):
            raise PaymentError("صورة إثبات التحويل مطلوبة")
        if payer_reference:
            existing = await session.scalar(
                select(Payment.id).where(
                    Payment.channel_code == channel.code,
                    Payment.payer_reference == payer_reference[:200],
                )
            )
            if existing is not None:
                raise PaymentError("رقم العملية مستخدم في طلب شحن سابق")
        payment = Payment(
            id=uuid.uuid4(),
            public_code=payment_code(),
            user_id=user_id,
            channel_code=channel.code,
            status=PaymentStatus.PENDING,
            credit_amount=quote.credit_amount,
            credit_currency=quote.credit_currency,
            expected_amount=quote.expected_amount,
            settlement_currency=quote.settlement_currency,
            rate_snapshot=quote.rate,
            fee_percent_snapshot=quote.fee_percent,
            payer_reference=(payer_reference or "")[:200] or None,
            proof_file_id=proof_file_id,
        )
        session.add(payment)
        await session.flush()
        add_audit(
            session,
            actor_user_id=user_id,
            action="payment.created",
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={"channel": channel.code, "public_code": payment.public_code},
        )
        return payment

    async def create_jeeb_pending(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        channel: PaymentChannel,
        quote: PaymentQuote,
        transaction_id: str,
        payer_account: str,
    ) -> tuple[Payment, WalletMutation | None]:
        if channel.code != "jeeb" or channel.kind is not PaymentKind.MANUAL:
            raise PaymentError("طريقة الدفع ليست محفظة جيب")
        transaction_id = normalize_jeeb_transaction_id(transaction_id)
        payer_account = normalize_jeeb_account(payer_account)
        if not 3 <= len(transaction_id) <= 200:
            raise PaymentError("رقم عملية جيب غير صالح")
        if not 6 <= len(payer_account) <= 20:
            raise PaymentError("رقم الحساب المحوّل منه غير صالح")
        payment = await self.create_pending(
            session,
            user_id=user_id,
            channel=channel,
            quote=quote,
            payer_reference=transaction_id,
            allow_unproved_manual=True,
        )
        session.add(JeebPaymentIntent(payment_id=payment.id, payer_account=payer_account))
        await session.flush()
        event = await session.scalar(
            select(JeebTransactionEvent)
            .where(JeebTransactionEvent.transaction_id == transaction_id)
            .with_for_update()
        )
        if event is None:
            return payment, None
        matched_payment, mutation = await self._match_jeeb_event(session, event)
        return matched_payment or payment, mutation

    async def receive_jeeb_event(
        self,
        session: AsyncSession,
        event: JeebPaymentEvent,
    ) -> tuple[JeebTransactionEvent, Payment | None, WalletMutation | None]:
        transaction_id = normalize_jeeb_transaction_id(event.transaction_id)
        sender_account = normalize_jeeb_account(event.sender_account)
        currency = event.currency.strip().upper()
        amount = money(event.amount)
        if not 3 <= len(transaction_id) <= 200:
            raise PaymentError("Invalid Jeeb transaction ID")
        if not 6 <= len(sender_account) <= 20:
            raise PaymentError("Invalid Jeeb sender account")
        if amount <= 0 or len(currency) > 8:
            raise PaymentError("Invalid Jeeb amount or currency")

        stored = await session.scalar(
            select(JeebTransactionEvent)
            .where(JeebTransactionEvent.transaction_id == transaction_id)
            .with_for_update()
        )
        if stored is None:
            stored = JeebTransactionEvent(
                transaction_id=transaction_id,
                amount=amount,
                currency=currency,
                sender_account=sender_account,
                occurred_at=event.occurred_at,
            )
            session.add(stored)
            await session.flush()
        elif (
            money(stored.amount) != amount
            or stored.currency.upper() != currency
            or stored.sender_account != sender_account
        ):
            raise PaymentError("Conflicting replay of a Jeeb transaction")

        payment, mutation = await self._match_jeeb_event(session, stored)
        return stored, payment, mutation

    async def _match_jeeb_event(
        self,
        session: AsyncSession,
        event: JeebTransactionEvent,
    ) -> tuple[Payment | None, WalletMutation | None]:
        if event.matched_payment_id is not None:
            payment = await session.get(Payment, event.matched_payment_id)
            if payment is None or payment.status is not PaymentStatus.CONFIRMED:
                return payment, None
            mutation = await self.wallet.credit(
                session,
                user_id=payment.user_id,
                amount=payment.credit_amount,
                kind=LedgerKind.DEPOSIT,
                idempotency_key=f"payment:{payment.id}",
                reference_type="payment",
                reference_id=str(payment.id),
                note="Jeeb trusted notification relay deposit",
            )
            return payment, mutation

        payment = await session.scalar(
            select(Payment)
            .where(
                Payment.channel_code == "jeeb",
                Payment.payer_reference == event.transaction_id,
            )
            .with_for_update()
        )
        if payment is None:
            return None, None
        intent = await session.get(JeebPaymentIntent, payment.id)
        if intent is None:
            # Old/manual deposits must keep the existing admin-proof workflow.
            return payment, None
        if payment.status is PaymentStatus.CONFIRMED:
            event.matched_payment_id = payment.id
            mutation = await self.wallet.credit(
                session,
                user_id=payment.user_id,
                amount=payment.credit_amount,
                kind=LedgerKind.DEPOSIT,
                idempotency_key=f"payment:{payment.id}",
                reference_type="payment",
                reference_id=str(payment.id),
                note="Jeeb trusted notification relay deposit",
            )
            return payment, mutation
        if payment.status is not PaymentStatus.PENDING:
            return payment, None

        mismatch: dict[str, str] = {}
        if money(event.amount) != money(payment.expected_amount):
            mismatch["amount"] = "mismatch"
        if event.currency.upper() != payment.settlement_currency.upper():
            mismatch["currency"] = "mismatch"
        if event.sender_account != intent.payer_account:
            mismatch["sender_account"] = "mismatch"
        if mismatch:
            event.matched_payment_id = payment.id
            payment.status = PaymentStatus.REVIEW_REQUIRED
            add_audit(
                session,
                actor_user_id=None,
                action="payment.jeeb_mismatch",
                entity_type="payment",
                entity_id=str(payment.id),
                metadata=mismatch,
            )
            return payment, None

        event.matched_payment_id = payment.id
        payment.status = PaymentStatus.CONFIRMED
        payment.confirmed_at = datetime.now(UTC)
        payment.external_id = event.transaction_id
        mutation = await self.wallet.credit(
            session,
            user_id=payment.user_id,
            amount=payment.credit_amount,
            kind=LedgerKind.DEPOSIT,
            idempotency_key=f"payment:{payment.id}",
            reference_type="payment",
            reference_id=str(payment.id),
            note="Jeeb trusted notification relay deposit",
        )
        add_audit(
            session,
            actor_user_id=None,
            action="payment.jeeb_confirmed",
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={"transaction_id": event.transaction_id},
        )
        return payment, mutation

    async def approve_manual(
        self,
        session: AsyncSession,
        *,
        payment_id: uuid.UUID,
        admin_id: int,
    ) -> tuple[Payment, WalletMutation]:
        payment = await session.scalar(
            select(Payment)
            .options(selectinload(Payment.channel))
            .where(Payment.id == payment_id)
            .with_for_update()
        )
        if payment is None:
            raise PaymentError("طلب الشحن غير موجود")
        if payment.channel.kind is not PaymentKind.MANUAL:
            raise PaymentError("لا يمكن اعتماد الدفع التلقائي يدويًا")
        if payment.status is PaymentStatus.CONFIRMED:
            mutation = await self.wallet.credit(
                session,
                user_id=payment.user_id,
                amount=payment.credit_amount,
                kind=LedgerKind.DEPOSIT,
                idempotency_key=f"payment:{payment.id}",
                reference_type="payment",
                reference_id=str(payment.id),
                actor_user_id=payment.confirmed_by,
                note=f"Confirmed {payment.channel_code} deposit",
            )
            return payment, mutation
        if payment.status not in {PaymentStatus.PENDING, PaymentStatus.REVIEW_REQUIRED}:
            raise PaymentError(f"لا يمكن اعتماد طلب حالته {payment.status.value}")

        payment.status = PaymentStatus.CONFIRMED
        payment.confirmed_at = datetime.now(UTC)
        payment.confirmed_by = admin_id
        mutation = await self.wallet.credit(
            session,
            user_id=payment.user_id,
            amount=payment.credit_amount,
            kind=LedgerKind.DEPOSIT,
            idempotency_key=f"payment:{payment.id}",
            reference_type="payment",
            reference_id=str(payment.id),
            actor_user_id=admin_id,
            note=f"Approved {payment.channel_code} deposit",
        )
        add_audit(
            session,
            actor_user_id=admin_id,
            action="payment.approved",
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={"credit": format(payment.credit_amount, "f")},
        )
        return payment, mutation

    async def reject_manual(
        self,
        session: AsyncSession,
        *,
        payment_id: uuid.UUID,
        admin_id: int,
        reason: str,
    ) -> Payment:
        payment = await session.scalar(
            select(Payment)
            .options(selectinload(Payment.channel))
            .where(Payment.id == payment_id)
            .with_for_update()
        )
        if payment is None:
            raise PaymentError("طلب الشحن غير موجود")
        if payment.channel.kind is not PaymentKind.MANUAL:
            raise PaymentError("لا يمكن رفض الدفع التلقائي يدويًا")
        if payment.status not in {PaymentStatus.PENDING, PaymentStatus.REVIEW_REQUIRED}:
            raise PaymentError("تمت معالجة طلب الشحن سابقًا")
        payment.status = PaymentStatus.REJECTED
        payment.rejection_reason = reason[:300]
        payment.confirmed_by = admin_id
        add_audit(
            session,
            actor_user_id=admin_id,
            action="payment.rejected",
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={"reason": reason[:300]},
        )
        return payment

    async def confirm_binance_event(
        self,
        session: AsyncSession,
        event: BinancePaymentEvent,
    ) -> tuple[Payment, WalletMutation | None]:
        payment = await session.scalar(
            select(Payment).where(Payment.public_code == event.merchant_trade_no).with_for_update()
        )
        if payment is None:
            raise PaymentError("Unknown merchant trade number")
        if event.status != "PAY_SUCCESS":
            return payment, None
        if payment.channel_code != "binance":
            raise PaymentError("Payment channel mismatch")
        if money(event.total_fee) != money(payment.expected_amount):
            payment.status = PaymentStatus.REVIEW_REQUIRED
            add_audit(
                session,
                actor_user_id=None,
                action="payment.amount_mismatch",
                entity_type="payment",
                entity_id=str(payment.id),
                metadata={
                    "expected": format(payment.expected_amount, "f"),
                    "received": format(event.total_fee, "f"),
                },
            )
            raise PaymentError("Paid amount does not match the order")
        if event.currency != payment.settlement_currency.upper():
            payment.status = PaymentStatus.REVIEW_REQUIRED
            raise PaymentError("Paid currency does not match the order")

        if payment.status is not PaymentStatus.CONFIRMED:
            payment.status = PaymentStatus.CONFIRMED
            payment.confirmed_at = datetime.now(UTC)
            payment.external_id = event.transaction_id
        mutation = await self.wallet.credit(
            session,
            user_id=payment.user_id,
            amount=payment.credit_amount,
            kind=LedgerKind.DEPOSIT,
            idempotency_key=f"payment:{payment.id}",
            reference_type="payment",
            reference_id=str(payment.id),
            note="Binance Pay deposit",
        )
        add_audit(
            session,
            actor_user_id=None,
            action="payment.binance_confirmed",
            entity_type="payment",
            entity_id=str(payment.id),
            metadata={"transaction_id": event.transaction_id},
        )
        return payment, mutation
