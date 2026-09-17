from __future__ import annotations

import importlib
import re

from fastapi.testclient import TestClient


def test_login_and_authenticated_scan_creation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'web.db'}")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-test-password")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        assert client.get("/health").json()["ok"] is True
        assert client.get("/").status_code == 401
        browser_response = client.get(
            "/",
            headers={"accept": "text/html,application/xhtml+xml"},
            follow_redirects=False,
        )
        assert browser_response.status_code == 303
        assert browser_response.headers["location"] == "/login"
        response = client.post("/login", data={"username": "admin", "password": "strong-test-password"}, follow_redirects=False)
        assert response.status_code == 303
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "智能频道研究 v2" in dashboard.text
        assert "全局线索池" in dashboard.text
        assert 'action="/scans/batch"' not in dashboard.text
        assert '<textarea name="target"' in dashboard.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', dashboard.text).group(1)
        created = client.post("/scans", data={"csrf": csrf, "target": "@example_news"}, follow_redirects=False)
        assert created.status_code == 303
        assert "/jobs/" in created.headers["location"]

        batch = client.post(
            "/scans",
            data={
                "csrf": csrf,
                "target": "请处理 https://t.me/batch_one 和 @batch_two\n重复 @BATCH_ONE\nnot-a-channel",
                "tags": "批量测试, 东南亚",
            },
            follow_redirects=False,
        )
        assert batch.status_code == 303
        assert "message=" in batch.headers["location"]
        batch_dashboard = client.get(batch.headers["location"])
        assert batch_dashboard.status_code == 200
        assert "@batch_one" in batch_dashboard.text
        assert "@batch_two" in batch_dashboard.text
        assert "批量测试" in batch_dashboard.text

        invite_page = client.get("/admin/users")
        csrf = re.search(r'name="csrf" value="([^"]+)"', invite_page.text).group(1)
        direct_user = client.post(
            "/admin/users",
            data={"csrf": csrf, "username": "temp_staff", "display_name": "Temp Staff", "role": "staff"},
        )
        assert direct_user.status_code == 200
        temporary_password = re.search(r'临时密码<input value="([^"]+)"', direct_user.text).group(1)
        forced_login = client.post(
            "/login",
            data={"username": "temp_staff", "password": temporary_password},
            follow_redirects=False,
        )
        assert forced_login.status_code == 303
        assert forced_login.headers["location"] == "/account/password?force=1"
        blocked_dashboard = client.get(
            "/",
            headers={"accept": "text/html,application/xhtml+xml"},
            follow_redirects=False,
        )
        assert blocked_dashboard.status_code == 303
        assert blocked_dashboard.headers["location"] == "/account/password?force=1"
        password_page = client.get("/account/password?force=1")
        csrf = re.search(r'name="csrf" value="([^"]+)"', password_page.text).group(1)
        changed = client.post(
            "/account/password",
            data={
                "csrf": csrf,
                "current_password": temporary_password,
                "new_password": "member-new-password",
                "confirm_password": "member-new-password",
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303
        assert changed.headers["location"].startswith("/?message=")
        assert client.get("/").status_code == 200

        client.post("/login", data={"username": "admin", "password": "strong-test-password"}, follow_redirects=False)
        invite_page = client.get("/admin/users")
        csrf = re.search(r'name="csrf" value="([^"]+)"', invite_page.text).group(1)
        invitation = client.post("/admin/invites", data={"csrf": csrf, "role": "staff"})
        assert invitation.status_code == 200
        invite_path = re.search(r'value="http://127\.0\.0\.1:8848(/join/[^"]+)"', invitation.text).group(1)
        joined = client.post(
            invite_path,
            data={"username": "new_staff", "display_name": "New Staff", "password": "another-strong-password"},
            follow_redirects=False,
        )
        assert joined.status_code == 303
        assert joined.headers["location"] == "/login"
        assert client.get(invite_path).status_code == 404

        dashboard = client.get("/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', dashboard.text).group(1)
        manual = client.post(
            "/scans",
            data={"csrf": csrf, "target": "https://t.me/+privateInvite"},
            follow_redirects=False,
        )
        assert manual.status_code == 303
        detail = client.get(manual.headers["location"])
        assert "manual_review" in detail.text
        assert "私密邀请" in detail.text


def test_untrusted_host_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'host.db'}")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app, base_url="http://evil.example") as client:
        assert client.get("/health").status_code == 400


def test_ready_ai_requires_enabled_key_and_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ready.db'}")
    monkeypatch.setenv("AI_ENABLED", "1")
    monkeypatch.setenv("AI_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("AI_API_KEY_FILE", raising=False)
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        body = client.get("/ready").json()
        assert body["ai_enabled"] is True
        assert body["ai_configured"] is False

    monkeypatch.setenv("AI_API_KEY", "test-key")
    web = importlib.reload(web)
    with TestClient(web.app) as client:
        body = client.get("/ready").json()
        assert body["ai_enabled"] is True
        assert body["ai_configured"] is True


def test_chat_export_page_creates_export_jobs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'exports.db'}")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-test-password")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        client.post("/login", data={"username": "admin", "password": "strong-test-password"}, follow_redirects=False)
        page = client.get("/chat-exports")
        assert page.status_code == 200
        assert "聊天历史导出" in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

        batch = client.post(
            "/chat-exports",
            data={
                "csrf": csrf,
                "targets": "5551686747\n@ExampleGroup\n重复 @examplegroup",
                "tags": "客户A, 取证",
                "date_range": "recent_30",
                "max_messages_mode": "5000",
            },
            follow_redirects=False,
        )
        assert batch.status_code == 303
        assert batch.headers["location"].startswith("/chat-exports?message=")
        listing = client.get(batch.headers["location"])
        assert "5551686747" in listing.text
        assert "@ExampleGroup" in listing.text
        assert "5000" in listing.text
        assert "取证" in listing.text

        single = client.post(
            "/chat-exports",
            data={
                "csrf": csrf,
                "targets": "5352295939",
                "date_range": "recent_7",
                "max_messages_mode": "custom",
                "max_messages_custom": "1200",
                "include_media": "1",
            },
            follow_redirects=False,
        )
        assert single.status_code == 303
        assert "/chat-exports/" in single.headers["location"]
        detail = client.get(single.headers["location"])
        assert "最近一周" in detail.text
        assert "1200" in detail.text
        assert "collector" in detail.text
        assert "是" in detail.text


def test_dashboard_and_detail_render_group_quality(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'quality-web.db'}")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-test-password")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        client.post("/login", data={"username": "admin", "password": "strong-test-password"}, follow_redirects=False)
        job, _ = web.repository.create_scan_job("quality_group", "@quality_group")
        web.repository.complete(
            job.public_id,
            {
                "channel": {
                    "telegram_chat_id": "-1003",
                    "username": "quality_group",
                    "title": "Quality Group",
                    "bio": "",
                    "public_url": "https://t.me/quality_group",
                    "linked_chat_id": "",
                    "linked_chat_title": "",
                    "linked_chat_username": "",
                    "member_count": 100,
                    "last_post_at": None,
                    "last_comment_at": None,
                    "last_activity_at": None,
                    "last_activity_source": "",
                    "posts_scanned": 80,
                    "pinned_scanned": 0,
                    "comments_fetched": 0,
                    "comments_truncated": False,
                },
                "commenters": [],
                "contacts": [],
                "raw_artifacts": [],
                "summary": {
                    "entity_type": "public_group",
                    "skip_reason": "pure_ad_group",
                    "group_messages_scanned": 80,
                    "group_active_user_limit": 50,
                    "auto_tags": ["公开群组", "纯广告群", "低价值"],
                    "group_quality": {
                        "classification": "pure_ad_group",
                        "confidence": "high",
                        "ad_score": 0.95,
                        "method": "ai",
                        "sampled_messages": 40,
                        "reasons": ["重复推广"],
                    },
                },
                "ai_status": "completed",
                "joined_during_job": False,
            },
        )

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "群质量" in dashboard.text
        assert "纯广告群" in dashboard.text
        assert "95%" in dashboard.text

        detail = client.get(f"/jobs/{job.public_id}")
        assert detail.status_code == 200
        assert "GROUP QUALITY" in detail.text
        assert "已跳过用户拓展" in detail.text
        assert "这是一份旧版扫描结果" in detail.text
        assert f'action="/jobs/{job.public_id}/v2/rescan"' in detail.text


