from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from app.enums import ProviderResultStatus
from app.services.providers.base import (
    ProviderRejectedError,
    ProviderTemporaryError,
    ProviderUncertainError,
    ProvisionRequest,
    ProvisionResult,
)


class VenteBotProvider:
    """Adapter for the documented VenteBot reseller API."""

    code = "ventebot"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        me_path: str = "/api/reseller/me",
        products_path: str = "/api/reseller/products",
        quote_path: str = "/api/reseller/quote",
        create_order_path: str = "/api/reseller/orders",
        status_path: str = "/api/reseller/orders/{order_id}",
        activation_identifier_path: str = "/api/reseller/orders/{order_id}/activation-identifier",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.me_path = me_path
        self.products_path = products_path
        self.quote_path = quote_path
        self.create_order_path = create_order_path
        self.status_path = status_path
        self.activation_identifier_path = activation_identifier_path
        self._headers = {"X-Reseller-Key": api_key, "Accept": "application/json"}
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=8.0),
            headers=self._headers,
            trust_env=False,
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def balance(self) -> Decimal:
        data = await self.account()
        value = data.get("balance", data.get("data", {}).get("balance"))
        if value is None:
            raise ProviderRejectedError("Supplier balance response is not recognized")
        return Decimal(str(value))

    async def account(self) -> dict[str, Any]:
        return await self._get(self.me_path)

    async def products(self, *, language: str = "ar") -> list[dict[str, Any]]:
        data = await self._get(
            self.products_path,
            extra_headers={"Accept-Language": language},
        )
        products = data.get("products", data.get("data", data))
        if not isinstance(products, list):
            raise ProviderRejectedError("Supplier product response is not recognized")
        return [dict(item) for item in products if isinstance(item, dict)]

    async def create_order(self, request: ProvisionRequest) -> ProvisionResult:
        # VenteBot requires a unique idempotency_key for every purchase. If the
        # request times out, we deliberately do not retry blindly.
        data = await self._post(
            self.create_order_path,
            {
                "product_id": self._product_id(request.product_id),
                "quantity": request.quantity,
                "activation_identifier": request.customer_input or None,
                "idempotency_key": request.client_order_id,
            },
        )
        return self._parse_order(data)

    async def get_order(self, external_order_id: str) -> ProvisionResult:
        data = await self._get(self.status_path.format(order_id=external_order_id))
        return self._parse_order(data)

    @staticmethod
    def _parse_order(data: dict[str, Any]) -> ProvisionResult:
        result = data.get("order", data.get("data", data))
        if not isinstance(result, dict):
            raise ProviderRejectedError("Supplier order response is not recognized")
        external_id = result.get("order_id", result.get("id"))
        if external_id is None:
            raise ProviderRejectedError("Supplier did not return an order id")
        raw_status = str(result.get("status", "completed")).lower()
        if raw_status in {"cancelled", "canceled", "failed", "rejected"}:
            raise ProviderRejectedError(f"Supplier order ended with status {raw_status}")
        delivery = result.get("delivery", result.get("result"))
        completed = raw_status in {"completed", "success", "delivered"}
        if completed and not delivery:
            delivery = "تم تنفيذ وتفعيل الخدمة بنجاح لدى المورد."
        status = ProviderResultStatus.COMPLETED if completed else ProviderResultStatus.PENDING
        return ProvisionResult(
            status=status,
            external_order_id=str(external_id),
            delivery=str(delivery) if delivery is not None else None,
            provider_status=raw_status,
            safe_metadata={"replayed": bool(data.get("replayed", False))},
        )

    @staticmethod
    def _product_id(value: str) -> int:
        try:
            return int(value)
        except ValueError as exc:
            raise ProviderRejectedError("VenteBot product id must be an integer") from exc

    async def _get(
        self,
        path: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**self._headers, **(extra_headers or {})}
        try:
            response = await self._client.get(f"{self.base_url}{path}", headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderTemporaryError("Supplier request timed out") from exc
        except httpx.NetworkError as exc:
            raise ProviderTemporaryError("Supplier network error") from exc
        return self._decode(response)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self.base_url}{path}", json=payload, headers=self._headers
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderUncertainError(
                "Supplier order may have been created; manual reconciliation is required"
            ) from exc
        if response.status_code >= 500:
            raise ProviderUncertainError(
                "Supplier returned an error after receiving the order; reconciliation is required"
            )
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise ProviderTemporaryError(f"Supplier HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderRejectedError(f"Supplier rejected request: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderRejectedError("Supplier returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderRejectedError("Supplier response must be a JSON object")
        return data
