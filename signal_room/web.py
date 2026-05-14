from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import re
from html import escape
from typing import Any
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
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
# Markdown renderer for assistant chat bubbles — registered as a Jinja filter
# so the template can do `{{ msg | render_chat_md | safe }}` without each
# route hand-rendering.
from . import onboarding as _onb_for_filter
templates.env.filters["render_chat_md"] = _onb_for_filter.render_assistant_markdown
store = SignalRoomStore()


@app.on_event("startup")
def startup() -> None:
    store.initialize()


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/search-classic")
def index_classic(request: Request, q: str = "", lookback_days: int = 30) -> Any:
    """Legacy single-query search home, preserved for backwards-compat.

    New canonical home (`/`) is the brand-list page; brand-scoped search
    lives at `/{brand}?mode=search`.
    """
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
            "reference_groups": [],
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


# ============================================================================
# Brand-routed surfaces (Alice + CE), each with a latest-run page and refetch.
# ============================================================================

_BRAND_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


def _allowed_brand(brand: str) -> Path:
    """Validate brand exists (DB or filesystem). Returns the brand's config dir.

    A brand is valid when EITHER a DB row exists for it OR a brief.yaml file
    sits at config/brands/<slug>/brief.yaml. DB is the new source of truth;
    filesystem is the legacy/local-dev fallback.
    """
    if not _BRAND_SLUG_RE.match(brand):
        raise HTTPException(status_code=404, detail=f"Unknown brand: {brand}")
    db_row = store.get_brand(brand)
    brand_dir = ROOT / "config" / "brands" / brand
    if db_row:
        brand_dir.mkdir(parents=True, exist_ok=True)
        return brand_dir
    brief = brand_dir / "brief.yaml"
    if not brief.exists():
        raise HTTPException(status_code=404, detail=f"Unknown brand: {brand}")
    return brand_dir


@app.get("/{brand}")
def brand_latest(request: Request, brand: str) -> Any:
    _allowed_brand(brand)
    run = store.latest_brand_run(brand)
    return templates.TemplateResponse(
        request,
        "latest_run.html",
        {
            "brand": brand,
            "run": run,
            "has_run": bool(run),
        },
    )


@app.post("/{brand}/refetch")
async def brand_refetch(brand: str, request: Request, background_tasks: BackgroundTasks) -> RedirectResponse:
    _allowed_brand(brand)
    # Read the options form. All fields optional → defaults to a full both-channels,
    # no-slim, no-cache run, matching the original Refetch button behavior.
    options: dict | None = None
    try:
        form = await request.form()
    except Exception:
        form = {}
    if form:
        channels = []
        if form.get("ch_last30days"): channels.append("last30days")
        if form.get("ch_gdelt"): channels.append("gdelt")
        if not channels:
            channels = ["last30days", "gdelt"]
        options = {
            "reuse_cache": bool(form.get("reuse_cache")),
            "channels": channels,
        }
        if form.get("slim"):
            options["slim"] = True
        elif "slim_unset" in form:
            options["slim"] = False
    run_id = store.create_brand_run(brand, options=options)
    if _inline_jobs_enabled():
        from .worker import process_brand_refetch
        background_tasks.add_task(process_brand_refetch, store, store.get_brand_run(run_id), _mock_fetch_enabled())
    return RedirectResponse(url=f"/{brand}/runs/{run_id}", status_code=303)


@app.get("/{brand}/runs/{run_id}")
def brand_run_detail(request: Request, brand: str, run_id: str) -> Any:
    _allowed_brand(brand)
    run = store.get_brand_run(run_id)
    if not run or run.get("brand") != brand:
        raise HTTPException(status_code=404, detail="Run not found")
    return templates.TemplateResponse(
        request,
        "latest_run.html",
        {
            "brand": brand,
            "run": run,
            "has_run": True,
        },
    )


@app.get("/{brand}/runs/{run_id}/trace", response_class=HTMLResponse)
def brand_run_trace(brand: str, run_id: str) -> HTMLResponse:
    _allowed_brand(brand)
    run = store.get_brand_run(run_id)
    if not run or run.get("brand") != brand:
        raise HTTPException(status_code=404)
    html = run.get("trace_html") or "<html><body><p>Trace not yet available.</p></body></html>"
    return HTMLResponse(content=html)


