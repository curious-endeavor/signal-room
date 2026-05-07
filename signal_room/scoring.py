from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Tuple

from .models import RawItem, ScoredItem


PILLAR_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "P1": (
        "content",
        "post",
        "campaign",
        "copy",
        "editorial",
        "creative",
        "social",
        "marketing",
        "cmo",
    ),
    "P2": (
        "design",
        "brand",
        "identity",
        "visual",
        "voice",
        "figma",
        "font",
        "system",
        "prototype",
    ),
    "P3": (
        "team",
        "company",
        "employee",
        "organization",
        "agency",
        "ai-first",
        "coworker",
        "service company",
        "consulting",
    ),
    "P4": (
        "workflow",
        "process",
        "ready",
        "handoff",
        "implementation",
        "operations",
        "infrastructure",
        "automation",
        "deploy",
    ),
    "P5": (
        "human",
        "review",
        "judgment",
        "grader",
        "failure",
        "quality",
        "approval",
        "risk",
        "evaluation",
    ),
}

SURF_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "S1": (
        "ai-native",
        "brand-building",
        "agency",
        "case study",
        "brand",
        "voice",
        "visual",
        "launch",
    ),
    "S2": (
        "classic branding",
        "identity",
        "typography",
        "brand new",
        "fonts",
        "design history",
        "reference",
    ),
}

MECHANISM_KEYWORDS = (
    "workflow",
    "method",
    "process",
    "playbook",
    "implementation",
    "how they",
    "step",
    "system",
    "human-in-the-loop",
    "review",
    "failure mode",
    "grader",
    "memory",
    "infrastructure",
    "case study",
    "operating model",
)

GENERIC_PHRASES = (
    "ai will change everything",
    "game changer",
    "revolutionize",
    "future of ai",
    "unlock productivity",
    "paradigm shift",
)

VENDOR_ONLY_PHRASES = (
    "announced",
    "launches",
    "introduces",
    "now available",
    "benchmark",
    "leaderboard",
)

NEGATIVE_MECHANISM_PHRASES = (
    "no case study",
    "no workflow",
    "no mechanism",
    "no agency",
    "no agency, brand, workflow",
    "no mechanism for",
    "almost no case study",
    "almost no workflow",
    "without workflow detail",
    "no implementation detail",
)


def score_items(
    raw_items: Iterable[RawItem],
    weights: Dict[str, Any],
    feedback_events: List[Dict[str, Any]],
    source_feedback_weights: Dict[str, float] = None,
) -> List[ScoredItem]:
    raw_item_list = list(raw_items)
    visible_items = _filter_low_traction(raw_item_list)
    feedback_by_item, feedback_by_source = _feedback_maps(feedback_events, raw_item_list)
    source_feedback_weights = source_feedback_weights or {}
    scored = [
        _score_one(
            item,
            weights,
            feedback_by_item.get(item.id, Counter()),
            feedback_by_source.get(item.source, 0.0) + float(source_feedback_weights.get(item.source, 0.0)),
        )
        for item in visible_items
    ]
    return sorted(scored, key=lambda item: (item.score, item.mechanism_present, item.date), reverse=True)


def _feedback_maps(
    feedback_events: List[Dict[str, Any]], raw_items: Iterable[RawItem]
) -> Tuple[Dict[str, Counter], Dict[str, float]]:
    source_by_item = {item.id: item.source for item in raw_items}
    by_item: Dict[str, Counter] = defaultdict(Counter)
    by_source: Dict[str, float] = defaultdict(float)
    for event in feedback_events:
        item_id = str(event.get("item_id", ""))
        action = str(event.get("action", ""))
        by_item[item_id][action] += 1
        source = source_by_item.get(item_id)
        if not source:
            continue
        if action in {"useful", "turned_into_content"}:
            by_source[source] += 1.5
        elif action == "source_worth_following":
            by_source[source] += 2.0
        elif action in {"not_useful", "too_generic"}:
            by_source[source] -= 1.0
    return by_item, by_source


