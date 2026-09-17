from __future__ import annotations

from types import SimpleNamespace

from app.bot import extract_scan_targets, process_update, summary_text


def test_group_summary_does_not_expose_contact_values_or_usernames() -> None:
    job = SimpleNamespace(
        status="completed",
        normalized_username="example_news",
        public_id="job-1",
        result_summary={
            "channel_title": "Example",
            "posts_scanned": 100,
            "comments_fetched": 90,
            "unique_user_commenters": 30,
            "public_usernames": 20,
            "contacts_found": 3,
            "ai_status": "completed",
            "secret_contact": "@must_not_leak",
        },
    )
    text = summary_text(job, "https://internal.example")
    assert "@must_not_leak" not in text
    assert "公开 username：20" in text
    assert "/jobs/job-1" in text


class FakeBot:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {"message_id": 9}


class FakeRepository:
    def __init__(self):
        self.created = []

    def create_scan_job(self, username, target, **kwargs):
        self.created.append((username, target, kwargs))
        return SimpleNamespace(
            status="queued",
            stage="queued",
            progress=0,
            normalized_username=username,
            public_id="job-2",
            result_summary={},
        ), False

    def set_bot_message(self, public_id, message_id):
        self.bot_message = (public_id, message_id)


def test_unauthorized_group_cannot_create_task() -> None:
    bot = FakeBot()
    repo = FakeRepository()
    settings = SimpleNamespace(workgroup_chat_id=-1001, public_base_url="https://internal.example")
    handled = process_update(
        {"message": {"chat": {"id": -2002}, "text": "/scan @example_news", "from": {"id": 7}}},
        bot=bot,
        repository=repo,
        settings=settings,
    )
    assert handled is False
    assert repo.created == []
    assert bot.sent == []


def test_authorized_group_creates_task_and_only_receives_summary() -> None:
    bot = FakeBot()
    repo = FakeRepository()
    settings = SimpleNamespace(workgroup_chat_id=-1001, public_base_url="https://internal.example")
    handled = process_update(
        {"message": {"chat": {"id": -1001}, "text": "/scan @example_news", "from": {"id": 7, "first_name": "A"}}},
        bot=bot,
        repository=repo,
        settings=settings,
    )
    assert handled is True
    assert repo.created[0][0] == "example_news"
    assert bot.sent and "job-2" in bot.sent[0][1]


def test_bot_mention_extracts_up_to_ten_unique_channels_and_excludes_itself() -> None:
    targets = extract_scan_targets(
        "@WelcomeAliceBot @example_news https://t.me/example_news @second_news",
        "WelcomeAliceBot",
    )
    assert targets == ["@example_news", "@second_news"]


def test_optional_user_allowlist_is_enforced() -> None:
    bot = FakeBot()
    repo = FakeRepository()
    settings = SimpleNamespace(
        workgroup_chat_id=-1001,
        allowed_chat_ids=(-1001,),
        allowed_user_ids=(8,),
        public_base_url="https://internal.example",
    )
    handled = process_update(
        {"message": {"chat": {"id": -1001}, "text": "/scan @example_news", "from": {"id": 7}}},
        bot=bot,
        repository=repo,
        settings=settings,
    )
    assert handled is False
    assert repo.created == []
