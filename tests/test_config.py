from pydantic import ValidationError

from app.config import Settings


def test_admin_ids_are_parsed_from_csv() -> None:
    settings = Settings(admin_ids="1, 2,3")
    assert settings.admin_ids == [1, 2, 3, 8884716304]


def test_admin_ids_are_parsed_from_environment_json_list(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "[123]")
    settings = Settings()
    assert settings.admin_ids == [123, 8884716304]


def test_enabled_binance_requires_merchant_secrets() -> None:
    try:
        Settings(binance_pay_enabled=True)
    except ValidationError as exc:
        assert "BINANCE_PAY_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected validation error")


def test_enabled_jeeb_relay_requires_long_secret() -> None:
    try:
        Settings(jeeb_auto_confirm_enabled=True, jeeb_relay_secret="short")
    except ValidationError as exc:
        assert "JEEB_RELAY_SECRET" in str(exc)
    else:
        raise AssertionError("Expected validation error")
