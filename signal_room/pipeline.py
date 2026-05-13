from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from .digest import render_digest
from .ingest import load_raw_items, source_candidates
from .fetchers.last30days import DISCOVERED_ITEMS_PATH, fetch_last30days
from .models import ScoredItem
from .scoring import score_items
from .storage import (
    CONFIG_DIR,
    DATA_DIR,
    FIXTURES_DIR,
    OUTPUT_DIR,
    ensure_dirs,
    read_json,
    read_jsonl,
    write_json,
)


SEEDS_PATH = CONFIG_DIR / "seed_sources.json"
WEIGHTS_PATH = CONFIG_DIR / "scoring_weights.json"
SOURCE_WEIGHTS_PATH = CONFIG_DIR / "source_feedback_weights.json"
FIXTURE_PATH = FIXTURES_DIR / "sample_items.json"
RAW_PATH = DATA_DIR / "raw_items.json"
ENRICHED_PATH = DATA_DIR / "enriched_items.json"
SOURCE_CANDIDATES_PATH = DATA_DIR / "source_candidates.json"
FEEDBACK_PATH = DATA_DIR / "feedback.jsonl"


def run_pipeline(
    limit: int = 10,
    fixture_path: Path = FIXTURE_PATH,
    discovered_path: Path = DISCOVERED_ITEMS_PATH,
    include_fixtures: bool = True,
    fetch_backend: str = "",
    fetch_mock: bool = False,
    fetch_query_limit: int = 0,
    fetch_lookback_days: int = 0,
    fetch_pillars=None,
    fetch_timespan=None,
    fetch_max=None,
) -> Dict[str, Any]:
    ensure_dirs()
    seed_payload = read_json(SEEDS_PATH, {"sources": []})
    weights = read_json(WEIGHTS_PATH, {})
    source_weights = read_json(SOURCE_WEIGHTS_PATH, {})
    if fetch_backend == "last30days":
        fetch_last30days(
            mock=fetch_mock,
            query_limit=fetch_query_limit or None,
            lookback_days=fetch_lookback_days or None,
        )
    fixture_payload = read_json(fixture_path, {"items": []}) if include_fixtures else {"items": []}
    discovered_payload = read_json(discovered_path, {"items": []})
    feedback_events = read_jsonl(FEEDBACK_PATH)

    raw_items = load_raw_items(seed_payload, [fixture_payload, discovered_payload])
    scored_items = score_items(raw_items, weights, feedback_events, source_weights)
    top_items = scored_items[:limit]
    digest_path = OUTPUT_DIR / f"signal-room-digest-{date.today().isoformat()}.html"

    write_json(RAW_PATH, [item.to_dict() for item in raw_items])
    write_json(ENRICHED_PATH, [item.to_dict() for item in scored_items])
    write_json(SOURCE_CANDIDATES_PATH, source_candidates(seed_payload, raw_items))
    render_digest(top_items, digest_path)

    return {
        "raw_items": len(raw_items),
        "scored_items": len(scored_items),
        "top_items": len(top_items),
        "digest_path": str(digest_path),
        "raw_path": str(RAW_PATH),
        "enriched_path": str(ENRICHED_PATH),
        "source_candidates_path": str(SOURCE_CANDIDATES_PATH),
        "feedback_path": str(FEEDBACK_PATH),
        "discovered_items_path": str(discovered_path),
    }


def load_enriched_items() -> List[ScoredItem]:
    payload = read_json(ENRICHED_PATH, [])
    items = []
    for row in payload:
        items.append(
            ScoredItem(
                id=row["id"],
                title=row["title"],
                source=row["source"],
                source_url=row["source_url"],
                date=row["date"],
                summary=row["summary"],
                pillar_fit=row["pillar_fit"],
                surf_fit=row["surf_fit"],
                mechanism_present=row["mechanism_present"],
                score=row["score"],
                reason_for_score=row["reason_for_score"],
                why_ce_should_care=row["why_ce_should_care"],
                suggested_ce_angle=row["suggested_ce_angle"],
                possible_ce_take=row["possible_ce_take"],
                follow_up_search_query=row["follow_up_search_query"],
                discovery_method=row["discovery_method"],
                candidate_source=row["candidate_source"],
                feedback_counts=row.get("feedback_counts", {}),
                source_weight=row.get("source_weight", 0.0),
            )
        )
    return items
