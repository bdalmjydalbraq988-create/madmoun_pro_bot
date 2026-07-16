from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.keyboards import main_menu


def build_common_router(settings: Settings) -> Router:
    router = Router(name="common")

    @router.callback_query(F.data == "cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("تم الإلغاء")
        if callback.message:
            await callback.message.edit_text("تم إلغاء العملية.")

    @router.message(F.text == "❌ إلغاء")
    async def cancel_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "تم إلغاء العملية.",
            reply_markup=main_menu(message.from_user.id in settings.admin_ids),
        )

    return router
