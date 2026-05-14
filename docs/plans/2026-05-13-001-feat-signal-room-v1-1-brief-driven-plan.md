r--
status: active
created: 2026-05-13
type: feat
title: "Signal Room v1.1 — brief-driven, auto-generated queries, GDELT-augmented"
plan_depth: standard
time_box: 90 min (12:15–13:45)
---

# Signal Room v1.1 — brief-driven, auto-generated queries, GDELT-augmented

## Summary

Make Signal Room v1.1 produce **high-quality, deduped, press-worthy signal** for any brand from a single URL input, with **zero manually-entered queries**. Build on top of Assaf's three existing backend atoms (already on `feat/brief-driven-pipeline`): the **projector** (`signal_room/projector/from_brief.py`), config-driven **pillar keywords**, and the **`--brief` LLM-scoring flag**. Add the two missing pieces — a **brand-audit step** that turns a URL into `brief.yaml`, and a **GDELT-15min news fetcher** — wire them end-to-end, validate against Alice, save the output as a committed artifact, and push to `main`.

Karpathy-style feedback loop is explicitly deferred (milestone 2). UI is irrelevant today — the data flow is what counts.

---

## Problem Frame

Yesterday's session burned an hour on a wrong assumption (Assaf's work was on a "separate UI-only repo"). It is **not** — Assaf's three commits sit on this branch, doing real backend work. v1.0 currently requires a hand-authored `brief.yaml` and only fetches from `/last30days`. That fails the v1.1 bar:

- **Onboarding bar**: a new client should not need to learn the brief schema.
- **Signal-volume bar**: `/last30days` (social) alone misses press/news surface that GDELT covers cheaply.
- **Generalizability bar**: if Alice works, any client URL should work.

v1.1 closes those three gaps without touching the projector / scoring / digest internals that already work.

---

## Scope

**In scope today:**

- URL → `brief.yaml` brand-audit step
- GDELT 15-min API as a second fetcher
- End-to-end run on Alice
- One committed evidence artifact (sample digest output) under `data/v1_1_evidence/`
- Merge `traction-first-ranking` into the branch if clean (signal quality lift)
- Push to `main`

**Out of scope (carry forward as-is):**

- Karpathy-style feedback loop — milestone 2 (Dan deferred explicitly)
- UI changes — `signal_room/web.py` / `templates/` / `static/` untouched
- Multi-tenant pillar refactor — projector keeps CE-only PILLAR_KEYWORDS fallback per its own SPEC note
- Polished brand-audit (visual analysis, full voice/positioning rigor) — staging's Brand Audit owns that; today's local step is "enough brief to drive the pipeline"

### Deferred to Follow-Up Work

- Wrapping the local brand-audit step as a callable to staging's deployed Brand Audit service (if/when its endpoint stabilises)
- Brief-quality regression tests (golden-brief diffing) — needs more than one brand on file
- Source-feedback weight tuning post-Alice run

---

## Key Technical Decisions

### D1. Brand-audit step is local Claude, not a staging API call

**Decision:** Implement `signal_room/brand_audit.py` as a local Python module that fetches a URL, extracts cleaned text, and asks Claude to emit a `brief.yaml` in the exact schema `projector/from_brief.py` expects (the `projection` block with `signal_room.{discovery_queries, pillars, seed_sources}`).

**Rationale:** The staging Brand Audit service's endpoint is not documented on `/internal/map.html`, and discovering it would burn the time budget. The projector already defines the contract — we just need *anything* that emits to that shape. Self-contained, no auth, no network dependency on staging, easy to swap for the staging service later.

### D2. GDELT 15-min API as a parallel fetcher, not a replacement

**Decision:** Add `signal_room/fetchers/gdelt.py` next to `last30days.py`. CLI exposes `--fetch gdelt` (and a combined `--fetch last30days,gdelt` shape, or two separate `--fetch` flags chained) writing into the same `discovered_items.json` shape the pipeline already consumes.

**Rationale:** GDELT is press/news-shaped; `/last30days` is social-shaped. They are complementary. Keeping them as peer fetchers means the existing pipeline (dedup, scoring, digest) absorbs both with zero downstream changes. The projector's `discovery_queries` flow drives both: each query goes to each backend.

### D3. Evidence artifact is committed, not gitignored

**Decision:** Save one Alice run's outputs (the `brief.yaml`, the projected configs, the enriched items JSON, the rendered digest HTML) into `data/v1_1_evidence/2026-05-13-alice/` and commit them. Update `.gitignore` only if needed to whitelist that subdir.

**Rationale:** "Verifiable artifact" is in the task brief. Without a committed sample, "it works for Alice" is unprovable across machines. One sample per milestone is cheap; we'll prune if it bloats.

### D4. Auto-generated queries live in the brief, not in code

