from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BASE_DIR, load_settings
from .tdlib_adapter import _formatted_text, _load_tdlib_core, collector_account_from_settings, public_username


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
PUBLIC_TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})(?:/.*)?$")
PRIVATE_TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/c/(\d+)(?:/\d+)?$")
EXPORT_TARGET_RE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:c/\d+(?:/\d+)?|[A-Za-z0-9_]{5,32}(?:/[^\s<>\]\[\)\(\"']*)?)"
    r"|(?<![\w@])@[A-Za-z0-9_]{5,32}\b"
    r"|(?<![\w-])-?\d{5,}(?![\w-])",
    re.I,
)
CSV_FIELDS = [
    "chat_id",
    "chat_title",
    "message_id",
    "date",
    "edit_date",
    "sender_type",
    "sender_id",
    "sender_username",
    "sender_name",
    "content_type",
    "text",
    "is_outgoing",
    "media_files",
]


def prompt_value(env_name: str, prompt: str, *, secret: bool = False) -> str:
    configured = str(os.getenv(env_name, "") or "").strip()
    if configured:
        return configured
    if not sys.stdin.isatty():
        raise RuntimeError(f"{env_name} 需要在交互式 Terminal 输入。")
    return (getpass.getpass(prompt) if secret else input(prompt)).strip()


def account_name(value: str = "") -> str:
    name = str(value or os.getenv("HISTORY_EXPORT_ACCOUNT_NAME", "history_export") or "history_export").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", name):
        raise RuntimeError("导出账号槽位名只能包含字母、数字、_、.、-，长度 3-64。")
    return name


def account_dir(name: str) -> Path:
    configured = str(os.getenv("HISTORY_EXPORT_ACCOUNT_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return BASE_DIR / "runtime" / "accounts" / name


def credentials(settings: Any, core: Any, name: str) -> dict[str, Any]:
    api_id = settings.telegram_api_id
    api_hash = settings.telegram_api_hash
    if not api_id or not api_hash:
        raise RuntimeError("缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH。")
    passphrase = settings.telegram_database_passphrase or prompt_value(
        "HISTORY_EXPORT_DATABASE_PASSPHRASE",
        "导出账号 TDLib 数据库口令（隐藏，至少8位）: ",
        secret=True,
    )
    return {
        "api_id": int(api_id),
        "api_hash": str(api_hash),
        "database_encryption_key": core.derive_database_encryption_key(passphrase, name),
    }


def proxy_from_settings(settings: Any) -> dict[str, Any]:
    if not settings.telegram_require_proxy:
        return {}
    return collector_account_from_settings(settings, "primary").proxy


def normalize_target(raw: str) -> dict[str, Any]:
    target = str(raw or "").strip()
    if not target:
        raise ValueError("目标不能为空。")
    if re.fullmatch(r"-?\d+", target):
        value = int(target)
        if value > 0:
            return {"kind": "numeric_id", "numeric_id": value, "label": target}
        return {"kind": "chat_id", "chat_id": value, "label": target}
    private_match = PRIVATE_TME_RE.match(target)
    if private_match:
        supergroup_id = int(private_match.group(1))
        return {
            "kind": "chat_id",
            "chat_id": -(10**12 + supergroup_id),
            "supergroup_id": supergroup_id,
            "label": f"tme-c-{supergroup_id}",
        }
    public_match = PUBLIC_TME_RE.match(target)
    if public_match:
        username = public_match.group(1)
        return {"kind": "username", "username": username, "label": username}
    username = target.lstrip("@")
    if USERNAME_RE.fullmatch(username):
        return {"kind": "username", "username": username, "label": username}
    raise ValueError("只支持 @username、公开 t.me 链接、数字 chat_id 或 t.me/c/... 链接。")


def target_key(target: dict[str, Any]) -> str:
    kind = str(target.get("kind", "") or "")
    if kind == "username":
        return f"username:{str(target.get('username', '')).lower()}"
    if kind == "numeric_id":
        return f"numeric:{int(target.get('numeric_id', 0) or 0)}"
    return f"chat:{int(target.get('chat_id', 0) or 0)}"


def parse_batch_export_targets(value: str, *, limit: int = 50) -> dict[str, Any]:
    source = str(value or "")
    candidates: list[str] = []
    for line in source.splitlines():
        clean_line = line.strip()
        if not clean_line:
            continue
        matches = [match.group(0).rstrip(".,;:!?，。；：！？") for match in EXPORT_TARGET_RE.finditer(clean_line)]
        candidates.extend(matches or [clean_line])
    if not candidates and source.strip():
        candidates = [source.strip()]

    targets: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    seen: set[str] = set()
    truncated = False
    for candidate in candidates:
        clean = candidate.replace("\\_", "_").strip()
        try:
            normalized = normalize_target(clean)
        except ValueError as exc:
            invalid.append({"target": clean[:256], "error": str(exc)})
            continue
        item = {"target": clean[:256], "normalized": normalized, "label": str(normalized.get("label", clean))[:160]}
        key = target_key(normalized)
        if key in seen:
            duplicates.append(item)
            continue
        if len(targets) >= limit:
            truncated = True
            continue
        seen.add(key)
        targets.append(item)
    return {"targets": targets, "duplicates": duplicates, "invalid": invalid, "truncated": truncated, "limit": limit}


def supergroup_chat_id(supergroup_id: int) -> int:
    return -(10**12 + abs(int(supergroup_id)))


def supergroup_id_from_chat_id(chat_id: int) -> int:
    value = abs(int(chat_id))
    return value - 10**12 if value > 10**12 else 0


def safe_name(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return cleaned[:80] or "chat"


def iso_time(timestamp: Any) -> str:
    try:
        raw = int(timestamp or 0)
    except (TypeError, ValueError):
        raw = 0
    if not raw:
        return ""
    return datetime.fromtimestamp(raw, timezone.utc).isoformat()


def timestamp_value(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(aware.timestamp())
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_tdlib_timeout(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "tdlib 请求超时" in text or "request timeout" in text or "timed out" in text


def invoke_with_retries(session: Any, method: str, *, retries: int = 2, retry_sleep: float = 2.0, **params: Any) -> dict[str, Any]:
    clean_retries = max(0, int(retries or 0))
    clean_sleep = max(0.0, float(retry_sleep or 0.0))
    for attempt in range(clean_retries + 1):
        try:
            return session.invoke(method, **params)
        except Exception as exc:
            if attempt >= clean_retries or not is_tdlib_timeout(exc):
                raise
            if clean_sleep:
                time.sleep(clean_sleep * (attempt + 1))
    raise RuntimeError(f"{method} retry loop exhausted")


def content_text(content: dict[str, Any]) -> str:
    kind = str(content.get("@type", "") or "")
    if kind == "messageText":
        return _formatted_text(content.get("text"))
    if "caption" in content:
        return _formatted_text(content.get("caption"))
    if kind == "messagePoll":
        poll = content.get("poll") or {}
        return str(poll.get("question", "") or "")
    if kind == "messageDice":
        return str(content.get("emoji", "") or "")
    if kind == "messageSticker":
        sticker = content.get("sticker") or {}
        return str(sticker.get("emoji", "") or "")
    if kind == "messageAnimatedEmoji":
        emoji = content.get("animated_emoji") or {}
        return str(emoji.get("emoji", "") or "")
    return ""


def safe_media_filename(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return cleaned[:120] or "media"


def _file_candidate(file_obj: Any, label: str, name_hint: str = "") -> dict[str, Any] | None:
    if not isinstance(file_obj, dict):
        return None
    file_id = int(file_obj.get("id", 0) or 0)
    if not file_id:
        return None
    return {"file_id": file_id, "label": label, "name_hint": name_hint}


def media_file_candidates(content: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(content.get("@type", "") or "")
    candidates: list[dict[str, Any] | None] = []
    if kind == "messagePhoto":
        sizes = list((content.get("photo") or {}).get("sizes") or [])
        sizes.sort(key=lambda item: int(item.get("width", 0) or 0) * int(item.get("height", 0) or 0), reverse=True)
        if sizes:
            candidates.append(_file_candidate(sizes[0].get("photo"), "photo", "photo.jpg"))
    elif kind == "messageVideo":
        video = content.get("video") or {}
        candidates.append(_file_candidate(video.get("video"), "video", video.get("file_name", "video.mp4")))
    elif kind == "messageDocument":
        document = content.get("document") or {}
        candidates.append(_file_candidate(document.get("document"), "document", document.get("file_name", "document")))
    elif kind == "messageAnimation":
        animation = content.get("animation") or {}
        candidates.append(_file_candidate(animation.get("animation"), "animation", animation.get("file_name", "animation.mp4")))
    elif kind == "messageAudio":
        audio = content.get("audio") or {}
        candidates.append(_file_candidate(audio.get("audio"), "audio", audio.get("file_name", "audio.mp3")))
    elif kind == "messageVoiceNote":
        candidates.append(_file_candidate((content.get("voice_note") or {}).get("voice"), "voice", "voice.ogg"))
    elif kind == "messageVideoNote":
        candidates.append(_file_candidate((content.get("video_note") or {}).get("video"), "video-note", "video-note.mp4"))
    elif kind == "messageSticker":
        candidates.append(_file_candidate((content.get("sticker") or {}).get("sticker"), "sticker", "sticker"))
    return [item for item in candidates if item]


def download_message_media(
    session: Any,
    content: dict[str, Any],
    media_dir: Path,
    message_id: int,
    *,
    retries: int = 2,
    retry_sleep: float = 2.0,
) -> list[str]:
    paths: list[str] = []
    candidates = media_file_candidates(content)
    if not candidates:
        return paths
    media_dir.mkdir(parents=True, exist_ok=True)
    for index, candidate in enumerate(candidates, start=1):
        try:
            downloaded = invoke_with_retries(
                session,
                "downloadFile",
                retries=retries,
                retry_sleep=retry_sleep,
                file_id=int(candidate["file_id"]),
                priority=1,
                offset=0,
                limit=0,
                synchronous=True,
            )
            local_path = str(((downloaded.get("local") or {}).get("path", "")) or "")
            if not local_path:
                continue
            source = Path(local_path)
            if not source.is_file():
                continue
            hint = safe_media_filename(candidate.get("name_hint") or candidate.get("label") or "media")
            suffix = Path(hint).suffix or source.suffix or ".bin"
            stem = safe_media_filename(Path(hint).stem or candidate.get("label") or "media")
            target = media_dir / f"{message_id}-{index}-{stem}{suffix}"
            shutil.copy2(source, target)
            paths.append(str(target))
        except Exception:
            continue
    return paths


def sender_info(session: Any, sender: dict[str, Any], cache: dict[tuple[str, int], dict[str, str]]) -> dict[str, str]:
    sender_type = str(sender.get("@type", "") or "")
    sender_id = int(sender.get("user_id", sender.get("chat_id", 0)) or 0)
    cache_key = (sender_type, sender_id)
    if cache_key in cache:
        return cache[cache_key]

    record = {"sender_type": "unknown", "sender_id": str(sender_id or ""), "sender_username": "", "sender_name": ""}
    try:
        if sender_type == "messageSenderUser":
            user = session.invoke("getUser", user_id=sender_id)
            record = {
                "sender_type": "user",
                "sender_id": str(sender_id),
                "sender_username": public_username(user),
                "sender_name": " ".join(
                    item for item in (str(user.get("first_name", "") or ""), str(user.get("last_name", "") or "")) if item
                ),
            }
        elif sender_type == "messageSenderChat":
            chat = session.invoke("getChat", chat_id=sender_id)
            username = ""
            chat_type = chat.get("type") or {}
            if str(chat_type.get("@type", "") or "") == "chatTypeSupergroup":
                supergroup_id = int(chat_type.get("supergroup_id", 0) or 0)
                if supergroup_id:
                    username = public_username(session.invoke("getSupergroup", supergroup_id=supergroup_id))
            record = {
                "sender_type": "chat",
                "sender_id": str(sender_id),
                "sender_username": username,
                "sender_name": str(chat.get("title", "") or ""),
            }
    except Exception:
        pass
    cache[cache_key] = record
    return record


def resolve_chat(session: Any, raw_target: str, *, retries: int = 2, retry_sleep: float = 2.0) -> tuple[dict[str, Any], dict[str, Any]]:
    target = normalize_target(raw_target)
    errors: list[str] = []
    if target["kind"] == "username":
        chat = invoke_with_retries(
            session,
            "searchPublicChat",
            retries=retries,
            retry_sleep=retry_sleep,
            username=str(target["username"]).lstrip("@"),
        )
    else:
        chat = None
        chat_ids: list[int] = []
        supergroup_ids: list[int] = []
        if target["kind"] == "numeric_id":
            numeric_id = int(target["numeric_id"])
            chat_ids.extend([numeric_id, -abs(numeric_id), supergroup_chat_id(numeric_id)])
            supergroup_ids.append(numeric_id)
        else:
            chat_id = int(target["chat_id"])
            chat_ids.append(chat_id)
            derived_supergroup_id = int(target.get("supergroup_id", 0) or supergroup_id_from_chat_id(chat_id))
            if derived_supergroup_id:
                supergroup_ids.append(derived_supergroup_id)

        for chat_id in dict.fromkeys(chat_ids):
            try:
                chat = invoke_with_retries(session, "getChat", retries=retries, retry_sleep=retry_sleep, chat_id=chat_id)
                break
            except Exception as exc:
                errors.append(f"getChat({chat_id}): {type(exc).__name__}: {exc}")
        if chat is None:
            for supergroup_id in dict.fromkeys(supergroup_ids):
                try:
                    chat = invoke_with_retries(
                        session,
                        "createSupergroupChat",
                        retries=retries,
                        retry_sleep=retry_sleep,
                        supergroup_id=supergroup_id,
                        force=True,
                    )
                    break
                except Exception as exc:
                    errors.append(f"createSupergroupChat({supergroup_id}): {type(exc).__name__}: {exc}")
        if chat is None:
            for basic_group_id in dict.fromkeys(supergroup_ids):
                try:
                    chat = invoke_with_retries(
                        session,
                        "createBasicGroupChat",
                        retries=retries,
                        retry_sleep=retry_sleep,
                        basic_group_id=basic_group_id,
                        force=True,
                    )
                    break
                except Exception as exc:
                    errors.append(f"createBasicGroupChat({basic_group_id}): {type(exc).__name__}: {exc}")
        if chat is None:
            raise RuntimeError(
                "无法用这个 ID 解析目标群组。请确认该账号已经加入/能访问这个群，"
                "或改用公开 @username、t.me 链接、群内任意消息链接。"
            )
    chat_id = int(chat.get("id", 0) or 0)
    if not chat_id:
        raise RuntimeError("无法解析目标群组。")
    return target, chat


def message_record(
    session: Any,
    chat: dict[str, Any],
    message: dict[str, Any],
    cache: dict[tuple[str, int], dict[str, str]],
    *,
    media_files: list[str] | None = None,
) -> dict[str, Any]:
    content = message.get("content") or {}
    sender = sender_info(session, message.get("sender_id") or {}, cache)
    return {
        "chat_id": int(chat.get("id", 0) or 0),
        "chat_title": str(chat.get("title", "") or ""),
        "message_id": int(message.get("id", 0) or 0),
        "date": iso_time(message.get("date")),
        "edit_date": iso_time(message.get("edit_date")),
        **sender,
        "content_type": str(content.get("@type", "") or ""),
        "text": content_text(content),
        "is_outgoing": bool(message.get("is_outgoing", False)),
        "media_files": "; ".join(media_files or []),
    }


def open_outputs(output_dir: Path, base: str, formats: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    jsonl_file = None
    csv_file = None
    writer = None
    paths: dict[str, Path] = {}
    if formats in {"jsonl", "both"}:
        paths["jsonl"] = output_dir / f"{base}-{timestamp}.jsonl"
        jsonl_file = paths["jsonl"].open("w", encoding="utf-8")
    if formats in {"csv", "both"}:
        paths["csv"] = output_dir / f"{base}-{timestamp}.csv"
        csv_file = paths["csv"].open("w", encoding="utf-8-sig", newline="")
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
    paths["metadata"] = output_dir / f"{base}-{timestamp}.metadata.json"
    return paths, jsonl_file, csv_file, writer


def make_export_archive(output_dir: Path, public_id: str) -> Path:
    archive_base = output_dir.parent / safe_name(public_id)
    archive_path = Path(shutil.make_archive(str(archive_base), "zip", output_dir))
    return archive_path


def export_chat(
    session: Any,
    raw_target: str,
    args: argparse.Namespace,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    tdlib_retries = max(0, int(getattr(args, "tdlib_retries", 2)))
    tdlib_retry_sleep = max(0.0, float(getattr(args, "tdlib_retry_sleep", 2.0)))
    target, chat = resolve_chat(session, raw_target, retries=tdlib_retries, retry_sleep=tdlib_retry_sleep)
    output_dir = Path(args.output_dir).expanduser().resolve()
    base = safe_name(f"{target['label']}-{int(chat.get('id', 0) or 0)}")
    paths, jsonl_file, csv_file, writer = open_outputs(output_dir, base, args.format)
    media_dir = output_dir / "media" / base
    sender_cache: dict[tuple[str, int], dict[str, str]] = {}
    seen_ids: set[int] = set()
    from_message_id = 0
    message_count = 0
    batch_count = 0
    scanned_count = 0
    media_files_count = 0
    newest_message_date = ""
    oldest_message_date = ""
    stop_reason = "unknown"
    empty_attempts = 0
    empty_retries = max(0, int(getattr(args, "empty_retries", 2)))
    empty_retry_sleep = max(0.0, float(getattr(args, "empty_retry_sleep", 1.0)))
    since_ts = timestamp_value(getattr(args, "since_ts", 0))
    until_ts = timestamp_value(getattr(args, "until_ts", 0))
    include_media = bool(getattr(args, "include_media", False))

    try:
        while True:
            remaining = int(args.max_messages) - message_count if int(args.max_messages) > 0 else 100
            if int(args.max_messages) > 0 and remaining <= 0:
                stop_reason = "max_messages_reached"
                break
            history = invoke_with_retries(
                session,
                "getChatHistory",
                retries=tdlib_retries,
                retry_sleep=tdlib_retry_sleep,
                chat_id=int(chat.get("id", 0) or 0),
                from_message_id=from_message_id,
                offset=0,
                limit=max(1, min(100, remaining)),
                only_local=False,
            )
            batch = [item for item in (history.get("messages") or []) if isinstance(item, dict)]
            if not batch:
                if empty_attempts < empty_retries:
                    empty_attempts += 1
                    if empty_retry_sleep:
                        time.sleep(empty_retry_sleep)
                    continue
                stop_reason = "no_older_messages_returned"
                break
            new_items = []
            for item in batch:
                message_id = int(item.get("id", 0) or 0)
                if message_id and message_id not in seen_ids:
                    seen_ids.add(message_id)
                    new_items.append(item)
            if not new_items:
                if empty_attempts < empty_retries:
                    empty_attempts += 1
                    if empty_retry_sleep:
                        time.sleep(empty_retry_sleep)
                    continue
                stop_reason = "no_new_messages_returned"
                break
            empty_attempts = 0
            batch_count += 1
            scanned_count += len(new_items)
            eligible_items: list[dict[str, Any]] = []
            batch_dates = [int(item.get("date", 0) or 0) for item in new_items if int(item.get("date", 0) or 0)]
            for item in new_items:
                message_date = int(item.get("date", 0) or 0)
                if until_ts and message_date and message_date > until_ts:
                    continue
                if since_ts and (not message_date or message_date < since_ts):
                    continue
                eligible_items.append(item)
            for item in eligible_items:
                content = item.get("content") or {}
                media_files = (
                    download_message_media(
                        session,
                        content,
                        media_dir,
                        int(item.get("id", 0) or 0),
                        retries=tdlib_retries,
                        retry_sleep=tdlib_retry_sleep,
                    )
                    if include_media
                    else []
                )
                media_files_count += len(media_files)
                record = message_record(session, chat, item, sender_cache, media_files=media_files)
                if record["date"]:
                    newest_message_date = newest_message_date or record["date"]
                    oldest_message_date = record["date"]
                if jsonl_file:
                    jsonl_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                if writer:
                    writer.writerow({field: record.get(field, "") for field in CSV_FIELDS})
                message_count += 1
            if jsonl_file:
                jsonl_file.flush()
            if csv_file:
                csv_file.flush()
            if message_count and message_count % 1000 == 0:
                print(f"{chat.get('title', raw_target)} 已导出 {message_count} 条...")
            next_from = min(int(item.get("id", 0) or 0) for item in batch if int(item.get("id", 0) or 0))
            if next_from == from_message_id:
                stop_reason = "pagination_cursor_unchanged"
                break
            from_message_id = next_from
            if since_ts and batch_dates and min(batch_dates) < since_ts:
                stop_reason = "date_range_exhausted"
                break
            if progress_callback:
                progress_callback(
                    {
                        "message_count": message_count,
                        "scanned_count": scanned_count,
                        "batch_count": batch_count,
                        "newest_message_date": newest_message_date,
                        "oldest_message_date": oldest_message_date,
                    }
                )
            if float(args.batch_sleep) > 0:
                time.sleep(float(args.batch_sleep))
    finally:
        if jsonl_file:
            jsonl_file.close()
        if csv_file:
            csv_file.close()

    metadata = {
        "target": raw_target,
        "resolved_target": target,
        "chat": {
            "id": int(chat.get("id", 0) or 0),
            "title": str(chat.get("title", "") or ""),
            "type": chat.get("type") or {},
        },
        "message_count": message_count,
        "batch_count": batch_count,
        "scanned_count": scanned_count,
        "stop_reason": stop_reason,
        "newest_message_date": newest_message_date,
        "oldest_message_date": oldest_message_date,
        "date_filter": {
            "since_ts": since_ts,
            "until_ts": until_ts,
            "since": iso_time(since_ts),
            "until": iso_time(until_ts),
        },
        "order": "newest_to_oldest",
        "media_requested": include_media,
        "media_downloaded": include_media,
        "media_files_count": media_files_count,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "files": {key: str(path) for key, path in paths.items()},
    }
    paths["metadata"].write_text(json.dumps(metadata, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8")
    return metadata


def session_context(args: argparse.Namespace):
    settings = load_settings()
    core = _load_tdlib_core()
    name = account_name(getattr(args, "account", ""))
    return core.locked_account_session(
        account_name=name,
        account_dir=account_dir(name),
        credentials=credentials(settings, core, name),
        library_path=settings.tdlib_library or None,
        proxy=proxy_from_settings(settings),
    )


def command_login(args: argparse.Namespace) -> int:
    phone = prompt_value("HISTORY_EXPORT_PHONE", "Telegram 手机号（隐藏，含国家码）: ", secret=True)
    with session_context(args) as session:
        me = session.login(phone)
    print(json.dumps({"ok": True, "account": account_name(args.account), "telegram_username": me.get("username", "")}, ensure_ascii=False))
    return 0


def command_whoami(args: argparse.Namespace) -> int:
    with session_context(args) as session:
        me = session.get_me()
    print(json.dumps({"ok": True, "account": account_name(args.account), "telegram_username": me.get("username", ""), "user_id": me.get("user_id", 0)}, ensure_ascii=False))
    return 0


def command_export(args: argparse.Namespace) -> int:
    failures = 0
    with session_context(args) as session:
        session.get_me()
        for target in args.targets:
            try:
                metadata = export_chat(session, target, args)
                print(json.dumps({"ok": True, "target": target, "message_count": metadata["message_count"], "files": metadata["files"]}, ensure_ascii=False))
            except Exception as exc:
                failures += 1
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "target": target,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
    return 1 if failures == len(args.targets) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出当前账号可访问的 Telegram 群组聊天历史")
    parser.add_argument("--account", default="", help="TDLib 账号槽位名，默认 history_export")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="登录导出专用 Telegram 账号槽位")
    sub.add_parser("whoami", help="查看导出账号槽位当前登录账号")

    export = sub.add_parser("export", help="导出一个或多个群组历史")
    export.add_argument("targets", nargs="+", help="@username、t.me 链接、数字 chat_id 或 t.me/c/... 链接")
    export.add_argument("--output-dir", default=os.getenv("HISTORY_EXPORT_OUTPUT_DIR", str(BASE_DIR / "exports" / "chat-history")))
    export.add_argument("--format", choices=["jsonl", "csv", "both"], default="both")
    export.add_argument("--max-messages", type=int, default=0, help="最多导出消息数；0 表示尽量导出全部可访问历史")
    export.add_argument("--since-ts", type=int, default=0, help="只导出此 Unix 时间之后的消息")
    export.add_argument("--until-ts", type=int, default=0, help="只导出此 Unix 时间之前的消息")
    export.add_argument("--include-media", action="store_true", help="同时下载可访问的媒体文件")
    export.add_argument("--batch-sleep", type=float, default=0.2, help="每批历史请求后的暂停秒数")
    export.add_argument("--empty-retries", type=int, default=2, help="遇到空页/重复页时额外重试次数，缓解 TDLib 渐进加载")
    export.add_argument("--empty-retry-sleep", type=float, default=1.0, help="空页/重复页重试前等待秒数")
    export.add_argument("--tdlib-retries", type=int, default=2, help="TDLib 请求超时时的额外重试次数")
    export.add_argument("--tdlib-retry-sleep", type=float, default=2.0, help="TDLib 请求超时重试前等待秒数")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "login":
        return command_login(args)
    if args.command == "whoami":
        return command_whoami(args)
    if args.command == "export":
        return command_export(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