def test_protected_site_api_uses_proxy_and_platform_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'site-api.db'}")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-test-password")
    monkeypatch.setenv("SITE_PROXY_TOKEN", "test-proxy-token")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        assert client.get("/api/site/jobs").status_code == 401
        proxy_headers = {"x-alice-proxy-token": "test-proxy-token"}
        login = client.post(
            "/api/site/session",
            headers=proxy_headers,
            json={"username": "admin", "password": "strong-test-password"},
        )
        assert login.status_code == 200
        body = login.json()
        auth_headers = {
            **proxy_headers,
            "authorization": f"Bearer {body['session_token']}",
            "x-csrf-token": body["csrf_token"],
        }
        created = client.post("/api/site/jobs", headers=auth_headers, json={"target": "@example_news"})
        assert created.status_code == 200
        assert created.json()["job"]["username"] == "example_news"
        listing = client.get("/api/site/jobs", headers=auth_headers)
        assert listing.status_code == 200
        assert listing.json()["cards"][0]["job"]["username"] == "example_news"

        preview = client.post(
            "/api/site/jobs/preview",
            headers=auth_headers,
            json={"targets": "@example_news\nhttps://t.me/second_news\n@EXAMPLE_NEWS"},
        )
        assert preview.status_code == 200
        assert [item["disposition"] for item in preview.json()["items"]] == ["active", "new"]
        assert len(preview.json()["duplicates"]) == 1

        batch = client.post(
            "/api/site/jobs/batch",
            headers=auth_headers,
            json={"targets": "@example_news\nhttps://t.me/second_news\n@EXAMPLE_NEWS"},
        )
        assert batch.status_code == 200
        assert batch.json()["counts"] == {
            "accepted": 2, "created": 1, "reused": 1, "duplicates": 1, "invalid": 0,
        }


