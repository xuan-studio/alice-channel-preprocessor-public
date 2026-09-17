from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BASE_DIR, load_settings
from .db import Database
from .repository import Repository
from .security import bootstrap_admin


DEMO_DATA = BASE_DIR / "demo" / "demo_data.json"


def _datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


def _prepared_result(raw: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(raw))
    channel = result["channel"]
    for field in ("last_post_at", "last_comment_at", "last_activity_at"):
        channel[field] = _datetime(channel.get(field))
    for item in result.get("commenters", []):
        item["last_comment_at"] = _datetime(item.get("last_comment_at"))
    for item in result.get("raw_artifacts", []):
        item["message_date"] = _datetime(item.get("message_date"))
    return result


def seed(path: Path = DEMO_DATA) -> tuple[int, int]:
    settings = load_settings()
    if not settings.demo_mode:
        raise RuntimeError("拒绝导入演示数据：请先设置 DEMO_MODE=1。")
    payload = json.loads(path.read_text(encoding="utf-8"))
    database = Database(settings)
    database.initialize()
    with database.session() as session:
        bootstrap_admin(session, settings)
    repository = Repository(database, settings)
    existing = {job.normalized_username for job in repository.list_jobs(limit=500)}
    created = 0
    skipped = 0
    for item in payload.get("jobs", []):
        username = str(item["username"]).strip().lower().lstrip("@")
        if username in existing:
            skipped += 1
            continue
        job, _cached = repository.create_scan_job(
            username,
            str(item.get("requested_target") or f"@{username}"),
            created_by_username=settings.bootstrap_username,
            tags=item.get("tags", ["虚构演示数据"]),
            force=True,
            actor="demo_seed",
        )
        repository.complete(job.public_id, _prepared_result(item["result"]))
        existing.add(username)
        created += 1
    return created, skipped


def main() -> int:
    created, skipped = seed()
    print(f"演示数据导入完成：新建 {created} 个，跳过 {skipped} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