@app.get("/{brand}/runs/{run_id}/trace.jsonl", response_class=PlainTextResponse)
def brand_run_trace_jsonl(brand: str, run_id: str) -> PlainTextResponse:
    _allowed_brand(brand)
    run = store.get_brand_run(run_id)
    if not run or run.get("brand") != brand:
        raise HTTPException(status_code=404)
    return PlainTextResponse(content=run.get("trace_jsonl") or "")


@app.get("/{brand}/runs/{run_id}/digest", response_class=HTMLResponse)
def brand_run_digest(brand: str, run_id: str) -> HTMLResponse:
    _allowed_brand(brand)
    run = store.get_brand_run(run_id)
    if not run or run.get("brand") != brand:
        raise HTTPException(status_code=404)
    return HTMLResponse(content=run.get("digest_html") or "<html><body><p>No digest.</p></body></html>")


@app.get("/api/brands/{brand}/runs/{run_id}/events")
def api_brand_run_events(brand: str, run_id: str, since: int = 0) -> JSONResponse:
    """Stream-ish: return events with id > `since` for this run, oldest first.
    Used by the terminal-style live view on /{brand}. Callers pass the highest
    event id they've seen; the response includes the new high-water mark plus
    the run's current status so the poller can stop on terminal states.
    """
    _allowed_brand(brand)
    run = store.get_brand_run(run_id)
    if not run or run.get("brand") != brand:
        return JSONResponse({"ok": False, "error": "Run not found"}, status_code=404)
    events = store.list_run_events_since(run_id, since_id=int(since or 0))
    last_id = events[-1]["id"] if events else int(since or 0)
    return JSONResponse({
        "ok": True,
        "run_id": run_id,
        "status": run.get("status", ""),
        "error": run.get("error", ""),
        "since": int(since or 0),
        "last_id": last_id,
        "events": events,
    })


@app.get("/api/brands/{brand}/runs/latest")
def api_brand_latest(brand: str) -> JSONResponse:
    _allowed_brand(brand)
    run = store.latest_brand_run(brand)
    if not run:
        return JSONResponse({"ok": True, "run": None})
    slim = {k: v for k, v in run.items() if k not in {"trace_jsonl", "trace_html", "digest_html"}}
    return JSONResponse({"ok": True, "run": slim})


# ============================================================================
# U7-U8 — Home page, onboarding entry, chat, brief finalization, editor.
# ============================================================================

def _slug_from_url(url: str) -> str:
    """Derive a kebab-case brand slug from a URL. Falls back to a generic name."""
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(url)
    host = (parsed.netloc or "").lower().replace("www.", "")
    base = host.split(".")[0] if host else "brand"
    # Keep only a-z 0-9 hyphens.
    slug = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")
    return slug or "brand"


def _next_available_slug(base: str) -> str:
    """If base is taken, suffix -2, -3, ..."""
    if not store.get_brand(base) and not (ROOT / "config" / "brands" / base / "brief.yaml").exists():
        return base
    for n in range(2, 100):
        cand = f"{base}-{n}"
        if not store.get_brand(cand) and not (ROOT / "config" / "brands" / cand / "brief.yaml").exists():
            return cand
    # Last resort.
    import uuid as _uuid
    return f"{base}-{_uuid.uuid4().hex[:6]}"


@app.get("/")
def home(request: Request) -> Any:
    """Brand list home page with 'Create new brand' CTA."""
    brands = store.list_brands()
    # Also surface filesystem-only brands (legacy) so we don't lose them.
    fs_only: list[dict[str, Any]] = []
    for d in (ROOT / "config" / "brands").iterdir() if (ROOT / "config" / "brands").exists() else []:
        if not d.is_dir():
            continue
        if any(b["slug"] == d.name for b in brands):
            continue
        if (d / "brief.yaml").exists():
            fs_only.append({"slug": d.name, "name": d.name.replace("-", " ").title(), "url": "", "created_at": "", "last_refetched_at": "", "legacy": True})
    return templates.TemplateResponse(
        request,
        "home.html",
        {"brands": brands + fs_only, "has_brands": bool(brands or fs_only)},
    )


