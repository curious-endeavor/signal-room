"""Render a trace.jsonl as a visual funnel in the Signal Room visual idiom.

Stages, top to bottom:
  1. Brief loaded
  2. Queries fired (horizontal bars sized by item count, label = actual search text)
  3. Raw items returned (big number)
  4. After dedup (big number, with dropped count)
  5. LLM scoring (count + four horizontal bars per score bucket)
  6. Digest (final top N)
Click any bar → jumps to the drill-down section.
Click any item → expands to show the exact Claude prompt + parsed response.
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


def _h(x: Any) -> str:
    return html.escape(str(x), quote=True)


def _json_block(obj: Any) -> str:
    try:
        s = json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        s = repr(obj)
    return f'<pre class="code">{_h(s)}</pre>'


def _pre(text: Any) -> str:
    return f'<pre class="code">{_h(text)}</pre>'


def _load_vendor_report(brand: str, qid: str) -> Dict[str, Any]:
    """Read the /last30days vendor report.json for one query if it's on disk.

    Looks in two layouts (vendor writes to one or the other depending on
    whether brand isolation is active for this run):
      1. data/last30days/runs/<brand>/<YYYY-MM-DD>/<qid>/report.json
      2. data/last30days/runs/<YYYY-MM-DD>/<qid>/report.json
    Picks the report with the latest mtime so the most recent run wins
    regardless of layout.
    """
    if not qid:
        return {}
    repo_root = Path(__file__).resolve().parents[1]
    runs_base = repo_root / "data" / "last30days" / "runs"
    if not runs_base.exists():
        return {}

    candidates: List[Path] = []
    branded = runs_base / brand
    if branded.exists():
        for date_dir in branded.iterdir():
            if not date_dir.is_dir():
                continue
            report = date_dir / qid / "report.json"
            if report.exists():
                candidates.append(report)
    for date_dir in runs_base.iterdir():
        if not date_dir.is_dir() or date_dir.name == brand:
            continue
        report = date_dir / qid / "report.json"
        if report.exists():
            candidates.append(report)

    if not candidates:
        return {}
    # Most recent mtime wins (latest run produced this report).
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    try:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception:
        return {}


def render_trace_html(jsonl_path: Path, html_path: Path, brand: str, started_at: str) -> Path:
    records: List[Dict[str, Any]] = []
    with Path(jsonl_path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    by_stage: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_stage[rec.get("stage", "?")].append(rec)

    state = _build_state(by_stage, records)
    state["brand"] = brand
    body = "\n".join([
        _header_html(brand, started_at, state),
        _funnel_html(state),
        _drilldowns_html(state),
    ])
    html_text = _PAGE_SHELL.format(title=f"Trace — {_h(brand)} — {_h(started_at)}", body=body, css=_CSS)
    Path(html_path).write_text(html_text, encoding="utf-8")
    return html_path


def _build_state(by_stage, records):
    brief = (by_stage.get("brief_loaded") or [{}])[0].get("payload", {})
    l30_started = (by_stage.get("last30days_started") or [{}])[0].get("payload", {})
    l30_done = (by_stage.get("last30days_complete") or [{}])[0].get("payload", {})
    items_returned = {r["payload"]["query_id"]: r["payload"] for r in by_stage.get("items_returned", [])}
    dedup = (by_stage.get("dedup_decision") or [{}])[0].get("payload", {})
    scoring_started = (by_stage.get("llm_scoring_started") or [{}])[0].get("payload", {})
    scoring_done = (by_stage.get("llm_scoring_complete") or [{}])[0].get("payload", {})
    scores = [r.get("payload", {}) for r in by_stage.get("llm_score", [])]
    digest = (by_stage.get("digest_built") or [{}])[0].get("payload", {})
    duration_s = records[-1].get("t_ms", 0) / 1000 if records else 0

    queries = []
    for q in l30_started.get("queries", []) or []:
        qid = q.get("id", "")
        ret = items_returned.get(qid, {})
        queries.append({
            "id": qid,
            "topic": q.get("topic", ""),
            "search_text": q.get("search_text") or q.get("topic", ""),
            "why": q.get("why", ""),
            "priority": q.get("priority"),
            "item_count": ret.get("item_count", 0),
            "samples": ret.get("sample_items", []),
        })
    max_q = max((q["item_count"] for q in queries), default=1) or 1

    # GDELT branch (parallel Stage 2 card when present).
    gdelt_started = (by_stage.get("gdelt_started") or [{}])[0].get("payload", {})
    gdelt_done = (by_stage.get("gdelt_complete") or [{}])[0].get("payload", {})
    pillar_returns = {
        r["payload"]["pillar"]: r["payload"]
        for r in by_stage.get("gdelt_pillar_items_returned", [])
    }
    gdelt_pillars = []
    for p in gdelt_started.get("pillars", []) or []:
        ret = pillar_returns.get(p, {})
        gdelt_pillars.append({
            "id": p,
            "item_count": ret.get("item_count", 0),
            "samples": ret.get("sample_items", []),
        })
    max_gp = max((p["item_count"] for p in gdelt_pillars), default=1) or 1

    buckets = {"core": [], "adjacent": [], "tangential": [], "off": []}
    for p in scores:
        score = (p.get("parsed") or {}).get("score", 0) or 0
        if score >= 80:
            buckets["core"].append(p)
        elif score >= 60:
            buckets["adjacent"].append(p)
        elif score >= 40:
            buckets["tangential"].append(p)
        else:
            buckets["off"].append(p)
    for key in buckets:
        buckets[key].sort(key=lambda p: -(p.get("parsed", {}).get("score") or 0))
    max_b = max((len(v) for v in buckets.values()), default=1) or 1

    return {
        "brief": brief,
        "l30_started": l30_started,
        "l30_done": l30_done,
        "queries": queries,
        "max_q": max_q,
        "gdelt_started": gdelt_started,
        "gdelt_done": gdelt_done,
        "gdelt_pillars": gdelt_pillars,
        "max_gp": max_gp,
        "dedup": dedup,
        "scoring_started": scoring_started,
        "scoring_done": scoring_done,
        "scores": scores,
        "buckets": buckets,
        "max_b": max_b,
        "digest": digest,
        "duration_s": duration_s,
    }


def _header_html(brand, started_at, state):
    return f"""
