from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


PILLARS: Dict[str, str] = {
    "P1": "Creating content with AI",
    "P2": "Designing with AI",
    "P3": "Turning a marketing team into AI-first",
    "P4": "Selecting which workflows are AI-ready and which are not",
    "P5": "Assessing good vs. bad in human-in-the-loop AI work",
}

SURF: Dict[str, str] = {
    "S1": "AI-native brand-building in the wild",
    "S2": "Excellent classic branding reference material",
}

FEEDBACK_ACTIONS = {
    "useful",
    "not_useful",
    "wrong_pillar",
    "too_generic",
    "source_worth_following",
    "turned_into_content",
}


@dataclass
class RawItem:
    id: str
    title: str
    source: str
    source_url: str
    date: str
    summary: str
    content: str
    discovery_method: str
    candidate_source: bool = False
    tags: List[str] = field(default_factory=list)
    engagement: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    engagement_score: Optional[float] = None
    local_rank_score: Optional[float] = None
    local_relevance: Optional[float] = None
    freshness: Optional[float] = None

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RawItem":
        return cls(
            id=str(payload["id"]),
            title=str(payload["title"]),
            source=str(payload["source"]),
            source_url=str(payload["source_url"]),
            date=str(payload["date"]),
            summary=str(payload.get("summary", "")),
            content=str(payload.get("content", "")),
            discovery_method=str(payload.get("discovery_method", "seed")),
            candidate_source=bool(payload.get("candidate_source", False)),
            tags=list(payload.get("tags", [])),
            engagement=dict(payload.get("engagement") or {}),
            metadata=dict(payload.get("metadata") or {}),
            engagement_score=_optional_float(payload.get("engagement_score")),
            local_rank_score=_optional_float(payload.get("local_rank_score")),
            local_relevance=_optional_float(payload.get("local_relevance")),
            freshness=_optional_float(payload.get("freshness")),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "source_url": self.source_url,
            "date": self.date,
            "summary": self.summary,
            "content": self.content,
            "discovery_method": self.discovery_method,
            "candidate_source": self.candidate_source,
            "tags": self.tags,
            "engagement": self.engagement,
            "metadata": self.metadata,
        }
        if self.engagement_score is not None:
            payload["engagement_score"] = self.engagement_score
        if self.local_rank_score is not None:
            payload["local_rank_score"] = self.local_rank_score
        if self.local_relevance is not None:
            payload["local_relevance"] = self.local_relevance
        if self.freshness is not None:
            payload["freshness"] = self.freshness
        return payload


@dataclass
class ScoredItem:
    id: str
    title: str
    source: str
    source_url: str
    date: str
    summary: str
    pillar_fit: List[str]
    surf_fit: List[str]
    mechanism_present: bool
    score: float
    reason_for_score: str
    why_ce_should_care: str
    suggested_ce_angle: str
    possible_ce_take: str
    follow_up_search_query: str
    discovery_method: str
    candidate_source: bool
    feedback_counts: Dict[str, int] = field(default_factory=dict)
    source_weight: float = 0.0
    engagement: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    engagement_score: Optional[float] = None
    local_rank_score: Optional[float] = None
    local_relevance: Optional[float] = None
    freshness: Optional[float] = None
    traction_label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "source_url": self.source_url,
            "date": self.date,
            "summary": self.summary,
            "pillar_fit": self.pillar_fit,
            "surf_fit": self.surf_fit,
            "mechanism_present": self.mechanism_present,
            "score": round(self.score, 2),
            "reason_for_score": self.reason_for_score,
            "why_ce_should_care": self.why_ce_should_care,
            "suggested_ce_angle": self.suggested_ce_angle,
            "possible_ce_take": self.possible_ce_take,
            "follow_up_search_query": self.follow_up_search_query,
            "discovery_method": self.discovery_method,
            "candidate_source": self.candidate_source,
            "feedback_counts": self.feedback_counts,
            "source_weight": self.source_weight,
            "engagement": self.engagement,
            "metadata": self.metadata,
            "traction_label": self.traction_label,
        }
        if self.engagement_score is not None:
            payload["engagement_score"] = self.engagement_score
        if self.local_rank_score is not None:
            payload["local_rank_score"] = self.local_rank_score
        if self.local_relevance is not None:
            payload["local_relevance"] = self.local_relevance
        if self.freshness is not None:
            payload["freshness"] = self.freshness
        return payload


@dataclass
class FeedbackEvent:
    item_id: str
    action: str
    note: str
    created_at: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "item_id": self.item_id,
            "action": self.action,
            "note": self.note,
            "created_at": self.created_at,
        }


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
