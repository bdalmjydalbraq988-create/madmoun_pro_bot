from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import (
    FulfillmentMode,
    LedgerKind,
    OrderStatus,
    PaymentKind,
    PaymentStatus,
)

MONEY = Numeric(20, 8)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(160), default="")
    language: Mapped[str] = mapped_column(String(8), default="ar")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    wallet: Mapped[Wallet] = relationship(back_populates="user", uselist=False)
    orders: Mapped[list[Order]] = relationship(back_populates="user")
    payments: Mapped[list[Payment]] = relationship(back_populates="user")


class Wallet(TimestampMixin, Base):
    __tablename__ = "wallets"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="USDT")

    user: Mapped[User] = relationship(back_populates="wallet")
    entries: Mapped[list[LedgerEntry]] = relationship(back_populates="wallet")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_idempotency_key"),
        Index("ix_ledger_wallet_created", "wallet_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("wallets.user_id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[LedgerKind] = mapped_column(Enum(LedgerKind, native_enum=False, length=32))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    balance_before: Mapped[Decimal] = mapped_column(MONEY)
    balance_after: Mapped[Decimal] = mapped_column(MONEY)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    reference_type: Mapped[str] = mapped_column(String(40))
    reference_id: Mapped[str] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(String(300))
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    wallet: Mapped[Wallet] = relationship(back_populates="entries")


class Category(TimestampMixin, Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120))
    emoji: Mapped[str] = mapped_column(String(16), default="🛍")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    products: Mapped[list[Product]] = relationship(back_populates="category")


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("provider_code", "provider_product_id", name="uq_provider_product"),
    )

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    name_ar: Mapped[str] = mapped_column(String(180))
    description_ar: Mapped[str] = mapped_column(Text, default="")
    sale_price: Mapped[Decimal] = mapped_column(MONEY)
    cost_price: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="USDT")
    fulfillment_mode: Mapped[FulfillmentMode] = mapped_column(
        Enum(FulfillmentMode, native_enum=False, length=16), default=FulfillmentMode.MANUAL
    )
    provider_code: Mapped[str | None] = mapped_column(String(40))
    provider_product_id: Mapped[str | None] = mapped_column(String(100))
    customer_input_label: Mapped[str] = mapped_column(String(160), default="البريد الإلكتروني")
    customer_input_pattern: Mapped[str | None] = mapped_column(String(300))
    customer_input_help: Mapped[str] = mapped_column(
        String(300), default="أرسل البيانات المطلوبة فقط، ولا ترسل كلمة المرور."
    )
    terms_ar: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped[Category] = relationship(back_populates="products")
    orders: Mapped[list[Order]] = relationship(back_populates="product")


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("public_code", name="uq_order_public_code"),
        UniqueConstraint("idempotency_key", name="uq_order_idempotency_key"),
        Index("ix_order_worker", "status", "next_retry_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    public_code: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(180))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    total_amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="USDT")
    customer_input_encrypted: Mapped[str] = mapped_column(Text)
    delivery_encrypted: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=32), default=OrderStatus.QUEUED, index=True
    )
    provider_code: Mapped[str | None] = mapped_column(String(40))
    provider_order_id: Mapped[str | None] = mapped_column(String(120))
    provider_status: Mapped[str | None] = mapped_column(String(80))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error_message: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="orders")
    product: Mapped[Product] = relationship(back_populates="orders")


class PaymentChannel(TimestampMixin, Base):
    __tablename__ = "payment_channels"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name_ar: Mapped[str] = mapped_column(String(100))
    kind: Mapped[PaymentKind] = mapped_column(Enum(PaymentKind, native_enum=False, length=32))
    settlement_currency: Mapped[str] = mapped_column(String(8), default="USDT")
    units_per_usdt: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("1"))
    fee_percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=Decimal("0"))
    min_credit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("1"))
    max_credit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("1000"))
    instructions_ar: Mapped[str] = mapped_column(Text, default="")
    account_label: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    payments: Mapped[list[Payment]] = relationship(back_populates="channel")


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("public_code", name="uq_payment_public_code"),
        UniqueConstraint("channel_code", "external_id", name="uq_payment_external"),
        UniqueConstraint("channel_code", "payer_reference", name="uq_payment_payer_reference"),
        Index("ix_payment_review", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    public_code: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="RESTRICT"), index=True
    )
    channel_code: Mapped[str] = mapped_column(
        ForeignKey("payment_channels.code", ondelete="RESTRICT"), index=True
    )
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=32), default=PaymentStatus.PENDING
    )
    credit_amount: Mapped[Decimal] = mapped_column(MONEY)
    credit_currency: Mapped[str] = mapped_column(String(8), default="USDT")
    expected_amount: Mapped[Decimal] = mapped_column(MONEY)
    settlement_currency: Mapped[str] = mapped_column(String(8))
    rate_snapshot: Mapped[Decimal] = mapped_column(MONEY)
    fee_percent_snapshot: Mapped[Decimal] = mapped_column(Numeric(9, 4))
    external_id: Mapped[str | None] = mapped_column(String(160))
    payer_reference: Mapped[str | None] = mapped_column(String(200))
    proof_file_id: Mapped[str | None] = mapped_column(String(300))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(String(300))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger)

    user: Mapped[User] = relationship(back_populates="payments")
    channel: Mapped[PaymentChannel] = relationship(back_populates="payments")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(100))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
