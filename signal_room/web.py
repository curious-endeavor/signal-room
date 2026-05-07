from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import re
from html import escape
from typing import Any
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import PILLARS
from .storage import DATA_DIR, ROOT, read_json
from .web_store import SignalRoomStore
from .worker import DEFAULT_SOURCES, process_run


TEMPLATE_DIR = ROOT / "signal_room" / "templates"
STATIC_DIR = ROOT / "signal_room" / "static"

app = FastAPI(title="Curious Endeavor Signal Room")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)
store = SignalRoomStore()


@app.on_event("startup")
def startup() -> None:
    store.initialize()


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/")
def index(request: Request, q: str = "", lookback_days: int = 30) -> Any:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "query": q,
            "lookback_days": lookback_days,
            "lookback_options": _lookback_options(),
            "sources": _source_options(),
            "selected_sources": DEFAULT_SOURCES,
            "recent_runs": store.list_runs(),
            "suggestions": _query_suggestions(),
            "run": {},
            "items": [],
            "source_counts": [],
            "date_groups": [],
            "worker_events": [],
        },
    )


@app.post("/search")
def search(
    request: Request,
    background_tasks: BackgroundTasks,
    query: str = Form(...),
    sources: list[str] = Form(default=[]),
    lookback_days: int = Form(30),
) -> RedirectResponse:
    clean_query = query.strip()
    selected_sources = sources or DEFAULT_SOURCES
    if not clean_query:
        return RedirectResponse("/", status_code=303)
    clean_lookback_days = _clean_lookback_days(lookback_days)
    active_run = store.find_active_run(clean_query, selected_sources, clean_lookback_days)
    run_id = active_run.get("id") or store.create_run(clean_query, selected_sources, lookback_days=clean_lookback_days)
    if _inline_jobs_enabled() and not active_run:
        run = store.get_run(run_id)
        background_tasks.add_task(process_run, store, run, _mock_fetch_enabled())
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}")
def results(request: Request, run_id: str) -> Any:
    run = store.get_run(run_id)
    if not run:
        return RedirectResponse("/sample", status_code=303)
    items = store.get_run_items(run_id)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "run": run,
            **_result_context(items),
            "worker_events": store.list_run_events(run_id),
            "suggestions": _query_suggestions(items),
            "lookback_options": _lookback_options(),
        },
    )


@app.get("/sample")
def sample_results(request: Request) -> Any:
    items = _demo_items()
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "run": {
                "id": "sample",
                "query": "ai-native marketing agency workflow",
                "status": "ready",
                "lookback_days": 30,
                "sources": DEFAULT_SOURCES,
                "item_count": len(items),
                "error": "",
            },
            **_result_context(items),
            "worker_events": [],
            "suggestions": _query_suggestions(items),
            "lookback_options": _lookback_options(),
        },
    )


@app.post("/feedback")
def feedback(run_id: str = Form(...), item_id: str = Form(...)) -> RedirectResponse:
    store.record_feedback(run_id, item_id, "thumbs_down")
    target = "/sample" if run_id == "sample" else f"/runs/{run_id}"
    return RedirectResponse(target, status_code=303)


@app.post("/create-content")
def create_content(run_id: str = Form(...), item_id: str = Form(...)) -> RedirectResponse:
    store.record_feedback(run_id, item_id, "create_content")
    target = "/sample" if run_id == "sample" else f"/runs/{run_id}"
    return RedirectResponse(target, status_code=303)


@app.post("/api/search")
def api_search(
    background_tasks: BackgroundTasks,
    query: str = Form(...),
    sources: list[str] = Form(default=[]),
    lookback_days: int = Form(30),
) -> JSONResponse:
    clean_query = query.strip()
    if not clean_query:
        return JSONResponse({"ok": False, "error": "Enter a query."}, status_code=400)
    selected_sources = sources or DEFAULT_SOURCES
    clean_lookback_days = _clean_lookback_days(lookback_days)
    active_run = store.find_active_run(clean_query, selected_sources, clean_lookback_days)
    run_id = active_run.get("id") or store.create_run(clean_query, selected_sources, lookback_days=clean_lookback_days)
    if _inline_jobs_enabled() and not active_run:
        background_tasks.add_task(process_run, store, store.get_run(run_id), _mock_fetch_enabled())
    return JSONResponse(_run_payload(run_id))


