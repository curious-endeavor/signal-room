"""Render a trace.jsonl as a visual funnel.

Goals:
- Show the pipeline as a top-to-bottom funnel: queries → raw items → dedup →
  score buckets → digest.
- Each stage is a card with a big number; nothing else by default.
- Per-query bars are sized by item count, so dud queries are visible at a glance.
- Score buckets are horizontal bars.
- Items at each stage are tucked behind click-to-reveal — never default-open.
- Per-item drill-down (the exact prompt sent and the exact response received)
  is one more click in.

The visual identity matches Signal Room (CE tokens, Typekit/Inter, red accent).
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List


def _h(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _json_block(obj: Any) -> str:
    try:
        s = json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        s = repr(obj)
    return f'<pre class="code">{_h(s)}</pre>'


def _pre(text: Any) -> str:
    return f'<pre class="code">{_h(text)}</pre>'


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

    # Build state once; render funnel + drill-downs from it.
    state = _build_state(by_stage, records)

    parts: List[str] = []
    parts.append(_header_html(brand, started_at, state))
    parts.append(_funnel_html(state))
    parts.append(_drilldowns_html(state))

    body = "\n".join(parts)
    html_text = _PAGE_SHELL.format(title=f"Trace — {_h(brand)} — {_h(started_at)}", body=body, css=_CSS)
    Path(html_path).write_text(html_text, encoding="utf-8")
    return html_path


def _build_state(by_stage, records):
    started = (by_stage.get("pipeline_started") or [{}])[0].get("payload", {})
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

    # Bucket items by score band
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
    max_b = max((len(v) for v in buckets.values()), default=1) or 1

    return {
        "brief": brief,
        "l30_started": l30_started,
        "l30_done": l30_done,
        "queries": queries,
        "max_q": max_q,
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

    # Stage 1: brief — minimal pill
    brief = state["brief"]

    # Stage 2: queries as bars — show the actual search text fired, not the slug ID.
    query_rows = "".join(f"""
<a class="funnel-bar" href="#q-{_h(q['id'])}">
  <span class="fb-label" title="{_h(q['id'])}">{_h(q['topic'] or q['search_text'] or q['id'])}</span>
  <span class="fb-track"><span class="fb-fill" style="width: {100 * q['item_count'] / max_q:.1f}%"></span></span>
  <span class="fb-count">{_h(q['item_count'])}</span>
</a>
""" for q in queries)

    # Stage 4: score buckets
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

  <div class="stage-card">
    <div class="stage-num">2</div>
    <div class="stage-body">
      <div class="stage-title-row">
        <div class="stage-title">Queries fired</div>
        <div class="big-number">{_h(len(queries))}</div>
      </div>
      <div class="bars">{query_rows}</div>
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
    for q in state["queries"]:
        samples_html = "".join(
            f"<li>{_h(s.get('title',''))} <span class='dim mono'>· {_h(s.get('source',''))}</span></li>"
            for s in q["samples"]
        ) or "<li class='dim'>nothing returned</li>"
        rows.append(f"""
<details class="drill-item" id="q-{_h(q['id'])}">
  <summary>
    <span class="mono">{_h(q['id'])}</span>
    <span class="drill-title">{_h(q['topic'])}</span>
    <span class="score">{_h(q['item_count'])}</span>
  </summary>
  <div class="drill-body">
    <p class="micro">why · {_h(q['why'])}</p>
    <p class="micro mono">search · {_h(q['search_text'])}</p>
    <ul class="bare-list">{samples_html}</ul>
  </div>
</details>""")
    return f"""
<section class="drilldown">
  <h2 class="drill-section">Stage 2 · Queries</h2>
  {"".join(rows)}
