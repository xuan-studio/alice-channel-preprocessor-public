from __future__ import annotations

import csv
import hmac
import io
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text as sql_text

from .chat_history_export import parse_batch_export_targets
from .config import BASE_DIR, load_settings
from .db import Database
from .extractors import ManualTargetError, normalize_channel_target, parse_batch_channel_targets
from .insights import LANGUAGE_LABELS
from .models import ChatExportJob, PlatformSession, PlatformUser, ScanJob
from .repository import ACTIVE_STATUSES, LEAD_STATUSES, USER_ROLES, Repository, clean_tags, is_explicit_contact_evidence
from .security import (
    bootstrap_admin,
    create_login_session,
    generate_temporary_password,
    hash_password,
    logout_session,
    resolve_login_session,
    token_hash,
    verify_password,
)


settings = load_settings()
database = Database(settings)
repository = Repository(database, settings)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def format_shanghai(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["shanghai"] = format_shanghai


class ScanRequest(BaseModel):
    target: str
    force: bool = False
    tags: list[str] = Field(default_factory=list)


class SiteLoginRequest(BaseModel):
    username: str
    password: str


class SiteJobRequest(BaseModel):
    target: str
    force: bool = False
    tags: list[str] = Field(default_factory=list)


class SiteBatchJobRequest(BaseModel):
    targets: str
    force: bool = False
    tags: list[str] = Field(default_factory=list)


class SiteJobTagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)


class SiteLeadUpdateRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)
    lead_status: str = "new"
    followup_note: str = ""


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        with database.session() as session:
            bootstrap_admin(session, settings)
        yield

    app = FastAPI(title=settings.app_name, version="0.2.1", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts) or ["*"])
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.exception_handler(HTTPException)
    async def browser_auth_redirect(request: Request, exc: HTTPException):
        accept = request.headers.get("accept", "").lower()
        if exc.status_code == 401 and exc.detail == "login_required" and "text/html" in accept:
            return RedirectResponse("/login", status_code=303)
        if exc.status_code == 403 and exc.detail == "password_change_required" and "text/html" in accept:
            return RedirectResponse("/account/password?force=1", status_code=303)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    app.include_router(build_routes())
    return app


def _auth(request: Request) -> tuple[PlatformUser, str]:
    raw = request.cookies.get("preprocessor_session", "")
    with database.session() as session:
        login = resolve_login_session(session, raw)
        if not login:
            raise HTTPException(status_code=401, detail="login_required")
        user = login.user
        session.expunge(user)
        return user, login.csrf_token


def require_user(request: Request) -> tuple[PlatformUser, str]:
    auth = _auth(request)
    if auth[0].must_change_password:
        raise HTTPException(status_code=403, detail="password_change_required")
    return auth


def require_user_allow_password_change(request: Request) -> tuple[PlatformUser, str]:
    return _auth(request)


def require_admin(auth=Depends(require_user)) -> tuple[PlatformUser, str]:
    if auth[0].role not in {"super_admin", "admin"}:
        raise HTTPException(status_code=403, detail="admin_required")
    return auth


def require_super_admin(auth=Depends(require_user)) -> tuple[PlatformUser, str]:
    if auth[0].role != "super_admin":
        raise HTTPException(status_code=403, detail="super_admin_required")
    return auth


def can_force_scan(user: PlatformUser) -> bool:
    return user.role in {"super_admin", "admin"}


def actor_name(user: PlatformUser) -> str:
    return f"platform:{user.username}"


EXPORT_DATE_RANGE_LABELS = {
    "recent_7": "最近一周",
    "recent_30": "最近一个月",
    "all": "不限日期",
    "custom": "自定义日期",
}
EXPORT_STOP_REASON_LABELS = {
    "max_messages_reached": "达到最大数量",
    "no_older_messages_returned": "没有更早可访问消息",
    "no_new_messages_returned": "没有新的可写入消息",
    "pagination_cursor_unchanged": "分页游标停止",
    "date_range_exhausted": "已到日期范围边界",
}
EXPORT_CHAT_TYPE_LABELS = {
    "chatTypeBasicGroup": "普通群",
    "chatTypeSupergroup": "超级群/频道",
    "chatTypePrivate": "私聊",
}


def parse_export_local_date(value: str, *, end_of_day: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        local_date = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("自定义日期格式无效。") from exc
    local_time = dt_time.max.replace(microsecond=0) if end_of_day else dt_time.min
    local_value = datetime.combine(local_date, local_time, tzinfo=ZoneInfo("Asia/Shanghai"))
    return local_value.astimezone(timezone.utc)


def resolve_export_date_range(date_range: str, since_date: str = "", until_date: str = "") -> tuple[str, datetime | None, datetime | None]:
    clean_range = date_range if date_range in EXPORT_DATE_RANGE_LABELS else "recent_7"
    now = datetime.now(timezone.utc)
    if clean_range == "recent_7":
        return clean_range, now - timedelta(days=7), now
    if clean_range == "recent_30":
        return clean_range, now - timedelta(days=30), now
    if clean_range == "all":
        return clean_range, None, None
    since_at = parse_export_local_date(since_date, end_of_day=False)
    until_at = parse_export_local_date(until_date, end_of_day=True)
    if not since_at and not until_at:
        raise ValueError("自定义日期需要填写开始或结束日期。")
    if since_at and until_at and since_at > until_at:
        raise ValueError("开始日期不能晚于结束日期。")
    return "custom", since_at, until_at


def resolve_export_max_messages(mode: str, custom_value: str = "") -> int:
    if mode == "custom":
        raw = str(custom_value or "").strip()
        if not raw:
            raise ValueError("自定义最大数量不能为空。")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("自定义最大数量必须是整数。") from exc
    else:
        try:
            value = int(mode or 1000)
        except ValueError:
            value = 1000
    if value < 1 or value > 100000:
        raise ValueError("最大数量需要在 1 到 100000 之间。")
    return value


def chat_export_json(job: ChatExportJob) -> dict[str, Any]:
    return {
        "id": job.public_id,
        "target": job.requested_target,
        "target_label": job.target_label,
        "created_by_username": job.created_by_username or "",
        "tags": clean_tags(job.tags or []),
        "date_range": job.date_range,
        "date_range_label": EXPORT_DATE_RANGE_LABELS.get(job.date_range, job.date_range),
        "since_at": _iso(job.since_at),
        "until_at": _iso(job.until_at),
        "max_messages": int(job.max_messages or 0),
        "include_media": bool(job.include_media),
        "status": job.status,
        "stage": job.stage,
        "progress": int(job.progress or 0),
        "chat_id": job.chat_id,
        "chat_title": job.chat_title,
        "chat_type": job.chat_type,
        "chat_type_label": EXPORT_CHAT_TYPE_LABELS.get(job.chat_type, job.chat_type),
        "message_count": int(job.message_count or 0),
        "scanned_count": int(job.scanned_count or 0),
        "media_files_count": int(job.media_files_count or 0),
        "stop_reason": job.stop_reason,
        "stop_reason_label": EXPORT_STOP_REASON_LABELS.get(job.stop_reason, job.stop_reason),
        "files": dict(job.files or {}),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "finished_at": _iso(job.finished_at),
    }


def scan_submission_redirect(
    *,
    target: str,
    tags: str,
    force: bool,
    user: PlatformUser,
) -> RedirectResponse:
    if settings.demo_mode:
        return RedirectResponse(f"/?error={quote('演示模式不会创建 Telegram 扫描任务')}", status_code=303)
    parsed = parse_batch_channel_targets(target)
    parsed_count = len(parsed["targets"]) + len(parsed["duplicates"]) + len(parsed["invalid"])

    if len(parsed["targets"]) == 1 and not parsed["duplicates"] and not parsed["invalid"]:
        item = parsed["targets"][0]
        job, _cached = repository.create_scan_job(
            item["username"],
            item["target"],
            created_by_user_id=user.id,
            created_by_username=user.username,
            tags=tags,
            force=force,
            actor=actor_name(user),
        )
        return RedirectResponse(f"/jobs/{job.public_id}", status_code=303)

    if parsed["targets"]:
        results = []
        for item in parsed["targets"]:
            job, cached = repository.create_scan_job(
                item["username"],
                item["target"],
                created_by_user_id=user.id,
                created_by_username=user.username,
                tags=tags,
                force=force,
                actor=actor_name(user),
            )
            results.append({"job": job, "cached": cached})

        created_count = sum(1 for item in results if not item["cached"])
        reused_count = len(results) - created_count
        parts = [f"提交 {len(results)} 个目标", f"新建 {created_count} 个", f"复用 {reused_count} 个"]
        if parsed["duplicates"]:
            parts.append(f"跳过重复 {len(parsed['duplicates'])} 个")
        if parsed["invalid"]:
            parts.append(f"无效 {len(parsed['invalid'])} 个")
        if parsed["truncated"]:
            parts.append(f"超过上限，仅处理前 {parsed['limit']} 个")
        return RedirectResponse(f"/?message={quote('，'.join(parts))}", status_code=303)

    if parsed_count == 1 and parsed["invalid"]:
        error = parsed["invalid"][0]["error"]
        if isinstance(error, str) and error.startswith("私密邀请"):
            job = repository.create_manual_review_job(
                target,
                error,
                created_by_user_id=user.id,
                created_by_username=user.username,
                tags=tags,
                actor=actor_name(user),
            )
            return RedirectResponse(f"/jobs/{job.public_id}", status_code=303)
        return RedirectResponse(f"/?error={quote(error)}", status_code=303)

    reason = parsed["invalid"][0]["error"] if parsed["invalid"] else "没有识别到可处理的公开频道或群组。"
    return RedirectResponse(f"/?error={quote(reason)}", status_code=303)


def chat_export_submission_redirect(
    *,
    targets: str,
    tags: str,
    date_range: str,
    since_date: str,
    until_date: str,
    max_messages_mode: str,
    max_messages_custom: str,
    include_media: bool,
    user: PlatformUser,
) -> RedirectResponse:
    if settings.demo_mode:
        return RedirectResponse(f"/chat-exports?error={quote('演示模式不会读取或导出 Telegram 聊天')}", status_code=303)
    parsed = parse_batch_export_targets(targets)
    parsed_count = len(parsed["targets"]) + len(parsed["duplicates"]) + len(parsed["invalid"])
    if not parsed["targets"]:
        reason = parsed["invalid"][0]["error"] if parsed["invalid"] else "没有识别到可导出的 Telegram 目标。"
        return RedirectResponse(f"/chat-exports?error={quote(reason)}", status_code=303)
    try:
        clean_range, since_at, until_at = resolve_export_date_range(date_range, since_date, until_date)
        max_messages = resolve_export_max_messages(max_messages_mode, max_messages_custom)
    except ValueError as exc:
        return RedirectResponse(f"/chat-exports?error={quote(str(exc))}", status_code=303)

    jobs = []
    for item in parsed["targets"]:
        jobs.append(
            repository.create_chat_export_job(
                item["target"],
                target_label=item["label"],
                created_by_user_id=user.id,
                created_by_username=user.username,
                tags=tags,
                date_range=clean_range,
                since_at=since_at,
                until_at=until_at,
                max_messages=max_messages,
                include_media=include_media,
                account_name=os.getenv("CHAT_EXPORT_ACCOUNT_NAME", "").strip() or settings.telegram_account_name or "history_export",
                actor=actor_name(user),
            )
        )
    if len(jobs) == 1 and not parsed["duplicates"] and not parsed["invalid"] and parsed_count == 1:
        return RedirectResponse(f"/chat-exports/{jobs[0].public_id}", status_code=303)
    parts = [f"提交 {len(jobs)} 个导出任务"]
    if parsed["duplicates"]:
        parts.append(f"跳过重复 {len(parsed['duplicates'])} 个")
    if parsed["invalid"]:
        parts.append(f"无效 {len(parsed['invalid'])} 个")
    if parsed["truncated"]:
        parts.append(f"超过上限，仅处理前 {parsed['limit']} 个")
    return RedirectResponse(f"/chat-exports?message={quote('，'.join(parts))}", status_code=303)


def admin_users_redirect(*, message: str = "", error: str = "") -> RedirectResponse:
    parts = []
    if message:
        parts.append(f"message={quote(message)}")
    if error:
        parts.append(f"error={quote(error)}")
    suffix = "?" + "&".join(parts) if parts else ""
    return RedirectResponse(f"/admin/users{suffix}", status_code=303)


def require_site_proxy(x_alice_proxy_token: str = Header("")) -> None:
    expected = settings.site_proxy_token
    if not expected or not x_alice_proxy_token or not hmac.compare_digest(expected, x_alice_proxy_token):
        raise HTTPException(status_code=401, detail="proxy_auth_failed")


def require_site_user(
    authorization: str = Header(""),
    _proxy=Depends(require_site_proxy),
) -> tuple[PlatformUser, str, str]:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="login_required")
    raw = authorization.split(" ", 1)[1].strip()
    with database.session() as session:
        login = resolve_login_session(session, raw)
        if not login:
            raise HTTPException(status_code=401, detail="login_required")
        if login.user.must_change_password:
            raise HTTPException(status_code=403, detail="password_change_required")
        user = login.user
        csrf = login.csrf_token
        session.expunge(user)
    return user, csrf, raw


