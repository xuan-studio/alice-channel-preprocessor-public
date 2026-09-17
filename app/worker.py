from __future__ import annotations

import signal
import time
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from .chat_history_export import export_chat, make_export_archive, session_context
from .config import BASE_DIR, load_settings
from .db import Database
from .repository import JobCancelled, JobPaused, Repository
from .security import bootstrap_admin
from .tdlib_adapter import (
    ChannelPreprocessor,
    JoinDailyLimitError,
    ManualReviewError,
    collector_account_from_settings,
    flood_wait_seconds,
)


def safe_error_message(exc: Exception, settings) -> str:
    text = str(exc or "")
    for secret in (
        settings.telegram_api_hash,
        settings.telegram_database_passphrase,
        settings.telegram_standby_database_passphrase,
        settings.bot_token,
        settings.ai_api_key,
        settings.bootstrap_password,
        settings.site_proxy_token,
    ):
        if secret:
            text = text.replace(secret, "<redacted>")
    return re.sub(r"(?<!\w)\+?\d{7,}(?!\w)", "<redacted-number>", text)[:1000]


def is_collector_infrastructure_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    markers = (
        "authorizationstateready",
        "尚未在此服务完成登录",
        "database encryption",
        "database_encryption",
        "session",
        "account is locked",
        "tdjson",
        "tdlib_runtime",
        "找不到已验证",
        "database加密口令",
        "数据库加密口令",
        "can't load",
        "cannot load",
        "broken pipe",
        "connection reset",
    )
    return isinstance(exc, (OSError, ImportError)) or any(marker in text for marker in markers)


def process_one(
    repository: Repository,
    preprocessor: ChannelPreprocessor,
    *,
    collector_alias: str = "primary",
    failover_reason: str = "",
) -> bool:
    job = repository.claim_next_job(collector_alias, failover_reason)
    if not job:
        return False
    try:
        result = preprocessor.run(job.public_id, job.normalized_username)
        repository.complete(job.public_id, result)
        repository.mark_collector_ready(collector_alias)
    except (JobPaused, JobCancelled):
        pass
    except ManualReviewError as exc:
        repository.fail(job.public_id, "manual_review", safe_error_message(exc, preprocessor.settings), manual_review=True)
    except JoinDailyLimitError as exc:
        repository.rate_limit(job.public_id, 3600, safe_error_message(exc, preprocessor.settings))
    except Exception as exc:
        wait_seconds = flood_wait_seconds(exc)
        if wait_seconds:
            repository.rate_limit(job.public_id, wait_seconds, "Telegram 要求等待，任务已按返回时间暂停。")
        else:
            message = safe_error_message(exc, preprocessor.settings)
            if is_collector_infrastructure_error(exc):
                runtimes = repository.collector_runtime()
                failures = int(getattr(runtimes.get(collector_alias), "consecutive_failures", 0) or 0) + 1
                unavailable = failures >= preprocessor.settings.collector_failover_threshold
                repository.mark_collector_failure(
                    collector_alias,
                    type(exc).__name__,
                    message,
                    unavailable=unavailable,
                )
                repository.fail(job.public_id, "collector_unavailable", message)
            else:
                repository.fail(job.public_id, type(exc).__name__, message)
    return True


def _timestamp(value: datetime | None) -> int:
    if value is None:
        return 0
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return int(aware.timestamp())


def process_one_chat_export(repository: Repository, settings) -> bool:
    job = repository.claim_next_chat_export_job()
    if not job:
        return False
    output_dir = BASE_DIR / "exports" / "chat-history" / "jobs" / job.public_id
    args = SimpleNamespace(
        account=job.account_name,
        output_dir=str(output_dir),
        format="both",
        max_messages=int(job.max_messages or 1000),
        since_ts=_timestamp(job.since_at),
        until_ts=_timestamp(job.until_at),
        include_media=bool(job.include_media),
        batch_sleep=0.2,
        empty_retries=2,
        empty_retry_sleep=1.0,
        tdlib_retries=2,
        tdlib_retry_sleep=2.0,
    )

    try:
        repository.update_chat_export_progress(job.public_id, progress=10, stage="resolving")
        with session_context(args) as session:
            session.get_me()

            def progress(detail):
                max_messages = max(1, int(job.max_messages or 1000))
                message_count = int(detail.get("message_count", 0) or 0)
                progress = 15 + min(80, int(message_count / max_messages * 80))
                repository.update_chat_export_progress(
                    job.public_id,
                    progress=progress,
                    stage="exporting",
                    detail=detail,
                )

            metadata = export_chat(session, job.requested_target, args, progress_callback=progress)
        if job.include_media and int(metadata.get("media_files_count", 0) or 0) > 0:
            archive_path = make_export_archive(output_dir, job.public_id)
            files = dict(metadata.get("files", {}) or {})
            files["archive"] = str(archive_path)
            metadata["files"] = files
        repository.complete_chat_export(job.public_id, metadata)
        return True
    except JobCancelled:
        return True
    except Exception as exc:
        message = safe_error_message(exc, settings)
        wait_seconds = flood_wait_seconds(exc)
        if wait_seconds:
            message = "Telegram 要求等待，导出任务请稍后重试。"
        repository.fail_chat_export(job.public_id, type(exc).__name__, message)
        return True


def main(*, once: bool = False) -> int:
    settings = load_settings()
    database = Database(settings)
    database.initialize()
    with database.session() as session:
        bootstrap_admin(session, settings)
    repository = Repository(database, settings)
    repository.recover_stale_jobs()
    repository.purge_expired()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_purge = time.monotonic()
    while not stopping:
        runtimes = repository.collector_runtime()
        primary_runtime = runtimes.get("primary")
        if primary_runtime and primary_runtime.status == "unavailable" and repository.collector_should_probe("primary"):
            try:
                primary_probe = ChannelPreprocessor(settings, repository, collector_account_from_settings(settings, "primary"))
                if primary_probe.probe():
                    repository.mark_collector_ready("primary")
                    primary_runtime = None
            except Exception as exc:
                repository.mark_collector_failure(
                    "primary",
                    type(exc).__name__,
                    safe_error_message(exc, settings),
                    unavailable=True,
                )

        primary_unavailable = bool(primary_runtime and primary_runtime.status == "unavailable")
        if primary_unavailable:
            account = collector_account_from_settings(settings, "standby")
            standby_runtime = repository.collector_runtime().get("standby")
            if not account.database_passphrase or (standby_runtime and standby_runtime.status == "unavailable"):
                worked = False
            else:
                preprocessor = ChannelPreprocessor(settings, repository, account)
                worked = process_one(
                    repository,
                    preprocessor,
                    collector_alias="standby",
                    failover_reason="primary_infrastructure_unavailable",
                )
        else:
            preprocessor = ChannelPreprocessor(settings, repository, collector_account_from_settings(settings, "primary"))
            worked = process_one(repository, preprocessor, collector_alias="primary")
        if not worked:
            worked = process_one_chat_export(repository, settings)
        if once:
            break
        if time.monotonic() - last_purge >= 3600:
            repository.purge_expired()
            last_purge = time.monotonic()
        if not worked:
            time.sleep(settings.worker_poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
