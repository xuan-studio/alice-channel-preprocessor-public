from __future__ import annotations

import os
import re
import sys
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ai_client import ChannelInsightAIClient, ContactAIClient, GroupQualityAIClient
from .config import BASE_DIR, Settings
from .extractors import extract_contacts, merge_contacts
from .insights import detect_language, engagement_metrics


def _load_tdlib_core():
    configured = str(os.getenv("TDLIB_CORE_PATH", "") or "").strip()
    candidates = [
        Path(configured) if configured else None,
        BASE_DIR.parent / "standalone-tdlib-cli",
        BASE_DIR / "vendor",
    ]
    for candidate in candidates:
        if candidate and (candidate / "tdlib_runtime.py").is_file():
            sys.path.insert(0, str(candidate.resolve()))
            import tdlib_runtime  # type: ignore

            return tdlib_runtime
    raise RuntimeError("找不到已验证的 tdlib_runtime.py；请设置 TDLIB_CORE_PATH。")


def _timestamp(value: Any) -> datetime | None:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        raw = 0
    return datetime.fromtimestamp(raw, timezone.utc) if raw else None


def _formatted_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text", "") or "")
    return ""


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    kind = str(content.get("@type", "") or "")
    if kind == "messageText":
        return _formatted_text(content.get("text"))
    return _formatted_text(content.get("caption"))


def is_service_message(message: dict[str, Any]) -> bool:
    kind = str(((message.get("content") or {}).get("@type", "")) or "")
    return kind.startswith("messageChat") or kind in {
        "messageForumTopicCreated",
        "messageForumTopicEdited",
        "messagePinMessage",
    }


def public_username(entity: dict[str, Any]) -> str:
    usernames = entity.get("usernames") or {}
    if isinstance(usernames, dict):
        active = usernames.get("active_usernames") or []
        if active:
            return str(active[0] or "")
    return str(entity.get("username", "") or "")


