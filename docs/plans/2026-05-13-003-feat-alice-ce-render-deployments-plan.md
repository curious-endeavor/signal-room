---
status: active
created: 2026-05-13
type: feat
title: "Alice + CE on Render with latest-run trace, refetch, and GDELT combined"
plan_depth: standard
---

# Alice + CE on Render with latest-run trace, refetch, and GDELT combined

## Summary

Two brand surfaces (Alice and CE) live on Render. Each one's landing page shows the **most recent pipeline run's digest** with a link to the **trace HTML** for that run. A **"Refetch" button** triggers a new run via the existing worker queue. Each run fetches from **both /last30days and GDELT**, and the trace funnel renders both sources side by side.

Today we already have on Render: a FastAPI web service (`ce-signal-room-web`), a polling worker (`ce-signal-room-worker`), and a Postgres DB (`ce-signal-room-db`). This plan **extends** that — no new platform, no new vendors.

---

## Problem Frame

The diagnostic work today proved the pipeline can produce press-worthy signal for both brands. But everything's local: you can run it on your laptop, see traces in browser, but a teammate (or future-Dan from a different machine) can't. We need:

1. **Persistent latest run per brand** — not just whatever happened locally
2. **One-click refetch** — no SSH, no CLI
3. **GDELT-augmented runs** — so trace funnel shows both press (GDELT) and social (/last30days) lenses
4. **Trace artifacts survive** restarts — Render's filesystem is ephemeral

---

## Scope

**In scope:**

- Brand-aware routing on the existing FastAPI app (Alice surface + CE surface)
- Per-brand "latest run" landing showing digest + trace link
- Refetch endpoint that enqueues a worker job
- Worker's `process_run` extended to fire **both** `/last30days` and GDELT (using the existing `--fetch both` plumbing)
- Trace artifacts (`.jsonl` + `.html`) persisted somewhere durable (Postgres blob or Render Persistent Disk)
- `render.yaml` updates as needed

**Out of scope (Deferred to Follow-Up Work):**

- Auth / login (public URL with obscure paths is fine today; auth comes later)
- Multi-brand admin UI (we hand-edit briefs)
- Auto-refresh / scheduling (refetch is manual)
- Karpathy-style feedback loop (still milestone 2+)

**Outside this product's identity:**

- General-purpose signal-room SaaS for arbitrary clients (this is a CE internal tool with Alice as the second tenant)

---

## Key Technical Decisions

### D1. One web service with brand routing, not two parallel services

**Decision:** Keep one `ce-signal-room-web` service. Routes become `/{brand}/...` (e.g., `/alice`, `/ce`). One worker, brand-aware job records.

**Rationale:**
- Render starter plan is $7/mo per service. Two web + two worker = $28/mo + DB. One of each = $14/mo + DB. Margin matters for an internal tool.
- The brand is just a slug on every config path and run record. The code already supports `--brand-config-dir` and `--data-suffix`. We're amplifying an existing seam.
- Future clients are just more rows in the DB, not more Render services.

**Tradeoff accepted:** One brand's heavy refetch can briefly tie up the shared worker. Acceptable for two brands with mostly-asynchronous use. If contention bites, split workers per brand.

### D2. Trace artifacts persisted as DB blobs, not files

**Decision:** Store `trace.jsonl` (compressed text) and `trace.html` (rendered) as columns/rows in Postgres alongside the run record. Don't rely on `/tmp/signal-room-runs` (ephemeral on Render).

**Rationale:**
- Today's `tracer.flush()` writes `.jsonl` to `data/traces/<brand>-<ts>.jsonl`. On Render that goes to `/tmp/...` and **vanishes on restart/redeploy**. We've already had trace files disappear once today.
- DB blob = single source of truth. Run record points to its own trace. Latest-run query is one row lookup.
- Storage cost: a typical trace is ~300-500K JSONL + ~400K HTML. At ~1MB per run × ~5 runs retained per brand × 2 brands = 10MB total. Well inside the 256MB DB.

