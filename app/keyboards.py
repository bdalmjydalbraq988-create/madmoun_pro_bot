from __future__ import annotations

from collections.abc import Iterable, Mapping
from uuid import UUID

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.formatting import money_label
from app.models import Category, PaymentChannel, Product, SupplierCatalogItem
from app.services.delivery import first_http_url


def main_menu(is_admin: bool = False):
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🛍 المتجر"), KeyboardButton(text="💰 رصيدي"))
    builder.row(KeyboardButton(text="➕ شحن الرصيد"), KeyboardButton(text="📦 طلباتي"))
    builder.row(KeyboardButton(text="🎁 ادعُ واربح"), KeyboardButton(text="🆔 رقمي"))
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


def _service_icon(name: str) -> str:
    search = name.casefold()
    rules = (
        ("✨", ("gemini", "جيمناي", "google ai")),
        ("🤖", ("chatgpt", "openai", "شات جي بي تي")),
        ("🧠", ("claude",)),
        ("🔎", ("perplexity",)),
        ("🛸", ("grok", "x ai", "xai")),
        ("📺", ("amazon prime", "prime video")),
        ("🎬", ("netflix", "disney", "shahid", "شاهد", "osn")),
        ("▶️", ("youtube", "يوتيوب")),
        ("🎵", ("spotify", "سبوتيفاي")),
        ("✂️", ("capcut",)),
        ("🎨", ("canva", "adobe", "figma")),
        ("🏗️", ("autodesk",)),
        ("🛡️", ("vpn", "surfshark", "nord", "expressvpn")),
        ("🎓", ("coursera", "udemy", "duolingo")),
        ("💼", ("linkedin", "microsoft", "office", "notion")),
        ("📧", ("gmail", "google mail")),
        ("☁️", ("google one", "drive")),
        ("⭐", ("telegram", "premium")),
    )
    for icon, keywords in rules:
        if any(keyword in search for keyword in keywords):
            return icon
    return "📦"


def products_keyboard(
    products: Iterable[Product],
    snapshots: Mapping[UUID, SupplierCatalogItem] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    snapshots = snapshots or {}
    for product in products:
        snapshot = snapshots.get(product.id)
        stock = ""
        if snapshot is not None and snapshot.stock is not None:
            stock = f" | 📦 {snapshot.stock}"
        suffix = f" | {money_label(product.sale_price, product.currency)}{stock}"
        icon = _service_icon(product.name_ar)
        max_name = max(12, 62 - len(icon) - len(suffix))
        display_name = (
            product.name_ar
            if len(product.name_ar) <= max_name
            else f"{product.name_ar[: max_name - 1]}…"
        )
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {display_name}{suffix}",
                callback_data=f"prd:{product.id}",
                style="success",
            )
        )
    builder.row(InlineKeyboardButton(text="↩️ الأقسام", callback_data="catalog", style="primary"))
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


def delivery_keyboard(delivery: str) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    url = first_http_url(delivery)
    if url and len(url) <= 256:
        rows.append([InlineKeyboardButton(text="🔗 فتح الرابط", url=url)])
    if len(delivery) <= 256:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 نسخ بيانات التسليم",
                    copy_text=CopyTextButton(text=delivery),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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


def jeeb_pending_keyboard(public_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📎 رفع إثبات للمراجعة",
                    callback_data=f"jeebproof:{public_code}",
                )
            ]
        ]
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
            [
                InlineKeyboardButton(text="👥 المشتركون", callback_data="adm:users"),
                InlineKeyboardButton(text="🔎 بحث بالمعرف", callback_data="adm:usersearch"),
            ],
            [InlineKeyboardButton(text="🔄 المورد والمزامنة", callback_data="adm:supplier")],
            [InlineKeyboardButton(text="🎁 نظام الإحالة", callback_data="adm:referrals")],
        ]
    )


def referral_keyboard(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 نسخ رابط دعوتي",
                    copy_text=CopyTextButton(text=link),
                    style="primary",
                )
            ],
            [InlineKeyboardButton(text="📤 مشاركة الرابط", url=f"https://t.me/share/url?url={link}")],
        ]
    )


def admin_referral_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle = "🔴 إيقاف المكافآت" if enabled else "🟢 تشغيل المكافآت"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle, callback_data="adm:referrals:toggle")],
            [
                InlineKeyboardButton(
                    text="💰 مكافأة الداعي",
                    callback_data="adm:referrals:set:referrer",
                ),
                InlineKeyboardButton(
                    text="🎉 مكافأة المدعو",
                    callback_data="adm:referrals:set:invitee",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧾 الحد الأدنى للطلب",
                    callback_data="adm:referrals:set:minimum",
                )
            ],
            [InlineKeyboardButton(text="↩️ لوحة الإدارة", callback_data="adm:dashboard")],
        ]
    )


def admin_user_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 نسخ رقم العميل",
                    copy_text=CopyTextButton(text=str(user_id)),
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 طلبات العميل",
                    callback_data=f"adm:userorders:{user_id}",
                ),
                InlineKeyboardButton(
                    text="💰 تعديل الرصيد",
                    callback_data=f"adm:userwallet:{user_id}",
                ),
            ],
            [InlineKeyboardButton(text="↩️ المشتركون", callback_data="adm:users")],
        ]
    )


def admin_user_orders_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ بيانات العميل",
                    callback_data=f"adm:user:{user_id}",
                )
            ],
            [InlineKeyboardButton(text="↩️ المشتركون", callback_data="adm:users")],
        ]
    )


def user_account_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 نسخ رقم حسابي",
                    copy_text=CopyTextButton(text=str(user_id)),
                    style="primary",
                )
            ]
        ]
    )


def admin_product_keyboard(product: Product) -> InlineKeyboardMarkup:
    active_text = "🔴 تعطيل" if product.is_active else "🟢 تفعيل"
    rows = [
        [InlineKeyboardButton(text="💵 تعديل السعر", callback_data=f"adm:price:{product.id}")],
        [
            InlineKeyboardButton(
                text="✏️ تعديل بيانات الخدمة", callback_data=f"adm:edit:{product.id}"
            )
        ],
        [InlineKeyboardButton(text=active_text, callback_data=f"adm:toggle:{product.id}")],
    ]
    if product.provider_code == "ventebot" and product.provider_product_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="♻️ سعر تلقائي", callback_data=f"adm:autoprice:{product.id}"
                ),
                InlineKeyboardButton(
                    text="♻️ توفر تلقائي", callback_data=f"adm:autoactive:{product.id}"
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="↩️ الخدمات", callback_data="adm:products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_supplier_keyboard(*, auto_activate: bool) -> InlineKeyboardMarkup:
    activation_text = "🔴 إيقاف التفعيل التلقائي" if auto_activate else "🟢 تفعيل تلقائي"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 جلب وتحديث الخدمات الآن", callback_data="adm:supplier:sync"
                )
            ],
            [
                InlineKeyboardButton(text="📈 نسبة الربح", callback_data="adm:supplier:markup"),
                InlineKeyboardButton(text="💵 أقل ربح", callback_data="adm:supplier:minprofit"),
            ],
            [InlineKeyboardButton(text=activation_text, callback_data="adm:supplier:autoactivate")],
            [
                InlineKeyboardButton(
                    text="🩺 إصلاح وإظهار الخدمات المتاحة",
                    callback_data="adm:supplier:repair",
                )
            ],
            [InlineKeyboardButton(text="↩️ لوحة الإدارة", callback_data="adm:dashboard")],
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
