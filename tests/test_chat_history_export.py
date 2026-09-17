from __future__ import annotations

import pytest

from app.chat_history_export import (
    content_text,
    invoke_with_retries,
    media_file_candidates,
    normalize_target,
    parse_batch_export_targets,
    safe_name,
    supergroup_chat_id,
    supergroup_id_from_chat_id,
)


def test_normalize_export_targets() -> None:
    assert normalize_target("@ExampleGroup") == {"kind": "username", "username": "ExampleGroup", "label": "ExampleGroup"}
    assert normalize_target("https://t.me/example_group/123") == {
        "kind": "username",
        "username": "example_group",
        "label": "example_group",
    }
    assert normalize_target("5551686747") == {
        "kind": "numeric_id",
        "numeric_id": 5551686747,
        "label": "5551686747",
    }
    assert normalize_target("-1001234567890") == {
        "kind": "chat_id",
        "chat_id": -1001234567890,
        "label": "-1001234567890",
    }
    assert normalize_target("https://t.me/c/1234567890/55") == {
        "kind": "chat_id",
        "chat_id": -1001234567890,
        "supergroup_id": 1234567890,
        "label": "tme-c-1234567890",
    }


def test_supergroup_id_conversion() -> None:
    assert supergroup_chat_id(5551686747) == -1005551686747
    assert supergroup_id_from_chat_id(-1005551686747) == 5551686747


def test_normalize_rejects_invite_links() -> None:
    with pytest.raises(ValueError):
        normalize_target("https://t.me/+privateInvite")


def test_parse_batch_export_targets_accepts_ids_and_links() -> None:
    parsed = parse_batch_export_targets(
        "请导出 https://t.me/c/4450373602/123 和 @ExampleGroup\n5551686747\n重复 @examplegroup"
    )

    assert [item["target"] for item in parsed["targets"]] == [
        "https://t.me/c/4450373602/123",
        "@ExampleGroup",
        "5551686747",
    ]
    assert parsed["duplicates"][0]["target"] == "@examplegroup"
    assert parsed["invalid"] == []


def test_content_text_reads_text_and_captions() -> None:
    assert content_text({"@type": "messageText", "text": {"text": "hello"}}) == "hello"
    assert content_text({"@type": "messagePhoto", "caption": {"text": "caption"}}) == "caption"
    assert content_text({"@type": "messageSticker", "sticker": {"emoji": "ok"}}) == "ok"


def test_media_file_candidates_prefers_largest_photo() -> None:
    candidates = media_file_candidates(
        {
            "@type": "messagePhoto",
            "photo": {
                "sizes": [
                    {"width": 100, "height": 100, "photo": {"id": 1}},
                    {"width": 800, "height": 600, "photo": {"id": 2}},
                ]
            },
        }
    )

    assert candidates == [{"file_id": 2, "label": "photo", "name_hint": "photo.jpg"}]


def test_invoke_with_retries_recovers_tdlib_timeout() -> None:
    class FlakySession:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, method: str, **params):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(f"TDLib 请求超时：{method}")
            return {"@type": "ok", "params": params}

    session = FlakySession()

    assert invoke_with_retries(session, "searchPublicChat", retries=1, retry_sleep=0, username="alicebotarmy") == {
        "@type": "ok",
        "params": {"username": "alicebotarmy"},
    }
    assert session.calls == 2


def test_safe_name_removes_path_like_characters() -> None:
    assert safe_name("../bad/chat name") == "..-bad-chat-name"