**Alternative considered:** Render Persistent Disk ($0.25/GB/mo) — pinned to one service, breaks horizontal scaling, more ops. DB blobs win.

### D3. GDELT runs in parallel with /last30days, surfaced as a peer stage in the funnel

**Decision:** The worker's `process_run` now calls the pipeline with `fetch_backend="both"` (already wired in cli.py post-merge). Trace funnel renders **two parallel cards in Stage 2** — one for /last30days, one for GDELT — instead of stacking them.

**Rationale:**
- They're complementary surfaces (social/long-tail vs press/news). Funnel should reflect that visually.
- The existing tracer doesn't know about GDELT stages today. We add new stage names (`gdelt_started`, `pillar_fired`, `pillar_items_returned`, `gdelt_complete`) and a parallel branch in `_funnel_html`.
- Items from both backends already merge in `discovery_store.write_merged_discovered_items` (URL-deduped). Downstream scoring/digest is unchanged.

### D4. Refetch is fire-and-forget with status polling

**Decision:** "Refetch" button POSTs to `/{brand}/refetch`. Server writes a new run record (status=`queued`) and returns the run ID. UI polls `/{brand}/runs/{id}` for status (`queued`/`running`/`done`/`failed`). Worker picks up via the existing poll loop.

**Rationale:** Existing pattern — `signal_room/worker.py:run_forever(poll_seconds=5)` already does this for search runs. Reuse, don't reinvent.

**Tradeoff:** No real-time push (WebSockets) — UI polls every 5s. That's fine for runs that take 3-5 minutes.

### D5. Plans are generated on-demand in the worker, not pre-staged

**Decision:** When the worker picks up a refetch job, before firing /last30days it calls `signal_room.planner.plan_query` per discovery_query (4–8 calls, 2-3s each via Claude). Plans for that run get stored as JSON in the run record. Vendor invocation gets `--plan <inline-or-tmpfile>`.