def test_csv_export_cells_are_formula_safe() -> None:
    import app.web as web

    assert web.csv_safe("=HYPERLINK(\"bad\")") == "'=HYPERLINK(\"bad\")"
    assert web.csv_safe("normal") == "normal"


def test_shanghai_time_format() -> None:
    from datetime import datetime, timezone
    import app.web as web

    value = datetime(2026, 8, 30, 21, 21, 21, tzinfo=timezone.utc)
    assert web.format_shanghai(value) == "2026-08-31 05:21:21"


def test_group_quality_view_marks_pure_ad_group() -> None:
    import app.web as web

    view = web.group_quality_view(
        {
            "entity_type": "public_group",
            "skip_reason": "pure_ad_group",
            "group_messages_scanned": 5000,
            "group_active_user_limit": 50,
            "group_quality": {
                "classification": "pure_ad_group",
                "confidence": "high",
                "ad_score": 0.87,
                "method": "ai",
                "sampled_messages": 80,
                "reasons": ["重复推广", "缺少真实对话"],
            },
        }
    )

    assert view["visible"] is True
    assert view["label"] == "纯广告群"
    assert view["tone"] == "danger"
    assert view["score_label"] == "87%"
    assert view["skip_reason_label"] == "已跳过用户拓展"
    assert view["messages_scanned"] == 5000
    assert view["candidate_limit"] == 50


def test_group_quality_view_handles_old_group_result_without_quality() -> None:
    import app.web as web

    view = web.group_quality_view({"entity_type": "public_group"})

    assert view["visible"] is True
    assert view["label"] == "待判断"
    assert view["score_label"] == "待重扫"
    assert "旧任务" in view["status_text"]


