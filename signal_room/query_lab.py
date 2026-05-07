from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .fetchers.last30days import Last30DaysError, fetch_last30days
from .ingest import load_raw_items
from .scoring import score_items
from .storage import CONFIG_DIR, QUERY_LAB_DIR, read_json, read_jsonl, write_json


BATCHES_DIR = QUERY_LAB_DIR / "batches"
LATEST_BATCH_PATH = QUERY_LAB_DIR / "latest_batch.json"
SEEDS_PATH = CONFIG_DIR / "seed_sources.json"
WEIGHTS_PATH = CONFIG_DIR / "scoring_weights.json"
SOURCE_WEIGHTS_PATH = CONFIG_DIR / "source_feedback_weights.json"
FEEDBACK_PATH = QUERY_LAB_DIR.parents[0] / "data" / "feedback.jsonl"


def run_query_lab(
    query_texts: Sequence[str],
    search_sources: Sequence[str],
    parallelism: int = 4,
    top_n: int = 5,
    mock: bool = False,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    if not query_texts:
        raise Last30DaysError("Query lab requires at least one query.")

    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_dir = BATCHES_DIR / batch_id
    run_root = batch_dir / "runs"
    output_path = batch_dir / "discovered_items.json"
    queries = _build_queries(query_texts, search_sources)
    fetch_summary = fetch_last30days(
        queries=queries,
        mock=mock,
        parallelism=parallelism,
        continue_on_error=True,
        run_root=run_root,
        output_path=output_path,
        lookback_days=lookback_days,
    )

    seed_payload = read_json(SEEDS_PATH, {"sources": []})
    weights = read_json(WEIGHTS_PATH, {})
    source_weights = read_json(SOURCE_WEIGHTS_PATH, {})
    feedback_events = read_jsonl(FEEDBACK_PATH)
    query_items_map = _group_items_by_query(fetch_summary["items"])
    scored_by_query = {}
    for query in queries:
        query_id = str(query["id"])
        raw_items = load_raw_items(seed_payload, [{"items": query_items_map.get(query_id, [])}])
        scored_by_query[query_id] = score_items(raw_items, weights, feedback_events, source_weights)
    summary = _build_batch_summary(
        batch_id=batch_id,
        mock=mock,
        parallelism=parallelism,
        lookback_days=lookback_days,
        search_sources=list(search_sources),
        queries=queries,
        fetch_summary=fetch_summary,
        scored_by_query=scored_by_query,
        top_n=top_n,
    )

    batch_dir.mkdir(parents=True, exist_ok=True)
    write_json(batch_dir / "summary.json", summary)
    (batch_dir / "summary.md").write_text(_render_markdown(summary), encoding="utf-8")
    write_json(LATEST_BATCH_PATH, {"batch_id": batch_id, "summary_path": str(batch_dir / "summary.json")})
    return summary


def load_query_lab_summary(batch_id: str = "latest") -> Dict[str, Any]:
    if batch_id == "latest":
        latest = read_json(LATEST_BATCH_PATH, {})
        batch_id = str(latest.get("batch_id", "")).strip()
        if not batch_id:
            raise Last30DaysError("No query lab batches found yet.")
    summary_path = BATCHES_DIR / batch_id / "summary.json"
    payload = read_json(summary_path, {})
    if not payload:
        raise Last30DaysError(f"Missing query lab batch summary: {summary_path}")
    enriched = _hydrate_summary(payload)
    if enriched != payload:
        write_json(summary_path, enriched)
        markdown_path = BATCHES_DIR / batch_id / "summary.md"
        markdown_path.write_text(_render_markdown(enriched), encoding="utf-8")
    return enriched


def render_query_lab_text(summary: Dict[str, Any], top_n: int = 3, query_id: str = "") -> str:
    lines = [
        f"Query Lab Batch {summary['batch_id']}",
        f"queries: {summary['query_count']}",
        f"items: {summary['item_count']}",
        f"errors: {summary['error_count']}",
        f"lookback_days: {summary.get('lookback_days', 30)}",
        f"sources: {', '.join(summary.get('search_sources', [])) or 'default'}",
        f"summary_path: {summary['summary_path']}",
    ]
    queries = list(summary.get("queries", []))
    if query_id:
        queries = [entry for entry in queries if entry["query_id"] == query_id]
    for entry in queries:
        lines.append("")
        lines.append(f"[{entry['query_id']}] {entry['query_text']}")
        lines.append(
            "  "
            + f"items={entry['item_count']} top_score={entry['top_score']} avg_top3={entry['avg_top_3_score']} "
            + f"mechanism_rate={entry['mechanism_rate']} strong_hits={entry['strong_hit_count']}"
        )
        lines.append(f"  requested={', '.join(entry.get('search_sources', [])) or 'n/a'}")
        lines.append(f"  planned={', '.join(entry.get('planned_sources', [])) or 'n/a'}")
        returned_counts = entry.get("returned_source_counts", {})
        rendered_counts = ", ".join(f"{source}:{count}" for source, count in returned_counts.items()) or "n/a"
        lines.append(f"  returned={rendered_counts}")
        if entry.get("errors_by_source"):
            lines.append(f"  source_errors={entry['errors_by_source']}")
        lines.append(f"  top_sources={', '.join(entry['top_sources']) or 'n/a'}")
        for item in entry.get("top_items", [])[:top_n]:
            lines.append(
                "  - "
                + f"{item['score']} | {item['source']} | {item['title']} | "
                + f"pillars={','.join(item['pillar_fit']) or '-'} | mechanism={'yes' if item['mechanism_present'] else 'no'}"
            )
            lines.append(f"    {item['reason_for_score']}")
    if summary.get("errors"):
        lines.append("")
        lines.append("Errors:")
        for error in summary["errors"]:
            lines.append(f"- {error['query_id']}: {error['error']}")
    return "\n".join(lines)


def _build_queries(query_texts: Sequence[str], search_sources: Sequence[str]) -> List[Dict[str, Any]]:
    queries = []
    for index, query_text in enumerate(query_texts, start=1):
        slug = _slug(query_text)[:40] or f"query-{index}"
        queries.append(
            {
                "id": f"lab-{index:02d}-{slug}",
                "topic": query_text,
                "search_text": query_text,
                "why": "Manual query-lab experiment for Curious Endeavor.",
                "priority": index,
                "search_sources": list(search_sources),
            }
        )
    return queries


def _build_batch_summary(
    batch_id: str,
    mock: bool,
    parallelism: int,
    lookback_days: int,
    search_sources: List[str],
    queries: List[Dict[str, Any]],
    fetch_summary: Dict[str, Any],
    scored_by_query: Dict[str, List[Any]],
    top_n: int,
) -> Dict[str, Any]:
    query_summaries = []
    query_meta = {str(query["id"]): query for query in queries}
    for run in fetch_summary.get("runs", []):
        query_id = str(run["query_id"])
        report_payload = read_json(Path(run["report_path"]), {})
        returned_source_counts = {
            str(source): len(rows)
            for source, rows in (report_payload.get("items_by_source") or {}).items()
            if isinstance(rows, list) and rows
        }
        planned_sources = []
        for subquery in (report_payload.get("query_plan") or {}).get("subqueries", []):
            for source in subquery.get("sources", []):
                source_name = str(source)
                if source_name not in planned_sources:
                    planned_sources.append(source_name)
        items = sorted((item.to_dict() for item in scored_by_query.get(query_id, [])), key=lambda row: row["score"], reverse=True)
        mechanism_count = sum(1 for item in items if item.get("mechanism_present"))
        strong_hit_count = sum(1 for item in items if float(item.get("score", 0.0)) >= 60.0)
        top_scores = [float(item["score"]) for item in items[:3]]
        top_sources = []
        seen_sources = set()
        for item in items:
            source = str(item["source"])
            if source in seen_sources:
                continue
            seen_sources.add(source)
            top_sources.append(source)
            if len(top_sources) == 5:
                break
        query_summaries.append(
            {
                "query_id": query_id,
                "query_text": str(query_meta[query_id].get("search_text") or query_meta[query_id]["topic"]),
                "label": str(query_meta[query_id]["topic"]),
                "item_count": len(items),
                "top_score": round(float(items[0]["score"]), 2) if items else 0.0,
                "avg_top_3_score": round(sum(top_scores) / len(top_scores), 2) if top_scores else 0.0,
                "mechanism_rate": round(mechanism_count / len(items), 2) if items else 0.0,
                "strong_hit_count": strong_hit_count,
                "top_sources": top_sources,
                "run_dir": run["run_dir"],
                "report_path": run["report_path"],
                "manifest_path": run["manifest_path"],
                "search_sources": run.get("search_sources", []),
                "planned_sources": planned_sources,
                "returned_source_counts": returned_source_counts,
                "errors_by_source": report_payload.get("errors_by_source", {}),
                "top_items": [
                    {
                        "id": item["id"],
                        "title": item["title"],
                        "source": item["source"],
                        "source_url": item["source_url"],
                        "date": item["date"],
                        "score": round(float(item["score"]), 2),
                        "pillar_fit": item.get("pillar_fit", []),
                        "surf_fit": item.get("surf_fit", []),
                        "mechanism_present": bool(item.get("mechanism_present")),
                        "reason_for_score": item.get("reason_for_score", ""),
                        "why_ce_should_care": item.get("why_ce_should_care", ""),
                        "suggested_ce_angle": item.get("suggested_ce_angle", ""),
                    }
                    for item in items[:top_n]
                ],
            }
        )

    query_summaries.sort(key=lambda entry: (entry["strong_hit_count"], entry["avg_top_3_score"], entry["top_score"]), reverse=True)
    return {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mock": mock,
        "parallelism": parallelism,
        "lookback_days": lookback_days,
        "query_count": len(query_summaries),
        "item_count": fetch_summary["item_count"],
        "error_count": fetch_summary.get("error_count", 0),
        "errors": fetch_summary.get("errors", []),
        "search_sources": search_sources,
        "summary_path": str(BATCHES_DIR / batch_id / "summary.json"),
        "markdown_path": str(BATCHES_DIR / batch_id / "summary.md"),
        "discovered_items_path": str(BATCHES_DIR / batch_id / "discovered_items.json"),
        "queries": query_summaries,
    }


def _hydrate_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    hydrated = dict(summary)
    query_entries = []
    for entry in hydrated.get("queries", []):
        query_entries.append(_hydrate_query_entry(dict(entry)))
    hydrated["queries"] = query_entries
    return hydrated


def _hydrate_query_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    if entry.get("planned_sources") and entry.get("returned_source_counts") is not None:
        return entry
    report_path = entry.get("report_path")
    if not report_path:
        return entry
    report_payload = read_json(Path(report_path), {})
    planned_sources = []
    for subquery in (report_payload.get("query_plan") or {}).get("subqueries", []):
        for source in subquery.get("sources", []):
            source_name = str(source)
            if source_name not in planned_sources:
                planned_sources.append(source_name)
    returned_source_counts = {
        str(source): len(rows)
        for source, rows in (report_payload.get("items_by_source") or {}).items()
        if isinstance(rows, list) and rows
    }
    entry.setdefault("search_sources", [])
    entry["planned_sources"] = planned_sources
    entry["returned_source_counts"] = returned_source_counts
    entry["errors_by_source"] = report_payload.get("errors_by_source", {})
    return entry


def _group_items_by_query(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        for tag in item.get("tags", []):
            if tag.startswith("query:"):
                grouped.setdefault(tag.split(":", 1)[1], []).append(item)
                break
    return grouped


def _render_markdown(summary: Dict[str, Any]) -> str:
    lines = [
        f"# Query Lab Batch {summary['batch_id']}",
        "",
        f"- Created at: {summary['created_at']}",
        f"- Queries: {summary['query_count']}",
        f"- Items: {summary['item_count']}",
        f"- Errors: {summary['error_count']}",
        f"- Lookback days: {summary.get('lookback_days', 30)}",
        f"- Sources: {', '.join(summary.get('search_sources', [])) or 'default'}",
        "",
    ]
    for entry in summary.get("queries", []):
        lines.extend(
            [
                f"## {entry['query_text']}",
                "",
                f"- Query ID: `{entry['query_id']}`",
                f"- Items: {entry['item_count']}",
                f"- Top score: {entry['top_score']}",
                f"- Avg top 3 score: {entry['avg_top_3_score']}",
                f"- Mechanism rate: {entry['mechanism_rate']}",
                f"- Strong hits (score >= 60): {entry['strong_hit_count']}",
                f"- Requested sources: {', '.join(entry.get('search_sources', [])) or 'n/a'}",
                f"- Planned sources: {', '.join(entry.get('planned_sources', [])) or 'n/a'}",
                f"- Returned source counts: {', '.join(f'{source}:{count}' for source, count in entry.get('returned_source_counts', {}).items()) or 'n/a'}",
                f"- Source errors: {entry.get('errors_by_source', {}) or 'none'}",
                f"- Top sources: {', '.join(entry['top_sources']) or 'n/a'}",
                "",
            ]
        )
        for item in entry.get("top_items", []):
            lines.extend(
                [
                    f"- {item['score']} | {item['source']} | [{item['title']}]({item['source_url']})",
                    f"  - Pillars: {', '.join(item['pillar_fit']) or '-'}",
                    f"  - Mechanism: {'yes' if item['mechanism_present'] else 'no'}",
                    f"  - Why CE cares: {item['why_ce_should_care']}",
                    f"  - Reason: {item['reason_for_score']}",
                ]
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _slug(text: str) -> str:
    lowered = text.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "query"
