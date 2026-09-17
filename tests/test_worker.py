from __future__ import annotations

from dataclasses import replace

from app.config import load_settings
from app.worker import is_collector_infrastructure_error, safe_error_message


def test_error_messages_redact_secrets_and_phone_numbers() -> None:
    settings = replace(
        load_settings(),
        telegram_api_hash="secret-hash",
        telegram_database_passphrase="secret-passphrase",
        ai_api_key="secret-ai-key",
    )
    cleaned = safe_error_message(
        RuntimeError("secret-hash secret-passphrase secret-ai-key +919588878127"),
        settings,
    )
    assert "secret" not in cleaned
    assert "919588878127" not in cleaned
    assert "<redacted>" in cleaned


def test_only_local_or_session_failures_trigger_account_failover() -> None:
    assert is_collector_infrastructure_error(RuntimeError("采集账号尚未在此服务完成登录。"))
    assert is_collector_infrastructure_error(OSError("broken pipe"))
    assert not is_collector_infrastructure_error(RuntimeError("FLOOD_WAIT_3600"))
    assert not is_collector_infrastructure_error(RuntimeError("达到每日公开频道自动加入上限。"))