<header class="site-header">
  <a class="brand" href="#">Signal Room</a>
  <div class="header-meta">
    <span>trace</span>
    <span class="brand-tag">{_h(brand)}</span>
    <span>{_h(started_at)}</span>
    <span>{state['duration_s']:.0f}s</span>
  </div>
</header>
"""


def _funnel_html(state):
    queries = state["queries"]
    max_q = state["max_q"]
    dedup = state["dedup"]
    buckets = state["buckets"]
    max_b = state["max_b"]
    digest = state["digest"]
    total_raw = state["l30_done"].get("total_item_count") or sum(q["item_count"] for q in queries)
    brief = state["brief"]

    query_rows = "".join(f"""
<a class="funnel-bar" href="#q-{_h(q['id'])}">
  <span class="fb-label" title="{_h(q['id'])}">{_h(q['topic'] or q['search_text'] or q['id'])}</span>
  <span class="fb-track"><span class="fb-fill" style="width: {100 * q['item_count'] / max_q:.1f}%"></span></span>
  <span class="fb-count">{_h(q['item_count'])}</span>
</a>
""" for q in queries)

    # GDELT branch
    gdelt_pillars = state.get("gdelt_pillars") or []
    max_gp = state.get("max_gp", 1) or 1
    has_gdelt = bool(state.get("gdelt_started"))
    gdelt_total = state.get("gdelt_done", {}).get("item_count") or sum(p["item_count"] for p in gdelt_pillars)
    pillar_rows = "".join(f"""
<a class="funnel-bar" href="#p-{_h(p['id'])}">
  <span class="fb-label">{_h(p['id'])}</span>
  <span class="fb-track"><span class="fb-fill fb-fill-gdelt" style="width: {100 * p['item_count'] / max_gp:.1f}%"></span></span>
  <span class="fb-count">{_h(p['item_count'])}</span>
</a>
""" for p in gdelt_pillars)

    bucket_meta = [
        ("core", "CORE", "80–100"),
        ("adjacent", "ADJACENT", "60–79"),
        ("tangential", "TANGENTIAL", "40–59"),
        ("off", "OFF-TERRITORY", "0–39"),
    ]
    bucket_rows = "".join(f"""
