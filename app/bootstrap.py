from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import FulfillmentMode, PaymentKind
from app.models import Category, PaymentChannel, Product


async def seed_defaults(session: AsyncSession, settings: Settings) -> None:
    category = await session.scalar(
        select(Category).where(Category.name_ar == "اشتراكات الذكاء الاصطناعي")
    )
    if category is None:
        category = Category(name_ar="اشتراكات الذكاء الاصطناعي", emoji="🤖", sort_order=10)
        session.add(category)
        await session.flush()

    product = await session.scalar(select(Product).where(Product.name_ar == "Gemini Pro"))
    if product is None and not settings.supplier_enabled:
        session.add(
            Product(
                category_id=category.id,
                name_ar="Gemini Pro",
                description_ar="اشتراك Gemini Pro. اضبط المدة والسعر وشروط المورد قبل التفعيل.",
                sale_price=Decimal("0"),
                fulfillment_mode=FulfillmentMode.MANUAL,
                provider_code="ventebot",
                provider_product_id=None,
                customer_input_label="بريد Google المستفيد",
                customer_input_pattern=r"[^@\s]+@[^@\s]+\.[^@\s]+",
                customer_input_help="أرسل البريد فقط. لا ترسل كلمة مرور حساب Google مطلقًا.",
                is_active=False,
            )
        )
    elif (
        product is not None
        and settings.supplier_enabled
        and product.provider_product_id is None
        and product.description_ar
        == "اشتراك Gemini Pro. اضبط المدة والسعر وشروط المورد قبل التفعيل."
    ):
        # Hide the setup placeholder once the real supplier catalog is enabled.
        product.is_active = False

    defaults = [
        PaymentChannel(
            code="binance",
            name_ar="🟡 Binance Pay",
            kind=PaymentKind.BINANCE_PAY,
            settlement_currency="USDT",
            units_per_usdt=Decimal("1"),
            fee_percent=Decimal("0"),
            instructions_ar="ادفع عبر صفحة Binance Pay الرسمية فقط.",
            is_active=settings.binance_pay_enabled,
            sort_order=10,
        ),
        PaymentChannel(
            code="jeeb",
            name_ar="🔴 محفظة جيب",
            kind=PaymentKind.MANUAL,
            settlement_currency="YER",
            units_per_usdt=Decimal("1"),
            fee_percent=Decimal("3"),
            instructions_ar="اضبط رقم المحفظة وسعر الصرف من لوحة الأدمن قبل التفعيل.",
            is_active=False,
            sort_order=20,
        ),
        PaymentChannel(
            code="kuraimi",
            name_ar="🏦 بنك الكريمي",
            kind=PaymentKind.MANUAL,
            settlement_currency="YER",
            units_per_usdt=Decimal("1"),
            fee_percent=Decimal("0"),
            instructions_ar="اضبط رقم الحساب وسعر الصرف من لوحة الأدمن قبل التفعيل.",
            is_active=False,
            sort_order=30,
        ),
    ]
    for channel in defaults:
        if await session.get(PaymentChannel, channel.code) is None:
            session.add(channel)
    await session.commit()
