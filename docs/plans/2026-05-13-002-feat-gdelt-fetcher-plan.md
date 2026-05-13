---
title: "feat: Add GDELT fetcher to signal room"
status: active
type: feat
created: 2026-05-13
origin: docs/plans/2026-05-13-integrate-gdelt-source.md
---

# feat: Add GDELT fetcher to signal room

**Origin brief:** [docs/plans/2026-05-13-integrate-gdelt-source.md](2026-05-13-integrate-gdelt-source.md) — read it for live-test results, the full GDELT JSON envelope, the §6 gotchas catalog, and the §11 smoke test.

---

## Summary

Wire the new `gdelt-pp-cli` (built via Printing Press, lives at `~/printing-press/library/gdelt/`) into the signal-room digest pipeline as a second discovery backend alongside `/last30days`. Same subprocess pattern, different surface: GDELT covers worldwide news media in 65+ languages with a 3-month rolling window; `/last30days` covers social + grounding. Their outputs merge into `data/discovered_items.json` through one shared URL-level merge path, preserving first-seen state and stamping each row with `meta.source` so downstream scoring can weight per source.

The brief's §3 contract, §6 gotchas, and §11 smoke test are treated as authoritative; this plan turns them into ordered implementation units.

---

## Problem Frame

`/last30days` is currently the only live-discovery backend feeding the signal room. It does not see global news media — only social platforms + grounding search. The Alice brief's pillars (chatbot failures, AI security tooling, AI regulation) routinely surface in mainstream news first, and we have a working, agent-native CLI for GDELT DOC 2.0 sitting unused at `~/printing-press/library/gdelt/`. Wiring it in is the smallest meaningful expansion of signal coverage.

**In scope.** A new pillar-based fetcher module that mirrors `signal_room/fetchers/last30days.py`, required wire-up in the digest pipeline and CLI (`pipeline.py`, `cli.py`), a shared discovery merge helper, a backend config, a pillar bootstrap script driven by `config/brands/alice/brief.yaml`, fixture + live tests, and a `make build-gdelt` path so the binary is reproducible on Render.

**Explicitly separate.** `worker.py` and `query_lab.py` are currently ad hoc query flows, not scheduled/pillar discovery flows. They should only be wired in this pass if the implementation adds a free-form GDELT `today` helper; otherwise leave them unchanged and record that web UI/query-lab GDELT support is follow-up work. Do not force pillar semantics into those paths.

**Out of scope.** Replacing `/last30days`. Rewriting the CLI in Python. GDELT surfaces other than DOC 2.0 (GEO/TV/Events/BigQuery). The deferred CLI subcommands in origin §10 (`since`, `sync`, `brief`, `heat`, `search`).

---

## Requirements (carried from origin)

Origin acceptance criteria (§8) map to U-IDs below:

- AC1: `signal_room/fetchers/gdelt.py` mirrors `last30days.py` shape with fixture-based unit test → **U2, U7**
- AC2: `config/gdelt_backend.json` exists; fetcher reads from it → **U1, U2**
- AC3: `scripts/bootstrap_gdelt_pillars_from_alice.py` creates the 10 pillars from `config/brands/alice/brief.yaml` → **U5**
- AC4: `uv run signal-room fetch --backend gdelt --pillars all --timespan 1d` runs end-to-end and writes `meta.source="gdelt"` rows to `data/discovered_items.json` → **U3, U4**
- AC5: Merging with `/last30days` dedups by URL → **U4**
- AC6: §7 vendoring decision implemented → **U6** (decision: option **b** — Go-build in deploy image; see Key Technical Decisions)
- AC7: All six §6 gotchas handled or noted → **U2** (covers gotchas 1–5; gotcha 6 "no write side" is informational, no code needed)
- AC8: README points to the brief and explains how to add a pillar → **U8**

---

## Key Technical Decisions

1. **Subprocess, not library.** Call `gdelt-pp-cli` as a subprocess exactly the way `last30days.py` shells out — no Python rewrite, no MCP. Rationale: pattern parity, zero new abstraction, and the CLI already enforces rate limiting and adaptive backoff. (Origin §2, §4.)

