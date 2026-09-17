from __future__ import annotations

from datetime import datetime, timezone

from app.insights import detect_language, engagement_metrics


def test_detect_language_handles_cjk_and_latin_languages() -> None:
    chinese = detect_language(["这是一个关于科技产品和人工智能的中文频道，每天发布行业新闻。"])
    english = detect_language(["This is the official channel for product news and updates from the team."])
    vietnamese = detect_language(["Đây là kênh tin tức công nghệ và những cập nhật mới nhất của chúng tôi."])

    assert chinese["code"] == "zh"
    assert english["code"] == "en"
    assert vietnamese["code"] == "vi"
    assert vietnamese["confidence"] >= 0.5


def test_engagement_metrics_uses_tdlib_message_fields() -> None:
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    messages = [
        {
            "date": int(datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()),
            "interaction_info": {
                "view_count": 100,
                "forward_count": 2,
                "reply_info": {"reply_count": 2},
                "reactions": {"total_count": 10},
            },
        },
        {
            "date": int(datetime(2026, 9, 17, tzinfo=timezone.utc).timestamp()),
            "interaction_info": {
                "view_count": 200,
                "forward_count": 1,
                "reply_info": {"reply_count": 0},
                "reactions": {"reactions": [{"total_count": 4}]},
            },
        },
    ]

    metrics = engagement_metrics(messages, now=now)

    assert metrics["avg_views"] == 150
    assert metrics["avg_reactions"] == 7.0
    assert metrics["avg_replies"] == 1.0
    assert metrics["avg_forwards"] == 1.5
    assert metrics["engagement_rate"] == 5.33
    assert metrics["posts_per_week"] == 1.0
    assert metrics["posts_last_30d"] == 2
