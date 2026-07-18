from __future__ import annotations

import html
import secrets
import uuid
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.crypto import PayloadCipher
from app.enums import OrderStatus, PaymentKind, PaymentStatus
from app.keyboards import (
    cancel_keyboard,
    categories_keyboard,
    confirm_buy_keyboard,
    main_menu,
    payment_channels_keyboard,
    product_keyboard,
    products_keyboard,
)
from app.models import Order, PaymentChannel, SupplierCatalogItem, Wallet
from app.services.catalog import (
    active_categories,
    active_products,
    get_active_product,
    validate_customer_input,
)
from app.services.orders import OrderError, OrderService
from app.services.payments.binance import BinancePayClient, BinancePayError
from app.services.payments.service import PaymentError, PaymentQuote, PaymentService
from app.services.supplier_catalog import catalog_item_for_product
from app.services.users import upsert_telegram_user
from app.services.wallet import InsufficientBalance
from app.states import BuyFlow, DepositFlow

STATUS_AR = {
    OrderStatus.QUEUED: "قيد التنفيذ",
    OrderStatus.PROCESSING: "قيد التنفيذ",
    OrderStatus.PROVIDER_PENDING: "قيد التنفيذ لدى المورد",
    OrderStatus.REVIEW_REQUIRED: "تحت مراجعة الإدارة",
    OrderStatus.COMPLETED: "مكتمل",
    OrderStatus.FAILED: "فشل",
    OrderStatus.REFUNDED: "تم رد الرصيد",
    OrderStatus.CANCELED: "ملغي",
}


