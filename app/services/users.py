from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, Wallet


async def upsert_telegram_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    display_name: str,
) -> User:
    user = await session.get(User, telegram_id)
    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            display_name=display_name[:160],
        )
        session.add(user)
        session.add(Wallet(user_id=telegram_id))
        await session.flush()
        return user

    changed = user.username != username or user.display_name != display_name[:160]
    if changed:
        user.username = username
        user.display_name = display_name[:160]
        await session.flush()
    return user


async def get_user_with_wallet(session: AsyncSession, telegram_id: int) -> User | None:
    stmt = select(User).where(User.telegram_id == telegram_id)
    return await session.scalar(stmt)