def csrf_guard(expected: str, supplied: str) -> None:
    if not supplied or supplied != expected:
        raise HTTPException(status_code=403, detail="csrf_failed")


def csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def contact_source_rank(group: dict[str, Any]) -> tuple[int, str, str]:
    source_types = set(group.get("source_types", []))
    rank = 0 if "bio" in source_types else 1 if "pinned" in source_types else 2
    return rank, str(group.get("contact_type", "")), str(group.get("value", "")).lower()


def is_direct_contact_group(group: dict[str, Any]) -> bool:
    return str(group.get("contact_type", "")) in {"telegram", "telegram_username"}


def normalized_contact_handle(value: Any) -> str:
    return str(value or "").strip().lower().lstrip("@")


def merge_direct_contact_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in sorted(groups, key=contact_source_rank):
        if not is_direct_contact_group(group):
            continue
        valid_examples = [item for item in group.get("examples", []) if is_explicit_contact_evidence(item)]
        if not valid_examples:
            continue
        key = normalized_contact_handle(group.get("value"))
        if not key:
            continue
        if key not in merged:
            merged[key] = {
                **group,
                "source_types": list(group.get("source_types", [])),
                "extractors": list(group.get("extractors", [])),
                "tags": list(group.get("tags", [])),
                "lead_status": str(group.get("lead_status", "new") or "new"),
                "followup_note": str(group.get("followup_note", "") or ""),
                "examples": valid_examples,
                "evidence_count": len(valid_examples),
            }
            continue
        current = merged[key]
        current["source_types"] = sorted(set(current.get("source_types", [])) | set(group.get("source_types", [])))
        current["extractors"] = sorted(set(current.get("extractors", [])) | set(group.get("extractors", [])))
        current["tags"] = sorted(set(current.get("tags", [])) | set(group.get("tags", [])), key=str.lower)
        statuses = {current.get("lead_status", "new"), group.get("lead_status", "new")}
        current["lead_status"] = next((status for status in ("converted", "interested", "contacted", "todo", "no_reply", "not_fit", "new") if status in statuses), "new")
        notes = [str(current.get("followup_note", "") or ""), str(group.get("followup_note", "") or "")]
        current["followup_note"] = "\n".join(dict.fromkeys(note for note in notes if note))
        current["evidence_count"] = int(current.get("evidence_count", 0)) + len(valid_examples)
        known = {(item.source_ref, item.evidence) for item in current.get("examples", [])}
        for example in valid_examples:
            if (example.source_ref, example.evidence) not in known and len(current["examples"]) < 5:
                current["examples"].append(example)
                known.add((example.source_ref, example.evidence))
    return sorted(merged.values(), key=contact_source_rank)


def commenter_priority(comment_count: int, post_count: int) -> str:
    if post_count >= 5 or (comment_count >= 10 and post_count >= 2):
        return "高"
    if post_count >= 2 or comment_count >= 3:
        return "中"
    return "低"


GROUP_QUALITY_LABELS = {
    "active_discussion": "真实互动群",
    "mixed": "混合内容群",
    "pure_ad_group": "纯广告群",
    "unknown": "待判断",
}
GROUP_QUALITY_TONES = {
    "active_discussion": "positive",
    "mixed": "warning",
    "pure_ad_group": "danger",
    "unknown": "neutral",
}
GROUP_QUALITY_METHOD_LABELS = {
    "ai": "AI 判断",
    "heuristic": "规则判断",
    "heuristic_after_ai_error": "AI 超时后规则兜底",
}
SKIP_REASON_LABELS = {
    "pure_ad_group": "已跳过用户拓展",
    "inactive": "已按活跃度过滤",
    "no_messages": "缺少可分析消息",
}

LEAD_STATUS_OPTIONS = [
    ("new", "新线索"),
    ("todo", "待跟进"),
    ("contacted", "已联系"),
    ("interested", "有意向"),
    ("converted", "已转化"),
    ("no_reply", "未回复"),
    ("not_fit", "不适合"),
]