@app.get("/onboarding/start")
def onboarding_start_get(request: Request) -> Any:
    return templates.TemplateResponse(request, "onboarding_start.html", {"error": "", "url": ""})


@app.post("/onboarding/start")
def onboarding_start_post(request: Request, background_tasks: BackgroundTasks,
                          url: str = Form(...), name: str = Form("")) -> Any:
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return templates.TemplateResponse(
            request, "onboarding_start.html",
            {"error": "Please enter a full URL starting with http:// or https://", "url": url},
        )
    slug = _next_available_slug(_slug_from_url(url))
    brand_name = (name.strip() or parsed.netloc.replace("www.", "").split(".")[0].title())

    # Generate passcode + hash + session token.
    from . import auth as _auth
    passcode = _auth.generate_passcode()
    passcode_hash = _auth.hash_passcode(passcode)
    session_token = _auth.generate_session_token()

    # Create brand row with placeholder brief.
    store.create_brand(
        slug=slug,
        name=brand_name,
        url=url.strip(),
        brief_json={"name": brand_name, "url": url.strip()},
        passcode_hash=passcode_hash,
        passcode_session_token=session_token,
    )
    # Kick off crawl in background; create the chat session so /onboarding can show messages immediately.
    chat_session_id = store.create_chat_session(slug, purpose="onboarding")

    # Fire crawl + first assistant turn as a background task so the user
    # sees the passcode-reveal page immediately.
    background_tasks.add_task(_onboarding_kickoff, slug, brand_name, url.strip(), chat_session_id)

    # Set the passcode cookie automatically so the user doesn't need to enter
    # it right away on the next page.
    response = RedirectResponse(url=f"/{slug}/passcode-reveal?passcode={passcode}", status_code=303)
    _auth.set_passcode_cookie(response, slug, session_token)
    return response


def _onboarding_kickoff(slug: str, brand_name: str, brand_url: str, chat_session_id: str) -> None:
    """Crawl the brand site + ask Claude for the first interview turn.

    Runs as a BackgroundTask so the user lands on the passcode-reveal page
    immediately while crawl+kickoff happen behind the scenes (~30-45s).
    """
    from . import onboarding as _onb
    try:
        crawl = _onb.crawl_brand(brand_url)
    except Exception as exc:
        crawl = {"context": f"[crawl failed: {exc}]", "pages": [], "errors": [str(exc)], "socials": {}}
    crawl_ctx = crawl.get("context", "")
    socials_from_crawl = crawl.get("socials") or {}

    # Enrichment passes (each one is a Claude+web_search call, ~10-30s).
    # Every failure path returns an empty value so the interview can fall
    # back to asking the user.
    try:
        competitors = _onb.discover_competitors(brand_name, brand_url, crawl_ctx)
    except Exception:
        competitors = []
    try:
        socials = _onb.discover_socials_via_search(brand_name, brand_url, known=socials_from_crawl)
    except Exception:
        socials = socials_from_crawl
    try:
        voice = _onb.analyze_voice(brand_name, brand_url, crawl_ctx, socials=socials)
    except Exception:
        voice = {}

    enrichment = {"competitors": competitors, "socials": socials, "voice": voice}
    full_context = _onb.embed_enrichment(crawl_ctx, enrichment)
    # Persist crawl context (+ enrichment blob) onto the session for use
    # during chat + finalize.
    store.execute(
        "update chat_sessions set brand_context = ? where id = ?",
        (full_context[:30000], chat_session_id),
    )
    # Generate the opening assistant turn.
    try:
        first_msg = _onb.generate_initial_assistant_turn(brand_name, brand_url, full_context)
    except Exception as exc:
        first_msg = (
            f"Hi — I'm here to help build a Signal Room brief for {brand_name}. "
            f"(The auto-kickoff hit an issue: {exc}. Tell me anyway: who's the primary audience for this brand?)"
        )
    store.append_chat_message(chat_session_id, "assistant", first_msg)


