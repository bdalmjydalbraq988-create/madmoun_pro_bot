import pytest
from pydantic import ValidationError

from app.config import Settings


def test_admin_ids_are_parsed_from_csv() -> None:
    settings = Settings(admin_ids="1, 2,3")
    assert settings.admin_ids == [1, 2, 3]


def test_admin_ids_are_parsed_from_environment_json_list(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "[123]")
    settings = Settings()
    assert settings.admin_ids == [123]


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


def test_enabled_jeeb_relay_requires_device_identity() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(jeeb_auto_confirm_enabled=True, jeeb_relay_secret="x" * 48)
    assert "JEEB_RELAY_DEVICE_ID" in str(exc.value)


def test_enabled_macrodroid_relay_requires_long_token_and_device() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(jeeb_macrodroid_relay_enabled=True, jeeb_macrodroid_relay_token="short")
    assert "JEEB_MACRODROID_RELAY_TOKEN" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        Settings(
            jeeb_macrodroid_relay_enabled=True,
            jeeb_macrodroid_relay_token="x" * 64,
        )
    assert "JEEB_RELAY_DEVICE_ID" in str(exc.value)


def test_macrodroid_can_be_commissioned_before_auto_credit() -> None:
    settings = Settings(
        jeeb_auto_confirm_enabled=False,
        jeeb_macrodroid_relay_enabled=True,
        jeeb_macrodroid_relay_token="x" * 64,
        jeeb_relay_device_id="owner-phone-play-store",
    )
    assert settings.jeeb_macrodroid_relay_enabled is True
    assert settings.jeeb_auto_confirm_enabled is False


def test_macrodroid_is_a_valid_authenticator_for_auto_credit() -> None:
    settings = Settings(
        jeeb_auto_confirm_enabled=True,
        jeeb_macrodroid_relay_enabled=True,
        jeeb_macrodroid_relay_token="x" * 64,
        jeeb_relay_device_id="owner-phone-play-store",
    )
    assert settings.jeeb_auto_confirm_enabled is True