2. **Pillars are the unit of work, not free-form queries.** The fetcher loops over named pillars from `gdelt-pp-cli pillar list` and calls `pillar pull <name>`. This keeps boolean queries versioned in `~/.config/gdelt-pp-cli/pillars.json` (overridable via `$GDELT_PILLARS_PATH`) instead of duplicating them in repo config. Bootstrap from `config/brands/alice/brief.yaml` is a one-shot/idempotent script (U5). (Origin §3, §4 Step 4.)

3. **Stderr lines starting with `rate limited, waiting Ns` are informational, filtered before checking `returncode`.** Treat empty `results.articles` as a successful zero-hit pull, not a failure (origin §6 gotchas 1, 2). The fetcher logs a warning when articles=0 with `results.query` from the pillar response so we can spot silently-empty short-acronym OR queries (gotcha 2).

4. **URL is the canonical dedup key across backends.** A shared discovery merge helper keys on normalized URL; when GDELT and `/last30days` both surface the same article, produce one row whose `meta.source` becomes a sorted list (`["gdelt", "last30days"]`) and whose other fields prefer the richer source (GDELT for `language`/`sourcecountry`/`domain`; `/last30days` for engagement/comments). (Origin §4 Step 3 wrap-up.)

5. **`first_seen_at` is stable across re-fetches.** GDELT's `seendate` is when *GDELT* indexed the article, not its publish date (origin §6 gotcha 5). Stamp `first_seen_at = now()` only when a URL first enters `discovered_items.json`; on later collisions preserve the existing `first_seen_at` and merge sources/metadata. This gives us "new since last check" semantics without needing the deferred `gdelt-pp-cli since` subcommand.

6. **Binary distribution: build via Go in the deploy image (origin §7 option b).** Vendor a source snapshot at `vendor/gdelt-pp-cli-src/`; add `make build-gdelt` that produces `bin/gdelt-pp-cli`. `config/gdelt_backend.json#binary_path` resolves in this order: `$GDELT_PP_CLI` env override → `bin/gdelt-pp-cli` (repo-relative) → `~/printing-press/library/gdelt/gdelt-pp-cli` (local dev fallback). Render's build adds a Go ≥1.26.3 step. Keeps repo lean and reproducible.

7. **One CLI surface, two backends.** Extend `--backend`/`--fetch` choices in `signal_room/cli.py` from `["last30days"]` to `["last30days", "gdelt", "both"]`. `both` runs them sequentially through the shared merge helper so rate limits, errors, and persistence stay deterministic. Same flag surface in `signal_room run` and `signal_room fetch`; do not introduce a second positional syntax like `signal-room fetch gdelt`.

---

## Output Structure

```
bin/
  gdelt-pp-cli                       # (gitignored) produced by `make build-gdelt`
config/
  gdelt_backend.json                 # NEW — binary path, timespan/max defaults, timeout
fixtures/
  gdelt_sample.json                  # NEW — captured pillar-pull JSON for unit tests
scripts/
  bootstrap_gdelt_pillars_from_alice.py  # NEW — reads brief.yaml, idempotent pillar add
signal_room/
  discovery_store.py                # NEW — merge/write discovered payloads by normalized URL
  fetchers/
    gdelt.py                         # NEW — mirrors last30days.py shape
tests/
  test_fetchers_gdelt.py             # NEW — fixture + error-path unit tests
  test_discovery_store.py            # NEW — URL dedup + first_seen preservation
vendor/
  gdelt-pp-cli-src/                  # NEW — source snapshot (pinned)
Makefile                             # NEW/MODIFIED — add build-gdelt target
.gitignore                           # MODIFIED — ignore bin/gdelt-pp-cli
README.md                            # MODIFIED — link brief, "how to add a pillar"
```

The per-unit `**Files:**` sections are authoritative.

---

## Implementation Units

### U1. Backend config + binary resolution

**Goal:** Land `config/gdelt_backend.json` and a `_resolve_binary()` helper that respects `$GDELT_PP_CLI`, then `bin/gdelt-pp-cli`, then the local-dev `~/printing-press/...` symlink target. No business logic yet.