def group_quality_view(summary: dict[str, Any] | None) -> dict[str, Any]:
    safe_summary = summary if isinstance(summary, dict) else {}
    raw_quality = safe_summary.get("group_quality", {})
    quality = raw_quality if isinstance(raw_quality, dict) else {}
    entity_type = str(safe_summary.get("entity_type", "") or "")
    visible = entity_type == "public_group" or bool(quality)
    classification = str(quality.get("classification", "") or "unknown").strip()
    if classification == "not_applicable":
        classification = "unknown"
        visible = False
    if classification not in GROUP_QUALITY_LABELS:
        classification = "unknown"

    ad_score: float | None
    try:
        ad_score = float(quality["ad_score"]) if "ad_score" in quality else None
    except (TypeError, ValueError):
        ad_score = None
    if ad_score is not None:
        ad_score = max(0.0, min(1.0, ad_score))
    ad_percent = int(round(ad_score * 100)) if ad_score is not None else None
    confidence = str(quality.get("confidence", "") or "").strip()
    confidence_label = {"high": "高", "medium": "中", "low": "低"}.get(confidence, "待判断")
    method = str(quality.get("method", "") or "").strip()
    reasons = quality.get("reasons", [])
    if not isinstance(reasons, list):
        reasons = []
    cleaned_reasons = [str(item).strip()[:300] for item in reasons if str(item).strip()][:8]
    warnings = safe_summary.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    ai_warning = any("AI group quality" in str(item) for item in warnings)
    skip_reason = str(safe_summary.get("skip_reason", "") or "").strip()

    if not quality and visible:
        status_text = "旧任务未保存群质量判断，强制重扫后会生成。"
    elif classification == "pure_ad_group":
        status_text = "广告或单向推广占比较高，当前任务会停止用户拓展。"
    elif classification == "active_discussion":
        status_text = "群内存在真实互动，适合继续筛选活跃用户。"
    elif classification == "mixed":
        status_text = "同时存在推广和互动，需要结合候选用户继续判断。"
    else:
        status_text = "样本不足或内容信号不明确。"
    if ai_warning and quality:
        status_text = f"{status_text} AI 调用超时，已保留规则兜底结果。"

    return {
        "visible": visible,
        "classification": classification,
        "label": GROUP_QUALITY_LABELS[classification],
        "tone": GROUP_QUALITY_TONES[classification],
        "confidence": confidence,
        "confidence_label": confidence_label,
        "ad_score": ad_score,
        "ad_percent": ad_percent,
        "score_label": f"{ad_percent}%" if ad_percent is not None else "待重扫",
        "method": method,
        "method_label": GROUP_QUALITY_METHOD_LABELS.get(method, "未保存"),
        "reasons": cleaned_reasons,
        "messages_scanned": int(safe_summary.get("group_messages_scanned", quality.get("messages_scanned", 0)) or 0),
        "sampled_messages": int(quality.get("sampled_messages", 0) or 0),
        "candidate_limit": int(safe_summary.get("group_active_user_limit", quality.get("candidate_limit", 0)) or 0),
        "skip_reason": skip_reason,
        "skip_reason_label": SKIP_REASON_LABELS.get(skip_reason, ""),
        "ai_warning": ai_warning,
        "status_text": status_text,
    }


def detail_view(bundle: dict[str, Any]) -> dict[str, Any]:
    groups = sorted(bundle.get("contact_groups", []), key=contact_source_rank)
    merged_direct_groups = merge_direct_contact_groups(groups)
    direct_groups = [
        group for group in merged_direct_groups if group.get("entity_kind", "unknown") not in {"bot", "channel", "group"}
    ]
    excluded_groups = [
        group for group in merged_direct_groups if group.get("entity_kind", "unknown") in {"bot", "channel", "group"}
    ]
    other_groups = [group for group in groups if not is_direct_contact_group(group)] + excluded_groups
    contact_handles = {normalized_contact_handle(group.get("value")) for group in direct_groups}
    cross_counts = bundle.get("cross_channel_counts", {})
    candidates: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    other_commenters = []
    for item in bundle.get("commenters", []):
        if item.sender_type != "user" or not item.username:
            other_commenters.append(item)
            continue
        row = {
            "item": item,
            "priority": commenter_priority(int(item.comment_count or 0), int(item.post_count or 0)),
            "cross_channel_count": int(cross_counts.get(str(item.user_id), 1) or 1),
            "source_posts": list(item.source_posts or []),
            "tags": clean_tags(getattr(item, "tags", []) or []),
            "lead_status": str(getattr(item, "lead_status", "new") or "new"),
            "followup_note": str(getattr(item, "followup_note", "") or ""),
        }
        if normalized_contact_handle(item.username) in contact_handles:
            overlaps.append(row)
        else:
            candidates.append(row)
    return {
        "direct_contact_groups": direct_groups,
        "other_contact_groups": other_groups,
        "excluded_contact_groups": excluded_groups,
        "audience_candidates": candidates,
        "contact_overlap_commenters": overlaps,
        "other_commenters": other_commenters,
    }


