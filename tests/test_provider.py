from __future__ import annotations

import httpx
import pytest

from app.services.providers.base import ProviderUncertainError, ProvisionRequest
from app.services.providers.quantumvault import VenteBotProvider


@pytest.mark.asyncio
async def test_ventebot_catalog_requests_arabic_and_parses_products() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Reseller-Key"] == "secret"
        assert request.headers["Accept-Language"] == "ar"
        return httpx.Response(
            200,
            json={"success": True, "products": [{"id": 12, "name": "Gemini Pro"}]},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = VenteBotProvider(
        base_url="https://supplier.example",
        api_key="secret",
        client=http_client,
    )
    products = await provider.products(language="ar")
    assert products == [{"id": 12, "name": "Gemini Pro"}]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supplier_post_timeout_is_never_blindly_retried() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = VenteBotProvider(
        base_url="https://supplier.example/api/v1",
        api_key="secret",
        client=http_client,
    )
    with pytest.raises(ProviderUncertainError):
        await provider.create_order(
            ProvisionRequest(
                client_order_id="O123",
                product_id="12",
                quantity=1,
                customer_input="user@example.com",
            )
        )
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supplier_5xx_after_post_requires_reconciliation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "unknown"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = VenteBotProvider(
        base_url="https://supplier.example/api/v1",
        api_key="secret",
        client=http_client,
    )
    with pytest.raises(ProviderUncertainError):
        await provider.create_order(
            ProvisionRequest(
                client_order_id="O124",
                product_id="12",
                quantity=1,
                customer_input="user@example.com",
            )
        )
    await http_client.aclose()


@pytest.mark.asyncio
async def test_ventebot_uses_documented_order_fields_and_parses_wrapper() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        assert request.headers["X-Reseller-Key"] == "secret"
        return httpx.Response(
            200,
            json={
                "success": True,
                "idempotent": False,
                "balance_after": 37.5,
                "unit_price": 5.0,
                "total": 5.0,
                "order": {
                    "id": 124,
                    "status": "COMPLETED",
                    "items": [
                        {
                            "id": 99,
                            "account_data": "https://serviceactivation.google/example",
                        }
                    ],
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = VenteBotProvider(
        base_url="https://supplier.example",
        api_key="secret",
        client=http_client,
    )
    result = await provider.create_order(
        ProvisionRequest(
            client_order_id="order-555",
            product_id="12",
            quantity=1,
            customer_input="user@example.com",
        )
    )
    assert captured == {
        "product_id": 12,
        "quantity": 1,
        "activation_identifier": "user@example.com",
        "customer_reference": "order-555",
        "idempotency_key": "order-555",
    }
    assert result.external_order_id == "124"
    assert result.status.value == "completed"
    assert result.delivery == "https://serviceactivation.google/example"
    assert result.safe_metadata["balance_after"] == 37.5
    await http_client.aclose()


@pytest.mark.asyncio
async def test_ventebot_reads_pending_order_status_without_creating_again() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/reseller/orders/124"
        return httpx.Response(
            200,
            json={
                "success": True,
                "order": {
                    "id": 124,
                    "status": "COMPLETED",
                    "items": [{"id": 99, "account_data": "login: password"}],
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = VenteBotProvider(
        base_url="https://supplier.example",
        api_key="secret",
        client=http_client,
    )
    result = await provider.get_order("124")
    assert result.external_order_id == "124"
    assert result.status.value == "completed"
    assert result.delivery == "login: password"
    await http_client.aclose()


def test_completed_supplier_order_without_items_is_not_falsely_delivered() -> None:
    result = VenteBotProvider._parse_order(
        {"success": True, "order": {"id": 124, "status": "COMPLETED", "items": []}}
    )
    assert result.status.value == "pending"
    assert result.delivery is None
    assert result.safe_metadata["delivery_missing"] is True


def test_supplier_delivery_combines_all_account_items() -> None:
    result = VenteBotProvider._parse_order(
        {
            "success": True,
            "order": {
                "id": 124,
                "status": "COMPLETED",
                "items": [
                    {"id": 1, "account_data": "first-account"},
                    {"id": 2, "account_data": "second-account"},
                ],
            },
        }
    )
    assert result.delivery == "first-account\n\nsecond-account"