def test_dual_funnel_separates_contacts_from_audience_candidates() -> None:
    from types import SimpleNamespace
    import app.web as web

    contact = SimpleNamespace(contact_type="telegram", source_type="bio", source_ref="bio", evidence="Contact @owner")
    bundle = {
        "contact_groups": [
            {"contact_type": "telegram", "value": "@owner", "source_types": ["bio"], "extractors": ["ai"], "evidence_count": 1, "confidence": "high", "examples": [contact]},
            {"contact_type": "telegram_username", "value": "@owner", "source_types": ["bio"], "extractors": ["rule"], "evidence_count": 1, "confidence": "high", "examples": [contact]},
        ],
        "commenters": [
            SimpleNamespace(sender_type="user", username="owner", user_id="1", display_name="Owner", comment_count=5, post_count=3, source_posts=["https://t.me/example/1"]),
            SimpleNamespace(sender_type="user", username="fan", user_id="2", display_name="Fan", comment_count=12, post_count=4, source_posts=["https://t.me/example/2"]),
            SimpleNamespace(sender_type="chat", username="", user_id="", display_name="Anonymous", comment_count=1, post_count=1, source_posts=[]),
        ],
        "cross_channel_counts": {"1": 1, "2": 2},
    }
    view = web.detail_view(bundle)
    assert len(view["direct_contact_groups"]) == 1
    assert [row["item"].username for row in view["contact_overlap_commenters"]] == ["owner"]
    assert [row["item"].username for row in view["audience_candidates"]] == ["fan"]
    assert view["audience_candidates"][0]["priority"] == "高"
    assert view["audience_candidates"][0]["cross_channel_count"] == 2


