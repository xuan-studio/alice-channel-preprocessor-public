from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PlatformUser(Base):
    __tablename__ = "platform_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="staff")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformSession(Base):
    __tablename__ = "platform_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("platform_users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[PlatformUser] = relationship()


class PlatformInvite(Base):
    __tablename__ = "platform_invites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="staff")
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("platform_users.id", ondelete="CASCADE"), index=True)
    claimed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("platform_users.id", ondelete="SET NULL"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    requested_target: Mapped[str] = mapped_column(String(256))
    normalized_username: Mapped[str] = mapped_column(String(64), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("platform_users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_username: Mapped[str] = mapped_column(String(64), default="", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    submitter_telegram_user_id: Mapped[str] = mapped_column(String(32), default="")
    submitter_name: Mapped[str] = mapped_column(String(160), default="")
    bot_chat_id: Mapped[str] = mapped_column(String(32), default="")
    bot_message_id: Mapped[str] = mapped_column(String(32), default="")
    bot_last_notified_status: Mapped[str] = mapped_column(String(32), default="")
    collector_account: Mapped[str] = mapped_column(String(24), default="")
    failover_reason: Mapped[str] = mapped_column(String(128), default="")
    joined_during_job: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("scan_jobs.id"), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    ai_status: Mapped[str] = mapped_column(String(32), default="pending")
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class ChatExportJob(Base):
    __tablename__ = "chat_export_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    requested_target: Mapped[str] = mapped_column(String(512))
    target_label: Mapped[str] = mapped_column(String(160), default="")
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("platform_users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_username: Mapped[str] = mapped_column(String(64), default="", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    account_name: Mapped[str] = mapped_column(String(64), default="history_export")
    date_range: Mapped[str] = mapped_column(String(24), default="recent_7")
    since_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_messages: Mapped[int] = mapped_column(Integer, default=1000)
    include_media: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    chat_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    chat_title: Mapped[str] = mapped_column(String(256), default="")
    chat_type: Mapped[str] = mapped_column(String(32), default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    scanned_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    media_files_count: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str] = mapped_column(String(64), default="")
    newest_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    oldest_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    files: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class ChatExportEvent(Base):
    __tablename__ = "chat_export_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    export_job_id: Mapped[int] = mapped_column(ForeignKey("chat_export_jobs.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class CollectorRuntime(Base):
    __tablename__ = "collector_runtime"
    alias: Mapped[str] = mapped_column(String(24), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str] = mapped_column(String(64), default="")
    last_error_message: Mapped[str] = mapped_column(Text, default="")
    last_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unavailable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ChannelSnapshot(Base):
    __tablename__ = "channel_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), unique=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    bio: Mapped[str] = mapped_column(Text, default="")
    public_url: Mapped[str] = mapped_column(String(512), default="")
    linked_chat_id: Mapped[str] = mapped_column(String(32), default="")
    linked_chat_title: Mapped[str] = mapped_column(String(256), default="")
    linked_chat_username: Mapped[str] = mapped_column(String(64), default="")
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_comment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_source: Mapped[str] = mapped_column(String(32), default="")
    posts_scanned: Mapped[int] = mapped_column(Integer, default=0)
    pinned_scanned: Mapped[int] = mapped_column(Integer, default=0)
    comments_fetched: Mapped[int] = mapped_column(Integer, default=0)
    comments_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Commenter(Base):
    __tablename__ = "commenters"
    __table_args__ = (UniqueConstraint("job_id", "sender_type", "sender_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), index=True)
    sender_type: Mapped[str] = mapped_column(String(20))
    sender_key: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32), default="")
    sender_chat_id: Mapped[str] = mapped_column(String(32), default="")
    username: Mapped[str] = mapped_column(String(64), default="", index=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    last_comment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_posts: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_username_reason: Mapped[str] = mapped_column(String(64), default="")
    lead_status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    followup_note: Mapped[str] = mapped_column(Text, default="")


class ContactEvidence(Base):
    __tablename__ = "contact_evidence"
    __table_args__ = (UniqueConstraint("job_id", "contact_type", "value", "source_ref"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), index=True)
    contact_type: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(24))
    source_ref: Mapped[str] = mapped_column(String(512), default="")
    evidence: Mapped[str] = mapped_column(String(320), default="")
    confidence: Mapped[str] = mapped_column(String(16), default="high")
    extractor: Mapped[str] = mapped_column(String(24), default="rule")
    entity_kind: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    entity_title: Mapped[str] = mapped_column(String(256), default="")
    is_contactable: Mapped[bool] = mapped_column(Boolean, default=False)
    classification_status: Mapped[str] = mapped_column(String(24), default="not_applicable")
    lead_status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    followup_note: Mapped[str] = mapped_column(Text, default="")


class RawArtifact(Base):
    __tablename__ = "raw_artifacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(24))
    source_ref: Mapped[str] = mapped_column(String(512), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    message_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    action: Mapped[str] = mapped_column(String(128), index=True)
    target: Mapped[str] = mapped_column(String(256), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
