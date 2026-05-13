from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .fetchers.last30days import Last30DaysError, fetch_last30days
from .ingest import load_raw_items
from .pipeline import SOURCE_WEIGHTS_PATH
from .query_lab import FEEDBACK_PATH, SEEDS_PATH, WEIGHTS_PATH
from .scoring import score_items
from .storage import DATA_DIR, read_json, read_jsonl
from .title_enrichment import clean_result_titles
from .web_store import SignalRoomStore


DEFAULT_SOURCES = ["grounding", "x", "youtube", "instagram", "github", "reddit", "hackernews"]


def process_run(store: SignalRoomStore, run: dict[str, Any], mock: bool = False) -> None:
    run_id = str(run["id"])
    query = str(run["query"])
    sources = list(run.get("sources") or DEFAULT_SOURCES)
    lookback_days = int(run.get("lookback_days") or 30)
    store.mark_run_status(run_id, "running")
    store.record_run_event(run_id, f"Worker picked up search: {query}", kind="running")
    try:
        fetch_summary = _fetch_sources(run_id, query, sources, lookback_days, mock, store)
        store.record_run_event(run_id, "Scoring and ranking fetched results", kind="running", item_count=len(fetch_summary["items"]))
        scored = _score_fetch_items(fetch_summary["items"])
        if scored:
            store.record_run_event(run_id, f"Cleaning titles for {len(scored)} results", kind="running", item_count=len(scored))
            scored, title_warning = clean_result_titles(scored)
            if title_warning:
                store.record_run_event(run_id, title_warning, kind="warning", item_count=len(scored))
            else:
                store.record_run_event(run_id, f"Cleaned titles for {len(scored)} results", kind="complete", item_count=len(scored))
        store.replace_run_items(run_id, scored)
        if scored:
            store.mark_run_status(run_id, "complete", error=_error_text(fetch_summary["errors"]), item_count=len(scored))
            store.record_run_event(run_id, f"Search complete with {len(scored)} scored results", kind="complete", item_count=len(scored))
        elif fetch_summary["errors"]:
            store.mark_run_status(run_id, "failed", error=_error_text(fetch_summary["errors"]))
            store.record_run_event(run_id, "Search failed before any results were found", kind="error")
        else:
            store.mark_run_status(run_id, "complete", item_count=0)
            store.record_run_event(run_id, "Search complete with no results", kind="complete")
    except Exception as exc:
        store.mark_run_status(run_id, "failed", error=str(exc))
        store.record_run_event(run_id, f"Worker failed: {exc}", kind="error")


def run_forever(poll_seconds: int = 5) -> None:
    store = SignalRoomStore()
    store.initialize()
    mock = os.environ.get("SIGNAL_ROOM_FETCH_MOCK", "").lower() in {"1", "true", "yes"}
    while True:
        run = store.next_queued_run()
        if run:
            process_run(store, run, mock=mock)
            continue
        brand_run = store.next_queued_brand_run()
        if brand_run:
            try:
                process_brand_refetch(store, brand_run, mock=mock)
            except Exception as exc:
                # Already marked failed inside; just log and continue polling.
                print(f"[worker] brand refetch {brand_run.get('id')} crashed: {exc}", flush=True)
            continue
        time.sleep(poll_seconds)