**Rationale:**
- Today's `config/plans/<qid>.json` flow works locally but doesn't fit "fresh brief change → next refetch picks it up." Brief-aware planning needs to happen at run time.
- ~30s overhead per refetch (8 queries × ~3s) is a rounding error compared to the 3-5 min total run time.
- Stored plans-per-run let the trace funnel show the *exact* plan that fired (not just whatever's currently on disk).

---

## System-Wide Impact

| Surface | Touched? | Notes |
|---|---|---|
| `signal_room/web.py` | Yes | Add brand-routed endpoints |
| `signal_room/worker.py` | Yes | New job type `refetch`; per-brand processing |
| `signal_room/web_store.py` | Yes | New columns for brand, trace blobs, plan JSONs |
| `signal_room/tracer.py` | Yes (small) | Add GDELT stage names |
| `signal_room/render_trace.py` | Yes (small) | Parallel-stage rendering for /last30days + GDELT |
| `signal_room/fetchers/gdelt.py` | Yes (small) | tracer.record() calls |
| `signal_room/pipeline.py` | No | Already supports `fetch_backend="both"` post-merge |
| `signal_room/planner.py` | No | Already callable |
| `signal_room/templates/` | Yes | New `latest_run.html` template |
| `signal_room/static/styles.css` | Yes (additive) | "Refetch" button + status states |
| `render.yaml` | Minor | One new env var (`SIGNAL_ROOM_BRANDS=alice,ce`) |

---

## Implementation Units

### U1. DB schema: per-brand runs + trace blobs + plans

**Goal:** Persist all the per-brand run state Postgres needs to answer "latest run for X" and "trace for run Y."

**Dependencies:** None.

**Files:**
- `signal_room/web_store.py` (modify)
- `signal_room/migrations/` (new, or inline `CREATE TABLE IF NOT EXISTS` on startup)

**Approach:**
- Add table `brand_runs` with columns: `id` (uuid), `brand` (text), `status` (text — queued/running/done/failed), `created_at`, `started_at`, `finished_at`, `error` (text nullable), `summary_json` (jsonb — counts + paths), `trace_jsonl` (text/bytea, gzip), `trace_html` (text/bytea, gzip), `plans_json` (jsonb — plan-per-query keyed by qid), `digest_html` (text).
- Index on `(brand, created_at DESC)` for the "latest" query.
- Reuse existing `SignalRoomStore` connection plumbing.

**Test scenarios:**
- Happy path: insert a run, fetch latest by brand, fetch by id.
- Two brands: latest-for-alice doesn't return CE's run.
- Status transitions: queued → running → done preserves timestamps.
- Trace blobs: roundtrip a 1MB jsonl through compress/decompress.
- Test expectation: integration tests against a temp Postgres (or sqlite for local).

**Verification:** `psql -c "\d brand_runs"` shows the schema; one fixture INSERT + SELECT works.

---

### U2. Brand-aware worker job: `refetch`

**Goal:** Worker recognises a new job type and runs the full pipeline (plan → fetch both → score → digest → trace) for one brand.

**Dependencies:** U1.

**Files:**
- `signal_room/worker.py` (modify)
- `signal_room/planner.py` (no change — already callable)
- `signal_room/pipeline.py` (no change — already supports `fetch_backend="both"`)

**Approach:**
- Add `process_refetch(store, run)` next to `process_run`. It:
  1. Updates run status to `running`.
  2. Loads `config/brands/<brand>/brief.yaml`.
  3. Generates plans via `planner.plan_query` for each discovery_query; writes them to `/tmp/sr-plans-<run_id>/<qid>.json`; stores the plans in `brand_runs.plans_json`.
  4. Sets `tracer.enable(brand, run_dir=...)`.
  5. Calls `run_pipeline(brief_path=…, brand_config_dir=…, data_suffix=brand, fetch_backend="both", …)`.
  6. After run finishes, reads `tracer.records`, gzips → stores in `trace_jsonl`. Renders HTML via `render_trace_html`, gzips → stores in `trace_html`. Stores digest HTML in `digest_html`.
  7. Updates status to `done` (or `failed` with error).
- Worker's main loop dispatches by job type.

**Patterns to follow:** `worker.py:process_run` end to end. Same logging, same error handling, same poll cadence.

**Test scenarios:**
- Mock-mode end-to-end: enqueue a refetch job for alice with `mock=True`, worker picks it up, run record reaches `done`, all blobs populated.
- Brief missing: enqueue for unknown brand → run goes to `failed` with clear error.
- Planner failure: mock planner to throw → run goes to `failed`, error captured.
- Concurrent refetch: enqueue two refetches for different brands → both complete independently.
- Pipeline crash mid-run: worker still updates status to `failed` (no zombie `running` rows).

**Verification:** From `python3 -m signal_room.worker_smoke alice --mock`, end up with one `done` row in `brand_runs` for alice with all four blobs populated.

---

### U3. Web endpoints: brand-routed latest run + refetch button

**Goal:** Each brand has its own landing URL. UI shows the latest run; a button refetches.

**Dependencies:** U1, U2.

**Files:**
- `signal_room/web.py` (modify — new endpoints)
- `signal_room/templates/latest_run.html` (new)
- `signal_room/templates/base.html` (modify — header navigation between brands)
- `signal_room/static/styles.css` (modify — refetch button states)

**Approach:**
- Routes:
  - `GET /{brand}` — latest run page (renders `latest_run.html`)
  - `POST /{brand}/refetch` — enqueues a `refetch` job, redirects to `/{brand}/runs/{id}`
  - `GET /{brand}/runs/{run_id}` — specific run page (handles queued/running with auto-refresh meta tag, done shows trace link, failed shows error)
  - `GET /{brand}/runs/{run_id}/trace` — serves the stored `trace_html` blob inflated (Content-Type: text/html)
  - `GET /{brand}/runs/{run_id}/trace.jsonl` — serves inflated jsonl for tooling
  - `GET /api/brands/{brand}/runs/latest` — JSON metadata
- Brand validator: only allow brands present in `config/brands/<slug>/brief.yaml`.

**Patterns to follow:** Existing `/runs/{run_id}` endpoint pattern; existing template-extends-base pattern.

**Test scenarios:**
- `GET /alice` with no runs → shows "No runs yet · click Refetch" empty state.
- `GET /alice` with one done run → shows digest + trace link + refetch button.
- `POST /alice/refetch` → returns 302 to `/alice/runs/<new_id>` and a queued row exists.
- `GET /alice/runs/<id>` queued → status banner + auto-refresh.
- `GET /alice/runs/<id>` done → trace iframe or link works.
- `GET /alice/runs/<id>/trace` returns inflated HTML matching the stored blob.
- `GET /zzz` (unknown brand) → 404.

**Verification:** Local uvicorn: `/alice` and `/ce` both render; refetch button POST returns a redirect; tail Postgres rows.

---

### U4. GDELT instrumentation in the tracer

**Goal:** Trace funnel renders /last30days and GDELT as **parallel branches** in Stage 2, each with their own per-source breakdown.

**Dependencies:** None (parallel work to U1-U3).

**Files:**
- `signal_room/fetchers/gdelt.py` (modify)
- `signal_room/tracer.py` (no schema change; just new stage names)
- `signal_room/render_trace.py` (modify — Stage 2 renders two cards side by side when both backends present)

**Approach:**
- Add `tracer.record()` calls in `gdelt.py` at: `gdelt_started` (pillars list), per-pillar `pillar_fired` and `pillar_items_returned`, `gdelt_complete`.
- In `render_trace.py:_funnel_html`, detect whether `gdelt_started` is present. If yes, render Stage 2 as a two-column grid: left = "/last30days" card, right = "GDELT" card. Each has its own bars.
- Aggregate counts: total raw items = last30days items + GDELT items. Already correct since `write_merged_discovered_items` is the sink.

**Test scenarios:**
- Trace with only /last30days → looks identical to today (single card).
- Trace with only GDELT → single GDELT card (mirror layout).
- Trace with both → two cards side-by-side, totals add correctly.
- Mobile/narrow viewport → cards stack vertically below 880px.

**Verification:** Generate a synthetic trace with both backends, render, eyeball.

---

### U5. render.yaml + secret reconciliation

**Goal:** Deploy on Render with both brands accessible.

**Dependencies:** U1-U3 landed.

**Files:**
- `render.yaml`
- `.gitignore` (no changes expected)

**Approach:**
- Add `SIGNAL_ROOM_BRANDS=alice,ce` env var to both services (web + worker).
- Confirm `ANTHROPIC_API_KEY` is in Render's secret manager (needed for planner + LLM scoring). It isn't today — add to `envVars` with `sync: false`.
- Bump the DB plan if needed (currently `basic-256mb` — should be fine for 10MB of trace blobs but watch growth).
- Push to main → Render auto-deploys.

**Patterns to follow:** Existing `render.yaml` structure.

**Test scenarios:**
- Deploy succeeds, `/healthz` returns OK.
- `/alice` renders.
- Manual refetch from `/alice` runs to completion within ~5 min.

**Verification:** Render dashboard shows both services `live`; production URLs render the latest-run page.

---

### U6. Latest-run page UX polish

**Goal:** The default view a teammate (or you in a meeting) sees is actually useful.

**Dependencies:** U3.

**Files:**
- `signal_room/templates/latest_run.html`
- `signal_room/static/styles.css`

**Approach:**
- Header: brand name + "Last refetch: 2h ago · 163 items · 4 core" with the "Refetch" button right-aligned.
- Body: render the digest's top 10 items as the same `.result-row` pattern as the existing search UI (for familiarity).
- Footer: "View trace" link (opens `/alice/runs/<id>/trace` in new tab), "Brief: `config/brands/alice/brief.yaml`" link (read-only view for now).
- Status banner above the body when refetch is in flight: "Refetch running · ~3 min · started 12s ago" with a tiny spinner.

**Test scenarios:**
- Test expectation: visual smoke + accessibility (keyboard navigation to Refetch button, alt text on spinner).

**Verification:** Open `/alice` in browser, eyeball against Signal Room search page for visual coherence.

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Refetch ties up the shared worker, blocking the other brand | Med | Per-job timeouts; worker picks up next job after current done. If real contention: split workers per brand (cheap, deferred to follow-up). |
| Render's free starter plan times out on long requests | Low | Refetch is fire-and-forget; the long work is in the worker, not the HTTP request. |
| GDELT API rate limits | Low | Already a free 15-min API; runs at most every few minutes per brand. |
| Trace blobs grow the DB unbounded | Med | Keep only N most recent runs per brand (default N=10); migration adds a retention sweep. |
| Anthropic API costs from per-refetch planning | Low-Med | ~$0.05 per refetch (8 queries × ~$0.005). Capped by manual refetch cadence. |
| Plans differ run-to-run for the same brief | Low | Planner uses `temperature=0.2`, so plans are stable. Worth noting if it becomes an issue. |

---

## Verification Strategy

End-to-end: deploy to Render. Open `/alice` in a browser. See the latest run's digest. Click Refetch. Watch status flip queued → running → done. Open the trace link in a new tab — see the full funnel with both /last30days and GDELT side-by-side and our QueryPlan visible per query. Switch to `/ce`, do the same. Take a screenshot. Send the URL to a teammate. They see the same thing.

---

## Out of Scope / Deferred to Follow-Up Work

- **Auth / login** — obscure URLs for now
- **Public brand picker UI** at `/` — hardcoded routes per brand for now
- **Per-brand workers** — single shared worker until contention bites
- **Auto-scheduling** of refetches (cron) — manual button only
- **Trace download** as `.html` file — `/trace` already serves it; "save as" works
- **Diffing two runs** — interesting but later
- **Karpathy-style feedback loop** — milestone 2+

---

## Open Questions / Call-outs

These are forks that materially change the build; flag yes/no before I start.

1. **Brand routing shape** — `/alice` and `/ce` as the canonical URLs, OR `/?brand=alice`? Plan above assumes path segments. If you'd rather have a dropdown at `/`, U3 reshapes.

2. **Brief editing on the deployed site** — read-only link to the YAML in U6, OR an actual textarea + save button + re-plan trigger? Plan above is read-only. Editable is ~1 more unit (U7) and a security consideration.

3. **Auth** — confirm obscure-URL is fine for now? Or do you want even basic HTTP Basic Auth (one line in FastAPI) before public deploy?

4. **CE brief.yaml** — was wiped at some point today and only `config/brands/ce/brief.yaml` (the new shell) exists. Before we deploy CE, the CE brief needs to be re-authored. Quick (~10 min) but should know it's pending.

5. **GDELT plans?** — our planner currently only emits /last30days-shaped plans. Does GDELT need its own equivalent (brand-aware pillar selection), or does the existing config-driven pillar config work fine? Plan above assumes the latter. If you want brand-aware GDELT pillars, that's milestone 1.5.

---

## Origin & References

- Today's `/ce-plan` invocation
- `render.yaml` — existing 2-service + DB deployment
- `signal_room/web.py`, `signal_room/worker.py` — existing FastAPI + polling worker pattern
- `signal_room/web_store.py` — existing Postgres ORM
- Today's commits on `feat/brief-driven-pipeline` (tracer, planner, --plan integration, GDELT fetcher) — the substrate this plan builds on
- `docs/plans/2026-05-13-001-feat-signal-room-v1-1-brief-driven-plan.md` — earlier brainstorm direction (scratched mid-session; superseded by today's actual implementation)
- `docs/plans/2026-05-13-002-feat-gdelt-fetcher-plan.md` — GDELT fetcher work that's now merged in
