from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

from .config import BASE_DIR, load_settings
from .tdlib_adapter import ChannelPreprocessor, _load_tdlib_core, collector_account_from_settings


def value(name: str, prompt: str, *, secret: bool = False) -> str:
    configured = str(os.getenv(name, "") or "").strip()
    if configured:
        return configured
    if not sys.stdin.isatty():
        raise RuntimeError(f"{name} 需要在交互式 Terminal 配置。")
    return (getpass.getpass(prompt) if secret else input(prompt)).strip()


def main() -> int:
    settings = load_settings()
    core = _load_tdlib_core()
    api_id = settings.telegram_api_id or int(value("TELEGRAM_API_ID", "Telegram API ID: "))
    api_hash = settings.telegram_api_hash or value("TELEGRAM_API_HASH", "Telegram API Hash（隐藏）: ", secret=True)
    passphrase = settings.telegram_database_passphrase or value(
        "TELEGRAM_DATABASE_PASSPHRASE", "服务 TDLib 数据库口令（隐藏，至少8位）: ", secret=True
    )
    phone = value("TELEGRAM_PHONE", "Telegram 手机号（隐藏，含国家码）: ", secret=True)
    account_dir = (
        Path(settings.telegram_account_dir).expanduser().resolve()
        if settings.telegram_account_dir
        else BASE_DIR / "runtime" / "accounts" / settings.telegram_account_name
    )
    credentials = {
        "api_id": api_id,
        "api_hash": api_hash,
        "database_encryption_key": core.derive_database_encryption_key(passphrase, settings.telegram_account_name),
    }
    scanner = ChannelPreprocessor(settings, repository=None, account=collector_account_from_settings(settings, "primary"))
    scanner._require_proxy_binding()
    try:
        with core.locked_account_session(
            account_name=settings.telegram_account_name,
            account_dir=account_dir,
            credentials=credentials,
            library_path=settings.tdlib_library or None,
            proxy=scanner.account.proxy,
        ) as session:
            me = session.login(phone)
            scanner._verify_current_session_exit(session)
        report = {
            "status": "passed",
            "account_alias": "primary",
            "account_name": settings.telegram_account_name,
            "telegram_username": str(me.get("username", "") or ""),
            "proxy_line_key": str(scanner.account.proxy.get("line_key", "") or ""),
            "telegram_matches_vpn": True,
            "raw_ip_retained": False,
        }
        report_path = BASE_DIR / "runtime" / "new-account-login-result.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.chmod(0o600)
        print(f"登录成功：{me.get('first_name', '')} @{me.get('username', '')}".rstrip())
        return 0
    finally:
        credentials.clear()


if __name__ == "__main__":
    raise SystemExit(main())
