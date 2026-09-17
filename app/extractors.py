from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable
from urllib.parse import urlparse


USERNAME_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{5,32})\b")
TME_RE = re.compile(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})(?:/[^\s]*)?", re.I)
TELEGRAM_INVITE_RE = re.compile(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)[A-Za-z0-9_-]+", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,24}\b", re.I)
URL_RE = re.compile(r"https?://[^\s<>\]\[\)\(\"'，。；：！？]+", re.I)
PHONE_RE = re.compile(r"(?:(?:WhatsApp|WA|电话|手机|Phone|Tel)\s*[:：]?\s*)(\+?[0-9][0-9 ()-]{6,20}[0-9])", re.I)
WECHAT_RE = re.compile(r"(?:(?:微信|WeChat)\s*[:：]?\s*)([A-Za-z][A-Za-z0-9_-]{5,19})", re.I)
LINE_RE = re.compile(r"(?:Line\s*(?:ID)?\s*[:：]?\s*)([A-Za-z0-9._-]{4,32})", re.I)
CONTACT_CONTEXT_RE = re.compile(
    r"contact|dm\b|direct message|private message|message me|send (?:me )?a message|"
    r"reach (?:me|us)|for (?:more |all )?(?:info|information)|question|interested|"
    r"join|group|vip|telegram|whatsapp|wechat|line\s*id|email|e-mail|website|official|blog|"
    r"联系|私信|咨询|加入|群|客服|邮箱|官网|微信",
    re.I,
)
NEGATIVE_CONTEXT_RE = re.compile(
    r"scam|scammer|fraud|fake|beware|be careful|be carefull|warning|warn(?:ing|ed)?|"
    r"stole|steal|骗子|诈骗|冒充|警告|小心",
    re.I,
)
TELEGRAM_SERVICE_PATHS = {"boost", "share", "iv", "s", "addstickers", "addemoji", "proxy", "socks"}
SCAN_TARGET_RE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s<>\]\[\)\(\"']+|(?<![\w])@[A-Za-z0-9_]{5,32}\b",
    re.I,
)


class ManualTargetError(ValueError):
    """The target is Telegram-shaped but needs an administrator to review it."""


