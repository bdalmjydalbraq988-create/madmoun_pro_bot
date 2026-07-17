from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import FulfillmentMode
from app.models import (
    Category,
    Product,
    SupplierCatalogItem,
    SupplierSyncConfig,
)
from app.services.audit import add_audit
from app.services.providers.quantumvault import VenteBotProvider

PROVIDER_CODE = "ventebot"
CENT = Decimal("0.01")


class CatalogSyncError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedSupplierProduct:
    provider_product_id: str
    name: str
    description: str
    image_url: str | None
    cost: Decimal
    delivery_type: str
    stock: int | None
    warranty_days: int
    price_tiers: list[dict[str, Any]]
    raw_payload: dict[str, Any]

    @property
    def available(self) -> bool:
        return self.cost > 0 and (self.stock is None or self.stock > 0)


@dataclass(frozen=True, slots=True)
class CatalogSyncResult:
    received: int
    created: int
    updated: int
    deactivated: int
    skipped: int


CATEGORY_RULES: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    (
        "🤖",
        "الذكاء الاصطناعي",
        10,
        (
            "gemini",
            "chatgpt",
            "openai",
            "claude",
            "grok",
            "perplexity",
            "copilot",
            "جيمناي",
            "شات جي بي تي",
            "ذكاء اصطناعي",
        ),
    ),
    (
        "🎬",
        "الترفيه والمشاهدة",
        20,
        (
            "netflix",
            "spotify",
            "youtube",
            "shahid",
            "osn",
            "prime video",
            "disney",
            "نتفلكس",
            "سبوتيفاي",
            "يوتيوب",
            "شاهد",
        ),
    ),
    (
        "🎨",
        "التصميم والإبداع",
        30,
        ("canva", "adobe", "capcut", "figma", "كانفا", "أدوبي", "تصميم"),
    ),
    (
        "🎓",
        "التعليم والعمل",
        40,
        (
            "coursera",
            "udemy",
            "duolingo",
            "linkedin",
            "كورسيرا",
            "يوديمي",
            "لينكد",
        ),
    ),
    (
        "💼",
        "الإنتاجية والأعمال",
        50,
        ("microsoft", "office", "notion", "zoom", "google one", "مايكروسوفت"),
    ),
    (
        "🛡",
        "الحماية والـ VPN",
        60,
        ("vpn", "nord", "surfshark", "expressvpn", "حماية"),
    ),
)


def calculate_sale_price(
    cost: Decimal,
    *,
    markup_percent: Decimal,
    minimum_profit: Decimal,
) -> Decimal:
    percentage_price = cost * (Decimal("1") + (markup_percent / Decimal("100")))
    minimum_price = cost + minimum_profit
    return max(percentage_price, minimum_price).quantize(CENT, rounding=ROUND_UP)


def normalize_supplier_product(payload: dict[str, Any]) -> NormalizedSupplierProduct:
    provider_id = payload.get("id", payload.get("product_id"))
    name = _text(payload.get("name", payload.get("name_ar")), limit=180)
    if provider_id is None or not name:
        raise CatalogSyncError("منتج المورد بلا معرّف أو اسم")
    try:
        cost = Decimal(str(payload.get("price_usd"))).quantize(Decimal("0.00000001"))
    except (InvalidOperation, TypeError):
        raise CatalogSyncError(f"سعر المنتج {provider_id} غير صالح") from None
    if cost <= 0:
        raise CatalogSyncError(f"سعر المنتج {provider_id} يجب أن يكون أكبر من صفر")

    delivery_type = str(payload.get("delivery_type") or "activation").lower()
    if delivery_type not in {"stock", "activation"}:
        delivery_type = "activation"
    stock = _optional_non_negative_int(payload.get("stock"))
    warranty_days = _optional_non_negative_int(payload.get("warranty_days")) or 0
    image_url = _safe_image_url(payload.get("image_url"))
    price_tiers = _normalize_price_tiers(payload.get("price_tiers"))
    description = _text(payload.get("description"), limit=3000)
    safe_payload = {
        "id": provider_id,
        "name": name,
        "description": description,
        "image_url": image_url,
        "price_usd": format(cost, "f"),
        "warranty_days": warranty_days,
        "delivery_type": delivery_type,
        "stock": stock,
        "price_tiers": price_tiers,
    }
    return NormalizedSupplierProduct(
        provider_product_id=str(provider_id),
        name=name,
        description=description,
        image_url=image_url,
        cost=cost,
        delivery_type=delivery_type,
        stock=stock,
        warranty_days=warranty_days,
        price_tiers=price_tiers,
        raw_payload=safe_payload,
    )


