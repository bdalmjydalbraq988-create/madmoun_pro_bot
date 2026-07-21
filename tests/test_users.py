from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.keyboards import (
    admin_dashboard_keyboard,
    admin_user_keyboard,
    admin_user_orders_keyboard,
    main_menu,
    referral_keyboard,
    user_account_keyboard,
)
from app.models import User, Wallet
from app.services.users import upsert_telegram_user


@pytest.mark.asyncio
async def test_user_registration_creates_exactly_one_wallet_and_updates_profile(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = await upsert_telegram_user(
            session,
            telegram_id=123456789,
            username="first_name",
            display_name="First Name",
        )
        await session.commit()

        replay = await upsert_telegram_user(
            session,
            telegram_id=123456789,
            username="new_name",
            display_name="New Name",
        )
        await session.commit()

        wallet = await session.get(Wallet, 123456789)
        wallet_count = await session.scalar(select(func.count()).select_from(Wallet))
        user_count = await session.scalar(select(func.count()).select_from(User))

        assert replay.telegram_id == user.telegram_id
        assert replay.username == "new_name"
        assert replay.display_name == "New Name"
        assert wallet.balance == Decimal("0E-8")
        assert wallet_count == 1
        assert user_count == 1


def test_user_and_admin_keyboards_expose_account_and_subscriber_controls() -> None:
    user_buttons = [button.text for row in main_menu().keyboard for button in row]
    admin_buttons = [
        button.callback_data for row in admin_dashboard_keyboard().inline_keyboard for button in row
    ]
    detail_buttons = [
        button.callback_data
        for row in admin_user_keyboard(123456789).inline_keyboard
        for button in row
    ]
    customer_copy_button = admin_user_keyboard(123456789).inline_keyboard[0][0]
    account_copy_button = user_account_keyboard(123456789).inline_keyboard[0][0]
    orders_back_buttons = [
        button.callback_data
        for row in admin_user_orders_keyboard(123456789).inline_keyboard
        for button in row
    ]

    assert "🆔 رقمي" in user_buttons
    assert "🎁 ادعُ واربح" in user_buttons
    assert "adm:users" in admin_buttons
    assert "adm:usersearch" in admin_buttons
    assert "adm:userwallet:123456789" in detail_buttons
    assert "adm:userorders:123456789" in detail_buttons
    assert customer_copy_button.copy_text.text == "123456789"
    assert account_copy_button.copy_text.text == "123456789"
    assert "adm:user:123456789" in orders_back_buttons
    assert referral_keyboard("https://t.me/test_bot?start=ref_123456789").inline_keyboard[0][
        0
    ].copy_text.text.endswith("ref_123456789")