@app.get("/api/runs/{run_id}")
def api_run(run_id: str) -> JSONResponse:
    return JSONResponse(_run_payload(run_id))


@app.post("/api/feedback")
def api_feedback(run_id: str = Form(...), item_id: str = Form(...)) -> JSONResponse:
    store.record_feedback(run_id, item_id, "thumbs_down")
    return JSONResponse({"ok": True})


@app.post("/api/create-content")
def api_create_content(run_id: str = Form(...), item_id: str = Form(...)) -> JSONResponse:
    store.record_feedback(run_id, item_id, "create_content")
    return JSONResponse({"ok": True})


def _demo_items(limit: int = 20) -> list[dict[str, Any]]:
    payload = read_json(DATA_DIR / "enriched_items.json", [])
    items = payload[:limit] if isinstance(payload, list) else []
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
        item["pillar"] = _primary_pillar(item)
    return items


def _run_payload(run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if not run:
        return {"ok": False, "error": "Run not found."}
    context = _result_context(store.get_run_items(run_id))
    return {
        "ok": True,
        "run": run,
        "items": context["items"],
        "source_counts": context["source_counts"],
        "date_groups": context["date_groups"],
        "worker_events": store.list_run_events(run_id),
        "suggestions": _query_suggestions(context["items"]),
    }


def _query_suggestions(items: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    follow_ups = []
    for item in items or []:
        follow_up = str(item.get("follow_up_search_query", "")).strip()
        if follow_up and follow_up not in follow_ups:
            follow_ups.append(follow_up)
    defaults = [
        (
            "AI-native marketing agency workflow",
            "Find agencies showing repeatable delivery mechanisms, not generic AI positioning.",
        ),
        (
            "human review workflow AI marketing content",
            "Find examples of quality judgment, approval, and failure modes.",
        ),
        (
            "AI CMO SMB restaurant marketing workflow",
            "Track productized marketing work for operators with constrained teams.",
        ),
        (
            "brand system AI case study agency workflow",
            "Find AI-native brand-building examples with process detail.",
        ),
    ]
    suggestions = [
        {"query": query, "why": "Follow-up from a surfaced result."}
        for query in follow_ups[:3]
    ]
    suggestions.extend({"query": query, "why": why} for query, why in defaults)
    return suggestions[:5]


def _source_options() -> list[dict[str, str]]:
    labels = {
        "grounding": "Web",
        "x": "X",
        "youtube": "YouTube",
        "instagram": "Instagram",
        "github": "GitHub",
        "reddit": "Reddit",
        "hackernews": "HN",
    }
    return [{"id": source, "label": labels[source]} for source in DEFAULT_SOURCES]


def _primary_pillar(item: dict[str, Any]) -> str:
    pillars = item.get("pillar_fit") or []
    if isinstance(pillars, list) and pillars:
        code = str(pillars[0])
        return f"{code} · {PILLARS.get(code, 'Signal')}"
    return "Unsorted"


def _decorate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decorated = []
    for rank, item in enumerate(items, start=1):
        row = dict(item)
        row.setdefault("rank", rank)
        row["pillar"] = row.get("pillar") or _primary_pillar(row)
        row["display_source"] = _display_source(row)
        row["display_date"] = _relative_date(str(row.get("date", "")))
        row["date_group"] = _date_group(str(row.get("date", "")))
        row["summary_text"] = _display_summary(row)
        row["summary_html"] = _emphasize_summary(row["summary_text"])
        decorated.append(row)
    return decorated


def _result_context(items: list[dict[str, Any]]) -> dict[str, Any]:
    decorated = _decorate_items(items)
    return {
        "items": decorated,
        "source_counts": _source_counts(decorated),
        "date_groups": _date_groups(decorated),
    }


def _source_counts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(item.get("display_source") or "Source") for item in items)
    return [{"source": source, "count": count} for source, count in counts.most_common()]


def _date_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for item in items:
        groups.setdefault(str(item.get("date_group") or "Earlier"), []).append(item)
    ordered_labels = ["Today", "Yesterday", "This week", "Last week", "Earlier this month", "Older", "Unknown date"]
    ordered_groups = [{"label": label, "rows": groups[label]} for label in ordered_labels if label in groups]
    ordered_groups.extend(
        {"label": label, "rows": rows} for label, rows in groups.items() if label not in ordered_labels
    )
    return ordered_groups


def _display_source(item: dict[str, Any]) -> str:
    for tag in item.get("tags", []) or []:
        if str(tag).startswith("platform:"):
            return _source_label(str(tag).split(":", 1)[1])
    source = str(item.get("source", "")).strip()
    lowered = source.lower()
    if "case stud" in lowered:
        return "Case study"
    if "coverage" in lowered:
        return "Web"
    if source:
        return source
    parsed = urlparse(str(item.get("source_url", "")))
    return parsed.netloc.replace("www.", "") if parsed.netloc else "Source"


def _source_label(source: str) -> str:
    labels = {item["id"]: item["label"] for item in _source_options()}
    return labels.get(source, source.replace("_", " ").title())


def _display_summary(item: dict[str, Any]) -> str:
    summary = str(item.get("summary", "")).strip()
    content = _clean_content(str(item.get("content", "")).strip(), str(item.get("title", "")).strip())
    generic_summaries = {
        "brave web search",
        "reddit public search",
        "mock reddit result",
        "hacker news",
        "hackernews",
    }
    if content:
        return _truncate(content, 320)
    if summary and summary.lower() not in generic_summaries:
        return _truncate(summary, 320)
    return "No summary available yet."


def _clean_content(content: str, title: str = "") -> str:
    if not content:
        return ""
    content = re.split(r"\n\s*\{", content, maxsplit=1)[0]
    text = re.sub(r"<[^>]+>", "", content)
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*\{.*$", "", text).strip()
    if title and text.lower().startswith(title.lower()):
        text = text[len(title) :].strip(" -|")
    if text in {"{}", "[]"}:
        return ""
    # Some sources duplicate the same excerpt. Keep the first complete-ish sentence.
    parts = re.split(r"(?<=[.!?])\s+", text)
    cleaned = []
    seen = set()
    for part in parts:
        normalized = part.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(part.strip())
        if sum(len(row) for row in cleaned) >= 220:
            break
    return " ".join(cleaned).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip()
    return f"{clipped}..."


def _emphasize_summary(summary: str) -> str:
    text = escape(summary)
    phrases = [
        "AI",
        "AI-native",
        "workflow",
        "workflows",
        "human approval",
        "human review",
        "review",
        "quality",
        "marketing",
        "brand",
        "content",
        "CMO-as-software",
        "case study",
        "operating layer",
        "implementation",
        "failure modes",
    ]
    pattern = "|".join(re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True))
    return re.sub(rf"(?<!\w)({pattern})(?!\w)", r"<strong>\1</strong>", text, flags=re.IGNORECASE)


def _relative_date(raw_date: str) -> str:
    parsed = _parse_date(raw_date)
    if not parsed:
        return raw_date or "Unknown date"
    today = date.today()
    delta = (today - parsed).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if 2 <= delta <= 6:
        return f"{delta} days ago"
    if 7 <= delta <= 13:
        return "last week"
    if 14 <= delta <= 30:
        return f"{delta // 7} weeks ago"
    if 31 <= delta <= 59:
        return "last month"
    return parsed.isoformat()


def _date_group(raw_date: str) -> str:
    parsed = _parse_date(raw_date)
    if not parsed:
        return "Unknown date"
    today = date.today()
    delta = (today - parsed).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta <= 6:
        return "This week"
    if delta <= 13:
        return "Last week"
    if delta <= 30:
        return "Earlier this month"
    return "Older"


def _parse_date(raw_date: str) -> date | None:
    if not raw_date:
        return None
    try:
        return datetime.fromisoformat(raw_date[:10]).date()
    except ValueError:
        return None


def _lookback_options() -> list[int]:
    return [1, 7, 14, 30, 60, 90]


def _clean_lookback_days(value: int) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 30
    return min(max(days, 1), 365)


def _inline_jobs_enabled() -> bool:
    return os.environ.get("SIGNAL_ROOM_INLINE_JOBS", "").lower() in {"1", "true", "yes"}


def _mock_fetch_enabled() -> bool:
    return os.environ.get("SIGNAL_ROOM_FETCH_MOCK", "").lower() in {"1", "true", "yes"}
