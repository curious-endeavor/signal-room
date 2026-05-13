import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .fetchers.gdelt import GdeltError, fetch_gdelt
from .fetchers.last30days import DISCOVERED_ITEMS_PATH, Last30DaysError, fetch_last30days
from .models import FEEDBACK_ACTIONS, FeedbackEvent
from .pipeline import FEEDBACK_PATH, RAW_PATH, SOURCE_WEIGHTS_PATH, load_enriched_items, run_pipeline
from .query_lab import load_query_lab_summary, render_query_lab_text, run_query_lab
from .storage import CONFIG_DIR, append_jsonl, read_json, read_jsonl, write_json


DISCOVERY_QUERIES_PATH = CONFIG_DIR / "discovery_queries.json"


def main(argv: Sequence[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="signal-room", description="Curious Endeavor Signal Room MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the local Signal Room pipeline")
    run_parser.add_argument("--limit", type=int, default=10, help="Number of digest items to render")
    run_parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Optional fixture JSON path. Defaults to fixtures/sample_items.json",
    )
    run_parser.add_argument("--fetch", choices=["last30days", "gdelt", "both"], default="", help="Optional live discovery backend")
    run_parser.add_argument("--fetch-mock", action="store_true", help="Run the live discovery backend in mock mode")
    run_parser.add_argument("--fetch-query-limit", type=int, default=0, help="Limit how many discovery queries to run (last30days)")
    run_parser.add_argument("--fetch-lookback-days", type=int, default=0, help="Override last30days lookback window in days")
    run_parser.add_argument("--fetch-parallelism", type=int, default=4, help="How many discovery queries to fire in parallel (default 4, last30days only)")
    run_parser.add_argument("--fetch-sources", default="", help="Comma-separated last30days search sources (default: all from config)")
    run_parser.add_argument("--fetch-pillars", default="all", help="GDELT pillars (CSV or 'all'). Ignored for last30days.")
    run_parser.add_argument("--fetch-timespan", default="", help="GDELT timespan (e.g. 1d, 7d). Ignored for last30days.")
    run_parser.add_argument("--fetch-max", type=int, default=0, help="GDELT max records per pillar. Ignored for last30days.")
    run_parser.add_argument(
        "--fixtures-only",
        action="store_true",
        help="Ignore discovered items and build the digest from fixtures only",
    )
    run_parser.add_argument("--emit", choices=["text", "json"], default="text")
    run_parser.add_argument("--brief", type=Path, default=None, help="Path to brief.yaml — switches to LLM-based scoring")
    run_parser.add_argument("--llm-model", default="claude-sonnet-4-6", help="Model for LLM scoring")
    run_parser.add_argument("--trace", action="store_true", help="Record a per-stage trace and emit HTML to data/traces/")
    run_parser.add_argument("--trace-brand", default="", help="Override brand name for trace filename (default: inferred from --brief)")
    run_parser.add_argument("--no-open", action="store_true", help="Skip auto-opening the trace HTML")
    run_parser.add_argument("--brand-config-dir", type=Path, default=None, help="Per-brand config dir (default: inferred from --brief, e.g. config/brands/<slug>/)")
    run_parser.add_argument("--data-suffix", default="", help="Per-brand data subdir name (default: inferred from --brief; '' = shared data/)")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch discovery items from a backend")
    fetch_parser.add_argument("--backend", choices=["last30days", "gdelt", "both"], required=True)
    fetch_parser.add_argument("--mock", action="store_true", help="Run the backend in mock mode")
    fetch_parser.add_argument("--query-limit", type=int, default=0, help="Limit how many discovery queries to run (last30days)")
    fetch_parser.add_argument("--lookback-days", type=int, default=0, help="Override last30days lookback window in days")
    fetch_parser.add_argument("--pillars", default="all", help="GDELT pillars (CSV or 'all'). Ignored for last30days.")
    fetch_parser.add_argument("--timespan", default="", help="GDELT timespan (e.g. 1d, 7d). Ignored for last30days.")
    fetch_parser.add_argument("--max", type=int, default=0, help="GDELT max records per pillar. Ignored for last30days.")
    fetch_parser.add_argument("--emit", choices=["text", "json"], default="text")

    feedback_parser = subparsers.add_parser("feedback", help="Record feedback for a surfaced item")
    feedback_parser.add_argument("--item-id", required=True)
    feedback_parser.add_argument("--action", required=True, choices=sorted(FEEDBACK_ACTIONS))
    feedback_parser.add_argument("--note", default="")
    feedback_parser.add_argument("--emit", choices=["text", "json"], default="text")

    queries_parser = subparsers.add_parser("queries", help="List configured discovery queries")
    queries_parser.add_argument("--emit", choices=["text", "json"], default="text")

    items_parser = subparsers.add_parser("items", help="List enriched items from the latest run")
    items_parser.add_argument("--limit", type=int, default=10)
    items_parser.add_argument("--emit", choices=["text", "json"], default="text")

    item_parser = subparsers.add_parser("item", help="Inspect one item from the latest run")
    item_parser.add_argument("--item-id", required=True)
    item_parser.add_argument("--emit", choices=["text", "json"], default="text")

    feedback_log_parser = subparsers.add_parser("feedback-log", help="Show recorded feedback events")
    feedback_log_parser.add_argument("--item-id", default="")
    feedback_log_parser.add_argument("--limit", type=int, default=20)
    feedback_log_parser.add_argument("--emit", choices=["text", "json"], default="text")

    lab_parser = subparsers.add_parser("lab", help="Run and review discovery query experiments")
    lab_subparsers = lab_parser.add_subparsers(dest="lab_command", required=True)

    lab_run_parser = lab_subparsers.add_parser("run", help="Run a batch of ad hoc discovery queries")
    lab_run_parser.add_argument("--query", action="append", default=[], help="Query text to run. Repeat for multiple queries.")
    lab_run_parser.add_argument("--sources", default="", help="Comma-separated search sources, e.g. grounding,reddit")
    lab_run_parser.add_argument("--parallelism", type=int, default=4, help="How many queries to run in parallel")
    lab_run_parser.add_argument("--top", type=int, default=5, help="How many top items to retain per query")
    lab_run_parser.add_argument("--lookback-days", type=int, default=30, help="last30days lookback window in days")
    lab_run_parser.add_argument("--mock", action="store_true", help="Run last30days in mock mode")
    lab_run_parser.add_argument("--emit", choices=["text", "json"], default="text")

    lab_show_parser = lab_subparsers.add_parser("show", help="Show a saved query-lab batch summary")
    lab_show_parser.add_argument("--batch-id", default="latest")
    lab_show_parser.add_argument("--query-id", default="")
    lab_show_parser.add_argument("--top", type=int, default=3)
    lab_show_parser.add_argument("--emit", choices=["text", "json"], default="text")

    plan_parser = subparsers.add_parser("plan", help="Generate Signal-Room QueryPlans for every query in a brief (skips vendor's grok planner)")
    plan_parser.add_argument("--brief", required=True, type=Path, help="Path to brief.yaml")
    plan_parser.add_argument("--out", type=Path, default=Path("config/plans"), help="Output dir for plan JSON files (default: config/plans/)")
    plan_parser.add_argument("--model", default="claude-sonnet-4-6", help="Planner model")
    plan_parser.add_argument("--only", default="", help="Comma-separated query ids to plan; empty = all")
    plan_parser.add_argument("--emit", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)
    if args.command == "run":
        fetch_sources_list = [s.strip() for s in args.fetch_sources.split(",") if s.strip()] or None
        inferred_slug = _infer_brand_from_brief(args.brief) if getattr(args, "brief", None) else ""
        brand_config_dir = args.brand_config_dir
        if brand_config_dir is None and inferred_slug:
            cand = Path("config") / "brands" / inferred_slug
            if cand.exists():
                brand_config_dir = cand
        data_suffix = args.data_suffix or inferred_slug or ""
        run_kwargs = {
            "limit": args.limit,
            "discovered_path": DISCOVERED_ITEMS_PATH,
            "include_fixtures": not args.fixtures_only,
            "fetch_backend": args.fetch,
            "fetch_mock": args.fetch_mock,
            "fetch_query_limit": args.fetch_query_limit,
            "fetch_lookback_days": args.fetch_lookback_days,
            "fetch_parallelism": args.fetch_parallelism,
            "fetch_sources": fetch_sources_list,
            "brand_config_dir": brand_config_dir,
            "data_suffix": data_suffix,
            "fetch_pillars": _parse_pillars(args.fetch_pillars),
            "fetch_timespan": args.fetch_timespan or None,
            "fetch_max": args.fetch_max or None,
        }
        if args.fixture:
            run_kwargs["fixture_path"] = args.fixture
        if getattr(args, "brief", None):
            run_kwargs["brief_path"] = args.brief
            run_kwargs["llm_model"] = args.llm_model

        if getattr(args, "trace", False):
            from .tracer import tracer
            from .storage import DATA_DIR
            brand = args.trace_brand or _infer_brand_from_brief(args.brief) or "unknown"
            tracer.enable(brand=brand, run_dir=DATA_DIR / "traces")

        summary = run_pipeline(**run_kwargs)

        if getattr(args, "trace", False):
            from .tracer import tracer
            jsonl_path = tracer.flush()
            html_path = tracer.flush_html(jsonl_path=jsonl_path)
            summary["trace_jsonl_path"] = str(jsonl_path) if jsonl_path else None
            summary["trace_html_path"] = str(html_path) if html_path else None
            if html_path and not args.no_open:
                import subprocess, sys as _sys
                opener = "open" if _sys.platform == "darwin" else "xdg-open"
                try:
                    subprocess.Popen([opener, str(html_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except FileNotFoundError:
                    pass

        _emit(summary, args.emit, "Signal Room digest generated")
        return 0

    if args.command == "fetch":
        try:
            summary = _dispatch_fetch(
                backend=args.backend,
                mock=args.mock,
                query_limit=args.query_limit or None,
                lookback_days=args.lookback_days or None,
                pillars=_parse_pillars(args.pillars),
                timespan=args.timespan or None,
                max_records=args.max or None,
            )
        except (Last30DaysError, GdeltError) as exc:
            if args.emit == "json":
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
            else:
                print(f"Fetch failed: {exc}")
            return 1
        _emit(summary, args.emit, "Signal Room discovery fetched")
        return 0

    if args.command == "feedback":
        item_source = _source_for_item(args.item_id)
        event = FeedbackEvent(
            item_id=args.item_id,
            action=args.action,
            note=args.note,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        append_jsonl(FEEDBACK_PATH, [event.to_dict()])
        if item_source:
            _update_source_weight(item_source, args.action)
        payload = {
            "ok": True,
            "item_id": args.item_id,
            "action": args.action,
            "note": args.note,
            "source": item_source,
            "feedback_path": str(FEEDBACK_PATH),
        }
        _emit(payload, args.emit, f"Recorded feedback: {args.action} for {args.item_id}")
        if args.emit == "text" and item_source:
            print(f"Updated local source weighting for: {item_source}")
            print("Run `signal-room run` or `python3 -m signal_room run` again to apply feedback to scoring.")
        return 0

    if args.command == "queries":
        queries = _load_queries()
        payload = {
            "query_count": len(queries),
            "queries": queries,
        }
        _emit(payload, args.emit, "Configured discovery queries")
        return 0

    if args.command == "items":
        items = [item.to_dict() for item in load_enriched_items()[: args.limit]]
        payload = {
            "item_count": len(items),
            "items": items,
        }
        _emit(payload, args.emit, "Latest enriched items")
        return 0

    if args.command == "item":
        item = _find_item(args.item_id)
        if not item:
            return _not_found(args.emit, f"Item not found: {args.item_id}")
        _emit(item, args.emit, f"Item: {args.item_id}")
        return 0

    if args.command == "feedback-log":
        rows = read_jsonl(FEEDBACK_PATH)
        if args.item_id:
            rows = [row for row in rows if row.get("item_id") == args.item_id]
        rows = rows[-args.limit :]
        payload = {
            "event_count": len(rows),
            "events": rows,
        }
        _emit(payload, args.emit, "Feedback log")
        return 0

    if args.command == "lab":
        try:
            if args.lab_command == "run":
                if not args.query:
                    return _not_found(args.emit, "Provide at least one --query for the lab run.")
                sources = _parse_sources(args.sources)
                summary = run_query_lab(
                    query_texts=args.query,
                    search_sources=sources,
                    parallelism=args.parallelism,
                    top_n=args.top,
                    mock=args.mock,
                    lookback_days=args.lookback_days,
                )
                if args.emit == "json":
                    print(json.dumps(summary, indent=2, sort_keys=True))
                else:
                    print(render_query_lab_text(summary, top_n=min(args.top, 3)))
                return 0
            if args.lab_command == "show":
                summary = load_query_lab_summary(args.batch_id)
                if args.emit == "json":
                    print(json.dumps(summary, indent=2, sort_keys=True))
                else:
                    print(render_query_lab_text(summary, top_n=args.top, query_id=args.query_id))
                return 0
        except Last30DaysError as exc:
            if args.emit == "json":
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
            else:
                print(f"Query lab failed: {exc}")
            return 1

    if args.command == "plan":
        import yaml
        from .planner import plan_query

        brief = yaml.safe_load(Path(args.brief).read_text(encoding="utf-8")) or {}
        queries = (((brief.get("projection") or {}).get("signal_room") or {}).get("discovery_queries") or [])
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        if only:
            queries = [q for q in queries if isinstance(q, dict) and q.get("id") in only]
        if not queries:
            print(f"No discovery_queries found in {args.brief} (after --only filter)")
            return 1
        args.out.mkdir(parents=True, exist_ok=True)
        results = []
        for q in queries:
            qid = q.get("id", "")
            if not qid:
                continue
            try:
                plan = plan_query(args.brief, q, model=args.model)
                out_path = args.out / f"{qid}.json"
                out_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                results.append({"id": qid, "topic": q.get("topic", ""), "out": str(out_path), "subqueries": len(plan.get("subqueries", [])), "ok": True})
                print(f"  ✓ {qid}  →  {out_path}  ({len(plan.get('subqueries', []))} subqueries)")
            except Exception as exc:
                results.append({"id": qid, "topic": q.get("topic", ""), "ok": False, "error": str(exc)})
                print(f"  ✗ {qid}: {exc}")
        payload = {"plans_dir": str(args.out), "results": results, "ok": all(r["ok"] for r in results)}
        if args.emit == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload["ok"] else 1

    parser.error("Unknown command")
    return 2


def _infer_brand_from_brief(brief_path) -> str:
    """Pull brand slug from a brief path like config/brands/<slug>/brief.yaml."""
    if not brief_path:
        return ""
    try:
        parts = Path(brief_path).resolve().parts
        if "brands" in parts:
            i = parts.index("brands")
            if i + 1 < len(parts):
                return parts[i + 1]
        return Path(brief_path).stem
    except Exception:
        return ""


def _emit(payload: dict, emit: str, heading: str) -> None:
    if emit == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(heading)
    for key, value in payload.items():
        print(f"{key}: {value}")


def _load_queries() -> list:
    payload = read_json(DISCOVERY_QUERIES_PATH, {"queries": []})
    queries = list(payload.get("queries", []))
    queries.sort(key=lambda item: (int(item.get("priority", 999)), item.get("id", "")))
    return queries


def _parse_pillars(raw: str):
    """Translate CLI pillars input. 'all' or empty → None (fetch every pillar)."""
    if not raw or raw.strip().lower() == "all":
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def _dispatch_fetch(backend, mock, query_limit, lookback_days, pillars, timespan, max_records):
    """Run one or both backends and persist a merged payload via discovery_store.

    All single-backend writes go through `write_merged_discovered_items` so
    `first_seen_at` survives re-fetches and `meta.source` stays consistent.
    """
    from . import discovery_store

    payloads = []
    if backend in {"last30days", "both"}:
        payloads.append(fetch_last30days(
            mock=mock,
            query_limit=query_limit,
            lookback_days=lookback_days,
            output_path=None,
            parallelism=4,
        ))
    if backend in {"gdelt", "both"}:
        payloads.append(fetch_gdelt(
            pillars=pillars,
            timespan=timespan,
            max_records=max_records,
            mock=mock,
            output_path=None,
        ))
    if not payloads:
        raise ValueError(f"Unknown fetch backend: {backend}")
    return discovery_store.write_merged_discovered_items(
        DISCOVERED_ITEMS_PATH,
        payloads,
    )


def _parse_sources(raw_sources: str) -> list:
    if not raw_sources.strip():
        return []
    return [source.strip() for source in raw_sources.split(",") if source.strip()]


def _find_item(item_id: str) -> dict:
    for item in load_enriched_items():
        if item.id == item_id:
            return item.to_dict()
    raw_payload = read_json(RAW_PATH, [])
    for item in raw_payload:
        if item.get("id") == item_id:
            return item
    return {}


def _not_found(emit: str, message: str) -> int:
    if emit == "json":
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    else:
        print(message)
    return 1


def _source_for_item(item_id: str) -> str:
    for item in load_enriched_items():
        if item.id == item_id:
            return item.source
    return ""


def _update_source_weight(source: str, action: str) -> None:
    deltas = {
        "useful": 0.5,
        "turned_into_content": 1.0,
        "source_worth_following": 1.5,
        "not_useful": -0.5,
        "too_generic": -1.0,
        "wrong_pillar": -0.25,
    }
    weights = read_json(SOURCE_WEIGHTS_PATH, {})
    weights[source] = round(float(weights.get(source, 0.0)) + deltas.get(action, 0.0), 2)
    write_json(SOURCE_WEIGHTS_PATH, weights)


if __name__ == "__main__":
    raise SystemExit(main())