<a class="funnel-bar bucket-{key}" href="#bucket-{key}">
  <span class="fb-label">{_h(label)} <span class="dim">{_h(range_)}</span></span>
  <span class="fb-track"><span class="fb-fill fb-fill-{key}" style="width: {100 * len(buckets[key]) / max_b:.1f}%"></span></span>
  <span class="fb-count">{_h(len(buckets[key]))}</span>
</a>
""" for key, label, range_ in bucket_meta)

    return f"""
<section class="funnel">

  <div class="stage-card stage-narrow">
    <div class="stage-num">1</div>
    <div class="stage-body">
      <div class="stage-title">Brief</div>
      <div class="stage-sub mono">{_h(brief.get('path','—'))} · {_h(brief.get('size_bytes', 0))} bytes</div>
    </div>
  </div>

  <div class="arrow">↓</div>

  <div class="stage-card stage-2 {'has-gdelt' if has_gdelt else ''}">
    <div class="stage-num">2</div>
    <div class="stage-body">
      <div class="stage-title-row">
        <div class="stage-title">Fetched</div>
      </div>
      <div class="stage-2-grid">
        <div class="stage-2-col">
          <div class="stage-2-sub mono">/last30days · social</div>
          <div class="stage-2-num">{_h(len(queries))} queries · {_h(sum(q["item_count"] for q in queries))} items</div>
          <div class="bars">{query_rows}</div>
        </div>
        {f'''<div class="stage-2-col stage-2-col-gdelt">
          <div class="stage-2-sub mono">GDELT · press</div>
          <div class="stage-2-num">{_h(len(gdelt_pillars))} pillars · {_h(gdelt_total)} items</div>
          <div class="bars">{pillar_rows}</div>
        </div>''' if has_gdelt else ''}
      </div>
    </div>
  </div>

  <div class="arrow">↓</div>

  <div class="stage-card stage-tight">
    <div class="stage-num">3</div>
    <div class="stage-body">
      <div class="stage-title-row">
        <div class="stage-title">Raw items returned</div>
        <div class="big-number">{_h(total_raw)}</div>
      </div>
      <div class="stage-sub">across {_h(len(queries))} queries · click a query above to see what each returned</div>
    </div>
  </div>

  <div class="arrow">↓</div>

  <div class="stage-card stage-tight">
    <div class="stage-num">4</div>
    <div class="stage-body">
      <div class="stage-title-row">
        <div class="stage-title">After dedup</div>
        <div class="big-number">{_h(dedup.get('output_count', 0))}</div>
      </div>
      <div class="stage-sub">{_h(dedup.get('input_count', 0))} in · <span class="dim">{_h(dedup.get('dropped_count', 0))} dropped</span></div>
    </div>
  </div>

  <div class="arrow">↓</div>

  <div class="stage-card">
    <div class="stage-num">5</div>
    <div class="stage-body">
      <div class="stage-title-row">
        <div class="stage-title">LLM scoring</div>
        <div class="big-number">{_h(len(state['scores']))}</div>
      </div>
      <div class="stage-sub mono">{_h(state['scoring_started'].get('model','—'))}</div>
      <div class="bars bars-scores">{bucket_rows}</div>
    </div>
  </div>

  <div class="arrow">↓</div>

  <div class="stage-card stage-tight">
    <div class="stage-num">6</div>
    <div class="stage-body">
      <div class="stage-title-row">
        <div class="stage-title">Digest</div>
        <div class="big-number">{_h(digest.get('top_count', 0))}</div>
      </div>
      <div class="stage-sub"><a href="#digest">jump to top items ↓</a></div>
    </div>
  </div>

</section>
"""


def _drilldowns_html(state):
    return "\n".join([
        _queries_drilldown(state),
        _buckets_drilldown(state),
        _digest_drilldown(state),
    ])


def _queries_drilldown(state):
    rows = []
    brand = state.get("brand", "")
    for q in state["queries"]:
        samples_html = "".join(
            f"<li>{_h(s.get('title',''))} <span class='dim mono'>· {_h(s.get('source',''))}</span></li>"
            for s in q["samples"]
        ) or "<li class='dim'>nothing returned</li>"

        # Pull the vendor processing report for this query.
        report = _load_vendor_report(brand, q["id"])
        processing_html = _vendor_processing_html(report) if report else ""

        rows.append(f"""
