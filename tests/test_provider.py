from __future__ import annotations

import httpx
import pytest

from app.services.providers.base import ProviderUncertainError, ProvisionRequest
from app.services.providers.quantumvault import VenteBotProvider


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