@app.get("/{brand}/passcode-reveal")
def passcode_reveal(request: Request, brand: str, passcode: str = "") -> Any:
    _allowed_brand(brand)
    brand_row = store.get_brand(brand)
    if not brand_row:
        raise HTTPException(status_code=404)
    if brand_row.get("passcode_revealed_at"):
        # One-shot: don't re-show.
        return templates.TemplateResponse(
            request, "passcode_reveal.html",
            {"brand": brand, "passcode": "", "already_revealed": True},
        )
    if not passcode:
        raise HTTPException(status_code=400, detail="passcode query param required")
    store.mark_passcode_revealed(brand)
    return templates.TemplateResponse(
        request, "passcode_reveal.html",
        {"brand": brand, "passcode": passcode, "already_revealed": False},
    )


@app.get("/{brand}/auth")
def auth_get(request: Request, brand: str, next: str = "", error: str = "") -> Any:
    _allowed_brand(brand)
    return templates.TemplateResponse(request, "passcode_gate.html",
                                      {"brand": brand, "next": next or f"/{brand}", "error": error})


@app.post("/{brand}/auth")
def auth_post(brand: str, passcode: str = Form(...), next: str = Form("")) -> Any:
    _allowed_brand(brand)
    brand_row = store.get_brand(brand)
    if not brand_row:
        raise HTTPException(status_code=404)
    from . import auth as _auth
    if not _auth.verify_passcode(passcode, brand_row.get("passcode_hash", "")):
        return RedirectResponse(url=f"/{brand}/auth?next={next}&error=wrong", status_code=303)
    target = next if next.startswith("/") else f"/{brand}"
    response = RedirectResponse(url=target, status_code=303)
    _auth.set_passcode_cookie(response, brand, brand_row.get("passcode_session_token", ""))
    return response


@app.get("/{brand}/onboarding")
def onboarding_page(request: Request, brand: str) -> Any:
    _allowed_brand(brand)
    brand_row = store.get_brand(brand)
    if not brand_row:
        raise HTTPException(status_code=404)
    from . import auth as _auth
    _auth.require_passcode_or_redirect(request, brand_row, next_path=f"/{brand}/onboarding")
    session = store.latest_chat_session(brand, purpose="onboarding")
    messages = store.get_chat_messages(session["id"]) if session else []
    return templates.TemplateResponse(
        request, "onboarding.html",
        {
            "brand": brand,
            "brand_row": brand_row,
            "session_id": session.get("id") if session else "",
            "messages": messages,
            "ready": any("READY_TO_GENERATE" in m.get("content", "") for m in messages if m.get("role") == "assistant"),
            "session_closed": (session or {}).get("status") == "closed",
        },
    )


