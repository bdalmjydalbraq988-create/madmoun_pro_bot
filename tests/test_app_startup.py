from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.crypto import PayloadCipher


def test_application_starts_and_health_checks_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("SUPPLIER_ENABLED", "false")
    monkeypatch.setenv("BINANCE_PAY_ENABLED", "false")
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", PayloadCipher.generate_key())
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    get_settings.cache_clear()
