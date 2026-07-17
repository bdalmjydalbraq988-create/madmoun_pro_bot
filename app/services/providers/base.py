from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.enums import ProviderResultStatus


@dataclass(frozen=True, slots=True)
class ProvisionRequest:
    client_order_id: str
    product_id: str
    quantity: int
    customer_input: str


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    status: ProviderResultStatus
    external_order_id: str
    delivery: str | None = None
    provider_status: str | None = None
    safe_metadata: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    code = "provider_error"


class ProviderTemporaryError(ProviderError):
    code = "provider_temporary"


class ProviderUncertainError(ProviderError):
    """The request may have reached the supplier; automatic retry is unsafe."""

    code = "provider_uncertain"


class ProviderRejectedError(ProviderError):
    code = "provider_rejected"


class SupplierProvider(Protocol):
    code: str

    async def create_order(self, request: ProvisionRequest) -> ProvisionResult: ...

    async def get_order(self, external_order_id: str) -> ProvisionResult: ...

    async def close(self) -> None: ...
