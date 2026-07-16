from __future__ import annotations

import html
import secrets
import uuid
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.crypto import PayloadCipher
from app.enums import FulfillmentMode, LedgerKind, OrderStatus, PaymentStatus
from app.keyboards import (
    admin_channel_keyboard,
    admin_dashboard_keyboard,
    admin_order_keyboard,
    admin_payment_keyboard,
    admin_product_keyboard,
    cancel_keyboard,
)
from app.models import Category, Order, Payment, PaymentChannel, Product, User, Wallet
from app.services.audit import add_audit
from app.services.orders import OrderError, OrderService
from app.services.payments.service import PaymentError, PaymentService
from app.services.wallet import InsufficientBalance, WalletService, money
from app.states import (
    AdminCategoryFlow,
    AdminChannelFeeFlow,
    AdminChannelInstructionsFlow,
    AdminChannelRateFlow,
    AdminDeliveryFlow,
    AdminPaymentRejectFlow,
    AdminPriceFlow,
    AdminProductEditFlow,
    AdminProductFlow,
    AdminRefundFlow,
    AdminWalletFlow,
)


class IsAdmin(Filter):
    def __init__(self, admin_ids: list[int]) -> None:
        self.admin_ids = set(admin_ids)

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id in self.admin_ids)


def build_admin_router(
    *,
    settings: Settings,
    cipher: PayloadCipher,
    order_service: OrderService,
    payment_service: PaymentService,
) -> Router:
    router = Router(name="admin")
    router.message.filter(IsAdmin(settings.admin_ids))
    router.callback_query.filter(IsAdmin(settings.admin_ids))
    wallet_service = WalletService()

    async def dashboard_text(session: AsyncSession) -> str:
        users = await session.scalar(select(func.count()).select_from(User)) or 0
        orders = await session.scalar(select(func.count()).select_from(Order)) or 0
        review_orders = (
            await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.status == OrderStatus.REVIEW_REQUIRED)
            )
            or 0
        )
        pending_payments = (
            await session.scalar(
                select(func.count())
                .select_from(Payment)
                .where(Payment.status == PaymentStatus.PENDING)
            )
            or 0
        )
        total_balances = await session.scalar(select(func.coalesce(func.sum(Wallet.balance), 0)))
        return (
            "⚙️ <b>لوحة الإدارة</b>\n\n"
            f"المستخدمون: {users}\n"
            f"إجمالي الطلبات: {orders}\n"
            f"طلبات تحتاج مراجعة: {review_orders}\n"
            f"شحنات معلقة: {pending_payments}\n"
            f"مجموع أرصدة العملاء: {Decimal(total_balances):g} USDT"
        )

    @router.message(Command("admin"))
    @router.message(F.text == "⚙️ لوحة الأدمن")
    async def admin_dashboard(message: Message, session: AsyncSession, state: FSMContext) -> None:
        await state.clear()
        await message.answer(await dashboard_text(session), reply_markup=admin_dashboard_keyboard())

    @router.callback_query(F.data == "adm:dashboard")
    async def admin_dashboard_callback(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                await dashboard_text(session), reply_markup=admin_dashboard_keyboard()
            )

    @router.callback_query(F.data == "adm:products")
    async def products_list(callback: CallbackQuery, session: AsyncSession) -> None:
        products = list(await session.scalars(select(Product).order_by(Product.name_ar)))
        builder = InlineKeyboardBuilder()
        for product in products:
            mark = "🟢" if product.is_active else "⚫️"
            builder.button(
                text=f"{mark} {product.name_ar} — {product.sale_price:g}",
                callback_data=f"adm:product:{product.id}",
            )
        builder.button(text="➕ إضافة خدمة", callback_data="adm:add_product")
        builder.button(text="↩️ لوحة الإدارة", callback_data="adm:dashboard")
        builder.adjust(1)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "🛍 <b>إدارة الخدمات</b>", reply_markup=builder.as_markup()
            )

    @router.callback_query(F.data.startswith("adm:product:"))
    async def product_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        product_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
        product = await session.get(Product, product_id)
        if product is None:
            await callback.answer("الخدمة غير موجودة", show_alert=True)
            return
        text = (
            f"<b>{html.escape(product.name_ar)}</b>\n"
            f"السعر: {product.sale_price:g} {product.currency}\n"
            f"التكلفة: {product.cost_price if product.cost_price is not None else '-'}\n"
            f"الحالة: {'مفعلة' if product.is_active else 'معطلة'}\n"
            f"التنفيذ: {product.fulfillment_mode.value}\n"
            f"معرّف المورد: <code>{html.escape(product.provider_product_id or '-')}</code>"
        )
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(text, reply_markup=admin_product_keyboard(product))

    @router.callback_query(F.data.startswith("adm:toggle:"))
    async def toggle_product(callback: CallbackQuery, session: AsyncSession) -> None:
        product = await session.get(Product, uuid.UUID(callback.data.rsplit(":", 1)[1]))
        if product is None:
            await callback.answer("الخدمة غير موجودة", show_alert=True)
            return
        if not product.is_active and product.sale_price <= 0:
            await callback.answer("اضبط سعرًا أكبر من صفر أولًا", show_alert=True)
            return
        if (
            not product.is_active
            and product.fulfillment_mode is FulfillmentMode.AUTO
            and not product.provider_product_id
        ):
            await callback.answer("اضبط معرّف منتج المورد أولًا", show_alert=True)
            return
        if (
            not product.is_active
            and product.fulfillment_mode is FulfillmentMode.AUTO
            and not settings.supplier_enabled
        ):
            await callback.answer("فعّل واختبر API المورد أولًا", show_alert=True)
            return
        product.is_active = not product.is_active
        add_audit(
            session,
            actor_user_id=callback.from_user.id,
            action="product.toggled",
            entity_type="product",
            entity_id=str(product.id),
            metadata={"active": product.is_active},
        )
        await callback.answer("تم التحديث")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=admin_product_keyboard(product))

    @router.callback_query(F.data.startswith("adm:price:"))
    async def begin_price(callback: CallbackQuery, state: FSMContext) -> None:
        product_id = callback.data.rsplit(":", 1)[1]
        await state.set_state(AdminPriceFlow.value)
        await state.set_data({"product_id": product_id})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل السعر الجديد بـ USDT:", reply_markup=cancel_keyboard()
            )

    @router.message(AdminPriceFlow.value, F.text)
    async def save_price(message: Message, session: AsyncSession, state: FSMContext) -> None:
        try:
            price = money(Decimal(message.text.replace(",", ".").strip()))
        except InvalidOperation:
            await message.answer("أرسل رقمًا صحيحًا.")
            return
        if price <= 0 or price > Decimal("100000"):
            await message.answer("السعر يجب أن يكون أكبر من صفر.")
            return
        data = await state.get_data()
        product = await session.get(Product, uuid.UUID(data["product_id"]))
        if product is None:
            await state.clear()
            await message.answer("الخدمة غير موجودة.")
            return
        old = product.sale_price
        product.sale_price = price
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="product.price_changed",
            entity_type="product",
            entity_id=str(product.id),
            metadata={"old": format(old, "f"), "new": format(price, "f")},
        )
        await state.clear()
        await message.answer(f"✅ أصبح سعر {html.escape(product.name_ar)}: {price:g} USDT")

    @router.callback_query(F.data.startswith("adm:edit:"))
    async def choose_product_edit_field(callback: CallbackQuery) -> None:
        product_id = callback.data.rsplit(":", 1)[1]
        builder = InlineKeyboardBuilder()
        fields = [
            ("الاسم", "name"),
            ("الوصف", "description"),
            ("معرّف المورد", "provider_id"),
            ("حقل العميل", "input_label"),
            ("الشروط", "terms"),
            ("نوع التنفيذ", "mode"),
        ]
        for label, field in fields:
            builder.button(text=label, callback_data=f"adm:editfield:{product_id}:{field}")
        builder.button(text="↩️ رجوع", callback_data=f"adm:product:{product_id}")
        builder.adjust(2, 2, 2, 1)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "اختر بيانات الخدمة التي تريد تعديلها:", reply_markup=builder.as_markup()
            )

    @router.callback_query(F.data.startswith("adm:editfield:"))
    async def begin_product_field_edit(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, product_id, field = callback.data.split(":", 3)
        prompts = {
            "name": "أرسل الاسم الجديد:",
            "description": "أرسل الوصف الجديد:",
            "provider_id": "أرسل معرّف المورد الجديد، أو - لمسحه:",
            "input_label": "أرسل اسم البيانات المطلوبة من العميل:",
            "terms": "أرسل الشروط الجديدة، أو - لمسحها:",
            "mode": "اكتب manual للتسليم اليدوي أو auto للمورد:",
        }
        if field not in prompts:
            await callback.answer("حقل غير صالح", show_alert=True)
            return
        await state.set_state(AdminProductEditFlow.value)
        await state.set_data({"product_id": product_id, "field": field})
        await callback.answer()
        if callback.message:
            await callback.message.answer(prompts[field], reply_markup=cancel_keyboard())

    @router.message(AdminProductEditFlow.value, F.text)
    async def save_product_field(
        message: Message, session: AsyncSession, state: FSMContext
    ) -> None:
        data = await state.get_data()
        product = await session.get(Product, uuid.UUID(data["product_id"]))
        if product is None:
            await state.clear()
            await message.answer("الخدمة غير موجودة.")
            return
        field = data["field"]
        value = message.text.strip()
        if field == "name":
            if not 2 <= len(value) <= 180:
                await message.answer("الاسم غير صالح.")
                return
            product.name_ar = value
        elif field == "description":
            if not value or len(value) > 2000:
                await message.answer("الوصف غير صالح.")
                return
            product.description_ar = value
        elif field == "provider_id":
            if len(value) > 100:
                await message.answer("معرّف المورد طويل جدًا.")
                return
            product.provider_product_id = None if value == "-" else value
            if product.fulfillment_mode is FulfillmentMode.AUTO and not product.provider_product_id:
                product.is_active = False
        elif field == "input_label":
            if not 2 <= len(value) <= 160:
                await message.answer("اسم الحقل غير صالح.")
                return
            product.customer_input_label = value
        elif field == "terms":
            if len(value) > 4000:
                await message.answer("الشروط طويلة جدًا.")
                return
            product.terms_ar = "" if value == "-" else value
        elif field == "mode":
            mode = value.lower()
            if mode not in {"manual", "auto"}:
                await message.answer("اكتب manual أو auto فقط.")
                return
            product.fulfillment_mode = (
                FulfillmentMode.MANUAL if mode == "manual" else FulfillmentMode.AUTO
            )
            product.provider_code = None if mode == "manual" else "ventebot"
            if mode == "auto" and (
                not settings.supplier_enabled or not product.provider_product_id
            ):
                product.is_active = False
        else:
            await state.clear()
            await message.answer("الحقل غير صالح.")
            return
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="product.field_changed",
            entity_type="product",
            entity_id=str(product.id),
            metadata={"field": field},
        )
        await state.clear()
        await message.answer(
            f"✅ تم تعديل {html.escape(product.name_ar)}. "
            f"الحالة الآن: {'مفعلة' if product.is_active else 'معطلة'}"
        )

    @router.callback_query(F.data == "adm:categories")
    async def categories_list(callback: CallbackQuery, session: AsyncSession) -> None:
        categories = list(await session.scalars(select(Category).order_by(Category.sort_order)))
        lines = ["📂 <b>الأقسام</b>"]
        for category in categories:
            lines.append(
                f"{category.id}. {category.emoji} {html.escape(category.name_ar)} — "
                f"{'مفعل' if category.is_active else 'معطل'}"
            )
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ إضافة قسم", callback_data="adm:add_category")
        builder.button(text="↩️ لوحة الإدارة", callback_data="adm:dashboard")
        builder.adjust(1)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text("\n".join(lines), reply_markup=builder.as_markup())

    @router.callback_query(F.data == "adm:add_category")
    async def begin_add_category(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminCategoryFlow.name)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل اسم القسم، ويمكن أن يبدأ بإيموجي. مثال: 🤖 ذكاء اصطناعي",
                reply_markup=cancel_keyboard(),
            )

    @router.message(AdminCategoryFlow.name, F.text)
    async def save_category(message: Message, session: AsyncSession, state: FSMContext) -> None:
        value = message.text.strip()
        if len(value) < 2 or len(value) > 120:
            await message.answer("اسم القسم غير صالح.")
            return
        parts = value.split(maxsplit=1)
        first_is_emoji = len(parts) == 2 and not any(char.isalnum() for char in parts[0])
        emoji, name = (parts[0], parts[1]) if first_is_emoji else ("🛍", value)
        category = Category(name_ar=name, emoji=emoji)
        session.add(category)
        await session.flush()
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="category.created",
            entity_type="category",
            entity_id=str(category.id),
        )
        await state.clear()
        await message.answer(f"✅ أُضيف القسم رقم {category.id}: {html.escape(category.name_ar)}")

    @router.callback_query(F.data == "adm:add_product")
    async def begin_add_product(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        categories = list(
            await session.scalars(select(Category).where(Category.is_active.is_(True)))
        )
        if not categories:
            await callback.answer("أضف قسمًا أولًا", show_alert=True)
            return
        choices = "\n".join(f"{c.id}: {html.escape(c.name_ar)}" for c in categories)
        await state.set_state(AdminProductFlow.category)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"أرسل رقم القسم:\n{choices}", reply_markup=cancel_keyboard()
            )

    @router.message(AdminProductFlow.category, F.text)
    async def add_product_category(
        message: Message, session: AsyncSession, state: FSMContext
    ) -> None:
        try:
            category_id = int(message.text.strip())
        except ValueError:
            await message.answer("أرسل رقم القسم.")
            return
        category = await session.get(Category, category_id)
        if category is None or not category.is_active:
            await message.answer("القسم غير موجود أو معطل.")
            return
        await state.update_data(category_id=category_id)
        await state.set_state(AdminProductFlow.name)
        await message.answer("أرسل اسم الخدمة:")

    @router.message(AdminProductFlow.name, F.text)
    async def add_product_name(message: Message, state: FSMContext) -> None:
        name = message.text.strip()
        if len(name) < 2 or len(name) > 180:
            await message.answer("اسم الخدمة غير صالح.")
            return
        await state.update_data(name=name)
        await state.set_state(AdminProductFlow.description)
        await message.answer("أرسل وصف الخدمة:")

    @router.message(AdminProductFlow.description, F.text)
    async def add_product_description(message: Message, state: FSMContext) -> None:
        description = message.text.strip()
        if len(description) > 2000:
            await message.answer("الوصف طويل جدًا.")
            return
        await state.update_data(description=description)
        await state.set_state(AdminProductFlow.price)
        await message.answer("أرسل سعر البيع بـ USDT:")

    @router.message(AdminProductFlow.price, F.text)
    async def add_product_price(message: Message, state: FSMContext) -> None:
        try:
            price = money(Decimal(message.text.replace(",", ".").strip()))
        except InvalidOperation:
            await message.answer("أرسل سعرًا صحيحًا.")
            return
        if price <= 0:
            await message.answer("السعر يجب أن يكون أكبر من صفر.")
            return
        await state.update_data(price=format(price, "f"))
        await state.set_state(AdminProductFlow.fulfillment)
        await message.answer("اكتب manual للتسليم اليدوي أو auto للمورد التلقائي:")

    @router.message(AdminProductFlow.fulfillment, F.text)
    async def add_product_fulfillment(message: Message, state: FSMContext) -> None:
        value = message.text.strip().lower()
        if value not in {"manual", "auto"}:
            await message.answer("اكتب manual أو auto فقط.")
            return
        await state.update_data(fulfillment=value)
        await state.set_state(AdminProductFlow.provider_product_id)
        prompt = "أرسل معرّف المنتج لدى المورد:" if value == "auto" else "أرسل - لأنه منتج يدوي:"
        await message.answer(prompt)

    @router.message(AdminProductFlow.provider_product_id, F.text)
    async def add_product_provider(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        data = await state.get_data()
        if data["fulfillment"] == "auto" and (not value or value == "-"):
            await message.answer("معرّف منتج المورد مطلوب للتنفيذ التلقائي.")
            return
        await state.update_data(provider_product_id=None if value == "-" else value[:100])
        await state.set_state(AdminProductFlow.input_label)
        await message.answer("ما البيانات التي سيرسلها العميل؟ مثال: البريد الإلكتروني")

    @router.message(AdminProductFlow.input_label, F.text)
    async def save_product(message: Message, session: AsyncSession, state: FSMContext) -> None:
        label = message.text.strip()
        if len(label) < 2 or len(label) > 160:
            await message.answer("اسم الحقل غير صالح.")
            return
        data = await state.get_data()
        auto = data["fulfillment"] == "auto"
        product = Product(
            category_id=data["category_id"],
            name_ar=data["name"],
            description_ar=data["description"],
            sale_price=Decimal(data["price"]),
            fulfillment_mode=FulfillmentMode.AUTO if auto else FulfillmentMode.MANUAL,
            provider_code="ventebot" if auto else None,
            provider_product_id=data.get("provider_product_id"),
            customer_input_label=label,
            customer_input_help="أرسل البيانات المطلوبة فقط، ولا ترسل كلمة المرور أو رمز التحقق.",
            is_active=False,
        )
        session.add(product)
        await session.flush()
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="product.created",
            entity_type="product",
            entity_id=str(product.id),
        )
        await state.clear()
        await message.answer(
            f"✅ أُضيفت الخدمة {html.escape(product.name_ar)} وهي معطلة للمراجعة. "
            "فعّلها من قائمة الخدمات بعد التحقق من التفاصيل."
        )

    @router.callback_query(F.data == "adm:wallet")
    async def begin_wallet_adjustment(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminWalletFlow.user_id)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل Telegram ID للعميل:", reply_markup=cancel_keyboard()
            )

    @router.message(AdminWalletFlow.user_id, F.text)
    async def wallet_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer("أرسل رقم Telegram ID صحيحًا.")
            return
        if await session.get(User, user_id) is None:
            await message.answer("المستخدم غير موجود؛ يجب أن يرسل /start للبوت أولًا.")
            return
        await state.update_data(user_id=user_id)
        await state.set_state(AdminWalletFlow.amount)
        await message.answer("أرسل المبلغ: موجب للإضافة، وسالب للخصم. مثال: 5 أو -2")

    @router.message(AdminWalletFlow.amount, F.text)
    async def wallet_amount(message: Message, state: FSMContext) -> None:
        try:
            amount = money(Decimal(message.text.replace(",", ".").strip()))
        except InvalidOperation:
            await message.answer("أرسل مبلغًا صحيحًا.")
            return
        if amount == 0 or abs(amount) > Decimal("100000"):
            await message.answer("المبلغ غير صالح.")
            return
        await state.update_data(amount=format(amount, "f"))
        await state.set_state(AdminWalletFlow.reason)
        await message.answer("أرسل سبب التعديل؛ سيُحفظ في سجل التدقيق:")

    @router.message(AdminWalletFlow.reason, F.text)
    async def wallet_reason(message: Message, session: AsyncSession, state: FSMContext) -> None:
        reason = message.text.strip()
        if len(reason) < 3:
            await message.answer("اكتب سببًا واضحًا.")
            return
        data = await state.get_data()
        amount = Decimal(data["amount"])
        key = f"admin:{message.from_user.id}:{secrets.token_hex(12)}"
        try:
            if amount > 0:
                result = await wallet_service.credit(
                    session,
                    user_id=data["user_id"],
                    amount=amount,
                    kind=LedgerKind.ADMIN_CREDIT,
                    idempotency_key=key,
                    reference_type="admin_adjustment",
                    reference_id=key,
                    actor_user_id=message.from_user.id,
                    note=reason,
                )
            else:
                result = await wallet_service.debit(
                    session,
                    user_id=data["user_id"],
                    amount=abs(amount),
                    kind=LedgerKind.ADMIN_DEBIT,
                    idempotency_key=key,
                    reference_type="admin_adjustment",
                    reference_id=key,
                    actor_user_id=message.from_user.id,
                    note=reason,
                )
        except InsufficientBalance as exc:
            await message.answer(f"الرصيد غير كافٍ؛ المتاح {exc.available:g} USDT")
            return
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="wallet.admin_adjusted",
            entity_type="wallet",
            entity_id=str(data["user_id"]),
            metadata={"amount": format(amount, "f"), "reason": reason[:300]},
        )
        await state.clear()
        await message.answer(f"✅ تم التعديل. الرصيد الجديد: {result.balance_after:g} USDT")

    @router.callback_query(F.data == "adm:payments")
    async def pending_payments(callback: CallbackQuery, session: AsyncSession) -> None:
        payments = list(
            await session.scalars(
                select(Payment)
                .where(
                    Payment.status == PaymentStatus.PENDING,
                    Payment.channel_code != "binance",
                )
                .order_by(Payment.created_at)
                .limit(10)
            )
        )
        await callback.answer()
        if not callback.message:
            return
        if not payments:
            await callback.message.answer("لا توجد طلبات شحن معلقة.")
            return
        for payment in payments:
            caption = (
                f"<b>{payment.public_code}</b>\n"
                f"العميل: <code>{payment.user_id}</code>\n"
                f"الطريقة: {payment.channel_code}\n"
                f"المبلغ المحول: {payment.expected_amount:g} {payment.settlement_currency}\n"
                f"الرصيد المطلوب: {payment.credit_amount:g} USDT\n"
                f"المرجع: {html.escape(payment.payer_reference or '-')}"
            )
            if payment.proof_file_id:
                await callback.message.answer_photo(
                    payment.proof_file_id,
                    caption=caption,
                    reply_markup=admin_payment_keyboard(str(payment.id)),
                )
            else:
                await callback.message.answer(
                    caption, reply_markup=admin_payment_keyboard(str(payment.id))
                )

    @router.callback_query(F.data.startswith("adm:payok:"))
    async def approve_payment(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
        payment_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
        try:
            payment, mutation = await payment_service.approve_manual(
                session, payment_id=payment_id, admin_id=callback.from_user.id
            )
        except PaymentError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await session.commit()
        await callback.answer("تم اعتماد الشحن")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        await bot.send_message(
            payment.user_id,
            f"✅ تم اعتماد الشحن <code>{payment.public_code}</code>.\n"
            f"رصيدك الجديد: <b>{mutation.balance_after:g} USDT</b>",
        )

    @router.callback_query(F.data.startswith("adm:payno:"))
    async def begin_reject_payment(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminPaymentRejectFlow.reason)
        await state.set_data({"payment_id": callback.data.rsplit(":", 1)[1]})
        await callback.answer()
        if callback.message:
            await callback.message.answer("اكتب سبب رفض طلب الشحن:", reply_markup=cancel_keyboard())

    @router.message(AdminPaymentRejectFlow.reason, F.text)
    async def reject_payment(
        message: Message, session: AsyncSession, state: FSMContext, bot: Bot
    ) -> None:
        data = await state.get_data()
        try:
            payment = await payment_service.reject_manual(
                session,
                payment_id=uuid.UUID(data["payment_id"]),
                admin_id=message.from_user.id,
                reason=message.text.strip(),
            )
        except PaymentError as exc:
            await message.answer(str(exc))
            return
        await session.commit()
        await state.clear()
        await message.answer("تم رفض طلب الشحن.")
        await bot.send_message(
            payment.user_id,
            f"❌ رُفض طلب الشحن <code>{payment.public_code}</code>.\n"
            f"السبب: {html.escape(payment.rejection_reason or '')}",
        )

    @router.callback_query(F.data == "adm:orders")
    async def review_orders(callback: CallbackQuery, session: AsyncSession) -> None:
        orders = list(
            await session.scalars(
                select(Order)
                .where(
                    Order.status.in_([OrderStatus.REVIEW_REQUIRED, OrderStatus.PROVIDER_PENDING])
                )
                .order_by(Order.created_at)
                .limit(10)
            )
        )
        await callback.answer()
        if not callback.message:
            return
        if not orders:
            await callback.message.answer("لا توجد طلبات تحتاج مراجعة.")
            return
        for order in orders:
            customer_input = cipher.decrypt(order.customer_input_encrypted)
            text = (
                f"📦 <b>{order.public_code}</b>\n"
                f"العميل: <code>{order.user_id}</code>\n"
                f"الخدمة: {html.escape(order.product_name_snapshot)}\n"
                f"البيانات: <code>{html.escape(customer_input)}</code>\n"
                f"الحالة: {order.status.value}\n"
                f"خطأ: {html.escape(order.last_error_message or '-')}"
            )
            await callback.message.answer(text, reply_markup=admin_order_keyboard(str(order.id)))

    @router.callback_query(F.data.startswith("adm:deliver:"))
    async def begin_delivery(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminDeliveryFlow.value)
        await state.set_data({"order_id": callback.data.rsplit(":", 1)[1]})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل بيانات/رسالة التسليم للعميل:", reply_markup=cancel_keyboard()
            )

    @router.message(AdminDeliveryFlow.value, F.text)
    async def complete_delivery(
        message: Message, session: AsyncSession, state: FSMContext, bot: Bot
    ) -> None:
        data = await state.get_data()
        try:
            order = await order_service.complete_manual(
                session,
                order_id=uuid.UUID(data["order_id"]),
                admin_id=message.from_user.id,
                delivery=message.text,
            )
        except OrderError as exc:
            await message.answer(str(exc))
            return
        await session.commit()
        await state.clear()
        await message.answer("✅ تم تسليم الطلب.")
        await bot.send_message(
            order.user_id,
            f"✅ اكتمل طلبك <code>{order.public_code}</code>.\n"
            f"اعرض التسليم بالأمر /delivery_{order.public_code}",
        )

    @router.callback_query(F.data.startswith("adm:refund:"))
    async def begin_refund(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminRefundFlow.reason)
        await state.set_data({"order_id": callback.data.rsplit(":", 1)[1]})
        await callback.answer()
        if callback.message:
            await callback.message.answer("اكتب سبب رد الرصيد:", reply_markup=cancel_keyboard())

    @router.message(AdminRefundFlow.reason, F.text)
    async def refund_order(
        message: Message, session: AsyncSession, state: FSMContext, bot: Bot
    ) -> None:
        data = await state.get_data()
        order = await session.scalar(
            select(Order).where(Order.id == uuid.UUID(data["order_id"])).with_for_update()
        )
        if order is None:
            await state.clear()
            await message.answer("الطلب غير موجود.")
            return
        try:
            await order_service.refund(
                session,
                order=order,
                actor_user_id=message.from_user.id,
                reason=message.text.strip(),
            )
        except OrderError as exc:
            await message.answer(str(exc))
            return
        await session.commit()
        await state.clear()
        await message.answer("✅ تم رد الرصيد.")
        await bot.send_message(
            order.user_id,
            f"↩️ تم رد مبلغ {order.total_amount:g} USDT للطلب "
            f"<code>{order.public_code}</code>.\n"
            f"السبب: {html.escape(order.last_error_message or '')}",
        )

    @router.callback_query(F.data == "adm:channels")
    async def channels_list(callback: CallbackQuery, session: AsyncSession) -> None:
        channels = list(
            await session.scalars(select(PaymentChannel).order_by(PaymentChannel.sort_order))
        )
        builder = InlineKeyboardBuilder()
        for channel in channels:
            mark = "🟢" if channel.is_active else "⚫️"
            builder.button(
                text=f"{mark} {channel.name_ar}", callback_data=f"adm:channel:{channel.code}"
            )
        builder.button(text="↩️ لوحة الإدارة", callback_data="adm:dashboard")
        builder.adjust(1)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "🏦 <b>طرق الدفع</b>", reply_markup=builder.as_markup()
            )

    @router.callback_query(F.data.startswith("adm:channel:"))
    async def channel_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        code = callback.data.rsplit(":", 1)[1]
        channel = await session.get(PaymentChannel, code)
        if channel is None:
            await callback.answer("طريقة الدفع غير موجودة", show_alert=True)
            return
        text = (
            f"<b>{channel.name_ar}</b>\n"
            f"الحالة: {'مفعلة' if channel.is_active else 'معطلة'}\n"
            f"سعر الصرف: 1 USDT = {channel.units_per_usdt:g} {channel.settlement_currency}\n"
            f"الرسوم: {channel.fee_percent:g}%\n"
            f"الحساب: {html.escape(channel.account_label or '-')}\n\n"
            f"{html.escape(channel.instructions_ar)}"
        )
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                text, reply_markup=admin_channel_keyboard(code, channel.is_active)
            )

    @router.callback_query(F.data.startswith("adm:chtoggle:"))
    async def toggle_channel(callback: CallbackQuery, session: AsyncSession) -> None:
        code = callback.data.rsplit(":", 1)[1]
        channel = await session.get(PaymentChannel, code)
        if channel is None:
            await callback.answer("غير موجود", show_alert=True)
            return
        if not channel.is_active:
            if channel.units_per_usdt <= 0:
                await callback.answer("اضبط سعر الصرف أولًا", show_alert=True)
                return
            if channel.kind.value == "manual" and not channel.account_label:
                await callback.answer("أضف رقم الحساب في التعليمات أولًا", show_alert=True)
                return
            if code == "binance" and not settings.binance_pay_enabled:
                await callback.answer("فعّل مفاتيح Binance Pay في الإعدادات أولًا", show_alert=True)
                return
        channel.is_active = not channel.is_active
        add_audit(
            session,
            actor_user_id=callback.from_user.id,
            action="payment_channel.toggled",
            entity_type="payment_channel",
            entity_id=code,
            metadata={"active": channel.is_active},
        )
        await callback.answer("تم التحديث")
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=admin_channel_keyboard(code, channel.is_active)
            )

    @router.callback_query(F.data.startswith("adm:chrate:"))
    async def begin_channel_rate(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminChannelRateFlow.value)
        await state.set_data({"channel_code": callback.data.rsplit(":", 1)[1]})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل عدد وحدات عملة الدفع مقابل 1 USDT. مثال: 2500",
                reply_markup=cancel_keyboard(),
            )

    @router.message(AdminChannelRateFlow.value, F.text)
    async def save_channel_rate(message: Message, session: AsyncSession, state: FSMContext) -> None:
        try:
            rate = money(Decimal(message.text.replace(",", ".").strip()))
        except InvalidOperation:
            await message.answer("أرسل رقمًا صحيحًا.")
            return
        if rate <= 0:
            await message.answer("السعر يجب أن يكون أكبر من صفر.")
            return
        data = await state.get_data()
        channel = await session.get(PaymentChannel, data["channel_code"])
        if channel is None:
            await state.clear()
            await message.answer("طريقة الدفع غير موجودة.")
            return
        old = channel.units_per_usdt
        channel.units_per_usdt = rate
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="payment_channel.rate_changed",
            entity_type="payment_channel",
            entity_id=channel.code,
            metadata={"old": format(old, "f"), "new": format(rate, "f")},
        )
        await state.clear()
        await message.answer(f"✅ السعر الجديد: 1 USDT = {rate:g} {channel.settlement_currency}")

    @router.callback_query(F.data.startswith("adm:chinfo:"))
    async def begin_channel_info(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminChannelInstructionsFlow.value)
        await state.set_data({"channel_code": callback.data.rsplit(":", 1)[1]})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل في رسالة واحدة رقم الحساب/المحفظة ثم تعليمات التحويل.",
                reply_markup=cancel_keyboard(),
            )

    @router.message(AdminChannelInstructionsFlow.value, F.text)
    async def save_channel_info(message: Message, session: AsyncSession, state: FSMContext) -> None:
        value = message.text.strip()
        if len(value) < 5 or len(value) > 2000:
            await message.answer("التعليمات غير صالحة.")
            return
        data = await state.get_data()
        channel = await session.get(PaymentChannel, data["channel_code"])
        if channel is None:
            await state.clear()
            await message.answer("طريقة الدفع غير موجودة.")
            return
        first_line, _, rest = value.partition("\n")
        channel.account_label = first_line[:200]
        channel.instructions_ar = (rest or first_line)[:2000]
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="payment_channel.instructions_changed",
            entity_type="payment_channel",
            entity_id=channel.code,
        )
        await state.clear()
        await message.answer("✅ حُفظت تعليمات التحويل.")

    @router.callback_query(F.data.startswith("adm:chfee:"))
    async def begin_channel_fee(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminChannelFeeFlow.value)
        await state.set_data({"channel_code": callback.data.rsplit(":", 1)[1]})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل نسبة الرسوم من 0 إلى 25. مثال: 3",
                reply_markup=cancel_keyboard(),
            )

    @router.message(AdminChannelFeeFlow.value, F.text)
    async def save_channel_fee(message: Message, session: AsyncSession, state: FSMContext) -> None:
        try:
            fee = Decimal(message.text.replace(",", ".").strip()).quantize(Decimal("0.0001"))
        except InvalidOperation:
            await message.answer("أرسل نسبة صحيحة.")
            return
        if fee < 0 or fee > 25:
            await message.answer("النسبة يجب أن تكون بين 0 و25.")
            return
        data = await state.get_data()
        channel = await session.get(PaymentChannel, data["channel_code"])
        if channel is None:
            await state.clear()
            await message.answer("طريقة الدفع غير موجودة.")
            return
        old = channel.fee_percent
        channel.fee_percent = fee
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="payment_channel.fee_changed",
            entity_type="payment_channel",
            entity_id=channel.code,
            metadata={"old": format(old, "f"), "new": format(fee, "f")},
        )
        await state.clear()
        await message.answer(f"✅ أصبحت رسوم {channel.name_ar}: {fee:g}%")

    return router