**Decision:** The Claude prompt in `brand_audit.py` must produce 4–6 `discovery_queries` directly into the brief — diverse across the brand's comms-strategy areas it identified. The projector already turns those into per-brand `discovery_queries.json`.

**Rationale:** This is the central v1.1 promise ("users don't manually enter queries"). Putting query authorship inside the brand-audit step means the *brief itself* is the source of truth for what the pipeline searches. No second config to maintain.

### D5. Traction-ranking merge is optional, not blocking

**Decision:** Attempt to merge `traction-first-ranking` (3 commits ahead of `main`, social-traction sort) into `feat/brief-driven-pipeline` after the brand-audit and GDELT units land. If it conflicts non-trivially, skip and defer.

**Rationale:** Signal quality lift, but not on the v1.1 critical path. Conflicts with scoring changes are plausible; not worth burning 20 min on.

---

## System-Wide Impact

| Surface | Touched? | Notes |
|---|---|---|
| `signal_room/projector/` | No | Already correct; we feed it. |
| `signal_room/scoring.py`, `llm_scoring.py` | No | `--brief` path already works. |
| `signal_room/fetchers/` | Yes | New `gdelt.py` peer to `last30days.py`. |
| `signal_room/cli.py` | Yes | Add `--fetch gdelt` choice and `audit` subcommand. |
| `signal_room/brand_audit.py` | New | URL → brief.yaml step. |
| `signal_room/web.py`, templates, static | No | Out of scope. |
| `config/brands/` | Yes | Gains `alice/` directory written by projector. |
| `data/v1_1_evidence/` | New | Committed sample output. |
| Branches | Yes | Possibly merge `traction-first-ranking`; push branch to `main` at end. |

---

## Implementation Units

### U1. `brand_audit.py` — URL → brief.yaml

**Goal:** Given a brand URL, produce a `brief.yaml` that `projector/from_brief.py` can consume to project per-brand configs (discovery_queries, seed_sources, pillar_keywords).

**Dependencies:** None.

**Files:**

- `signal_room/brand_audit.py` (new)
- `signal_room/cli.py` (add `audit` subcommand)
- `tests/test_brand_audit.py` (new)

**Approach:**