def build_user_router(
    *,
    settings: Settings,
    order_service: OrderService,
    payment_service: PaymentService,
    cipher: PayloadCipher,
    binance: BinancePayClient | None,
) -> Router:
    router = Router(name="user")

    async def ensure_user(message: Message, session: AsyncSession) -> None:
        if not message.from_user:
            return
        await upsert_telegram_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            display_name=message.from_user.full_name,
        )

    @router.message(CommandStart())
    async def start(message: Message, session: AsyncSession, state: FSMContext) -> None:
        await state.clear()
        await ensure_user(message, session)
        await message.answer(
            f"أهلًا بك في <b>{html.escape(settings.store_name)}</b> 👋\n\n"
            "يمكنك شراء الخدمات من رصيدك ومتابعة الطلبات من داخل البوت.",
            reply_markup=main_menu(message.from_user.id in settings.admin_ids),
        )

    @router.message(Command("menu"))
    async def menu(message: Message, session: AsyncSession, state: FSMContext) -> None:
        await state.clear()
        await ensure_user(message, session)
        await message.answer(
            "القائمة الرئيسية:", reply_markup=main_menu(message.from_user.id in settings.admin_ids)
        )

    async def show_catalog(target: Message | CallbackQuery, session: AsyncSession) -> None:
        categories = await active_categories(session)
        text = "🛍 <b>اختر قسم الخدمات</b>"
        if not categories:
            text = "لا توجد أقسام متاحة حاليًا."
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message:
                await target.message.edit_text(text, reply_markup=categories_keyboard(categories))
        else:
            await target.answer(text, reply_markup=categories_keyboard(categories))

    @router.message(F.text == "🛍 المتجر")
    async def catalog_message(message: Message, session: AsyncSession) -> None:
        await ensure_user(message, session)
        await show_catalog(message, session)

    @router.callback_query(F.data == "catalog")
    async def catalog_callback(callback: CallbackQuery, session: AsyncSession) -> None:
        await show_catalog(callback, session)

    @router.callback_query(F.data.startswith("cat:"))
    async def category_products(callback: CallbackQuery, session: AsyncSession) -> None:
        try:
            category_id = int(callback.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("طلب غير صالح", show_alert=True)
            return
        products = await active_products(session, category_id)
        product_ids = [product.id for product in products]
        snapshots = {}
        if product_ids:
            items = await session.scalars(
                select(SupplierCatalogItem).where(SupplierCatalogItem.product_id.in_(product_ids))
            )
            snapshots = {item.product_id: item for item in items if item.product_id is not None}
        await callback.answer()
        if callback.message:
            text = "اختر الخدمة:" if products else "لا توجد خدمات مفعّلة في هذا القسم."
            if callback.message.photo:
                await callback.message.answer(
                    text, reply_markup=products_keyboard(products, snapshots)
                )
                await callback.message.delete()
            else:
                await callback.message.edit_text(
                    text,
                    reply_markup=products_keyboard(products, snapshots),
                )

    @router.callback_query(F.data.startswith("prd:"))
    async def product_details(callback: CallbackQuery, session: AsyncSession) -> None:
        try:
            product_id = uuid.UUID(callback.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("الخدمة غير صالحة", show_alert=True)
            return
        product = await get_active_product(session, product_id)
        if product is None:
            await callback.answer("الخدمة غير متاحة", show_alert=True)
            return
        snapshot = await catalog_item_for_product(session, product.id)
        requirement = (
            "التسليم: تلقائي فور اكتمال طلب المورد"
            if snapshot is not None and snapshot.delivery_type == "stock"
            else f"المطلوب: {html.escape(product.customer_input_label)}"
        )
        supplier_facts: list[str] = []
        if snapshot is not None:
            if snapshot.stock is not None:
                supplier_facts.append(f"المخزون: <b>{snapshot.stock}</b>")
            if snapshot.warranty_days:
                supplier_facts.append(f"الضمان: <b>{snapshot.warranty_days} يومًا</b>")
        facts = f"\n{' | '.join(supplier_facts)}" if supplier_facts else ""
        text = (
            f"<b>{html.escape(product.name_ar)}</b>\n\n"
            f"{html.escape(product.description_ar)}\n\n"
            f"السعر: <b>{product.sale_price:g} {product.currency}</b>\n"
            f"{requirement}{facts}"
        )
        if snapshot is None or snapshot.delivery_type != "stock":
            text += f"\n\n⚠️ {html.escape(product.customer_input_help)}"
        if product.terms_ar:
            text += f"\n\nالشروط:\n{html.escape(product.terms_ar)}"
        await callback.answer()
        if callback.message:
            if snapshot is not None and snapshot.image_url:
                try:
                    if len(text) <= 1000:
                        await callback.message.answer_photo(
                            snapshot.image_url,
                            caption=text,
                            reply_markup=product_keyboard(product),
                        )
                    else:
                        await callback.message.answer_photo(
                            snapshot.image_url,
                            caption=(
                                f"<b>{html.escape(product.name_ar)}</b>\n"
                                f"💵 <b>{product.sale_price:g} {product.currency}</b>"
                                f"{facts}"
                            ),
                        )
                        await callback.message.answer(
                            text,
                            reply_markup=product_keyboard(product),
                        )
                    await callback.message.delete()
                    return
                except (TelegramBadRequest, TelegramAPIError):
                    pass
            await callback.message.edit_text(text, reply_markup=product_keyboard(product))

    @router.callback_query(F.data.startswith("buy:"))
    async def begin_buy(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
        try:
            product_id = uuid.UUID(callback.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("الخدمة غير صالحة", show_alert=True)
            return
        product = await get_active_product(session, product_id)
        wallet = await session.get(Wallet, callback.from_user.id)
        if product is None or wallet is None:
            await callback.answer("الخدمة أو المحفظة غير متاحة", show_alert=True)
            return
        if wallet.balance < product.sale_price:
            await callback.answer(
                f"رصيدك {wallet.balance:g} USDT والسعر {product.sale_price:g} USDT",
                show_alert=True,
            )
            return
        snapshot = await catalog_item_for_product(session, product.id)
        token = secrets.token_hex(8)
        if snapshot is not None and snapshot.delivery_type == "stock":
            await state.set_state(BuyFlow.confirmation)
            await state.set_data(
                {
                    "product_id": str(product.id),
                    "purchase_token": token,
                    "customer_input": "",
                }
            )
            await callback.answer()
            if callback.message:
                await callback.message.answer(
                    f"راجع الطلب:\n\n"
                    f"الخدمة: <b>{html.escape(product.name_ar)}</b>\n"
                    f"السعر: <b>{product.sale_price:g} {product.currency}</b>\n"
                    "التسليم: تلقائي بعد تنفيذ المورد\n\n"
                    "بعد التأكيد سيُخصم المبلغ من الرصيد.",
                    reply_markup=confirm_buy_keyboard(token),
                )
            return
        await state.set_state(BuyFlow.customer_input)
        await state.set_data({"product_id": str(product.id), "purchase_token": token})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"أرسل الآن: <b>{html.escape(product.customer_input_label)}</b>\n"
                f"{html.escape(product.customer_input_help)}",
                reply_markup=cancel_keyboard(),
            )

    @router.message(BuyFlow.customer_input, F.text)
    async def receive_customer_input(
        message: Message, session: AsyncSession, state: FSMContext
    ) -> None:
        data = await state.get_data()
        product = await get_active_product(session, uuid.UUID(data["product_id"]))
        if product is None:
            await state.clear()
            await message.answer("الخدمة لم تعد متاحة.")
            return
        try:
            customer_input = validate_customer_input(product, message.text)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=cancel_keyboard())
            return
        await state.update_data(customer_input=customer_input)
        await state.set_state(BuyFlow.confirmation)
        await message.answer(
            f"راجع الطلب:\n\n"
            f"الخدمة: <b>{html.escape(product.name_ar)}</b>\n"
            f"السعر: <b>{product.sale_price:g} {product.currency}</b>\n"
            f"{html.escape(product.customer_input_label)}: "
            f"<code>{html.escape(customer_input)}</code>\n\n"
            "بعد التأكيد سيُخصم المبلغ من الرصيد.",
            reply_markup=confirm_buy_keyboard(data["purchase_token"]),
        )

    @router.callback_query(BuyFlow.confirmation, F.data.startswith("buyok:"))
    async def confirm_buy(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        data = await state.get_data()
        token = callback.data.split(":", 1)[1]
        if token != data.get("purchase_token"):
            await callback.answer("انتهت صلاحية التأكيد", show_alert=True)
            return
        try:
            order = await order_service.place_order(
                session,
                user_id=callback.from_user.id,
                product_id=uuid.UUID(data["product_id"]),
                customer_input=data["customer_input"],
                idempotency_key=f"tg:{callback.from_user.id}:buy:{token}",
            )
        except InsufficientBalance:
            await callback.answer("الرصيد غير كافٍ", show_alert=True)
            return
        except OrderError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await callback.answer("تم إنشاء الطلب")
        if callback.message:
            await callback.message.edit_text(
                f"✅ تم إنشاء الطلب <code>{order.public_code}</code>\n"
                f"الحالة: {STATUS_AR[order.status]}\n"
                "يمكن متابعته من «📦 طلباتي»."
            )

    @router.message(F.text == "💰 رصيدي")
    async def wallet_balance(message: Message, session: AsyncSession) -> None:
        await ensure_user(message, session)
        wallet = await session.get(Wallet, message.from_user.id)
        await message.answer(f"رصيدك الحالي: <b>{wallet.balance:g} USDT</b>")

    @router.message(F.text == "📦 طلباتي")
    async def my_orders(message: Message, session: AsyncSession) -> None:
        orders = list(
            await session.scalars(
                select(Order)
                .where(Order.user_id == message.from_user.id)
                .order_by(Order.created_at.desc())
                .limit(10)
            )
        )
        if not orders:
            await message.answer("لا توجد لديك طلبات بعد.")
            return
        lines = ["📦 <b>آخر طلباتك</b>"]
        for order in orders:
            line = (
                f"\n<code>{order.public_code}</code> — {html.escape(order.product_name_snapshot)}\n"
                f"{STATUS_AR[order.status]} — {order.total_amount:g} {order.currency}"
            )
            if order.status is OrderStatus.COMPLETED:
                line += f"\nعرض التسليم: /delivery_{order.public_code}"
            lines.append(line)
        await message.answer("\n".join(lines))

    @router.message(F.text.regexp(r"^/delivery_[A-Z0-9]+$"))
    async def show_delivery(message: Message, session: AsyncSession) -> None:
        public_code = message.text.removeprefix("/delivery_")
        order = await session.scalar(
            select(Order).where(
                Order.public_code == public_code,
                Order.user_id == message.from_user.id,
                Order.status == OrderStatus.COMPLETED,
            )
        )
        if order is None or not order.delivery_encrypted:
            await message.answer("بيانات التسليم غير متاحة.")
            return
        delivery = cipher.decrypt(order.delivery_encrypted)
        await message.answer(
            f"📨 تسليم الطلب <code>{order.public_code}</code>:\n\n"
            f"<code>{html.escape(delivery)}</code>\n\n"
            "احفظ البيانات في مكان آمن ولا تشاركها."
        )

    @router.message(F.text == "➕ شحن الرصيد")
    async def deposit_channels(message: Message, session: AsyncSession) -> None:
        channels = list(
            await session.scalars(
                select(PaymentChannel)
                .where(PaymentChannel.is_active.is_(True))
                .order_by(PaymentChannel.sort_order)
            )
        )
        if not channels:
            await message.answer("لا توجد طريقة شحن مفعلة حاليًا. تواصل مع الدعم.")
            return
        await message.answer("اختر طريقة الشحن:", reply_markup=payment_channels_keyboard(channels))

    @router.callback_query(F.data.startswith("pay:"))
    async def choose_payment_channel(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        code = callback.data.split(":", 1)[1]
        channel = await session.get(PaymentChannel, code)
        if channel is None or not channel.is_active:
            await callback.answer("طريقة الدفع غير متاحة", show_alert=True)
            return
        await state.set_state(DepositFlow.amount)
        await state.set_data({"channel_code": code})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"أرسل مبلغ الرصيد المطلوب إضافته بـ USDT\n"
                f"الحد: {channel.min_credit:g} — {channel.max_credit:g} USDT",
                reply_markup=cancel_keyboard(),
            )

    @router.message(DepositFlow.amount, F.text)
    async def deposit_amount(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        data = await state.get_data()
        channel = await session.get(PaymentChannel, data["channel_code"])
        if channel is None or not channel.is_active:
            await state.clear()
            await message.answer("طريقة الدفع لم تعد متاحة.")
            return
        try:
            amount = Decimal(message.text.replace(",", ".").strip())
            quote = payment_service.quote(channel, amount)
        except (InvalidOperation, PaymentError):
            await message.answer("أرسل مبلغًا رقميًا صحيحًا ضمن الحدود المحددة.")
            return
        await state.update_data(
            credit_amount=format(quote.credit_amount, "f"),
            expected_amount=format(quote.expected_amount, "f"),
            settlement_currency=quote.settlement_currency,
            fee_percent=format(quote.fee_percent, "f"),
            rate=format(quote.rate, "f"),
        )

        if channel.kind is PaymentKind.BINANCE_PAY:
            if binance is None:
                await state.clear()
                await message.answer("Binance Pay غير مهيأ حاليًا.")
                return
            payment = await payment_service.create_pending(
                session,
                user_id=message.from_user.id,
                channel=channel,
                quote=quote,
            )
            await session.commit()
            webhook_url = (
                f"{settings.public_base_url}/webhooks/binance/"
                f"{settings.webhook_secret_path.get_secret_value()}"
            )
            try:
                checkout = await binance.create_order(
                    merchant_trade_no=payment.public_code,
                    amount=quote.expected_amount,
                    currency=quote.settlement_currency,
                    description="Digital store wallet top up",
                    product_name="Wallet top up",
                    webhook_url=webhook_url,
                )
            except BinancePayError:
                payment.status = PaymentStatus.REVIEW_REQUIRED
                await session.commit()
                await state.clear()
                await message.answer(
                    f"تعذر إنشاء رابط الدفع للطلب <code>{payment.public_code}</code>. "
                    "لم يُضف أي رصيد، وسيقوم الأدمن بمراجعته."
                )
                return
            payment.external_id = checkout.prepay_id
            payment.checkout_url = checkout.checkout_url
            await session.commit()
            await state.clear()
            await message.answer(
                f"طلب الشحن: <code>{payment.public_code}</code>\n"
                f"المبلغ: <b>{quote.expected_amount:g} {quote.settlement_currency}</b>\n\n"
                f"ادفع من الرابط الرسمي:\n{html.escape(checkout.checkout_url)}\n\n"
                "سيُضاف الرصيد تلقائيًا بعد إشعار Binance الموقّع."
            )
            return

        await state.set_state(DepositFlow.reference)
        await message.answer(
            f"حوّل بالضبط: <b>{quote.expected_amount:g} {quote.settlement_currency}</b>\n"
            f"سيضاف لرصيدك: <b>{quote.credit_amount:g} USDT</b>\n\n"
            f"{html.escape(channel.instructions_ar)}\n"
            f"{html.escape(channel.account_label)}\n\n"
            "بعد التحويل أرسل رقم العملية/المرجع.",
            reply_markup=cancel_keyboard(),
        )

    @router.message(DepositFlow.reference, F.text)
    async def deposit_reference(message: Message, state: FSMContext) -> None:
        reference = message.text.strip()
        if len(reference) < 3 or len(reference) > 200:
            await message.answer("أرسل رقم عملية صالحًا.")
            return
        await state.update_data(payer_reference=reference)
        await state.set_state(DepositFlow.proof)
        await message.answer("أرسل الآن صورة واضحة لإثبات التحويل.")

    @router.message(DepositFlow.proof, F.photo)
    async def deposit_proof(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        data = await state.get_data()
        channel = await session.get(PaymentChannel, data["channel_code"])
        if channel is None or not channel.is_active:
            await state.clear()
            await message.answer("طريقة الدفع لم تعد متاحة.")
            return
        quote = PaymentQuote(
            credit_amount=Decimal(data["credit_amount"]),
            expected_amount=Decimal(data["expected_amount"]),
            credit_currency="USDT",
            settlement_currency=data["settlement_currency"],
            fee_percent=Decimal(data["fee_percent"]),
            rate=Decimal(data["rate"]),
        )
        payment = await payment_service.create_pending(
            session,
            user_id=message.from_user.id,
            channel=channel,
            quote=quote,
            payer_reference=data["payer_reference"],
            proof_file_id=message.photo[-1].file_id,
        )
        await session.commit()
        await state.clear()
        await message.answer(
            f"✅ استلمنا طلب الشحن <code>{payment.public_code}</code>.\n"
            "لن يُضاف الرصيد إلا بعد مطابقة التحويل من الأدمن."
        )
        caption = (
            f"طلب شحن جديد {payment.public_code}\n"
            f"المستخدم: {message.from_user.id}\n"
            f"الطريقة: {channel.name_ar}\n"
            f"المطلوب: {payment.expected_amount:g} {payment.settlement_currency}\n"
            f"الرصيد: {payment.credit_amount:g} USDT\n"
            f"المرجع: {html.escape(payment.payer_reference or '')}"
        )
        for admin_id in settings.admin_ids:
            try:
                await bot.send_photo(admin_id, payment.proof_file_id, caption=caption)
            except Exception:
                pass

    @router.message(DepositFlow.proof)
    async def proof_must_be_photo(message: Message) -> None:
        await message.answer("أرسل الإثبات كصورة، أو اضغط إلغاء.")

    @router.message(F.text == "🧑‍💻 الدعم")
    async def support(message: Message) -> None:
        await message.answer(
            f"للدعم تواصل مع: {html.escape(settings.support_username)}\n"
            "أرسل رقم الطلب أو رقم الشحن، ولا ترسل كلمة مرورك أو رمز التحقق."
        )

    return router