@dataclass(frozen=True)
class ContactCandidate:
    contact_type: str
    value: str
    source_type: str
    source_ref: str
    evidence: str
    confidence: str = "high"
    extractor: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_channel_target(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("频道不能为空。")
    if clean.startswith("@"):
        username = clean[1:]
    elif re.fullmatch(r"[A-Za-z0-9_]{5,32}", clean):
        username = clean
    else:
        parsed = urlparse(clean if "://" in clean else f"https://{clean}")
        if parsed.hostname not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            raise ValueError("只接受公开 Telegram @username 或 t.me 链接。")
        first = parsed.path.strip("/").split("/", 1)[0]
        if first in {"joinchat", "+", "c"} or first.startswith("+"):
            raise ManualTargetError("私密邀请或内部链接第一版进入人工复核，不能自动扫描。")
        username = first
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("公开频道 username 格式无效。")
    return username.lower()


def parse_batch_channel_targets(value: str, *, limit: int = 50) -> dict[str, Any]:
    """Extract and normalize a pasted list of public Telegram channel targets."""
    source = str(value or "")
    candidates: list[str] = []
    for line in source.splitlines():
        clean_line = line.strip()
        if not clean_line:
            continue
        matches = [match.group(0).rstrip(".,;:!?，。；：！？") for match in SCAN_TARGET_RE.finditer(clean_line)]
        candidates.extend(matches or [clean_line])
    if not candidates and source.strip():
        candidates = [source.strip()]

    targets: list[dict[str, str]] = []
    duplicates: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    seen: set[str] = set()
    truncated = False
    for candidate in candidates:
        clean = candidate.replace("\\_", "_").strip()
        try:
            username = normalize_channel_target(clean)
        except (ManualTargetError, ValueError) as exc:
            invalid.append({"target": clean[:256], "error": str(exc)})
            continue
        item = {"target": clean[:256], "username": username}
        if username in seen:
            duplicates.append(item)
            continue
        if len(targets) >= limit:
            truncated = True
            continue
        seen.add(username)
        targets.append(item)
    return {
        "targets": targets,
        "duplicates": duplicates,
        "invalid": invalid,
        "truncated": truncated,
        "limit": limit,
    }


def _evidence(text: str, start: int, end: int, limit: int = 220) -> str:
    left = max(0, start - 70)
    right = min(len(text), end + 70)
    snippet = " ".join(text[left:right].split())
    return snippet[:limit]


def _is_explicit_contact(text: str, match: re.Match[str], source_type: str) -> bool:
    left = max(0, match.start() - 140)
    right = min(len(text), match.end() + 140)
    context = text[left:right]
    if NEGATIVE_CONTEXT_RE.search(context):
        return False
    if source_type == "bio":
        return True
    return bool(CONTACT_CONTEXT_RE.search(context))


def extract_contacts(text: str, *, source_type: str, source_ref: str) -> list[ContactCandidate]:
    source = str(text or "")
    found: list[ContactCandidate] = []
    occupied: set[tuple[str, str]] = set()

    def add(kind: str, value: str, match: re.Match[str]) -> None:
        normalized = value.strip().rstrip(".,;:!?，。；：！？")
        key = (kind, normalized.lower())
        if not normalized or key in occupied:
            return
        occupied.add(key)
        found.append(
            ContactCandidate(kind, normalized, source_type, source_ref, _evidence(source, match.start(), match.end()))
        )

    for match in EMAIL_RE.finditer(source):
        if _is_explicit_contact(source, match, source_type):
            add("email", match.group(0), match)
    for match in TME_RE.finditer(source):
        path = urlparse(match.group(0)).path.strip("/").split("/", 1)[0].lower()
        if path not in TELEGRAM_SERVICE_PATHS and _is_explicit_contact(source, match, source_type):
            add("telegram", match.group(0), match)
    for match in TELEGRAM_INVITE_RE.finditer(source):
        if _is_explicit_contact(source, match, source_type):
            add("discussion_group", match.group(0), match)
    for match in USERNAME_RE.finditer(source):
        if _is_explicit_contact(source, match, source_type):
            add("telegram_username", f"@{match.group(1)}", match)
    for match in PHONE_RE.finditer(source):
        if _is_explicit_contact(source, match, source_type):
            add("public_phone_or_whatsapp", match.group(1), match)
    for match in WECHAT_RE.finditer(source):
        if _is_explicit_contact(source, match, source_type):
            add("wechat", match.group(1), match)
    for match in LINE_RE.finditer(source):
        if _is_explicit_contact(source, match, source_type):
            add("line", match.group(1), match)
    for match in URL_RE.finditer(source):
        value = match.group(0).rstrip(".,;:!?，。；：！？")
        if TME_RE.fullmatch(value) or TELEGRAM_INVITE_RE.fullmatch(value):
            continue
        if _is_explicit_contact(source, match, source_type):
            add("website", value, match)
    return found


def merge_contacts(items: Iterable[ContactCandidate | dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        value = item.to_dict() if isinstance(item, ContactCandidate) else dict(item)
        key = (
            str(value.get("contact_type", "")),
            str(value.get("value", "")).lower(),
            str(value.get("source_ref", "")),
        )
        if key[0] and key[1] and key not in merged:
            merged[key] = value
    return sorted(merged.values(), key=lambda item: (item["contact_type"], item["value"], item["source_ref"]))


def parse_ai_json(raw_text: str) -> list[dict[str, Any]]:
    clean = str(raw_text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.I)
    payload = json.loads(clean)
    items = payload.get("contacts", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("AI contacts 必须是数组。")
    return [item for item in items if isinstance(item, dict)]


def validate_ai_contacts(
    candidates: Iterable[dict[str, Any]], sources: dict[str, str]
) -> list[dict[str, Any]]:
    allowed_types = {
        "telegram",
        "telegram_username",
        "discussion_group",
        "admin_dm",
        "bot",
        "email",
        "website",
        "public_phone_or_whatsapp",
        "wechat",
        "line",
        "other",
    }
    valid: list[dict[str, Any]] = []
    for item in candidates:
        source_ref = str(item.get("source_ref", "") or "")
        source_text = str(sources.get(source_ref, "") or "")
        value = str(item.get("value", "") or "").strip()
        contact_type = str(item.get("contact_type", "") or "").strip()
        if not value or not source_text or contact_type not in allowed_types:
            continue
        if value.lower() not in source_text.lower():
            continue
        index = source_text.lower().find(value.lower())
        valid.append(
            {
                "contact_type": contact_type,
                "value": value,
                "source_type": str(item.get("source_type", "post") or "post"),
                "source_ref": source_ref,
                "evidence": _evidence(source_text, index, index + len(value)),
                "confidence": str(item.get("confidence", "medium") or "medium"),
                "extractor": "ai",
            }
        )
    return merge_contacts(valid)
