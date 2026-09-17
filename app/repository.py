from __future__ import annotations

import uuid
import secrets
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from .config import Settings
from .models import (
    AuditLog,
    ChannelSnapshot,
    ChatExportEvent,
    ChatExportJob,
    CollectorRuntime,
    Commenter,
    ContactEvidence,
    JobEvent,
    PlatformSession,
    PlatformUser,
    PlatformInvite,
    RawArtifact,
    ScanJob,
    utcnow,
)
from .security import token_hash


ACTIVE_STATUSES = {"queued", "resolving", "joining", "metadata", "posts", "comments", "contact_analysis", "rate_limited"}
DEDUP_STATUSES = ACTIVE_STATUSES | {"paused"}
FINAL_STATUSES = {"completed", "failed", "cancelled", "expired", "manual_review"}
CHAT_EXPORT_ACTIVE_STATUSES = {"queued", "exporting"}
CHAT_EXPORT_FINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
USER_ROLES = {"super_admin", "admin", "staff"}
LEAD_STATUSES = {"new", "todo", "contacted", "interested", "converted", "no_reply", "not_fit"}
LEAD_STATUS_PRIORITY = ("converted", "interested", "contacted", "todo", "no_reply", "not_fit", "new")

CONTACT_INTENT_TERMS = {
    "contact", "contactez", "contacter", "message", "envoyez", "dm", "manager", "representative",
    "iletişim", "iletişime", "mesaj", "ulaş", "temsilci", "تواصل", "راسل", "اتصل", "مدير",
    "联系", "私信", "负责人",
}
CONTACT_LIST_MARKERS = {"📍", "winner", "winners", "gagnant", "gagnants", "liste des", "获奖", "名单"}


def clean_role(role: str) -> str:
    return role if role in USER_ROLES else "staff"


def clean_tags(tags: Any, *, limit: int = 20) -> list[str]:
    if tags is None:
        raw_items: list[Any] = []
    elif isinstance(tags, str):
        raw_items = re.split(r"[,，;；\n]+", tags)
    elif isinstance(tags, (list, tuple, set)):
        raw_items = list(tags)
    else:
        raw_items = [tags]
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        tag = str(raw or "").strip().strip("#").strip()
        tag = re.sub(r"\s+", " ", tag)[:32]
        key = tag.lower()
        if not tag or key in seen:
            continue
        cleaned.append(tag)
        seen.add(key)
        if len(cleaned) >= limit:
            break
    return cleaned


def clean_lead_status(status: str) -> str:
    return status if status in LEAD_STATUSES else "new"


