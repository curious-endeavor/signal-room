import hashlib
from typing import Any, Dict, Iterable, List, Sequence

from .models import RawItem


def load_raw_items(seed_payload: Dict[str, Any], item_payloads: Sequence[Dict[str, Any]]) -> List[RawItem]:
    raw_items = []
    for payload in item_payloads:
        for item_payload in payload.get("items", []):
            raw_items.append(RawItem.from_dict(_normalize_item(item_payload)))
    return _dedupe(raw_items)


def source_candidates(seed_payload: Dict[str, Any], raw_items: Iterable[RawItem]) -> List[Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for source in seed_payload.get("sources", []):
        candidates[str(source["name"])] = {
            "name": source["name"],
            "category": source.get("category", "seed"),
            "url": source.get("url", ""),
            "trusted": True,
            "reason": source.get("reason", "Seed source from the CE brief."),
        }

    for item in raw_items:
        if item.candidate_source and item.source not in candidates:
            candidates[item.source] = {
                "name": item.source,
                "category": "daily_candidate",
                "url": item.source_url,
                "trusted": False,
                "reason": "Discovered through search fixture. Do not trust until marked source_worth_following.",
            }
    return sorted(candidates.values(), key=lambda source: (not source["trusted"], source["name"]))


def _normalize_item(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload)
    if not normalized.get("id"):
        identity = f'{normalized.get("title", "")}|{normalized.get("source_url", "")}'
        normalized["id"] = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return normalized


def _dedupe(items: Iterable[RawItem]) -> List[RawItem]:
    seen = set()
    deduped = []
    for item in items:
        source_url = item.source_url.strip().lower()
        key = ("url", source_url) if source_url else ("title", item.title.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
