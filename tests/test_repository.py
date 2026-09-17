from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.config import load_settings
from app.db import Database
from app.extractors import normalize_channel_target
from app.security import hash_password
from app.repository import JobPaused, Repository, is_explicit_contact_evidence


def build_repo(tmp_path: Path) -> Repository:
    settings = replace(load_settings(), database_url=f"sqlite:///{tmp_path / 'test.db'}", cache_hours=24)
    database = Database(settings)
    database.initialize()
    return Repository(database, settings)


def test_job_cache_and_lifecycle(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    username = normalize_channel_target("@example_news")
    job, cached = repo.create_scan_job(username, "@example_news")
    assert not cached
    assert job.status == "queued"
    claimed = repo.claim_next_job()
    assert claimed and claimed.public_id == job.public_id
    paused = repo.lifecycle(job.public_id, "pause", "test")
    assert paused.status == "paused"
    resumed = repo.lifecycle(job.public_id, "resume", "test")
    assert resumed.status == "queued"


def test_scan_target_inspection_and_paused_dedup(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    assert repo.inspect_scan_target("example_news")["disposition"] == "new"
    job, _ = repo.create_scan_job("example_news", "@example_news")
    assert repo.inspect_scan_target("example_news")["disposition"] == "active"
    repo.lifecycle(job.public_id, "pause", "test")
    reused, cached = repo.create_scan_job("example_news", "@example_news")
    assert cached is True
    assert reused.public_id == job.public_id


def test_complete_separates_user_and_chat_identity(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    job, _ = repo.create_scan_job("example_news", "@example_news")
    result = {
        "channel": {
            "telegram_chat_id": "-1001",
            "username": "example_news",
            "title": "Example",
            "bio": "",
            "public_url": "https://t.me/example_news",
            "linked_chat_id": "",
            "linked_chat_title": "",
            "linked_chat_username": "",
            "member_count": 0,
            "last_post_at": None,
            "last_comment_at": None,
            "last_activity_at": None,
            "last_activity_source": "",
            "posts_scanned": 1,
            "pinned_scanned": 0,
            "comments_fetched": 2,
            "comments_truncated": False,
        },
        "commenters": [
            {"sender_type": "user", "sender_key": "42", "user_id": "42", "sender_chat_id": "", "username": "reader", "display_name": "Reader", "comment_count": 1, "post_count": 1, "last_comment_at": None, "source_posts": ["https://t.me/example_news/1"], "missing_username_reason": ""},
            {"sender_type": "chat", "sender_key": "-2", "user_id": "", "sender_chat_id": "-2", "username": "group_identity", "display_name": "Group", "comment_count": 1, "post_count": 1, "last_comment_at": None, "source_posts": ["https://t.me/example_news/1"], "missing_username_reason": ""},
        ],
        "contacts": [],
        "raw_artifacts": [
            {"source_type": "pinned", "source_ref": "https://t.me/example_news/1", "text": "Contact @reader", "message_date": None},
            {"source_type": "post", "source_ref": "https://t.me/example_news/2", "text": "A recent post", "message_date": None},
        ],
        "summary": {"unique_user_commenters": 1, "public_usernames": 1},
        "ai_status": "degraded",
        "joined_during_job": False,
    }
    completed = repo.complete(job.public_id, result)
    assert completed.status == "completed"
    bundle = repo.get_job_bundle(job.public_id)
    assert bundle is not None
    assert {item.sender_type for item in bundle["commenters"]} == {"user", "chat"}
    assert len(bundle["pinned_messages"]) == 1
    assert len(bundle["recent_posts"]) == 1
    cached, hit = repo.create_scan_job("example_news", "@example_news")
    assert hit and cached.public_id == job.public_id

    duplicate, _ = repo.create_scan_job("example_news", "@example_news", force=True)
    repo.fail(duplicate.public_id, "test_failure", "duplicate failed")
    dashboard = repo.list_job_cards()
    assert len(dashboard["cards"]) == 1
    assert dashboard["cards"][0]["job"].public_id == job.public_id
    assert [item.public_id for item in dashboard["history"]] == [duplicate.public_id]


def test_update_group_quality_report_backfills_summary_tags(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    job, _ = repo.create_scan_job("example_group", "@example_group")
    result = {
        "channel": {
            "telegram_chat_id": "-1002",
            "username": "example_group",
            "title": "Example Group",
            "bio": "",
            "public_url": "https://t.me/example_group",
            "linked_chat_id": "",
            "linked_chat_title": "",
            "linked_chat_username": "",
            "member_count": 10,
            "last_post_at": None,
            "last_comment_at": None,
            "last_activity_at": None,
            "last_activity_source": "",
            "posts_scanned": 1,
            "pinned_scanned": 0,
            "comments_fetched": 0,
            "comments_truncated": False,
        },
        "commenters": [],
        "contacts": [],
        "raw_artifacts": [],
        "summary": {"entity_type": "public_group", "auto_tags": ["公开群组"], "warnings": ["AI group quality: ReadTimeout"]},
        "ai_status": "completed",
        "joined_during_job": False,
    }
    repo.complete(job.public_id, result)

    repo.update_group_quality_report(
        job.public_id,
        {
            "classification": "pure_ad_group",
            "is_pure_ad_group": True,
            "confidence": "high",
            "ad_score": 0.9,
            "reasons": ["重复推广"],
            "method": "ai",
            "sampled_messages": 20,
            "messages_scanned": 100,
            "candidate_limit": 50,
        },
        "completed",
        [],
        actor="test",
    )

    updated = repo.get_job(job.public_id)
    summary = updated.result_summary
    assert summary["group_quality"]["classification"] == "pure_ad_group"
    assert summary["group_messages_scanned"] == 100
    assert "纯广告群" in summary["auto_tags"]
    assert "低价值" in summary["auto_tags"]
    assert summary["warnings"] == []


def test_paused_job_cannot_be_completed_by_inflight_worker(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    job, _ = repo.create_scan_job("example_news", "@example_news")
    repo.claim_next_job()
    repo.lifecycle(job.public_id, "pause", "test")
    try:
        repo.complete(job.public_id, {})
        raise AssertionError("paused job must not complete")
    except JobPaused:
        pass
    assert repo.fail(job.public_id, "late_error", "ignored").status == "paused"
    assert repo.rate_limit(job.public_id, 30, "ignored").status == "paused"


def test_private_invite_is_recorded_for_manual_review(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    job = repo.create_manual_review_job("https://t.me/+private", "private invite")
    assert job.status == "manual_review"
    assert job.requested_target == "https://t.me/+private"


def test_chat_export_job_lifecycle(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    job = repo.create_chat_export_job(
        "5551686747",
        target_label="5551686747",
        created_by_username="alice",
        tags="客户A, 取证",
        date_range="recent_7",
        since_at=since,
        max_messages=1000,
        include_media=True,
    )

    assert job.status == "queued"
    assert job.tags == ["客户A", "取证"]
    claimed = repo.claim_next_chat_export_job()
    assert claimed and claimed.public_id == job.public_id
    assert claimed.status == "exporting"
    repo.update_chat_export_progress(job.public_id, progress=45, detail={"message_count": 120, "scanned_count": 150, "batch_count": 2})
    completed = repo.complete_chat_export(
        job.public_id,
        {
            "chat": {"id": -5551686747, "title": "Export Group", "type": {"@type": "chatTypeBasicGroup"}},
            "message_count": 120,
            "scanned_count": 150,
            "batch_count": 2,
            "media_files_count": 3,
            "stop_reason": "max_messages_reached",
            "newest_message_date": "2026-09-05T00:00:00+00:00",
            "oldest_message_date": "2026-09-01T00:00:00+00:00",
            "files": {"csv": "/app/exports/chat-history/jobs/test/file.csv"},
        },
    )

    assert completed.status == "completed"
    assert completed.chat_title == "Export Group"
    assert completed.message_count == 120
    assert completed.media_files_count == 3
    bundle = repo.get_chat_export_bundle(job.public_id)
    assert bundle is not None
    assert [event.event_type for event in bundle["events"]][:1] == ["completed"]


def test_join_quota_is_counted_before_job_completion(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    job, _ = repo.create_scan_job("example_news", "@example_news")
    repo.mark_joined(job.public_id)
    assert repo.joins_today() == 1


def test_explicit_contact_filter_rejects_username_lists() -> None:
    from types import SimpleNamespace

    assert is_explicit_contact_evidence(SimpleNamespace(contact_type="telegram", source_type="bio", evidence="@owner"))
    assert is_explicit_contact_evidence(SimpleNamespace(contact_type="telegram", source_type="post", evidence="Contactez @manager pour commencer"))
    assert not is_explicit_contact_evidence(SimpleNamespace(contact_type="telegram", source_type="post", evidence="Liste des gagnants 📍 @one 📍 @two 📍 @three"))


def test_recovered_job_keeps_collector_account_affinity(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    original, _ = repo.create_scan_job("original_news", "@original_news")
    assert repo.claim_next_job("primary").collector_account == "primary"
    assert repo.recover_stale_jobs() == 1
    later, _ = repo.create_scan_job("later_news", "@later_news")

    standby_job = repo.claim_next_job("standby", "primary_infrastructure_unavailable")
    assert standby_job and standby_job.public_id == later.public_id
    assert standby_job.collector_account == "standby"
    assert repo.get_job(original.public_id).status == "queued"
    assert repo.get_job(original.public_id).collector_account == "primary"


def test_job_and_lead_tags_are_persisted(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    job, _ = repo.create_scan_job(
        "example_news",
        "@example_news",
        created_by_username="alice",
        tags="越南 KOL, 待报价, 越南 KOL",
    )
    assert job.created_by_username == "alice"
    assert job.tags == ["越南 KOL", "待报价"]
    result = {
        "channel": {
            "telegram_chat_id": "-1001",
            "username": "example_news",
            "title": "Example",
            "bio": "Contact @owner",
            "public_url": "https://t.me/example_news",
            "linked_chat_id": "",
            "linked_chat_title": "",
            "linked_chat_username": "",
            "member_count": 0,
            "last_post_at": None,
            "last_comment_at": None,
            "last_activity_at": None,
            "last_activity_source": "",
            "posts_scanned": 1,
            "pinned_scanned": 0,
            "comments_fetched": 1,
            "comments_truncated": False,
        },
        "commenters": [
            {"sender_type": "user", "sender_key": "42", "user_id": "42", "sender_chat_id": "", "username": "reader", "display_name": "Reader", "comment_count": 3, "post_count": 1, "last_comment_at": None, "source_posts": ["https://t.me/example_news/1"], "missing_username_reason": ""},
        ],
        "contacts": [
            {"contact_type": "telegram", "value": "@owner", "source_type": "bio", "source_ref": "bio", "evidence": "Contact @owner", "confidence": "high", "extractor": "rule"},
        ],
        "raw_artifacts": [],
        "summary": {"unique_user_commenters": 1, "public_usernames": 1},
        "ai_status": "degraded",
        "joined_during_job": False,
    }
    repo.complete(job.public_id, result)
    bundle = repo.get_job_bundle(job.public_id)
    commenter_id = bundle["commenters"][0].id
    repo.update_job_tags(job.public_id, "已分配, 9月批次", actor="test")
    repo.update_contact_lead(
        job.public_id,
        "telegram",
        "@owner",
        tags="负责人, 高优先级",
        lead_status="contacted",
        followup_note="已发第一轮消息",
        actor="test",
    )
    repo.update_commenter_lead(
        commenter_id,
        tags="活跃用户",
        lead_status="todo",
        followup_note="检查公开资料",
        actor="test",
    )

    updated = repo.get_job_bundle(job.public_id)
    assert updated["job"].tags == ["已分配", "9月批次"]
    assert updated["contact_groups"][0]["tags"] == ["负责人", "高优先级"]
    assert updated["contact_groups"][0]["lead_status"] == "contacted"
    assert updated["commenters"][0].tags == ["活跃用户"]
    assert updated["commenters"][0].lead_status == "todo"


def test_super_admin_user_management_guards_self_demotion(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    super_admin = repo.create_user("owner", hash_password("owner-strong-password"), "super_admin", "Owner", actor="test")
    staff = repo.create_user("staff", hash_password("staff-strong-password"), "staff", "Staff", actor="test")

    repo.update_user(
        staff.id,
        display_name="Sales Staff",
        role="admin",
        active=True,
        actor_user_id=super_admin.id,
        actor="platform:owner",
    )
    repo.reset_user_password(staff.id, hash_password("new-staff-password"), actor="platform:owner")
    assert repo.get_valid_invite("missing") is None

    try:
        repo.update_user(
            super_admin.id,
            display_name="Owner",
            role="staff",
            active=True,
            actor_user_id=super_admin.id,
            actor="platform:owner",
        )
        raise AssertionError("self demotion should be blocked")
    except ValueError:
        pass


def test_global_leads_merge_contacts_and_audience_across_channels(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)

    def complete_channel(username: str, user_id: str) -> None:
        job, _ = repo.create_scan_job(username, f"@{username}")
        repo.complete(
            job.public_id,
            {
                "channel": {
                    "telegram_chat_id": f"-100{user_id}",
                    "username": username,
                    "title": username.replace("_", " ").title(),
                    "bio": "Contact @owner",
                    "public_url": f"https://t.me/{username}",
                    "linked_chat_id": "",
                    "linked_chat_title": "",
                    "linked_chat_username": "",
                    "member_count": 100,
                    "last_post_at": None,
                    "last_comment_at": None,
                    "last_activity_at": None,
                    "last_activity_source": "",
                    "posts_scanned": 1,
                    "pinned_scanned": 0,
                    "comments_fetched": 1,
                    "comments_truncated": False,
                },
                "commenters": [
                    {
                        "sender_type": "user",
                        "sender_key": user_id,
                        "user_id": user_id,
                        "sender_chat_id": "",
                        "username": "owner",
                        "display_name": "Channel Owner",
                        "comment_count": 2,
                        "post_count": 1,
                        "last_comment_at": None,
                        "source_posts": [f"https://t.me/{username}/1"],
                        "missing_username_reason": "",
                    }
                ],
                "contacts": [
                    {
                        "contact_type": "telegram",
                        "value": "@owner",
                        "source_type": "bio",
                        "source_ref": "bio",
                        "evidence": "Contact @owner",
                        "confidence": "high",
                        "extractor": "rule",
                    }
                ],
                "raw_artifacts": [],
                "summary": {"unique_user_commenters": 1, "public_usernames": 1},
                "ai_status": "completed",
                "joined_during_job": False,
            },
        )

    complete_channel("first_news", "41")
    complete_channel("second_news", "42")

    leads = repo.list_global_leads()
    owner = next(item for item in leads if item["key"] == "telegram:owner")

    assert owner["value"] == "@owner"
    assert owner["display_name"] == "Channel Owner"
    assert owner["entity_kind"] == "user"
    assert owner["lead_kind"] == "both"
    assert owner["source_count"] == 2
    assert owner["evidence_count"] == 4
    assert owner["comment_count"] == 4


def test_v2_discovery_contact_identity_language_override_and_global_lead_update(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    source, _ = repo.create_scan_job("source_news", "@source_news")
    repo.complete(
        source.public_id,
        {
            "channel": {
                "telegram_chat_id": "-1001",
                "username": "source_news",
                "title": "Source News",
                "bio": "Contact @owner_user",
                "public_url": "https://t.me/source_news",
                "linked_chat_id": "",
                "linked_chat_title": "",
                "linked_chat_username": "",
                "member_count": 500,
                "last_post_at": None,
                "last_comment_at": None,
                "last_activity_at": None,
                "last_activity_source": "",
                "posts_scanned": 3,
                "pinned_scanned": 0,
                "comments_fetched": 1,
                "comments_truncated": False,
            },
            "commenters": [
                {
                    "sender_type": "user",
                    "sender_key": "42",
                    "user_id": "42",
                    "sender_chat_id": "",
                    "username": "owner_user",
                    "display_name": "Owner User",
                    "comment_count": 2,
                    "post_count": 1,
                    "last_comment_at": None,
                    "source_posts": ["https://t.me/source_news/1"],
                    "missing_username_reason": "",
                }
            ],
            "contacts": [
                {
                    "contact_type": "telegram_username",
                    "value": "@owner_user",
                    "source_type": "bio",
                    "source_ref": "bio",
                    "evidence": "Contact @owner_user",
                    "confidence": "high",
                    "extractor": "rule",
                    "entity_kind": "user",
                    "entity_title": "Owner User",
                    "is_contactable": True,
                    "classification_status": "completed",
                }
            ],
            "raw_artifacts": [],
            "summary": {
                "language": {"code": "en", "label": "英语", "confidence": 0.8, "method": "local_heuristic"},
                "engagement": {"method": "tdlib_message_metrics", "avg_views": 100},
                "similar_channels": [
                    {
                        "rank": 2,
                        "username": "next_news",
                        "title": "Next News",
                        "member_count": 1200,
                        "public_url": "https://t.me/next_news",
                        "avg_views": 300,
                        "avg_reactions": 7.5,
                        "engagement_posts": 3,
                    }
                ],
            },
            "ai_status": "completed",
            "joined_during_job": False,
        },
    )

    bundle = repo.get_job_bundle(source.public_id)
    group = bundle["contact_groups"][0]
    assert group["entity_kind"] == "user"
    assert group["is_contactable"] is True

    discovery = repo.list_discovery_candidates()
    assert discovery["stats"]["total"] == 1
    assert discovery["stats"]["new"] == 1
    assert discovery["candidates"][0]["avg_views"] == 300
    assert discovery["candidates"][0]["best_rank"] == 2

    repo.update_language_override(source.public_id, "vi", "越南语", actor="test")
    assert repo.get_job(source.public_id).result_summary["language"]["method"] == "manual_override"
    assert repo.get_job(source.public_id).result_summary["language"]["code"] == "vi"
    repo.update_language_override(source.public_id, "", "", actor="test")
    assert repo.get_job(source.public_id).result_summary["language"]["code"] == "en"

    updated = repo.update_global_lead(
        "telegram:owner_user",
        tags="负责人, 高优先级",
        lead_status="contacted",
        followup_note="统一跟进",
        actor="test",
    )
    assert updated == 2
    owner = next(item for item in repo.list_global_leads() if item["key"] == "telegram:owner_user")
    assert owner["entity_kind"] == "user"
    assert owner["lead_status"] == "contacted"
    assert owner["tags"] == ["负责人", "高优先级"]