def process_brand_refetch(store: SignalRoomStore, run: dict[str, Any], mock: bool = False) -> None:
    """Full pipeline run for one brand: plan → fetch both → score → digest → trace.

    Persists all artifacts (trace.jsonl, trace.html, digest html, plans JSON,
    summary counts) back into the brand_runs row so the web layer can render
    `/{brand}` without touching the filesystem.
    """
    import json as _json
    from datetime import date as _date
    from pathlib import Path as _Path

    run_id = str(run["id"])
    brand = str(run["brand"])
    store.mark_brand_run_started(run_id)
    try:
        import tempfile as _tempfile
        from . import planner as _planner
        from .pipeline import run_pipeline as _run_pipeline, OUTPUT_DIR as _OUTPUT_DIR
        from .tracer import tracer as _tracer
        import yaml as _yaml

        # Resolve brief: DB is authoritative (Render filesystem is ephemeral
        # and runtime-onboarded brands only live in Postgres). Fall back to
        # the repo's filesystem brief.yaml for legacy/local-dev brands.
        brand_row = store.get_brand(brand)
        brief_dict_from_db = (brand_row or {}).get("brief_json") or {}
        repo_brand_dir = _Path("config") / "brands" / brand
        repo_brief_path = repo_brand_dir / "brief.yaml"

        if brief_dict_from_db and brief_dict_from_db.get("pillars"):
            # Materialize a temp brief.yaml (LLM scorer + projector read from disk).
            # Wrap in the projection envelope the projector expects.
            wrapped = brief_dict_from_db if "projection" in brief_dict_from_db else {
                "brand": {
                    "name": brief_dict_from_db.get("name", brand),
                    "slug": brand,
                    "url": brief_dict_from_db.get("url", ""),
                    "one_liner": brief_dict_from_db.get("one_liner", ""),
                    "audience": brief_dict_from_db.get("audience", []),
                },
                "projection": {
                    "signal_room": {
                        "pillars": brief_dict_from_db.get("pillars", []),
                        "discovery_queries": brief_dict_from_db.get("discovery_queries", []),
                        "seed_sources": brief_dict_from_db.get("seed_sources", []),
                    },
                },
            }
            brand_dir = _Path(_tempfile.gettempdir()) / f"sr-brand-{brand}-{run_id}"
            brand_dir.mkdir(parents=True, exist_ok=True)
            brief_path = brand_dir / "brief.yaml"
            brief_path.write_text(_yaml.safe_dump(wrapped, sort_keys=False, allow_unicode=True), encoding="utf-8")
        elif repo_brief_path.exists():
            brand_dir = repo_brand_dir
            brief_path = repo_brief_path
        else:
            raise FileNotFoundError(
                f"No brief in DB or on disk for {brand}. "
                "Finish onboarding (or paste a brief into the editor) before refetching."
            )

        # Generate plans per discovery query, write into the brand's plans/ dir
        # so pipeline auto-attaches plan_path. Also stash in-memory for DB store.
        brief = _yaml.safe_load(brief_path.read_text(encoding="utf-8")) or {}
        queries = (((brief.get("projection") or {}).get("signal_room") or {}).get("discovery_queries") or [])
        plans_by_qid: dict[str, Any] = {}
        plans_dir = brand_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        for q in queries:
            if not isinstance(q, dict) or not q.get("id"):
                continue
            if mock:
                # Skip Claude calls in mock mode; vendor planner fallback runs.
                continue
            try:
                plan = _planner.plan_query(brief_path, q)
                qid = str(q["id"])
                (plans_dir / f"{qid}.json").write_text(_json.dumps(plan, indent=2) + "\n", encoding="utf-8")
                plans_by_qid[qid] = plan
            except Exception as exc:
                plans_by_qid.setdefault(str(q.get("id", "?")), {"error": str(exc)})

        # Enable tracer for this run.
        traces_dir = DATA_DIR / "traces"
        _tracer.enable(brand=brand, run_dir=traces_dir)

        # Fire the full pipeline (both backends combined). Brand isolation
        # writes to data/<brand>/*.json and config/brands/<brand>/plans/*.json.
        summary = _run_pipeline(
            brief_path=brief_path,
            brand_config_dir=brand_dir,
            data_suffix=brand,
            fetch_backend="both",
            fetch_mock=mock,
            fetch_parallelism=4,
            include_fixtures=False,
        )

        # Flush tracer to disk and read it back for DB storage.
        trace_jsonl_path = _tracer.flush()
        trace_html_path = _tracer.flush_html(jsonl_path=trace_jsonl_path)
        trace_jsonl = trace_jsonl_path.read_text(encoding="utf-8") if trace_jsonl_path else ""
        trace_html = trace_html_path.read_text(encoding="utf-8") if trace_html_path else ""

        # Pipeline wrote digest to OUTPUT_DIR / signal-room-digest-<brand>-<date>.html
        digest_path = _OUTPUT_DIR / f"signal-room-digest-{brand}-{_date.today().isoformat()}.html"
        digest_html = digest_path.read_text(encoding="utf-8") if digest_path.exists() else ""

        store.store_brand_run_artifacts(
            run_id,
            trace_jsonl=trace_jsonl,
            trace_html=trace_html,
            digest_html=digest_html,
            plans_json=plans_by_qid,
        )

        store.mark_brand_run_done(run_id, {
            "raw_items": summary.get("raw_items", 0),
            "scored_items": summary.get("scored_items", 0),
            "top_items": summary.get("top_items", 0),
            "digest_path": summary.get("digest_path"),
            "trace_jsonl_path": str(trace_jsonl_path) if trace_jsonl_path else None,
            "trace_html_path": str(trace_html_path) if trace_html_path else None,
            "plans_generated": len(plans_by_qid),
        })

        # Retention: keep only N most-recent runs per brand.
        keep = int(os.environ.get("SIGNAL_ROOM_BRAND_RUN_KEEP", "10"))
        store.prune_brand_runs(brand, keep=keep)
    except Exception as exc:
        store.mark_brand_run_failed(run_id, str(exc))
        raise


