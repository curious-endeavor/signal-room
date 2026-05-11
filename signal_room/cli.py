import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

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
    run_parser.add_argument("--fetch", choices=["last30days"], default="", help="Optional live discovery backend")
    run_parser.add_argument("--fetch-mock", action="store_true", help="Run the live discovery backend in mock mode")
    run_parser.add_argument("--fetch-query-limit", type=int, default=0, help="Limit how many discovery queries to run")
    run_parser.add_argument("--fetch-lookback-days", type=int, default=0, help="Override last30days lookback window in days")
    run_parser.add_argument(
        "--fixtures-only",
        action="store_true",
        help="Ignore discovered items and build the digest from fixtures only",
    )
    run_parser.add_argument("--emit", choices=["text", "json"], default="text")
    run_parser.add_argument("--brief", type=Path, default=None, help="Path to brief.yaml — switches to LLM-based scoring")
    run_parser.add_argument("--llm-model", default="claude-sonnet-4-6", help="Model for LLM scoring")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch discovery items from a backend")
    fetch_parser.add_argument("--backend", choices=["last30days"], required=True)
    fetch_parser.add_argument("--mock", action="store_true", help="Run the backend in mock mode")
    fetch_parser.add_argument("--query-limit", type=int, default=0, help="Limit how many discovery queries to run")
    fetch_parser.add_argument("--lookback-days", type=int, default=0, help="Override last30days lookback window in days")
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

    args = parser.parse_args(argv)
    if args.command == "run":
        run_kwargs = {
            "limit": args.limit,
            "discovered_path": DISCOVERED_ITEMS_PATH,
            "include_fixtures": not args.fixtures_only,
            "fetch_backend": args.fetch,
            "fetch_mock": args.fetch_mock,
            "fetch_query_limit": args.fetch_query_limit,
            "fetch_lookback_days": args.fetch_lookback_days,
        }
        if args.fixture:
            run_kwargs["fixture_path"] = args.fixture
        if getattr(args, "brief", None):
            run_kwargs["brief_path"] = args.brief
            run_kwargs["llm_model"] = args.llm_model
        summary = run_pipeline(**run_kwargs)
        _emit(summary, args.emit, "Signal Room digest generated")
        return 0

    if args.command == "fetch":
        try:
            if args.backend == "last30days":
                summary = fetch_last30days(
                    mock=args.mock,
                    query_limit=args.query_limit or None,
                    lookback_days=args.lookback_days or None,
                )
            else:
                parser.error("Unknown fetch backend")
                return 2
        except Last30DaysError as exc:
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

    parser.error("Unknown command")
    return 2


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
