from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus


BASE_DIR = Path(__file__).resolve().parents[1]


def _local_proxy_binding() -> dict:
    path = Path(os.getenv("TELEGRAM_PROXY_BINDING_FILE", BASE_DIR / "runtime" / "proxy-binding.json"))
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("本机代理绑定文件损坏；已停止，禁止回落直连。") from exc
    return payload if isinstance(payload, dict) else {}


def _local_account_binding() -> dict:
    path = Path(os.getenv("TELEGRAM_ACCOUNT_BINDING_FILE", BASE_DIR / "runtime" / "account-binding.json"))
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("本机账号绑定文件损坏；已停止，禁止打开错误的 TDLib 数据库。") from exc
    return payload if isinstance(payload, dict) else {}


def _secret(name: str, default: str = "") -> str:
    file_name = str(os.getenv(f"{name}_FILE", "") or "").strip()
    if file_name:
        path = Path(file_name)
        if not path.is_file():
            raise RuntimeError(f"{name}_FILE 指向的 Secret 文件不存在。")
        return path.read_text(encoding="utf-8").strip()
    return str(os.getenv(name, default) or default).strip()


def _database_url(default: str) -> str:
    configured = str(os.getenv("DATABASE_URL", "") or "").strip()
    if configured:
        return configured
    host = str(os.getenv("DATABASE_HOST", "") or "").strip()
    if not host:
        return default
    user = str(os.getenv("DATABASE_USER", "preprocessor") or "preprocessor").strip()
    name = str(os.getenv("DATABASE_NAME", "channel_preprocessor") or "channel_preprocessor").strip()
    port = _int("DATABASE_PORT", 5432, 1, 65535)
    password = _secret("DATABASE_PASSWORD")
    return f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{quote_plus(name)}"


def _bool(name: str, default: bool = False) -> bool:
    value = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _int_tuple(name: str, *, fallback: int = 0) -> tuple[int, ...]:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw and fallback:
        return (fallback,)
    values: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise RuntimeError(f"{name} 必须是逗号分隔的数字 ID。") from exc
        if value not in values:
            values.append(value)
    return tuple(values)


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_name: str
    demo_mode: bool
    database_url: str
    public_base_url: str
    trusted_hosts: tuple[str, ...]
    session_cookie_secure: bool
    session_days: int
    bootstrap_username: str
    bootstrap_password: str
    bot_token: str
    workgroup_chat_id: int
    allowed_chat_ids: tuple[int, ...]
    allowed_user_ids: tuple[int, ...]
    team_site_url: str
    site_proxy_token: str
    telegram_api_id: int
    telegram_api_hash: str
    telegram_account_name: str
    telegram_account_dir: str
    telegram_database_passphrase: str
    telegram_require_proxy: bool
    telegram_proxy_line_key: str
    telegram_proxy_host: str
    telegram_proxy_port: int
    telegram_proxy_exit_fingerprint: str
    telegram_standby_account_name: str
    telegram_standby_account_dir: str
    telegram_standby_database_passphrase: str
    collector_failover_threshold: int
    collector_recovery_seconds: int
    tdlib_library: str
    ai_enabled: bool
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    ai_max_sources: int
    inactive_days: int
    max_group_messages: int
    group_active_user_limit: int
    group_ad_sample_messages: int
    raw_retention_days: int
    result_retention_days: int
    audit_retention_days: int
    cache_hours: int
    max_posts: int
    max_pinned: int
    max_comments_per_post: int
    max_comments_per_job: int
    max_new_joins_per_day: int
    worker_poll_seconds: int

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def detail_base_url(self) -> str:
        return self.team_site_url or self.public_base_url

    def validate(self) -> None:
        if self.production:
            if self.bootstrap_password in {"", "change-me", "change-me-now", "admin"} or len(self.bootstrap_password) < 16:
                raise RuntimeError("生产环境必须设置至少 16 位的 PLATFORM_BOOTSTRAP_PASSWORD Secret。")
            if self.database_url.startswith("sqlite"):
                raise RuntimeError("生产环境必须使用 PostgreSQL。")
            if not self.public_base_url.startswith("https://"):
                raise RuntimeError("生产环境 PUBLIC_BASE_URL 必须使用 HTTPS。")
            if not self.session_cookie_secure:
                raise RuntimeError("生产环境必须启用 SESSION_COOKIE_SECURE。")
            if not self.trusted_hosts or "*" in self.trusted_hosts:
                raise RuntimeError("生产环境必须配置明确的 TRUSTED_HOSTS。")
        if bool(self.telegram_api_id) != bool(self.telegram_api_hash):
            raise RuntimeError("TELEGRAM_API_ID 和 TELEGRAM_API_HASH 必须同时配置。")
        if self.telegram_require_proxy:
            if self.telegram_proxy_host not in {"127.0.0.1", "::1", "localhost"} or self.telegram_proxy_port <= 0:
                raise RuntimeError("代理强制模式必须配置 localhost SOCKS5 端口。")
            if not self.telegram_proxy_line_key or len(self.telegram_proxy_exit_fingerprint) < 16:
                raise RuntimeError("代理强制模式必须配置 line_key 与出口指纹。")