def normalize_telegram_contact(value: Any) -> str:
    clean = str(value or "").strip().lower()
    if clean.startswith("@"):
        return clean[1:]
    match = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([a-z0-9_]{5,32})(?:[/#?]|$)", clean)
    return match.group(1) if match else clean.lstrip("@")


def summarize_lead_status(statuses: Any) -> str:
    values = {clean_lead_status(str(item or "")) for item in (statuses or [])}
    for status in LEAD_STATUS_PRIORITY:
        if status in values:
            return status
    return "new"


def is_explicit_contact_evidence(item: Any) -> bool:
    if str(item.contact_type) not in {"telegram", "telegram_username"}:
        return False
    if str(item.source_type) in {"bio", "pinned"}:
        return True
    if str(item.source_type) != "post":
        return False
    evidence = str(item.evidence or "").lower()
    if evidence.count("@") > 3 or any(marker in evidence for marker in CONTACT_LIST_MARKERS):
        return False
    return any(term in evidence for term in CONTACT_INTENT_TERMS)


class JobPaused(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _metadata_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Repository:
    def __init__(self, database, settings: Settings):
        self.database = database
        self.settings = settings

    def audit(self, session, action: str, *, actor: str = "system", target: str = "", detail: dict[str, Any] | None = None) -> None:
        session.add(AuditLog(actor=actor, action=action, target=target, detail=detail or {}))

    def create_scan_job(
        self,
        username: str,
        requested_target: str,
        *,
        created_by_user_id: int | None = None,
        created_by_username: str = "",
        tags: Any = None,
        submitter_id: str = "",
        submitter_name: str = "",
        bot_chat_id: str = "",
        force: bool = False,
        actor: str = "",
    ) -> tuple[ScanJob, bool]:
        with self.database.session() as session:
            if not force and self.settings.cache_hours > 0:
                cutoff = utcnow() - timedelta(hours=self.settings.cache_hours)
                cached = session.scalar(
                    select(ScanJob)
                    .where(
                        ScanJob.normalized_username == username,
                        ScanJob.status == "completed",
                        ScanJob.finished_at >= cutoff,
                    )
                    .order_by(ScanJob.finished_at.desc())
                )
                if cached:
                    self.audit(
                        session,
                        "scan.cache_hit",
                        actor=actor or (f"telegram:{submitter_id}" if submitter_id else "web"),
                        target=cached.public_id,
                        detail={"username": username},
                    )
                    return cached, True
            active = session.scalar(
                select(ScanJob).where(
                    ScanJob.normalized_username == username,
                    ScanJob.status.in_(DEDUP_STATUSES),
                ).order_by(ScanJob.created_at.desc())
            )
            if active:
                return active, True
            job = ScanJob(
                public_id=str(uuid.uuid4()),
                requested_target=requested_target,
                normalized_username=username,
                created_by_user_id=created_by_user_id,
                created_by_username=created_by_username[:64],
                tags=clean_tags(tags),
                submitter_telegram_user_id=submitter_id,
                submitter_name=submitter_name,
                bot_chat_id=bot_chat_id,
            )
            session.add(job)
            session.flush()
            session.add(JobEvent(job_id=job.id, event_type="created", detail={
                "username": username,
                "created_by_username": created_by_username[:64],
                "tags": clean_tags(tags),
            }))
            self.audit(
                session,
                "scan.created",
                actor=actor or (f"telegram:{submitter_id}" if submitter_id else "web"),
                target=job.public_id,
                detail={"username": username, "created_by_username": created_by_username[:64], "tags": clean_tags(tags)},
            )
            return job, False

    def inspect_scan_target(self, username: str) -> dict[str, Any]:
        """Describe what a non-force submission would do without mutating data."""
        with self.database.session() as session:
            cutoff = utcnow() - timedelta(hours=self.settings.cache_hours)
            cached = None
            if self.settings.cache_hours > 0:
                cached = session.scalar(
                    select(ScanJob)
                    .where(
                        ScanJob.normalized_username == username,
                        ScanJob.status == "completed",
                        ScanJob.finished_at >= cutoff,
                    )
                    .order_by(ScanJob.finished_at.desc())
                )
            active = session.scalar(
                select(ScanJob)
                .where(
                    ScanJob.normalized_username == username,
                    ScanJob.status.in_(DEDUP_STATUSES),
                )
                .order_by(ScanJob.created_at.desc())
            )
            latest = session.scalar(
                select(ScanJob)
                .where(ScanJob.normalized_username == username)
                .order_by(ScanJob.created_at.desc())
            )
            matched = cached or active or latest
            if cached:
                disposition = "cached_24h"
            elif active:
                disposition = "active"
            elif latest:
                disposition = "previously_scanned"
            else:
                disposition = "new"
            return {
                "disposition": disposition,
                "seen_before": latest is not None,
                "job_id": matched.public_id if matched else "",
                "status": matched.status if matched else "",
                "progress": matched.progress if matched else 0,
                "updated_at": matched.updated_at.isoformat() if matched and matched.updated_at else None,
            }

    def create_manual_review_job(
        self,
        requested_target: str,
        reason: str,
        *,
        created_by_user_id: int | None = None,
        created_by_username: str = "",
        tags: Any = None,
        submitter_id: str = "",
        submitter_name: str = "",
        bot_chat_id: str = "",
        actor: str = "",
    ) -> ScanJob:
        with self.database.session() as session:
            job = ScanJob(
                public_id=str(uuid.uuid4()),
                requested_target=requested_target[:256],
                normalized_username="manual_review",
                created_by_user_id=created_by_user_id,
                created_by_username=created_by_username[:64],
                tags=clean_tags(tags),
                status="manual_review",
                stage="manual_review",
                progress=0,
                submitter_telegram_user_id=submitter_id,
                submitter_name=submitter_name,
                bot_chat_id=bot_chat_id,
                error_code="manual_target",
                error_message=reason[:1000],
                finished_at=utcnow(),
            )
            session.add(job)
            session.flush()
            session.add(JobEvent(job_id=job.id, event_type="manual_review", detail={
                "reason": reason[:300],
                "created_by_username": created_by_username[:64],
                "tags": clean_tags(tags),
            }))
            self.audit(
                session,
                "scan.manual_review.created",
                actor=actor or (f"telegram:{submitter_id}" if submitter_id else "web"),
                target=job.public_id,
                detail={"reason": reason[:300], "created_by_username": created_by_username[:64], "tags": clean_tags(tags)},
            )
            return job

    def create_chat_export_job(
        self,
        requested_target: str,
        *,
        target_label: str = "",
        created_by_user_id: int | None = None,
        created_by_username: str = "",
        tags: Any = None,
        date_range: str = "recent_7",
        since_at: datetime | None = None,
        until_at: datetime | None = None,
        max_messages: int = 1000,
        include_media: bool = False,
        account_name: str = "history_export",
        actor: str = "",
    ) -> ChatExportJob:
        clean_max = max(1, min(int(max_messages or 1000), 100000))
        clean_account = re.sub(r"[^A-Za-z0-9_.-]+", "", str(account_name or "history_export"))[:64] or "history_export"
        with self.database.session() as session:
            job = ChatExportJob(
                public_id=str(uuid.uuid4()),
                requested_target=requested_target.strip()[:512],
                target_label=(target_label or requested_target).strip()[:160],
                created_by_user_id=created_by_user_id,
                created_by_username=created_by_username[:64],
                tags=clean_tags(tags),
                account_name=clean_account,
                date_range=date_range[:24],
                since_at=_aware(since_at),
                until_at=_aware(until_at),
                max_messages=clean_max,
                include_media=bool(include_media),
            )
            session.add(job)
            session.flush()
            session.add(
                ChatExportEvent(
                    export_job_id=job.id,
                    event_type="created",
                    detail={
                        "target": job.requested_target,
                        "date_range": job.date_range,
                        "max_messages": job.max_messages,
                        "include_media": job.include_media,
                        "tags": clean_tags(tags),
                    },
                )
            )
            self.audit(
                session,
                "chat_export.created",
                actor=actor or "web",
                target=job.public_id,
                detail={"target": job.requested_target, "date_range": job.date_range, "max_messages": job.max_messages},
            )
            return job

    def list_chat_export_jobs(self, limit: int = 100) -> list[ChatExportJob]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ChatExportJob).order_by(ChatExportJob.id.desc()).limit(max(1, min(limit, 500)))
                )
            )

    def get_chat_export_job(self, public_id: str) -> ChatExportJob | None:
        with self.database.session() as session:
            return session.scalar(select(ChatExportJob).where(ChatExportJob.public_id == public_id))

    def get_chat_export_bundle(self, public_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            job = session.scalar(select(ChatExportJob).where(ChatExportJob.public_id == public_id))
            if not job:
                return None
            events = list(
                session.scalars(
                    select(ChatExportEvent)
                    .where(ChatExportEvent.export_job_id == job.id)
                    .order_by(ChatExportEvent.id.desc())
                    .limit(100)
                )
            )
            return {"job": job, "events": events}

    def assert_chat_export_runnable(self, public_id: str) -> None:
        with self.database.session() as session:
            job = session.scalar(select(ChatExportJob).where(ChatExportJob.public_id == public_id))
            if not job:
                raise ValueError("导出任务不存在。")
            if job.status == "cancelled":
                raise JobCancelled("导出任务已取消。")

    def claim_next_chat_export_job(self) -> ChatExportJob | None:
        now = utcnow()
        with self.database.session() as session:
            query = (
                select(ChatExportJob)
                .where(ChatExportJob.status == "queued")
                .order_by(ChatExportJob.created_at)
                .limit(1)
            )
            if not self.settings.database_url.startswith("sqlite"):
                query = query.with_for_update(skip_locked=True)
            job = session.scalar(query)
            if not job:
                return None
            job.status = "exporting"
            job.stage = "resolving"
            job.progress = 5
            job.started_at = job.started_at or now
            job.updated_at = now
            job.error_code = ""
            job.error_message = ""
            session.add(ChatExportEvent(export_job_id=job.id, event_type="exporting", detail={}))
            session.flush()
            return job

    def update_chat_export_progress(
        self,
        public_id: str,
        *,
        progress: int,
        stage: str = "exporting",
        detail: dict[str, Any] | None = None,
    ) -> ChatExportJob:
        with self.database.session() as session:
            job = session.scalar(select(ChatExportJob).where(ChatExportJob.public_id == public_id))
            if not job:
                raise ValueError("导出任务不存在。")
            if job.status == "cancelled":
                raise JobCancelled("导出任务已取消。")
            job.status = "exporting"
            job.stage = stage[:32]
            job.progress = max(0, min(99, int(progress)))
            job.updated_at = utcnow()
            if detail:
                job.message_count = int(detail.get("message_count", job.message_count) or 0)
                job.scanned_count = int(detail.get("scanned_count", job.scanned_count) or 0)
                job.batch_count = int(detail.get("batch_count", job.batch_count) or 0)
            session.add(ChatExportEvent(export_job_id=job.id, event_type=job.stage, detail=detail or {}))
            session.flush()
            return job

    def complete_chat_export(self, public_id: str, metadata: dict[str, Any]) -> ChatExportJob:
        now = utcnow()
        chat = metadata.get("chat") or {}
        chat_type = chat.get("type") or {}
        with self.database.session() as session:
            job = session.scalar(select(ChatExportJob).where(ChatExportJob.public_id == public_id))
            if not job:
                raise ValueError("导出任务不存在。")
            if job.status == "cancelled":
                raise JobCancelled("导出任务已取消。")
            job.status = "completed"
            job.stage = "completed"
            job.progress = 100
            job.chat_id = str(chat.get("id", "") or "")[:32]
            job.chat_title = str(chat.get("title", "") or "")[:256]
            job.chat_type = str(chat_type.get("@type", "") or "")[:32] if isinstance(chat_type, dict) else ""
            job.message_count = int(metadata.get("message_count", 0) or 0)
            job.scanned_count = int(metadata.get("scanned_count", 0) or 0)
            job.batch_count = int(metadata.get("batch_count", 0) or 0)
            job.media_files_count = int(metadata.get("media_files_count", 0) or 0)
            job.stop_reason = str(metadata.get("stop_reason", "") or "")[:64]
            job.newest_message_at = _metadata_datetime(metadata.get("newest_message_date"))
            job.oldest_message_at = _metadata_datetime(metadata.get("oldest_message_date"))
            job.files = dict(metadata.get("files", {}) or {})
            job.finished_at = now
            job.updated_at = now
            job.result_expires_at = now + timedelta(days=self.settings.result_retention_days)
            session.add(
                ChatExportEvent(
                    export_job_id=job.id,
                    event_type="completed",
                    detail={
                        "message_count": job.message_count,
                        "media_files_count": job.media_files_count,
                        "stop_reason": job.stop_reason,
                    },
                )
            )
            self.audit(
                session,
                "chat_export.completed",
                target=job.public_id,
                detail={"message_count": job.message_count, "stop_reason": job.stop_reason},
            )
            session.flush()
            return job

    def fail_chat_export(self, public_id: str, code: str, message: str) -> ChatExportJob:
        with self.database.session() as session:
            job = session.scalar(select(ChatExportJob).where(ChatExportJob.public_id == public_id))
            if not job:
                raise ValueError("导出任务不存在。")
            if job.status == "cancelled":
                return job
            job.status = "failed"
            job.stage = "failed"
            job.error_code = code[:64]
            job.error_message = message[:1000]
            job.finished_at = utcnow()
            job.updated_at = utcnow()
            session.add(ChatExportEvent(export_job_id=job.id, event_type="failed", detail={"code": code, "message": message[:300]}))
            session.flush()
            return job

    def chat_export_lifecycle(self, public_id: str, action: str, actor: str) -> ChatExportJob:
        transitions = {
            "retry": ({"failed", "cancelled"}, "queued"),
            "cancel": (CHAT_EXPORT_ACTIVE_STATUSES | {"failed"}, "cancelled"),
        }
        if action not in transitions:
            raise ValueError("未知导出任务动作。")
        sources, target = transitions[action]
        with self.database.session() as session:
            job = session.scalar(select(ChatExportJob).where(ChatExportJob.public_id == public_id))
            if not job or job.status not in sources:
                raise ValueError("当前状态不能执行该动作。")
            job.status = target
            job.stage = target
            job.progress = 0 if target == "queued" else job.progress
            job.error_code = "" if target == "queued" else job.error_code
            job.error_message = "" if target == "queued" else job.error_message
            if target == "queued":
                job.started_at = None
                job.finished_at = None
            if target == "cancelled":
                job.finished_at = utcnow()
            session.add(ChatExportEvent(export_job_id=job.id, event_type=target, detail={"actor": actor}))
            self.audit(session, f"chat_export.{action}", actor=actor, target=job.public_id)
            session.flush()
            return job

    def assert_runnable(self, public_id: str) -> None:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            if job.status == "paused":
                raise JobPaused("任务已暂停。")
            if job.status == "cancelled":
                raise JobCancelled("任务已取消。")

    def get_job(self, public_id: str) -> ScanJob | None:
        with self.database.session() as session:
            return session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))

    def get_job_bundle(self, public_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                return None
            job_creator = session.get(PlatformUser, job.created_by_user_id) if job.created_by_user_id else None
            snapshot = session.scalar(select(ChannelSnapshot).where(ChannelSnapshot.job_id == job.id))
            commenters = list(
                session.scalars(
                    select(Commenter).where(Commenter.job_id == job.id).order_by(Commenter.comment_count.desc(), Commenter.id)
                )
            )
            contacts = list(
                session.scalars(
                    select(ContactEvidence).where(ContactEvidence.job_id == job.id).order_by(ContactEvidence.contact_type, ContactEvidence.value)
                )
            )
            raw_artifacts = list(
                session.scalars(
                    select(RawArtifact)
                    .where(RawArtifact.job_id == job.id)
                    .order_by(RawArtifact.message_date.desc(), RawArtifact.id.desc())
                )
            )
            events = list(
                session.scalars(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id.desc()).limit(100))
            )
            contact_groups: dict[tuple[str, str], dict[str, Any]] = {}
            for item in contacts:
                key = (item.contact_type, item.value)
                group = contact_groups.setdefault(
                    key,
                    {
                        "contact_type": item.contact_type,
                        "value": item.value,
                        "confidence": item.confidence,
                        "entity_kinds": set(),
                        "entity_titles": [],
                        "classification_statuses": set(),
                        "is_contactable": False,
                        "extractors": set(),
                        "source_types": set(),
                        "tags": set(),
                        "lead_statuses": set(),
                        "followup_notes": [],
                        "evidence_count": 0,
                        "examples": [],
                    },
                )
                group["extractors"].add(item.extractor)
                group["entity_kinds"].add(str(getattr(item, "entity_kind", "unknown") or "unknown"))
                entity_title = str(getattr(item, "entity_title", "") or "").strip()
                if entity_title and entity_title not in group["entity_titles"]:
                    group["entity_titles"].append(entity_title)
                group["classification_statuses"].add(
                    str(getattr(item, "classification_status", "not_applicable") or "not_applicable")
                )
                group["is_contactable"] = group["is_contactable"] or bool(
                    getattr(item, "is_contactable", False)
                )
                group["source_types"].add(item.source_type)
                group["tags"].update(clean_tags(item.tags or []))
                group["lead_statuses"].add(clean_lead_status(item.lead_status or "new"))
                note = str(item.followup_note or "").strip()
                if note and note not in group["followup_notes"]:
                    group["followup_notes"].append(note[:1000])
                group["evidence_count"] += 1
                if len(group["examples"]) < 5:
                    group["examples"].append(item)
            grouped_contacts = []
            for group in contact_groups.values():
                entity_kinds = group.pop("entity_kinds", {"unknown"})
                group["entity_kind"] = next(
                    (kind for kind in ("user", "bot", "group", "channel", "unknown") if kind in entity_kinds),
                    "unknown",
                )
                group["entity_title"] = next(iter(group.pop("entity_titles", [])), "")
                statuses = group.pop("classification_statuses", {"not_applicable"})
                group["classification_status"] = next(
                    (status for status in ("completed", "unavailable", "limit_reached", "not_applicable") if status in statuses),
                    "not_applicable",
                )
                group["extractors"] = sorted(group["extractors"])
                group["source_types"] = sorted(group["source_types"])
                group["tags"] = sorted(group["tags"], key=str.lower)
                group["lead_status"] = summarize_lead_status(group.pop("lead_statuses", {"new"}))
                notes = group.pop("followup_notes", [])
                group["followup_note"] = "\n".join(notes[:3])
                grouped_contacts.append(group)
            user_ids = [item.user_id for item in commenters if item.sender_type == "user" and item.user_id]
            cross_channel_counts: dict[str, int] = {}
            if user_ids:
                rows = session.execute(
                    select(Commenter.user_id, func.count(func.distinct(ScanJob.normalized_username)))
                    .join(ScanJob, ScanJob.id == Commenter.job_id)
                    .where(
                        Commenter.user_id.in_(user_ids),
                        Commenter.sender_type == "user",
                        ScanJob.status == "completed",
                    )
                    .group_by(Commenter.user_id)
                )
                cross_channel_counts = {str(user_id): int(count) for user_id, count in rows}
            return {
                "job": job,
                "job_creator": job_creator,
                "snapshot": snapshot,
                "commenters": commenters,
                "contacts": contacts,
                "contact_groups": grouped_contacts,
                "pinned_messages": [item for item in raw_artifacts if item.source_type == "pinned"],
                "recent_posts": [item for item in raw_artifacts if item.source_type == "post"],
                "events": events,
                "cross_channel_counts": cross_channel_counts,
            }

    def get_raw_sources(self, public_id: str) -> list[dict[str, str]]:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            artifacts = list(session.scalars(select(RawArtifact).where(RawArtifact.job_id == job.id).order_by(RawArtifact.id)))
            return [
                {"source_type": item.source_type, "source_ref": item.source_ref, "text": item.text}
                for item in artifacts
            ]

    def replace_contacts(self, public_id: str, contacts: list[dict[str, Any]], actor: str = "system") -> int:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            session.execute(delete(ContactEvidence).where(ContactEvidence.job_id == job.id))
            for item in contacts:
                session.add(ContactEvidence(job_id=job.id, **item))
            summary = dict(job.result_summary or {})
            summary["contacts_found"] = len(contacts)
            job.result_summary = summary
            session.add(JobEvent(job_id=job.id, event_type="contacts_reanalyzed", detail={"contacts_found": len(contacts)}))
            self.audit(
                session,
                "scan.contacts_reanalyzed",
                actor=actor,
                target=job.public_id,
                detail={"contacts_found": len(contacts)},
            )
            return len(contacts)

    def update_ai_report(
        self,
        public_id: str,
        report: dict[str, Any] | None,
        status: str,
        errors: list[str],
        actor: str = "system",
    ) -> None:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            summary = dict(job.result_summary or {})
            summary["ai_report"] = report or {}
            summary["ai_status"] = status
            summary["ai_warnings"] = [str(item)[:300] for item in errors[:20]]
            job.result_summary = summary
            job.ai_status = status
            session.add(JobEvent(job_id=job.id, event_type="ai_report_updated", detail={"status": status, "warnings": len(errors)}))
            self.audit(
                session,
                "scan.ai_report_updated",
                actor=actor,
                target=job.public_id,
                detail={"status": status, "warnings": len(errors)},
            )

    def update_group_quality_report(
        self,
        public_id: str,
        report: dict[str, Any],
        status: str,
        errors: list[str],
        actor: str = "system",
    ) -> None:
        def int_value(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            summary = dict(job.result_summary or {})
            summary["group_quality"] = dict(report or {})
            summary["group_messages_scanned"] = int_value(report.get("messages_scanned", summary.get("group_messages_scanned", 0)))
            summary["group_active_user_limit"] = int_value(report.get("candidate_limit", summary.get("group_active_user_limit", 0)))

            warnings = summary.get("warnings", [])
            if not isinstance(warnings, list):
                warnings = []
            warnings = [str(item)[:300] for item in warnings if not str(item).startswith("AI group quality:")]
            warnings.extend(str(item)[:300] for item in errors[:20])
            summary["warnings"] = warnings

            auto_tags = clean_tags(summary.get("auto_tags", []))
            classification = str(report.get("classification", "") or "")
            for tag in {
                "pure_ad_group": ["纯广告群", "低价值"],
                "active_discussion": ["真实互动"],
                "mixed": ["混合内容"],
            }.get(classification, []):
                if tag.lower() not in {item.lower() for item in auto_tags}:
                    auto_tags.append(tag)
            summary["auto_tags"] = clean_tags(auto_tags)

            if status == "degraded":
                job.ai_status = "degraded"
            elif job.ai_status == "pending":
                job.ai_status = status
            summary["ai_status"] = job.ai_status
            job.result_summary = summary
            session.add(JobEvent(job_id=job.id, event_type="group_quality_updated", detail={"status": status, "warnings": len(errors)}))
            self.audit(
                session,
                "scan.group_quality_updated",
                actor=actor,
                target=job.public_id,
                detail={"status": status, "classification": classification, "warnings": len(errors)},
            )

    def update_job_tags(self, public_id: str, tags: Any, actor: str = "system") -> ScanJob:
        cleaned = clean_tags(tags)
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            job.tags = cleaned
            job.updated_at = utcnow()
            session.add(JobEvent(job_id=job.id, event_type="tags_updated", detail={"tags": cleaned, "actor": actor}))
            self.audit(session, "scan.tags_updated", actor=actor, target=job.public_id, detail={"tags": cleaned})
            session.flush()
            return job

    def update_contact_lead(
        self,
        public_id: str,
        contact_type: str,
        value: str,
        *,
        tags: Any,
        lead_status: str,
        followup_note: str,
        actor: str = "system",
    ) -> int:
        cleaned_tags = clean_tags(tags)
        cleaned_status = clean_lead_status(lead_status)
        note = str(followup_note or "").strip()[:1000]
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            rows = list(
                session.scalars(
                    select(ContactEvidence).where(
                        ContactEvidence.job_id == job.id,
                        ContactEvidence.contact_type == contact_type,
                        ContactEvidence.value == value,
                    )
                )
            )
            if not rows:
                raise ValueError("线索不存在。")
            for row in rows:
                row.tags = cleaned_tags
                row.lead_status = cleaned_status
                row.followup_note = note
            session.add(JobEvent(job_id=job.id, event_type="contact_lead_updated", detail={
                "contact_type": contact_type,
                "value": value,
                "tags": cleaned_tags,
                "lead_status": cleaned_status,
                "actor": actor,
            }))
            self.audit(
                session,
                "lead.contact_updated",
                actor=actor,
                target=f"{public_id}:{contact_type}:{value}",
                detail={"tags": cleaned_tags, "lead_status": cleaned_status},
            )
            return len(rows)

    def update_commenter_lead(
        self,
        commenter_id: int,
        *,
        tags: Any,
        lead_status: str,
        followup_note: str,
        actor: str = "system",
    ) -> Commenter:
        cleaned_tags = clean_tags(tags)
        cleaned_status = clean_lead_status(lead_status)
        note = str(followup_note or "").strip()[:1000]
        with self.database.session() as session:
            commenter = session.get(Commenter, commenter_id)
            if not commenter:
                raise ValueError("线索不存在。")
            commenter.tags = cleaned_tags
            commenter.lead_status = cleaned_status
            commenter.followup_note = note
            session.add(JobEvent(job_id=commenter.job_id, event_type="commenter_lead_updated", detail={
                "commenter_id": commenter.id,
                "tags": cleaned_tags,
                "lead_status": cleaned_status,
                "actor": actor,
            }))
            self.audit(
                session,
                "lead.commenter_updated",
                actor=actor,
                target=str(commenter.id),
                detail={"tags": cleaned_tags, "lead_status": cleaned_status},
            )
            session.flush()
            return commenter

    def update_global_lead(
        self,
        key: str,
        *,
        tags: Any,
        lead_status: str,
        followup_note: str,
        actor: str = "system",
    ) -> int:
        clean_key = str(key or "").strip().lower()
        if ":" not in clean_key:
            raise ValueError("线索标识无效。")
        kind, raw_value = clean_key.split(":", 1)
        if not raw_value:
            raise ValueError("线索标识无效。")
        cleaned_tags = clean_tags(tags)
        cleaned_status = clean_lead_status(lead_status)
        note = str(followup_note or "").strip()[:1000]
        updated = 0
        with self.database.session() as session:
            if kind == "telegram":
                for row in session.scalars(
                    select(ContactEvidence).where(ContactEvidence.contact_type.in_({"telegram", "telegram_username"}))
                ):
                    if normalize_telegram_contact(row.value) != raw_value:
                        continue
                    row.tags = cleaned_tags
                    row.lead_status = cleaned_status
                    row.followup_note = note
                    updated += 1
                for row in session.scalars(select(Commenter).where(Commenter.username != "")):
                    if str(row.username or "").strip().lower().lstrip("@") != raw_value:
                        continue
                    row.tags = cleaned_tags
                    row.lead_status = cleaned_status
                    row.followup_note = note
                    updated += 1
            else:
                for row in session.scalars(select(ContactEvidence).where(ContactEvidence.contact_type == kind)):
                    if str(row.value or "").strip().lower() != raw_value:
                        continue
                    row.tags = cleaned_tags
                    row.lead_status = cleaned_status
                    row.followup_note = note
                    updated += 1
            if not updated:
                raise ValueError("线索不存在。")
            self.audit(
                session,
                "lead.global_updated",
                actor=actor,
                target=clean_key,
                detail={"tags": cleaned_tags, "lead_status": cleaned_status, "records": updated},
            )
        return updated

    def update_language_override(self, public_id: str, code: str, label: str, actor: str = "system") -> ScanJob:
        clean_code = str(code or "").strip().lower()
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            summary = dict(job.result_summary or {})
            current = summary.get("language", {}) if isinstance(summary.get("language", {}), dict) else {}
            detected = summary.get("detected_language", {}) if isinstance(summary.get("detected_language", {}), dict) else {}
            if clean_code:
                if current.get("method") != "manual_override":
                    summary["detected_language"] = current
                summary["language"] = {
                    "code": clean_code,
                    "label": str(label or clean_code)[:64],
                    "confidence": 1.0,
                    "method": "manual_override",
                    "sample_chars": int(current.get("sample_chars", 0) or 0),
                }
            elif detected:
                summary["language"] = detected
                summary.pop("detected_language", None)
            job.result_summary = summary
            job.updated_at = utcnow()
            session.add(
                JobEvent(
                    job_id=job.id,
                    event_type="language_updated",
                    detail={"code": clean_code or str((summary.get("language") or {}).get("code", "")), "actor": actor},
                )
            )
            self.audit(
                session,
                "scan.language_updated",
                actor=actor,
                target=job.public_id,
                detail={"code": clean_code, "manual": bool(clean_code)},
            )
            session.flush()
            return job

    def list_global_leads(self, *, max_rows: int = 10000, limit: int = 2000) -> list[dict[str, Any]]:
        row_limit = max(100, min(int(max_rows), 50000))
        with self.database.session() as session:
            leads: dict[str, dict[str, Any]] = {}

            def get_lead(key: str, value: str) -> dict[str, Any]:
                return leads.setdefault(
                    key,
                    {
                        "key": key,
                        "value": value,
                        "display_name": "",
                        "entity_kinds": set(),
                        "lead_kinds": set(),
                        "statuses": set(),
                        "tags": set(),
                        "notes": [],
                        "source_channels": set(),
                        "sources": [],
                        "evidence_count": 0,
                        "comment_count": 0,
                        "post_count": 0,
                        "latest_at": None,
                    },
                )

            contact_rows = session.execute(
                select(ContactEvidence, ScanJob, ChannelSnapshot)
                .join(ScanJob, ScanJob.id == ContactEvidence.job_id)
                .outerjoin(ChannelSnapshot, ChannelSnapshot.job_id == ScanJob.id)
                .where(ScanJob.status == "completed")
                .order_by(ContactEvidence.id.desc())
                .limit(row_limit)
            )
            for evidence, job, snapshot in contact_rows:
                contact_type = str(evidence.contact_type or "").strip().lower()
                if contact_type in {"telegram", "telegram_username"} and not is_explicit_contact_evidence(evidence):
                    continue
                raw_value = str(evidence.value or "").strip()
                normalized = normalize_telegram_contact(raw_value) if contact_type in {"telegram", "telegram_username"} else raw_value.lower()
                if not normalized:
                    continue
                key = f"telegram:{normalized}" if contact_type in {"telegram", "telegram_username"} else f"{contact_type}:{normalized}"
                display_value = f"@{normalized}" if key.startswith("telegram:") else raw_value
                lead = get_lead(key, display_value)
                lead["lead_kinds"].add("direct_contact")
                lead["entity_kinds"].add(
                    str(getattr(evidence, "entity_kind", "unknown") or "unknown")
                    if key.startswith("telegram:")
                    else contact_type
                )
                lead["display_name"] = lead["display_name"] or str(getattr(evidence, "entity_title", "") or "")
                lead["statuses"].add(clean_lead_status(evidence.lead_status))
                lead["tags"].update(clean_tags(evidence.tags or []))
                note = str(evidence.followup_note or "").strip()
                if note and note not in lead["notes"]:
                    lead["notes"].append(note[:1000])
                channel_username = str(getattr(snapshot, "username", "") or job.normalized_username)
                lead["source_channels"].add(channel_username.lower())
                if len(lead["sources"]) < 8:
                    lead["sources"].append(
                        {
                            "job_id": job.public_id,
                            "username": channel_username,
                            "title": str(getattr(snapshot, "title", "") or ""),
                            "source_ref": str(evidence.source_ref or ""),
                            "evidence": str(evidence.evidence or "")[:320],
                            "source_type": str(evidence.source_type or ""),
                        }
                    )
                lead["evidence_count"] += 1
                latest = _aware(job.finished_at or job.created_at)
                if latest and (lead["latest_at"] is None or latest > lead["latest_at"]):
                    lead["latest_at"] = latest

            commenter_rows = session.execute(
                select(Commenter, ScanJob, ChannelSnapshot)
                .join(ScanJob, ScanJob.id == Commenter.job_id)
                .outerjoin(ChannelSnapshot, ChannelSnapshot.job_id == ScanJob.id)
                .where(ScanJob.status == "completed", Commenter.username != "")
                .order_by(Commenter.id.desc())
                .limit(row_limit)
            )
            for commenter, job, snapshot in commenter_rows:
                username = str(commenter.username or "").strip().lower().lstrip("@")
                if not username:
                    continue
                lead = get_lead(f"telegram:{username}", f"@{username}")
                lead["lead_kinds"].add("audience")
                lead["entity_kinds"].add("user" if commenter.sender_type == "user" else "channel_or_group")
                lead["display_name"] = lead["display_name"] or str(commenter.display_name or "")
                lead["statuses"].add(clean_lead_status(commenter.lead_status))
                lead["tags"].update(clean_tags(commenter.tags or []))
                note = str(commenter.followup_note or "").strip()
                if note and note not in lead["notes"]:
                    lead["notes"].append(note[:1000])
                channel_username = str(getattr(snapshot, "username", "") or job.normalized_username)
                lead["source_channels"].add(channel_username.lower())
                if len(lead["sources"]) < 8:
                    lead["sources"].append(
                        {
                            "job_id": job.public_id,
                            "username": channel_username,
                            "title": str(getattr(snapshot, "title", "") or ""),
                            "source_ref": (commenter.source_posts or [""])[0],
                            "evidence": f"{int(commenter.comment_count or 0)} 次互动，参与 {int(commenter.post_count or 0)} 条内容",
                            "source_type": "commenter",
                        }
                    )
                lead["evidence_count"] += 1
                lead["comment_count"] += int(commenter.comment_count or 0)
                lead["post_count"] += int(commenter.post_count or 0)
                latest = _aware(commenter.last_comment_at or job.finished_at or job.created_at)
                if latest and (lead["latest_at"] is None or latest > lead["latest_at"]):
                    lead["latest_at"] = latest

            result: list[dict[str, Any]] = []
            for lead in leads.values():
                entity_kinds = set(lead.pop("entity_kinds"))
                lead_kinds = set(lead.pop("lead_kinds"))
                statuses = set(lead.pop("statuses"))
                source_channels = set(lead.pop("source_channels"))
                lead["entity_kind"] = "user" if "user" in entity_kinds else sorted(entity_kinds)[0]
                lead["lead_kind"] = "both" if len(lead_kinds) > 1 else next(iter(lead_kinds), "audience")
                lead["lead_status"] = summarize_lead_status(statuses)
                lead["tags"] = sorted(lead["tags"], key=str.lower)
                lead["followup_note"] = "\n".join(lead.pop("notes")[:3])
                lead["source_count"] = len(source_channels)
                result.append(lead)
            priority = {status: index for index, status in enumerate(LEAD_STATUS_PRIORITY)}
            result.sort(
                key=lambda item: (
                    priority.get(item["lead_status"], len(priority)),
                    -int(item["source_count"]),
                    -int(item["evidence_count"]),
                    item["value"].lower(),
                )
            )
            return result[: max(1, min(int(limit), 5000))]

    def list_discovery_candidates(self, *, limit: int = 2000) -> dict[str, Any]:
        with self.database.session() as session:
            completed_jobs = list(
                session.scalars(
                    select(ScanJob)
                    .where(ScanJob.status == "completed")
                    .order_by(ScanJob.id.desc())
                    .limit(5000)
                )
            )
            candidates: dict[str, dict[str, Any]] = {}
            for source_job in completed_jobs:
                recommendations = (source_job.result_summary or {}).get("similar_channels", [])
                if not isinstance(recommendations, list):
                    continue
                for raw in recommendations:
                    if not isinstance(raw, dict):
                        continue
                    username = str(raw.get("username", "") or "").strip().lower().lstrip("@")
                    if not username or username == source_job.normalized_username:
                        continue
                    rank = max(1, int(raw.get("rank", 999) or 999))
                    item = candidates.setdefault(
                        username,
                        {
                            "username": username,
                            "title": str(raw.get("title", "") or ""),
                            "member_count": int(raw.get("member_count", 0) or 0),
                            "public_url": str(raw.get("public_url", "") or f"https://t.me/{username}"),
                            "best_rank": rank,
                            "avg_views": int(raw.get("avg_views", 0) or 0),
                            "avg_reactions": float(raw.get("avg_reactions", 0) or 0),
                            "engagement_posts": int(raw.get("engagement_posts", 0) or 0),
                            "sources": [],
                            "source_usernames": set(),
                        },
                    )
                    if rank < item["best_rank"]:
                        item.update(
                            {
                                "title": str(raw.get("title", "") or item["title"]),
                                "member_count": int(raw.get("member_count", 0) or item["member_count"]),
                                "best_rank": rank,
                                "avg_views": int(raw.get("avg_views", 0) or item["avg_views"]),
                                "avg_reactions": float(raw.get("avg_reactions", 0) or item["avg_reactions"]),
                                "engagement_posts": int(raw.get("engagement_posts", 0) or item["engagement_posts"]),
                            }
                        )
                    if source_job.normalized_username not in item["source_usernames"]:
                        item["source_usernames"].add(source_job.normalized_username)
                        item["sources"].append(
                            {"username": source_job.normalized_username, "job_id": source_job.public_id, "rank": rank}
                        )

            usernames = set(candidates)
            latest_jobs: dict[str, ScanJob] = {}
            if usernames:
                for job in session.scalars(
                    select(ScanJob).where(ScanJob.normalized_username.in_(usernames)).order_by(ScanJob.id.desc())
                ):
                    latest_jobs.setdefault(job.normalized_username, job)
            rows = []
            for username, item in candidates.items():
                latest = latest_jobs.get(username)
                if not latest:
                    state = "new"
                elif latest.status in ACTIVE_STATUSES:
                    state = "active"
                elif latest.status == "completed":
                    summary = latest.result_summary or {}
                    language = summary.get("language", {}) if isinstance(summary.get("language", {}), dict) else {}
                    engagement = summary.get("engagement", {}) if isinstance(summary.get("engagement", {}), dict) else {}
                    state = "completed_v2" if language.get("code") and engagement.get("method") else "completed_legacy"
                else:
                    state = "failed"
                item["source_count"] = len(item.pop("source_usernames"))
                item["state"] = state
                item["job_id"] = latest.public_id if latest else ""
                item["job_status"] = latest.status if latest else ""
                rows.append(item)
            state_order = {"new": 0, "failed": 1, "completed_legacy": 2, "active": 3, "completed_v2": 4}
            rows.sort(
                key=lambda item: (
                    state_order.get(item["state"], 9),
                    -int(item["source_count"]),
                    int(item["best_rank"]),
                    item["username"],
                )
            )
            rows = rows[: max(1, min(int(limit), 5000))]
            stats = {
                "total": len(rows),
                "new": sum(1 for item in rows if item["state"] == "new"),
                "active": sum(1 for item in rows if item["state"] == "active"),
                "completed": sum(1 for item in rows if item["state"] == "completed_v2"),
                "needs_upgrade": sum(1 for item in rows if item["state"] == "completed_legacy"),
            }
            return {"candidates": rows, "stats": stats}

    def list_jobs(self, limit: int = 100) -> list[ScanJob]:
        with self.database.session() as session:
            return list(session.scalars(select(ScanJob).order_by(ScanJob.id.desc()).limit(max(1, min(limit, 500)))))

    def list_job_cards(self, limit: int = 500) -> dict[str, Any]:
        """Return one representative card per channel plus non-destructive history."""
        with self.database.session() as session:
            jobs = list(
                session.scalars(
                    select(ScanJob).order_by(ScanJob.id.desc()).limit(max(1, min(limit, 500)))
                )
            )
            grouped: dict[str, list[ScanJob]] = {}
            for job in jobs:
                grouped.setdefault(job.normalized_username, []).append(job)

            primary: list[ScanJob] = []
            for channel_jobs in grouped.values():
                chosen = next((job for job in channel_jobs if job.status in ACTIVE_STATUSES), None)
                chosen = chosen or next((job for job in channel_jobs if job.status == "completed"), None)
                primary.append(chosen or channel_jobs[0])
            primary.sort(key=lambda item: item.id, reverse=True)
            primary_ids = {job.id for job in primary}
            latest_by_username = {username: channel_jobs[0] for username, channel_jobs in grouped.items()}

            snapshots = {
                item.job_id: item
                for item in session.scalars(
                    select(ChannelSnapshot).where(ChannelSnapshot.job_id.in_(primary_ids))
                )
            } if primary_ids else {}
            direct_contacts: dict[int, set[str]] = {job_id: set() for job_id in primary_ids}
            if primary_ids:
                for item in session.scalars(select(ContactEvidence).where(ContactEvidence.job_id.in_(primary_ids))):
                    if is_explicit_contact_evidence(item) and str(getattr(item, "entity_kind", "unknown") or "unknown") not in {"bot", "channel", "group"}:
                        direct_contacts.setdefault(item.job_id, set()).add(item.value.strip().lower().lstrip("@"))

            cards: list[dict[str, Any]] = []
            for job in primary:
                summary = dict(job.result_summary or {})
                ai_report = summary.get("ai_report", {}) if isinstance(summary.get("ai_report", {}), dict) else {}
                language = summary.get("language", {}) if isinstance(summary.get("language", {}), dict) else {}
                engagement = summary.get("engagement", {}) if isinstance(summary.get("engagement", {}), dict) else {}
                snapshot = snapshots.get(job.id)
                public_usernames = int(summary.get("public_usernames", 0) or 0)
                comments_fetched = int(getattr(snapshot, "comments_fetched", 0) or 0)
                latest_job = latest_by_username.get(job.normalized_username, job)
                latest_error = f"{latest_job.error_code} {latest_job.error_message}".upper()
                upgrade_blocked = bool(
                    latest_job.id > job.id
                    and latest_job.status in {"failed", "manual_review"}
                    and "CHANNEL_PRIVATE" in latest_error
                )
                cards.append(
                    {
                        "job": job,
                        "snapshot": snapshot,
                        "auto_tags": clean_tags(summary.get("auto_tags", []) if isinstance(summary.get("auto_tags", []), list) else []),
                        "direct_contact_count": len(direct_contacts.get(job.id, set())),
                        "comment_clue_count": public_usernames,
                        "comments_fetched": comments_fetched,
                        "has_comments": comments_fetched > 0,
                        "language": language,
                        "engagement": engagement,
                        "v2_ready": bool(language.get("code") and engagement.get("method")),
                        "upgrade_blocked": upgrade_blocked,
                        "upgrade_error": latest_job.error_message if upgrade_blocked else "",
                        "similar_channels_count": len(summary.get("similar_channels", [])) if isinstance(summary.get("similar_channels", []), list) else 0,
                        "profile": str(ai_report.get("executive_summary", "") or getattr(snapshot, "bio", "") or job.error_message or ""),
                    }
                )
            history = [job for job in jobs if job.id not in primary_ids]
            return {"cards": cards, "history": history}

    def claim_next_job(self, collector_account: str = "primary", failover_reason: str = "") -> ScanJob | None:
        now = utcnow()
        with self.database.session() as session:
            query = (
                select(ScanJob)
                .where(
                    ScanJob.status.in_({"queued", "rate_limited"}),
                    or_(ScanJob.next_run_at.is_(None), ScanJob.next_run_at <= now),
                    or_(ScanJob.collector_account == "", ScanJob.collector_account == collector_account),
                )
                .order_by(ScanJob.created_at)
                .limit(1)
            )
            if not self.settings.database_url.startswith("sqlite"):
                query = query.with_for_update(skip_locked=True)
            job = session.scalar(query)
            if not job:
                return None
            job.status = "resolving"
            job.stage = "resolving"
            job.progress = 5
            job.started_at = job.started_at or now
            if not job.collector_account:
                job.collector_account = collector_account
                job.failover_reason = failover_reason[:128]
            job.updated_at = now
            job.next_run_at = None
            session.add(JobEvent(job_id=job.id, event_type="resolving", detail={
                "collector_account": collector_account,
                "failover_reason": failover_reason[:128],
            }))
            session.flush()
            return job

    def collector_runtime(self) -> dict[str, CollectorRuntime]:
        with self.database.session() as session:
            rows = list(session.scalars(select(CollectorRuntime).order_by(CollectorRuntime.alias)))
            return {row.alias: row for row in rows}

    def mark_collector_ready(self, alias: str) -> None:
        now = utcnow()
        with self.database.session() as session:
            row = session.get(CollectorRuntime, alias)
            if not row:
                row = CollectorRuntime(alias=alias)
                session.add(row)
            row.status = "ready"
            row.consecutive_failures = 0
            row.last_error_code = ""
            row.last_error_message = ""
            row.last_ready_at = now
            row.unavailable_since = None
            row.recovery_due_at = None
            row.updated_at = now

    def mark_collector_failure(self, alias: str, code: str, message: str, *, unavailable: bool) -> CollectorRuntime:
        now = utcnow()
        with self.database.session() as session:
            row = session.get(CollectorRuntime, alias)
            if not row:
                row = CollectorRuntime(alias=alias)
                session.add(row)
            row.consecutive_failures = int(row.consecutive_failures or 0) + 1
            row.last_error_code = code[:64]
            row.last_error_message = message[:500]
            row.updated_at = now
            if unavailable:
                row.status = "unavailable"
                row.unavailable_since = row.unavailable_since or now
                row.recovery_due_at = now + timedelta(seconds=self.settings.collector_recovery_seconds)
            else:
                row.status = "degraded"
            session.flush()
            return row

    def collector_should_probe(self, alias: str) -> bool:
        with self.database.session() as session:
            row = session.get(CollectorRuntime, alias)
            if not row or row.status != "unavailable":
                return False
            due = _aware(row.recovery_due_at)
            return due is None or due <= utcnow()

    def transition(self, public_id: str, status: str, progress: int, detail: dict[str, Any] | None = None) -> ScanJob:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            if job.status == "paused":
                raise JobPaused("任务已暂停。")
            if job.status == "cancelled":
                raise JobCancelled("任务已取消。")
            job.status = status
            job.stage = status
            job.progress = max(0, min(100, int(progress)))
            job.updated_at = utcnow()
            session.add(JobEvent(job_id=job.id, event_type=status, detail=detail or {}))
            session.flush()
            return job

    def joins_today(self) -> int:
        cutoff = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(JobEvent.id)).where(
                        JobEvent.event_type == "joined",
                        JobEvent.created_at >= cutoff,
                    )
                )
                or 0
            )

    def mark_joined(self, public_id: str) -> None:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            job.joined_during_job = True
            session.add(JobEvent(job_id=job.id, event_type="joined", detail={}))
            self.audit(session, "telegram.channel.joined", target=job.public_id, detail={"username": job.normalized_username})

    def complete(self, public_id: str, result: dict[str, Any]) -> ScanJob:
        now = utcnow()
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            if job.status == "paused":
                raise JobPaused("任务已暂停。")
            if job.status == "cancelled":
                raise JobCancelled("任务已取消。")
            session.execute(delete(ChannelSnapshot).where(ChannelSnapshot.job_id == job.id))
            session.execute(delete(Commenter).where(Commenter.job_id == job.id))
            session.execute(delete(ContactEvidence).where(ContactEvidence.job_id == job.id))
            session.execute(delete(RawArtifact).where(RawArtifact.job_id == job.id))
            channel = result["channel"]
            session.add(ChannelSnapshot(job_id=job.id, **channel))
            for item in result.get("commenters", []):
                session.add(Commenter(job_id=job.id, **item))
            for item in result.get("contacts", []):
                session.add(ContactEvidence(job_id=job.id, **item))
            raw_expiry = now + timedelta(days=self.settings.raw_retention_days)
            for item in result.get("raw_artifacts", []):
                session.add(RawArtifact(job_id=job.id, expires_at=raw_expiry, **item))
            summary = dict(result.get("summary", {}))
            job.status = "completed"
            job.stage = "completed"
            job.progress = 100
            job.finished_at = now
            job.updated_at = now
            job.ai_status = str(result.get("ai_status", "degraded"))
            job.joined_during_job = job.joined_during_job or bool(result.get("joined_during_job", False))
            job.result_summary = summary
            job.result_expires_at = now + timedelta(days=self.settings.result_retention_days)
            session.add(JobEvent(job_id=job.id, event_type="completed", detail=summary))
            self.audit(session, "scan.completed", target=job.public_id, detail=summary)
            session.flush()
            return job

    def fail(self, public_id: str, code: str, message: str, *, manual_review: bool = False) -> ScanJob:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            if job.status in {"paused", "cancelled"}:
                return job
            job.status = "manual_review" if manual_review else "failed"
            job.stage = job.status
            job.error_code = code[:64]
            job.error_message = message[:1000]
            job.finished_at = utcnow()
            job.updated_at = utcnow()
            session.add(JobEvent(job_id=job.id, event_type=job.status, detail={"code": code, "message": message[:300]}))
            session.flush()
            return job

    def rate_limit(self, public_id: str, seconds: int, message: str) -> ScanJob:
        wait_seconds = max(1, min(int(seconds), 7 * 24 * 3600))
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job:
                raise ValueError("任务不存在。")
            if job.status in {"paused", "cancelled"}:
                return job
            job.status = "rate_limited"
            job.stage = "rate_limited"
            job.next_run_at = utcnow() + timedelta(seconds=wait_seconds)
            job.error_message = message[:1000]
            session.add(JobEvent(job_id=job.id, event_type="rate_limited", detail={"wait_seconds": wait_seconds}))
            session.flush()
            return job

    def lifecycle(self, public_id: str, action: str, actor: str) -> ScanJob:
        transitions = {
            "pause": (ACTIVE_STATUSES, "paused"),
            "resume": ({"paused"}, "queued"),
            "retry": ({"failed", "manual_review"}, "queued"),
            "cancel": (ACTIVE_STATUSES | {"paused", "failed", "manual_review"}, "cancelled"),
        }
        if action not in transitions:
            raise ValueError("未知任务动作。")
        sources, target = transitions[action]
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if not job or job.status not in sources:
                raise ValueError("当前状态不能执行该动作。")
            job.status = target
            job.stage = target
            job.next_run_at = None
            job.error_code = "" if target == "queued" else job.error_code
            job.error_message = "" if target == "queued" else job.error_message
            job.finished_at = utcnow() if target == "cancelled" else None
            session.add(JobEvent(job_id=job.id, event_type=target, detail={"actor": actor}))
            self.audit(session, f"scan.{action}", actor=actor, target=job.public_id)
            session.flush()
            return job

    def set_bot_message(self, public_id: str, message_id: str) -> None:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if job:
                job.bot_message_id = message_id

    def bot_jobs_needing_notification(self) -> list[ScanJob]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ScanJob).where(
                        ScanJob.bot_chat_id != "",
                        ScanJob.bot_message_id != "",
                        ScanJob.bot_last_notified_status != ScanJob.status,
                    ).order_by(ScanJob.id)
                )
            )

    def mark_bot_notified(self, public_id: str, status: str) -> None:
        with self.database.session() as session:
            job = session.scalar(select(ScanJob).where(ScanJob.public_id == public_id))
            if job:
                job.bot_last_notified_status = status

    def recover_stale_jobs(self) -> int:
        with self.database.session() as session:
            jobs = list(session.scalars(select(ScanJob).where(ScanJob.status.in_(ACTIVE_STATUSES - {"queued", "rate_limited"}))))
            for job in jobs:
                job.status = "queued"
                job.stage = "queued"
                job.next_run_at = None
                session.add(JobEvent(job_id=job.id, event_type="recovered", detail={}))
            export_jobs = list(session.scalars(select(ChatExportJob).where(ChatExportJob.status == "exporting")))
            for job in export_jobs:
                job.status = "queued"
                job.stage = "queued"
                job.progress = 0
                session.add(ChatExportEvent(export_job_id=job.id, event_type="recovered", detail={}))
            return len(jobs) + len(export_jobs)

    def purge_expired(self) -> dict[str, int]:
        now = utcnow()
        raw_cutoff = now - timedelta(days=self.settings.raw_retention_days)
        audit_cutoff = now - timedelta(days=self.settings.audit_retention_days)
        with self.database.session() as session:
            raw_count = session.execute(delete(RawArtifact).where(RawArtifact.expires_at <= now)).rowcount or 0
            bio_count = session.execute(
                update(ChannelSnapshot)
                .where(ChannelSnapshot.created_at <= raw_cutoff, ChannelSnapshot.bio != "")
                .values(bio="")
            ).rowcount or 0
            expired_jobs = list(
                session.scalars(
                    select(ScanJob).where(
                        ScanJob.result_expires_at.is_not(None),
                        ScanJob.result_expires_at <= now,
                        ScanJob.status == "completed",
                    )
                )
            )
            for job in expired_jobs:
                session.execute(delete(ChannelSnapshot).where(ChannelSnapshot.job_id == job.id))
                session.execute(delete(Commenter).where(Commenter.job_id == job.id))
                session.execute(delete(ContactEvidence).where(ContactEvidence.job_id == job.id))
                job.status = "expired"
                job.stage = "expired"
                job.result_summary = {"expired": True}
            audit_count = session.execute(delete(AuditLog).where(AuditLog.created_at < audit_cutoff)).rowcount or 0
            return {
                "raw_deleted": raw_count,
                "bios_redacted": bio_count,
                "results_expired": len(expired_jobs),
                "audit_deleted": audit_count,
            }

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str,
        display_name: str,
        actor: str,
        *,
        must_change_password: bool = False,
    ) -> PlatformUser:
        clean_user_role = clean_role(role)
        with self.database.session() as session:
            if session.scalar(select(PlatformUser).where(PlatformUser.username == username)):
                raise ValueError("用户名已存在。")
            user = PlatformUser(
                username=username,
                password_hash=password_hash,
                role=clean_user_role,
                display_name=display_name,
                must_change_password=bool(must_change_password),
            )
            session.add(user)
            session.flush()
            self.audit(
                session,
                "platform.user.created",
                actor=actor,
                target=str(user.id),
                detail={"username": username, "role": clean_user_role, "must_change_password": bool(must_change_password)},
            )
            return user

    def update_user(
        self,
        user_id: int,
        *,
        display_name: str,
        role: str,
        active: bool,
        actor_user_id: int,
        actor: str,
    ) -> PlatformUser:
        clean_user_role = clean_role(role)
        with self.database.session() as session:
            user = session.get(PlatformUser, user_id)
            if not user:
                raise ValueError("账号不存在。")
            if user.id == actor_user_id and (not active or clean_user_role != "super_admin"):
                raise ValueError("不能停用或降级当前超级管理员账号。")
            user.display_name = display_name.strip()[:120]
            user.role = clean_user_role
            user.active = bool(active)
            if not user.active:
                session.execute(delete(PlatformSession).where(PlatformSession.user_id == user.id))
            self.audit(
                session,
                "platform.user.updated",
                actor=actor,
                target=str(user.id),
                detail={"username": user.username, "role": user.role, "active": user.active},
            )
            session.flush()
            return user

    def reset_user_password(
        self,
        user_id: int,
        password_hash: str,
        actor: str,
        *,
        must_change_password: bool = True,
    ) -> PlatformUser:
        with self.database.session() as session:
            user = session.get(PlatformUser, user_id)
            if not user:
                raise ValueError("账号不存在。")
            user.password_hash = password_hash
            user.must_change_password = bool(must_change_password)
            session.execute(delete(PlatformSession).where(PlatformSession.user_id == user.id))
            self.audit(
                session,
                "platform.user.password_reset",
                actor=actor,
                target=str(user.id),
                detail={"username": user.username, "must_change_password": bool(must_change_password)},
            )
            session.flush()
            return user

    def create_invite(self, created_by_user_id: int, role: str, actor: str) -> tuple[str, datetime]:
        clean_user_role = clean_role(role)
        raw_token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(days=7)
        with self.database.session() as session:
            invite = PlatformInvite(
                token_hash=token_hash(raw_token),
                role=clean_user_role,
                created_by_user_id=created_by_user_id,
                expires_at=expires_at,
            )
            session.add(invite)
            session.flush()
            self.audit(
                session,
                "platform.invite.created",
                actor=actor,
                target=str(invite.id),
                detail={"role": clean_user_role, "expires_at": expires_at.isoformat()},
            )
        return raw_token, expires_at

    def get_valid_invite(self, raw_token: str) -> PlatformInvite | None:
        with self.database.session() as session:
            invite = session.scalar(select(PlatformInvite).where(PlatformInvite.token_hash == token_hash(raw_token)))
            if not invite or invite.claimed_at or _aware(invite.expires_at) <= utcnow():
                return None
            return invite

    def claim_invite(self, raw_token: str, username: str, password_hash: str, display_name: str) -> PlatformUser:
        with self.database.session() as session:
            invite = session.scalar(
                select(PlatformInvite)
                .where(PlatformInvite.token_hash == token_hash(raw_token))
                .with_for_update()
            )
            if not invite or invite.claimed_at or _aware(invite.expires_at) <= utcnow():
                raise ValueError("邀请链接无效、已使用或已过期。")
            if session.scalar(select(PlatformUser).where(PlatformUser.username == username)):
                raise ValueError("用户名已存在。")
            user = PlatformUser(
                username=username,
                password_hash=password_hash,
                role=invite.role,
                display_name=display_name,
                must_change_password=False,
            )
            session.add(user)
            session.flush()
            invite.claimed_by_user_id = user.id
            invite.claimed_at = utcnow()
            self.audit(
                session,
                "platform.invite.claimed",
                actor=f"platform:{username}",
                target=str(invite.id),
                detail={"user_id": user.id, "role": user.role},
            )
            return user
