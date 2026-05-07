from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .fetchers.last30days import Last30DaysError, fetch_last30days
from .ingest import load_raw_items
from .query_lab import SEEDS_PATH
from .storage import DATA_DIR, read_json
from .title_enrichment import clean_result_titles
from .traction import rank_items_by_traction
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
        store.record_run_event(run_id, "Ranking fetched results by social traction", kind="running", item_count=len(fetch_summary["items"]))
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
        time.sleep(poll_seconds)


def _score_fetch_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seed_payload = read_json(SEEDS_PATH, {"sources": []})
    raw_items = load_raw_items(seed_payload, [{"items": items}])
    return rank_items_by_traction(raw_items)


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