@app.post("/api/brands/{brand}/onboarding/chat")
async def onboarding_chat(brand: str, request: Request, body: dict = None) -> JSONResponse:
    _allowed_brand(brand)
    brand_row = store.get_brand(brand)
    if not brand_row:
        raise HTTPException(status_code=404)
    from . import auth as _auth
    if not _auth.has_valid_passcode(request, brand_row):
        return JSONResponse({"ok": False, "error": "passcode required"}, status_code=401)
    payload = await request.json()
    user_msg = (payload.get("message") or "").strip()
    session_id = payload.get("session_id") or ""
    if not user_msg or not session_id:
        return JSONResponse({"ok": False, "error": "session_id + message required"}, status_code=400)
    session = store.get_chat_session(session_id)
    if not session or session.get("brand_slug") != brand:
        return JSONResponse({"ok": False, "error": "session not found"}, status_code=404)
    if session.get("status") == "closed":
        return JSONResponse({"ok": False, "error": "session closed"}, status_code=409)

    store.append_chat_message(session_id, "user", user_msg)
    messages = store.get_chat_messages(session_id)
    history = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] in ("user", "assistant")]

    from . import onboarding as _onb
    # If the user pasted URLs in this turn, fetch them server-side and inline
    # the stripped text into the latest user message before calling Claude.
    # The stored transcript keeps the original message verbatim; only the
    # in-flight history Claude sees gets the <fetched> blocks appended.
    fetched = _onb.fetch_urls_for_chat(user_msg)
    if fetched and history and history[-1].get("role") == "user":
        history[-1] = {
            "role": "user",
            "content": _onb.augment_user_message_with_fetches(history[-1]["content"], fetched),
        }
    try:
        assistant_msg = _onb.next_assistant_turn(
            brand_row.get("name") or brand,
            brand_row.get("url") or "",
            session.get("brand_context") or "",
            history,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Claude call failed: {exc}"}, status_code=502)
    store.append_chat_message(session_id, "assistant", assistant_msg)
    return JSONResponse({
        "ok": True,
        "assistant": assistant_msg,
        "assistant_html": _onb.render_assistant_markdown(assistant_msg),
        "ready_to_generate": _onb.is_ready_to_generate(assistant_msg),
    })


@app.post("/{brand}/onboarding/finalize")
def onboarding_finalize(request: Request, brand: str) -> Any:
    _allowed_brand(brand)
    brand_row = store.get_brand(brand)
    if not brand_row:
        raise HTTPException(status_code=404)
    from . import auth as _auth
    _auth.require_passcode_or_redirect(request, brand_row, next_path=f"/{brand}/brief")
    session = store.latest_chat_session(brand, purpose="onboarding")
    if not session:
        raise HTTPException(status_code=400, detail="No onboarding session to finalize.")
    messages = store.get_chat_messages(session["id"])
    transcript = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] in ("user", "assistant")]
    from . import onboarding as _onb
    try:
        brief = _onb.generate_brief(
            brand_row.get("name") or brand,
            brand_row.get("url") or "",
            session.get("brand_context") or "",
            transcript,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Brief generation failed: {exc}")
    # Tripwire for cross-brand contamination: refuse to persist a brief whose
    # URL host doesn't match the brand row. Stash it on the session for audit
    # rather than silently writing the wrong brand's content.
    mismatch = _brief_host_mismatch(brand_row, brief)
    if mismatch:
        store.close_chat_session(session["id"], generated_brief_json=brief)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Brief host mismatch — brand '{brand}' has url host "
                f"'{mismatch['brand_host']}' but generated brief has host "
                f"'{mismatch['brief_host']}'. Not writing. Inspect the session "
                f"({session['id']}) and re-run onboarding."
            ),
        )
    store.update_brand_brief(brand, brief)
    store.close_chat_session(session["id"], generated_brief_json=brief)
    # Mirror to filesystem so projector + planner CLI still work.
    _mirror_brief_to_yaml(brand, brief)
    return RedirectResponse(url=f"/{brand}/brief", status_code=303)


def _brief_host_mismatch(brand_row: dict, brief: dict) -> dict | None:
    """Return mismatch info if the generated brief's URL host disagrees with
    the brand row's URL host. Returns None when they match (or when we lack
    enough info to compare — be lenient there, the guard is for catching
    obvious cross-brand drift, not nitpicking missing fields).
    """
    from urllib.parse import urlparse as _urlparse
    brand_url = (brand_row.get("url") or "").strip()
    brief_url = (brief.get("url") or "").strip()
    if not brand_url or not brief_url:
        return None
    def _host(u: str) -> str:
        h = _urlparse(u).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    bh = _host(brand_url)
    fh = _host(brief_url)
    if not bh or not fh:
        return None
    if bh == fh:
        return None
    return {"brand_host": bh, "brief_host": fh}


