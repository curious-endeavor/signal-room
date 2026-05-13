from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from .digest import render_digest
from .ingest import load_raw_items, source_candidates
from .fetchers.last30days import DISCOVERED_ITEMS_PATH, fetch_last30days
from .models import ScoredItem
from .scoring import score_items
from .llm_scoring import score_items_with_brief
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
from .tracer import tracer


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
    fetch_parallelism: int = 4,
    fetch_sources: List[str] = None,
    brief_path: Path = None,
    llm_model: str = "claude-sonnet-4-6",
    brand_config_dir: Path = None,
    data_suffix: str = "",
) -> Dict[str, Any]:
    ensure_dirs()
    # Per-brand isolation: when data_suffix is set, all intermediate writes go
    # to DATA_DIR/<suffix>/ so parallel runs against different brands don't
    # collide on raw_items/enriched_items/discovered_items/etc.
    run_data_dir = DATA_DIR / data_suffix if data_suffix else DATA_DIR
    run_data_dir.mkdir(parents=True, exist_ok=True)
    raw_path = run_data_dir / "raw_items.json"
    enriched_path = run_data_dir / "enriched_items.json"
    source_candidates_path = run_data_dir / "source_candidates.json"
    if data_suffix:
        # When isolating, never read the global discovered_items.json — write
        # a brand-scoped one and read it back. Caller can still override.
        if discovered_path == DISCOVERED_ITEMS_PATH:
            discovered_path = run_data_dir / "discovered_items.json"
    # Brand-scoped config: read seed_sources from the brand dir when provided,
    # else fall back to top-level config/.
    seeds_path = (Path(brand_config_dir) / "seed_sources.json") if brand_config_dir else SEEDS_PATH
    seed_payload = read_json(seeds_path, read_json(SEEDS_PATH, {"sources": []}))
    weights = read_json(WEIGHTS_PATH, {})
    source_weights = read_json(SOURCE_WEIGHTS_PATH, {})
    tracer.record("pipeline_started", {
        "limit": limit,
        "include_fixtures": include_fixtures,
        "fetch_backend": fetch_backend or None,
        "fetch_mock": fetch_mock,
        "brief_path": str(brief_path) if brief_path else None,
        "llm_model": llm_model if brief_path else None,
        "seed_source_count": len(seed_payload.get("sources", [])),
    })
    if brief_path:
        try:
            brief_text = Path(brief_path).read_text(encoding="utf-8")
            tracer.record("brief_loaded", {
                "path": str(brief_path),
                "size_bytes": len(brief_text.encode("utf-8")),
                "excerpt_first_400": brief_text[:400],
            })
        except Exception as exc:
            tracer.record("brief_load_error", {"path": str(brief_path), "error": str(exc)})
    if fetch_backend == "last30days":
        # Load queries from the brand's config dir if provided (so parallel
        # runs use the right brand's queries, not whatever was last projected
        # to top-level config/).
        brand_queries = None
        if brand_config_dir:
            brand_queries_payload = read_json(Path(brand_config_dir) / "discovery_queries.json", {})
            brand_queries = brand_queries_payload.get("queries") or None
        # Brand-scoped runs dir for vendor /last30days subprocess outputs.
        from .storage import LAST30DAYS_RUNS_DIR as _RUNS_DIR
        from datetime import date as _date
        run_root = (_RUNS_DIR / (data_suffix or "")) / _date.today().isoformat() if data_suffix else None
        fetch_last30days(
            mock=fetch_mock,
            query_limit=fetch_query_limit or None,
            lookback_days=fetch_lookback_days or None,
            parallelism=fetch_parallelism,
            search_sources=fetch_sources,
            queries=brand_queries,
            output_path=discovered_path,
            run_root=run_root,
        )
    fixture_payload = read_json(fixture_path, {"items": []}) if include_fixtures else {"items": []}
    discovered_payload = read_json(discovered_path, {"items": []})
    feedback_events = read_jsonl(FEEDBACK_PATH)
    tracer.record("inputs_assembled", {
        "fixture_item_count": len(fixture_payload.get("items", [])),
        "discovered_item_count": len(discovered_payload.get("items", [])),
        "feedback_event_count": len(feedback_events),
    })

    raw_items_pre_dedup_count = (
        len(fixture_payload.get("items", [])) + len(discovered_payload.get("items", []))
    )
    raw_items = load_raw_items(seed_payload, [fixture_payload, discovered_payload])
    tracer.record("dedup_decision", {
        "input_count": raw_items_pre_dedup_count,
        "output_count": len(raw_items),
        "dropped_count": raw_items_pre_dedup_count - len(raw_items),
        "sample_items": [
            {
                "id": item.id,
                "title": item.title[:120],
                "source": item.source,
                "source_url": item.source_url,
                "discovery_method": item.discovery_method,
            }
            for item in raw_items[:50]
        ],
    })
    if brief_path:
        scored_items = score_items_with_brief(raw_items, brief_path, model=llm_model)
    else:
        scored_items = score_items(raw_items, weights, feedback_events, source_weights)
        tracer.record("keyword_scoring_complete", {
            "scored_count": len(scored_items),
            "score_distribution": _score_buckets(scored_items),
        })
    top_items = scored_items[:limit]
    digest_filename = (
        f"signal-room-digest-{data_suffix}-{date.today().isoformat()}.html"
        if data_suffix else f"signal-room-digest-{date.today().isoformat()}.html"
    )
    digest_path = OUTPUT_DIR / digest_filename

    write_json(raw_path, [item.to_dict() for item in raw_items])
    write_json(enriched_path, [item.to_dict() for item in scored_items])
    write_json(source_candidates_path, source_candidates(seed_payload, raw_items))
    render_digest(top_items, digest_path)
    tracer.record("digest_built", {
        "path": str(digest_path),
        "top_count": len(top_items),
        "top_item_ids": [item.id for item in top_items],
        "top_summaries": [
            {
                "id": item.id,
                "score": item.score,
                "pillar_fit": item.pillar_fit,
                "title": item.title[:120],
                "source": item.source,
            }
            for item in top_items
        ],
    })

    return {
        "raw_items": len(raw_items),
        "scored_items": len(scored_items),
        "top_items": len(top_items),
        "digest_path": str(digest_path),
        "raw_path": str(raw_path),
        "enriched_path": str(enriched_path),
        "source_candidates_path": str(source_candidates_path),
        "feedback_path": str(FEEDBACK_PATH),
        "discovered_items_path": str(discovered_path),
    }


def _score_buckets(items):
    buckets = {"80-100": 0, "60-79": 0, "40-59": 0, "0-39": 0}
    for item in items:
        s = getattr(item, "score", 0) or 0
        if s >= 80:
            buckets["80-100"] += 1
        elif s >= 60:
            buckets["60-79"] += 1
        elif s >= 40:
            buckets["40-59"] += 1
        else:
            buckets["0-39"] += 1
    return buckets


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