- Fetch the brand URL (use `requests` or `httpx`; library already in `pyproject.toml` if `last30days.py` uses one — mirror that).
- Strip HTML to plain text (BeautifulSoup or a minimal regex strip — match whatever scoring/title cleaning already uses).
- Single Claude call (model from `--llm-model`, default `claude-sonnet-4-6`) with a prompt that:
  - Identifies 3–5 **core comms-strategy areas** from the brand text
  - For each area, emits 1–2 **discovery queries** with `id`, `priority`, `topic`, `why`
  - Identifies 3–6 **pillars** (P1..Pn) with id, name, and 4–8 keywords
  - Suggests 3–8 **seed_sources** (URLs the brand's audience reads)
- Wrap the model output in the `{ projection: { signal_room: {...} } }` envelope the projector expects.
- Write to a target path (default `config/brands/<slug>/brief.yaml`).

**Patterns to follow:**

- `signal_room/llm_scoring.py` for Claude API client setup, auth, error handling
- `signal_room/projector/from_brief.py` for the exact brief schema shape (it is the consumer — its expectations are the contract)

**Test scenarios:**

- Happy path: pass a fixture HTML file (no network) → produces a brief with ≥3 discovery queries, ≥3 pillars, ≥3 seed_sources, all in the projector's expected schema.
- Schema round-trip: the brief produced by `brand_audit` parses cleanly through `projector/from_brief.py --dry-run` with no schema errors.
- Slug derivation: passing `https://alice.com/` and `https://www.alice.com/about` both resolve to slug `alice`.
- LLM failure: mock Claude returning malformed JSON — surfaces a clear error, does not write a half-finished file.
- Empty page: URL returns near-empty body — error message says "insufficient text to audit", does not call Claude.

**Verification:** `python3 -m signal_room.cli audit --url https://alice.com/ --out config/brands/alice/brief.yaml` produces a file; `python3 -m signal_room.projector.from_brief --brief config/brands/alice/brief.yaml --dry-run` prints valid `discovery_queries.json`, `seed_sources.json`, `pillar_keywords.json`.

---

### U2. `fetchers/gdelt.py` — GDELT 15-min news fetcher

**Goal:** Add a second fetcher that runs the same `discovery_queries` against the free 15-min GDELT Doc API and writes results into the pipeline's `discovered_items.json` shape.

**Dependencies:** U1 is not strictly required (you can build U2 against any existing brief), but the Alice E2E run (U4) depends on both.

**Files:**

- `signal_room/fetchers/gdelt.py` (new)
- `signal_room/cli.py` (extend `--fetch` choices to include `gdelt`; allow combining or chaining backends)
- `signal_room/pipeline.py` (audit: confirm pipeline reads `discovered_items.json` regardless of which fetcher wrote it; no logic change expected)
- `tests/test_fetchers_gdelt.py` (new)

**Approach:**

- GDELT Doc API endpoint: `https://api.gdeltproject.org/api/v2/doc/doc` with `query`, `mode=ArtList`, `format=json`, `timespan=15min` (or `1d` for backfill).
- For each query in `config/discovery_queries.json`, hit GDELT, parse the `articles[]` array (each has `url`, `title`, `seendate`, `domain`, `language`, `socialimage`).
- Normalise into the same item shape `last30days.py` produces (look at its output to mirror fields exactly — `id`, `url`, `title`, `source`, `discovered_at`, `query`, etc.).
- Dedupe by URL within the GDELT batch before merging.
- Append (don't overwrite) into `data/discovered_items.json` if `last30days` already ran in the same session, OR write to a peer file (e.g. `data/gdelt_items.json`) and have the pipeline read both — whichever matches the pattern `last30days.py` already established for the discovery layer (check before deciding).

**Patterns to follow:**

- `signal_room/fetchers/last30days.py` end to end — query iteration, error handling, output format, mock mode (CLI exposes `--fetch-mock`)

**Test scenarios:**

- Happy path: mock GDELT response with 5 articles → 5 normalised items written, fields match `last30days` shape.
- Dedupe: GDELT returns the same URL twice across two queries → one item in output, `query` field reflects the first match (or merged — match `last30days` semantics).
- Rate limit / 4xx: GDELT returns 429 → fetcher backs off (single retry then surfaces error), does not corrupt the output file.
- Empty result: query returns zero articles → fetcher writes an empty list cleanly, exit code 0.
- Mock mode: `--fetch-mock` returns deterministic fixtures, no network call.

**Verification:** `python3 -m signal_room.cli fetch --backend gdelt --mock --emit json` returns ≥1 item in the expected shape. Live (non-mock) call against a known-good query (e.g. "AI marketing") returns ≥3 real items.

---

### U3. CLI glue — `audit` + multi-fetcher run

**Goal:** Make the end-to-end flow runnable as one or two commands.

**Dependencies:** U1, U2.

**Files:**

- `signal_room/cli.py`

**Approach:**

- Add `audit` subcommand: `signal-room audit --url <URL> --out <path>` → invokes `brand_audit.audit_url(...)`.
- Extend `run --fetch` to accept `gdelt` and (stretch) comma-separated combinations like `--fetch last30days,gdelt` that runs both fetchers before the pipeline. If parsing comma-lists is messy, support `--fetch` as a repeatable flag instead — match argparse idioms already used elsewhere in `cli.py`.
- Ensure `--brief PATH` already-existing flag chains naturally after `audit`: the brief that `audit` writes is what `run --brief` reads.

**Patterns to follow:**

- Existing subparser shape in `cli.py` (`run`, `fetch`, `feedback`, `queries`, `items`, `item`, `feedback-log`, `lab`)
- Existing `--fetch` `choices=` pattern for the new value

**Test scenarios:**

- `audit --url ... --out ...` writes a file and exits 0.
- `run --fetch gdelt --fixtures-only false --brief config/brands/alice/brief.yaml` runs the full pipeline using GDELT + LLM scoring with the projected Alice configs.
- Argument validation: missing `--url` for `audit` produces clear error.
- Test expectation: smoke tests only (one test per new subcommand); no need for exhaustive CLI permutations — the underlying modules carry that coverage.

**Verification:** `signal-room --help` lists `audit`. `signal-room audit --help` shows `--url`/`--out`. `signal-room run --help` lists `gdelt` under `--fetch`.

---

### U4. End-to-end Alice run + committed evidence

**Goal:** Prove v1.1 works on Alice and leave a committed artifact future-Dan can point at.

**Dependencies:** U1, U2, U3.

**Files:**

- `data/v1_1_evidence/2026-05-13-alice/brief.yaml` (output of `audit`)
- `data/v1_1_evidence/2026-05-13-alice/discovery_queries.json` (projector output)
- `data/v1_1_evidence/2026-05-13-alice/seed_sources.json` (projector output)
- `data/v1_1_evidence/2026-05-13-alice/pillar_keywords.json` (projector output)
- `data/v1_1_evidence/2026-05-13-alice/enriched_items.json` (pipeline output, top 10–20)
- `data/v1_1_evidence/2026-05-13-alice/digest.html` (rendered digest)
- `data/v1_1_evidence/2026-05-13-alice/README.md` (one-paragraph: what this is, how to reproduce, date, what Alice's URL was)
- `.gitignore` (audit; whitelist `data/v1_1_evidence/` if `data/` is currently broadly ignored)

**Approach:**

1. Run `signal-room audit --url <Alice URL> --out data/v1_1_evidence/2026-05-13-alice/brief.yaml`.
2. Run `python3 -m signal_room.projector.from_brief --brief <that path> --out data/v1_1_evidence/2026-05-13-alice/`.
3. Copy/symlink projected configs into `config/` (or point `signal-room run` at the brand dir — match whatever projector convention exists).
4. Run `signal-room run --fetch gdelt --brief <brief path> --emit text` and let the pipeline produce the digest.
5. Eyeball the top 10 items: are they press-worthy? Deduped? On-topic for Alice's comms strategy?
6. Write the README.md with a one-paragraph reproduction note.

**Patterns to follow:** Existing digest render path in `signal_room/digest.py`.

**Test scenarios:**

- Subjective quality gate (manual, not automated): ≥6 of top 10 items would plausibly be useful to an Alice comms lead. If <4, surface the gap rather than ship false confidence.
- Reproducibility: re-running the exact same command 5 min later yields a similar (not identical, since GDELT/last30days move) digest with similar topic coverage.
- Test expectation: none (manual verification step); the underlying modules carry automated coverage.

**Verification:** All 7 files exist in `data/v1_1_evidence/2026-05-13-alice/`, digest.html opens in a browser without errors, top 10 items are inspected and the README captures the take.

---

### U5. Optional — merge `traction-first-ranking`, then push to main

**Goal:** Land v1.1 on `main`.

**Dependencies:** U1–U4 complete and green.

**Files:** none new.

**Approach:**

1. `git merge --no-ff traction-first-ranking` into `feat/brief-driven-pipeline`. If conflicts are small (scoring sort logic), resolve. If large or touching `--brief` / projector territory, **abort and skip** — defer to a separate PR.
2. Re-run the U4 Alice E2E once after merge to confirm nothing regressed.
3. `git merge feat/brief-driven-pipeline` into `main` (or open a PR if branch-protected — check `gh pr create` is the path).
4. Push.

**Patterns to follow:** Existing release flow (check `README.md` and recent `main` commits).

**Test scenarios:**

- Test expectation: none (release operation); the merge re-runs U4 as its own quality gate.

**Verification:** `main` contains all four Assaf commits + U1–U4 (+ optionally U5 traction merge). `git log main --oneline` shows the expected tip.

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| GDELT API key / rate limits surprise us mid-flow | Low (free tier, no key) | Mock mode lands first; live call is verification only. |
| Claude returns brief that projector rejects | Med | U1 test scenario #2 explicitly round-trips; iterate prompt until clean. |
| Alice's homepage is too thin for a good brief | Med | Brand-audit step accepts multiple URLs (stretch) or Dan supplies an "about" page; if still thin, hand-edit the brief once — don't let it block U2/U3/U4. |
| `traction-first-ranking` merge conflicts non-trivially | Med | D5: skip it, defer to follow-up. Not blocking. |
| Time overrun past 13:45 | Med-High | Strict cut: U1+U2+U3 must land by 13:15 or we ship without U2 (last30days-only) and call it v1.1-partial. |

---

## Verification Strategy

End-to-end: one Alice command produces a press-worthy, deduped, top-10 digest with auto-generated queries, no hand-written config, drawing from both `/last30days` and GDELT. Evidence committed to repo. Branch on `main`.

Per-unit verification lives in each unit's section above.

---

## Out of Scope / Deferred

- **Karpathy-style feedback loop** (milestone 2). Dan explicitly deferred.
- **UI** — not touching `signal_room/web.py` or templates today.
- **Calling staging's Brand Audit service** instead of local Claude — once its endpoint is documented, swap is trivial (D1 keeps the seam clean).
- **Multi-tenant pillar refactor** — projector keeps CE-only fallback per its own SPEC note; Alice will get its own projected `pillar_keywords.json`, scoring will use that.
- **Polished Visual Analysis** for brand-audit — staging owns that, milestone 2+.

---

## Origin & References

- Task brief: today's `/ce-plan` invocation (Slot 1, 12:15–13:45)
- Existing infra: `signal_room/projector/from_brief.py` (Assaf c18187a), `signal_room/scoring.py` config loading (Assaf c9e3690), `signal_room/cli.py --brief` flag (Assaf c7cb68b)
- Staging map: `staging.curiousendeavor.com/internal/map.html` — confirms 6-component product surface (Brand Audit, Listen Daily, Draft Moves, etc.). Used for context only; today's work is local.
- Goal doc: `curious-endeavor-signal-room-goal.md` (v1 spec — pillars, surfs, scoring rubric)
- PRD: `curious-endeavor-signal-room-prd.md`