def _mirror_brief_to_yaml(brand: str, brief: dict) -> None:
    """Write brief.yaml mirror so the projector + planner CLIs see it.

    The brief dict from the chat finalizer has a flat shape; we wrap it
    in the `projection.signal_room.{pillars, discovery_queries, seed_sources}`
    envelope that signal_room/projector/from_brief.py expects.
    """
    import yaml as _yaml
    brand_dir = ROOT / "config" / "brands" / brand
    brand_dir.mkdir(parents=True, exist_ok=True)
    wrapped = {
        "brand": {
            "name": brief.get("name", brand),
            "slug": brand,
            "url": brief.get("url", ""),
            "one_liner": brief.get("one_liner", ""),
            "audience": brief.get("audience", []),
        },
        "projection": {
            "signal_room": {
                "pillars": brief.get("pillars", []),
                "discovery_queries": brief.get("discovery_queries", []),
                "seed_sources": brief.get("seed_sources", []),
            },
        },
    }
    (brand_dir / "brief.yaml").write_text(_yaml.safe_dump(wrapped, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _load_brief_from_disk(brand: str) -> dict:
    """Read config/brands/<brand>/brief.yaml and flatten the projection envelope
    into the shape the editor template expects. Returns {} if the file is
    missing or unparseable.

    Disk YAML has:
      brand: {name, slug, url, one_liner, audience}
      projection: {signal_room: {pillars, discovery_queries, seed_sources}}

    Editor template expects:
      {name, url, one_liner, audience, pillars, discovery_queries, seed_sources}
    """
    import yaml as _yaml
    brief_path = ROOT / "config" / "brands" / brand / "brief.yaml"
    if not brief_path.exists():
        return {}
    try:
        raw = _yaml.safe_load(brief_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    brand_block = raw.get("brand") or {}
    sr = ((raw.get("projection") or {}).get("signal_room") or {})
    return {
        "name": brand_block.get("name") or brand,
        "url": brand_block.get("url", ""),
        "one_liner": brand_block.get("one_liner", ""),
        "audience": brand_block.get("audience", []),
        "pillars": sr.get("pillars", []),
        "discovery_queries": sr.get("discovery_queries", []),
        "seed_sources": sr.get("seed_sources", []),
    }


def _resolve_brief_for_editor(brand: str) -> tuple[dict, dict, str]:
    """Return (brand_row_or_synth, brief, source). DB wins when populated;
    otherwise fall back to disk YAML. `source` is "db" or "disk" or "empty".

    For filesystem-only brands we synthesize a brand_row-shaped dict so the
    template + auth code don't crash on missing keys.
    """
    row = store.get_brand(brand) or {}
    db_brief = row.get("brief_json") or {}
    if db_brief.get("pillars") or db_brief.get("discovery_queries"):
        return row, db_brief, "db"
    disk_brief = _load_brief_from_disk(brand)
    if disk_brief.get("pillars") or disk_brief.get("discovery_queries"):
        synth = row or {"slug": brand, "name": disk_brief.get("name") or brand,
                        "url": disk_brief.get("url", ""), "passcode_hash": "",
                        "passcode_session_token": ""}
        return synth, disk_brief, "disk"
    # Nothing yet — surface an empty editor seeded with whatever the DB knows.
    synth = row or {"slug": brand, "name": brand, "url": "",
                    "passcode_hash": "", "passcode_session_token": ""}
    return synth, db_brief or {"name": brand, "url": row.get("url", "")}, "empty"


def _auth_gate_for_brief_editor(request: Request, brand_row: dict, brand: str) -> None:
    """Filesystem-only brands have no passcode_hash — they're committed in the
    repo, so anyone with repo access already has the data. Skip the gate when
    no passcode is configured. Otherwise enforce the normal flow.
    """
    if not (brand_row.get("passcode_hash") or "").strip():
        return
    from . import auth as _auth
    _auth.require_passcode_or_redirect(request, brand_row, next_path=f"/{brand}/brief")


@app.get("/{brand}/brief")
def brief_editor_get(request: Request, brand: str) -> Any:
    _allowed_brand(brand)
    brand_row, brief, source = _resolve_brief_for_editor(brand)
    _auth_gate_for_brief_editor(request, brand_row, brand)
    return templates.TemplateResponse(
        request, "brief_editor.html",
        {"brand": brand, "brand_row": brand_row, "brief": brief,
         "brief_source": source, "errors": {}, "saved": False},
    )


@app.post("/{brand}/brief")
async def brief_editor_post(request: Request, brand: str) -> Any:
    _allowed_brand(brand)
    brand_row, current_brief, _source = _resolve_brief_for_editor(brand)
    _auth_gate_for_brief_editor(request, brand_row, brand)
    form = await request.form()
    brief = _parse_brief_form(form, fallback=current_brief)
    # Persist to DB when a row exists; always mirror to disk so the worker +
    # planner CLIs stay in sync. For filesystem-only brands, disk is canonical.
    if store.get_brand(brand):
        store.update_brand_brief(brand, brief)
        store.update_brand_name(brand, brief.get("name", brand))
    _mirror_brief_to_yaml(brand, brief)
    return templates.TemplateResponse(
        request, "brief_editor.html",
        {"brand": brand, "brand_row": store.get_brand(brand) or brand_row,
         "brief": brief, "brief_source": "db" if store.get_brand(brand) else "disk",
         "errors": {}, "saved": True},
    )


def _parse_brief_form(form, fallback: dict) -> dict:
    """Parse the brief editor form fields into a BrandBrief-shaped dict.

    Repeating sections (pillars / queries / seeds) use indexed names like
    `pillar_0_name`, `pillar_0_keywords`, etc. We walk indices 0..N until
    we hit the first empty pillar name (treat as end-of-list).
    """
    def _get(key: str, default: str = "") -> str:
        v = form.get(key)
        return v.strip() if isinstance(v, str) else default

    out = {
        "name": _get("name", fallback.get("name", "")),
        "url": _get("url", fallback.get("url", "")),
        "one_liner": _get("one_liner", fallback.get("one_liner", "")),
        "audience": [line.strip() for line in _get("audience").splitlines() if line.strip()],
        "pillars": [],
        "discovery_queries": [],
        "seed_sources": [],
    }
    for i in range(20):
        pname = _get(f"pillar_{i}_name")
        if not pname:
            break
        kws = [k.strip().lower() for k in _get(f"pillar_{i}_keywords").splitlines() if k.strip()]
        out["pillars"].append({
            "id": f"P{i+1}",
            "name": pname,
            "why": _get(f"pillar_{i}_why"),
            "keywords": kws,
        })
    for i in range(30):
        topic = _get(f"query_{i}_topic")
        if not topic:
            break
        try:
            prio = int(_get(f"query_{i}_priority", "2"))
        except ValueError:
            prio = 2
        out["discovery_queries"].append({
            "id": _get(f"query_{i}_id") or topic.lower().replace(" ", "-")[:40],
            "priority": max(1, min(3, prio)),
            "topic": topic,
            "why": _get(f"query_{i}_why"),
        })
    for i in range(30):
        u = _get(f"seed_{i}_url")
        if not u:
            break
        out["seed_sources"].append({
            "url": u,
            "name": _get(f"seed_{i}_name") or u,
            "category": _get(f"seed_{i}_category") or "other",
            "why": _get(f"seed_{i}_why"),
        })
    return out


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
        "reference_groups": context["reference_groups"],
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
    social_items = [item for item in decorated if _result_bucket(item) == "social"]
    reference_items = [item for item in decorated if _result_bucket(item) == "reference"]
    ordered_items = social_items + reference_items
    return {
        "items": ordered_items,
        "source_counts": _source_counts(ordered_items),
        "date_groups": _date_groups(social_items),
        "reference_groups": _date_groups(reference_items),
    }


def _result_bucket(item: dict[str, Any]) -> str:
    bucket = str(item.get("result_bucket", "")).strip().lower()
    if bucket in {"social", "reference"}:
        return bucket
    source = str(item.get("display_source") or item.get("source") or "").lower()
    if source in {"x", "instagram", "youtube", "reddit", "tiktok"}:
        return "social"
    return "reference"


def _source_counts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(item.get("display_source") or "Source") for item in items)
    return [{"source": source, "count": count} for source, count in counts.most_common()]


def _date_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for item in items:
        groups.setdefault(str(item.get("date_group") or "Earlier"), []).append(item)
    for rows in groups.values():
        rows.sort(key=_group_row_sort_key)
    ordered_labels = ["Today", "Yesterday", "This week", "Last week", "Earlier this month", "Older", "Unknown date"]
    ordered_groups = [{"label": label, "rows": groups[label]} for label in ordered_labels if label in groups]
    ordered_groups.extend(
        {"label": label, "rows": rows} for label, rows in groups.items() if label not in ordered_labels
    )
    return ordered_groups


def _group_row_sort_key(item: dict[str, Any]) -> tuple[float, float, int, str]:
    rank = item.get("rank")
    try:
        normalized_rank = int(rank)
    except (TypeError, ValueError):
        normalized_rank = 1_000_000
    return (
        -_float_value(item.get("traction_score")),
        -_float_value(item.get("score")),
        normalized_rank,
        str(item.get("title", "")),
    )


def _float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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
