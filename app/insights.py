from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable


LANGUAGE_LABELS = {
    "ar": "阿拉伯语",
    "de": "德语",
    "en": "英语",
    "es": "西班牙语",
    "fr": "法语",
    "id": "印尼语",
    "ja": "日语",
    "ko": "韩语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "th": "泰语",
    "tr": "土耳其语",
    "und": "待判断",
    "vi": "越南语",
    "zh": "中文",
}


_LATIN_MARKERS = {
    "de": {"aber", "auch", "der", "die", "ein", "eine", "für", "ist", "mit", "nicht", "und", "von"},
    "en": {"and", "are", "for", "from", "in", "is", "of", "on", "that", "the", "this", "to", "with"},
    "es": {"con", "de", "el", "en", "es", "la", "las", "los", "para", "por", "que", "una"},
    "fr": {"avec", "dans", "de", "des", "est", "la", "le", "les", "pour", "que", "un", "une"},
    "id": {"akan", "dalam", "dan", "dari", "dengan", "ini", "itu", "kami", "untuk", "yang"},
    "pt": {"com", "da", "de", "do", "em", "os", "para", "por", "que", "uma"},
    "tr": {"ama", "bir", "bu", "da", "de", "için", "ile", "ve", "ya"},
    "vi": {"các", "cho", "của", "được", "không", "là", "một", "những", "trong", "và", "với"},
}


def _confidence(value: float) -> float:
    return round(max(0.0, min(0.99, value)), 2)


def detect_language(texts: Iterable[str]) -> dict[str, Any]:
    sample = "\n".join(str(item or "") for item in texts if str(item or "").strip())[:20000]
    letters = [char for char in sample if char.isalpha()]
    if len(letters) < 12:
        return {"code": "und", "label": LANGUAGE_LABELS["und"], "confidence": 0.0, "method": "local_heuristic", "sample_chars": len(letters)}

    script_counts = {
        "ar": len(re.findall(r"[\u0600-\u06ff]", sample)),
        "cyrillic": len(re.findall(r"[\u0400-\u04ff]", sample)),
        "han": len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", sample)),
        "ja": len(re.findall(r"[\u3040-\u30ff]", sample)),
        "ko": len(re.findall(r"[\uac00-\ud7af]", sample)),
        "th": len(re.findall(r"[\u0e00-\u0e7f]", sample)),
    }
    total_letters = max(1, len(letters))
    if script_counts["ja"] >= 3:
        code = "ja"
        count = script_counts["ja"] + script_counts["han"]
    elif script_counts["ko"] >= 3:
        code = "ko"
        count = script_counts["ko"]
    elif script_counts["han"] >= 6:
        code = "zh"
        count = script_counts["han"]
    elif script_counts["cyrillic"] >= 6:
        code = "ru"
        count = script_counts["cyrillic"]
    elif script_counts["ar"] >= 6:
        code = "ar"
        count = script_counts["ar"]
    elif script_counts["th"] >= 6:
        code = "th"
        count = script_counts["th"]
    else:
        code = ""
        count = 0
    if code:
        return {
            "code": code,
            "label": LANGUAGE_LABELS[code],
            "confidence": _confidence(0.62 + min(0.35, count / total_letters * 0.4)),
            "method": "local_heuristic",
            "sample_chars": len(letters),
        }

    words = re.findall(r"[a-zà-žğışçöü]+", sample.lower())
    scores = {language: sum(1 for word in words if word in markers) for language, markers in _LATIN_MARKERS.items()}
    lower_sample = sample.lower()
    accent_bonus = {
        "tr": len(re.findall(r"[ğışçöü]", lower_sample)),
        "vi": len(re.findall(r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]", lower_sample)),
    }
    for language, bonus in accent_bonus.items():
        scores[language] += min(12, bonus * 2)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_code, best_score = ranked[0]
    second_score = ranked[1][1]
    if best_score < 2:
        best_code = "en" if len(words) >= 8 else "und"
        confidence = 0.38 if best_code == "en" else 0.0
    else:
        confidence = 0.5 + min(0.43, (best_score - second_score + 1) / (best_score + 4) * 0.43)
    return {
        "code": best_code,
        "label": LANGUAGE_LABELS[best_code],
        "confidence": _confidence(confidence),
        "method": "local_heuristic",
        "sample_chars": len(letters),
    }


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def engagement_metrics(messages: Iterable[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    rows = list(messages)
    total_views = 0
    total_forwards = 0
    total_replies = 0
    total_reactions = 0
    with_views = 0
    dates: list[datetime] = []
    for message in rows:
        interaction = message.get("interaction_info") or {}
        views = _int_value(interaction.get("view_count"))
        forwards = _int_value(interaction.get("forward_count"))
        replies = _int_value((interaction.get("reply_info") or {}).get("reply_count"))
        reactions = interaction.get("reactions") or message.get("reactions") or {}
        reaction_count = _int_value(reactions.get("total_count"))
        if not reaction_count:
            reaction_count = sum(_int_value(item.get("total_count")) for item in list(reactions.get("reactions") or []))
        total_views += views
        total_forwards += forwards
        total_replies += replies
        total_reactions += reaction_count
        with_views += int(views > 0)
        timestamp = _int_value(message.get("date"))
        if timestamp:
            dates.append(datetime.fromtimestamp(timestamp, timezone.utc))

    count = len(rows)
    observed_posts_per_week = 0.0
    if len(dates) == 1:
        observed_posts_per_week = 1.0
    elif len(dates) > 1:
        span_days = max(1.0, (max(dates) - min(dates)).total_seconds() / 86400)
        observed_posts_per_week = round((len(dates) - 1) / span_days * 7, 1)
    reference = now or datetime.now(timezone.utc)
    recent_30d = sum(1 for value in dates if 0 <= (reference - value).total_seconds() <= 30 * 86400)
    return {
        "posts_measured": count,
        "posts_with_views": with_views,
        "avg_views": round(total_views / with_views) if with_views else 0,
        "avg_reactions": round(total_reactions / count, 1) if count else 0.0,
        "avg_replies": round(total_replies / count, 1) if count else 0.0,
        "avg_forwards": round(total_forwards / count, 1) if count else 0.0,
        "engagement_rate": round((total_reactions + total_replies) / total_views * 100, 2) if total_views else 0.0,
        "posts_per_week": observed_posts_per_week,
        "posts_last_30d": recent_30d,
        "method": "tdlib_message_metrics",
    }