<details class="drill-item" id="q-{_h(q['id'])}">
  <summary>
    <span class="drill-title">{_h(q['topic'])}</span>
    <span class="score">{_h(q['item_count'])}</span>
  </summary>
  <div class="drill-body">
    <p class="micro">id · <span class="mono">{_h(q['id'])}</span></p>
    <p class="micro">why · {_h(q['why'])}</p>
    <p class="micro mono">search · {_h(q['search_text'])}</p>
    {processing_html}
    <p class="micro">items returned</p>
    <ul class="bare-list">{samples_html}</ul>
  </div>
</details>""")
    return f"""
<section class="drilldown">
  <h2 class="drill-section">Stage 2 · Queries</h2>
  {"".join(rows)}
</section>
"""


def _vendor_processing_html(report: Dict[str, Any]) -> str:
    """Render the /last30days vendor report.json as a compact breakdown.

    Shows the journey: raw query → planner's interpretation → per-provider
    actual search → items grouped by source. Click any source to see its
    individual items so you can spot which providers returned noise.
    """
    items_by_source = report.get("items_by_source") or {}
    errors_by_source = report.get("errors_by_source") or {}
    provider_runtime = report.get("provider_runtime") or {}
    query_plan = report.get("query_plan") or {}
    artifacts = report.get("artifacts") or {}
    resolved = artifacts.get("resolved") or {}
    range_from = report.get("range_from", "")
    range_to = report.get("range_to", "")

    raw_topic = query_plan.get("raw_topic", "")
    intent = query_plan.get("intent", "—")
    freshness = query_plan.get("freshness_mode", "—")
    planner_model = provider_runtime.get("planner_model", "—")
    plan_source = artifacts.get("plan_source", "—")
    plan_notes = query_plan.get("notes") or []
    used_fallback = any("fallback" in str(n).lower() for n in plan_notes)

    # The transformation: raw topic → planner intent + subqueries.
    subqueries = query_plan.get("subqueries") or []
    subq_items_html = ""
    if subqueries:
        items = []
        for s in subqueries:
            if isinstance(s, dict):
                rq = s.get("ranking_query") or s.get("label", "")
                items.append(f"<li>{_h(rq)}</li>")
            else:
                items.append(f"<li>{_h(s)}</li>")
        subq_items_html = "<ul class='bare-list'>" + "".join(items) + "</ul>"
    else:
        subq_items_html = "<p class='dim micro'>none — raw topic used as-is</p>"

    # Per-provider details: artifacts + items, grouped by source.
    def _provider_artifact_html(source: str) -> str:
        art = artifacts.get(source)
        if not art:
            return ""
        if source == "grounding" and isinstance(art, list):
            bits = []
            for entry in art:
                lbl = entry.get("label", "")
                count = entry.get("resultCount", "")
                queries = entry.get("webSearchQueries", []) or []
                bits.append(f"<div class='prov-art-line'><span class='mono dim'>{_h(lbl)}</span> · {_h(count)} results · queries: <span class='mono'>{_h(', '.join(queries))}</span></div>")
            return "".join(bits)
        return f"<div class='prov-art-line mono dim'>{_h(json.dumps(art, ensure_ascii=False)[:300])}</div>"

    src_max = max((len(v) if isinstance(v, list) else int(v or 0) for v in items_by_source.values()), default=1) or 1
    sorted_sources = sorted(items_by_source.items(), key=lambda kv: -(len(kv[1]) if isinstance(kv[1], list) else int(kv[1] or 0)))

    source_blocks = []
    for source, items in sorted_sources:
        items_list = items if isinstance(items, list) else []
        count = len(items_list)
        prov_artifact_html = _provider_artifact_html(source)
        err = errors_by_source.get(source, "")
        if count == 0 and not err and not prov_artifact_html:
            source_blocks.append(f"""
<details class="src-block">
  <summary>
    <span class="src-label mono">{_h(source)}</span>
    <span class="src-track"><span class="src-fill" style="width: 0%"></span></span>
    <span class="src-count mono">0</span>
    <span class="dim mono">no results, no errors</span>
  </summary>
</details>""")
            continue
        item_rows = "".join(
            f"<li><a href='{_h(it.get('source_url',''))}' target='_blank' rel='noreferrer'>{_h(it.get('title',''))}</a></li>"
            for it in items_list
        ) or "<li class='dim'>no items returned by this source</li>"
        source_blocks.append(f"""
