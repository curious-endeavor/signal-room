from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from .digest import render_digest
from .discovery_store import write_merged_discovered_items
from .ingest import load_raw_items, source_candidates
from .fetchers.gdelt import fetch_gdelt
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
    fetch_pillars=None,
    fetch_timespan=None,
    fetch_max=None,
    slim_cap: int = 0,
    channel_filter: list = None,
) -> Dict[str, Any]:
    """If slim_cap > 0, raw_items are truncated to that many entries before
    LLM scoring runs. Used in dev to iterate on scoring quality without paying
    for 179 LLM calls every loop. Truncation is post-dedup, pre-score, so the
    items that survive are a representative slice of what would have been
    scored, just smaller."""
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
    # `cache` mode: skip both fetchers, reuse the existing discovered_items.json
    # for this brand. Lets the user iterate on scoring/slim/digest without
    # paying for ~5 minutes of vendor work + LLM planner calls every loop.
    if fetch_backend == "cache":
        if not discovered_path.exists():
            tracer.record("cache_miss", {"discovered_path": str(discovered_path)})
            raise FileNotFoundError(
                f"No cached fetch at {discovered_path}. Run a normal Refetch first."
            )
        tracer.record("cache_reused", {"discovered_path": str(discovered_path)})
    elif fetch_backend in {"last30days", "gdelt", "both"}:
        payloads = []
        if fetch_backend in {"last30days", "both"}:
            # Brand-aware path: load queries from the brand's config dir if
            # provided (so parallel runs use the right brand's queries, not
            # whatever was last projected to top-level config/). Plans on
            # disk at <brand>/plans/<qid>.json are auto-attached by
            # _load_queries → passed to vendor via `--plan <path>`.
            brand_queries = None
            if brand_config_dir:
                brand_queries_payload = read_json(Path(brand_config_dir) / "discovery_queries.json", {})
                brand_queries = brand_queries_payload.get("queries") or None
                # When a per-brand plans dir is present, auto-attach plan_path so
                # the vendor gets `--plan <path>` and skips its internal grok planner.
                if brand_queries:
                    brand_plans_dir = Path(brand_config_dir) / "plans"
                    if brand_plans_dir.exists():
                        for q in brand_queries:
                            qid = str(q.get("id", ""))
                            candidate = brand_plans_dir / f"{qid}.json"
                            if qid and candidate.exists():
                                q["plan_path"] = str(candidate)
            # Brand-scoped runs dir for vendor /last30days subprocess outputs.
            from .storage import LAST30DAYS_RUNS_DIR as _RUNS_DIR
            from datetime import date as _date
            run_root = (_RUNS_DIR / (data_suffix or "")) / _date.today().isoformat() if data_suffix else None
            payloads.append(fetch_last30days(
                mock=fetch_mock,
                query_limit=fetch_query_limit or None,
                lookback_days=fetch_lookback_days or None,
                parallelism=fetch_parallelism,
                search_sources=fetch_sources,
                queries=brand_queries,
                output_path=None,
                run_root=run_root,
                # One slow / wedged vendor subprocess used to kill the whole
                # pipeline. Keep going on per-query failures (timeout, JSON
                # parse, vendor exit≠0) — the run_events log captures each
                # individual error, and what we recover is more useful than
                # nothing.
                continue_on_error=True,
            ))
        if fetch_backend in {"gdelt", "both"}:
            # Per-brand GDELT pillars file: the worker projects the brief's
            # pillars (keywords) into gdelt_pillars.json (boolean queries) and
            # drops it into brand_config_dir. Pass that path so GDELT uses the
            # right brand's pillars instead of the global home-dir default.
            brand_gdelt_pillars = None
            if brand_config_dir:
                candidate = Path(brand_config_dir) / "gdelt_pillars.json"
                if candidate.exists():
                    brand_gdelt_pillars = candidate
            payloads.append(fetch_gdelt(
                pillars=fetch_pillars,
                timespan=fetch_timespan or None,
                max_records=fetch_max or None,
                mock=fetch_mock,
                output_path=None,
                pillars_path=brand_gdelt_pillars,
            ))
        write_merged_discovered_items(discovered_path, payloads)
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
    # Channel filter (only meaningful when reading from cache): keep items
    # whose discovery_method is in the allowed list. Lets the user say
    # "rescore GDELT-only from cache" without disturbing the cached fetch.
    if channel_filter:
        allowed = set(channel_filter)
        before = len(raw_items)
        raw_items = [r for r in raw_items if (r.discovery_method or "") in allowed]
        if before != len(raw_items):
            tracer.record("channel_filter_applied", {
                "allowed": sorted(allowed),
                "before": before,
                "after": len(raw_items),
            })

    # Slim-run: cap items before LLM scoring. Stratified by discovery_method
    # so GDELT items (which come last in raw_items) don't get wiped out by a
    # naive [:slim_cap]. Allocate the budget proportionally and pull in the
    # original order within each bucket. Falls back to a flat truncate if
    # there's only one source.
    if slim_cap and slim_cap > 0 and len(raw_items) > slim_cap:
        pre_slim_count = len(raw_items)
        buckets: dict[str, list] = {}
        for item in raw_items:
            key = getattr(item, "discovery_method", None) or "unknown"
            buckets.setdefault(key, []).append(item)
        if len(buckets) <= 1:
            kept = raw_items[:slim_cap]
            allocation = {next(iter(buckets), "unknown"): len(kept)}
        else:
            total = sum(len(v) for v in buckets.values())
            # First pass: floor allocation so we don't overshoot the cap.
            allocation = {k: max(1, (slim_cap * len(v)) // total) for k, v in buckets.items()}
            # Trim/grow so the totals match slim_cap exactly. Largest buckets
            # absorb the residue.
            diff = slim_cap - sum(allocation.values())
            if diff != 0:
                order = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))
                i = 0
                step = 1 if diff > 0 else -1
                while diff != 0 and i < len(order) * 4:
                    k = order[i % len(order)]
                    if allocation[k] + step >= 1 and allocation[k] + step <= len(buckets[k]):
                        allocation[k] += step
                        diff -= step
                    i += 1
            kept = []
            for k, n in allocation.items():
                kept.extend(buckets[k][:n])
        raw_items = kept
        tracer.record("slim_cap_applied", {
            "cap": slim_cap,
            "pre_count": pre_slim_count,
            "post_count": len(raw_items),
            "allocation": allocation,
        })

    if brief_path:
        scored_items = score_items_with_brief(raw_items, brief_path, model=llm_model)
    else:
        scored_items = score_items(raw_items, weights, feedback_events, source_weights)
        tracer.record("keyword_scoring_complete", {
            "scored_count": len(scored_items),
            "score_distribution": _score_buckets(scored_items),
        })
    # Cluster near-duplicate stories by LLM-emitted `story_key` in metadata.
    # Same key (e.g. "openai-chatgpt-teen-overdose-lawsuit-2026") = same
    # underlying event across multiple outlets. The highest-scored item in
    # each cluster becomes the representative; the rest get attached as
    # `cluster_members` in its metadata so the digest can show a "+N more
    # outlets" pickup signal. Items without a story_key (or older runs
    # that pre-date the field) cluster individually by their own id.
    clusters: dict[str, list] = {}
    cluster_order: list[str] = []
    for item in scored_items:
        key = (item.metadata or {}).get("story_key") or f"unique-{item.id[:12]}"
        if key not in clusters:
            clusters[key] = []
            cluster_order.append(key)
        clusters[key].append(item)

    deduped: list = []
    for key in cluster_order:
        members = sorted(clusters[key], key=lambda s: s.score, reverse=True)
        rep = members[0]
        if len(members) > 1:
            rep.metadata = {
                **(rep.metadata or {}),
                "cluster_size": len(members),
                "cluster_members": [
                    {
                        "id": m.id,
                        "title": m.title,
                        "source": m.source,
                        "source_url": m.source_url,
                        "score": m.score,
                    }
                    for m in members[1:]
                ],
            }
        deduped.append(rep)
    deduped.sort(key=lambda s: s.score, reverse=True)
    tracer.record("story_clustering", {
        "raw_scored": len(scored_items),
        "after_dedup": len(deduped),
        "clusters_with_dupes": sum(1 for k in clusters if len(clusters[k]) > 1),
        "largest_cluster": max((len(v) for v in clusters.values()), default=0),
    })

    top_items = deduped[:limit]
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
