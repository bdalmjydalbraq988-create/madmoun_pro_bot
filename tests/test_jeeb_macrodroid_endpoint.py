from __future__ import annotations

import importlib
from decimal import Decimal

from fastapi.testclient import TestClient

from app.config import get_settings
from app.crypto import PayloadCipher
from app.services.payments.jeeb_macrodroid import JEEB_ANDROID_PACKAGE


def _configure_environment(monkeypatch, tmp_path, *, token: str, auto_confirm: bool) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("SUPPLIER_ENABLED", "false")
    monkeypatch.setenv("BINANCE_PAY_ENABLED", "false")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", PayloadCipher.generate_key())
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("JEEB_AUTO_CONFIRM_ENABLED", str(auto_confirm).lower())
    monkeypatch.setenv("JEEB_MACRODROID_RELAY_ENABLED", "true")
    monkeypatch.setenv("JEEB_MACRODROID_RELAY_TOKEN", token)
    monkeypatch.setenv("JEEB_RELAY_DEVICE_ID", "owner-phone-play-store")
    get_settings.cache_clear()


def test_macrodroid_endpoint_commissions_without_credit(monkeypatch, tmp_path) -> None:
    token = "commissioning-token-" + ("x" * 48)
    _configure_environment(monkeypatch, tmp_path, token=token, auto_confirm=False)

    import app.main as main_module

    main_module = importlib.reload(main_module)
    body = (
        f"{JEEB_ANDROID_PACKAGE}\n"
        "جيب\n"
        "تم استلام حوالة واردة بنجاح\n"
        "المبلغ: 25001 ريال يمني\n"
        "من حساب: 777123456\n"
        "رقم العملية: TEST-200"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Jeeb-Device-Id": "owner-phone-play-store",
        "Content-Type": "text/plain; charset=utf-8",
    }
    with TestClient(main_module.app) as client:
        response = client.post("/webhooks/jeeb-macrodroid", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "matched": False,
            "status": "validated_only",
        }

        rejected = client.post(
            "/webhooks/jeeb-macrodroid",
            content=body,
            headers={**headers, "Authorization": "Bearer wrong-token"},
        )
        assert rejected.status_code == 401
    get_settings.cache_clear()


def test_macrodroid_endpoint_credits_matching_intent_exactly_once(monkeypatch, tmp_path) -> None:
    from app.models import PaymentChannel, User, Wallet

    token = "automatic-credit-token-" + ("x" * 48)
    _configure_environment(monkeypatch, tmp_path, token=token, auto_confirm=True)

    import app.main as main_module

    main_module = importlib.reload(main_module)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Jeeb-Device-Id": "owner-phone-play-store",
        "Content-Type": "text/plain; charset=utf-8",
    }

    with TestClient(main_module.app) as client:

        async def create_intent() -> tuple[str, Decimal]:
            async with main_module.app.state.session_factory() as session:
                session.add(User(telegram_id=501, display_name="Test"))
                session.add(Wallet(user_id=501, balance=Decimal("0")))
                channel = await session.get(PaymentChannel, "jeeb")
                assert channel is not None
                channel.is_active = True
                channel.units_per_usdt = Decimal("5000")
                channel.fee_percent = Decimal("0")
                quote = main_module.app.state.payment_service.quote(channel, Decimal("5"))
                payment = await main_module.app.state.payment_service.create_jeeb_intent(
                    session,
                    user_id=501,
                    channel=channel,
                    quote=quote,
                    payer_account="777123456",
                )
                await session.commit()
                return payment.public_code, payment.expected_amount

        assert client.portal is not None
        payment_code, expected_amount = client.portal.call(create_intent)
        body = (
            f"{JEEB_ANDROID_PACKAGE}\n"
            "جيب\n"
            "تم استلام حوالة واردة بنجاح\n"
            f"المبلغ: {expected_amount:.0f} ريال يمني\n"
            "من حساب: 777123456\n"
            "رقم العملية: TEST-AUTO-200"
        )
        first = client.post("/webhooks/jeeb-macrodroid", content=body, headers=headers)
        assert first.status_code == 200
        assert first.json()["status"] == "confirmed"
        replay = client.post("/webhooks/jeeb-macrodroid", content=body, headers=headers)
        assert replay.status_code == 200

        async def read_result() -> tuple[str, Decimal]:
            async with main_module.app.state.session_factory() as session:
                channel = await session.get(PaymentChannel, "jeeb")
                wallet = await session.get(Wallet, 501)
                assert channel is not None and wallet is not None
                from sqlalchemy import select

                from app.models import Payment

                payment = await session.scalar(
                    select(Payment).where(Payment.public_code == payment_code)
                )
                assert payment is not None
                return payment.status.value, wallet.balance

        status, balance = client.portal.call(read_result)
        assert status == "confirmed"
        assert balance == Decimal("5.00000000")
    get_settings.cache_clear()