**Requirements:** AC2.
**Dependencies:** none.
**Files:**
- `config/gdelt_backend.json` (new)
- `signal_room/fetchers/gdelt.py` (new — only the config-load + binary-resolve helpers in this unit)
- `tests/test_fetchers_gdelt.py` (new — only the binary-resolve cases in this unit)

**Approach:** Mirror `_load_backend_config()` and `_resolve_*` helpers in `last30days.py:393`+. Config shape per origin §4 Step 1:

```json
{
  "binary_path": null,
  "pillars_path": "~/.config/gdelt-pp-cli/pillars.json",
  "default_timespan": "1d",
  "default_max": 75,
  "rate_limit_rps": 0.2,
  "timeout_seconds": 60
}
```

`binary_path: null` means "use the resolver chain". An explicit string overrides resolution.

**Patterns to follow:** `signal_room/fetchers/last30days.py` — `_load_backend_config` (line 393), `_resolve_python_command` (line 371). Same env-override-first → config → defaults order.

**Test scenarios:**
- `_resolve_binary` returns `$GDELT_PP_CLI` value when env var is set and path exists.
- Returns `bin/gdelt-pp-cli` (repo-relative resolved against `ROOT`) when env unset and file exists.
- Falls back to `~/printing-press/library/gdelt/gdelt-pp-cli` when neither prior candidate exists.
- Raises `GdeltError` with a clear message when no candidate resolves.
- `_load_backend_config` returns `{}` when config file missing (matches last30days behavior).

**Verification:** Unit tests above pass. `python3 -c "from signal_room.fetchers.gdelt import _resolve_binary; print(_resolve_binary())"` prints a real path on Dan's machine.

---

### U2. Core `fetch_gdelt()` — subprocess, parse, normalize

**Goal:** Implement `fetch_gdelt()` end-to-end against a single pillar: spawn the CLI, parse the JSON envelope, normalize each article into a `discovered_items.json` row, handle the three gotcha classes (rate-limit stderr noise, empty results, exit-code mapping).

**Requirements:** AC1, AC7.
**Dependencies:** U1.
**Files:**
- `signal_room/fetchers/gdelt.py` (extend)
- `fixtures/gdelt_sample.json` (new — capture from a real `gdelt-pp-cli pillar pull chatbot-failures --timespan 7d --max 8 --json` and commit)
- `tests/test_fetchers_gdelt.py` (extend)

**Approach:**

Public surface (mirrors `fetch_last30days` shape):

```
class GdeltError(RuntimeError): pass

def fetch_gdelt(
    pillars: list[str] | None = None,   # None → fetch all from `pillar list`
    timespan: str = "1d",
    max_records: int = 75,
    mock: bool = False,
    run_root: Path | None = None,
    output_path: Path | None = None,    # direct CLI can pass DISCOVERED_ITEMS_PATH; "both" must pass None
    continue_on_error: bool = True,
    parallelism: int = 1,               # GDELT rate limit makes >1 mostly pointless; honor anyway
) -> dict
```

Per-pillar flow:
1. Build command: `[binary, "pillar", "pull", name, "--timespan", timespan, "--max", str(max_records), "--json"]`. Pass `GDELT_PILLARS_PATH` through env when set.
2. `subprocess.run(..., capture_output=True, text=True, timeout=cfg["timeout_seconds"])`.
3. Filter stderr: drop any line matching `^rate limited, waiting \d+s` before deciding the call failed. The remaining stderr (if any) is the real error message.
4. Map exit codes per origin §3: `0` → parse stdout; `3` → pillar not found (warn + skip); `4` → network (raise unless `continue_on_error`); `7` → rate-limited after 3 retries (raise); `10` → server (raise unless `continue_on_error`). Other non-zero → raise.
5. Parse `stdout` as JSON; pull `results.articles`. **Empty array is a valid result** (gotcha 1, 2) — log a warning, return zero rows, do not raise.
6. Normalize each article via `_normalize_article(article, pillar)`.
7. Return a payload shaped like `fetch_last30days()` (`backend`, `item_count`, `items`, `errors`, `runs`). If `output_path` is supplied, write only this backend's payload; callers that combine backends must pass `output_path=None` and persist through `signal_room/discovery_store.py`.

