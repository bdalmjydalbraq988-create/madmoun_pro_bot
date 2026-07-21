from __future__ import annotations

import html
import secrets
import uuid
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings
from app.crypto import PayloadCipher
from app.enums import FulfillmentMode, LedgerKind, OrderStatus, PaymentStatus
from app.formatting import decimal_number, money_label
from app.keyboards import (
    admin_channel_keyboard,
    admin_dashboard_keyboard,
    admin_order_keyboard,
    admin_payment_keyboard,
    admin_product_keyboard,
    admin_referral_keyboard,
    admin_supplier_keyboard,
    admin_user_keyboard,
    admin_user_orders_keyboard,
    cancel_keyboard,
)
from app.models import (
    Category,
    Order,
    Payment,
    PaymentChannel,
    Product,
    Referral,
    SupplierCatalogItem,
    User,
    Wallet,
)
from app.services.audit import add_audit
from app.services.orders import OrderError, OrderService
from app.services.payments.service import PaymentError, PaymentService
from app.services.providers.quantumvault import VenteBotProvider
from app.services.supplier_catalog import (
    calculate_sale_price,
    catalog_item_for_product,
    get_sync_config,
    reprice_unlocked_products,
    sync_supplier_catalog,
)
try:
    from app.services.supplier_catalog import repair_supplier_catalog_visibility as _native_repair
except ImportError:  # Compatibility with partially synchronized hosting deployments.
    _native_repair = None
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
    AdminReferralConfigFlow,
    AdminRefundFlow,
    AdminSupplierMarkupFlow,
    AdminSupplierMinimumProfitFlow,
    AdminUserLookupFlow,
    AdminWalletFlow,
)


class IsAdmin(Filter):
    def __init__(self, admin_ids: list[int]) -> None:
        self.admin_ids = set(admin_ids)

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id in self.admin_ids)


ADMIN_ORDER_STATUS_AR = {
    OrderStatus.QUEUED: "قيد التنفيذ",
    OrderStatus.PROCESSING: "قيد التنفيذ",
    OrderStatus.PROVIDER_PENDING: "قيد التنفيذ لدى المورد",
    OrderStatus.REVIEW_REQUIRED: "تحت المراجعة",
    OrderStatus.COMPLETED: "مكتمل",
    OrderStatus.FAILED: "فشل",
    OrderStatus.REFUNDED: "تم رد الرصيد",
    OrderStatus.CANCELED: "ملغي",
}