</section>
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
        f"""<div class="digest-row" id="digest">
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
html {
  color: var(--ce-ink);
  background: var(--ce-bg);
  font-family: var(--sans);
  font-size: var(--t-body);
  line-height: var(--lh-body);
  -webkit-font-smoothing: antialiased;
}
body { margin: 0; min-width: 320px; background: var(--ce-bg); }
::selection { color: #fff; background: var(--ce-red); }
a { color: inherit; text-decoration: none; }
a:hover { color: var(--ce-red); }

.site-header {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--s-4);
  width: min(var(--container), calc(100vw - (var(--gutter) * 2)));
  margin: 0 auto;
  padding: 14px 0;
  border-bottom: var(--hair);
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(20px) saturate(160%);
}
.brand {
  color: var(--ce-black);
  font-family: var(--serif);
  font-size: 18px;
  font-weight: 400;
  letter-spacing: -0.01em;
}
.brand::after { content: "."; color: var(--ce-red); }
.header-meta {
  display: flex;
  align-items: center;
  gap: var(--s-3);
  color: var(--ce-grey);
  font-family: var(--mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
}
.brand-tag { color: var(--ce-red); font-weight: 500; }

/* === FUNNEL === */
.funnel {
  width: min(820px, calc(100vw - (var(--gutter) * 2)));
  margin: var(--s-6) auto var(--s-5);
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 0;
}
.stage-card {
  display: grid;
  grid-template-columns: 48px 1fr;
  gap: var(--s-3);
  align-items: start;
  padding: var(--s-4) var(--s-5);
  border: 1px solid var(--ce-border);
  border-radius: var(--r-3);
  background: #fff;
}
.stage-card.stage-tight { padding: var(--s-3) var(--s-5); }
.stage-card.stage-narrow {
  background: var(--ce-bg);
  border-style: dashed;
}
.stage-num {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--ce-red);
  color: #fff;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 500;
}
.stage-body { min-width: 0; }
.stage-title-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--s-3);
}
.stage-title {
  color: var(--ce-black);
  font-family: var(--serif);
  font-size: 20px;
  font-weight: 400;
  letter-spacing: -0.015em;
}
.stage-sub {
  margin-top: 4px;
  color: var(--ce-grey);
  font-size: 13px;
}
.stage-sub a { color: var(--ce-red); }
.stage-sub a:hover { text-decoration: underline; }
.big-number {
  color: var(--ce-red);
  font-family: var(--mono);
  font-size: 28px;
  font-weight: 500;
  line-height: 1;
}
.arrow {
  align-self: center;
  color: var(--ce-light);
  font-size: 18px;
  line-height: 1;
  padding: 6px 0;
}

/* === BARS === */
.bars {
  margin-top: var(--s-3);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.funnel-bar {
  display: grid;
  grid-template-columns: minmax(140px, 1fr) minmax(120px, 3fr) auto;
  gap: var(--s-3);
  align-items: center;
  padding: 4px 0;
  cursor: pointer;
}
.funnel-bar:hover { color: var(--ce-red); }
.fb-label {
  color: var(--ce-ink);
  font-family: var(--sans);
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bars-scores .fb-label {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.fb-track {
  display: block;
  height: 8px;
  background: var(--ce-bg);
  border: 1px solid var(--ce-border);
  border-radius: var(--r-pill);
  overflow: hidden;
}
.fb-fill {
  display: block;
  height: 100%;
  background: var(--ce-red);
  border-radius: var(--r-pill);
}
.fb-count {
  color: var(--ce-red);
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 500;
  text-align: right;
  min-width: 32px;
}
.fb-fill-core { background: var(--ce-red); }
.fb-fill-adjacent { background: var(--ce-grey); }
.fb-fill-tangential { background: var(--ce-light); }
.fb-fill-off { background: var(--ce-border); }
.bucket-core .fb-count { color: var(--ce-red); }
.bucket-adjacent .fb-count { color: var(--ce-grey); }
.bucket-tangential .fb-count, .bucket-off .fb-count { color: var(--ce-light); }

/* === DRILLDOWNS === */
.drilldown {
  width: min(var(--container), calc(100vw - (var(--gutter) * 2)));
  margin: var(--s-6) auto var(--s-5);
}
.drill-section {
  margin: var(--s-5) 0 var(--s-3);
  color: var(--ce-red);
  font-family: var(--mono);
  font-size: var(--t-label);
  font-weight: 500;
  letter-spacing: var(--ls-label);
  text-transform: uppercase;
}
.drill-section.bucket-core { color: var(--ce-red); }
.drill-section.bucket-adjacent { color: var(--ce-grey); }
.drill-section.bucket-tangential, .drill-section.bucket-off { color: var(--ce-light); }

.drill-item {
  padding: var(--s-3) 0;
  border-bottom: var(--hair);
}
.drill-item > summary {
  cursor: pointer;
  list-style: none;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  gap: var(--s-3);
  align-items: baseline;
}
.drill-item > summary::-webkit-details-marker { display: none; }
.drill-item > summary::before {
  content: "▸";
  display: inline-block;
  color: var(--ce-light);
  font-size: 10px;
  width: 10px;
  transition: transform 0.15s;
}
.drill-item[open] > summary::before { transform: rotate(90deg); }
.drill-title {
  color: var(--ce-black);
  font-family: var(--serif);
  font-size: 16px;
  letter-spacing: -0.01em;
  line-height: 1.25;
  min-width: 0;
}
.row-meta {
  display: inline-flex;
  flex-wrap: wrap;
  gap: var(--s-2);
  align-items: center;
  font-size: 12px;
}
.score {
  color: var(--ce-red);
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 500;
  min-width: 36px;
  text-align: right;
}

.pill {
  border: 1px solid var(--ce-border);
  border-radius: var(--r-pill);
  padding: 1px 7px;
  color: var(--ce-grey);
  background: var(--ce-bg);
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}
.pill-pillar { color: var(--ce-red); }
.pill-fit-core { border-color: var(--ce-red); color: var(--ce-red); background: var(--ce-red-soft); }
.pill-fit-adjacent { color: var(--ce-grey); }
.pill-fit-tangential, .pill-fit-off-territory { color: var(--ce-light); }

.drill-body {
  margin-top: var(--s-3);
  padding-top: var(--s-3);
  padding-left: 20px;
  border-top: 1px dashed var(--ce-border);
}
.drill-body.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--s-5);
}
@media (max-width: 880px) {
  .drill-body.two-col { grid-template-columns: 1fr; }
}
.micro {
  margin: var(--s-3) 0 var(--s-2);
  color: var(--ce-light);
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}
.micro a { color: var(--ce-red); }
.dim { color: var(--ce-light); }
.mono { font-family: var(--mono); font-size: 12px; }
.bare-list { list-style: none; padding: 0; margin: 4px 0 0; }
.bare-list li {
  padding: 4px 0;
  color: var(--ce-ink);
  font-size: 13px;
  line-height: 1.5;
  border-bottom: 1px dotted var(--ce-border);
}
.bare-list li:last-child { border-bottom: none; }
.code {
  margin: 4px 0;
  padding: var(--s-3);
  border: 1px solid var(--ce-border);
  border-radius: var(--r-2);
  background: var(--ce-bg);
  color: var(--ce-ink);
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
  max-height: 280px;
  overflow-y: auto;
}

/* Digest rows */
.digest-row {
  display: grid;
  grid-template-columns: 36px 1fr auto;
  gap: var(--s-3);
  align-items: center;
  padding: var(--s-3) 0;
  border-bottom: var(--hair);
}
.digest-rank { color: var(--ce-light); font-size: 12px; }
.digest-body { min-width: 0; }
.bucket-block { margin-top: var(--s-4); }
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
