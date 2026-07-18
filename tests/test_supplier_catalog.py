from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Category, Product, SupplierCatalogItem, SupplierSyncConfig
from app.services.supplier_catalog import (
    calculate_sale_price,
    sync_supplier_catalog,
)


class FakeSupplier:
    def __init__(self, products: list[dict[str, object]]) -> None:
        self.payload = products
        self.language: str | None = None

    async def products(self, *, language: str = "ar") -> list[dict[str, object]]:
        self.language = language
        return self.payload


def test_sale_price_uses_percentage_or_minimum_profit() -> None:
    assert calculate_sale_price(
        Decimal("5"), markup_percent=Decimal("20"), minimum_profit=Decimal("0.25")
    ) == Decimal("6.00")
    assert calculate_sale_price(
        Decimal("0.50"), markup_percent=Decimal("20"), minimum_profit=Decimal("0.25")
    ) == Decimal("0.75")


@pytest.mark.asyncio
async def test_supplier_sync_creates_priced_product_with_image(session_factory) -> None:
    supplier = FakeSupplier(
        [
            {
                "id": 12,
                "name": "Gemini Pro 12 شهر",
                "description": "اشتراك ذكاء اصطناعي",
                "image_url": "https://cdn.example/gemini.png",
                "price_usd": 5,
                "warranty_days": 30,
                "delivery_type": "activation",
                "stock": None,
                "price_tiers": [{"min_qty": 10, "price_usd": 4.5}],
            }
        ]
    )
    async with session_factory() as session:
        result = await sync_supplier_catalog(
            session,
            provider=supplier,  # type: ignore[arg-type]
            actor_user_id=999,
        )
        await session.commit()
        product = await session.scalar(select(Product).where(Product.provider_product_id == "12"))
        snapshot = await session.get(SupplierCatalogItem, ("ventebot", "12"))
        category = await session.get(Category, product.category_id)
        config = await session.get(SupplierSyncConfig, "ventebot")

        assert supplier.language == "ar"
        assert result.created == 1
        assert product.sale_price == Decimal("6.25000000")
        assert product.cost_price == Decimal("5.00000000")
        assert product.is_active
        assert product.customer_input_pattern
        assert category.name_ar == "الذكاء الاصطناعي"
        assert snapshot.image_url == "https://cdn.example/gemini.png"
        assert snapshot.delivery_type == "activation"
        assert snapshot.price_tiers_json == [{"min_qty": 10, "price_usd": "4.5"}]
        assert config.last_sync_status == "success"


@pytest.mark.asyncio
async def test_supplier_sync_hides_setup_placeholder(session_factory) -> None:
    supplier = FakeSupplier(
        [
            {
                "id": 47,
                "name": "Gemini 18 months",
                "description": "حساب جاهز",
                "image_url": "https://cdn.example/gemini-18.png",
                "price_usd": 0.6,
                "delivery_type": "stock",
                "stock": 47,
            }
        ]
    )
    async with session_factory() as session:
        category = Category(name_ar="تجريبي", emoji="🧪", is_active=True)
        session.add(category)
        await session.flush()
        placeholder = Product(
            category_id=category.id,
            name_ar="Gemini Pro",
            description_ar="اشتراك Gemini Pro. اضبط المدة والسعر وشروط المورد قبل التفعيل.",
            sale_price=Decimal("3"),
            provider_code="ventebot",
            provider_product_id=None,
            is_active=True,
        )
        session.add(placeholder)
        await session.commit()

        result = await sync_supplier_catalog(
            session,
            provider=supplier,  # type: ignore[arg-type]
            actor_user_id=999,
        )
        await session.commit()

        assert result.created == 1
        assert not placeholder.is_active
        imported = await session.scalar(select(Product).where(Product.provider_product_id == "47"))
        snapshot = await session.get(SupplierCatalogItem, ("ventebot", "47"))
        assert imported.is_active
        assert snapshot.stock == 47
        assert snapshot.delivery_type == "stock"


@pytest.mark.asyncio
async def test_sync_respects_manual_price_and_disables_zero_stock(session_factory) -> None:
    supplier = FakeSupplier(
        [
            {
                "id": 22,
                "name": "Netflix Premium",
                "description": "حساب جاهز",
                "image_url": "https://cdn.example/netflix.png",
                "price_usd": 2,
                "delivery_type": "stock",
                "stock": 5,
            }
        ]
    )
    async with session_factory() as session:
        await sync_supplier_catalog(
            session,
            provider=supplier,  # type: ignore[arg-type]
            actor_user_id=999,
        )
        await session.commit()
        product = await session.scalar(select(Product).where(Product.provider_product_id == "22"))
        snapshot = await session.get(SupplierCatalogItem, ("ventebot", "22"))
        product.sale_price = Decimal("9")
        snapshot.price_locked = True
        await session.commit()

        supplier.payload[0]["price_usd"] = 3
        supplier.payload[0]["stock"] = 0
        await sync_supplier_catalog(
            session,
            provider=supplier,  # type: ignore[arg-type]
            actor_user_id=999,
        )
        await session.commit()

        assert product.cost_price == Decimal("3.00000000")
        assert product.sale_price == Decimal("9.00000000")
        assert not product.is_active
