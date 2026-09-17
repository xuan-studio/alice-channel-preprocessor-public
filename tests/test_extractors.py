from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from app.ai_client import (
    ContactAIClient,
    GroupQualityAIClient,
    group_quality_heuristic,
    validate_ai_report,
    validate_ai_signals,
    validate_group_quality_report,
)
from app.config import load_settings
from app.extractors import extract_contacts, normalize_channel_target, validate_ai_contacts


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Example_News", "example_news"),
        ("https://t.me/Example_News/42", "example_news"),
        ("telegram.me/example_news", "example_news"),
    ],
)
def test_normalize_public_channel(raw: str, expected: str) -> None:
    assert normalize_channel_target(raw) == expected


@pytest.mark.parametrize("raw", ["https://example.com/a", "https://t.me/+secret", "", "@no"])
def test_reject_non_public_targets(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_channel_target(raw)


def test_rule_extraction_keeps_only_explicit_contacts() -> None:
    text = "合作请联系 @Alice_ops 或 team@example.com，官网 https://example.com/contact，微信: alice_team"
    found = extract_contacts(text, source_type="bio", source_ref="bio")
    values = {(item.contact_type, item.value) for item in found}
    assert ("telegram_username", "@Alice_ops") in values
    assert ("email", "team@example.com") in values
    assert ("website", "https://example.com/contact") in values
    assert ("wechat", "alice_team") in values


def test_ai_hallucination_is_rejected() -> None:
    sources = {"post:1": "Business contact: @literal_name"}
    candidates = [
        {"contact_type": "telegram_username", "value": "@literal_name", "source_ref": "post:1"},
        {"contact_type": "email", "value": "invented@example.com", "source_ref": "post:1"},
    ]
    valid = validate_ai_contacts(candidates, sources)
    assert [item["value"] for item in valid] == ["@literal_name"]


def test_rule_extraction_rejects_scam_warning_mentions_and_boost_links() -> None:
    text = "Warning: be careful, scammer @BadActor. https://t.me/boost/SomeChannel"
    found = extract_contacts(text, source_type="post", source_ref="post:1")
    assert found == []


def test_rule_extraction_keeps_explicit_group_invite() -> None:
    text = "Join our VIP group here: https://t.me/+AbCdEf123"
    found = extract_contacts(text, source_type="pinned", source_ref="post:2")
    assert [(item.contact_type, item.value) for item in found] == [
        ("discussion_group", "https://t.me/+AbCdEf123")
    ]


def test_ai_signals_require_literal_evidence() -> None:
    sources = {"post:1": "Join the VIP group by messaging @owner today."}
    candidates = [
        {"category": "contact", "finding": "公开私聊入口", "source_ref": "post:1", "evidence": "messaging @owner", "importance": "high"},
        {"category": "offer", "finding": "虚构优惠", "source_ref": "post:1", "evidence": "50% discount", "importance": "high"},
    ]
    valid = validate_ai_signals(candidates, sources)
    assert len(valid) == 1
    assert valid[0]["finding"] == "公开私聊入口"


def test_ai_report_is_bounded() -> None:
    report = validate_ai_report({"executive_summary": "x" * 3000, "risk_notes": ["one", "two"]})
    assert len(report["executive_summary"]) == 2000
    assert report["risk_notes"] == ["one", "two"]


def test_group_quality_heuristic_marks_pure_ad_group() -> None:
    messages = [
        {"text": f"VIP casino bonus deposit now https://t.me/deal{i % 2} referral promo win"}
        for i in range(12)
    ]
    report = group_quality_heuristic(messages)
    assert report["classification"] == "pure_ad_group"
    assert report["is_pure_ad_group"] is True
    assert report["ad_score"] >= 0.72


def test_group_quality_heuristic_keeps_discussion_groups_open() -> None:
    messages = [
        {"text": "请问这个 API 怎么配置？谢谢"},
        {"text": "有人测试过今天的新模型吗？"},
        {"text": "I tried it yesterday, latency was ok."},
        {"text": "那费用怎么计算？"},
        {"text": "thanks, this helps"},
        {"text": "how do you handle retries?"},
        {"text": "收到，我晚点再试一下"},
        {"text": "yes it works for me"},
    ]
    report = group_quality_heuristic(messages)
    assert report["is_pure_ad_group"] is False
    assert report["classification"] == "mixed"


def test_validate_group_quality_report_bounds_ai_payload() -> None:
    fallback = group_quality_heuristic([])
    report = validate_group_quality_report(
        {
            "classification": "active_discussion",
            "confidence": "high",
            "ad_score": "1.8",
            "reasons": ["多人真实问答", "重复广告少"],
        },
        fallback,
    )
    assert report["classification"] == "active_discussion"
    assert report["is_pure_ad_group"] is False
    assert report["ad_score"] == 1.0
    assert report["method"] == "ai"


def test_ai_client_sends_application_user_agent(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"contacts":[]}'}}]}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr("app.ai_client.httpx.post", fake_post)
    settings = replace(
        load_settings(),
        ai_enabled=True,
        ai_base_url="https://api.example.test/v1",
        ai_api_key="test-key",
        ai_model="test-model",
    )

    contacts, status, errors = ContactAIClient(settings).analyze(
        [{"source_type": "post", "source_ref": "post:1", "text": "no contact"}]
    )

    assert contacts == []
    assert status == "completed"
    assert errors == []
    assert calls[0][1]["headers"]["User-Agent"] == "AliceChannelPreprocessor/0.1"
    assert calls[0][1]["headers"]["Accept"] == "application/json"


def test_ai_client_retries_timeout_once(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"contacts":[]}'}}]}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out")
        return FakeResponse()

    monkeypatch.setattr("app.ai_client.httpx.post", fake_post)
    monkeypatch.setattr("app.ai_client.time.sleep", lambda _seconds: None)
    settings = replace(
        load_settings(),
        ai_enabled=True,
        ai_base_url="https://api.example.test/v1",
        ai_api_key="test-key",
        ai_model="test-model",
    )

    contacts, status, errors = ContactAIClient(settings).analyze(
        [{"source_type": "post", "source_ref": "post:1", "text": "no contact"}]
    )

    assert contacts == []
    assert status == "completed"
    assert errors == []
    assert len(calls) == 2


def test_group_quality_ai_retries_and_keeps_ai_result(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            content = '{"classification":"pure_ad_group","confidence":"high","ad_score":0.91,"reasons":["重复推广", "缺少真实对话"]}'
            return {"choices": [{"message": {"content": content}}]}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out")
        return FakeResponse()

    monkeypatch.setattr("app.ai_client.httpx.post", fake_post)
    monkeypatch.setattr("app.ai_client.time.sleep", lambda _seconds: None)
    settings = replace(
        load_settings(),
        ai_enabled=True,
        ai_base_url="https://api.example.test/v1",
        ai_api_key="test-key",
        ai_model="test-model",
    )

    messages = [{"source_ref": f"message:{index}", "text": "casino bonus referral promo"} for index in range(12)]
    report, status, errors = GroupQualityAIClient(settings).analyze(messages, {"channel": "@ads"})

    assert status == "completed"
    assert errors == []
    assert report["classification"] == "pure_ad_group"
    assert report["method"] == "ai"
    assert len(calls) == 2
