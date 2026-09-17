from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config import load_settings
from app.tdlib_adapter import ChannelPreprocessor, _auto_tags


def test_run_session_returns_complete_result_payload() -> None:
    source = textwrap.dedent(inspect.getsource(ChannelPreprocessor._run_session))
    tree = ast.parse(source)
    required_keys = {"channel", "commenters", "contacts", "raw_artifacts", "summary", "ai_status", "joined_during_job"}
    returned_dict_keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        for key in node.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert required_keys <= returned_dict_keys


class FakeSession:
    def invoke(self, method: str, **kwargs):
        if method == "getMessageProperties":
            return {"can_get_message_thread": True}
        if method == "getMessageThread":
            assert kwargs == {"chat_id": -10, "message_id": 100}
            return {"chat_id": -20, "message_thread_id": 500}
        if method == "getMessageThreadHistory":
            assert kwargs["chat_id"] == -20
            assert kwargs["message_id"] == 500
            if kwargs["from_message_id"]:
                return {"messages": []}
            return {
                "messages": [
                    {"id": 500, "chat_id": -20, "is_channel_post": True, "sender_id": {"@type": "messageSenderChat", "chat_id": -10}, "date": 100},
                    {"id": 90, "content": {"@type": "messageChatAddMembers"}, "sender_id": {"@type": "messageSenderUser", "user_id": 1}, "date": 110},
                    {"id": 80, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 1}, "date": 120},
                    {"id": 70, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderChat", "chat_id": -20}, "date": 130},
                ]
            }
        if method == "getUser":
            return {"first_name": "Alice", "last_name": "Reader", "usernames": {"active_usernames": ["alice_reader"]}}
        if method == "getChat":
            return {"title": "Anonymous Group", "type": {"@type": "chatTypeSupergroup", "supergroup_id": 20}}
        if method == "getSupergroup":
            return {"usernames": {"active_usernames": []}}
        raise AssertionError(method)


def test_commenter_scan_excludes_source_post_and_service_messages() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    scanner.settings = replace(load_settings(), max_comments_per_post=1000, max_comments_per_job=10000)
    commenters, summary = scanner._commenters(
        FakeSession(),
        -10,
        [{"message": {"id": 100, "interaction_info": {"reply_info": {"reply_count": 2}}}, "message_id": 100, "link": "https://t.me/example/1", "text": ""}],
    )
    assert summary["comments_fetched"] == 2
    assert len(commenters) == 2
    assert {item["sender_type"] for item in commenters} == {"user", "chat"}
    user = next(item for item in commenters if item["sender_type"] == "user")
    assert user["username"] == "alice_reader"
    assert user["comment_count"] == 1


class UnavailableThreadSession:
    def invoke(self, method: str, **kwargs):
        if method == "getMessageProperties":
            return {"can_get_message_thread": True}
        if method == "getMessageThread":
            raise RuntimeError("thread unavailable")
        raise AssertionError(method)


def test_unavailable_comment_thread_does_not_fail_whole_scan() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    scanner.settings = replace(load_settings(), max_comments_per_post=1000, max_comments_per_job=10000)
    commenters, summary = scanner._commenters(
        UnavailableThreadSession(),
        -10,
        [{"message": {"id": 100, "interaction_info": {"reply_info": {"reply_count": 2}}}, "message_id": 100, "link": "https://t.me/example/1", "text": ""}],
    )
    assert commenters == []
    assert summary["unavailable_threads"] == 1
    assert summary["truncated"] is True


class FakeHistorySession:
    def __init__(self):
        self.calls = 0

    def invoke(self, method: str, **kwargs):
        assert method == "getChatHistory"
        self.calls += 1
        if self.calls == 1:
            return {
                "messages": [
                    {"id": 30, "is_channel_post": True, "content": {"@type": "messageChatAddMembers"}},
                    {"id": 20, "is_channel_post": True, "content": {"@type": "messageText"}},
                ]
            }
        if self.calls == 2:
            return {"messages": [{"id": 10, "is_channel_post": True, "content": {"@type": "messagePhoto"}}]}
        return {"messages": []}


def test_history_counts_only_real_channel_posts() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    posts = scanner._history(FakeHistorySession(), -10, 2)
    assert [item["id"] for item in posts] == [20, 10]


class FakeGroupHistorySession:
    def __init__(self):
        self.calls = 0

    def invoke(self, method: str, **kwargs):
        assert method == "getChatHistory"
        self.calls += 1
        if self.calls == 1:
            return {
                "messages": [
                    {"id": 30, "is_channel_post": False, "content": {"@type": "messageChatAddMembers"}},
                    {"id": 20, "is_channel_post": False, "content": {"@type": "messageText"}},
                    {"id": 10, "is_channel_post": False, "content": {"@type": "messagePhoto"}},
                ]
            }
        return {"messages": []}