Normalized row shape (parity with `last30days._normalize_entry`):

```
{
  "id": "gdelt:<sha1(url)>",
  "title": article["title"][:280],
  "source": article["domain"],          # bare domain; ranker can prettify
  "source_url": article["url"],
  "date": _parse_seendate(article["seendate"]),  # YYYYMMDDTHHMMSSZ → ISO date
  "summary": "",                        # GDELT has no abstract
  "content": "",
  "engagement": {},
  "metadata": {
    "language": article.get("language"),
    "sourcecountry": article.get("sourcecountry"),
    "domain": article.get("domain"),
    "copies": article.get("copies", 1),
    "also_in": article.get("also_in", []),
    "socialimage": article.get("socialimage"),
  },
  "discovery_method": "gdelt",
  "candidate_source": True,
  "first_seen_at": datetime.now(timezone.utc).isoformat(),  # overwritten by merge helper on existing URLs
  "tags": [f"pillar:{pillar}", "platform:news"],
  "meta": {"source": "gdelt"},          # the cross-backend marker
}
```

`mock=True` reads `fixtures/gdelt_sample.json` instead of spawning.

**Execution note:** Test-first for `_normalize_article` and the stderr filter — both are pure functions and easy to drive from the fixture.

**Patterns to follow:** `signal_room/fetchers/last30days.py` — overall function shape (`fetch_last30days`, line 70), per-query manifest (line 257), normalized output (`_normalize_entry`, line 494), stable ID via `_stable_id` (line 594), timeout handling (line 240).

**Test scenarios:**
- Covers AC1. Fixture parse: load `fixtures/gdelt_sample.json`, run `_normalize_article` over every entry, assert `id`, `source_url`, `metadata.language`, `metadata.sourcecountry`, `meta.source == "gdelt"`, and `discovery_method == "gdelt"` all present.
- `seendate` parsing: `"20260513T120000Z"` normalizes to ISO date `"2026-05-13"`; malformed value falls back to today (matches last30days `_entry_date` behavior).
- Stderr filter: input `"rate limited, waiting 5s (attempt 1/3, rate adjusted to 0.18 req/s)\nreal error\n"` → filter retains only `"real error"`.
- Empty `results.articles` returns `[]` and the run summary records a warning, does **not** raise (gotchas 1, 2).
- Exit code 3 (pillar not found) with `continue_on_error=True` → empty rows for that pillar, run continues. With `continue_on_error=False` → `GdeltError` raised.
- Exit code 4 (network) always raises `GdeltError` when there is only one pillar; with multiple pillars and `continue_on_error=True`, that pillar is skipped and the rest run.
- Timeout (subprocess.TimeoutExpired) → `GdeltError` with `"timed out after N seconds"` in message; manifest still written.
- `mock=True` reads the fixture without invoking subprocess (mock `subprocess.run` to fail the test if called).
- `first_seen_at` is stamped on every new normalized row at normalize time (assert ISO-8601 with timezone); merge tests in U4 cover preserving the original stamp on URL collisions.
- Stable ID: same URL produces same ID across runs.

**Verification:** Unit tests pass. Live one-shot: `python3 -c "from signal_room.fetchers.gdelt import fetch_gdelt; print(len(fetch_gdelt(pillars=['chatbot-failures'], timespan='7d', max_records=5, output_path=None)['items']))"` exits 0 and prints a count; non-zero is expected for this known pillar, but zero should be investigated as data/query drift before treating it as a code failure.

---

### U3. CLI subcommand wiring

**Goal:** Expose `signal-room fetch --backend gdelt` and extend `signal-room run --fetch` to accept `gdelt` and `both`. Map flag names so they read naturally for both backends.

**Requirements:** AC4.
**Dependencies:** U2.
**Files:**
- `signal_room/cli.py` (modify)
- `tests/test_fetchers_gdelt.py` (extend with CLI argparse smoke tests)

