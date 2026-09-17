from __future__ import annotations

import argparse
import json

import httpx

from .ai_client import ChannelInsightAIClient, GroupQualityAIClient
from .config import load_settings
from .db import Database
from .extractors import normalize_channel_target
from .extractors import extract_contacts, merge_contacts
from .repository import Repository
from .security import bootstrap_admin
from .worker import main as worker_main


def runtime() -> Repository:
    settings = load_settings()
    database = Database(settings)
    database.initialize()
    with database.session() as session:
        bootstrap_admin(session, settings)
    return Repository(database, settings)


def insight_context(bundle: dict) -> dict:
    snapshot = bundle.get("snapshot")
    job = bundle.get("job")
    commenters = bundle.get("commenters", [])
    contacts = bundle.get("contacts", [])
    return {
        "channel": f"@{snapshot.username}" if snapshot else f"@{job.normalized_username}",
        "title": snapshot.title if snapshot else "",
        "bio": snapshot.bio if snapshot else "",
        "member_count": snapshot.member_count if snapshot else 0,
        "last_activity_at": str(snapshot.last_activity_at or "") if snapshot else "",
        "last_activity_source": snapshot.last_activity_source if snapshot else "",
        "posts_scanned": snapshot.posts_scanned if snapshot else 0,
        "pinned_scanned": snapshot.pinned_scanned if snapshot else 0,
        "comments_fetched": snapshot.comments_fetched if snapshot else 0,
        "unique_comment_senders": len(commenters),
        "public_comment_usernames": sum(1 for item in commenters if item.sender_type == "user" and item.username),
        "public_contacts": sorted({item.value for item in contacts}),
        "linked_discussion": (snapshot.linked_chat_username or snapshot.linked_chat_title) if snapshot else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram 频道预处理服务管理工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化数据库和管理员")
    enqueue = sub.add_parser("enqueue", help="提交一个公开频道扫描任务")
    enqueue.add_argument("target")
    enqueue.add_argument("--force", action="store_true")
    status = sub.add_parser("status", help="查看任务状态")
    status.add_argument("job_id")
    reextract = sub.add_parser("reextract-contacts", help="用已保存原文重新执行联系方式规则")
    reextract.add_argument("job_id")
    reanalyze = sub.add_parser("reanalyze-ai", help="用已保存原文重新生成 AI 频道总结")
    reanalyze.add_argument("job_id")
    reanalyze_quality = sub.add_parser("reanalyze-group-quality", help="用已保存原文重新判断公开群组是否为广告群")
    reanalyze_quality.add_argument("job_id")
    sub.add_parser("probe-ai", help="检查 OpenAI-compatible 接口和当前模型")
    sub.add_parser("worker-once", help="只处理一个排队任务")
    sub.add_parser("purge", help="执行数据过期清理")
    args = parser.parse_args()

    if args.command == "worker-once":
        return worker_main(once=True)
    repository = runtime()
    if args.command == "init":
        print("initialized")
        return 0
    if args.command == "enqueue":
        username = normalize_channel_target(args.target)
        job, cached = repository.create_scan_job(username, args.target, force=args.force)
        print(json.dumps({"job_id": job.public_id, "status": job.status, "cached": cached}, ensure_ascii=False))
        return 0
    if args.command == "status":
        job = repository.get_job(args.job_id)
        if not job:
            raise SystemExit("job not found")
        print(
            json.dumps(
                {
                    "job_id": job.public_id,
                    "channel": f"@{job.normalized_username}",
                    "status": job.status,
                    "stage": job.stage,
                    "progress": job.progress,
                    "summary": job.result_summary,
                    "error": job.error_message,
                },
                ensure_ascii=False,
                default=str,
                indent=2,
            )
        )
        return 0
    if args.command == "reextract-contacts":
        sources = repository.get_raw_sources(args.job_id)
        rule_contacts = []
        for source in sources:
            rule_contacts.extend(
                extract_contacts(
                    source["text"],
                    source_type=source["source_type"],
                    source_ref=source["source_ref"],
                )
            )
        bundle = repository.get_job_bundle(args.job_id)
        ai_contacts = [
            {column.name: getattr(item, column.name) for column in item.__table__.columns if column.name not in {"id", "job_id"}}
            for item in (bundle or {}).get("contacts", [])
            if item.extractor == "ai"
        ]
        contacts = merge_contacts([*rule_contacts, *ai_contacts])
        count = repository.replace_contacts(args.job_id, contacts, actor="local:reextract")
        print(json.dumps({"job_id": args.job_id, "contacts_found": count}, ensure_ascii=False))
        return 0
    if args.command == "reanalyze-ai":
        bundle = repository.get_job_bundle(args.job_id)
        if not bundle:
            raise SystemExit("job not found")
        sources = repository.get_raw_sources(args.job_id)
        client = ChannelInsightAIClient(load_settings())
        report, ai_status, errors = client.analyze(sources, insight_context(bundle), chunk_size=20)
        repository.update_ai_report(args.job_id, report, ai_status, errors, actor="local:ai-reanalysis")
        print(
            json.dumps(
                {
                    "job_id": args.job_id,
                    "ai_status": ai_status,
                    "signals": len((report or {}).get("signals", [])),
                    "warnings": errors,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "reanalyze-group-quality":
        bundle = repository.get_job_bundle(args.job_id)
        if not bundle:
            raise SystemExit("job not found")
        settings = load_settings()
        sources = [
            source for source in repository.get_raw_sources(args.job_id)
            if source.get("source_type") == "post" and str(source.get("text", "") or "").strip()
        ]
        messages = [
            {"source_ref": source["source_ref"], "text": source["text"]}
            for source in sources[: settings.group_ad_sample_messages]
        ]
        report, ai_status, errors = GroupQualityAIClient(settings).analyze(messages, insight_context(bundle))
        report.update({
            "sampled_messages": len(messages),
            "messages_scanned": len(sources),
            "candidate_limit": settings.group_active_user_limit,
        })
        repository.update_group_quality_report(args.job_id, report, ai_status, errors, actor="local:group-quality-reanalysis")
        print(
            json.dumps(
                {
                    "job_id": args.job_id,
                    "ai_status": ai_status,
                    "classification": report.get("classification", ""),
                    "ad_score": report.get("ad_score", 0),
                    "warnings": errors,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "probe-ai":
        settings = load_settings()
        if not settings.ai_api_key:
            raise SystemExit("AI Key 尚未配置。")
        response = httpx.get(
            f"{settings.ai_base_url}/models",
            headers={"Authorization": f"Bearer {settings.ai_api_key}", "User-Agent": "AliceChannelPreprocessor/0.1"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        models = sorted(
            str(item.get("id", "") or "")
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "base_url": settings.ai_base_url,
                    "configured_model": settings.ai_model,
                    "configured_model_available": settings.ai_model in models,
                    "model_count": len(models),
                    "models": models[:100],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "purge":
        print(json.dumps(repository.purge_expired(), ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