<details class="src-block">
  <summary>
    <span class="src-label mono">{_h(source)}</span>
    <span class="src-track"><span class="src-fill" style="width: {100 * count / src_max:.1f}%"></span></span>
    <span class="src-count mono">{_h(count)}</span>
    {('<span class="src-err">err: ' + _h(err) + '</span>') if err else ''}
  </summary>
  <div class="src-detail">
    {prov_artifact_html}
    <ul class="bare-list">{item_rows}</ul>
  </div>
</details>""")

    # Resolved entities (what the planner pulled out of the topic).
    resolved_summary = []
    for k, v in (resolved or {}).items():
        if not v:
            continue
        if isinstance(v, list):
            if len(v) == 0:
                continue
            resolved_summary.append(f"<span><b>{_h(k)}</b> {_h(', '.join(map(str, v)))}</span>")
        else:
            resolved_summary.append(f"<span><b>{_h(k)}</b> {_h(v)}</span>")
    resolved_html = ""
    if resolved_summary:
        resolved_html = "<p class='micro'>resolved entities (planner extraction)</p><div class='vendor-meta'>" + "".join(resolved_summary) + "</div>"

    notes_html = ""
    if plan_notes:
        notes_html = "<p class='micro'>planner notes</p><ul class='bare-list'>" + "".join(f"<li class='mono dim'>{_h(n)}</li>" for n in plan_notes) + "</ul>"

    return f"""
<p class="micro">how /last30days processed this</p>
<div class="vendor-block">
  <div class="vendor-meta">
    <span><b>intent</b> {_h(intent)}</span>
    <span><b>freshness</b> {_h(freshness)}</span>
    <span><b>range</b> {_h(range_from)} → {_h(range_to)}</span>
    <span><b>planner</b> {_h(planner_model)} <span class="dim">({_h(plan_source)})</span></span>
    {'<span class="vendor-fallback">⚠ planner fell back</span>' if used_fallback else ''}
  </div>

  <p class="micro">raw topic we sent</p>
  <div class="vendor-quote mono">{_h(raw_topic)}</div>

  <p class="micro">subqueries the planner actually ran</p>
  {subq_items_html}

  {resolved_html}

  <p class="micro">per-source breakdown (click to see items)</p>
  <div class="src-bars">{"".join(source_blocks)}</div>

  {notes_html}
</div>
"""


def _buckets_drilldown(state):
    blocks = []
    bucket_labels = {
        "core": ("Core (80–100)", "CORE"),
        "adjacent": ("Adjacent (60–79)", "ADJACENT"),
        "tangential": ("Tangential (40–59)", "TANGENTIAL"),
        "off": ("Off-territory (0–39)", "OFF"),
    }
    for key, (heading, _) in bucket_labels.items():
        items = state["buckets"][key]
        if not items:
            blocks.append(f"""
<section class="bucket-block" id="bucket-{key}">
  <h3 class="drill-section bucket-{key}">{heading} <span class="dim">· 0</span></h3>
  <p class="dim micro">no items</p>
</section>""")
            continue
        item_rows = "".join(_score_item_row(p) for p in items)
        blocks.append(f"""
<section class="bucket-block" id="bucket-{key}">
  <h3 class="drill-section bucket-{key}">{heading} <span class="dim">· {len(items)}</span></h3>
  {item_rows}
</section>""")
    return f"""
<section class="drilldown">
  <h2 class="drill-section">Stage 5 · Scoring drill-down</h2>
  {"".join(blocks)}
</section>
"""


def _score_item_row(p):
    item = p.get("item", {})
    parsed = p.get("parsed", {})
    score = int(parsed.get("score", 0) or 0)
    fit = parsed.get("fit", "?")
    pillar = ", ".join(parsed.get("pillar_fit") or []) or "—"
    action = parsed.get("action_type", "?")
    return f"""