def test_history_counts_public_group_messages() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    posts = scanner._history(FakeGroupHistorySession(), -10, 3, channel_only=False)
    assert [item["id"] for item in posts] == [20, 10]


class FakeGroupSenderSession:
    def invoke(self, method: str, **kwargs):
        if method == "getUser":
            if kwargs["user_id"] == 1:
                return {"first_name": "Alice", "last_name": "Reader", "usernames": {"active_usernames": ["alice_reader"]}}
            return {"first_name": "Bob", "last_name": "", "usernames": {"active_usernames": []}}
        raise AssertionError(method)


def test_public_group_message_senders_become_audience_candidates() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    scanner.settings = replace(load_settings(), max_posts=100, max_comments_per_post=1000, max_comments_per_job=10000)
    scanner.repository = SimpleNamespace(assert_runnable=lambda _public_id: None)
    commenters, summary = scanner._message_senders(
        FakeGroupSenderSession(),
        [
            {
                "message": {"id": 20, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 1}, "date": 120},
                "message_id": 20,
                "link": "https://t.me/group/1",
                "text": "hello",
            },
            {
                "message": {"id": 10, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 1}, "date": 130},
                "message_id": 10,
                "link": "https://t.me/group/2",
                "text": "again",
            },
            {
                "message": {"id": 5, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 2}, "date": 140},
                "message_id": 5,
                "link": "https://t.me/group/3",
                "text": "no username",
            },
        ],
    )

    assert summary["comments_fetched"] == 3
    assert summary["unavailable_threads"] == 0
    assert len(commenters) == 2
    assert commenters[0]["username"] == "alice_reader"
    assert commenters[0]["comment_count"] == 2
    assert commenters[0]["post_count"] == 2
    assert commenters[1]["missing_username_reason"] == "no_public_username"


class FakeGroupSenderLimitSession:
    def invoke(self, method: str, **kwargs):
        if method == "getUser":
            user_id = kwargs["user_id"]
            if user_id == 1:
                return {"first_name": "No", "last_name": "Username", "usernames": {"active_usernames": []}}
            if user_id == 2:
                return {"first_name": "Public", "last_name": "User", "usernames": {"active_usernames": ["public_user"]}}
            raise AssertionError("sender resolution should stop after the public candidate limit")
        raise AssertionError(method)


def test_public_group_message_senders_limit_public_candidates_only() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    scanner.repository = SimpleNamespace(assert_runnable=lambda _public_id: None)
    commenters, summary = scanner._message_senders(
        FakeGroupSenderLimitSession(),
        [
            {
                "message": {"id": 30, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 1}, "date": 130},
                "message_id": 30,
                "link": "https://t.me/group/30",
                "text": "first ad",
            },
            {
                "message": {"id": 20, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 1}, "date": 120},
                "message_id": 20,
                "link": "https://t.me/group/20",
                "text": "again",
            },
            {
                "message": {"id": 10, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 2}, "date": 110},
                "message_id": 10,
                "link": "https://t.me/group/10",
                "text": "real question",
            },
            {
                "message": {"id": 5, "content": {"@type": "messageText"}, "sender_id": {"@type": "messageSenderUser", "user_id": 3}, "date": 100},
                "message_id": 5,
                "link": "https://t.me/group/5",
                "text": "should not resolve",
            },
        ],
        candidate_limit=1,
        history_limit=4,
    )

    assert [item["username"] for item in commenters] == ["public_user"]
    assert summary["comments_fetched"] == 4
    assert summary["public_candidates"] == 1
    assert summary["skipped_non_public"] == 1
    assert summary["candidate_limit"] == 1
    assert summary["truncated"] is True


def test_auto_tags_mark_inactive_pure_ad_group() -> None:
    tags = _auto_tags(
        entity_type="public_group",
        last_activity_at=None,
        inactive_days=90,
        group_quality={"classification": "pure_ad_group"},
    )
    assert tags == ["公开群组", "无近期内容", "低优先级", "纯广告群", "低价值"]


def test_current_telegram_session_must_match_proxy_exit_fingerprint() -> None:
    raw_ip = "203.0.113.10"
    fingerprint = hashlib.sha256(raw_ip.encode("utf-8")).hexdigest()[:24]
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    scanner.account = SimpleNamespace(proxy={"exit_fingerprint": fingerprint})

    class ActiveSession:
        def invoke(self, method: str, **kwargs):
            assert method == "getActiveSessions"
            return {"sessions": [{"is_current": True, "ip_address": raw_ip}]}

    scanner._verify_current_session_exit(ActiveSession())
    scanner.account = SimpleNamespace(proxy={"exit_fingerprint": "0" * 24})
    with pytest.raises(RuntimeError, match="出口"):
        scanner._verify_current_session_exit(ActiveSession())