**Approach:**
- `run` subparser: change `--fetch` choices to `["last30days", "gdelt", "both"]`. Add `--fetch-pillars` (CSV or `all`, default `all`), `--fetch-timespan` (default `1d`), `--fetch-max` (default 75). Existing `--fetch-lookback-days` and `--fetch-sources` stay last30days-only and are ignored when backend is `gdelt`.
- `fetch` subparser: change `--backend` choices similarly. Add `--pillars`, `--timespan`, `--max`.
- Dispatch in command handler: `if args.backend in ("gdelt", "both"): fetch_gdelt(...)`. For `both`, run sequentially with both fetchers called using `output_path=None`, then use `discovery_store.write_merged_discovered_items(...)` to write one deduped payload. This avoids whichever backend runs second clobbering the first backend's `discovered_items.json`.
- Keep the existing `signal-room fetch --backend ...` shape. Do not add or document `signal-room fetch gdelt` unless an explicit positional alias is implemented and tested.

**Patterns to follow:** `signal_room/cli.py:29` (run subparser flags), `signal_room/cli.py:50` (fetch subparser), `signal_room/cli.py:152` (dispatch).

**Test scenarios:**
- `signal-room fetch --backend gdelt --pillars chatbot-failures --timespan 1d --max 3` parses successfully and reaches `fetch_gdelt` with the right kwargs (assert via mock).
- `signal-room fetch --backend gdelt` with no `--pillars` defaults to `pillars=None` (fetch all).
- `signal-room run --fetch both` triggers both backends (assert both are called via mocks).
- `signal-room fetch gdelt` remains an argparse error unless a deliberate alias is added.
- Unknown `--backend foo` → argparse error, exit 2.
- `--fetch-sources` is silently ignored when `--fetch gdelt` (not an error — last30days-specific flag).

**Verification:** `uv run signal-room fetch --backend gdelt --pillars chatbot-failures --timespan 7d --max 3 --emit json` prints valid JSON. `item_count >= 1` is expected for this known pillar, but the code-level assertion is successful execution and parseable output.

---

### U4. Pipeline integration + cross-backend URL dedup

**Goal:** Teach the digest pipeline to invoke `fetch_gdelt` and merge its rows with `/last30days` output. Dedup by normalized URL; produce one row per URL with merged `meta.source`; preserve stable `first_seen_at` across repeated fetches.

**Requirements:** AC4, AC5.
**Dependencies:** U2, U3.
**Files:**
- `signal_room/pipeline.py` (modify — around line 89, branch on `fetch_backend in {"last30days","gdelt","both"}`)
- `signal_room/discovery_store.py` (new — normalized URL keying, source merge, first_seen preservation, payload write)
- `tests/test_discovery_store.py` (new) — focused dedup test

**Approach:**
1. Add `signal_room/discovery_store.py` with `write_merged_discovered_items(path, payloads, generated_at=None)`.
2. The helper loads the existing file (if any), normalizes URLs (lowercase scheme/host, strip fragment, strip trailing slash, drop tracking params like `utm_*`, `fbclid`, `gclid`), builds `{url_key → row}`, and writes a single payload:
   ```
   {
     "generated_at": "...",
     "backend": "merged",
     "backends": ["last30days", "gdelt"],
     "item_count": N,
     "items": [...],
     "errors": [...],
     "runs": [...]
   }
   ```
3. On collision, merge `meta.source` into a sorted unique list. If an existing last30days row lacks `meta.source`, infer `"last30days"` from `discovery_method` before merging.
4. Preserve the earliest existing `first_seen_at`; only assign a new timestamp when the URL is new to the file. Preserve richer fields instead of blindly replacing: non-empty `summary`/`content` wins, engagement/comment fields win from last30days, and GDELT-specific article fields are merged into `metadata`.
5. In `pipeline.py`, call fetchers with `output_path=None` when `fetch_backend in {"gdelt", "both"}` and persist with `write_merged_discovered_items(...)`. For `fetch_backend == "last30days"`, either keep existing behavior or route through the helper; if routed through the helper, preserve the existing CLI/output contract.
6. Add trace events around the merge: input counts by backend, output count, duplicate count, and a small sample of merged source lists.

**Worker/query-lab note:** Do not edit `worker.py` or `query_lab.py` in this unit. Those paths run free-form user queries against selected sources, while this unit is pillar-based scheduled discovery. If product wants GDELT in the web UI/query lab, add a separate `fetch_gdelt_today(query_text=...)` helper and wire it deliberately as U9.