<details class="drill-item">
  <summary>
    <span class="drill-title">{_h(item.get('title',''))}</span>
    <span class="score">{score}</span>
    <span class="row-meta dim">
      <span class="mono">{_h(item.get('source',''))}</span>
      <span class="pill pill-fit-{_h(fit)}">{_h(fit)}</span>
      <span class="pill pill-pillar">{_h(pillar)}</span>
      <span class="pill">{_h(action)}</span>
    </span>
  </summary>
  <div class="drill-body two-col">
    <div>
      <p class="micro">Item</p>
      <p class="micro mono"><a href="{_h(item.get('source_url',''))}" target="_blank" rel="noreferrer">{_h(item.get('source_url',''))}</a></p>
      <p class="micro">Summary</p>
      {_pre((item.get('summary','') or '')[:500])}
    </div>
    <div>
      <p class="micro">Sent to Claude</p>
      {_pre(p.get('user_message',''))}
      <p class="micro">Got back</p>
      {_json_block(parsed)}
    </div>
  </div>
</details>"""


def _digest_drilldown(state):
    digest = state["digest"]
    rows = "".join(
        f"""<div class="digest-row">
  <span class="digest-rank mono">#{i+1}</span>
  <div class="digest-body">
    <div class="drill-title">{_h(s.get('title',''))}</div>
    <div class="row-meta dim"><span class="mono">{_h(s.get('source',''))}</span> · <span>{_h(', '.join(s.get('pillar_fit', [])) or '—')}</span></div>
  </div>
  <span class="score">{int(s.get('score',0))}</span>
</div>"""
        for i, s in enumerate(digest.get("top_summaries", []))
    )
    return f"""
<section class="drilldown" id="digest">
  <h2 class="drill-section">Stage 6 · Digest (top {digest.get('top_count', 0)})</h2>
  {rows or '<p class="dim micro">No digest items.</p>'}
