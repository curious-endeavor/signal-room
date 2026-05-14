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
    """Polling loop that dispatches brand refetches across a thread pool so
    multiple brands can run in parallel. Search runs (legacy `runs` table)
    stay serial — they aren't on the hot path for brand work.

    Concurrency is bounded by SIGNAL_ROOM_WORKER_PARALLEL (default 3). A
    single brand can never have two simultaneous runs because
    `claim_next_brand_run` skips brands that already have a row in `running`.
    The tracer is per-thread (see tracer._TracerProxy), so listeners and
    record buffers don't cross between threads.
    """
    store = SignalRoomStore()
    store.initialize()
    mock = os.environ.get("SIGNAL_ROOM_FETCH_MOCK", "").lower() in {"1", "true", "yes"}
    max_parallel = max(1, int(os.environ.get("SIGNAL_ROOM_WORKER_PARALLEL", "3")))
    print(f"[worker] started · max_parallel={max_parallel} · mock={mock} · slim={os.environ.get('SIGNAL_ROOM_SLIM_RUN','')}", flush=True)
    executor = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="sr-worker")
    in_flight: dict = {}  # future -> (brand, run_id)

    def _run_brand_safe(claimed_run: dict) -> None:
        rid = claimed_run.get("id")
        brand = claimed_run.get("brand")
        try:
            process_brand_refetch(store, claimed_run, mock=mock)
        except Exception as exc:
            print(f"[worker] brand refetch {rid} ({brand}) crashed: {exc}", flush=True)

    try:
        while True:
            # Reap finished futures.
            for f in [fut for fut in in_flight if fut.done()]:
                brand, rid = in_flight.pop(f)
                # Surface exceptions from the worker thread; status/error were
                # already persisted by process_brand_refetch.
                exc = f.exception()
                if exc:
                    print(f"[worker] {brand}/{rid} thread raised: {exc}", flush=True)
                else:
                    print(f"[worker] {brand}/{rid} thread finished", flush=True)

            # Fill spare capacity with newly-claimed brand runs.
            dispatched = 0
            while len(in_flight) < max_parallel:
                claimed = store.claim_next_brand_run()
                if not claimed:
                    break
                fut = executor.submit(_run_brand_safe, claimed)
                in_flight[fut] = (claimed.get("brand"), claimed.get("id"))
                dispatched += 1
                print(f"[worker] dispatched {claimed.get('brand')}/{claimed.get('id')} · in_flight={len(in_flight)}/{max_parallel}", flush=True)

            # Legacy search-runs path stays serial: one run, blocking. Skip
            # when we're at capacity so we don't starve brand refetches.
            if len(in_flight) < max_parallel:
                search_run = store.next_queued_run()
                if search_run:
                    process_run(store, search_run, mock=mock)
                    continue

            if dispatched == 0:
                time.sleep(poll_seconds)
    finally:
        executor.shutdown(wait=True)


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
    # Per-run options live in plans_json under "options". Set at queue time by
    # the /{brand}/refetch form. plans_json gets overwritten with real plans
    # at end-of-run, which is fine — options only need to live until start.
    opts = ((run.get("plans_json") or {}).get("options") or {}) if isinstance(run.get("plans_json"), dict) else {}
    reuse_cache = bool(opts.get("reuse_cache"))
    channels = list(opts.get("channels") or ["last30days", "gdelt"])
    options_slim = opts.get("slim")
    store.mark_brand_run_started(run_id)
    if opts:
        store.record_run_event(run_id, f"Run started for {brand} · options={opts}", kind="running")
    else:
        store.record_run_event(run_id, f"Run started for {brand}", kind="running")
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

        store.record_run_event(run_id, f"Resolving brief for {brand}", kind="running")
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
            # Project the brief into the discovery_queries.json / pillar_keywords.json /
            # seed_sources.json files the pipeline + fetchers actually read. Without
            # this, the fetcher falls back to the GLOBAL config/discovery_queries.json
            # (a legacy file from the CE-only era) and runs the wrong brand's queries.
            # Slim mode: also cap the number of discovery queries planned + fetched,
            # so dev loops don't pay for 10 planner Claude calls + 10 vendor scrapes
            # when 4 is enough to evaluate scoring quality. Default 4; override with
            # SIGNAL_ROOM_SLIM_QUERIES. Truncation happens on the wrapped projection
            # BEFORE we write discovery_queries.json or run the planner loop, so the
            # fetcher only ever sees the slim subset.
            slim_run_for_queries = os.environ.get("SIGNAL_ROOM_SLIM_RUN", "").lower() in {"1", "true", "yes"}
            if slim_run_for_queries:
                q_cap = max(1, int(os.environ.get("SIGNAL_ROOM_SLIM_QUERIES", "4")))
                sr_block = ((wrapped.get("projection") or {}).get("signal_room") or {})
                full_qs = sr_block.get("discovery_queries") or []
                if len(full_qs) > q_cap:
                    sr_block["discovery_queries"] = full_qs[:q_cap]
                    # rewrite brief.yaml so planning + scoring see the truncated list
                    brief_path.write_text(
                        _yaml.safe_dump(wrapped, sort_keys=False, allow_unicode=True),
                        encoding="utf-8",
                    )
                    store.record_run_event(
                        run_id,
                        f"Slim queries: capped {len(full_qs)} → {q_cap}",
                        kind="running",
                    )
            try:
                from .projector.from_brief import (
                    project_discovery_queries as _proj_dq,
                    project_seed_sources as _proj_ss,
                    project_pillar_keywords as _proj_pk,
                    project_gdelt_pillars as _proj_gp,
                )
                projection = wrapped.get("projection") or {}
                (brand_dir / "discovery_queries.json").write_text(
                    _json.dumps(_proj_dq(projection), indent=2) + "\n", encoding="utf-8")
                (brand_dir / "seed_sources.json").write_text(
                    _json.dumps(_proj_ss(projection), indent=2) + "\n", encoding="utf-8")
                (brand_dir / "pillar_keywords.json").write_text(
                    _json.dumps(_proj_pk(projection), indent=2) + "\n", encoding="utf-8")
                # GDELT-shaped pillars (name + boolean query). Without this the
                # GDELT fetcher reads ~/.config/gdelt-pp-cli/pillars.json — a
                # global file that holds whatever brand was last operated on,
                # producing the wrong-brand contamination we just diagnosed.
                gdelt_pillars_path = brand_dir / "gdelt_pillars.json"
                gdelt_pillars_path.write_text(
                    _json.dumps(_proj_gp(projection), indent=2) + "\n", encoding="utf-8")
            except Exception as exc:
                store.record_run_event(run_id, f"Projector failed: {exc}", kind="warning")
            store.record_run_event(run_id, f"Brief loaded from DB ({len(brief_dict_from_db.get('pillars') or [])} pillars, {len(((wrapped.get('projection') or {}).get('signal_room') or {}).get('discovery_queries') or [])} queries)", kind="running")
        elif repo_brief_path.exists():
            brand_dir = repo_brand_dir
            brief_path = repo_brief_path
            store.record_run_event(run_id, f"Brief loaded from disk: {repo_brief_path}", kind="running")
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
        # Planning only matters when we're about to fetch — the plans are
        # consumed by the last30days fetcher. In cache mode the fetch is
        # skipped, so paying for 12 Claude planner calls would be pure waste.
        # Same for gdelt-only runs (the planner only feeds last30days).
        skip_planning = reuse_cache or ("last30days" not in channels)
        if skip_planning:
            reason = "reuse_cache" if reuse_cache else f"channels={channels} excludes last30days"
            store.record_run_event(
                run_id,
                f"Planning skipped ({reason})",
                kind="running",
            )
            queries_to_plan = []
        else:
            store.record_run_event(run_id, f"Planning {len(queries)} discovery queries", kind="running", item_count=len(queries))
            queries_to_plan = queries
        for q in queries_to_plan:
            if not isinstance(q, dict) or not q.get("id"):
                continue
            if mock:
                # Skip Claude calls in mock mode; vendor planner fallback runs.
                continue
            qid = str(q["id"])
            try:
                plan = _planner.plan_query(brief_path, q)
                (plans_dir / f"{qid}.json").write_text(_json.dumps(plan, indent=2) + "\n", encoding="utf-8")
                plans_by_qid[qid] = plan
                store.record_run_event(run_id, f"Planned: {qid}", kind="running", source="planner")
            except Exception as exc:
                plans_by_qid.setdefault(qid, {"error": str(exc)})
                store.record_run_event(run_id, f"Plan failed for {qid}: {exc}", kind="warning", source="planner")

        # Enable tracer for this run.
        traces_dir = DATA_DIR / "traces"
        _tracer.enable(brand=brand, run_dir=traces_dir)

        # Bridge: every tracer event also becomes a row in run_events so the
        # terminal-style live view streams real pipeline progress (fetch
        # queries firing, items returned per source, per-item LLM scores)
        # instead of stalling at the worker's coarse "started pipeline" line.
        # Filter heavy/noisy events; reformat known stages into prose.
        _scoring_state = {"done": 0, "total": 0}

        def _bridge(entry):
            stage = entry.get("stage", "")
            payload = entry.get("payload") or {}
            kind = "running"
            msg = None
            source = ""
            count = 0
            if stage == "pipeline_started":
                msg = "Pipeline: started"
            elif stage == "brief_loaded":
                msg = f"Pipeline: brief loaded ({payload.get('pillar_count', 0)} pillars, {payload.get('query_count', 0)} queries)"
            elif stage == "last30days_started":
                msg = f"Fetch: last30days starting ({payload.get('query_count', 0)} queries)"
                source = "last30days"
            elif stage == "query_fired":
                qid = payload.get("query_id") or payload.get("topic") or "?"
                msg = f"Fetch: query → {qid}"
                source = payload.get("backend") or "last30days"
            elif stage == "items_returned":
                qid = payload.get("query_id") or "?"
                # Payload key is `item_count` from last30days, `count` from
                # gdelt — accept either so the live view shows the truth.
                cnt = int(payload.get("item_count") or payload.get("count") or 0)
                count = cnt
                msg = f"Fetch: {qid} → {cnt} items"
                source = payload.get("backend") or "last30days"
            elif stage == "query_error":
                qid = payload.get("query_id") or "?"
                msg = f"Fetch: {qid} failed — {payload.get('error', '')}"
                kind = "warning"
            elif stage == "last30days_complete":
                msg = f"Fetch: last30days complete ({payload.get('item_count', 0)} items)"
                count = int(payload.get("item_count") or 0)
            elif stage == "gdelt_started":
                msg = f"Fetch: GDELT starting ({payload.get('pillar_count', 0)} pillars)"
                source = "gdelt"
            elif stage == "gdelt_pillar_items_returned":
                p = payload.get("pillar", "?")
                cnt = int(payload.get("item_count") or payload.get("count") or 0)
                count = cnt
                msg = f"Fetch: GDELT {p} → {cnt} items"
                source = "gdelt"
            elif stage == "gdelt_pillar_error":
                msg = f"Fetch: GDELT {payload.get('pillar', '?')} failed — {payload.get('error', '')}"
                kind = "warning"
                source = "gdelt"
            elif stage == "gdelt_unavailable":
                msg = f"Fetch: GDELT unavailable — {payload.get('reason', '')}"
                kind = "warning"
                source = "gdelt"
            elif stage == "gdelt_complete":
                msg = f"Fetch: GDELT complete ({payload.get('item_count', 0)} items)"
                count = int(payload.get("item_count") or 0)
            elif stage == "inputs_assembled":
                msg = f"Pipeline: assembled raw inputs"
            elif stage == "slim_cap_applied":
                msg = f"Slim: capped {payload.get('pre_count', 0)} → {payload.get('post_count', 0)} items before scoring"
                count = int(payload.get("post_count") or 0)
            elif stage == "dedup_decision":
                msg = f"Pipeline: deduped {payload.get('input_count', 0)} → {payload.get('output_count', 0)} (dropped {payload.get('dropped_count', 0)})"
                count = int(payload.get("output_count") or 0)
            elif stage == "llm_scoring_started":
                _scoring_state["total"] = int(payload.get("item_count") or 0)
                _scoring_state["done"] = 0
                msg = f"Scoring: starting ({_scoring_state['total']} items × {payload.get('model', 'claude')})"
                count = _scoring_state["total"]
            elif stage == "llm_score":
                _scoring_state["done"] += 1
                # llm_score payload is nested: item.{title,...} + parsed.{score, fit, pillar_fit}.
                item_block = payload.get("item") or {}
                parsed = payload.get("parsed") or {}
                title = (item_block.get("title") or "")[:80]
                score_val = parsed.get("score")
                score_str = f"{int(score_val):3d}" if isinstance(score_val, (int, float)) else str(score_val or "?")
                fit = parsed.get("fit") or ""
                msg = f"Scoring: {_scoring_state['done']}/{_scoring_state['total']} · {score_str} · {fit:13s} · {title}"
                count = _scoring_state["done"]
            elif stage == "llm_scoring_complete":
                msg = f"Scoring: complete ({payload.get('scored_count', _scoring_state['done'])} items)"
                count = int(payload.get("scored_count") or _scoring_state["done"])
                kind = "complete"
            elif stage == "keyword_scoring_complete":
                msg = f"Scoring: keyword-only path complete ({payload.get('scored_count', 0)} items)"
                count = int(payload.get("scored_count") or 0)
            elif stage == "digest_built":
                msg = f"Digest: built ({payload.get('top_count', 0)} items)"
                count = int(payload.get("top_count") or 0)
                kind = "complete"
            if msg is None:
                return
            store.record_run_event(run_id, msg, kind=kind, source=source, item_count=count)

        _tracer.add_listener(_bridge)

        # Fire the full pipeline (both backends combined). Brand isolation
        # writes to data/<brand>/*.json and config/brands/<brand>/plans/*.json.
        # Slim-cap: per-run option overrides the env var. UI choice wins so a
        # user un-checking Slim while SIGNAL_ROOM_SLIM_RUN=1 is the global
        # default still gets a full-budget scoring pass.
        if options_slim is None:
            slim_run = os.environ.get("SIGNAL_ROOM_SLIM_RUN", "").lower() in {"1", "true", "yes"}
        else:
            slim_run = bool(options_slim)
        slim_cap = 0
        if slim_run:
            pillars = ((brief.get("projection") or {}).get("signal_room") or {}).get("pillars") or []
            slim_cap = max(10, 10 * len(pillars))
            store.record_run_event(
                run_id,
                f"Slim-run mode: capping LLM-scored items to {slim_cap} (10 × {len(pillars)} pillars)",
                kind="running",
            )

        # Translate channel selection into the fetch_backend the pipeline expects.
        if reuse_cache:
            fetch_backend = "cache"
        elif set(channels) == {"last30days"}:
            fetch_backend = "last30days"
        elif set(channels) == {"gdelt"}:
            fetch_backend = "gdelt"
        else:
            fetch_backend = "both"

        if reuse_cache:
            store.record_run_event(
                run_id,
                f"Reusing cached discovered_items.json (channels={channels})",
                kind="running",
            )
        else:
            store.record_run_event(
                run_id,
                f"Starting fetch pipeline (channels={channels})",
                kind="running",
            )
        summary = _run_pipeline(
            brief_path=brief_path,
            brand_config_dir=brand_dir,
            data_suffix=brand,
            fetch_backend=fetch_backend,
            fetch_mock=mock,
            fetch_parallelism=4,
            include_fixtures=False,
            slim_cap=slim_cap,
            channel_filter=channels if reuse_cache else None,
        )

        store.record_run_event(
            run_id,
            f"Pipeline complete: {summary.get('raw_items', 0)} raw, {summary.get('scored_items', 0)} scored, {summary.get('top_items', 0)} in digest",
            kind="running",
            item_count=int(summary.get("raw_items", 0)),
        )
        # Flush tracer to disk and read it back for DB storage. Clear the
        # bridge listener first so the run_finished trace event doesn't also
        # become a (redundant) "Run finished" row in run_events.
        _tracer.clear_listeners()
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

        store.record_run_event(run_id, "Run finished", kind="complete",
                               item_count=int(summary.get("top_items", 0)))
        # Retention: keep only N most-recent runs per brand.
        keep = int(os.environ.get("SIGNAL_ROOM_BRAND_RUN_KEEP", "10"))
        store.prune_brand_runs(brand, keep=keep)
    except Exception as exc:
        _tracer.clear_listeners()
        store.record_run_event(run_id, f"Run failed: {exc}", kind="error")
        store.mark_brand_run_failed(run_id, str(exc))
        raise


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