def test_v2_insights_global_leads_and_similar_channel_queue(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'v2-web.db'}")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-test-password")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        client.post("/login", data={"username": "admin", "password": "strong-test-password"}, follow_redirects=False)
        job, _ = web.repository.create_scan_job("source_news", "@source_news")
        web.repository.complete(
            job.public_id,
            {
                "channel": {
                    "telegram_chat_id": "-1001",
                    "username": "source_news",
                    "title": "Source News",
                    "bio": "Contact @owner",
                    "public_url": "https://t.me/source_news",
                    "linked_chat_id": "",
                    "linked_chat_title": "",
                    "linked_chat_username": "",
                    "member_count": 5000,
                    "last_post_at": None,
                    "last_comment_at": None,
                    "last_activity_at": None,
                    "last_activity_source": "",
                    "posts_scanned": 20,
                    "pinned_scanned": 1,
                    "comments_fetched": 0,
                    "comments_truncated": False,
                },
                "commenters": [],
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
                "summary": {
                    "entity_type": "channel",
                    "language": {"code": "en", "label": "英语", "confidence": 0.91},
                    "engagement": {"avg_views": 1200, "avg_reactions": 8.0, "avg_replies": 2.0, "engagement_rate": 0.83, "posts_per_week": 7.0, "posts_last_30d": 20, "posts_with_views": 20, "method": "tdlib_message_metrics"},
                    "similar_channels_status": "completed",
                    "similar_channels": [
                        {"rank": 1, "username": "next_news", "title": "Next News", "member_count": 4000, "public_url": "https://t.me/next_news"}
                    ],
                },
                "ai_status": "completed",
                "joined_during_job": False,
            },
        )

        detail = client.get(f"/jobs/{job.public_id}")
        assert detail.status_code == 200
        assert "语种与互动画像" in detail.text
        assert "英语" in detail.text
        assert "@next_news" in detail.text
        assert "人工纠正语种" in detail.text
        assert "v2 画像已生成" not in detail.text
        assert "这是一份旧版扫描结果" not in detail.text

        leads = client.get("/leads")
        assert leads.status_code == 200
        assert "全局线索池" in leads.text
        assert "@owner" in leads.text
        assert "统一维护" in leads.text

        csrf = re.search(r'name="csrf" value="([^"]+)"', detail.text).group(1)
        language_update = client.post(
            f"/jobs/{job.public_id}/language",
            data={"csrf": csrf, "language_code": "vi"},
            follow_redirects=False,
        )
        assert language_update.status_code == 303
        assert web.repository.get_job(job.public_id).result_summary["language"]["code"] == "vi"

        lead_update = client.post(
            "/leads/update",
            data={"csrf": csrf, "key": "telegram:owner", "lead_status": "todo", "tags": "负责人", "followup_note": "核验身份"},
            follow_redirects=False,
        )
        assert lead_update.status_code == 303
        assert next(item for item in web.repository.list_global_leads() if item["key"] == "telegram:owner")["lead_status"] == "todo"

        discovery = client.get("/discover")
        assert discovery.status_code == 200
        assert "频道发现池" in discovery.text
        assert "@next_news" in discovery.text
        discovery_queue = client.post(
            "/discover/scan",
            data={"csrf": csrf, "usernames": "next_news"},
            follow_redirects=False,
        )
        assert discovery_queue.status_code == 303

        queued = client.post(
            f"/jobs/{job.public_id}/similar/scan",
            data={"csrf": csrf, "usernames": "next_news"},
            follow_redirects=False,
        )
        assert queued.status_code == 303
        assert queued.headers["location"].startswith("/?message=")
        assert web.repository.get_job_bundle(job.public_id)["job"].status == "completed"
        assert any(item.normalized_username == "next_news" for item in web.repository.list_jobs())

        rescanned = client.post(
            f"/jobs/{job.public_id}/v2/rescan",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert rescanned.status_code == 303
        assert rescanned.headers["location"].startswith("/jobs/")
        assert rescanned.headers["location"] != f"/jobs/{job.public_id}"


def test_admin_can_batch_upgrade_legacy_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'v2-batch-web.db'}")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-test-password")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        client.post("/login", data={"username": "admin", "password": "strong-test-password"}, follow_redirects=False)
        old, _ = web.repository.create_scan_job("legacy_news", "@legacy_news")
        web.repository.complete(
            old.public_id,
            {
                "channel": {
                    "telegram_chat_id": "-1009", "username": "legacy_news", "title": "Legacy", "bio": "",
                    "public_url": "https://t.me/legacy_news", "linked_chat_id": "", "linked_chat_title": "",
                    "linked_chat_username": "", "member_count": 10, "last_post_at": None, "last_comment_at": None,
                    "last_activity_at": None, "last_activity_source": "", "posts_scanned": 1, "pinned_scanned": 0,
                    "comments_fetched": 0, "comments_truncated": False,
                },
                "commenters": [], "contacts": [], "raw_artifacts": [], "summary": {},
                "ai_status": "degraded", "joined_during_job": False,
            },
        )
        blocked_old, _ = web.repository.create_scan_job("private_news", "@private_news")
        web.repository.complete(
            blocked_old.public_id,
            {
                "channel": {
                    "telegram_chat_id": "-1010", "username": "private_news", "title": "Private", "bio": "",
                    "public_url": "https://t.me/private_news", "linked_chat_id": "", "linked_chat_title": "",
                    "linked_chat_username": "", "member_count": 10, "last_post_at": None, "last_comment_at": None,
                    "last_activity_at": None, "last_activity_source": "", "posts_scanned": 1, "pinned_scanned": 0,
                    "comments_fetched": 0, "comments_truncated": False,
                },
                "commenters": [], "contacts": [], "raw_artifacts": [], "summary": {},
                "ai_status": "degraded", "joined_during_job": False,
            },
        )
        blocked_retry, _ = web.repository.create_scan_job("private_news", "@private_news", force=True)
        web.repository.fail(blocked_retry.public_id, "TdlibError", "getChatHistory failed: CHANNEL_PRIVATE")
        dashboard = client.get("/")
        assert "升级全部旧目标" in dashboard.text
        assert "无法升级 · 访问受限" in dashboard.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', dashboard.text).group(1)
        response = client.post("/v2/rescan-pending", data={"csrf": csrf}, follow_redirects=False)
        assert response.status_code == 303
        jobs = [item for item in web.repository.list_jobs() if item.normalized_username == "legacy_news"]
        assert len(jobs) == 2
        assert jobs[0].status == "queued"
        assert "v2 批量升级" in jobs[0].tags
        blocked_jobs = [item for item in web.repository.list_jobs() if item.normalized_username == "private_news"]
        assert len(blocked_jobs) == 2