async def get_sync_config(session: AsyncSession) -> SupplierSyncConfig:
    config = await session.get(SupplierSyncConfig, PROVIDER_CODE)
    if config is None:
        config = SupplierSyncConfig(provider_code=PROVIDER_CODE)
        session.add(config)
        await session.flush()
    return config


async def sync_supplier_catalog(
    session: AsyncSession,
    *,
    provider: VenteBotProvider,
    actor_user_id: int | None,
) -> CatalogSyncResult:
    config = await get_sync_config(session)
    try:
        raw_products = await provider.products(language="ar")
    except Exception as exc:
        config.last_sync_status = "failed"
        config.last_sync_message = str(exc)[:500]
        raise

    existing_products = list(
        await session.scalars(select(Product).where(Product.provider_code == PROVIDER_CODE))
    )
    products_by_provider_id = {
        product.provider_product_id: product
        for product in existing_products
        if product.provider_product_id
    }
    unlinked_by_name = {
        product.name_ar.casefold(): product
        for product in list(
            await session.scalars(select(Product).where(Product.provider_product_id.is_(None)))
        )
    }
    snapshots = list(
        await session.scalars(
            select(SupplierCatalogItem).where(SupplierCatalogItem.provider_code == PROVIDER_CODE)
        )
    )
    snapshots_by_id = {item.provider_product_id: item for item in snapshots}
    categories: dict[str, Category] = {}
    now = datetime.now(UTC)
    seen_ids: set[str] = set()
    created = updated = skipped = 0

    for raw_product in raw_products:
        try:
            item = normalize_supplier_product(raw_product)
        except CatalogSyncError:
            skipped += 1
            continue
        if item.provider_product_id in seen_ids:
            skipped += 1
            continue
        seen_ids.add(item.provider_product_id)

        snapshot = snapshots_by_id.get(item.provider_product_id)
        product = products_by_provider_id.get(item.provider_product_id)
        if product is None:
            product = unlinked_by_name.get(item.name.casefold())
        is_new = product is None
        if is_new:
            category = await _category_for(session, item.name, categories)
            label, pattern, help_text = _customer_input(item)
            product = Product(
                category_id=category.id,
                name_ar=item.name,
                description_ar=item.description or "اشتراك رقمي يُنفذ تلقائيًا عبر المورد.",
                sale_price=calculate_sale_price(
                    item.cost,
                    markup_percent=config.markup_percent,
                    minimum_profit=config.minimum_profit,
                ),
                cost_price=item.cost,
                fulfillment_mode=FulfillmentMode.AUTO,
                provider_code=PROVIDER_CODE,
                provider_product_id=item.provider_product_id,
                customer_input_label=label,
                customer_input_pattern=pattern,
                customer_input_help=help_text,
                terms_ar=_terms(item),
                is_active=config.auto_activate and item.available,
                sort_order=_sort_order(item.provider_product_id),
            )
            session.add(product)
            await session.flush()
            created += 1
        else:
            product.provider_code = PROVIDER_CODE
            product.provider_product_id = item.provider_product_id
            product.name_ar = item.name
            if item.description:
                product.description_ar = item.description
            product.cost_price = item.cost
            product.fulfillment_mode = FulfillmentMode.AUTO
            label, pattern, help_text = _customer_input(item)
            product.customer_input_label = label
            product.customer_input_pattern = pattern
            product.customer_input_help = help_text
            product.terms_ar = _terms(item)
            if snapshot is None or not snapshot.price_locked:
                product.sale_price = calculate_sale_price(
                    item.cost,
                    markup_percent=config.markup_percent,
                    minimum_profit=config.minimum_profit,
                )
            if not item.available:
                product.is_active = False
            elif snapshot is None or not snapshot.activation_locked:
                product.is_active = config.auto_activate
            updated += 1

        if snapshot is None:
            snapshot = SupplierCatalogItem(
                provider_code=PROVIDER_CODE,
                provider_product_id=item.provider_product_id,
                product_id=product.id,
            )
            session.add(snapshot)
            snapshots_by_id[item.provider_product_id] = snapshot
        snapshot.product_id = product.id
        snapshot.image_url = item.image_url
        snapshot.delivery_type = item.delivery_type
        snapshot.stock = item.stock
        snapshot.warranty_days = item.warranty_days
        snapshot.price_tiers_json = item.price_tiers
        snapshot.raw_payload_json = item.raw_payload
        snapshot.last_seen_at = now

    deactivated = 0
    if config.deactivate_missing:
        for snapshot in snapshots:
            if snapshot.provider_product_id in seen_ids or snapshot.product_id is None:
                continue
            product = await session.get(Product, snapshot.product_id)
            if product is not None and product.is_active:
                product.is_active = False
                deactivated += 1

    config.last_synced_at = now
    config.last_sync_status = "success"
    config.last_sync_message = (
        f"received={len(raw_products)}, created={created}, updated={updated}, "
        f"deactivated={deactivated}, skipped={skipped}"
    )
    add_audit(
        session,
        actor_user_id=actor_user_id,
        action="supplier.catalog_synced",
        entity_type="supplier",
        entity_id=PROVIDER_CODE,
        metadata={
            "received": len(raw_products),
            "created": created,
            "updated": updated,
            "deactivated": deactivated,
            "skipped": skipped,
        },
    )
    await session.flush()
    return CatalogSyncResult(
        received=len(raw_products),
        created=created,
        updated=updated,
        deactivated=deactivated,
        skipped=skipped,
    )


