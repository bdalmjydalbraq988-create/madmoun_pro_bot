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
                "replayed": False,
                "order": {"id": 124, "status": "COMPLETED"},
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
        "idempotency_key": "order-555",
    }
    assert result.external_order_id == "124"
    assert result.status.value == "completed"
    assert result.delivery
    await http_client.aclose()


@pytest.mark.asyncio
async def test_ventebot_reads_pending_order_status_without_creating_again() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/reseller/orders/124"
        return httpx.Response(
            200,
            json={"success": True, "order": {"id": 124, "status": "COMPLETED"}},
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
    assert result.delivery
    await http_client.aclose()
