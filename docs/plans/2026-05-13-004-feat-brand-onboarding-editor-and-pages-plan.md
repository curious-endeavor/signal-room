---
status: active
created: 2026-05-13
type: feat
title: "Brand onboarding chatbot, structured brief editor, and dual-mode results pages"
plan_depth: standard
---

# Brand onboarding chatbot, structured brief editor, and dual-mode results pages

## Summary

Turn the existing `ce-signal-room-web` service into a multi-brand product. **Each brand has its own home page** that shows results in the same `result-row` format as today's `/runs/{id}` view — but with **two switchable modes**: brief-driven results (with lookback dropdown) or free-form search results. **New brands onboard via a Claude-powered chatbot** that takes a URL, crawls the brand's site, interviews the owner, and produces an initial brief. The brief is **editable via a structured form** (not raw YAML). **Auth is per-brand passcode** — no user accounts. **Ships to Render today** by extending the existing FastAPI + worker + Postgres stack.

---

## Problem Frame

We have the pieces:

- ✅ Planner-driven /last30days fetches with high-quality signal (today's afternoon work)
- ✅ GDELT fetcher merged in
- ✅ Tracer + funnel HTML render
- ✅ Brand-routed Render endpoints (`/alice`, `/ce`, refetch button, trace serving)

What's still missing for a **real product**:

1. Anyone with a URL can't bootstrap a brand themselves — Dan or a teammate has to hand-author `config/brands/<slug>/brief.yaml`.
2. The brief is a YAML file on disk — opaque to non-engineers, ephemeral on Render.
3. The brand page only shows the most recent run (no mode toggle, no lookback control, no free-form query option).
4. There's no auth — anyone could refetch anyone's brand.

This plan closes all four gaps in one push so the deployed product is something Dan can hand to Alice's team (or any third client) without further engineering involvement.

---

## Scope

**In scope today:**

- DB schema for brands, chat sessions, and chat messages
- Brand listing home page (`/`) with "Create new brand" CTA
- Onboarding flow: URL input → text crawl → Claude interview (polling chat) → brief generation
- Structured brief editor (form, passcode-gated)
- Brand page with **two modes** (brief / free-search), shared result-row layout
- Lookback dropdown (1/7/30) on brief mode
- Per-brand passcode auth (cookie-based, edit/refetch gated)
- Render deploy (after the private-repo reconnect)

**Out of scope (Deferred to Follow-Up Work):**

- Visual research (screenshots, color palette, typography sniff) — text-only crawl ships today; visual passes go to v2
- Full user accounts (email/password, sessions, password reset)
- Brand sharing / teams / permissions beyond a single passcode
- Brief versioning / undo history
- Cross-brand digest aggregation
- Auto-scheduled refetches (cron) — manual button only
- Karpathy-style feedback loop (still milestone 2+)

**Outside this product's identity:**

- Becoming a general-purpose competitor to Brandwatch / Meltwater — this is CE's tool with a small set of trusted brand tenants
- Becoming a CRM or content-management system — we surface signal, we don't manage it

---

## Key Technical Decisions

### D1. Per-brand passcode auth, no user accounts

**Decision:** Each brand row has a `passcode_hash` (bcrypt) and `passcode_changed_at`. View pages (`/{brand}`, `/{brand}/runs/{id}`, `/{brand}/runs/{id}/trace`) are **public**. Edit + refetch + onboarding actions require a valid `sr_passcode_<brand>` signed cookie. Cookie is set when the user enters the passcode on a gate page or immediately after creating a brand; it expires in 30 days.

**Rationale:**

- Cheapest auth that's not "nothing" — no users table, sessions, password reset, OAuth
- Matches the deploy posture (per-brand cookies = naturally scoped to that brand)
- Public view is what the team actually wants — easy to share a URL with a teammate / journalist without making them log in
- Easy to upgrade to real accounts later: passcode becomes "team admin password"; brand owners get individual logins added

**Tradeoff:** Anyone with the passcode has full control. No revocation per-user. Acceptable for two friendly brands; revisit at brand #5.

### D2. Brief is stored as JSON in Postgres; YAML/config files are generated runtime artifacts

**Decision:** Canonical brief lives in `brands.brief_json` as serialized JSON. On every save, and immediately before every brand refetch, the server writes a generated `config/brands/<slug>/brief.yaml` plus projected `discovery_queries.json`, `seed_sources.json`, and `pillar_keywords.json`.

**Rationale:**

- Render's filesystem is ephemeral — pure YAML-on-disk would lose data on restart
- The current pipeline still expects files, so DB → generated files is the smallest compatible bridge
- `signal_room/projector/from_brief.py` remains the projection contract, but it gets called from a helper rather than by a human
- Brand lookup must check DB first. Filesystem fallback remains only for legacy local brands that have `config/brands/<slug>/brief.yaml` but no `brands` row yet
- Worker must hydrate the brand config directory from DB before calling `process_brand_refetch`; otherwise a Render restart can leave a DB-backed brand with no files on disk

**Tradeoff:** There are still generated files on disk, but they are explicitly cache/build artifacts. Local YAML edits are not authoritative once a brand row exists.

### D3. Chat uses polling POST per turn (matches `danpeg.ai` pattern)

**Decision:** Onboarding chat fires `POST /api/brands/{brand}/onboarding/chat` per user message using the existing Anthropic HTTP pattern from `signal_room/planner.py`. Server:
1. Persists user message in `chat_messages` table
2. Loads full session history (DB-backed, not in-memory — survives Render restarts)
3. Calls Claude with full message history + system prompt
4. Persists assistant response, returns full JSON
5. Frontend appends + saves to localStorage as a UX nicety (backup, not authoritative)

**Rationale:**

- Same pattern Dan already validated on `danpeg.ai` (`POST /api/chat` per turn, no streaming)
- Works on Render free tier without WebSocket / SSE complexity
- 3-5s per turn is fine for an onboarding interview
- DB-backed session state survives Render redeploys

**Tradeoff:** No streaming "typing" effect. Show a typing indicator client-side to fake feel. Migrate to SSE later if it bothers anyone.

### D4. Onboarding crawl is text-only

**Decision:** Crawl flow: fetch homepage with `httpx`, parse with BeautifulSoup, follow 5-10 internal links (`/about`, `/pricing`, `/case-studies`, `/press`, `/blog`, etc.), strip HTML, concatenate into a `brand_context` blob. Cap at ~30K chars (Claude context limit headroom). Add `httpx` and `beautifulsoup4` to `pyproject.toml`; neither is currently declared. **No screenshots, no Playwright, no `/visual-research` skill** in v1.

**Rationale:**

- Most positioning signal is in TEXT — copy on the homepage, about page, customer stories
- Visual extraction is the long pole (Playwright binary, screenshot capture, color extraction, browser memory limits on Render starter plan)
- Crawl finishes in ~30s, leaves Claude with rich context, fits inside a single request lifecycle
- Visual research is in `Deferred to Follow-Up Work` — re-enable when we have a stronger reason

**Tradeoff:** Brands with thin homepages (early-stage startups with a single landing page) get less context. Mitigated by the chatbot interview filling gaps.

### D5. Two modes on the brand page; both reuse the existing `result-row` layout

**Decision:** `/{brand}` has a mode toggle (brief / search). Mode state in URL query (`?mode=brief` default, `?mode=search&q=...`). Both modes render results using the existing `signal_room/templates/partials/results_list.html` result-row markup after small parameterization for brand-specific form actions. **The trace HTML stays separate** (one trace per brief-mode run).

**Rationale:**

- One row partial, two backends → consistent UX, smaller code
- Existing `result-row` already has score, source, date, pillar, summary, CE angle — those exist on brand brief items too (they all go through the same scoring path)
- Free-search mode reuses the existing run/item worker path where possible, but must add brand/mode metadata so query reuse is scoped correctly

**Tradeoff:** Brief mode and search mode share the "Refetch" button label but mean different things (refetch brief = full pipeline; refetch search = re-fire one query). UI distinguishes via the input row above.

### D6. Onboarding interview uses a structured "interview script" prompt — not free-form chat

**Decision:** Claude's system prompt enumerates 5-6 specific questions it must answer before generating the brief:

1. Who is the brand's primary audience? (role, industry, persona)
2. What are the brand's 3-5 positioning pillars?
3. What's the brand's voice — formal / playful / technical / opinionated?
4. Who are 3-5 named competitors (and how does the brand differ from each)?
5. What kind of news / signal does the team most want to see surfaced?
6. What's been said about the brand publicly in the last 30 days that they care about?

Claude is told: "Don't lecture. Ask one question at a time. Acknowledge their answer. Move to the next question. After 5-6 turns, generate the brief and end the interview."

**Rationale:**

- Free-form chat with Claude often meanders. Explicit interview script bounds the interaction
- 5-6 turns → ~3-5 min total → fits the "reasonable time" constraint
- Generation step at the end is a single Claude call: take crawl context + interview transcript → output a structured brief JSON

**Tradeoff:** Less "natural" feeling than free-form chat. Acceptable for a one-shot onboarding flow where consistency > delight.

---

## System-Wide Impact

| Surface | Touched? | Notes |
|---|---|---|
| `signal_room/web.py` | Yes | New routes for home, onboarding, chat, editor, mode-toggle |
| `signal_room/worker.py` | Yes | Hydrate DB-backed brand config files before refetch; persist top scored items for result-row rendering |
| `signal_room/web_store.py` | Yes | New tables: `brands`, `chat_sessions`, `chat_messages`, `brand_run_items`; add brand/mode metadata to free-search runs or introduce equivalent scoped search-run helpers |
| `signal_room/templates/` | Yes | New: home, onboarding, chat, brief-editor, brand-results. Reuse partials/results_list.html |
| `signal_room/static/styles.css` | Yes | New: chat bubbles, structured form, mode toggle, passcode gate |
| `signal_room/planner.py` | No | Already works |
| `signal_room/render_trace.py` | No | Already works |
| `signal_room/onboarding.py` | New | Crawl + interview orchestration |
| `signal_room/auth.py` | New | Passcode hash + cookie helpers |
| `signal_room/projector/from_brief.py` | No (preserved) | DB → YAML/config helper calls this on save and before refetch |
| `pyproject.toml` | Yes | Declare `httpx`, `beautifulsoup4`, `bcrypt`, `itsdangerous`, `pydantic`, and `pyyaml` |
| `render.yaml` | Minor | New env: `SIGNAL_ROOM_PASSCODE_PEPPER`; verify `ANTHROPIC_API_KEY` exists on both services |
| Repo deploy access | External | Render GitHub App needs reconnect for private repo (separate from this plan) |

---

## Implementation Units

### U1. DB schema: brands + chat sessions

**Goal:** Persistent storage for brands, brief content, passcode, and chat history.

**Dependencies:** None.

**Files:**
- `signal_room/web_store.py` (modify — add tables/helpers to `_schema_sql`; keep SQL compatible with SQLite and Postgres)

**Approach:**

- `brands`: `slug` (pk, text, kebab-case), `name` (text), `url` (text — original homepage), `brief_json` (text, JSON serialized), `passcode_hash` (text, bcrypt), `passcode_changed_at`, `created_at`, `updated_at`, `last_refetched_at` (nullable).
- `chat_sessions`: `id` (uuid hex 12), `brand_slug`, `purpose` (text — "onboarding" v1, "edit" v2), `crawl_context` (text), `created_at`, `updated_at`, `closed_at` (nullable), `generated_brief_json` (text, set when interview produces a brief).
- `chat_messages`: `id` (uuid hex 12), `session_id` (fk), `role` (user / assistant / system), `content` (text), `created_at`. Indexed on `(session_id, created_at)`.
- `brand_run_items`: `run_id`, `item_id`, `rank`, `title`, `source`, `source_url`, `date`, `score`, `summary`, `suggested_ce_angle`, `pillar`, `follow_up_query`, `payload_json`; same decoded shape as `items`, but keyed to `brand_runs`.
- Add `lookback_days` to `brand_runs` so brief-mode refetches can support 1/7/30 day runs. For existing rows, default to 30.
- Add brand scoping for free-search runs. Either add nullable `brand` and `mode` columns to `runs`, or create `brand_search_runs`; choose the smaller implementation once the code is open. The important contract is that `/alice?mode=search&q=x` cannot accidentally reuse `/ce` or global search results.
- Helper methods: `create_brand`, `get_brand`, `list_brands`, `update_brand_brief`, `set_brand_passcode_hash`, `create_session`, `append_message`, `get_session_messages`, `close_session_with_brief`, `replace_brand_run_items`, `get_brand_run_items`, `find_active_brand_search_run`.

**Patterns to follow:** Existing `brand_runs`, `runs`, and `items` helpers. Keep the same "tiny store object with JSON-in-text columns" style; do not introduce a migration framework for this push.

**Test scenarios:**

- Create-then-read a brand; verify slug uniqueness (insert duplicate → IntegrityError surfaces clearly).
- Round-trip a brief_json through update + fetch; preserves nested fields.
- Set passcode, verify with same input → true; with different → false.
- Append 6 messages to a session, list them ordered by created_at ASC.
- `close_session_with_brief` flips closed_at + stores final brief, idempotent on retry.
- Persist 10 brand_run_items, read them back in rank order, and feed them through `_result_context`.
- Legacy filesystem-only brand (`config/brands/alice/brief.yaml` but no `brands` row) still works until migrated.

**Verification:** `\d brands` shows the schema in Postgres; smoke test creates one brand, attaches one session, appends 6 messages, closes session, reads everything back.

---

### U2. Passcode auth (cookie-based, per-brand)

**Goal:** Edit and refetch actions require the brand's passcode; view actions stay public.

**Dependencies:** U1.

**Files:**
- `signal_room/auth.py` (new — `hash_passcode`, `verify_passcode`, `set_cookie`, `require_passcode` dependency)
- `signal_room/web.py` (modify — apply `Depends(require_passcode)` to gated routes)
- `signal_room/templates/passcode_gate.html` (new — form: "Enter passcode for {brand}", on submit posts to `/{brand}/auth` which sets cookie + redirects to next)
- `pyproject.toml` (modify — add `bcrypt` and `itsdangerous`)

**Approach:**

- Hash with bcrypt + a server-side pepper (`SIGNAL_ROOM_PASSCODE_PEPPER` env var on Render). 12 rounds.
- Cookie: `sr_passcode_{slug}=<signed payload>`, signed via `itsdangerous`, 30-day max-age, `httponly`, `samesite=lax`, `secure` when `RENDER`/production is set.
- Signed payload shape: `{slug, passcode_changed_at}`. `require_passcode` validates signature, slug match, and that the payload's `passcode_changed_at` equals the current brand row. This makes old cookies invalid after a passcode reset without storing per-cookie tokens.
- `require_passcode` FastAPI dependency reads cookie and raises a 303 redirect to `/{brand}/auth?next={path}` when missing/invalid. Do not put the full absolute URL in `next`; store only a same-origin path to avoid open redirects.
- Gated routes: `POST /{brand}/refetch`, `POST /{brand}/brief`, `POST /{brand}/onboarding/start`, `POST /api/brands/{brand}/onboarding/chat`.
- Ungated: `GET /{brand}`, `GET /{brand}/runs/{id}`, `GET /{brand}/runs/{id}/trace`.

**Patterns to follow:** Existing `_allowed_brand` helper for 404. FastAPI Depends pattern for cleanliness.

**Test scenarios:**

- GET `/{brand}` without cookie → 200 (public).
- POST `/{brand}/refetch` without cookie → 302 to `/{brand}/auth?next=...`.
- POST `/{brand}/auth` with correct passcode → 302 to `next` + sets cookie.
- POST `/{brand}/auth` with wrong passcode → 200 re-renders with "wrong passcode" error.
- Cookie expiry path: forge an expired cookie → 302 to /auth.
- Cookie scoping: cookie for alice should NOT authorize ce actions.
- Passcode reset updates `passcode_changed_at`; an old signed cookie no longer authorizes gated actions.

**Verification:** End-to-end: visit `/alice/brief` without cookie → redirected to passcode gate → enter passcode → land back on editor → save works.

---

### U3. Onboarding crawler

**Goal:** Given a homepage URL, fetch the site's text content and return a brand-context blob for Claude.

**Dependencies:** None (parallel work).

**Files:**
- `signal_room/onboarding.py` (new — `crawl_brand(url) → str`)
- `tests/test_onboarding_crawler.py` (new)
- `pyproject.toml` (modify — add `httpx` and `beautifulsoup4`)

**Approach:**

- Use `httpx.AsyncClient` with a realistic browser User-Agent and per-request timeout.
- Fetch homepage. Parse with `bs4` (BeautifulSoup4).
- Discover internal links: same-host `<a href>` matching `/about|/pricing|/case-studies|/customers|/blog|/press|/team|/product|/features|/faq` heuristics. Cap at 10 unique paths.
- Fetch each in parallel (asyncio + httpx), strip script/style/nav/footer, extract main content. Concatenate into `brand_context` with section headers per URL.
- Hard cap: 30,000 chars (`brand_context[:30000]`). If a page errors, skip it and continue.
- 30s overall timeout via `asyncio.wait_for`. Always returns at least the homepage content on timeout.

**Test scenarios:**

- Happy path: mock httpx, return fixture HTML pages → crawler returns expected concatenated text.
- Homepage 404: returns empty-ish context but doesn't crash (informative error in returned text).
- Slow page: simulate 10s delay on one of 5 sub-pages → overall finishes before 30s with that page skipped.
- Hostile robots.txt / blocked / DDoS protection: catches and surfaces the failure.
- Non-HTML response (PDF, JSON): skip, don't crash.
- Foreign-language site: returns the raw text (Claude handles translation).

**Verification:** `python3 -c "import asyncio; from signal_room.onboarding import crawl_brand; print(asyncio.run(crawl_brand('https://alice.io'))[:2000])"` returns plausible brand text.

---

### U4. Onboarding chat (polling per turn, Claude-driven interview)

**Goal:** A Claude-powered interview that takes the crawl context + a few back-and-forth turns and emits a structured brief JSON.

**Dependencies:** U1, U2 (passcode-gated), U3 (crawler output feeds the system prompt).

**Files:**
- `signal_room/onboarding.py` (modify — `build_system_prompt(brand_context)`, `next_turn(session_id) → assistant_msg`, `finalize_brief(session_id) → brief_dict`)
- `signal_room/web.py` (modify — new routes `POST /{brand}/onboarding/start`, `POST /api/brands/{brand}/onboarding/chat`, `GET /{brand}/onboarding`)
- `signal_room/templates/onboarding.html` (new — chat UI with message list, input box, typing indicator, "Generate brief & finish" button when ready)
- `signal_room/static/chat.js` (new — polling pattern adapted from `danpeg.ai/chat.js`: send → typing indicator → render reply)
- `tests/test_onboarding_chat.py` (new)

**Approach:**

System prompt structure (Claude sonnet, temp 0.4 for the chat, temp 0.1 for the final brief generation):

```
You are a brand researcher conducting a 5-6 question interview to build a Signal Room brief for {brand_name} ({url}).

You already have this crawl context:
{brand_context (30K chars max)}

Your job:
1. Ask one focused question per turn.
2. Acknowledge the answer briefly, then ask the next.
3. After 5-6 questions, summarize what you've learned and OUTPUT a structured brief.

Questions to cover (in any order that flows naturally):
- Primary audience (role, industry, named personas if any)
- 3-5 positioning pillars (what the brand wants to be known for)
- Voice / tone (formal, playful, technical, opinionated, ...)
- 3-5 named competitors and the brand's specific differentiation
- Categories of news / signal the team most wants to see
- Sensitive areas / things they DON'T want surfaced

When you have enough to write a brief, respond with: `READY_TO_GENERATE` on its own line, then a one-paragraph summary, then nothing else.
```

Polling loop:
- Frontend sends user message → `POST /api/brands/{brand}/onboarding/chat` with `{session_id, message}`.
- Server appends to `chat_messages`, loads full session, calls Claude, returns `{assistant_msg, ready_to_generate: bool}`.
- The crawl context is stored on `chat_sessions.crawl_context` at session start and reused for every turn/finalize call. Do not depend on localStorage or in-memory state.
- When `ready_to_generate=true`, UI shows "Generate brief & finish" button.
- Click → `POST /{brand}/onboarding/finalize` → server calls Claude with a DIFFERENT system prompt (the brief-generation prompt — see U5's schema) → writes to `brands.brief_json`, generates the YAML/config files, and redirects to `/{brand}/brief` editor for review.

**Patterns to follow:** `danpeg.ai/server.js` polling pattern. `signal_room/planner.py` for Claude API client.

**Test scenarios:**

- New session start: crawl runs, system prompt built, first assistant turn arrives in <10s.
- 6-turn happy path: user answers 6 questions, Claude emits `READY_TO_GENERATE`, finalize endpoint produces a brief that validates against U5's schema.
- User skips a question ("don't know"): Claude moves on rather than getting stuck.
- Claude rate limit / 429: server retries with backoff (2 attempts), then surfaces error to user.
- Long answer (5000 chars): server stores it cleanly, Claude responds normally.
- Concurrent turn from same session (double-click): second request waits (DB row lock) or rejects (409); doesn't fork the conversation.
- Session resume after page reload: GET `/{brand}/onboarding` with existing session shows full message history.

**Verification:** Run onboarding for `https://alice.io` from a fresh DB → end up with a populated `brands.brief_json` containing 3-5 pillars and 5-10 discovery queries.

---

### U5. Structured brief editor

**Goal:** Form-based editor for the brand brief that hides YAML/JSON internals.

**Dependencies:** U1, U2.

**Files:**
- `signal_room/web.py` (modify — `GET /{brand}/brief`, `POST /{brand}/brief`)
- `signal_room/templates/brief_editor.html` (new — form with sectioned inputs)
- `signal_room/static/styles.css` (modify — form styles)
- `signal_room/brief_schema.py` (new — pydantic model for the existing `brief.yaml` shape: `brand.{name, slug, url, one_liner, audience_primary, audience_secondary, strategic_frame...}` plus `projection.signal_room.{pillars, discovery_queries, seed_sources}`. Each nested model validates required fields and length caps.)
- `tests/test_brief_editor.py` (new)

**Approach:**

- Form fields (visible to user):
  - **Brand name** (single line)
  - **One-liner** (textarea, max 300 chars)
  - **Audience** (textarea — each line is one audience description)
  - **Pillars** (5 repeating blocks, each: pillar name + textarea of keywords, one per line)
  - **Discovery queries** (10 repeating blocks, each: topic + why)
  - **Seed sources** (10 repeating URL + name + category + why)
- Hidden but preserved on save: `forbidden_phrases`, `anti_patterns`, `strategic_frame`, `brand` metadata. These come from the original brief; the form doesn't expose them, but they round-trip.
- Serializer writes the same nested shape that `signal_room/projector/from_brief.py` already consumes, not a flattened editor-only shape.
- "Add another" / "Remove" buttons per repeating section (vanilla JS, no framework).
- On submit: server validates via `BrandBrief` pydantic model. If invalid, re-renders form with field-level errors. If valid:
  1. Update `brands.brief_json` in DB
  2. Write `config/brands/<slug>/brief.yaml` (preserving hidden fields)
  3. Run projector to refresh `config/brands/<slug>/{discovery_queries,seed_sources,pillar_keywords}.json`
  4. Redirect to `/{brand}` with a "Brief saved" toast

**Patterns to follow:** Existing `signal_room/templates/index.html` form layout. Pydantic for validation is new but lightweight.

**Test scenarios:**

- Load editor for existing brand → all fields prefilled from `brands.brief_json`.
- Save with all required fields → DB updated, brief.yaml refreshed, projector ran cleanly.
- Save with missing required field (e.g., no pillars) → 200 re-renders with error message inline near the empty pillars section, original data preserved.
- Save without passcode cookie → 302 to passcode gate.
- Race: two saves at the same time → last write wins (acceptable for v1); a versioning unit is in Deferred.
- Hidden fields (strategic_frame, forbidden_phrases) survive a save-then-reload cycle.
- Brief generated by onboarding → loads into editor cleanly without re-prompting for any field.

**Verification:** Round-trip an Alice brief through the editor → re-projected configs match a baseline diff.

---

### U6. Brand page with dual-mode results (brief / search)

**Goal:** `/{brand}` shows results in the existing `result-row` format, with a mode toggle between "brief-driven" and "free-form query".

**Dependencies:** U1.

**Files:**
- `signal_room/web.py` (modify — `GET /{brand}` reshape, `POST /{brand}/search`, `GET /{brand}/runs/{id}` updates)
- `signal_room/templates/brand_results.html` (new — wraps the existing `results_list.html` partial with brand header + mode toggle + lookback selector)
- `signal_room/templates/partials/results_list.html` (small changes — parameterize form actions/search route and label text so it can serve global and brand pages)
- `signal_room/static/styles.css` (modify — mode toggle, lookback dropdown)

**Approach:**

URL routing:
- `GET /{brand}` → defaults to `?mode=brief` → shows the latest brand_run's persisted `brand_run_items`
- `GET /{brand}?mode=search` → shows free-form search input (and most recent search run for this brand if any)
- `GET /{brand}?mode=search&q=foo` → either finds an active run for that exact query OR shows "Submit to search"
- `POST /{brand}/search` → enqueues a search run (existing flow but scoped to brand context — brand brief feeds LLM scoring even in free-search mode)
- Mode toggle is a `<form>` with hidden `mode` value + submit button styled as tabs

Brief mode UI:
- Sticky header: brand name + Refetch button + lookback dropdown (1 / 7 / 30 days)
- Body: result-rows from the latest brand_run, sorted by score desc
- Changing lookback dropdown changes the form value; clicking Refetch enqueues a `brand_runs` row with that `lookback_days`

Search mode UI:
- Same brand header + lookback options + search box (existing search-form widget)
- Submit fires a search-shaped worker run scoped to `{brand, mode=search}`; result page renders with the same partial

Worker/persistence notes:
- After `run_pipeline`, read `summary["enriched_path"]`, take the top N scored rows, and persist them into `brand_run_items`.
- `latest_run.html` currently embeds digest HTML; replace that page with result-row rendering for brand pages, keeping digest/trace links as secondary actions.
- The existing global `items` table can still back `/runs/{id}`. Brand brief-mode rows use `brand_run_items`; brand search mode can use `items` only if `runs` gains a brand/mode column.

**Patterns to follow:** Existing `/runs/{run_id}` rendering. Keep `partials/results_list.html` as the single result-row renderer, but make its routes/labels configurable enough for brand pages.

**Test scenarios:**

- `/alice` with a done brand_run → shows top items.
- `/alice` with no runs → empty state ("No brief results yet · Refetch") and offer to switch to search mode.
- `/alice?mode=search` no query → search box only.
- `/alice?mode=search&q=GPT-5` → finds matching active run if exists, else shows "Submit to search".
- Switching from brief to search mode preserves the lookback selection if applicable.
- POST `/alice/search` without cookie → passcode gate.
- POST `/alice/search` with cookie → 303 redirect to `/alice/runs/<new_search_run_id>?mode=search`.

**Verification:** Visit `/alice` with a populated DB → see top brief-mode results → switch to search mode → type a free query → see results in same UI format.

---

### U7. Home page: brand list + create-new entry

**Goal:** `/` lists existing brands and provides the "Create new brand" entry point to the onboarding flow.

**Dependencies:** U1.

**Files:**
- `signal_room/web.py` (modify — `GET /` reshape; `POST /onboarding/start` redirects into the chat flow)
- `signal_room/templates/home.html` (new, or replace current `index.html` if the global search home is intentionally being retired)
- `signal_room/templates/onboarding_start.html` (new — single field: brand URL, click "Begin onboarding")
- `signal_room/static/styles.css` (modify — home page card grid)

**Approach:**

- `GET /` queries `list_brands()` → renders a card per brand with: name, URL, last-refetched timestamp, link to `/{brand}`. Adds a prominent "Create new brand" CTA.
- "Create new brand" → `GET /onboarding/start` → simple page with a single text input ("What's your brand's homepage URL?") and a Begin button.
- Route order matters: declare `/onboarding/start`, `/api/...`, `/runs/...`, `/search`, and other fixed routes before the catch-all `/{brand}` routes. Reserve slugs such as `api`, `runs`, `search`, `static`, `sample`, `healthz`, and `onboarding`.
- `POST /onboarding/start {url}` →
  1. Validate URL (must parse, http/https only)
  2. Derive slug (from URL hostname, lowercase, hyphen-separated, fallback to numeric suffix if collision)
  3. Create `brands` row with empty brief_json + placeholder name (later updated by Claude)
  4. Generate a passcode (10-12 chars, unambiguous letters + digits), hash it, store it, set the brand auth cookie, and render a **POST response** that displays the passcode once. Do not redirect to a reveal URL and do not store the plaintext passcode.
  5. Create onboarding `chat_session`
  6. Run the crawl inline with a 30s cap or in a background task that persists `crawl_context` before the first chat turn. Prefer inline today unless it makes Render time out.
  7. The reveal page links to `/{slug}/onboarding`; refresh shows the browser's normal form-resubmission warning instead of re-displaying stored plaintext.

**Patterns to follow:** Existing `index.html` layout. Brand-card style follows `result-row` visual idiom.

**Test scenarios:**

- `/` with no brands → "Create new brand" card only.
- `/` with two brands → two cards + CTA.
- POST `/onboarding/start` with valid URL → creates brand, returns passcode reveal page, and sets auth cookie.
- Refreshing the passcode reveal response does not depend on server-stored plaintext; browser may warn about resubmission. If resubmitted, slug collision creates a suffix and a new brand, so the UI should discourage refresh and make the next-step link prominent.
- Slug collision → suffixed slug (`alice` exists → `alice-2`).
- Invalid URL (no scheme) → form re-renders with error.

**Verification:** From a fresh DB, click through `/` → create new brand → see passcode → land on `/{slug}/onboarding` with crawler already firing.

---

### U8. Wire the onboarding chat + brief finalization end-to-end

**Goal:** Connect U3-U7 so a user can go from URL → onboarding chat → generated brief → editor → first refetch.

**Dependencies:** U3, U4, U5, U6, U7.

**Files:**
- `signal_room/onboarding.py` (modify — `finalize_brief(session_id)` returns a `BrandBrief`-shaped dict)
- `signal_room/web.py` (modify — `POST /{brand}/onboarding/finalize`)
- `tests/test_onboarding_finalize.py` (new)

**Approach:**

- Final Claude call takes the full chat transcript + persisted crawl context and outputs **JSON only** matching the nested `BrandBrief` schema consumed by the projector.
- System prompt:

  > "From the conversation above and the crawl context, produce a brand brief in this JSON shape. Use the user's own words where possible. Generate 5-8 discovery queries that would surface press-worthy signal for THIS brand specifically. Return ONLY JSON. No prose."

- Server validates with pydantic; if validation fails, retries once with the error message appended to the prompt.
- On success: writes `brands.brief_json`, writes generated config files, runs projector, marks session closed, redirects to `/{slug}/brief` editor (passcode-gated) for human review.

**Test scenarios:**

- Happy path: transcript with 6 turns → valid `BrandBrief` JSON → all derived configs written.
- Claude returns markdown-wrapped JSON → server unwraps and parses correctly.
- Claude returns invalid schema → retry once; if still invalid, surface "Generation failed, try the editor manually" with a stub brief prefilled.
- Session already closed → 409, can't re-finalize.

**Verification:** Run the entire flow for `https://alice.io` from scratch → end with a populated brief on `/alice/brief` that matches `config/brands/alice/brief.yaml` byte-for-byte.

---

### U9. Render deploy + secrets + GitHub reconnect

**Goal:** Ship to Render today.

**Dependencies:** U1-U8 landed and tested locally.

**Files:**
- `render.yaml` (modify)
- `scripts/render-build.sh` (no changes expected)

**Approach:**

1. **Reconnect GitHub App** for `ce-signal-room-web` and `ce-signal-room-worker` services (private-repo fallout — Dan does this in Render dashboard).
2. **Add new env vars** to both services:
   - `SIGNAL_ROOM_PASSCODE_PEPPER` (sync: false, generate via `openssl rand -hex 32`)
   - `ANTHROPIC_API_KEY` already added earlier
3. **Push to main** → Render auto-deploys.
4. **Manual smoke** on production:
   - `/` shows migrated existing brands plus "Create new brand" (or just the CTA on a fresh DB)
   - Click "Create new brand", enter `https://alice.io`
   - See passcode reveal
   - Onboarding chat runs (5-6 turns)
   - Finalize → brief saved → editor shows form
   - Edit one pillar, save → /alice mode=brief shows latest run after refetch

**Test scenarios:**

- Test expectation: none (deploy operation). Smoke checklist above is the verification.

**Verification:** Production URL `https://ce-signal-room-web.onrender.com/` renders the new home page. Full onboarding-to-refetch flow works against the live deployment.

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Render deploy still broken (private-repo) blocks shipping | High | U9 starts with the reconnect. If reconnect fails: temp flip to public, deploy, flip back. |
| New dependencies fail deploy because `pyproject.toml` omitted them | Med | Add and test declared dependencies locally before deploy: `httpx`, `beautifulsoup4`, `bcrypt`, `itsdangerous`, `pydantic`, `pyyaml`. |
| DB-backed brand disappears from filesystem after Render restart | Med | Treat generated config files as cache only; hydrate from `brands.brief_json` on save and immediately before every brand refetch. |
| Onboarding crawl times out on slow sites | Med | 30s wall clock + per-page timeouts + skip-and-continue. Returns partial context rather than failing. |
| Claude generates an invalid brief JSON | Med | Validate with pydantic; retry once with error feedback; fall back to stub + editor. |
| Passcode reveal refresh/resubmit creates confusion | Low | Reveal only in the initial POST response, set the auth cookie immediately, and make the next-step link prominent. Do not store plaintext passcodes. |
| Two simultaneous brief saves clobber each other | Low | Last-write-wins for v1; brief versioning in Deferred. |
| User abandons onboarding mid-flow | Med | Session persists in DB; resume on return. After 7 days idle → soft-archive. |
| ANTHROPIC_API_KEY exhausted | Low | Tier-1 limits are fine for the volume here. Surface 429s clearly. |
| Trace blobs grow the DB unbounded | Med (from prior plan) | `SIGNAL_ROOM_BRAND_RUN_KEEP` retention already in place; add same for `chat_sessions` (keep 50 most recent). |

---

## Verification Strategy

End-to-end on production:

1. Fresh Render deploy from `main`.
2. Anonymous visitor goes to `/` → "Create new brand" → enters `https://alice.io`.
3. Sees passcode `ABC123XY`, copies it.
4. Onboarding chat runs: ~6 turns over ~3-5 min. Claude produces brief.
5. Lands on `/alice/brief`. If the auth cookie from onboarding is present, the editor opens directly; otherwise the passcode gate appears. Sees structured editor pre-filled.
6. Edits one field (e.g., adds a pillar keyword). Saves.
7. Returns to `/alice`. Sees "No runs yet". Clicks Refetch.
8. Run completes in 3-5 min. Top 10 items show as result-rows.
9. Toggles to `search` mode. Types "AI security funding". Submits. Sees free-form query results.
10. Trace page (`/alice/runs/<id>/trace`) loads with the funnel + GDELT branch.

---

## Out of Scope / Deferred to Follow-Up Work

- Visual research (screenshots, color extraction, typography) — text-only crawl ships today
- Full user accounts with email + password / magic link
- Brief versioning / undo
- Multi-user collaboration on a single brand
- Auto-scheduling cron refetches
- Brand sharing across deploys / multi-environment promotion
- Diffing two runs against each other
- Karpathy-style feedback loop (still milestone 2+)
- Real-time chat streaming (SSE) — polling ships today
- Cross-brand digest aggregation
- Public brand discovery / leaderboard

---

## Open Questions

1. **Passcode lost / forgot flow** — v1 has no recovery. If you lose the passcode, you contact Dan, who runs a CLI tool to reset it. Acceptable for two-brand deploy, but worth noting.

2. **Migrating existing filesystem brands into `brands` rows** — `config/brands/alice/brief.yaml` and `config/brands/ce/brief.yaml` both exist today. The plan should either include a startup/bootstrap helper that imports those YAML files into Postgres when missing, or keep filesystem fallback for reads until each brand is edited/saved through the UI. Prefer the bootstrap helper so `/alice` and `/ce` immediately appear on `/`.

3. **Per-source weighting for the free-search mode** — the existing search flow doesn't know about the brand's `source_weights`. Should the brand-scoped search apply them? Currently the plan says no (free search = vanilla last30days search). Worth a follow-up if Dan wants brand context to shape both modes consistently.

---

## Origin & References

- This `/ce-plan` invocation (today)
- `docs/plans/2026-05-13-003-feat-alice-ce-render-deployments-plan.md` — the deploy plan whose foundation this builds on
- `~/Projects/archive/danpeg-chat/server.js` — chat-polling pattern reference
- `~/Projects/archive/danpeguine-site/chat.js` — frontend pattern reference
- `signal_room/web.py` — existing FastAPI routes
- `signal_room/web_store.py` — existing Postgres schema and ORM-ish patterns
- `signal_room/planner.py` — Claude API client pattern to reuse for onboarding and brief generation
- `https://ce-signal-room-web.onrender.com/runs/9bdb56049a3a` — UI format reference (currently unreachable due to Render deploy issue but shape known from `signal_room/templates/partials/results_list.html`)