def _score_fetch_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seed_payload = read_json(SEEDS_PATH, {"sources": []})
    weights = read_json(WEIGHTS_PATH, {})
    source_weights = read_json(SOURCE_WEIGHTS_PATH, {})
    feedback_events = read_jsonl(FEEDBACK_PATH)
    raw_items = load_raw_items(seed_payload, [{"items": items}])
    raw_by_id = {item.id: item.to_dict() for item in raw_items}
    rows = []
    for item in score_items(raw_items, weights, feedback_events, source_weights):
        payload = dict(raw_by_id.get(item.id, {}))
        payload.update(item.to_dict())
        rows.append(payload)
    return rows


def _fetch_sources(
    run_id: str,
    query: str,
    sources: list[str],
    lookback_days: int,
    mock: bool,
    store: SignalRoomStore,
) -> dict[str, Any]:
    if len(sources) <= 1:
        source = sources[0] if sources else "default"
        store.record_run_event(run_id, f"Searching {source}", kind="running", source=source)
        return _fetch_one_source(run_id, query, source, lookback_days, mock)

    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    max_workers = _source_parallelism(len(sources))
    store.record_run_event(run_id, f"Starting {len(sources)} sources with {max_workers} workers", kind="running")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_fetch_one_source_with_progress, store, run_id, query, source, lookback_days, mock): source
            for source in sources
        }
        for future in as_completed(future_map):
            source = future_map[future]
            try:
                summary = future.result()
            except Exception as exc:
                errors.append({"source": source, "error": str(exc)})
                store.record_run_event(run_id, f"{source} failed: {exc}", kind="error", source=source)
                continue
            source_items = summary.get("items", [])
            source_errors = summary.get("errors", [])
            items.extend(source_items)
            errors.extend(source_errors)
            scored = _score_fetch_items(items)
            store.replace_run_items(run_id, scored)
            store.mark_run_status(run_id, "running", error=_error_text(errors), item_count=len(scored))
            if source_errors:
                store.record_run_event(
                    run_id,
                    f"{source} returned {len(source_items)} items with a warning",
                    kind="warning",
                    source=source,
                    item_count=len(source_items),
                )
            else:
                store.record_run_event(
                    run_id,
                    f"{source} returned {len(source_items)} items",
                    kind="complete",
                    source=source,
                    item_count=len(source_items),
                )
    return {"items": items, "errors": errors}


def _fetch_one_source_with_progress(
    store: SignalRoomStore,
    run_id: str,
    query: str,
    source: str,
    lookback_days: int,
    mock: bool,
) -> dict[str, Any]:
    store.record_run_event(run_id, f"Searching {source}", kind="running", source=source)
    return _fetch_one_source(run_id, query, source, lookback_days, mock)


def _fetch_one_source(
    run_id: str,
    query: str,
    source: str,
    lookback_days: int,
    mock: bool,
) -> dict[str, Any]:
    return fetch_last30days(
        queries=[
            {
                "id": f"web-{run_id}-{source}",
                "topic": query,
                "search_text": query,
                "why": f"Signal Room web search via {source}",
                "priority": 1,
                "search_sources": [source],
                "lookback_days": lookback_days,
            }
        ],
        search_sources=[source],
        mock=mock,
        continue_on_error=True,
        run_root=_run_root() / run_id,
        output_path=None,
        parallelism=1,
        lookback_days=lookback_days,
    )


def _source_parallelism(source_count: int) -> int:
    raw_value = os.environ.get("SIGNAL_ROOM_SOURCE_PARALLELISM", "4")
    try:
        configured = int(raw_value)
    except ValueError:
        configured = 4
    return max(1, min(source_count, configured))


def _error_text(errors: list[dict[str, str]]) -> str:
    if not errors:
        return ""
    parts = []
    for error in errors:
        source = error.get("source") or error.get("query_id") or "source"
        parts.append(f"{source}: {error.get('error', 'failed')}")
    return "; ".join(parts)


def _run_root() -> Path:
    raw_path = os.environ.get("SIGNAL_ROOM_RUN_ROOT", "")
    if raw_path:
        return Path(raw_path)
    return Path("/tmp/signal-room-runs") if os.environ.get("RENDER") else DATA_DIR / "web_runs"


if __name__ == "__main__":
    run_forever()