def _score_one(
    item: RawItem,
    weights: Dict[str, Any],
    item_feedback: Counter,
    source_weight: float,
) -> ScoredItem:
    text = f"{item.title} {item.summary} {item.content} {' '.join(item.tags)}".lower()
    pillar_fit = _matches(text, PILLAR_KEYWORDS)
    surf_fit = _matches(text, SURF_KEYWORDS)
    mechanism_hits = [keyword for keyword in MECHANISM_KEYWORDS if keyword in text]
    generic_hits = [phrase for phrase in GENERIC_PHRASES if phrase in text]
    vendor_hits = [phrase for phrase in VENDOR_ONLY_PHRASES if phrase in text]
    negative_mechanism_hits = [phrase for phrase in NEGATIVE_MECHANISM_PHRASES if phrase in text]

    if negative_mechanism_hits:
        mechanism_hits = []
    mechanism_present = bool(mechanism_hits)
    score = float(weights.get("base_score", 20))
    score += len(pillar_fit) * float(weights.get("pillar_match", 9))
    score += len(surf_fit) * float(weights.get("surf_match", 5))
    if mechanism_present:
        score += float(weights.get("mechanism_present", 25))
        score += min(len(mechanism_hits), 4) * float(weights.get("mechanism_keyword", 3))
    if item.candidate_source:
        score += float(weights.get("candidate_source_penalty", -4))
    if item.discovery_method == "search":
        score += float(weights.get("search_discovery_bonus", 2))
    if generic_hits:
        score += float(weights.get("generic_penalty", -24))
    if vendor_hits and not mechanism_present:
        score += float(weights.get("vendor_without_mechanism_penalty", -18))

    score += _traction_score_adjustment(item, weights)
    score += item_feedback.get("useful", 0) * 4
    score += item_feedback.get("turned_into_content", 0) * 6
    score -= item_feedback.get("not_useful", 0) * 7
    score -= item_feedback.get("too_generic", 0) * 8
    score -= item_feedback.get("wrong_pillar", 0) * 5
    score += source_weight * float(weights.get("source_feedback_multiplier", 2))
    score = max(0.0, min(100.0, score))

    reason = _reason(
        pillar_fit,
        surf_fit,
        mechanism_hits,
        generic_hits,
        vendor_hits,
        negative_mechanism_hits,
        item_feedback,
        source_weight,
    )
    primary_pillar = pillar_fit[0] if pillar_fit else "P3"
    why = _why_ce_should_care(item, primary_pillar, mechanism_present)
    angle = _suggested_angle(item, primary_pillar, mechanism_present)
    take = _possible_take(item, primary_pillar, mechanism_present)
    query = _follow_up_query(item, pillar_fit, surf_fit)

    return ScoredItem(
        id=item.id,
        title=item.title,
        source=item.source,
        source_url=item.source_url,
        date=item.date,
        summary=item.summary,
        pillar_fit=pillar_fit,
        surf_fit=surf_fit,
        mechanism_present=mechanism_present,
        score=score,
        reason_for_score=reason,
        why_ce_should_care=why,
        suggested_ce_angle=angle,
        possible_ce_take=take,
        follow_up_search_query=query,
        discovery_method=item.discovery_method,
        candidate_source=item.candidate_source,
        feedback_counts=dict(item_feedback),
        source_weight=source_weight,
        engagement=item.engagement,
        metadata=item.metadata,
        engagement_score=item.engagement_score,
        local_rank_score=item.local_rank_score,
        local_relevance=item.local_relevance,
        freshness=item.freshness,
        traction_label=_traction_label(item),
    )


def _filter_low_traction(items: List[RawItem]) -> List[RawItem]:
    filtered = [item for item in items if _passes_traction_floor(item)]
    return filtered or items


def _passes_traction_floor(item: RawItem) -> bool:
    platform = _platform(item)
    if platform == "instagram":
        return _views(item) >= 1_000 or _metric(item, "likes") >= 50 or _metric(item, "comments") >= 20
    if platform == "tiktok":
        return _views(item) >= 5_000 or _metric(item, "likes") >= 100 or _metric(item, "comments") >= 20
    if platform == "youtube":
        return _views(item) >= 1_000 or _social_reactions(item) >= 50 or _engagement_score(item) >= 50
    return True


def _traction_score_adjustment(item: RawItem, weights: Dict[str, Any]) -> float:
    platform = _platform(item)
    if platform not in {"instagram", "tiktok", "youtube", "x", "reddit"}:
        return 0.0
    engagement_score = _engagement_score(item)
    adjustment = min(10.0, engagement_score / 10.0)
    if platform == "x" and engagement_score == 0 and item.engagement:
        adjustment += float(weights.get("low_social_traction_penalty", -18))
    if platform == "reddit" and engagement_score == 0 and item.engagement:
        adjustment += float(weights.get("low_social_traction_penalty", -18))
    return adjustment


def _platform(item: RawItem) -> str:
    for tag in item.tags:
        if str(tag).startswith("platform:"):
            return str(tag).split(":", 1)[1].lower()
    source = item.source.lower()
    if "instagram" in source:
        return "instagram"
    if "youtube" in source:
        return "youtube"
    if source.startswith("x") or "twitter" in source:
        return "x"
    if "reddit" in source:
        return "reddit"
    return source


def _engagement_score(item: RawItem) -> float:
    return float(item.engagement_score or 0.0)


def _views(item: RawItem) -> int:
    return _int_metric(item.engagement.get("views") or item.engagement.get("view_count"))


def _metric(item: RawItem, field: str) -> int:
    return _int_metric(item.engagement.get(field))


def _social_reactions(item: RawItem) -> int:
    fields = ("likes", "comments", "reposts", "retweets", "shares", "replies", "score", "num_comments")
    return sum(_int_metric(item.engagement.get(field)) for field in fields)