def load_settings() -> Settings:
    default_db = f"sqlite:///{(BASE_DIR / 'data' / 'preprocessor.db').resolve()}"
    legacy_workgroup_chat_id = _int("TELEGRAM_WORKGROUP_CHAT_ID", 0, -10**20, 10**20)
    proxy_binding = _local_proxy_binding()
    account_binding = _local_account_binding()
    settings = Settings(
        app_env=str(os.getenv("APP_ENV", "development") or "development").strip().lower(),
        app_name=str(os.getenv("APP_NAME", "Telegram 频道预处理台") or "Telegram 频道预处理台"),
        demo_mode=_bool("DEMO_MODE", False),
        database_url=_database_url(default_db),
        public_base_url=str(os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8848") or "").rstrip("/"),
        trusted_hosts=tuple(
            item.strip()
            for item in str(os.getenv("TRUSTED_HOSTS", "127.0.0.1,localhost,testserver") or "").split(",")
            if item.strip()
        ),
        session_cookie_secure=_bool("SESSION_COOKIE_SECURE", False),
        session_days=_int("SESSION_DAYS", 14, 1, 90),
        bootstrap_username=str(os.getenv("PLATFORM_BOOTSTRAP_USERNAME", "admin") or "admin").strip().lower(),
        bootstrap_password=_secret("PLATFORM_BOOTSTRAP_PASSWORD", "change-me-now"),
        bot_token=_secret("TELEGRAM_BOT_TOKEN"),
        workgroup_chat_id=legacy_workgroup_chat_id,
        allowed_chat_ids=_int_tuple("TELEGRAM_ALLOWED_CHAT_IDS", fallback=legacy_workgroup_chat_id),
        allowed_user_ids=_int_tuple("TELEGRAM_ALLOWED_USER_IDS"),
        team_site_url=str(os.getenv("TEAM_SITE_URL", "") or "").rstrip("/"),
        site_proxy_token=_secret("SITE_PROXY_TOKEN"),
        telegram_api_id=_int("TELEGRAM_API_ID", 0, 0),
        telegram_api_hash=_secret("TELEGRAM_API_HASH"),
        telegram_account_name=str(account_binding.get("account_name") or os.getenv("TELEGRAM_ACCOUNT_NAME", "collector") or "collector").strip(),
        telegram_account_dir=str(account_binding.get("account_dir") or os.getenv("TELEGRAM_ACCOUNT_DIR", "") or "").strip(),
        telegram_database_passphrase=_secret("TELEGRAM_DATABASE_PASSPHRASE"),
        telegram_require_proxy=_bool("TELEGRAM_REQUIRE_PROXY", bool(proxy_binding)),
        telegram_proxy_line_key=str(os.getenv("TELEGRAM_PROXY_LINE_KEY", proxy_binding.get("line_key", "")) or "").strip(),
        telegram_proxy_host=str(os.getenv("TELEGRAM_PROXY_HOST", proxy_binding.get("host", "127.0.0.1")) or "127.0.0.1").strip(),
        telegram_proxy_port=_int("TELEGRAM_PROXY_PORT", int(proxy_binding.get("port", 0) or 0), 0, 65535),
        telegram_proxy_exit_fingerprint=str(os.getenv("TELEGRAM_PROXY_EXIT_FINGERPRINT", proxy_binding.get("exit_fingerprint", "")) or "").strip().lower(),
        telegram_standby_account_name=str(os.getenv("TELEGRAM_STANDBY_ACCOUNT_NAME", "standby") or "standby").strip(),
        telegram_standby_account_dir=str(os.getenv("TELEGRAM_STANDBY_ACCOUNT_DIR", "") or "").strip(),
        telegram_standby_database_passphrase=_secret("TELEGRAM_STANDBY_DATABASE_PASSPHRASE"),
        collector_failover_threshold=_int("COLLECTOR_FAILOVER_THRESHOLD", 2, 1, 10),
        collector_recovery_seconds=_int("COLLECTOR_RECOVERY_SECONDS", 900, 60, 86400),
        tdlib_library=str(os.getenv("TDLIB_JSON_LIBRARY", "") or "").strip(),
        ai_enabled=_bool("AI_ENABLED", False),
        ai_base_url=str(os.getenv("AI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/"),
        ai_api_key=_secret("AI_API_KEY"),
        ai_model=str(os.getenv("AI_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini").strip(),
        ai_max_sources=_int("AI_MAX_SOURCES", 30, 1, 100),
        inactive_days=_int("INACTIVE_DAYS", 90, 1, 3650),
        max_group_messages=_int("MAX_GROUP_MESSAGES", 5000, 100, 5000),
        group_active_user_limit=_int("GROUP_ACTIVE_USER_LIMIT", 50, 1, 200),
        group_ad_sample_messages=_int("GROUP_AD_SAMPLE_MESSAGES", 80, 10, 300),
        raw_retention_days=_int("RAW_RETENTION_DAYS", 7, 1, 30),
        result_retention_days=_int("RESULT_RETENTION_DAYS", 90, 1, 365),
        audit_retention_days=_int("AUDIT_RETENTION_DAYS", 365, 30, 730),
        cache_hours=_int("SCAN_CACHE_HOURS", 24, 0, 168),
        max_posts=_int("MAX_POSTS", 100, 1, 100),
        max_pinned=_int("MAX_PINNED", 20, 1, 20),
        max_comments_per_post=_int("MAX_COMMENTS_PER_POST", 1000, 1, 10000),
        max_comments_per_job=_int("MAX_COMMENTS_PER_JOB", 10000, 1, 100000),
        max_new_joins_per_day=_int("MAX_NEW_JOINS_PER_DAY", 20, 0, 100),
        worker_poll_seconds=_int("WORKER_POLL_SECONDS", 3, 1, 60),
    )
    settings.validate()
    return settings