**Patterns to follow:** `signal_room/pipeline.py:89` (existing fetch dispatch), `signal_room/fetchers/last30days.py:516` (`_normalize_entry`) for row shape, `signal_room/ingest.py` for existing downstream dedup expectations.

**Test scenarios:**
- Covers AC5. Existing file plus a new GDELT payload with one shared URL → merged result has one row with `meta.source == ["gdelt", "last30days"]` (sorted) and both sources' metadata preserved (assert by inspection).
- Existing last30days row without `meta` collides with a GDELT row → merged result infers `"last30days"` and keeps both source markers.
- Existing `first_seen_at` survives a re-fetch; new URLs receive a fresh timezone-aware ISO timestamp.
- URL normalization: `https://example.com/path/` and `https://example.com/path?utm_source=foo` collapse to the same key.
- No-collision case: payloads with fully disjoint URLs produce `len(left) + len(right)` rows.
- `run_pipeline(..., fetch_backend="gdelt")` writes `discovered_items.json` with `meta.source == "gdelt"` rows and no `last30days` invocation (assert via mock).
- `run_pipeline(..., fetch_backend="both")` invokes both fetchers and the resulting file is the merged dedup.

**Verification:** End-to-end run: `uv run signal-room run --brief config/brands/alice/brief.yaml --fetch both --fetch-pillars chatbot-failures --fetch-timespan 1d --fetch-query-limit 1` completes without error, `data/discovered_items.json` (or brand-scoped equivalent) contains rows from both sources, and at least one row has `meta.source` as a list of two if overlap occurred.

---

### U5. Pillar bootstrap from Alice brief

**Goal:** A one-shot, idempotent script that reads `config/brands/alice/brief.yaml` and creates/updates the 10 pillars defined in origin §4 Step 4 via `gdelt-pp-cli pillar add`.

**Requirements:** AC3.
**Dependencies:** U1 (binary resolver).
**Files:**
- `scripts/bootstrap_gdelt_pillars_from_alice.py` (new)
- `tests/test_bootstrap_gdelt_pillars.py` (new)

**Approach:**
- Read `config/brands/alice/brief.yaml` via PyYAML.
- Hold the pillar-name → boolean-query mapping inline at the top of the script (the table in origin §4 Step 4 is the source of truth for initial GDELT syntax; `brief.yaml` has pillar IDs/topics but not ready-to-run GDELT boolean strings). The script should validate that the expected Alice pillar IDs still exist in the brief and fail clearly if the brief shape changed.
- For each pillar: run `gdelt-pp-cli pillar list --json` once, check whether the pillar exists; if not, `gdelt-pp-cli pillar add <name> '<query>'`. If it exists and the query differs, `pillar rm` then re-`add` (idempotent update). Skip if identical.
- Honor `$GDELT_PILLARS_PATH` so test runs don't touch the user's pillars file.
- CLI args: `--dry-run` (print actions without executing), `--pillars-path PATH` (override), `--binary PATH` (override).
- Unit tests should use a fake CLI script or mocked `subprocess.run`; the verification command can use the real binary. Do not let default tests mutate `~/.config/gdelt-pp-cli/pillars.json`.