class FakeSimilarChannelsSession:
    def invoke(self, method: str, **kwargs):
        if method == "getChatSimilarChats":
            assert kwargs == {"chat_id": -100}
            return {"chat_ids": [-200, -300, -400, -200]}
        if method == "getChat":
            chat_id = kwargs["chat_id"]
            if chat_id == -400:
                raise RuntimeError("unavailable")
            return {
                "id": chat_id,
                "title": "Related" if chat_id == -200 else "Public group",
                "type": {
                    "@type": "chatTypeSupergroup",
                    "supergroup_id": abs(chat_id),
                    "is_channel": chat_id == -200,
                },
            }
        if method == "getSupergroup":
            return {
                "member_count": 1234,
                "usernames": {"active_usernames": ["related_news"]},
            }
        raise AssertionError(method)


def test_similar_channels_keeps_public_channels_and_deduplicates() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)

    recommendations, status = scanner._similar_channels(
        FakeSimilarChannelsSession(),
        -100,
        "source_news",
    )

    assert status == "completed"
    assert recommendations == [
        {
            "rank": 1,
            "telegram_chat_id": "-200",
            "username": "related_news",
            "title": "Related",
            "member_count": 1234,
            "public_url": "https://t.me/related_news",
            "avg_views": 0,
            "avg_reactions": 0.0,
            "engagement_posts": 0,
            "engagement_status": "unavailable",
        }
    ]


def test_similar_channels_degrades_when_method_is_unavailable() -> None:
    class UnsupportedSession:
        def invoke(self, method: str, **kwargs):
            raise RuntimeError("method not found")

    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    assert scanner._similar_channels(UnsupportedSession(), -100, "source_news") == ([], "unavailable")


class FakeContactClassificationSession:
    def invoke(self, method: str, **kwargs):
        username = kwargs.get("username", "")
        if method == "searchPublicChat":
            if username == "owner_user":
                return {"title": "Owner", "type": {"@type": "chatTypePrivate", "user_id": 1}}
            if username == "support_bot":
                return {"title": "Support", "type": {"@type": "chatTypePrivate", "user_id": 2}}
            if username == "brand_news":
                return {"title": "Brand", "type": {"@type": "chatTypeSupergroup", "is_channel": True}}
            if username == "public_group":
                return {"title": "Community", "type": {"@type": "chatTypeSupergroup", "is_channel": False}}
            raise RuntimeError("not found")
        if method == "getUser":
            return {"type": {"@type": "userTypeBot" if kwargs["user_id"] == 2 else "userTypeRegular"}}
        raise AssertionError(method)


def test_contact_classification_only_marks_real_users_contactable() -> None:
    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    contacts = scanner._classify_contacts(
        FakeContactClassificationSession(),
        [
            {"contact_type": "telegram_username", "value": "@owner_user"},
            {"contact_type": "telegram", "value": "https://t.me/support_bot"},
            {"contact_type": "telegram_username", "value": "@brand_news"},
            {"contact_type": "telegram_username", "value": "@public_group"},
            {"contact_type": "email", "value": "team@example.com"},
        ],
    )

    assert [item["entity_kind"] for item in contacts] == ["user", "bot", "channel", "group", "unknown"]
    assert [item["is_contactable"] for item in contacts] == [True, False, False, False, False]
    assert contacts[0]["classification_status"] == "completed"
    assert contacts[-1]["classification_status"] == "not_applicable"


def test_similar_channel_engagement_uses_three_recent_messages() -> None:
    class EngagementSession:
        def invoke(self, method: str, **kwargs):
            assert method == "getChatHistory"
            assert kwargs["limit"] == 3
            return {
                "messages": [
                    {"interaction_info": {"view_count": 100, "reactions": {"total_count": 4}}},
                    {"interaction_info": {"view_count": 200, "reactions": {"total_count": 8}}},
                    {"interaction_info": {"view_count": 300, "reactions": {"total_count": 0}}},
                ]
            }

    scanner = ChannelPreprocessor.__new__(ChannelPreprocessor)
    metrics = scanner._similar_channel_engagement(EngagementSession(), -200)
    assert metrics == {
        "avg_views": 200,
        "avg_reactions": 4.0,
        "engagement_posts": 3,
        "engagement_status": "completed",
    }
