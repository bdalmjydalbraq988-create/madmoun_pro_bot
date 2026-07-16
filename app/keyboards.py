from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.models import Category, PaymentChannel, Product


def main_menu(is_admin: bool = False):
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🛍 المتجر"), KeyboardButton(text="💰 رصيدي"))
    builder.row(KeyboardButton(text="➕ شحن الرصيد"), KeyboardButton(text="📦 طلباتي"))
    builder.row(KeyboardButton(text="🧑‍💻 الدعم"))
    if is_admin:
        builder.row(KeyboardButton(text="⚙️ لوحة الأدمن"))
    return builder.as_markup(resize_keyboard=True)


def categories_keyboard(categories: Iterable[Category]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(
            text=f"{category.emoji} {category.name_ar}", callback_data=f"cat:{category.id}"
        )
    builder.adjust(1)
    return builder.as_markup()


def products_keyboard(products: Iterable[Product]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.button(
            text=f"{product.name_ar} — {product.sale_price:g} USDT",
            callback_data=f"prd:{product.id}",
        )
    builder.button(text="↩️ الأقسام", callback_data="catalog")
    builder.adjust(1)
    return builder.as_markup()


def product_keyboard(product: Product) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ شراء الآن", callback_data=f"buy:{product.id}")],
            [InlineKeyboardButton(text="↩️ رجوع", callback_data=f"cat:{product.category_id}")],
        ]
    )


def confirm_buy_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأكيد الشراء", callback_data=f"buyok:{token}")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel")],
        ]
    )


def payment_channels_keyboard(channels: Iterable[PaymentChannel]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.button(text=channel.name_ar, callback_data=f"pay:{channel.code}")
    builder.adjust(1)
    return builder.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel")]]
    )


def admin_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛍 الخدمات", callback_data="adm:products"),
                InlineKeyboardButton(text="📂 الأقسام", callback_data="adm:categories"),
            ],
            [
                InlineKeyboardButton(text="💳 الشحن المعلق", callback_data="adm:payments"),
                InlineKeyboardButton(text="📦 طلبات المراجعة", callback_data="adm:orders"),
            ],
            [
                InlineKeyboardButton(text="💰 تعديل رصيد", callback_data="adm:wallet"),
                InlineKeyboardButton(text="🏦 طرق الدفع", callback_data="adm:channels"),
            ],
            [InlineKeyboardButton(text="➕ إضافة خدمة", callback_data="adm:add_product")],
            [InlineKeyboardButton(text="➕ إضافة قسم", callback_data="adm:add_category")],
        ]
    )


def admin_product_keyboard(product: Product) -> InlineKeyboardMarkup:
    active_text = "🔴 تعطيل" if product.is_active else "🟢 تفعيل"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 تعديل السعر", callback_data=f"adm:price:{product.id}")],
            [
                InlineKeyboardButton(
                    text="✏️ تعديل بيانات الخدمة", callback_data=f"adm:edit:{product.id}"
                )
            ],
            [InlineKeyboardButton(text=active_text, callback_data=f"adm:toggle:{product.id}")],
            [InlineKeyboardButton(text="↩️ الخدمات", callback_data="adm:products")],
        ]
    )


def admin_payment_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ اعتماد", callback_data=f"adm:payok:{payment_id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"adm:payno:{payment_id}"),
            ]
        ]
    )


def admin_order_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📨 تسليم يدوي", callback_data=f"adm:deliver:{order_id}"),
                InlineKeyboardButton(text="↩️ رد الرصيد", callback_data=f"adm:refund:{order_id}"),
            ]
        ]
    )


def admin_channel_keyboard(code: str, active: bool) -> InlineKeyboardMarkup:
    toggle = "🔴 تعطيل" if active else "🟢 تفعيل"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle, callback_data=f"adm:chtoggle:{code}")],
            [InlineKeyboardButton(text="💱 تعديل السعر", callback_data=f"adm:chrate:{code}")],
            [InlineKeyboardButton(text="➕ تعديل الرسوم", callback_data=f"adm:chfee:{code}")],
            [InlineKeyboardButton(text="📝 تعليمات التحويل", callback_data=f"adm:chinfo:{code}")],
            [InlineKeyboardButton(text="↩️ طرق الدفع", callback_data="adm:channels")],
        ]
    )
