from __future__ import annotations

import pytest

from app.config import load_settings


def test_production_rejects_default_bootstrap_password(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://internal.example")
    monkeypatch.setenv("TRUSTED_HOSTS", "internal.example")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "change-me-now")
    with pytest.raises(RuntimeError, match="16"):
        load_settings()
