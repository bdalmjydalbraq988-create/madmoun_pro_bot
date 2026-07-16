from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Category, Product


async def active_categories(session: AsyncSession) -> list[Category]:
    result = await session.scalars(
        select(Category)
        .where(Category.is_active.is_(True))
        .order_by(Category.sort_order, Category.id)
    )
    return list(result)


async def active_products(session: AsyncSession, category_id: int) -> list[Product]:
    result = await session.scalars(
        select(Product)
        .where(
            Product.category_id == category_id,
            Product.is_active.is_(True),
        )
        .order_by(Product.sort_order, Product.name_ar)
    )
    return list(result)


async def get_active_product(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    return await session.scalar(
        select(Product)
        .options(selectinload(Product.category))
        .where(Product.id == product_id, Product.is_active.is_(True))
    )


def validate_customer_input(product: Product, value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 500:
        raise ValueError("قيمة البيانات المطلوبة غير صالحة.")
    if product.customer_input_pattern:
        try:
            matched = re.fullmatch(product.customer_input_pattern, clean)
        except re.error as exc:
            raise ValueError("قاعدة التحقق من المنتج غير صحيحة؛ تواصل مع الإدارة.") from exc
        if not matched:
            raise ValueError("البيانات لا تطابق الصيغة المطلوبة لهذه الخدمة.")
    return clean