**Test scenarios:**
- With an empty temporary `$GDELT_PILLARS_PATH` file and fake CLI, running the script creates all 10 pillars (assert via captured subprocess calls or fake JSON file content).
- Running it twice in a row is a no-op the second time (idempotent — assert no `pillar add` invoked on second run via subprocess mock or by stat'ing mtime).
- Editing the inline query map and re-running updates the changed pillar (rm + add) and leaves the others untouched.
- `--dry-run` prints actions and exits 0 without modifying the pillars file.
- Short-acronym P4 pillars are added as separate entries, not OR'd together (regression guard for origin §6 gotcha 2).

**Verification:** `GDELT_PILLARS_PATH=/tmp/test-pillars.json python3 scripts/bootstrap_gdelt_pillars_from_alice.py && gdelt-pp-cli pillar list --json` (with `GDELT_PILLARS_PATH=/tmp/test-pillars.json`) prints all 10 pillar names.

---

### U6. Vendored source + `make build-gdelt`

**Goal:** Make the binary reproducible on Render/CI by vendoring the Go source and adding a build target.

**Requirements:** AC6.
**Dependencies:** none (parallel with U1–U5).
**Files:**
- `vendor/gdelt-pp-cli-src/` (new — source snapshot, see Approach)
- `Makefile` (new if absent — add `build-gdelt` target)
- `.gitignore` (modify — add `bin/gdelt-pp-cli`)
- `README.md` (modify — mention `make build-gdelt`)
- `scripts/render-build.sh` (modify — run `make build-gdelt` before app startup packaging)
- `render.yaml` (modify only if needed to select a build image/toolchain that provides Go `1.26.3` or newer)

**Approach:**
- Snapshot source from `~/printing-press/library/gdelt/` (the generator's output). Strip the binary itself if present; keep `go.mod`, `go.sum`, `cmd/`, internal packages, `SKILL.md`, `README.md` as a paper trail. Pin the Printing Press generator commit in a top-level `VENDORED_FROM.md` in the directory.
- Makefile target:
  ```
  build-gdelt:
  	mkdir -p bin
  	cd vendor/gdelt-pp-cli-src && go build -o ../../bin/gdelt-pp-cli ./cmd/gdelt-pp-cli
  ```
- Render: the repo currently builds through `bash scripts/render-build.sh` from `render.yaml`. Add `make build-gdelt` there after Python deps install; if the Render Python runtime does not include the required Go toolchain, switch to an install step or documented build image change in `render.yaml`.
- `config/gdelt_backend.json` resolver (already built in U1) handles the local-dev fallback to `~/printing-press/...` when `bin/gdelt-pp-cli` is absent.

**Test scenarios:** none (build infrastructure; verified by running the target).

**Test expectation: none — build infrastructure unit, verified by `make build-gdelt` producing a working binary and the smoke test in §11 of the origin passing against `bin/gdelt-pp-cli`.**

**Verification:**
- `make build-gdelt` on a clean checkout produces `bin/gdelt-pp-cli`.
- `./bin/gdelt-pp-cli doctor --json` returns `{"ok": true, ...}`.
- `./bin/gdelt-pp-cli today "ukraine" --max 5 --json` returns articles (origin §11 smoke test).

---

### U7. Fixture capture + live integration test

**Goal:** Capture a real GDELT response as the unit-test fixture, and add a `@pytest.mark.live` integration test that hits the real API.

**Requirements:** AC1, AC4.
**Dependencies:** U2, U6.
**Files:**
- `fixtures/gdelt_sample.json` (commit captured output)
- `tests/test_fetchers_gdelt_live.py` (new — marked `@pytest.mark.live`, skipped by default)

**Approach:**
- Capture: `~/printing-press/library/gdelt/gdelt-pp-cli pillar pull chatbot-failures --timespan 7d --max 8 --json > fixtures/gdelt_sample.json`. Commit verbatim.
- Live test: pull `chatbot-failures` (timespan `7d`, max 5) and assert the command succeeds, JSON parses, and every returned row has `meta.source == "gdelt"`. Prefer asserting `item_count >= 1` for this known pillar, but make the failure message explain that this is a data-availability failure, not necessarily a code regression.
- Run live tests via `pytest -m live`; default `pytest` skips them.

**Test scenarios:**
- (Live) `fetch_gdelt(pillars=["chatbot-failures"], timespan="7d", max_records=5)` exits successfully and every returned item has populated `metadata.language` and `metadata.sourcecountry`; if zero items return, the test should emit the compiled/query metadata in the assertion message.
- (Live) Pulling a known short-acronym P4 pillar (e.g., `ai-reg-ab-1988`) returns either non-zero hits OR zero hits with a logged warning — never raises.

**Verification:** `pytest -m live tests/test_fetchers_gdelt_live.py` passes on Dan's machine with network.

---

### U8. README update

**Goal:** Add a short section to the repo README that links the origin brief and documents how to add a new pillar.

**Requirements:** AC8.
**Dependencies:** U5.
**Files:**
- `README.md` (modify)

**Approach:** A short section ("GDELT signal source") with:
- One-paragraph what-and-why
- Pointer to `docs/plans/2026-05-13-integrate-gdelt-source.md` for the contract
- Three-step "add a pillar": edit the inline map in `scripts/bootstrap_gdelt_pillars_from_alice.py`, run the script, run `signal-room fetch --backend gdelt --pillars <new-name> --timespan 1d --max 5` to smoke-test.
- A note about origin §6 gotcha 2: never mega-OR short acronyms — split into separate pillars.

**Test expectation: none — documentation unit.**

**Verification:** A teammate reading only the README section can add a new pillar without opening the origin brief.

---

## Scope Boundaries

### In scope
Everything in U1–U8.

### Deferred to Follow-Up Work
- GDELT in the web worker and query lab. Those paths need free-form `today` semantics, not pillar pull semantics, and should be wired only after adding a dedicated helper/test surface.
- The CLI subcommands listed in origin §10 (`since`, `sync`, `brief`, `heat`, `search`). The fetcher does its own URL dedup against `discovered_items.json` to cover the `since` use case; the others are nice-to-haves.
- Cron / scheduled-pull setup (origin §5). Wiring is in scope; scheduling is a separate ops change.
- Per-source weighting in scoring. `meta.source = "gdelt"` is stamped on every row so the ranker *can* weight differently — actually tuning the weights is a downstream change.
- GEO/TV/Events/BigQuery surfaces — out of GDELT DOC 2.0 scope, explicitly excluded by origin §2.

### Outside this product's identity
- Rewriting the GDELT CLI in Python. The subprocess pattern is intentional.

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| GDELT silently returns empty for short-acronym OR queries (gotcha 2). | U5 splits P4 into per-phrase pillars. U2 logs a warning when `articles=[]` so we notice if a pillar goes dark. |
| Stderr rate-limit noise misread as failure. | U2 filters lines matching `^rate limited, waiting \d+s` before checking returncode; tested explicitly. |
| Render build fails because Go isn't in the build image. | U6 adds the Go step; if Render doesn't allow it cleanly, the fallback is to ship option (a) — vendor prebuilt binaries — without changing the runtime resolver chain (it already handles `bin/gdelt-pp-cli` either way). |
| GDELT `seendate` lag causes stale articles to surface as "new". | U2 stamps `first_seen_at = now()` at ingest; U4 dedups by URL against existing `discovered_items.json` so the same article isn't re-emitted on later pulls. |
| Pillar set drift between brief.yaml and `~/.config/gdelt-pp-cli/pillars.json`. | U5 is idempotent and re-runnable; documented in U8 README as the canonical way to update pillars. |
| The plan accidentally changes ad hoc web-search behavior while adding scheduled discovery. | U4 keeps `worker.py`/`query_lab.py` out of the pillar path; a separate U9 must add GDELT `today` support if needed. |

---

## Verification Strategy

1. Unit tests (U1, U2, U3, U4, U5) pass under `pytest tests/test_fetchers_gdelt.py tests/test_discovery_store.py tests/test_bootstrap_gdelt_pillars.py`.
2. Smoke test from origin §11 passes against `bin/gdelt-pp-cli`.
3. Live integration (U7, gated by `-m live`): `pytest -m live` returns green on a network-connected machine.
4. End-to-end: `uv run signal-room run --brief config/brands/alice/brief.yaml --fetch both --fetch-pillars chatbot-failures --fetch-timespan 1d --fetch-query-limit 1` exits 0 and produces a digest whose discovered payload contains GDELT-sourced rows.
5. Render build green after U6 lands.

---

## Sequencing

```
U1 (config + resolver)
 ├─→ U2 (core fetch_gdelt)
 │    ├─→ U3 (CLI wiring)
 │    │    └─→ U4 (pipeline + dedup)
 │    └─→ U7 (live test)         [also depends on U6]
 ├─→ U5 (bootstrap script)
 │    └─→ U8 (README)
 └─→ U6 (vendored build)         [parallel; merges into U7]
```

Critical path: U1 → U2 → U3 → U4. U5/U6/U7/U8 can land in parallel once their deps are met.