async def _repair_supplier_catalog_visibility(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
):
    """Use the native repair, or a safe compatibility repair on mixed deployments."""

    if _native_repair is not None:
        return await _native_repair(session, actor_user_id=actor_user_id)

    config = await get_sync_config(session)
    config.auto_activate = True
    snapshots = list(
        await session.scalars(
            select(SupplierCatalogItem).where(
                SupplierCatalogItem.provider_code == "ventebot",
                SupplierCatalogItem.product_id.is_not(None),
            )
        )
    )
    activated = unavailable = categories_reactivated = active = 0
    seen_categories: set[int] = set()
    for snapshot in snapshots:
        product = await session.get(Product, snapshot.product_id)
        if product is None:
            continue
        available = (
            product.cost_price is not None
            and product.cost_price > 0
            and (snapshot.stock is None or snapshot.stock > 0)
        )
        snapshot.activation_locked = False
        if not available:
            product.is_active = False
            unavailable += 1
            continue
        active += 1
        if not product.is_active:
            product.is_active = True
            activated += 1
        if product.category_id not in seen_categories:
            category = await session.get(Category, product.category_id)
            if category is not None and not category.is_active:
                category.is_active = True
                categories_reactivated += 1
            seen_categories.add(product.category_id)

    add_audit(
        session,
        actor_user_id=actor_user_id,
        action="supplier.catalog_visibility_repaired_compat",
        entity_type="supplier",
        entity_id="ventebot",
        metadata={
            "linked": len(snapshots),
            "active": active,
            "activated": activated,
            "unavailable": unavailable,
            "categories_reactivated": categories_reactivated,
        },
    )
    await session.flush()
    return SimpleNamespace(
        linked=len(snapshots),
        active=active,
        activated=activated,
        unavailable=unavailable,
        categories_reactivated=categories_reactivated,
    )


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
                .where(
                    Payment.status.in_(
                        [PaymentStatus.PENDING, PaymentStatus.REVIEW_REQUIRED]
                    )
                )
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
            f"مجموع أرصدة العملاء: {money_label(Decimal(total_balances))}\n\n"
            f"إصدار البوت: <code>{__version__}</code>"
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

    async def referral_panel_text(session: AsyncSession) -> tuple[str, bool]:
        config = await order_service.referrals.get_config(session)
        registered = await session.scalar(select(func.count()).select_from(Referral)) or 0
        rewarded = (
            await session.scalar(
                select(func.count())
                .select_from(Referral)
                .where(Referral.rewarded_at.is_not(None))
            )
            or 0
        )
        paid = await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        Referral.referrer_reward_amount + Referral.invitee_reward_amount
                    ),
                    0,
                )
            ).where(Referral.rewarded_at.is_not(None))
        )
        text = (
            "🎁 <b>نظام الإحالة</b>\n\n"
            f"الحالة: {'✅ يعمل' if config.enabled else '⏸ متوقف'}\n"
            f"الإحالات المسجلة: {registered}\n"
            f"الإحالات المكافأة: {rewarded}\n"
            f"إجمالي المكافآت المصروفة: {money_label(paid or 0)}\n\n"
            f"مكافأة الداعي: <b>{money_label(config.referrer_reward)}</b>\n"
            f"هدية المدعو: <b>{money_label(config.invitee_reward)}</b>\n"
            f"الحد الأدنى لأول طلب: <b>{money_label(config.minimum_order_amount)}</b>\n\n"
            "الحماية: عميل جديد فقط، لا إحالة ذاتية، لا تغيير للداعي، "
            "والصرف مرة واحدة بعد أول طلب مكتمل."
        )
        return text, config.enabled

    @router.callback_query(F.data == "adm:referrals")
    async def referral_panel(callback: CallbackQuery, session: AsyncSession) -> None:
        text, enabled = await referral_panel_text(session)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=admin_referral_keyboard(enabled),
            )

    @router.callback_query(F.data == "adm:referrals:toggle")
    async def toggle_referrals(callback: CallbackQuery, session: AsyncSession) -> None:
        config = await order_service.referrals.get_config(session)
        if not config.enabled and money(config.referrer_reward) <= 0 and money(
            config.invitee_reward
        ) <= 0:
            await callback.answer(
                "اضبط مكافأة الداعي أو المدعو أولًا، ثم شغّل النظام.",
                show_alert=True,
            )
            return
        config.enabled = not config.enabled
        add_audit(
            session,
            actor_user_id=callback.from_user.id,
            action="referral.config_toggled",
            entity_type="referral_config",
            entity_id="default",
            metadata={"enabled": config.enabled},
        )
        text, enabled = await referral_panel_text(session)
        await callback.answer("تم تحديث حالة نظام الإحالة")
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=admin_referral_keyboard(enabled),
            )

    @router.callback_query(F.data.startswith("adm:referrals:set:"))
    async def begin_referral_config(callback: CallbackQuery, state: FSMContext) -> None:
        field = callback.data.rsplit(":", 1)[-1]
        prompts = {
            "referrer": "أرسل مكافأة الداعي بوحدة USDT (مثال 0.25):",
            "invitee": "أرسل هدية العميل المدعو بوحدة USDT (مثال 0.10):",
            "minimum": "أرسل أقل قيمة لأول طلب مؤهل بوحدة USDT (مثال 1):",
        }
        if field not in prompts:
            await callback.answer("إعداد غير صالح", show_alert=True)
            return
        await state.set_state(AdminReferralConfigFlow.value)
        await state.set_data({"referral_field": field})
        await callback.answer()
        if callback.message:
            await callback.message.answer(prompts[field], reply_markup=cancel_keyboard())

    @router.message(AdminReferralConfigFlow.value, F.text)
    async def save_referral_config(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        try:
            value = money(Decimal(message.text.replace(",", ".").strip()))
        except (InvalidOperation, ValueError):
            await message.answer("أرسل رقمًا صحيحًا، مثل 0.25")
            return
        if value < 0 or value > Decimal("1000"):
            await message.answer("القيمة يجب أن تكون بين 0 و1000 USDT.")
            return
        data = await state.get_data()
        field = data.get("referral_field")
        config = await order_service.referrals.get_config(session)
        if field == "referrer":
            config.referrer_reward = value
        elif field == "invitee":
            config.invitee_reward = value
        elif field == "minimum":
            config.minimum_order_amount = value
        else:
            await state.clear()
            await message.answer("انتهت جلسة الإعداد؛ افتح نظام الإحالة مجددًا.")
            return
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="referral.config_updated",
            entity_type="referral_config",
            entity_id="default",
            metadata={"field": field, "value": format(value, "f")},
        )
        await state.clear()
        text, enabled = await referral_panel_text(session)
        await message.answer(
            "✅ تم حفظ الإعداد.\n\n" + text,
            reply_markup=admin_referral_keyboard(enabled),
        )

    async def user_card_text(session: AsyncSession, user_id: int) -> str | None:
        user = await session.get(User, user_id)
        if user is None:
            return None
        wallet = await session.get(Wallet, user_id)
        orders_count = (
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.user_id == user_id)
            )
            or 0
        )
        completed_count = (
            await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.user_id == user_id,
                    Order.status == OrderStatus.COMPLETED,
                )
            )
            or 0
        )
        referral = await session.get(Referral, user_id)
        referral_stats = await order_service.referrals.stats(session, user_id)
        username = f"@{html.escape(user.username)}" if user.username else "غير محدد"
        registered = user.created_at.strftime("%Y-%m-%d %H:%M")
        return (
            "👤 <b>بيانات المشترك</b>\n\n"
            f"الاسم: {html.escape(user.display_name or '-')}\n"
            f"المعرف: {username}\n"
            f"Telegram ID: <code>{user.telegram_id}</code>\n"
            f"الرصيد: <b>{money_label(wallet.balance if wallet is not None else 0)}</b>\n"
            f"الطلبات: {orders_count}\n"
            f"المكتملة: {completed_count}\n"
            f"دعاه العميل: "
            f"{f'<code>{referral.referrer_id}</code>' if referral else 'رابط مباشر'}\n"
            f"دعواته: {referral_stats.invited} — "
            f"مكافآته: {money_label(referral_stats.earned)}\n"
            f"تاريخ التسجيل: {registered} UTC"
        )

    @router.callback_query(F.data == "adm:users")
    async def subscribers_list(callback: CallbackQuery, session: AsyncSession) -> None:
        total = await session.scalar(select(func.count()).select_from(User)) or 0
        rows = (
            await session.execute(
                select(User, Wallet)
                .join(Wallet, Wallet.user_id == User.telegram_id, isouter=True)
                .order_by(User.created_at.desc())
                .limit(15)
            )
        ).all()
        builder = InlineKeyboardBuilder()
        for user, wallet in rows:
            name = (user.display_name or user.username or str(user.telegram_id))[:22]
            builder.button(
                text=f"👤 {name} | {money_label(wallet.balance if wallet else 0)}",
                callback_data=f"adm:user:{user.telegram_id}",
            )
        builder.button(text="🔎 بحث بالمعرف", callback_data="adm:usersearch")
        builder.button(text="↩️ لوحة الإدارة", callback_data="adm:dashboard")
        builder.adjust(1)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                f"👥 <b>المشتركون</b>\n\nالإجمالي: {total}\nآخر 15 مشتركًا:",
                reply_markup=builder.as_markup(),
            )

    @router.callback_query(F.data.startswith("adm:user:"))
    async def subscriber_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("معرف غير صالح", show_alert=True)
            return
        text = await user_card_text(session, user_id)
        if text is None:
            await callback.answer("المستخدم غير موجود", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(text, reply_markup=admin_user_keyboard(user_id))

    @router.callback_query(F.data.startswith("adm:userorders:"))
    async def subscriber_orders(callback: CallbackQuery, session: AsyncSession) -> None:
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("معرف غير صالح", show_alert=True)
            return
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("المستخدم غير موجود", show_alert=True)
            return
        orders = list(
            await session.scalars(
                select(Order)
                .where(Order.user_id == user_id)
                .order_by(Order.created_at.desc())
                .limit(10)
            )
        )
        title_name = html.escape(user.display_name or user.username or str(user.telegram_id))
        lines = [
            f"📦 <b>آخر طلبات {title_name}</b>",
            f"رقم العميل: <code>{user.telegram_id}</code>",
        ]
        if not orders:
            lines.append("\nلا توجد طلبات لهذا العميل حتى الآن.")
        else:
            for order in orders:
                status = ADMIN_ORDER_STATUS_AR.get(order.status, order.status.value)
                created = order.created_at.strftime("%Y-%m-%d %H:%M")
                lines.append(
                    "\n"
                    f"<code>{order.public_code}</code> — "
                    f"{html.escape(order.product_name_snapshot)}\n"
                    f"{status} • {money_label(order.total_amount, order.currency)} • "
                    f"{created} UTC"
                )
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "\n".join(lines),
                reply_markup=admin_user_orders_keyboard(user_id),
            )

    async def begin_user_search(target: Message | CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminUserLookupFlow.user_id)
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message:
                await target.message.answer(
                    "أرسل Telegram ID للمشترك:", reply_markup=cancel_keyboard()
                )
        else:
            await target.answer("أرسل Telegram ID للمشترك:", reply_markup=cancel_keyboard())

    @router.callback_query(F.data == "adm:usersearch")
    async def user_search_button(callback: CallbackQuery, state: FSMContext) -> None:
        await begin_user_search(callback, state)

    @router.message(Command("user"))
    async def user_search_command(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 1:
            await begin_user_search(message, state)
            return
        try:
            user_id = int(parts[1].strip())
        except ValueError:
            await message.answer("الاستخدام الصحيح: <code>/user 123456789</code>")
            return
        text = await user_card_text(session, user_id)
        if text is None:
            await message.answer("المستخدم غير موجود؛ يجب أن يرسل /start للبوت أولًا.")
            return
        await state.clear()
        await message.answer(text, reply_markup=admin_user_keyboard(user_id))

    @router.message(AdminUserLookupFlow.user_id, F.text)
    async def user_search_result(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer("أرسل رقم Telegram ID صحيحًا.")
            return
        text = await user_card_text(session, user_id)
        if text is None:
            await message.answer("المستخدم غير موجود؛ يجب أن يرسل /start للبوت أولًا.")
            return
        await state.clear()
        await message.answer(text, reply_markup=admin_user_keyboard(user_id))

    def supplier_client() -> VenteBotProvider | None:
        api_key = settings.supplier_api_key.get_secret_value()
        if not settings.supplier_enabled or not api_key:
            return None
        return VenteBotProvider(
            base_url=settings.supplier_base_url,
            api_key=api_key,
            me_path=settings.supplier_me_path,
            products_path=settings.supplier_products_path,
            quote_path=settings.supplier_quote_path,
            create_order_path=settings.supplier_create_order_path,
            status_path=settings.supplier_order_status_path,
            activation_identifier_path=settings.supplier_activation_identifier_path,
        )

    async def supplier_panel_text(session: AsyncSession) -> str:
        config = await get_sync_config(session)
        linked = (
            await session.scalar(
                select(func.count())
                .select_from(SupplierCatalogItem)
                .where(SupplierCatalogItem.provider_code == "ventebot")
            )
            or 0
        )
        active = (
            await session.scalar(
                select(func.count())
                .select_from(Product)
                .where(
                    Product.provider_code == "ventebot",
                    Product.is_active.is_(True),
                )
            )
            or 0
        )
        available = (
            await session.scalar(
                select(func.count())
                .select_from(SupplierCatalogItem)
                .where(
                    SupplierCatalogItem.provider_code == "ventebot",
                    or_(
                        SupplierCatalogItem.stock.is_(None),
                        SupplierCatalogItem.stock > 0,
                    ),
                )
            )
            or 0
        )
        locked = (
            await session.scalar(
                select(func.count())
                .select_from(SupplierCatalogItem)
                .where(
                    SupplierCatalogItem.provider_code == "ventebot",
                    SupplierCatalogItem.price_locked.is_(True),
                )
            )
            or 0
        )
        last_sync = (
            config.last_synced_at.strftime("%Y-%m-%d %H:%M UTC")
            if config.last_synced_at
            else "لم تتم بعد"
        )
        enabled = settings.supplier_enabled and bool(settings.supplier_api_key.get_secret_value())
        return (
            "🔌 <b>المورد والمزامنة</b>\n\n"
            f"حالة الربط: {'✅ جاهز' if enabled else '⚠️ غير مهيأ'}\n"
            f"الخدمات المربوطة: {linked}\n"
            f"المتاحة لدى المورد: {available}\n"
            f"الخدمات المفعلة: {active}\n"
            f"أسعار يدوية محمية: {locked}\n"
            f"نسبة الربح: {decimal_number(config.markup_percent)}%\n"
            f"أقل ربح للخدمة: {money_label(config.minimum_profit)}\n"
            f"التفعيل بعد الجلب: {'تلقائي' if config.auto_activate else 'معطل للمراجعة'}\n"
            f"آخر مزامنة: {last_sync}\n"
            f"نتيجة آخر مزامنة: {html.escape(config.last_sync_status)}\n"
            f"التفاصيل: {html.escape(config.last_sync_message or '-')}"
            + (
                "\n\n⚠️ الخدمات متاحة لكنها مخفية؛ اضغط «إصلاح وإظهار الخدمات المتاحة»."
                if available and not active
                else ""
            )
        )

    async def run_catalog_repair(
        session: AsyncSession,
        *,
        actor_user_id: int,
    ):
        sync_error: str | None = None
        provider = supplier_client()
        if provider is not None:
            try:
                await sync_supplier_catalog(
                    session,
                    provider=provider,
                    actor_user_id=actor_user_id,
                )
            except Exception as exc:
                # Existing safe snapshots are still enough to restore visibility.
                sync_error = str(exc)[:250]
            finally:
                await provider.close()
        repair = await _repair_supplier_catalog_visibility(
            session,
            actor_user_id=actor_user_id,
        )
        return repair, sync_error

    def catalog_repair_text(repair, sync_error: str | None) -> str:
        warning = (
            f"\n⚠️ تعذر التحديث اللحظي، فاستُخدمت آخر نسخة آمنة: "
            f"<code>{html.escape(sync_error)}</code>"
            if sync_error
            else ""
        )
        return (
            "✅ <b>تم إصلاح الكتالوج</b>\n\n"
            f"الخدمات المربوطة: {repair.linked}\n"
            f"الخدمات المتاحة والظاهرة: {repair.active}\n"
            f"خدمات أُعيد إظهارها الآن: {repair.activated}\n"
            f"خدمات غير متاحة لدى المورد: {repair.unavailable}\n"
            f"أقسام أُعيد تفعيلها: {repair.categories_reactivated}\n\n"
            "لم تتغير أسعارك اليدوية أو الطلبات أو أرصدة العملاء."
            f"{warning}"
        )

    @router.message(Command("repair_catalog"))
    async def repair_catalog_command(message: Message, session: AsyncSession) -> None:
        progress = await message.answer("⏳ أفحص المورد وأصلح ظهور الخدمات…")
        repair, sync_error = await run_catalog_repair(
            session,
            actor_user_id=message.from_user.id,
        )
        await progress.edit_text(catalog_repair_text(repair, sync_error))

    @router.callback_query(F.data == "adm:supplier:repair")
    async def repair_catalog_callback(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer("جاري إصلاح الكتالوج…")
        if callback.message:
            await callback.message.edit_text("⏳ أفحص المورد وأصلح ظهور الخدمات…")
        repair, sync_error = await run_catalog_repair(
            session,
            actor_user_id=callback.from_user.id,
        )
        if callback.message:
            await callback.message.edit_text(
                catalog_repair_text(repair, sync_error),
                reply_markup=admin_supplier_keyboard(auto_activate=True),
            )

    @router.callback_query(F.data == "adm:supplier")
    async def supplier_panel(callback: CallbackQuery, session: AsyncSession) -> None:
        config = await get_sync_config(session)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                await supplier_panel_text(session),
                reply_markup=admin_supplier_keyboard(auto_activate=config.auto_activate),
            )

    @router.callback_query(F.data == "adm:supplier:sync")
    async def supplier_sync(callback: CallbackQuery, session: AsyncSession) -> None:
        provider = supplier_client()
        if provider is None:
            await callback.answer(
                "أضف SUPPLIER_API_KEY واجعل SUPPLIER_ENABLED=true ثم أعد التشغيل.",
                show_alert=True,
            )
            return
        await callback.answer("جاري الاتصال بالمورد وجلب الخدمات…")
        if callback.message:
            await callback.message.edit_text("⏳ جاري التحقق من المورد وتحديث الكتالوج…")
        try:
            balance = await provider.balance()
            result = await sync_supplier_catalog(
                session,
                provider=provider,
                actor_user_id=callback.from_user.id,
            )
        except Exception as exc:
            config = await get_sync_config(session)
            config.last_sync_status = "failed"
            config.last_sync_message = str(exc)[:500]
            if callback.message:
                await callback.message.edit_text(
                    "❌ فشلت المزامنة دون تغيير أرصدة العملاء أو إنشاء طلبات.\n\n"
                    f"السبب: <code>{html.escape(str(exc)[:350])}</code>",
                    reply_markup=admin_supplier_keyboard(auto_activate=config.auto_activate),
                )
            return
        finally:
            await provider.close()
        config = await get_sync_config(session)
        if callback.message:
            await callback.message.edit_text(
                "✅ <b>اكتملت مزامنة المورد</b>\n\n"
                f"رصيد المورد: {money_label(balance)}\n"
                f"وصل من المورد: {result.received}\n"
                f"خدمات جديدة: {result.created}\n"
                f"خدمات محدثة: {result.updated}\n"
                f"خدمات عُطلت لعدم توفرها: {result.deactivated}\n"
                f"عناصر متجاوزة لخلل بياناتها: {result.skipped}",
                reply_markup=admin_supplier_keyboard(auto_activate=config.auto_activate),
            )

    @router.callback_query(F.data == "adm:supplier:markup")
    async def begin_supplier_markup(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminSupplierMarkupFlow.value)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل نسبة الربح فوق تكلفة المورد. مثال: 30",
                reply_markup=cancel_keyboard(),
            )

    @router.message(AdminSupplierMarkupFlow.value, F.text)
    async def save_supplier_markup(
        message: Message, session: AsyncSession, state: FSMContext
    ) -> None:
        try:
            value = Decimal(message.text.replace(",", ".").strip()).quantize(Decimal("0.0001"))
        except InvalidOperation:
            await message.answer("أرسل نسبة رقمية صحيحة.")
            return
        if value < 0 or value > 500:
            await message.answer("النسبة يجب أن تكون بين 0 و500.")
            return
        config = await get_sync_config(session)
        config.markup_percent = value
        changed = await reprice_unlocked_products(session)
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="supplier.markup_changed",
            entity_type="supplier",
            entity_id="ventebot",
            metadata={"markup_percent": format(value, "f"), "repriced": changed},
        )
        await state.clear()
        await message.answer(
            f"✅ نسبة الربح الآن {decimal_number(value)}%، وأُعيد تسعير {changed} خدمة غير مقفلة."
        )

    @router.callback_query(F.data == "adm:supplier:minprofit")
    async def begin_supplier_minimum_profit(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminSupplierMinimumProfitFlow.value)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "أرسل أقل ربح لكل خدمة بـ USDT. مثال: 0.50",
                reply_markup=cancel_keyboard(),
            )

    @router.message(AdminSupplierMinimumProfitFlow.value, F.text)
    async def save_supplier_minimum_profit(
        message: Message, session: AsyncSession, state: FSMContext
    ) -> None:
        try:
            value = money(Decimal(message.text.replace(",", ".").strip()))
        except InvalidOperation:
            await message.answer("أرسل مبلغًا رقميًا صحيحًا.")
            return
        if value < 0 or value > Decimal("1000"):
            await message.answer("أقل ربح يجب أن يكون بين 0 و1000 USDT.")
            return
        config = await get_sync_config(session)
        config.minimum_profit = value
        changed = await reprice_unlocked_products(session)
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="supplier.minimum_profit_changed",
            entity_type="supplier",
            entity_id="ventebot",
            metadata={"minimum_profit": format(value, "f"), "repriced": changed},
        )
        await state.clear()
        await message.answer(f"✅ أقل ربح الآن {money_label(value)}، وأُعيد تسعير {changed} خدمة.")

    @router.callback_query(F.data == "adm:supplier:autoactivate")
    async def toggle_supplier_auto_activate(callback: CallbackQuery, session: AsyncSession) -> None:
        config = await get_sync_config(session)
        config.auto_activate = not config.auto_activate
        activated = 0
        if config.auto_activate:
            repair = await _repair_supplier_catalog_visibility(
                session,
                actor_user_id=callback.from_user.id,
            )
            activated = repair.activated
        add_audit(
            session,
            actor_user_id=callback.from_user.id,
            action="supplier.auto_activate_toggled",
            entity_type="supplier",
            entity_id="ventebot",
            metadata={"auto_activate": config.auto_activate},
        )
        await callback.answer(
            f"تم تشغيل التفعيل وإظهار {activated} خدمة"
            if config.auto_activate
            else "تم إيقاف التفعيل التلقائي؛ الخدمات الحالية ستبقى ظاهرة"
        )
        if callback.message:
            await callback.message.edit_text(
                await supplier_panel_text(session),
                reply_markup=admin_supplier_keyboard(auto_activate=config.auto_activate),
            )

    @router.callback_query(F.data == "adm:products")
    async def products_list(callback: CallbackQuery, session: AsyncSession) -> None:
        products = list(await session.scalars(select(Product).order_by(Product.name_ar)))
        builder = InlineKeyboardBuilder()
        for product in products:
            mark = "🟢" if product.is_active else "⚫️"
            builder.button(
                text=f"{mark} {product.name_ar} — "
                f"{money_label(product.sale_price, product.currency)}",
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
        snapshot = await catalog_item_for_product(session, product.id)
        supplier_details = ""
        if snapshot is not None:
            stock = "غير محدد" if snapshot.stock is None else str(snapshot.stock)
            supplier_details = (
                f"\nالمخزون: {stock}"
                f"\nنوع التسليم: {html.escape(snapshot.delivery_type)}"
                f"\nضمان المورد: {snapshot.warranty_days} يوم"
                f"\nالتسعير: {'يدوي محمي' if snapshot.price_locked else 'تلقائي'}"
                f"\nالتفعيل: {'يدوي محمي' if snapshot.activation_locked else 'تلقائي'}"
            )
        cost_text = (
            money_label(product.cost_price, product.currency)
            if product.cost_price is not None
            else "-"
        )
        text = (
            f"<b>{html.escape(product.name_ar)}</b>\n"
            f"السعر: {money_label(product.sale_price, product.currency)}\n"
            f"التكلفة: {cost_text}\n"
            f"الحالة: {'مفعلة' if product.is_active else 'معطلة'}\n"
            f"التنفيذ: {product.fulfillment_mode.value}\n"
            f"معرّف المورد: <code>{html.escape(product.provider_product_id or '-')}</code>"
            f"{supplier_details}"
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
        snapshot = await catalog_item_for_product(session, product.id)
        if snapshot is not None:
            snapshot.activation_locked = True
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
        snapshot = await catalog_item_for_product(session, product.id)
        if snapshot is not None:
            snapshot.price_locked = True
        add_audit(
            session,
            actor_user_id=message.from_user.id,
            action="product.price_changed",
            entity_type="product",
            entity_id=str(product.id),
            metadata={"old": format(old, "f"), "new": format(price, "f")},
        )
        await state.clear()
        await message.answer(f"✅ أصبح سعر {html.escape(product.name_ar)}: {money_label(price)}")

    @router.callback_query(F.data.startswith("adm:autoprice:"))
    async def restore_automatic_price(callback: CallbackQuery, session: AsyncSession) -> None:
        product = await session.get(Product, uuid.UUID(callback.data.rsplit(":", 1)[1]))
        if product is None or product.cost_price is None:
            await callback.answer("الخدمة أو تكلفة المورد غير متاحة", show_alert=True)
            return
        snapshot = await catalog_item_for_product(session, product.id)
        if snapshot is None:
            await callback.answer("هذه ليست خدمة مورّد متزامنة", show_alert=True)
            return
        config = await get_sync_config(session)
        snapshot.price_locked = False
        product.sale_price = calculate_sale_price(
            product.cost_price,
            markup_percent=config.markup_percent,
            minimum_profit=config.minimum_profit,
        )
        add_audit(
            session,
            actor_user_id=callback.from_user.id,
            action="product.automatic_price_restored",
            entity_type="product",
            entity_id=str(product.id),
            metadata={"sale_price": format(product.sale_price, "f")},
        )
        await callback.answer(f"السعر التلقائي: {money_label(product.sale_price)}", show_alert=True)

    @router.callback_query(F.data.startswith("adm:autoactive:"))
    async def restore_automatic_activation(callback: CallbackQuery, session: AsyncSession) -> None:
        product = await session.get(Product, uuid.UUID(callback.data.rsplit(":", 1)[1]))
        if product is None:
            await callback.answer("الخدمة غير موجودة", show_alert=True)
            return
        snapshot = await catalog_item_for_product(session, product.id)
        if snapshot is None:
            await callback.answer("هذه ليست خدمة مورّد متزامنة", show_alert=True)
            return
        config = await get_sync_config(session)
        snapshot.activation_locked = False
        available = snapshot.stock is None or snapshot.stock > 0
        product.is_active = config.auto_activate and available
        add_audit(
            session,
            actor_user_id=callback.from_user.id,
            action="product.automatic_activation_restored",
            entity_type="product",
            entity_id=str(product.id),
            metadata={"active": product.is_active},
        )
        await callback.answer(
            f"أصبح التوفر تلقائيًا؛ الحالة الآن: {'مفعلة' if product.is_active else 'معطلة'}",
            show_alert=True,
        )

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

    @router.callback_query(F.data.startswith("adm:userwallet:"))
    async def begin_selected_wallet_adjustment(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("معرف غير صالح", show_alert=True)
            return
        user = await session.get(User, user_id)
        wallet = await session.get(Wallet, user_id)
        if user is None or wallet is None:
            await callback.answer("المستخدم غير موجود", show_alert=True)
            return
        await state.set_state(AdminWalletFlow.amount)
        await state.set_data({"user_id": user_id})
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"المشترك: <code>{user_id}</code>\n"
                f"الرصيد الحالي: <b>{money_label(wallet.balance)}</b>\n\n"
                "أرسل المبلغ: موجب للإضافة، وسالب للخصم. مثال: 5 أو -2",
                reply_markup=cancel_keyboard(),
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
        wallet = await session.get(Wallet, user_id)
        await state.update_data(user_id=user_id)
        await state.set_state(AdminWalletFlow.amount)
        await message.answer(
            f"الرصيد الحالي: <b>{money_label(wallet.balance if wallet else 0)}</b>\n\n"
            "أرسل المبلغ: موجب للإضافة، وسالب للخصم. مثال: 5 أو -2"
        )

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
            await message.answer(f"الرصيد غير كافٍ؛ المتاح {money_label(exc.available)}")
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
        await message.answer(f"✅ تم التعديل. الرصيد الجديد: {money_label(result.balance_after)}")

    @router.callback_query(F.data == "adm:payments")
    async def pending_payments(callback: CallbackQuery, session: AsyncSession) -> None:
        payments = list(
            await session.scalars(
                select(Payment)
                .where(
                    Payment.status.in_(
                        [PaymentStatus.PENDING, PaymentStatus.REVIEW_REQUIRED]
                    ),
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
                f"المبلغ المحول: "
                f"{money_label(payment.expected_amount, payment.settlement_currency)}\n"
                f"الرصيد المطلوب: {money_label(payment.credit_amount)}\n"
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
            f"رصيدك الجديد: <b>{money_label(mutation.balance_after)}</b>",
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
        referral = await session.scalar(
            select(Referral).where(
                Referral.qualified_order_id == order.id,
                Referral.rewarded_at.is_not(None),
            )
        )
        if referral is not None:
            if referral.invitee_reward_amount > 0:
                await bot.send_message(
                    referral.invitee_id,
                    "🎉 أُضيفت هدية الإحالة إلى رصيدك: "
                    f"<b>{money_label(referral.invitee_reward_amount)}</b>",
                )
            if referral.referrer_reward_amount > 0:
                await bot.send_message(
                    referral.referrer_id,
                    "🎁 اكتملت أول عملية شراء لأحد المدعوين، وأُضيفت مكافأتك: "
                    f"<b>{money_label(referral.referrer_reward_amount)}</b>",
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
            f"↩️ تم رد مبلغ {money_label(order.total_amount)} للطلب "
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
            f"سعر الصرف: 1 USDT = {decimal_number(channel.units_per_usdt)} "
            f"{channel.settlement_currency}\n"
            f"الرسوم: {decimal_number(channel.fee_percent)}%\n"
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
        await message.answer(
            f"✅ السعر الجديد: 1 USDT = {decimal_number(rate)} {channel.settlement_currency}"
        )

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
        await message.answer(f"✅ أصبحت رسوم {channel.name_ar}: {decimal_number(fee)}%")

    return router