def _int_metric(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _traction_label(item: RawItem) -> str:
    platform = _platform(item)
    if platform not in {"instagram", "tiktok", "youtube", "x", "reddit"} or not item.engagement:
        return ""
    labels = []
    for key, label in [
        ("views", "views"),
        ("likes", "likes"),
        ("comments", "comments"),
        ("reposts", "reposts"),
        ("retweets", "retweets"),
        ("replies", "replies"),
        ("score", "points"),
        ("num_comments", "comments"),
    ]:
        value = _int_metric(item.engagement.get(key))
        if value:
            labels.append(f"{_compact_number(value)} {label}")
    return " · ".join(labels[:3])


def _compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".rstrip("0").rstrip(".")
    return str(value)


def _matches(text: str, keyword_map: Dict[str, Tuple[str, ...]]) -> List[str]:
    matches = []
    for key, keywords in keyword_map.items():
        if any(keyword in text for keyword in keywords):
            matches.append(key)
    return matches


def _reason(
    pillars: List[str],
    surf: List[str],
    mechanism_hits: List[str],
    generic_hits: List[str],
    vendor_hits: List[str],
    negative_mechanism_hits: List[str],
    feedback: Counter,
    source_weight: float,
) -> str:
    parts = []
    if pillars:
        parts.append(f"fits {', '.join(pillars)}")
    if surf:
        parts.append(f"also has surf value ({', '.join(surf)})")
    if mechanism_hits:
        parts.append(f"mechanism evidence: {', '.join(mechanism_hits[:4])}")
    else:
        parts.append("limited mechanism detail")
    if negative_mechanism_hits:
        parts.append(f"explicitly lacks mechanism: {', '.join(negative_mechanism_hits[:2])}")
    if generic_hits:
        parts.append("penalized as generic AI language")
    if vendor_hits and not mechanism_hits:
        parts.append("penalized as announcement-like without workflow detail")
    if feedback:
        parts.append(f"prior feedback: {dict(feedback)}")
    if source_weight:
        parts.append(f"source feedback weight {source_weight:+.1f}")
    return "; ".join(parts)


def _why_ce_should_care(item: RawItem, primary_pillar: str, mechanism_present: bool) -> str:
    pillar_reason = {
        "P1": "It can feed CE's own content practice and help clients see AI content as an operating system, not a prompt trick.",
        "P2": "It gives CE material for how AI changes design, brand systems, taste, and creative judgment.",
        "P3": "It shows how marketing work is being reorganized around AI-native teams and services.",
        "P4": "It helps CE decide which workflows are ready for AI and which still need human structure.",
        "P5": "It gives CE a concrete example of where human judgment, review, and failure modes matter.",
    }.get(primary_pillar, "It gives CE a concrete market signal to evaluate through its AI-native agency lens.")
    mechanism_note = " The mechanism detail makes it useful for content, client thinking, or pitch references." if mechanism_present else " It needs follow-up before becoming a strong CE reference."
    return f"{pillar_reason}{mechanism_note}"


def _suggested_angle(item: RawItem, primary_pillar: str, mechanism_present: bool) -> str:
    if primary_pillar == "P1":
        return "Use this to show how content work is being productized into repeatable AI-assisted workflows."
    if primary_pillar == "P2":
        return "Frame this as a design/taste problem: AI changes the production layer, but judgment still decides what is good."
    if primary_pillar == "P3":
        return "Use this as evidence that AI-first marketing is becoming an org design question, not a tool adoption question."
    if primary_pillar == "P4":
        return "Turn this into a workflow-readiness lesson: what can be automated, what needs structure, and what should stay human-led."
    if primary_pillar == "P5":
        return "Use it to argue that useful AI systems need graders, review loops, and explicit failure boundaries."
    return "Interrogate the underlying mechanism before deciding whether this is content-worthy."


def _possible_take(item: RawItem, primary_pillar: str, mechanism_present: bool) -> str:
    if not mechanism_present:
        return "The interesting question is not the announcement; it is whether there is a real workflow behind it."
    if primary_pillar == "P3":
        return "The next marketing team is not smaller because of AI; it is more explicit about what each human and agent is responsible for."
    if primary_pillar == "P4":
        return "AI readiness is not about task size. It is about whether the workflow has clear inputs, judgment points, and recovery paths."
    if primary_pillar == "P5":
        return "The best AI work is not fully automated. It has visible grading, escalation, and taste built into the loop."
    if primary_pillar == "P2":
        return "AI can multiply visual options, but brand value still comes from choosing the right constraint."
    return "The winners will not be the teams with more AI tools, but the teams with better mechanisms for using them."


def _follow_up_query(item: RawItem, pillars: List[str], surf: List[str]) -> str:
    lens = " ".join(pillars + surf) or "AI marketing workflow"
    return f'"{item.title}" {lens} case study workflow implementation'
