from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_admin_ids(value: object) -> list[int]:
    if isinstance(value, str):
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    if isinstance(value, int):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [int(item) for item in value]
    return []


AdminIds = Annotated[list[int], BeforeValidator(_parse_admin_ids)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    store_name: str = "مضمون برو"
    support_username: str = "@support"
    public_base_url: str = "http://localhost:8000"
    webhook_secret_path: SecretStr = SecretStr("change-me")

    bot_token: SecretStr = SecretStr("")
    admin_ids: AdminIds = Field(default_factory=lambda: [8884716304])

    database_url: str = "sqlite+aiosqlite:///./store.db"
    redis_url: str = "redis://localhost:6379/0"
    data_encryption_key: SecretStr = SecretStr("")

    supplier_enabled: bool = False
    supplier_base_url: str = "https://ventetelegrambotrailway-production.up.railway.app"
    supplier_api_key: SecretStr = SecretStr("")
    supplier_me_path: str = "/api/reseller/me"
    supplier_products_path: str = "/api/reseller/products"
    supplier_quote_path: str = "/api/reseller/quote"
    supplier_create_order_path: str = "/api/reseller/orders"
    supplier_order_status_path: str = "/api/reseller/orders/{order_id}"
    supplier_activation_identifier_path: str = (
        "/api/reseller/orders/{order_id}/activation-identifier"
    )

    binance_pay_enabled: bool = False
    binance_pay_api_key: SecretStr = SecretStr("")
    binance_pay_secret_key: SecretStr = SecretStr("")
    binance_pay_base_url: str = "https://bpay.binanceapi.com"
    binance_webhook_tolerance_seconds: int = 300

    order_worker_interval_seconds: float = 5.0
    order_max_retries: int = 3

    @field_validator("public_base_url", "supplier_base_url", "binance_pay_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("admin_ids", mode="after")
    @classmethod
    def always_include_owner(cls, value: list[int]) -> list[int]:
        """Keep the store owner authorized even if a hosting panel overrides ADMIN_IDS."""
        return sorted(set(value) | {8884716304})

    @model_validator(mode="after")
    def validate_enabled_integrations(self) -> Settings:
        if self.supplier_enabled and not self.supplier_api_key.get_secret_value():
            raise ValueError("SUPPLIER_API_KEY is required when SUPPLIER_ENABLED=true")
        if self.binance_pay_enabled:
            if not self.binance_pay_api_key.get_secret_value():
                raise ValueError("BINANCE_PAY_API_KEY is required when Binance Pay is enabled")
            if not self.binance_pay_secret_key.get_secret_value():
                raise ValueError("BINANCE_PAY_SECRET_KEY is required when Binance Pay is enabled")
        if self.app_env == "production":
            if not self.bot_token.get_secret_value():
                raise ValueError("BOT_TOKEN is required in production")
            if not self.admin_ids:
                raise ValueError("At least one ADMIN_IDS value is required in production")
            if len(self.webhook_secret_path.get_secret_value()) < 32:
                raise ValueError("WEBHOOK_SECRET_PATH must have at least 32 characters")
            if not self.data_encryption_key.get_secret_value():
                raise ValueError("DATA_ENCRYPTION_KEY is required in production")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL must use HTTPS in production")
            if self.database_url.startswith("sqlite"):
                raise ValueError("PostgreSQL is required in production")
        if self.supplier_enabled and not self.supplier_base_url.startswith("https://"):
            raise ValueError("SUPPLIER_BASE_URL must use HTTPS")
        if self.binance_pay_enabled and not self.binance_pay_base_url.startswith("https://"):
            raise ValueError("BINANCE_PAY_BASE_URL must use HTTPS")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