async def reprice_unlocked_products(session: AsyncSession) -> int:
    config = await get_sync_config(session)
    snapshots = list(
        await session.scalars(
            select(SupplierCatalogItem).where(
                SupplierCatalogItem.provider_code == PROVIDER_CODE,
                SupplierCatalogItem.price_locked.is_(False),
                SupplierCatalogItem.product_id.is_not(None),
            )
        )
    )
    changed = 0
    for snapshot in snapshots:
        product = await session.get(Product, snapshot.product_id)
        if product is None or product.cost_price is None:
            continue
        product.sale_price = calculate_sale_price(
            product.cost_price,
            markup_percent=config.markup_percent,
            minimum_profit=config.minimum_profit,
        )
        changed += 1
    return changed


async def catalog_item_for_product(
    session: AsyncSession, product_id: Any
) -> SupplierCatalogItem | None:
    return await session.scalar(
        select(SupplierCatalogItem).where(SupplierCatalogItem.product_id == product_id)
    )


async def _category_for(
    session: AsyncSession,
    name: str,
    cache: dict[str, Category],
) -> Category:
    search = name.casefold()
    emoji, category_name, sort_order = "🛍", "اشتراكات رقمية", 100
    for rule_emoji, rule_name, rule_sort, keywords in CATEGORY_RULES:
        if any(keyword in search for keyword in keywords):
            emoji, category_name, sort_order = rule_emoji, rule_name, rule_sort
            break
    if category_name in cache:
        return cache[category_name]
    category = await session.scalar(select(Category).where(Category.name_ar == category_name))
    if category is None:
        category = Category(
            name_ar=category_name,
            emoji=emoji,
            is_active=True,
            sort_order=sort_order,
        )
        session.add(category)
        await session.flush()
    cache[category_name] = category
    return category


def _customer_input(item: NormalizedSupplierProduct) -> tuple[str, str | None, str]:
    if item.delivery_type == "stock":
        return (
            "لا يلزم إدخال بيانات",
            None,
            "سيتم تسليم بيانات الاشتراك تلقائيًا بعد تأكيد الشراء.",
        )
    search = item.name.casefold()
    if "gemini" in search or "جيمناي" in search or "google" in search:
        return (
            "بريد Google المستفيد",
            r"[^@\s]+@[^@\s]+\.[^@\s]+",
            "أرسل البريد فقط، ولا ترسل كلمة المرور أو رمز التحقق.",
        )
    return (
        "البريد أو معرّف التفعيل",
        None,
        "أرسل المعرّف المطلوب فقط، ولا ترسل كلمة المرور أو رمز التحقق.",
    )


def _terms(item: NormalizedSupplierProduct) -> str:
    parts = ["التنفيذ تلقائي حسب توفر الخدمة لدى المورد."]
    if item.warranty_days:
        parts.append(f"ضمان المورد: {item.warranty_days} يومًا.")
    if item.stock is not None:
        parts.append(f"المخزون المتاح وقت آخر تحديث: {item.stock}.")
    return "\n".join(parts)


def _sort_order(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _safe_image_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url[:2000]


def _normalize_price_tiers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for tier in value[:50]:
        if not isinstance(tier, dict):
            continue
        try:
            minimum = max(1, int(tier.get("min_qty", 1)))
            price = Decimal(str(tier.get("price_usd")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        result.append({"min_qty": minimum, "price_usd": format(price, "f")})
    return result