</section>
"""


_CSS = """
@import url("https://staging.curiousendeavor.com/canon/system/tokens.css");
* { box-sizing: border-box; }
html { color: var(--ce-ink); background: var(--ce-bg); font-family: var(--sans); font-size: var(--t-body); line-height: var(--lh-body); -webkit-font-smoothing: antialiased; }
body { margin: 0; min-width: 320px; background: var(--ce-bg); }
::selection { color: #fff; background: var(--ce-red); }
a { color: inherit; text-decoration: none; }
a:hover { color: var(--ce-red); }

.site-header { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; justify-content: space-between; gap: var(--s-4); width: min(960px, calc(100vw - 48px)); margin: 0 auto; padding: 14px 0; border-bottom: var(--hair); background: rgba(255,255,255,0.94); backdrop-filter: blur(20px) saturate(160%); }
.brand { color: var(--ce-black); font-family: var(--serif); font-size: 18px; font-weight: 400; letter-spacing: -0.01em; }
.brand::after { content: "."; color: var(--ce-red); }
.header-meta { display: flex; align-items: center; gap: var(--s-3); color: var(--ce-grey); font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; }
.brand-tag { color: var(--ce-red); font-weight: 500; }

.funnel { width: min(960px, calc(100vw - 48px)); margin: var(--s-6) auto var(--s-5); display: flex; flex-direction: column; gap: 0; }
.stage-card { display: grid; grid-template-columns: 48px 1fr; gap: var(--s-3); align-items: start; padding: var(--s-4) var(--s-5); border: 1px solid var(--ce-border); border-radius: var(--r-3); background: #fff; }
.stage-card.stage-tight { padding: var(--s-3) var(--s-5); }
.stage-card.stage-narrow { background: var(--ce-bg); border-style: dashed; }
.stage-num { width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; background: var(--ce-red); color: #fff; font-family: var(--mono); font-size: 12px; font-weight: 500; }
.stage-body { min-width: 0; }
.stage-title-row { display: flex; align-items: baseline; justify-content: space-between; gap: var(--s-3); }
.stage-title { color: var(--ce-black); font-family: var(--serif); font-size: 20px; font-weight: 400; letter-spacing: -0.015em; }
.stage-sub { margin-top: 4px; color: var(--ce-grey); font-size: 13px; }
.stage-sub a { color: var(--ce-red); }
.stage-sub a:hover { text-decoration: underline; }
.big-number { color: var(--ce-red); font-family: var(--mono); font-size: 28px; font-weight: 500; line-height: 1; }
.arrow { align-self: center; color: var(--ce-light); font-size: 18px; line-height: 1; padding: 6px 0; }

.bars { margin-top: var(--s-3); display: flex; flex-direction: column; gap: 8px; }
.funnel-bar { display: grid; grid-template-columns: minmax(0, 3fr) minmax(80px, 1fr) auto; gap: var(--s-3); align-items: center; padding: 6px 0; cursor: pointer; }
.funnel-bar:hover { color: var(--ce-red); }
.fb-label { color: var(--ce-ink); font-family: var(--sans); font-size: 14px; line-height: 1.4; padding-right: 8px; }
.bars-scores .fb-label { font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }
.fb-track { display: block; height: 8px; background: var(--ce-bg); border: 1px solid var(--ce-border); border-radius: var(--r-pill); overflow: hidden; }
.fb-fill { display: block; height: 100%; background: var(--ce-red); border-radius: var(--r-pill); }
.fb-count { color: var(--ce-red); font-family: var(--mono); font-size: 13px; font-weight: 500; text-align: right; min-width: 32px; }
.fb-fill-core { background: var(--ce-red); }
.fb-fill-adjacent { background: var(--ce-grey); }
.fb-fill-tangential { background: var(--ce-light); }
.fb-fill-off { background: var(--ce-border); }
.fb-fill-gdelt { background: #0e7490; }

/* Stage 2 parallel columns (last30days + GDELT) */
.stage-2-grid { display: grid; grid-template-columns: 1fr; gap: var(--s-4); margin-top: var(--s-2); }
.stage-2.has-gdelt .stage-2-grid { grid-template-columns: 1fr 1fr; }
@media (max-width: 880px) { .stage-2.has-gdelt .stage-2-grid { grid-template-columns: 1fr; } }
.stage-2-col { min-width: 0; }
.stage-2-col-gdelt { padding-left: var(--s-4); border-left: 1px dashed var(--ce-border); }
@media (max-width: 880px) { .stage-2-col-gdelt { padding-left: 0; border-left: none; border-top: 1px dashed var(--ce-border); padding-top: var(--s-3); } }
.stage-2-sub { color: var(--ce-grey); font-size: 11px; text-transform: uppercase; letter-spacing: 0.10em; margin-bottom: 4px; }
.stage-2-num { font-family: var(--mono); font-size: 12px; color: var(--ce-red); margin-bottom: var(--s-2); }
.bucket-core .fb-count { color: var(--ce-red); }
.bucket-adjacent .fb-count { color: var(--ce-grey); }
.bucket-tangential .fb-count, .bucket-off .fb-count { color: var(--ce-light); }

.drilldown { width: min(var(--container), calc(100vw - 48px)); margin: var(--s-6) auto var(--s-5); }
.drill-section { margin: var(--s-5) 0 var(--s-3); color: var(--ce-red); font-family: var(--mono); font-size: var(--t-label); font-weight: 500; letter-spacing: var(--ls-label); text-transform: uppercase; }
.drill-section.bucket-adjacent { color: var(--ce-grey); }
.drill-section.bucket-tangential, .drill-section.bucket-off { color: var(--ce-light); }
.drill-item { padding: var(--s-3) 0; border-bottom: var(--hair); }
.drill-item > summary { cursor: pointer; list-style: none; display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; gap: var(--s-3); align-items: baseline; }
.drill-item > summary::-webkit-details-marker { display: none; }
.drill-item > summary::before { content: "▸"; display: inline-block; color: var(--ce-light); font-size: 10px; width: 10px; transition: transform 0.15s; }
.drill-item[open] > summary::before { transform: rotate(90deg); }
.drill-title { color: var(--ce-black); font-family: var(--serif); font-size: 16px; letter-spacing: -0.01em; line-height: 1.25; min-width: 0; }
.row-meta { display: inline-flex; flex-wrap: wrap; gap: var(--s-2); align-items: center; font-size: 12px; }
.score { color: var(--ce-red); font-family: var(--mono); font-size: 13px; font-weight: 500; min-width: 36px; text-align: right; }
.pill { border: 1px solid var(--ce-border); border-radius: var(--r-pill); padding: 1px 7px; color: var(--ce-grey); background: var(--ce-bg); font-family: var(--mono); font-size: 9.5px; letter-spacing: 0.10em; text-transform: uppercase; }
.pill-pillar { color: var(--ce-red); }
.pill-fit-core { border-color: var(--ce-red); color: var(--ce-red); background: var(--ce-red-soft); }
.pill-fit-adjacent { color: var(--ce-grey); }
.pill-fit-tangential, .pill-fit-off-territory { color: var(--ce-light); }
.drill-body { margin-top: var(--s-3); padding-top: var(--s-3); padding-left: 20px; border-top: 1px dashed var(--ce-border); }
.drill-body.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: var(--s-5); }
@media (max-width: 880px) { .drill-body.two-col { grid-template-columns: 1fr; } }
.micro { margin: var(--s-3) 0 var(--s-2); color: var(--ce-light); font-family: var(--mono); font-size: 10px; font-weight: 500; letter-spacing: 0.10em; text-transform: uppercase; }
.micro a { color: var(--ce-red); }
.dim { color: var(--ce-light); }
.mono { font-family: var(--mono); font-size: 12px; }
.bare-list { list-style: none; padding: 0; margin: 4px 0 0; }
.bare-list li { padding: 4px 0; color: var(--ce-ink); font-size: 13px; line-height: 1.5; border-bottom: 1px dotted var(--ce-border); }
.bare-list li:last-child { border-bottom: none; }
.code { margin: 4px 0; padding: var(--s-3); border: 1px solid var(--ce-border); border-radius: var(--r-2); background: var(--ce-bg); color: var(--ce-ink); font-family: var(--mono); font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; overflow-x: auto; max-height: 280px; overflow-y: auto; }
.digest-row { display: grid; grid-template-columns: 36px 1fr auto; gap: var(--s-3); align-items: center; padding: var(--s-3) 0; border-bottom: var(--hair); }
.digest-rank { color: var(--ce-light); font-size: 12px; }
.digest-body { min-width: 0; }
.bucket-block { margin-top: var(--s-4); }

.vendor-block { margin: var(--s-2) 0 var(--s-3); padding: var(--s-3); border: 1px solid var(--ce-border); border-radius: var(--r-2); background: var(--ce-bg); }
.vendor-meta { display: flex; flex-wrap: wrap; gap: var(--s-3); margin-bottom: var(--s-2); color: var(--ce-grey); font-family: var(--mono); font-size: 11px; }
.vendor-meta b { color: var(--ce-light); font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em; margin-right: 4px; }
.vendor-fallback { color: var(--ce-red); font-weight: 500; }
.src-bars { display: flex; flex-direction: column; gap: 4px; margin: 4px 0; }
.src-bar { display: grid; grid-template-columns: 110px minmax(80px, 1fr) auto auto; gap: var(--s-2); align-items: center; }
.src-label { color: var(--ce-ink); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
.src-track { display: block; height: 6px; background: #fff; border: 1px solid var(--ce-border); border-radius: var(--r-pill); overflow: hidden; }
.src-fill { display: block; height: 100%; background: var(--ce-red); }
.src-count { color: var(--ce-red); font-size: 12px; min-width: 28px; text-align: right; }
.src-err { color: var(--ce-red); font-size: 10px; font-family: var(--mono); }

.vendor-quote { padding: 8px 12px; margin: 4px 0; background: #fff; border-left: 3px solid var(--ce-red); font-size: 13px; color: var(--ce-ink); }

.src-block { padding: 4px 0; border-bottom: 1px dotted var(--ce-border); }
.src-block:last-child { border-bottom: none; }
.src-block > summary { cursor: pointer; list-style: none; display: grid; grid-template-columns: 110px minmax(80px, 1fr) auto auto; gap: var(--s-2); align-items: center; padding: 4px 0; }
.src-block > summary::-webkit-details-marker { display: none; }
.src-block > summary::before { content: "▸"; display: inline-block; color: var(--ce-light); font-size: 9px; width: 8px; margin-right: 2px; transition: transform 0.15s; }
.src-block[open] > summary::before { transform: rotate(90deg); }
.src-detail { padding: 6px 0 8px 18px; }
.src-detail .bare-list li a { color: var(--ce-ink); }
.src-detail .bare-list li a:hover { color: var(--ce-red); text-decoration: underline; }
.prov-art-line { margin: 4px 0; font-size: 12px; color: var(--ce-grey); }
.prov-art-line .mono { font-size: 11px; }
"""

_PAGE_SHELL = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://use.typekit.net" crossorigin>
<link rel="stylesheet" href="https://use.typekit.net/ffj8sbd.css">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap">
<style>{css}</style>
</head><body>{body}</body></html>
"""
