from __future__ import annotations

import html
import re
import signal
import time
from typing import Any

import httpx

from .config import load_settings
from .db import Database
from .extractors import ManualTargetError, normalize_channel_target
from .repository import Repository


FINAL = {"completed", "failed", "cancelled", "manual_review", "expired"}


class TelegramBot:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        response = httpx.post(f"{self.base}/{method}", json=payload, timeout=65)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(str(result.get("description", "Telegram Bot API 请求失败。")))
        return result.get("result")

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = self.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        return result if isinstance(result, list) else []

    def get_me(self) -> dict[str, Any]:
        result = self.call("getMe")
        return result if isinstance(result, dict) else {}

    def send(self, chat_id: str, text: str) -> dict[str, Any]:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)

    def edit(self, chat_id: str, message_id: str, text: str) -> None:
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


def summary_text(job, public_base_url: str) -> str:
    title = html.escape(str(job.result_summary.get("channel_title", "") or f"@{job.normalized_username}"))
    if job.status == "completed":
        summary = job.result_summary or {}
        return (
            f"✅ <b>{title}</b> 预处理完成\n"
            f"帖子：{int(summary.get('posts_scanned', 0))} · 评论：{int(summary.get('comments_fetched', 0))}\n"
            f"评论用户：{int(summary.get('unique_user_commenters', 0))} · 公开 username：{int(summary.get('public_usernames', 0))}\n"
            f"联系方式证据：{int(summary.get('contacts_found', 0))} · AI：{html.escape(str(summary.get('ai_status', 'degraded')))}\n"
            f"详情（需登录）：{html.escape(public_base_url)}/jobs/{job.public_id}"
        )
    if job.status == "manual_review":
        return f"⚠️ <b>{html.escape(job.requested_target)}</b> 需要管理员人工复核。\n任务：<code>{job.public_id}</code>"
    if job.status == "failed":
        return f"❌ <b>@{html.escape(job.normalized_username)}</b> 处理失败。\n任务：<code>{job.public_id}</code>"
    if job.status == "rate_limited":
        return f"⏳ <b>@{html.escape(job.normalized_username)}</b> 正在按 Telegram 要求等待。\n任务：<code>{job.public_id}</code>"
    return f"🔄 <b>@{html.escape(job.normalized_username)}</b>：{html.escape(job.stage)}（{job.progress}%）\n任务：<code>{job.public_id}</code>"


def _allowed_ids(settings, plural_name: str, legacy_name: str = "") -> tuple[int, ...]:
    values = getattr(settings, plural_name, ()) or ()
    if values:
        return tuple(int(value) for value in values)
    legacy = int(getattr(settings, legacy_name, 0) or 0) if legacy_name else 0
    return (legacy,) if legacy else ()


def extract_scan_targets(text: str, bot_username: str = "") -> list[str]:
    clean = text.strip()
    bot_handle = bot_username.lower().lstrip("@")
    is_command = bool(re.match(r"^/scan(?:@\w+)?(?:\s|$)", clean, flags=re.I))
    is_mention = bool(bot_handle and re.match(rf"^@{re.escape(bot_handle)}(?:\s|$)", clean, flags=re.I))
    if not is_command and not is_mention:
        return []
    if is_command:
        clean = re.sub(r"^/scan(?:@\w+)?\s*", "", clean, count=1, flags=re.I)
    else:
        clean = re.sub(rf"^@{re.escape(bot_handle)}\s*", "", clean, count=1, flags=re.I)
    candidates = re.findall(r"https?://(?:t|telegram)\.me/[^\s<>]+|(?<![\w])@[A-Za-z0-9_]{5,32}", clean, flags=re.I)
    targets: list[str] = []
    seen: set[str] = set()
    for target in candidates:
        handle = target.lower().lstrip("@")
        if bot_handle and handle == bot_handle:
            continue
        try:
            key = normalize_channel_target(target).lower()
        except (ManualTargetError, ValueError):
            key = target.lower()
        if key not in seen:
            targets.append(target)
            seen.add(key)
        if len(targets) >= 10:
            break
    return targets


def process_update(
    update: dict[str, Any],
    *,
    bot: TelegramBot,
    repository: Repository,
    settings,
    bot_username: str = "",
) -> bool:
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    allowed_chats = _allowed_ids(settings, "allowed_chat_ids", "workgroup_chat_id")
    if not allowed_chats or int(chat_id or 0) not in allowed_chats:
        return False
    text = str(message.get("text", "") or "").strip()
    sender = message.get("from") or {}
    sender_id = str(sender.get("id", "") or "")
    allowed_users = _allowed_ids(settings, "allowed_user_ids")
    if allowed_users and int(sender_id or 0) not in allowed_users:
        return False
    sender_name = " ".join(
        part for part in (str(sender.get("first_name", "") or ""), str(sender.get("last_name", "") or "")) if part
    ).strip()
    targets = extract_scan_targets(text, bot_username)
    if text.lower().startswith("/scan") or targets:
        if not targets:
            bot.send(chat_id, "没有识别到公开频道。\n用法：<code>/scan @channel</code>，一条最多 10 个。")
            return True
        if len(targets) > 1:
            bot.send(chat_id, f"📥 已识别 {len(targets)} 个频道，正在逐个创建任务。")
        for target in targets:
            try:
                username = normalize_channel_target(target)
                job, cached = repository.create_scan_job(
                    username,
                    target,
                    submitter_id=sender_id,
                    submitter_name=sender_name,
                    bot_chat_id=chat_id,
                )
                sent = bot.send(
                    chat_id,
                    ("♻️ 已返回 24 小时缓存。\n" if cached and job.status == "completed" else "📥 已接单。\n")
                    + summary_text(job, settings.detail_base_url if hasattr(settings, "detail_base_url") else settings.public_base_url),
                )
                repository.set_bot_message(job.public_id, str(sent.get("message_id", "")))
            except ManualTargetError as exc:
                job = repository.create_manual_review_job(
                    target,
                    str(exc),
                    submitter_id=sender_id,
                    submitter_name=sender_name,
                    bot_chat_id=chat_id,
                )
                sent = bot.send(chat_id, summary_text(job, settings.public_base_url))
                repository.set_bot_message(job.public_id, str(sent.get("message_id", "")))
            except ValueError as exc:
                bot.send(chat_id, f"无法创建任务：{html.escape(str(exc))}\n用法：<code>/scan @channel</code>")
    elif text.startswith("/status") or text.startswith("/result"):
        public_id = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""
        job = repository.get_job(public_id)
        base_url = settings.detail_base_url if hasattr(settings, "detail_base_url") else settings.public_base_url
        bot.send(chat_id, summary_text(job, base_url) if job else "没有找到该任务。")
    return True


def main() -> int:
    settings = load_settings()
    if not settings.bot_token or not settings.allowed_chat_ids:
        raise RuntimeError("Bot 未配置：需要 TELEGRAM_BOT_TOKEN 与 TELEGRAM_ALLOWED_CHAT_IDS。")
    database = Database(settings)
    database.initialize()
    repository = Repository(database, settings)
    bot = TelegramBot(settings.bot_token)
    bot_username = str(bot.get_me().get("username", "") or "")
    offset = 0
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            for update in bot.get_updates(offset):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                process_update(update, bot=bot, repository=repository, settings=settings, bot_username=bot_username)
            for job in repository.bot_jobs_needing_notification():
                try:
                    bot.edit(job.bot_chat_id, job.bot_message_id, summary_text(job, settings.detail_base_url))
                    repository.mark_bot_notified(job.public_id, job.status)
                except Exception:
                    pass
        except (httpx.HTTPError, RuntimeError):
            time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