def filter_global_leads(
    leads: list[dict[str, Any]],
    *,
    query: str = "",
    status: str = "",
    kind: str = "",
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().lower().lstrip("@")
    filtered = []
    for lead in leads:
        if status and lead.get("lead_status") != status:
            continue
        if kind and lead.get("lead_kind") != kind:
            continue
        haystack = " ".join(
            [
                str(lead.get("value", "")),
                str(lead.get("display_name", "")),
                " ".join(lead.get("tags", [])),
                " ".join(str(item.get("username", "")) for item in lead.get("sources", [])),
            ]
        ).lower()
        if needle and needle not in haystack:
            continue
        filtered.append(lead)
    return filtered


def build_community_export_rows(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    job = bundle["job"]
    snapshot = bundle.get("snapshot")
    ai_report = dict((job.result_summary or {}).get("ai_report", {}) or {})
    quality = group_quality_view(job.result_summary or {})
    view = detail_view(bundle)
    groups = view["direct_contact_groups"] or [None]
    rows: list[dict[str, Any]] = []
    for group in groups:
        examples = list(group.get("examples", [])) if group else []
        source_types = list(group.get("source_types", [])) if group else []
        if "bio" in source_types:
            role = "BIO 公开负责人候选"
        elif "pinned" in source_types:
            role = "置顶公开负责人候选"
        else:
            role = "帖子公开联系人候选" if group else "未发现直接联系入口"
        rows.append(
            {
                "频道": f"@{job.normalized_username}",
                "频道标题": getattr(snapshot, "title", "") or "",
                "成员数": getattr(snapshot, "member_count", 0) or 0,
                "最后活跃（北京时间）": format_shanghai(getattr(snapshot, "last_activity_at", None)),
                "群组质量": quality["label"] if quality["visible"] else "",
                "广告分": quality["score_label"] if quality["visible"] else "",
                "质量依据": " | ".join(quality["reasons"]),
                "社群画像": ai_report.get("executive_summary", ""),
                "合作建议": ai_report.get("contact_and_conversion", ""),
                "内容策略": ai_report.get("content_strategy", ""),
                "负责人候选": group.get("value", "") if group else "",
                "候选角色": role,
                "来源": " / ".join(source_types),
                "原文证据": " | ".join(dict.fromkeys(str(item.evidence or "") for item in examples if item.evidence)),
                "证据链接": " ; ".join(dict.fromkeys(str(item.source_ref or "") for item in examples if str(item.source_ref or "").startswith("http"))),
                "置信度": group.get("confidence", "") if group else "",
                "跟进状态": group.get("lead_status", "new") if group else "new",
                "标签": "，".join(clean_tags(group.get("tags", []))) if group else "",
                "跟进备注": group.get("followup_note", "") if group else "",
                "备注": "公开合作线索；身份和联系授权需人工核验。",
            }
        )
    return rows


def build_audience_export_rows(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    job = bundle["job"]
    snapshot = bundle.get("snapshot")
    quality = group_quality_view(job.result_summary or {})
    rows: list[dict[str, Any]] = []
    for candidate in detail_view(bundle)["audience_candidates"]:
        item = candidate["item"]
        reason = f"在 {item.post_count} 篇帖子下发表 {item.comment_count} 条评论"
        if candidate["cross_channel_count"] > 1:
            reason += f"；出现在 {candidate['cross_channel_count']} 个已扫描频道"
        rows.append(
            {
                "username": item.username,
                "显示名称": item.display_name,
                "来源频道": f"@{job.normalized_username}",
                "频道标题": getattr(snapshot, "title", "") or "",
                "来源原帖": " ; ".join(candidate["source_posts"]),
                "群组质量": quality["label"] if quality["visible"] else "",
                "广告分": quality["score_label"] if quality["visible"] else "",
                "评论数": item.comment_count,
                "参与帖子数": item.post_count,
                "最后评论（北京时间）": format_shanghai(item.last_comment_at),
                "跨频道数": candidate["cross_channel_count"],
                "推荐等级": candidate["priority"],
                "跟进状态": candidate["lead_status"],
                "标签": "，".join(candidate["tags"]),
                "跟进备注": candidate["followup_note"],
                "筛选理由": reason,
                "画像范围": "仅根据公开互动频率和来源帖子主题；未分析评论正文。",
                "备注": "候选研究记录；不代表已获准联系。",
            }
        )
    return rows


def csv_response(rows: list[dict[str, Any]], fields: list[str], filename: str) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: csv_safe(row.get(field, "")) for field in fields})
    data = "\ufeff" + output.getvalue()
    return StreamingResponse(
        iter([data.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def chat_export_file_response(job: ChatExportJob, kind: str) -> FileResponse:
    files = dict(job.files or {})
    if kind not in {"csv", "jsonl", "metadata", "archive"} or not files.get(kind):
        raise HTTPException(status_code=404, detail="export_file_not_found")
    path = Path(str(files[kind])).expanduser()
    allowed_root = (BASE_DIR / "exports").resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(allowed_root) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="export_file_not_found")
    media_types = {
        "csv": "text/csv; charset=utf-8",
        "jsonl": "application/x-ndjson",
        "metadata": "application/json",
        "archive": "application/zip",
    }
    return FileResponse(resolved, media_type=media_types[kind], filename=resolved.name)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _job_json(job: ScanJob) -> dict[str, Any]:
    summary = job.result_summary or {}
    return {
        "id": job.public_id,
        "target": job.requested_target,
        "username": job.normalized_username,
        "created_by_username": job.created_by_username or "",
        "tags": clean_tags(job.tags or []),
        "auto_tags": clean_tags(summary.get("auto_tags", []) if isinstance(summary.get("auto_tags", []), list) else []),
        "status": job.status,
        "stage": job.stage,
        "progress": int(job.progress or 0),
        "collector_account": job.collector_account or "",
        "failover_reason": job.failover_reason or "",
        "ai_status": job.ai_status,
        "group_quality": group_quality_view(summary),
        "summary": summary,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "finished_at": _iso(job.finished_at),
    }


def _snapshot_json(snapshot: Any) -> dict[str, Any] | None:
    if not snapshot:
        return None
    fields = (
        "username", "title", "bio", "public_url", "linked_chat_title", "linked_chat_username",
        "member_count", "last_post_at", "last_comment_at", "last_activity_at", "last_activity_source",
        "posts_scanned", "pinned_scanned", "comments_fetched", "comments_truncated",
    )
    return {field: _iso(getattr(snapshot, field, None)) for field in fields}


def _site_card_json(card: dict[str, Any]) -> dict[str, Any]:
    summary = card["job"].result_summary or {}
    return {
        "job": _job_json(card["job"]),
        "channel": _snapshot_json(card.get("snapshot")),
        "direct_contact_count": int(card.get("direct_contact_count", 0) or 0),
        "comment_clue_count": int(card.get("comment_clue_count", 0) or 0),
        "comments_fetched": int(card.get("comments_fetched", 0) or 0),
        "has_comments": bool(card.get("has_comments")),
        "auto_tags": clean_tags(card.get("auto_tags", [])),
        "group_quality": group_quality_view(summary),
        "profile": str(card.get("profile", "") or ""),
        "language": card.get("language", {}),
        "engagement": card.get("engagement", {}),
        "v2_ready": bool(card.get("v2_ready")),
        "similar_channels_count": int(card.get("similar_channels_count", 0) or 0),
    }


def _site_bundle_json(bundle: dict[str, Any]) -> dict[str, Any]:
    view = detail_view(bundle)
    contacts = []
    for group in view["direct_contact_groups"] + view["other_contact_groups"]:
        contacts.append({
            "type": group.get("contact_type", ""),
            "value": group.get("value", ""),
            "entity_kind": group.get("entity_kind", "unknown"),
            "entity_title": group.get("entity_title", ""),
            "is_contactable": bool(group.get("is_contactable")),
            "classification_status": group.get("classification_status", "not_applicable"),
            "confidence": group.get("confidence", ""),
            "source_types": group.get("source_types", []),
            "tags": clean_tags(group.get("tags", [])),
            "lead_status": group.get("lead_status", "new"),
            "followup_note": group.get("followup_note", ""),
            "evidence_count": group.get("evidence_count", 0),
            "examples": [
                {"source_type": item.source_type, "source_ref": item.source_ref, "evidence": item.evidence}
                for item in group.get("examples", [])
            ],
        })
    audience = []
    for candidate in view["audience_candidates"]:
        item = candidate["item"]
        audience.append({
            "username": item.username,
            "display_name": item.display_name,
            "comment_count": int(item.comment_count or 0),
            "post_count": int(item.post_count or 0),
            "last_comment_at": _iso(item.last_comment_at),
            "source_posts": list(item.source_posts or []),
            "priority": candidate["priority"],
            "cross_channel_count": candidate["cross_channel_count"],
            "tags": candidate["tags"],
            "lead_status": candidate["lead_status"],
            "followup_note": candidate["followup_note"],
        })
    return {
        "job": _job_json(bundle["job"]),
        "channel": _snapshot_json(bundle.get("snapshot")),
        "group_quality": group_quality_view(bundle["job"].result_summary or {}),
        "contacts": contacts,
        "audience": audience,
        "pinned_messages": [
            {"source_ref": item.source_ref, "text": item.text, "message_date": _iso(item.message_date)}
            for item in bundle.get("pinned_messages", [])
        ],
        "recent_posts": [
            {"source_ref": item.source_ref, "text": item.text, "message_date": _iso(item.message_date)}
            for item in bundle.get("recent_posts", [])[:100]
        ],
    }


def build_routes():
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "channel-preprocessor", "version": "0.2.1"}

    @router.get("/ready")
    def ready() -> dict[str, Any]:
        try:
            with database.session() as session:
                session.execute(sql_text("SELECT 1"))
            database_ready = True
        except Exception:
            database_ready = False
        bot_enabled = str(os.getenv("BOT_ENABLED", "") or "").lower() in {"1", "true", "yes"}
        worker_enabled = str(os.getenv("TELEGRAM_WORKER_ENABLED", "") or "").lower() in {"1", "true", "yes"}
        ai_configured = bool(settings.ai_enabled and settings.ai_base_url and settings.ai_api_key and settings.ai_model)
        return {
            "ok": database_ready,
            "database": "ready" if database_ready else "unavailable",
            "bot_configured": bot_enabled or bool(settings.bot_token and settings.workgroup_chat_id),
            "telegram_credentials_configured": bool(settings.telegram_api_id and settings.telegram_api_hash) or worker_enabled,
            "telegram_passphrase_configured": bool(settings.telegram_database_passphrase) or worker_enabled,
            "ai_enabled": settings.ai_enabled,
            "ai_configured": ai_configured,
        }

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = ""):
        return templates.TemplateResponse(request, "login.html", {"settings": settings, "error": error})

    @router.post("/login")
    def login(username: str = Form(...), password: str = Form(...)):
        with database.session() as session:
            user = session.scalar(select(PlatformUser).where(PlatformUser.username == username.strip().lower()))
            if not user or not user.active or not verify_password(user.password_hash, password):
                return RedirectResponse("/login?error=invalid", status_code=303)
            raw, _record = create_login_session(session, user, settings)
            must_change_password = bool(user.must_change_password)
        response = RedirectResponse("/account/password?force=1" if must_change_password else "/", status_code=303)
        response.set_cookie(
            "preprocessor_session",
            raw,
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
            max_age=settings.session_days * 86400,
        )
        return response

    @router.post("/logout")
    def logout(request: Request, csrf: str = Form(...), auth=Depends(require_user_allow_password_change)):
        csrf_guard(auth[1], csrf)
        with database.session() as session:
            logout_session(session, request.cookies.get("preprocessor_session"))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("preprocessor_session")
        return response

    @router.get("/account/password", response_class=HTMLResponse)
    def password_page(
        request: Request,
        force: str = "",
        error: str = "",
        message: str = "",
        auth=Depends(require_user_allow_password_change),
    ):
        return templates.TemplateResponse(
            request,
            "account_password.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "force": bool(force) or bool(auth[0].must_change_password),
                "error": error,
                "message": message,
            },
        )

    @router.post("/account/password")
    def change_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        confirm_password: str = Form(...),
        csrf: str = Form(...),
        auth=Depends(require_user_allow_password_change),
    ):
        csrf_guard(auth[1], csrf)

        def error_response(message: str):
            return templates.TemplateResponse(
                request,
                "account_password.html",
                {
                    "settings": settings,
                    "user": auth[0],
                    "csrf": auth[1],
                    "force": bool(auth[0].must_change_password),
                    "error": message,
                    "message": "",
                },
                status_code=400,
            )

        if new_password != confirm_password:
            return error_response("两次输入的新密码不一致。")
        if len(new_password) < 10:
            return error_response("新密码至少需要 10 个字符。")
        with database.session() as session:
            user = session.get(PlatformUser, auth[0].id)
            if not user or not user.active:
                raise HTTPException(status_code=401, detail="login_required")
            if not verify_password(user.password_hash, current_password):
                return error_response("当前密码不正确。")
            if verify_password(user.password_hash, new_password):
                return error_response("新密码不能和当前密码相同。")
            user.password_hash = hash_password(new_password)
            user.must_change_password = False
            raw_session = request.cookies.get("preprocessor_session", "")
            current_session_hash = token_hash(raw_session) if raw_session else ""
            session.execute(
                delete(PlatformSession).where(
                    PlatformSession.user_id == user.id,
                    PlatformSession.token_hash != current_session_hash,
                )
            )
            repository.audit(
                session,
                "platform.user.password_changed",
                actor=actor_name(user),
                target=str(user.id),
                detail={"username": user.username},
            )
        return RedirectResponse("/?message=密码已更新", status_code=303)

    @router.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        tag: str = "",
        owner: str = "",
        message: str = "",
        error: str = "",
        auth=Depends(require_user),
    ):
        dashboard_data = repository.list_job_cards()
        all_cards = []
        for card in dashboard_data["cards"]:
            enriched = dict(card)
            enriched["group_quality_view"] = group_quality_view(enriched["job"].result_summary or {})
            all_cards.append(enriched)
        all_history_jobs = dashboard_data["history"]
        all_jobs = [card["job"] for card in all_cards] + all_history_jobs
        all_tags = sorted({item for job in all_jobs for item in clean_tags(job.tags or [])}, key=str.lower)
        all_owners = sorted({job.created_by_username for job in all_jobs if job.created_by_username}, key=str.lower)

        def matches(job: ScanJob) -> bool:
            if tag and tag not in clean_tags(job.tags or []):
                return False
            if owner and job.created_by_username != owner:
                return False
            return True

        cards = [card for card in all_cards if matches(card["job"])]
        history_jobs = [job for job in all_history_jobs if matches(job)]
        card_stats = {
            "channels": len(cards),
            "with_comments": sum(1 for card in cards if card["has_comments"]),
            "comment_clues": sum(int(card["comment_clue_count"] or 0) for card in cards),
            "direct_contacts": sum(int(card["direct_contact_count"] or 0) for card in cards),
            "pure_ad_groups": sum(1 for card in cards if card["group_quality_view"]["classification"] == "pure_ad_group"),
            "v2_ready": sum(1 for card in cards if card.get("v2_ready")),
            "v2_blocked": sum(1 for card in cards if card.get("upgrade_blocked")),
            "global_leads": len(repository.list_global_leads(limit=5000)),
        }
        discovery_data = repository.list_discovery_candidates(limit=5000)
        card_stats["discovery"] = discovery_data["stats"]["total"]
        card_stats["discovery_new"] = discovery_data["stats"]["new"]
        card_stats["needs_v2"] = max(0, card_stats["channels"] - card_stats["v2_ready"] - card_stats["v2_blocked"])
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "cards": cards,
                "history_jobs": history_jobs,
                "card_stats": card_stats,
                "all_tags": all_tags,
                "all_owners": all_owners,
                "tag_filter": tag,
                "owner_filter": owner,
                "message": message,
                "error": error,
                "can_force_scan": can_force_scan(auth[0]),
            },
        )

    @router.get("/leads", response_class=HTMLResponse)
    def leads_page(
        request: Request,
        q: str = "",
        status: str = "",
        kind: str = "",
        message: str = "",
        error: str = "",
        auth=Depends(require_user),
    ):
        all_leads = repository.list_global_leads()
        leads = filter_global_leads(all_leads, query=q, status=status, kind=kind)
        stats = {
            "total": len(all_leads),
            "visible": len(leads),
            "direct": sum(1 for item in all_leads if item["lead_kind"] in {"direct_contact", "both"}),
            "audience": sum(1 for item in all_leads if item["lead_kind"] in {"audience", "both"}),
            "multi_source": sum(1 for item in all_leads if int(item["source_count"]) > 1),
        }
        return templates.TemplateResponse(
            request,
            "leads.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "leads": leads,
                "stats": stats,
                "query": q,
                "status_filter": status,
                "kind_filter": kind,
                "lead_statuses": LEAD_STATUS_OPTIONS,
                "message": message,
                "error": error,
            },
        )

    @router.post("/leads/update")
    def update_global_lead(
        key: str = Form(...),
        tags: str = Form(""),
        lead_status: str = Form("new"),
        followup_note: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        try:
            count = repository.update_global_lead(
                key,
                tags=tags,
                lead_status=lead_status,
                followup_note=followup_note,
                actor=actor_name(auth[0]),
            )
        except ValueError as exc:
            return RedirectResponse(f"/leads?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/leads?message={quote(f'已同步更新 {count} 条线索记录')}", status_code=303)

    @router.get("/discover", response_class=HTMLResponse)
    def discovery_page(
        request: Request,
        q: str = "",
        state: str = "",
        message: str = "",
        error: str = "",
        auth=Depends(require_user),
    ):
        data = repository.list_discovery_candidates(limit=5000)
        needle = str(q or "").strip().lower().lstrip("@")
        candidates = []
        for item in data["candidates"]:
            if state and item["state"] != state:
                continue
            haystack = " ".join(
                [item["username"], item["title"], *(source["username"] for source in item["sources"])]
            ).lower()
            if needle and needle not in haystack:
                continue
            candidates.append(item)
        return templates.TemplateResponse(
            request,
            "discover.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "candidates": candidates,
                "stats": data["stats"],
                "query": q,
                "state_filter": state,
                "message": message,
                "error": error,
            },
        )

    @router.post("/discover/scan")
    def scan_discovery_candidates(
        usernames: list[str] = Form(default=[]),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        if settings.demo_mode:
            return RedirectResponse(f"/discover?error={quote('演示模式不会创建 Telegram 扫描任务')}", status_code=303)
        allowed = {item["username"]: item for item in repository.list_discovery_candidates(limit=5000)["candidates"]}
        selected = []
        for raw in usernames[:50]:
            username = str(raw or "").strip().lower().lstrip("@")
            if username in allowed and username not in selected:
                selected.append(username)
        if not selected:
            return RedirectResponse(f"/discover?error={quote('请选择至少一个待扫描频道')}", status_code=303)
        created = 0
        for username in selected:
            _job, cached = repository.create_scan_job(
                username,
                f"@{username}",
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=["频道发现"],
                actor=actor_name(auth[0]),
            )
            created += int(not cached)
        message = f"已提交 {len(selected)} 个发现目标，新建 {created} 个任务"
        return RedirectResponse(f"/discover?message={quote(message)}", status_code=303)

    @router.get("/leads/export.csv")
    def export_global_leads(q: str = "", status: str = "", kind: str = "", auth=Depends(require_user)):
        leads = filter_global_leads(repository.list_global_leads(limit=5000), query=q, status=status, kind=kind)
        fields = [
            "线索", "显示名称", "身份类型", "线索类型", "跟进状态", "标签", "来源频道数", "证据数",
            "互动数", "参与内容数", "最后出现", "来源频道", "跟进备注",
        ]
        rows = []
        for item in leads:
            rows.append(
                {
                    "线索": item["value"],
                    "显示名称": item["display_name"],
                    "身份类型": item["entity_kind"],
                    "线索类型": item["lead_kind"],
                    "跟进状态": item["lead_status"],
                    "标签": "，".join(item["tags"]),
                    "来源频道数": item["source_count"],
                    "证据数": item["evidence_count"],
                    "互动数": item["comment_count"],
                    "参与内容数": item["post_count"],
                    "最后出现": format_shanghai(item["latest_at"]),
                    "来源频道": "，".join(dict.fromkeys(f"@{source['username']}" for source in item["sources"])),
                    "跟进备注": item["followup_note"],
                }
            )
        return csv_response(rows, fields, "global-leads.csv")

    @router.get("/chat-exports", response_class=HTMLResponse)
    def chat_exports_page(
        request: Request,
        message: str = "",
        error: str = "",
        auth=Depends(require_user),
    ):
        jobs = repository.list_chat_export_jobs()
        stats = {
            "total": len(jobs),
            "active": sum(1 for job in jobs if job.status in {"queued", "exporting"}),
            "completed": sum(1 for job in jobs if job.status == "completed"),
            "failed": sum(1 for job in jobs if job.status == "failed"),
            "media": sum(1 for job in jobs if job.include_media),
        }
        return templates.TemplateResponse(
            request,
            "chat_exports.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "jobs": jobs,
                "stats": stats,
                "message": message,
                "error": error,
                "date_range_labels": EXPORT_DATE_RANGE_LABELS,
                "stop_reason_labels": EXPORT_STOP_REASON_LABELS,
                "chat_type_labels": EXPORT_CHAT_TYPE_LABELS,
            },
        )

    @router.post("/chat-exports")
    def create_chat_export(
        targets: str = Form(...),
        tags: str = Form(""),
        date_range: str = Form("recent_7"),
        since_date: str = Form(""),
        until_date: str = Form(""),
        max_messages_mode: str = Form("1000"),
        max_messages_custom: str = Form(""),
        include_media: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        return chat_export_submission_redirect(
            targets=targets,
            tags=tags,
            date_range=date_range,
            since_date=since_date,
            until_date=until_date,
            max_messages_mode=max_messages_mode,
            max_messages_custom=max_messages_custom,
            include_media=bool(include_media),
            user=auth[0],
        )

    @router.get("/chat-exports/{public_id}", response_class=HTMLResponse)
    def chat_export_detail(public_id: str, request: Request, auth=Depends(require_user)):
        bundle = repository.get_chat_export_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            request,
            "chat_export_job.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "date_range_labels": EXPORT_DATE_RANGE_LABELS,
                "stop_reason_labels": EXPORT_STOP_REASON_LABELS,
                "chat_type_labels": EXPORT_CHAT_TYPE_LABELS,
                **bundle,
            },
        )

    @router.post("/chat-exports/{public_id}/{action}")
    def chat_export_action(public_id: str, action: str, csrf: str = Form(...), auth=Depends(require_user)):
        csrf_guard(auth[1], csrf)
        if action not in {"retry", "cancel"}:
            raise HTTPException(status_code=404)
        try:
            repository.chat_export_lifecycle(public_id, action, actor=actor_name(auth[0]))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/chat-exports/{public_id}", status_code=303)

    @router.get("/chat-exports/{public_id}/download/{kind}")
    def chat_export_download(public_id: str, kind: str, auth=Depends(require_user)):
        job = repository.get_chat_export_job(public_id)
        if not job:
            raise HTTPException(status_code=404)
        return chat_export_file_response(job, kind)

    @router.post("/scans")
    def create_scan(
        target: str = Form(...),
        tags: str = Form(""),
        force: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        if force and not can_force_scan(auth[0]):
            raise HTTPException(status_code=403, detail="admin_required_for_force_scan")
        return scan_submission_redirect(target=target, tags=tags, force=bool(force), user=auth[0])

    @router.post("/scans/batch")
    def create_scan_batch(
        targets: str = Form(...),
        tags: str = Form(""),
        force: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        if force and not can_force_scan(auth[0]):
            raise HTTPException(status_code=403, detail="admin_required_for_force_scan")
        return scan_submission_redirect(target=targets, tags=tags, force=bool(force), user=auth[0])

    @router.get("/jobs/{public_id}", response_class=HTMLResponse)
    def job_detail(
        public_id: str,
        request: Request,
        error: str = "",
        message: str = "",
        auth=Depends(require_user),
    ):
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404)
        view = detail_view(bundle)
        return templates.TemplateResponse(
            request,
            "job.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "lead_statuses": LEAD_STATUS_OPTIONS,
                "group_quality": group_quality_view(bundle["job"].result_summary or {}),
                "language": (bundle["job"].result_summary or {}).get("language", {}),
                "engagement": (bundle["job"].result_summary or {}).get("engagement", {}),
                "similar_channels": (bundle["job"].result_summary or {}).get("similar_channels", []),
                "similar_channels_status": (bundle["job"].result_summary or {}).get("similar_channels_status", ""),
                "v2_ready": bool(
                    ((bundle["job"].result_summary or {}).get("language") or {}).get("code")
                    and ((bundle["job"].result_summary or {}).get("engagement") or {}).get("method")
                ),
                "can_force_scan": can_force_scan(auth[0]),
                "language_options": [(code, label) for code, label in LANGUAGE_LABELS.items() if code != "und"],
                "message": message,
                "error": error,
                **bundle,
                **view,
            },
        )

    @router.post("/jobs/{public_id}/v2/rescan")
    def rescan_for_v2(public_id: str, csrf: str = Form(...), auth=Depends(require_user)):
        csrf_guard(auth[1], csrf)
        if settings.demo_mode:
            return RedirectResponse(f"/jobs/{public_id}?error={quote('演示模式不会创建 Telegram 扫描任务')}", status_code=303)
        if not can_force_scan(auth[0]):
            raise HTTPException(status_code=403, detail="admin_required_for_force_scan")
        source = repository.get_job(public_id)
        if not source or source.normalized_username == "manual_review":
            raise HTTPException(status_code=404)
        tags = clean_tags([*(source.tags or []), "v2 重扫"])
        job, _cached = repository.create_scan_job(
            source.normalized_username,
            source.requested_target,
            created_by_user_id=auth[0].id,
            created_by_username=auth[0].username,
            tags=tags,
            force=True,
            actor=actor_name(auth[0]),
        )
        return RedirectResponse(f"/jobs/{job.public_id}", status_code=303)

    @router.post("/v2/rescan-pending")
    def rescan_all_pending_v2(csrf: str = Form(...), auth=Depends(require_user)):
        csrf_guard(auth[1], csrf)
        if settings.demo_mode:
            return RedirectResponse(f"/?error={quote('演示模式不会创建 Telegram 扫描任务')}", status_code=303)
        if not can_force_scan(auth[0]):
            raise HTTPException(status_code=403, detail="admin_required_for_force_scan")
        queued = 0
        skipped = 0
        for card in repository.list_job_cards(limit=500)["cards"]:
            source = card["job"]
            if card.get("v2_ready") or card.get("upgrade_blocked") or source.status != "completed" or source.normalized_username == "manual_review":
                skipped += 1
                continue
            _job, cached = repository.create_scan_job(
                source.normalized_username,
                source.requested_target,
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=clean_tags([*(source.tags or []), "v2 批量升级"]),
                force=True,
                actor=actor_name(auth[0]),
            )
            queued += int(not cached)
        message = f"已创建 {queued} 个 v2 升级任务"
        if skipped:
            message += f"，跳过 {skipped} 个无需升级或正在处理的目标"
        return RedirectResponse(f"/?message={quote(message)}", status_code=303)

    @router.post("/jobs/{public_id}/language")
    def update_job_language(
        public_id: str,
        language_code: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        code = str(language_code or "").strip().lower()
        if code and code not in LANGUAGE_LABELS:
            return RedirectResponse(f"/jobs/{public_id}?error={quote('不支持的语种代码')}", status_code=303)
        try:
            repository.update_language_override(
                public_id,
                code,
                LANGUAGE_LABELS.get(code, ""),
                actor=actor_name(auth[0]),
            )
        except ValueError as exc:
            return RedirectResponse(f"/jobs/{public_id}?error={quote(str(exc))}", status_code=303)
        message = "已更新主要语种" if code else "已恢复自动识别语种"
        return RedirectResponse(f"/jobs/{public_id}?message={quote(message)}", status_code=303)

    @router.post("/jobs/{public_id}/similar/scan")
    def scan_similar_channels(
        public_id: str,
        usernames: list[str] = Form(default=[]),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        if settings.demo_mode:
            return RedirectResponse(f"/jobs/{public_id}?error={quote('演示模式不会创建 Telegram 扫描任务')}", status_code=303)
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404)
        recommendations = (bundle["job"].result_summary or {}).get("similar_channels", [])
        allowed = {
            str(item.get("username", "")).strip().lower(): str(item.get("username", "")).strip()
            for item in recommendations
            if isinstance(item, dict) and str(item.get("username", "")).strip()
        }
        selected = []
        for raw in usernames[:20]:
            key = str(raw or "").strip().lower().lstrip("@")
            if key in allowed and key not in selected:
                selected.append(key)
        if not selected:
            return RedirectResponse(f"/jobs/{public_id}?error={quote('请选择至少一个相似频道')}", status_code=303)
        inherited_tags = clean_tags([*(bundle["job"].tags or []), "相似频道"])
        jobs = []
        for key in selected:
            username = allowed[key]
            job, cached = repository.create_scan_job(
                username,
                f"@{username}",
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=inherited_tags,
                actor=actor_name(auth[0]),
            )
            jobs.append((job, cached))
        created_count = sum(1 for _job, cached in jobs if not cached)
        message = f"已提交 {len(jobs)} 个相似频道，新建 {created_count} 个任务"
        return RedirectResponse(f"/?message={quote(message)}", status_code=303)

    @router.post("/jobs/{public_id}/tags")
    def update_job_tags(public_id: str, tags: str = Form(""), csrf: str = Form(...), auth=Depends(require_user)):
        csrf_guard(auth[1], csrf)
        repository.update_job_tags(public_id, tags, actor=actor_name(auth[0]))
        return RedirectResponse(f"/jobs/{public_id}", status_code=303)

    @router.post("/jobs/{public_id}/contact-leads")
    def update_contact_lead(
        public_id: str,
        contact_type: str = Form(...),
        value: str = Form(...),
        lead_status: str = Form("new"),
        tags: str = Form(""),
        followup_note: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        repository.update_contact_lead(
            public_id,
            contact_type,
            value,
            tags=tags,
            lead_status=lead_status,
            followup_note=followup_note,
            actor=actor_name(auth[0]),
        )
        return RedirectResponse(f"/jobs/{public_id}", status_code=303)

    @router.post("/jobs/{public_id}/commenters/{commenter_id}/lead")
    def update_commenter_lead(
        public_id: str,
        commenter_id: int,
        lead_status: str = Form("new"),
        tags: str = Form(""),
        followup_note: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_user),
    ):
        csrf_guard(auth[1], csrf)
        repository.update_commenter_lead(
            commenter_id,
            tags=tags,
            lead_status=lead_status,
            followup_note=followup_note,
            actor=actor_name(auth[0]),
        )
        return RedirectResponse(f"/jobs/{public_id}", status_code=303)

    @router.post("/jobs/{public_id}/{action}")
    def job_action(public_id: str, action: str, csrf: str = Form(...), auth=Depends(require_user)):
        csrf_guard(auth[1], csrf)
        if action not in {"pause", "resume", "retry", "cancel"}:
            raise HTTPException(status_code=404)
        repository.lifecycle(public_id, action, actor=f"platform:{auth[0].username}")
        return RedirectResponse(f"/jobs/{public_id}", status_code=303)

    @router.get("/jobs/{public_id}/export.csv")
    def export_csv(public_id: str, auth=Depends(require_user)):
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404)
        output = io.StringIO()
        fields = ["sender_type", "user_id", "sender_chat_id", "username", "display_name", "comment_count", "post_count", "last_comment_at", "missing_username_reason"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in bundle["commenters"]:
            writer.writerow({field: csv_safe(getattr(item, field, "") or "") for field in fields})
        data = "\ufeff" + output.getvalue()
        return StreamingResponse(iter([data.encode("utf-8")]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="commenters-{public_id}.csv"'})

    @router.get("/jobs/{public_id}/export-community.csv")
    def export_community_csv(public_id: str, auth=Depends(require_user)):
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404)
        fields = [
            "频道", "频道标题", "成员数", "最后活跃（北京时间）", "群组质量", "广告分", "质量依据", "社群画像", "合作建议", "内容策略",
            "负责人候选", "候选角色", "来源", "原文证据", "证据链接", "置信度", "跟进状态", "标签", "跟进备注", "备注",
        ]
        return csv_response(build_community_export_rows(bundle), fields, f"community-partnership-{public_id}.csv")

    @router.get("/jobs/{public_id}/export-audience.csv")
    def export_audience_csv(public_id: str, auth=Depends(require_user)):
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404)
        fields = [
            "username", "显示名称", "来源频道", "频道标题", "来源原帖", "群组质量", "广告分", "评论数", "参与帖子数",
            "最后评论（北京时间）", "跨频道数", "推荐等级", "跟进状态", "标签", "跟进备注", "筛选理由", "画像范围", "备注",
        ]
        return csv_response(build_audience_export_rows(bundle), fields, f"audience-candidates-{public_id}.csv")

    @router.get("/jobs/{public_id}/export.json")
    def export_json(public_id: str, auth=Depends(require_user)):
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404)
        job = bundle["job"]
        snapshot = bundle["snapshot"]
        payload = {
            "job": {"id": job.public_id, "username": job.normalized_username, "status": job.status, "summary": job.result_summary},
            "channel": ({column.name: getattr(snapshot, column.name) for column in snapshot.__table__.columns if column.name != "id"} if snapshot else None),
            "commenters": [{column.name: getattr(item, column.name) for column in item.__table__.columns if column.name not in {"id", "job_id"}} for item in bundle["commenters"]],
            "contacts": [{column.name: getattr(item, column.name) for column in item.__table__.columns if column.name not in {"id", "job_id"}} for item in bundle["contacts"]],
        }
        return Response(json.dumps(payload, ensure_ascii=False, default=str, indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="result-{public_id}.json"'})

    @router.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request, message: str = "", error: str = "", auth=Depends(require_super_admin)):
        with database.session() as session:
            users = list(session.scalars(select(PlatformUser).order_by(PlatformUser.id)))
        return templates.TemplateResponse(
            request,
            "users.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "users": users,
                "invite_url": "",
                "message": message,
                "error": error,
                "roles": ["staff", "admin", "super_admin"],
            },
        )

    @router.post("/admin/invites", response_class=HTMLResponse)
    def create_invite(request: Request, role: str = Form("staff"), csrf: str = Form(...), auth=Depends(require_super_admin)):
        csrf_guard(auth[1], csrf)
        raw_token, expires_at = repository.create_invite(
            auth[0].id,
            role,
            actor=actor_name(auth[0]),
        )
        with database.session() as session:
            users = list(session.scalars(select(PlatformUser).order_by(PlatformUser.id)))
        invite_url = f"{settings.public_base_url}/join/{raw_token}"
        return templates.TemplateResponse(
            request,
            "users.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "users": users,
                "invite_url": invite_url,
                "invite_expires_at": expires_at,
                "message": "",
                "error": "",
                "roles": ["staff", "admin", "super_admin"],
            },
        )

    @router.get("/join/{raw_token}", response_class=HTMLResponse)
    def join_page(raw_token: str, request: Request, error: str = ""):
        if not repository.get_valid_invite(raw_token):
            raise HTTPException(status_code=404, detail="invite_invalid")
        return templates.TemplateResponse(
            request,
            "join.html",
            {"settings": settings, "raw_token": raw_token, "error": error},
        )

    @router.post("/join/{raw_token}")
    def join(
        raw_token: str,
        request: Request,
        username: str = Form(...),
        display_name: str = Form(""),
        password: str = Form(...),
    ):
        clean_username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,64}", clean_username):
            return templates.TemplateResponse(
                request,
                "join.html",
                {"settings": settings, "raw_token": raw_token, "error": "用户名仅限 3–64 位小写字母、数字及 _.-"},
                status_code=400,
            )
        try:
            repository.claim_invite(raw_token, clean_username, hash_password(password), display_name.strip())
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "join.html",
                {"settings": settings, "raw_token": raw_token, "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse("/login", status_code=303)

    @router.post("/admin/users")
    def create_user(
        request: Request,
        username: str = Form(...),
        display_name: str = Form(""),
        role: str = Form("staff"),
        csrf: str = Form(...),
        auth=Depends(require_super_admin),
    ):
        csrf_guard(auth[1], csrf)
        clean_username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,64}", clean_username):
            return admin_users_redirect(error="用户名仅限 3-64 位小写字母、数字及 _.-")
        temporary_password = generate_temporary_password()
        try:
            repository.create_user(
                clean_username,
                hash_password(temporary_password),
                role,
                display_name.strip(),
                actor=actor_name(auth[0]),
                must_change_password=True,
            )
        except ValueError as exc:
            return admin_users_redirect(error=str(exc))
        with database.session() as session:
            users = list(session.scalars(select(PlatformUser).order_by(PlatformUser.id)))
        return templates.TemplateResponse(
            request,
            "users.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "users": users,
                "invite_url": "",
                "message": f"已创建账号 {clean_username}，请把一次性临时密码交给成员。",
                "error": "",
                "roles": ["staff", "admin", "super_admin"],
                "temporary_username": clean_username,
                "temporary_password": temporary_password,
            },
        )

    @router.post("/admin/users/{user_id}")
    def update_user(
        user_id: int,
        display_name: str = Form(""),
        role: str = Form("staff"),
        active: str = Form(""),
        csrf: str = Form(...),
        auth=Depends(require_super_admin),
    ):
        csrf_guard(auth[1], csrf)
        try:
            repository.update_user(
                user_id,
                display_name=display_name,
                role=role,
                active=bool(active),
                actor_user_id=auth[0].id,
                actor=actor_name(auth[0]),
            )
        except ValueError as exc:
            return admin_users_redirect(error=str(exc))
        return admin_users_redirect(message="账号已更新")

    @router.post("/admin/users/{user_id}/reset-password")
    def reset_user_password(
        request: Request,
        user_id: int,
        csrf: str = Form(...),
        auth=Depends(require_super_admin),
    ):
        csrf_guard(auth[1], csrf)
        temporary_password = generate_temporary_password()
        try:
            reset_user = repository.reset_user_password(
                user_id,
                hash_password(temporary_password),
                actor=actor_name(auth[0]),
                must_change_password=True,
            )
        except ValueError as exc:
            return admin_users_redirect(error=str(exc))
        with database.session() as session:
            users = list(session.scalars(select(PlatformUser).order_by(PlatformUser.id)))
        return templates.TemplateResponse(
            request,
            "users.html",
            {
                "settings": settings,
                "user": auth[0],
                "csrf": auth[1],
                "users": users,
                "invite_url": "",
                "message": f"{reset_user.username} 的密码已重置，原有登录会话已失效。",
                "error": "",
                "roles": ["staff", "admin", "super_admin"],
                "temporary_username": reset_user.username,
                "temporary_password": temporary_password,
            },
        )

    @router.post("/api/scans")
    def api_scan(payload: ScanRequest, x_csrf_token: str = Header(""), auth=Depends(require_user)):
        csrf_guard(auth[1], x_csrf_token)
        if settings.demo_mode:
            raise HTTPException(status_code=409, detail="demo_mode_read_only")
        if payload.force and not can_force_scan(auth[0]):
            raise HTTPException(status_code=403, detail="admin_required_for_force_scan")
        try:
            username = normalize_channel_target(payload.target)
            job, cached = repository.create_scan_job(
                username,
                payload.target,
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=payload.tags,
                force=payload.force,
                actor=actor_name(auth[0]),
            )
        except ManualTargetError as exc:
            job = repository.create_manual_review_job(
                payload.target,
                str(exc),
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=payload.tags,
                actor=actor_name(auth[0]),
            )
            cached = False
        return {"id": job.public_id, "status": job.status, "cached": cached, "detail_url": f"{settings.public_base_url}/jobs/{job.public_id}"}

    @router.get("/api/scans/{public_id}")
    def api_job(public_id: str, auth=Depends(require_user)):
        job = repository.get_job(public_id)
        if not job:
            raise HTTPException(status_code=404)
        return {"id": job.public_id, "username": job.normalized_username, "status": job.status, "stage": job.stage, "progress": job.progress, "summary": job.result_summary}

    @router.post("/api/site/session")
    def site_login(payload: SiteLoginRequest, _proxy=Depends(require_site_proxy)):
        with database.session() as session:
            user = session.scalar(select(PlatformUser).where(PlatformUser.username == payload.username.strip().lower()))
            if not user or not user.active or not verify_password(user.password_hash, payload.password):
                raise HTTPException(status_code=401, detail="invalid_credentials")
            raw, record = create_login_session(session, user, settings)
            username = user.username
            role = user.role
            display_name = user.display_name
            must_change_password = bool(user.must_change_password)
        return {
            "session_token": raw,
            "csrf_token": record.csrf_token,
            "user": {
                "username": username,
                "display_name": display_name,
                "role": role,
                "must_change_password": must_change_password,
            },
        }

    @router.delete("/api/site/session")
    def site_logout(x_csrf_token: str = Header(""), auth=Depends(require_site_user)):
        csrf_guard(auth[1], x_csrf_token)
        with database.session() as session:
            logout_session(session, auth[2])
        return {"ok": True}

    @router.get("/api/site/jobs")
    def site_jobs(auth=Depends(require_site_user)):
        data = repository.list_job_cards()
        return {
            "cards": [_site_card_json(card) for card in data["cards"]],
            "history": [_job_json(job) for job in data["history"]],
        }

    @router.post("/api/site/jobs")
    def site_create_job(
        payload: SiteJobRequest,
        x_csrf_token: str = Header(""),
        auth=Depends(require_site_user),
    ):
        csrf_guard(auth[1], x_csrf_token)
        if settings.demo_mode:
            raise HTTPException(status_code=409, detail="demo_mode_read_only")
        if payload.force and not can_force_scan(auth[0]):
            raise HTTPException(status_code=403, detail="admin_required_for_force_scan")
        try:
            username = normalize_channel_target(payload.target)
            job, cached = repository.create_scan_job(
                username,
                payload.target,
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=payload.tags,
                force=payload.force,
                actor=actor_name(auth[0]),
            )
        except ManualTargetError as exc:
            job = repository.create_manual_review_job(
                payload.target,
                str(exc),
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=payload.tags,
                actor=actor_name(auth[0]),
            )
            cached = False
        return {"job": _job_json(job), "cached": cached}

    @router.post("/api/site/jobs/preview")
    def site_preview_jobs(
        payload: SiteBatchJobRequest,
        x_csrf_token: str = Header(""),
        auth=Depends(require_site_user),
    ):
        csrf_guard(auth[1], x_csrf_token)
        parsed = parse_batch_channel_targets(payload.targets)
        items = []
        for item in parsed["targets"]:
            items.append({**item, **repository.inspect_scan_target(item["username"])})
        return {
            "items": items,
            "duplicates": parsed["duplicates"],
            "invalid": parsed["invalid"],
            "truncated": parsed["truncated"],
            "limit": parsed["limit"],
        }

    @router.post("/api/site/jobs/batch")
    def site_create_jobs_batch(
        payload: SiteBatchJobRequest,
        x_csrf_token: str = Header(""),
        auth=Depends(require_site_user),
    ):
        csrf_guard(auth[1], x_csrf_token)
        if settings.demo_mode:
            raise HTTPException(status_code=409, detail="demo_mode_read_only")
        if payload.force and not can_force_scan(auth[0]):
            raise HTTPException(status_code=403, detail="admin_required_for_force_scan")
        parsed = parse_batch_channel_targets(payload.targets)
        results = []
        for item in parsed["targets"]:
            before = repository.inspect_scan_target(item["username"])
            job, cached = repository.create_scan_job(
                item["username"],
                item["target"],
                created_by_user_id=auth[0].id,
                created_by_username=auth[0].username,
                tags=payload.tags,
                force=payload.force,
                actor=actor_name(auth[0]),
            )
            results.append(
                {
                    **item,
                    "job": _job_json(job),
                    "cached": cached,
                    "disposition": before["disposition"] if cached else (
                        "rescan_created" if before["seen_before"] else "created"
                    ),
                }
            )
        return {
            "results": results,
            "duplicates": parsed["duplicates"],
            "invalid": parsed["invalid"],
            "truncated": parsed["truncated"],
            "counts": {
                "accepted": len(results),
                "created": sum(1 for item in results if not item["cached"]),
                "reused": sum(1 for item in results if item["cached"]),
                "duplicates": len(parsed["duplicates"]),
                "invalid": len(parsed["invalid"]),
            },
        }

    @router.get("/api/site/jobs/{public_id}")
    def site_job_detail(public_id: str, auth=Depends(require_site_user)):
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="job_not_found")
        return _site_bundle_json(bundle)

    @router.post("/api/site/jobs/{public_id}/{action}")
    def site_job_action(
        public_id: str,
        action: str,
        x_csrf_token: str = Header(""),
        auth=Depends(require_site_user),
    ):
        csrf_guard(auth[1], x_csrf_token)
        if action not in {"pause", "resume", "retry", "cancel"}:
            raise HTTPException(status_code=404, detail="action_not_found")
        try:
            job = repository.lifecycle(public_id, action, actor=actor_name(auth[0]))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job": _job_json(job)}

    @router.put("/api/site/jobs/{public_id}/tags")
    def site_update_job_tags(
        public_id: str,
        payload: SiteJobTagsRequest,
        x_csrf_token: str = Header(""),
        auth=Depends(require_site_user),
    ):
        csrf_guard(auth[1], x_csrf_token)
        job = repository.update_job_tags(public_id, payload.tags, actor=actor_name(auth[0]))
        return {"job": _job_json(job)}

    @router.put("/api/site/jobs/{public_id}/contacts")
    def site_update_contact_lead(
        public_id: str,
        contact_type: str,
        value: str,
        payload: SiteLeadUpdateRequest,
        x_csrf_token: str = Header(""),
        auth=Depends(require_site_user),
    ):
        csrf_guard(auth[1], x_csrf_token)
        try:
            updated = repository.update_contact_lead(
                public_id,
                contact_type,
                value,
                tags=payload.tags,
                lead_status=payload.lead_status,
                followup_note=payload.followup_note,
                actor=actor_name(auth[0]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"updated": updated}

    @router.put("/api/site/jobs/{public_id}/commenters/{commenter_id}")
    def site_update_commenter_lead(
        public_id: str,
        commenter_id: int,
        payload: SiteLeadUpdateRequest,
        x_csrf_token: str = Header(""),
        auth=Depends(require_site_user),
    ):
        csrf_guard(auth[1], x_csrf_token)
        try:
            commenter = repository.update_commenter_lead(
                commenter_id,
                tags=payload.tags,
                lead_status=payload.lead_status,
                followup_note=payload.followup_note,
                actor=actor_name(auth[0]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"commenter_id": commenter.id, "job_id": public_id}

    @router.get("/api/site/jobs/{public_id}/exports/{kind}")
    def site_job_export(public_id: str, kind: str, auth=Depends(require_site_user)):
        bundle = repository.get_job_bundle(public_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="job_not_found")
        if kind == "community":
            fields = [
                "频道", "频道标题", "成员数", "最后活跃（北京时间）", "群组质量", "广告分", "质量依据", "社群画像", "合作建议", "内容策略",
                "负责人候选", "候选角色", "来源", "原文证据", "证据链接", "置信度", "跟进状态", "标签", "跟进备注", "备注",
            ]
            return csv_response(build_community_export_rows(bundle), fields, f"community-partnership-{public_id}.csv")
        if kind == "audience":
            fields = [
                "username", "显示名称", "来源频道", "频道标题", "来源原帖", "群组质量", "广告分", "评论数", "参与帖子数",
                "最后评论（北京时间）", "跨频道数", "推荐等级", "跟进状态", "标签", "跟进备注", "筛选理由", "画像范围", "备注",
            ]
            return csv_response(build_audience_export_rows(bundle), fields, f"audience-candidates-{public_id}.csv")
        raise HTTPException(status_code=404, detail="export_not_found")

    @router.get("/api/site/runtime")
    def site_runtime(auth=Depends(require_site_user)):
        runtimes = repository.collector_runtime()
        with database.session() as session:
            queued = int(session.scalar(select(func.count(ScanJob.id)).where(ScanJob.status.in_(ACTIVE_STATUSES))) or 0)
        def runtime_json(alias: str) -> dict[str, Any]:
            row = runtimes.get(alias)
            configured = bool(
                settings.telegram_database_passphrase if alias == "primary" else settings.telegram_standby_database_passphrase
            )
            return {
                "alias": alias,
                "configured": configured,
                "status": row.status if row else ("unknown" if configured else "not_configured"),
                "last_ready_at": _iso(row.last_ready_at) if row else None,
                "updated_at": _iso(row.updated_at) if row else None,
                "last_error_code": row.last_error_code if row else "",
            }
        return {
            "host": "online",
            "last_heartbeat_at": datetime.now().astimezone().isoformat(),
            "queue_count": queued,
            "collectors": [runtime_json("primary"), runtime_json("standby")],
        }

    return router


app = create_app()