def telegram_contact_username(value: Any) -> str:
    clean = str(value or "").strip()
    if clean.startswith("@"):
        candidate = clean[1:]
    else:
        match = re.search(
            r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})(?:[/#?]|$)",
            clean,
            flags=re.I,
        )
        candidate = match.group(1) if match else ""
    return candidate.lower() if re.fullmatch(r"[A-Za-z0-9_]{5,32}", candidate) else ""


def _content_age_days(value: datetime | None, *, now: datetime | None = None) -> int | None:
    if not value:
        return None
    reference = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((reference - value).total_seconds() // 86400))


def _auto_tags(
    *,
    entity_type: str,
    last_activity_at: datetime | None,
    inactive_days: int,
    group_quality: dict[str, Any] | None = None,
) -> list[str]:
    tags = ["公开频道" if entity_type == "channel" else "公开群组"]
    age_days = _content_age_days(last_activity_at)
    if age_days is None:
        tags.extend(["无近期内容", "低优先级"])
    elif age_days > inactive_days:
        tags.extend(["疑似死群", "低优先级"])
    else:
        tags.append("近期活跃")
    quality = group_quality or {}
    classification = str(quality.get("classification", "") or "")
    if classification == "pure_ad_group":
        tags.extend(["纯广告群", "低价值"])
    elif classification == "active_discussion":
        tags.append("真实互动")
    elif classification == "mixed":
        tags.append("混合内容")
    return list(dict.fromkeys(tags))


@dataclass(frozen=True)
class CollectorAccountConfig:
    alias: str
    account_name: str
    account_dir: str
    database_passphrase: str
    proxy: dict[str, Any]


def collector_account_from_settings(settings: Settings, alias: str) -> CollectorAccountConfig:
    if alias == "standby":
        return CollectorAccountConfig(
            alias="standby",
            account_name=settings.telegram_standby_account_name,
            account_dir=settings.telegram_standby_account_dir,
            database_passphrase=settings.telegram_standby_database_passphrase,
            proxy={},
        )
    return CollectorAccountConfig(
        alias="primary",
        account_name=settings.telegram_account_name,
        account_dir=settings.telegram_account_dir,
        database_passphrase=settings.telegram_database_passphrase,
        proxy=(
            {
                "type": "socks5",
                "host": settings.telegram_proxy_host,
                "port": settings.telegram_proxy_port,
                "line_key": settings.telegram_proxy_line_key,
                "exit_fingerprint": settings.telegram_proxy_exit_fingerprint,
            }
            if settings.telegram_require_proxy
            else {}
        ),
    )


class ChannelPreprocessor:
    def __init__(self, settings: Settings, repository, account: CollectorAccountConfig | None = None):
        self.settings = settings
        self.repository = repository
        self.account = account or collector_account_from_settings(settings, "primary")
        self.core = _load_tdlib_core()

    def _credentials(self) -> dict[str, Any]:
        if not self.account.database_passphrase:
            raise RuntimeError(f"{self.account.alias} 采集账号缺少数据库加密口令。")
        api_id = self.settings.telegram_api_id
        api_hash = self.settings.telegram_api_hash
        if not api_id or not api_hash:
            raise RuntimeError("缺少正式 TELEGRAM_API_ID / TELEGRAM_API_HASH。")
        return {
            "api_id": api_id,
            "api_hash": api_hash,
            "database_encryption_key": self.core.derive_database_encryption_key(
                self.account.database_passphrase,
                self.account.account_name,
            ),
        }

    def _account_dir(self) -> Path:
        return (
            Path(self.account.account_dir).expanduser().resolve()
            if self.account.account_dir
            else BASE_DIR / "runtime" / "accounts" / self.account.account_name
        )

    def probe(self) -> bool:
        self._require_proxy_binding()
        with self.core.locked_account_session(
            account_name=self.account.account_name,
            account_dir=self._account_dir(),
            credentials=self._credentials(),
            library_path=self.settings.tdlib_library or None,
            proxy=self.account.proxy,
        ) as session:
            ready = (session.bootstrap() or {}).get("@type") == "authorizationStateReady"
            if ready:
                self._verify_current_session_exit(session)
            return ready

    def run(self, public_job_id: str, username: str) -> dict[str, Any]:
        self._require_proxy_binding()
        with self.core.locked_account_session(
            account_name=self.account.account_name,
            account_dir=self._account_dir(),
            credentials=self._credentials(),
            library_path=self.settings.tdlib_library or None,
            proxy=self.account.proxy,
        ) as session:
            state = session.bootstrap()
            if state.get("@type") != "authorizationStateReady":
                raise RuntimeError("采集账号尚未在此服务完成登录。")
            self._verify_current_session_exit(session)
            return self._run_session(session, public_job_id, username)

    def _require_proxy_binding(self) -> None:
        if self.settings.telegram_require_proxy and not self.account.proxy:
            raise RuntimeError("采集账号缺少固定代理绑定；已冻结，禁止直连。")

    def _verify_current_session_exit(self, session) -> None:
        expected = str(self.account.proxy.get("exit_fingerprint", "") or "").strip().lower()
        if not expected:
            return
        result = session.invoke("getActiveSessions")
        current = next((item for item in list(result.get("sessions") or []) if bool(item.get("is_current"))), None)
        raw_ip = str((current or {}).get("ip_address", "") or "").strip()
        actual = hashlib.sha256(raw_ip.encode("utf-8")).hexdigest()[: len(expected)] if raw_ip else ""
        if not actual or actual != expected:
            raise RuntimeError("Telegram 当前授权会话出口与绑定节点不一致；账号已冻结，禁止继续。")

    def _run_session(self, session, public_job_id: str, username: str) -> dict[str, Any]:
        self.repository.assert_runnable(public_job_id)
        chat = session.invoke("searchPublicChat", username=username)
        chat_id = int(chat.get("id", 0) or 0)
        chat_type = chat.get("type") or {}
        if not chat_id or chat_type.get("@type") != "chatTypeSupergroup":
            raise ValueError("目标不是可处理的公开频道或群组。")
        is_channel = bool(chat_type.get("is_channel", False))
        entity_type = "channel" if is_channel else "public_group"
        supergroup_id = int(chat_type.get("supergroup_id", 0) or 0)
        supergroup = session.invoke("getSupergroup", supergroup_id=supergroup_id)
        if public_username(supergroup).lower() != username.lower():
            raise ValueError("频道公开 username 与请求不一致。")
        status_type = str((supergroup.get("status") or {}).get("@type", "") or "")
        if status_type == "chatMemberStatusBanned":
            raise ValueError("采集账号已被该频道封禁。")

        joined = False
        if status_type == "chatMemberStatusLeft":
            self.repository.assert_runnable(public_job_id)
            if self.repository.joins_today() >= self.settings.max_new_joins_per_day:
                raise JoinDailyLimitError("达到每日公开频道自动加入上限。")
            self.repository.transition(public_job_id, "joining", 12, {"username": username})
            join_result = session.invoke("joinChat", chat_id=chat_id)
            if join_result.get("@type") != "chatJoinResultSuccess":
                raise ManualReviewError(f"频道加入未立即完成：{join_result.get('@type', 'unknown')}")
            joined = True
            self.repository.mark_joined(public_job_id)

        self.repository.assert_runnable(public_job_id)
        self.repository.transition(public_job_id, "metadata", 20)
        full_info = session.invoke("getSupergroupFullInfo", supergroup_id=supergroup_id)
        bio = str(full_info.get("description", "") or "")
        linked_chat = self._linked_chat(session, int(full_info.get("linked_chat_id", 0) or 0))
        pinned = self._pinned_messages(session, chat_id)
        similar_channels, similar_channels_status = (
            self._similar_channels(session, chat_id, username) if is_channel else ([], "not_applicable")
        )

        self.repository.assert_runnable(public_job_id)
        self.repository.transition(public_job_id, "posts", 35)
        history_limit = self.settings.max_posts if is_channel else self.settings.max_group_messages
        posts = self._history(session, chat_id, history_limit, channel_only=is_channel)
        post_records: list[dict[str, Any]] = []
        raw_artifacts: list[dict[str, Any]] = []
        sources: list[dict[str, str]] = []
        high_value_sources: list[dict[str, str]] = []
        if bio:
            raw_artifacts.append({"source_type": "bio", "source_ref": "bio", "text": bio, "message_date": None})
            source = {"source_type": "bio", "source_ref": "bio", "text": bio}
            sources.append(source)
            high_value_sources.append(source)
        for index, item in enumerate(pinned):
            text = message_text(item)
            link = self._message_link(session, chat_id, int(item.get("id", 0) or 0), username)
            if text:
                raw_artifacts.append({"source_type": "pinned", "source_ref": link, "text": text, "message_date": _timestamp(item.get("date"))})
                source = {"source_type": "pinned", "source_ref": link, "text": text}
                sources.append(source)
                high_value_sources.append(source)
        for item in posts:
            message_id = int(item.get("id", 0) or 0)
            link = self._message_link(session, chat_id, message_id, username) if is_channel else self._fallback_message_link(username, message_id)
            text = message_text(item)
            post_records.append({"message": item, "message_id": message_id, "link": link, "text": text})
            if text:
                raw_artifacts.append({"source_type": "post", "source_ref": link, "text": text, "message_date": _timestamp(item.get("date"))})
                sources.append({"source_type": "post", "source_ref": link, "text": text})

        last_post_at = max((_timestamp(item.get("date")) for item in posts), default=None)
        language = detect_language([bio, *(message_text(item) for item in pinned), *(record["text"] for record in post_records)])
        engagement = engagement_metrics([record["message"] for record in post_records])
        content_age_days = _content_age_days(last_post_at)
        inactive = content_age_days is None or content_age_days > self.settings.inactive_days
        group_quality: dict[str, Any] = {"classification": "not_applicable", "is_pure_ad_group": False}
        group_quality_status = "skipped"
        group_quality_errors: list[str] = []
        if not is_channel and not inactive:
            quality_messages = [
                {"source_ref": record["link"], "text": record["text"]}
                for record in post_records[: self.settings.group_ad_sample_messages]
                if record["text"]
            ]
            group_quality, group_quality_status, group_quality_errors = GroupQualityAIClient(self.settings).analyze(
                quality_messages,
                {
                    "group": f"@{username}",
                    "title": str(chat.get("title", "") or ""),
                    "bio": bio,
                    "member_count": int(supergroup.get("member_count", 0) or 0),
                    "messages_scanned": len(posts),
                    "sampled_messages": len(quality_messages),
                    "last_message_at": last_post_at.isoformat() if last_post_at else "",
                },
            )
        pure_ad_group = bool(group_quality.get("is_pure_ad_group", False))

        self.repository.assert_runnable(public_job_id)
        self.repository.transition(public_job_id, "comments", 55)
        if inactive:
            commenters = []
            comment_summary = {
                "comments_fetched": 0,
                "truncated": False,
                "unavailable_threads": 0,
                "last_comment_at": None,
                "skip_reason": "inactive",
            }
        elif pure_ad_group:
            commenters = []
            comment_summary = {
                "comments_fetched": len(posts),
                "truncated": len(posts) >= history_limit,
                "unavailable_threads": 0,
                "last_comment_at": last_post_at,
                "skip_reason": "pure_ad_group",
            }
        elif is_channel:
            commenters, comment_summary = self._commenters(session, chat_id, post_records, public_job_id)
        else:
            commenters, comment_summary = self._message_senders(
                session,
                post_records,
                public_job_id,
                candidate_limit=self.settings.group_active_user_limit,
                history_limit=history_limit,
            )

        self.repository.assert_runnable(public_job_id)
        self.repository.transition(public_job_id, "contact_analysis", 80)
        contact_sources = high_value_sources if inactive or pure_ad_group else sources
        rule_contacts = []
        for source in contact_sources:
            rule_contacts.extend(
                extract_contacts(source["text"], source_type=source["source_type"], source_ref=source["source_ref"])
            )
        ai_sources = contact_sources[: self.settings.ai_max_sources]
        ai_limit_warnings = []
        if len(contact_sources) > len(ai_sources):
            ai_limit_warnings.append(f"AI 仅分析前 {len(ai_sources)} 条公开内容；规则提取已扫描全部 {len(contact_sources)} 条。")
        if inactive:
            ai_limit_warnings.append(f"最后活跃超过 {self.settings.inactive_days} 天，已跳过评论/群用户深挖。")
        if pure_ad_group:
            ai_limit_warnings.append("AI 判定疑似纯广告群，已跳过活跃用户沉淀。")
        if is_channel and similar_channels_status == "unavailable":
            ai_limit_warnings.append("当前 TDLib 或 Telegram 账号未返回相似频道，已跳过相关推荐。")
        self.repository.transition(
            public_job_id,
            "contact_analysis",
            82,
            {"phase": "ai_contacts", "ai_sources": len(ai_sources), "total_sources": len(contact_sources)},
        )
        ai_contacts, ai_status, ai_errors = ContactAIClient(self.settings).analyze(ai_sources, chunk_size=20)
        contacts = self._classify_contacts(session, merge_contacts([*rule_contacts, *ai_contacts]))

        last_comment_at = comment_summary["last_comment_at"]
        activity_candidates = [(last_post_at, "post" if is_channel else "message"), (last_comment_at, "comment")]
        activity_candidates = [(date, source) for date, source in activity_candidates if date]
        last_activity_at, last_activity_source = max(activity_candidates, default=(None, ""), key=lambda item: item[0])
        channel_payload = {
            "telegram_chat_id": str(chat_id),
            "username": username,
            "title": str(chat.get("title", "") or ""),
            "bio": bio,
            "public_url": f"https://t.me/{username}",
            "linked_chat_id": str(linked_chat.get("chat_id", "") or ""),
            "linked_chat_title": str(linked_chat.get("title", "") or ""),
            "linked_chat_username": str(linked_chat.get("username", "") or ""),
            "member_count": int(supergroup.get("member_count", 0) or 0),
            "last_post_at": last_post_at,
            "last_comment_at": last_comment_at,
            "last_activity_at": last_activity_at,
            "last_activity_source": last_activity_source,
            "posts_scanned": len(posts),
            "pinned_scanned": len(pinned),
            "comments_fetched": int(comment_summary["comments_fetched"]),
            "comments_truncated": bool(comment_summary["truncated"]),
        }
        insight_context = {
            "channel": f"@{username}",
            "entity_type": entity_type,
            "title": channel_payload["title"],
            "bio": bio,
            "member_count": channel_payload["member_count"],
            "last_activity_at": last_activity_at.isoformat() if last_activity_at else "",
            "last_activity_source": last_activity_source or ("post" if is_channel else "message"),
            "posts_scanned": len(posts),
            "pinned_scanned": len(pinned),
            "comments_fetched": channel_payload["comments_fetched"],
            "unique_comment_senders": len(commenters),
            "public_comment_usernames": sum(
                1 for item in commenters if item["sender_type"] == "user" and item["username"]
            ),
            "public_contacts": sorted({item["value"] for item in contacts}),
            "linked_discussion": linked_chat.get("username") or linked_chat.get("title") or "",
            "language": language,
            "engagement": engagement,
        }
        self.repository.transition(
            public_job_id,
            "contact_analysis",
            88,
            {"phase": "ai_summary", "ai_sources": len(ai_sources), "total_sources": len(contact_sources)},
        )
        if inactive:
            ai_report = self._inactive_report(insight_context, content_age_days)
            insight_status = "skipped"
            insight_errors = []
        elif pure_ad_group:
            ai_report = self._pure_ad_group_report(insight_context, group_quality)
            insight_status = group_quality_status
            insight_errors = group_quality_errors
        else:
            ai_report, insight_status, insight_errors = ChannelInsightAIClient(self.settings).analyze(
                ai_sources,
                insight_context,
                chunk_size=20,
            )
            if not is_channel and group_quality_status == "degraded":
                insight_errors = [*group_quality_errors, *insight_errors]
        if inactive:
            combined_ai_status = "skipped"
        elif ai_status == "completed" and insight_status == "completed":
            combined_ai_status = "completed"
        else:
            combined_ai_status = "degraded"
        auto_tags = _auto_tags(
            entity_type=entity_type,
            last_activity_at=last_activity_at,
            inactive_days=self.settings.inactive_days,
            group_quality=group_quality,
        )
        summary = {
            "channel_title": channel_payload["title"],
            "posts_scanned": len(posts),
            "pinned_scanned": len(pinned),
            "comments_fetched": channel_payload["comments_fetched"],
            "unique_user_commenters": sum(1 for item in commenters if item["sender_type"] == "user"),
            "public_usernames": sum(1 for item in commenters if item["sender_type"] == "user" and item["username"]),
            "contacts_found": len(contacts),
            "contactable_users": len(
                {
                    telegram_contact_username(item.get("value"))
                    for item in contacts
                    if item.get("is_contactable") and telegram_contact_username(item.get("value"))
                }
            ),
            "excluded_contact_entities": len(
                {
                    telegram_contact_username(item.get("value"))
                    for item in contacts
                    if item.get("entity_kind") in {"bot", "channel", "group"}
                    and telegram_contact_username(item.get("value"))
                }
            ),
            "comments_truncated": channel_payload["comments_truncated"],
            "comment_threads_unavailable": int(comment_summary.get("unavailable_threads", 0) or 0),
            "last_activity_at": last_activity_at.isoformat() if last_activity_at else "",
            "last_activity_source": last_activity_source or ("post" if is_channel else "message"),
            "entity_type": entity_type,
            "auto_tags": auto_tags,
            "content_age_days": content_age_days,
            "inactive_days": self.settings.inactive_days,
            "skip_reason": str(comment_summary.get("skip_reason", "") or ""),
            "group_quality": group_quality if not is_channel else {},
            "group_messages_scanned": len(posts) if not is_channel else 0,
            "group_active_user_limit": self.settings.group_active_user_limit if not is_channel else 0,
            "language": language,
            "engagement": engagement,
            "similar_channels": similar_channels,
            "similar_channels_status": similar_channels_status,
            "ai_status": combined_ai_status,
            "ai_report": ai_report or {},
            "warnings": [*ai_limit_warnings, *ai_errors, *insight_errors],
        }
        return {
            "channel": channel_payload,
            "commenters": commenters,
            "contacts": contacts,
            "raw_artifacts": raw_artifacts,
            "summary": summary,
            "ai_status": combined_ai_status,
            "joined_during_job": joined,
        }

    def _classify_contacts(self, session, contacts: list[dict[str, Any]], *, limit: int = 40) -> list[dict[str, Any]]:
        classified: dict[str, dict[str, Any]] = {}
        for item in contacts:
            if len(classified) >= max(1, min(limit, 100)):
                break
            if str(item.get("contact_type", "")) not in {"telegram", "telegram_username", "admin_dm", "bot"}:
                continue
            username = telegram_contact_username(item.get("value"))
            if not username or username in classified:
                continue
            result = {
                "entity_kind": "unknown",
                "entity_title": "",
                "is_contactable": False,
                "classification_status": "unavailable",
            }
            try:
                chat = session.invoke("searchPublicChat", username=username)
                chat_type = chat.get("type") or {}
                type_name = str(chat_type.get("@type", "") or "")
                result["entity_title"] = str(chat.get("title", "") or "")[:256]
                if type_name == "chatTypePrivate":
                    user = session.invoke("getUser", user_id=int(chat_type.get("user_id", 0) or 0))
                    is_bot = str((user.get("type") or {}).get("@type", "")) == "userTypeBot"
                    result["entity_kind"] = "bot" if is_bot else "user"
                    result["is_contactable"] = not is_bot
                elif type_name == "chatTypeSupergroup":
                    result["entity_kind"] = "channel" if bool(chat_type.get("is_channel", False)) else "group"
                elif type_name == "chatTypeBasicGroup":
                    result["entity_kind"] = "group"
                result["classification_status"] = "completed"
            except Exception:
                pass
            classified[username] = result

        enriched = []
        for item in contacts:
            row = dict(item)
            username = telegram_contact_username(row.get("value"))
            if username and username in classified:
                row.update(classified[username])
            else:
                row.update(
                    {
                        "entity_kind": "unknown",
                        "entity_title": "",
                        "is_contactable": False,
                        "classification_status": "not_applicable" if not username else "limit_reached",
                    }
                )
            enriched.append(row)
        return enriched

    def _inactive_report(self, context: dict[str, Any], age_days: int | None) -> dict[str, Any]:
        age_text = "没有可见近期内容" if age_days is None else f"最后可见内容距今约 {age_days} 天"
        return {
            "executive_summary": f"{context.get('channel', '')} {age_text}，已按低优先级处理，未继续深挖互动用户。",
            "channel_positioning": "仅保留 BIO、置顶和最近可见内容中的公开信息。",
            "activity_assessment": f"超过当前阈值 {self.settings.inactive_days} 天，疑似死群或停止更新。",
            "contact_and_conversion": "如 BIO 或置顶存在公开联系人，可人工核验；否则不建议投入触达。",
            "audience_and_comments": "已跳过评论/群消息用户拓展。",
            "content_strategy": "活跃度不足，不生成内容策略判断。",
            "risk_notes": ["低活跃目标可能无法带来有效合作或用户转化。"],
            "signals": [],
            "model": "local-rule",
        }

    def _pure_ad_group_report(self, context: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
        reasons = [str(item) for item in quality.get("reasons", []) if str(item)]
        return {
            "executive_summary": f"{context.get('channel', '')} 疑似纯广告群，已按低价值目标处理，跳过活跃用户沉淀。",
            "channel_positioning": "群内样本以广告、推广、链接或重复模板为主，不适合作为真实用户拓展池。",
            "activity_assessment": "可见内容仍有更新，但互动质量不足。",
            "contact_and_conversion": "仅建议核验 BIO/置顶中的公开商务入口；不建议批量触达群内发言用户。",
            "audience_and_comments": "已跳过 50 个 active 用户提取，避免把广告账号沉淀为线索。",
            "content_strategy": "广告主导，缺少可用于判断真实需求的对话上下文。",
            "risk_notes": reasons[:8] or ["疑似纯广告群，用户拓展价值低。"],
            "signals": [],
            "model": self.settings.ai_model if quality.get("method") == "ai" else "local-rule",
        }

    def _linked_chat(self, session, chat_id: int) -> dict[str, Any]:
        if not chat_id:
            return {}
        try:
            chat = session.invoke("getChat", chat_id=chat_id)
            chat_type = chat.get("type") or {}
            result = {"chat_id": chat_id, "title": str(chat.get("title", "") or ""), "username": ""}
            if chat_type.get("@type") == "chatTypeSupergroup":
                supergroup = session.invoke("getSupergroup", supergroup_id=int(chat_type.get("supergroup_id", 0) or 0))
                result["username"] = public_username(supergroup)
            return result
        except Exception:
            return {"chat_id": chat_id}

    def _similar_channels(
        self,
        session,
        chat_id: int,
        source_username: str,
        *,
        limit: int = 12,
    ) -> tuple[list[dict[str, Any]], str]:
        try:
            result = session.invoke("getChatSimilarChats", chat_id=chat_id)
        except Exception:
            return [], "unavailable"
        recommendations: list[dict[str, Any]] = []
        seen: set[str] = {source_username.lower()}
        for similar_chat_id in list(result.get("chat_ids") or []):
            if len(recommendations) >= max(1, min(limit, 20)):
                break
            try:
                similar_chat = session.invoke("getChat", chat_id=int(similar_chat_id))
                chat_type = similar_chat.get("type") or {}
                if chat_type.get("@type") != "chatTypeSupergroup" or not bool(chat_type.get("is_channel", False)):
                    continue
                similar_supergroup = session.invoke(
                    "getSupergroup",
                    supergroup_id=int(chat_type.get("supergroup_id", 0) or 0),
                )
                similar_username = public_username(similar_supergroup).strip()
                key = similar_username.lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                engagement = self._similar_channel_engagement(session, int(similar_chat_id))
                recommendations.append(
                    {
                        "rank": len(recommendations) + 1,
                        "telegram_chat_id": str(similar_chat_id),
                        "username": similar_username,
                        "title": str(similar_chat.get("title", "") or ""),
                        "member_count": int(similar_supergroup.get("member_count", 0) or 0),
                        "public_url": f"https://t.me/{similar_username}",
                        **engagement,
                    }
                )
            except Exception:
                continue
        return recommendations, "completed"

    def _similar_channel_engagement(self, session, chat_id: int) -> dict[str, Any]:
        try:
            result = session.invoke(
                "getChatHistory",
                chat_id=chat_id,
                from_message_id=0,
                offset=0,
                limit=3,
                only_local=False,
            )
            messages = [item for item in list(result.get("messages") or []) if not is_service_message(item)][:3]
            metrics = engagement_metrics(messages)
            return {
                "avg_views": metrics["avg_views"],
                "avg_reactions": metrics["avg_reactions"],
                "engagement_posts": len(messages),
                "engagement_status": "completed",
            }
        except Exception:
            return {
                "avg_views": 0,
                "avg_reactions": 0.0,
                "engagement_posts": 0,
                "engagement_status": "unavailable",
            }

    def _pinned_messages(self, session, chat_id: int) -> list[dict[str, Any]]:
        try:
            result = session.invoke(
                "searchChatMessages",
                chat_id=chat_id,
                topic_id=None,
                query="",
                sender_id=None,
                from_message_id=0,
                offset=0,
                limit=self.settings.max_pinned,
                filter={"@type": "searchMessagesFilterPinned"},
            )
            return [item for item in (result.get("messages") or []) if isinstance(item, dict)][: self.settings.max_pinned]
        except Exception:
            try:
                return [session.invoke("getChatPinnedMessage", chat_id=chat_id)]
            except Exception:
                return []

    def _history(self, session, chat_id: int, limit: int, *, channel_only: bool = True) -> list[dict[str, Any]]:
        posts: list[dict[str, Any]] = []
        seen: set[int] = set()
        from_message_id = 0
        max_pages = max(1, (max(1, int(limit)) + 99) // 100 + 5)
        for _ in range(max_pages):
            if len(posts) >= limit:
                break
            result = session.invoke(
                "getChatHistory",
                chat_id=chat_id,
                from_message_id=from_message_id,
                offset=0,
                limit=min(100, limit - len(posts)),
                only_local=False,
            )
            batch = [item for item in (result.get("messages") or []) if isinstance(item, dict)]
            if not batch:
                break
            new_count = 0
            for item in batch:
                message_id = int(item.get("id", 0) or 0)
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                new_count += 1
                is_channel_post = bool(item.get("is_channel_post", False))
                if not is_service_message(item) and (is_channel_post if channel_only else not is_channel_post):
                    posts.append(item)
                    if len(posts) >= limit:
                        break
            next_from = min(int(item.get("id", 0) or 0) for item in batch if int(item.get("id", 0) or 0))
            if not new_count or next_from == from_message_id:
                break
            from_message_id = next_from
        return posts[:limit]

    def _message_link(self, session, chat_id: int, message_id: int, username: str) -> str:
        try:
            result = session.invoke(
                "getMessageLink",
                chat_id=chat_id,
                message_id=message_id,
                media_timestamp=0,
                checklist_task_id=0,
                poll_option_id="",
                for_album=False,
                in_message_thread=False,
            )
            if result.get("link"):
                return str(result["link"])
        except Exception:
            pass
        return f"https://t.me/{username}/{message_id // 1048576}"

    def _fallback_message_link(self, username: str, message_id: int) -> str:
        return f"https://t.me/{username}/{message_id // 1048576}"

    def _sender(self, session, sender: dict[str, Any]) -> dict[str, Any]:
        kind = str(sender.get("@type", "") or "")
        if kind == "messageSenderUser":
            user_id = int(sender.get("user_id", 0) or 0)
            try:
                user = session.invoke("getUser", user_id=user_id)
                username = public_username(user)
                display_name = " ".join(
                    part for part in (str(user.get("first_name", "") or ""), str(user.get("last_name", "") or "")) if part
                ).strip()
                return {
                    "sender_type": "user",
                    "sender_key": str(user_id),
                    "user_id": str(user_id),
                    "sender_chat_id": "",
                    "username": username,
                    "display_name": display_name,
                    "missing_username_reason": "" if username else "no_public_username",
                }
            except Exception:
                return {"sender_type": "user", "sender_key": str(user_id), "user_id": str(user_id), "sender_chat_id": "", "username": "", "display_name": "", "missing_username_reason": "user_unavailable"}
        if kind == "messageSenderChat":
            sender_chat_id = int(sender.get("chat_id", 0) or 0)
            title = ""
            username = ""
            try:
                chat = session.invoke("getChat", chat_id=sender_chat_id)
                title = str(chat.get("title", "") or "")
                chat_type = chat.get("type") or {}
                if chat_type.get("@type") == "chatTypeSupergroup":
                    username = public_username(session.invoke("getSupergroup", supergroup_id=int(chat_type.get("supergroup_id", 0) or 0)))
            except Exception:
                pass
            return {"sender_type": "chat", "sender_key": str(sender_chat_id), "user_id": "", "sender_chat_id": str(sender_chat_id), "username": username, "display_name": title, "missing_username_reason": "" if username else "anonymous_or_chat_identity"}
        return {"sender_type": "unknown", "sender_key": "0", "user_id": "", "sender_chat_id": "", "username": "", "display_name": "", "missing_username_reason": "missing_sender"}

    def _message_senders(
        self,
        session,
        message_records: list[dict[str, Any]],
        public_job_id: str = "",
        *,
        candidate_limit: int | None = None,
        history_limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        fetched_total = 0
        last_comment_at: datetime | None = None
        for record in message_records:
            if public_job_id:
                self.repository.assert_runnable(public_job_id)
            item = record["message"]
            if is_service_message(item):
                continue
            sender = item.get("sender_id") or {}
            sender_kind = str(sender.get("@type", "") or "")
            sender_number = int(sender.get("user_id", sender.get("chat_id", 0)) or 0)
            if not sender_kind or not sender_number:
                continue
            fetched_total += 1
            key = (sender_kind, str(sender_number))
            if key not in aggregates:
                aggregates[key] = {
                    "sender": sender,
                    "comment_count": 0,
                    "source_posts": set(),
                    "last_comment_at": None,
                }
            at = _timestamp(item.get("date"))
            aggregate = aggregates[key]
            aggregate["comment_count"] += 1
            aggregate["source_posts"].add(str(record["link"]))
            if at and (not aggregate["last_comment_at"] or at > aggregate["last_comment_at"]):
                aggregate["last_comment_at"] = at
            if at and (not last_comment_at or at > last_comment_at):
                last_comment_at = at
        ordered = sorted(
            aggregates.values(),
            key=lambda item: (
                -int(item["comment_count"]),
                -len(item["source_posts"]),
                -int((item["last_comment_at"] or datetime.fromtimestamp(0, timezone.utc)).timestamp()),
            ),
        )
        result = []
        public_candidates = 0
        skipped_non_public = 0
        resolved_senders = 0
        for aggregate in ordered:
            if candidate_limit is not None and public_candidates >= candidate_limit:
                break
            resolved = self._sender(session, aggregate["sender"])
            if resolved["sender_type"] == "unknown":
                continue
            row = {
                **resolved,
                "comment_count": int(aggregate["comment_count"]),
                "post_count": len(aggregate["source_posts"]),
                "source_posts": sorted(aggregate["source_posts"]),
                "last_comment_at": aggregate["last_comment_at"],
            }
            resolved_senders += 1
            if candidate_limit is not None:
                if row["sender_type"] == "user" and row["username"]:
                    result.append(row)
                    public_candidates += 1
                else:
                    skipped_non_public += 1
                continue
            result.append(row)
        result.sort(key=lambda item: (-int(item["comment_count"]), item["sender_type"], item["sender_key"]))
        return result, {
            "comments_fetched": fetched_total,
            "truncated": bool(history_limit and len(message_records) >= history_limit),
            "unavailable_threads": 0,
            "last_comment_at": last_comment_at,
            "unique_senders_seen": len(aggregates),
            "resolved_senders": resolved_senders,
            "public_candidates": public_candidates,
            "skipped_non_public": skipped_non_public,
            "candidate_limit": candidate_limit or 0,
        }

    def _commenters(
        self,
        session,
        chat_id: int,
        posts: list[dict[str, Any]],
        public_job_id: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        sender_cache: dict[tuple[str, int], dict[str, Any]] = {}
        fetched_total = 0
        truncated = False
        unavailable_threads = 0
        last_comment_at: datetime | None = None
        for post in posts:
            if public_job_id:
                self.repository.assert_runnable(public_job_id)
            if fetched_total >= self.settings.max_comments_per_job:
                truncated = True
                break
            message = post["message"]
            reply_info = ((message.get("interaction_info") or {}).get("reply_info") or {})
            reported = int(reply_info.get("reply_count", 0) or 0)
            if reported <= 0:
                continue
            message_id = int(post["message_id"])
            properties = session.invoke("getMessageProperties", chat_id=chat_id, message_id=message_id)
            if not bool(properties.get("can_get_message_thread", False)):
                unavailable_threads += 1
                continue
            try:
                thread = session.invoke("getMessageThread", chat_id=chat_id, message_id=message_id)
                thread_chat_id = int(thread.get("chat_id", 0) or 0)
                thread_message_id = int(thread.get("message_thread_id", 0) or 0)
            except Exception:
                unavailable_threads += 1
                truncated = True
                continue
            if not thread_chat_id or not thread_message_id:
                unavailable_threads += 1
                truncated = True
                continue
            per_post_cap = min(
                self.settings.max_comments_per_post,
                self.settings.max_comments_per_job - fetched_total,
            )
            seen: set[int] = set()
            from_message_id = 0
            post_fetched = 0
            for _ in range(30):
                if post_fetched >= per_post_cap:
                    break
                try:
                    page = session.invoke(
                        "getMessageThreadHistory",
                        chat_id=thread_chat_id,
                        message_id=thread_message_id,
                        from_message_id=from_message_id,
                        offset=0,
                        limit=min(100, per_post_cap - post_fetched),
                    )
                except Exception:
                    unavailable_threads += 1
                    truncated = True
                    break
                batch = [item for item in (page.get("messages") or []) if isinstance(item, dict)]
                new_items = []
                for item in batch:
                    item_id = int(item.get("id", 0) or 0)
                    if not item_id or item_id in seen:
                        continue
                    seen.add(item_id)
                    new_items.append(item)
                if not new_items:
                    break
                counted = 0
                for item in new_items:
                    item_id = int(item.get("id", 0) or 0)
                    # TDLib may include the source channel post and thread service events.
                    # Neither is a human comment and neither may contribute an identity.
                    if (
                        item_id == thread_message_id
                        and int(item.get("chat_id", thread_chat_id) or 0) == thread_chat_id
                    ) or bool(item.get("is_channel_post", False)):
                        continue
                    if is_service_message(item):
                        continue
                    sender = item.get("sender_id") or {}
                    sender_kind = str(sender.get("@type", "") or "")
                    sender_number = int(sender.get("user_id", sender.get("chat_id", 0)) or 0)
                    cache_key = (sender_kind, sender_number)
                    if cache_key not in sender_cache:
                        sender_cache[cache_key] = self._sender(session, sender)
                    resolved = sender_cache[cache_key]
                    if resolved["sender_type"] == "unknown":
                        continue
                    counted += 1
                    key = (resolved["sender_type"], resolved["sender_key"])
                    if key not in aggregates:
                        aggregates[key] = {
                            **resolved,
                            "comment_count": 0,
                            "source_posts": set(),
                            "last_comment_at": None,
                        }
                    at = _timestamp(item.get("date"))
                    aggregate = aggregates[key]
                    aggregate["comment_count"] += 1
                    aggregate["source_posts"].add(str(post["link"]))
                    if at and (not aggregate["last_comment_at"] or at > aggregate["last_comment_at"]):
                        aggregate["last_comment_at"] = at
                    if at and (not last_comment_at or at > last_comment_at):
                        last_comment_at = at
                post_fetched += counted
                fetched_total += counted
                next_from = min(int(item.get("id", 0) or 0) for item in batch if int(item.get("id", 0) or 0))
                if next_from == from_message_id:
                    break
                from_message_id = next_from
            if reported > post_fetched:
                truncated = True
        result = []
        for aggregate in aggregates.values():
            result.append(
                {
                    **{key: value for key, value in aggregate.items() if key != "source_posts"},
                    "post_count": len(aggregate["source_posts"]),
                    "source_posts": sorted(aggregate["source_posts"]),
                }
            )
        result.sort(key=lambda item: (-int(item["comment_count"]), item["sender_type"], item["sender_key"]))
        return result, {
            "comments_fetched": fetched_total,
            "truncated": truncated,
            "last_comment_at": last_comment_at,
            "unavailable_threads": unavailable_threads,
        }


class ManualReviewError(RuntimeError):
    pass


class JoinDailyLimitError(RuntimeError):
    pass


def flood_wait_seconds(exc: Exception) -> int | None:
    text = str(exc or "")
    if "429" not in text and "FLOOD" not in text.upper() and "Too Many Requests" not in text:
        return None
    match = re.search(r"(?:retry after|FLOOD_WAIT_?|wait of)\s*(\d+)", text, re.I)
    return int(match.group(1)) if match else 300
