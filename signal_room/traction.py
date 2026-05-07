from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any, Iterable

from .models import RawItem


SOCIAL_PLATFORMS = {"instagram", "youtube", "x", "reddit", "tiktok"}
REFERENCE_PLATFORMS = {"grounding", "github", "hackernews"}


def rank_items_by_traction(raw_items: Iterable[RawItem]) -> list[dict[str, Any]]:
    """Rank discovered items by social traction while preserving reference items."""
    valid_items = [_with_traction_fields(item) for item in raw_items if _valid_result(item)]
    social_items = [item for item in valid_items if item["result_bucket"] == "social"]
    reference_items = [item for item in valid_items if item["result_bucket"] == "reference"]
    social_items.sort(key=_social_sort_key, reverse=True)
    reference_items.sort(key=_reference_sort_key, reverse=True)
    return social_items + reference_items


def platform_for_item(item: RawItem | dict[str, Any]) -> str:
    tags = item.tags if isinstance(item, RawItem) else item.get("tags", [])
    for tag in tags or []:
        if str(tag).startswith("platform:"):
            return str(tag).split(":", 1)[1].lower()
    source = (item.source if isinstance(item, RawItem) else str(item.get("source", ""))).lower()
    if "instagram" in source:
        return "instagram"
    if "youtube" in source:
        return "youtube"
    if source.startswith("x") or "twitter" in source:
        return "x"
    if "reddit" in source:
        return "reddit"
    if "github" in source:
        return "github"
    if "hacker" in source:
        return "hackernews"
    return source


def traction_label(item: RawItem | dict[str, Any]) -> str:
    platform = platform_for_item(item)
    engagement = item.engagement if isinstance(item, RawItem) else dict(item.get("engagement") or {})
    if platform not in SOCIAL_PLATFORMS or not engagement:
        return ""
    labels = []
    for key, label in [
        ("views", "views"),
        ("view_count", "views"),
        ("likes", "likes"),
        ("comments", "comments"),
        ("reposts", "reposts"),
        ("retweets", "retweets"),
        ("shares", "shares"),
        ("replies", "replies"),
        ("score", "points"),
        ("num_comments", "comments"),
    ]:
        value = _int_metric(engagement.get(key))
        if value and (key != "view_count" or "views" not in engagement):
            labels.append(f"{_compact_number(value)} {label}")
    return " · ".join(labels[:3])


def traction_score(item: RawItem | dict[str, Any]) -> float:
    engagement_score = (
        item.engagement_score
        if isinstance(item, RawItem)
        else _optional_float(item.get("engagement_score"))
    )
    if engagement_score is not None:
        return max(0.0, float(engagement_score))
    platform = platform_for_item(item)
    engagement = item.engagement if isinstance(item, RawItem) else dict(item.get("engagement") or {})
    if platform == "instagram":
        return _log_score(engagement, {"views": 4.0, "view_count": 4.0, "likes": 8.0, "comments": 18.0})
    if platform == "youtube":
        return _log_score(engagement, {"views": 4.5, "view_count": 4.5, "likes": 8.0, "comments": 18.0})
    if platform == "x":
        return _log_score(engagement, {"likes": 10.0, "reposts": 18.0, "retweets": 18.0, "quotes": 12.0, "replies": 12.0})
    if platform == "reddit":
        return _log_score(engagement, {"score": 10.0, "num_comments": 16.0, "comments": 16.0})
    return 0.0


def _with_traction_fields(item: RawItem) -> dict[str, Any]:
    payload = item.to_dict()
    platform = platform_for_item(item)
    score = traction_score(item)
    payload["platform"] = platform
    payload["result_bucket"] = "social" if platform in SOCIAL_PLATFORMS else "reference"
    payload["traction_score"] = round(score, 2)
    payload["score"] = round(score, 2)
    payload["traction_label"] = traction_label(item)
    payload["follow_up_search_query"] = _follow_up_query(item)
    payload.setdefault("suggested_ce_angle", "")
    payload.setdefault("pillar_fit", [])
    payload.setdefault("surf_fit", [])
    payload.setdefault("mechanism_present", False)
    return payload


def _valid_result(item: RawItem) -> bool:
    return bool(item.title.strip() and item.source_url.strip())


def _social_sort_key(item: dict[str, Any]) -> tuple[float, int, float, str]:
    return (
        float(item.get("traction_score") or 0.0),
        _has_any_engagement(item),
        _date_ordinal(str(item.get("date", ""))),
        str(item.get("title", "")),
    )


def _reference_sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
    local_rank = _optional_float(item.get("local_rank_score")) or 0.0
    local_relevance = _optional_float(item.get("local_relevance")) or 0.0
    return (
        local_rank + local_relevance,
        _date_ordinal(str(item.get("date", ""))),
        str(item.get("title", "")),
    )


def _has_any_engagement(item: dict[str, Any]) -> int:
    return int(any(_int_metric(value) > 0 for value in dict(item.get("engagement") or {}).values()))


def _date_ordinal(raw_date: str) -> float:
    if not raw_date:
        return 0.0
    try:
        return float(datetime.fromisoformat(raw_date[:10]).date().toordinal())
    except ValueError:
        return 0.0


def _log_score(engagement: dict[str, Any], weights: dict[str, float]) -> float:
    score = 0.0
    used_views = False
    for field, weight in weights.items():
        if field == "view_count" and used_views:
            continue
        value = _int_metric(engagement.get(field))
        if field == "views" and value:
            used_views = True
        if value:
            score += math.log1p(value) * weight
    return min(100.0, score)


def _follow_up_query(item: RawItem) -> str:
    title = item.title.strip()
    if not title:
        return ""
    return f'"{title}" audience reaction why it matters'


def _int_metric(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".rstrip("0").rstrip(".")
    return str(value)
