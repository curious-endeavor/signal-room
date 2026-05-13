# Brief — Integrate the new GDELT CLI as a signal-room source

**Author:** Dan (via printing-press build session on 2026-05-13)
**Audience:** the coding agent working on `curious-endeavor` (this repo)
**Status:** ready to implement; CLI is already built, live-tested, and accessible

---

## 1. What just happened

I generated a new agent-native CLI for the **GDELT DOC 2.0 API** using the Printing Press generator. It's installed at:

```
~/printing-press/library/gdelt/gdelt-pp-cli         # 18 MB Go binary
~/printing-press/library/gdelt/gdelt-pp-mcp         # MCP server (stdio)
~/printing-press/library/gdelt/SKILL.md
~/printing-press/library/gdelt/README.md
```

A symlink already exists at repo root: `./gdelt-pp-cli -> ~/printing-press/library/gdelt/gdelt-pp-cli`.

GDELT is a global news-monitoring service: ~3-month rolling window, 65+ machine-translated languages, updated every 15 minutes, **keyless / no auth**, courtesy-rate-limited to **1 request per 5 seconds** (the CLI already enforces this with `--rate-limit 0.2` by default).

I live-tested it against three Alice pillars from `config/brands/alice/brief.yaml`:

- **P1 chatbot-failures** → 7 of 8 hits directly relevant (Shapiro task force on misleading chatbots; multiple ChatGPT lawsuits; Nature on AI bioweapons).
- **P3 ai-security-tooling** → caught the Palo Alto Networks "Idira" AI security launch — the exact "incumbent moves into Alice's lane" signal the brief monitors for.
- **P4 ai-regulation** → 0 hits; surfaced a real GDELT limitation (see §6).

---

## 2. Goal of this work

Wire `gdelt-pp-cli` into the signal-room pipeline as **an additional source alongside `/last30days`**. Same role, different surface: GDELT covers worldwide *news media* in 65+ languages; `/last30days` covers social + grounding. The two should compose into one signal feed.

**Non-goals:**
- Don't replace `/last30days`.
- Don't rewrite the GDELT CLI in Python — call it as a subprocess, same pattern `last30days` uses.
- Don't expand scope beyond DOC 2.0 (no GEO/TV/Events/BigQuery).

---

## 3. CLI contract (what the agent needs to know)

### Headline commands

```bash
# One-off pull for a topic. Headline use case.
gdelt-pp-cli today "<query>" --timespan 1d --max 75 --json
#   timespan: 15min | 1h | 24h | 7d | 2w | 3m  (max ~3 months)
#   sort: defaults to datedesc; dedups syndicated copies

# Saved boolean query (pillar) CRUD
gdelt-pp-cli pillar add <name> '<gdelt-boolean-query>'
gdelt-pp-cli pillar list --json
gdelt-pp-cli pillar pull <name> --timespan 1d --max 75 --json
gdelt-pp-cli pillar rm <name>

# Spec-derived commands (also work, lower-level)
gdelt-pp-cli news <query> --timespan 1d --json
gdelt-pp-cli timeline {volume,volraw,tone,bycountry,bylang} <query> --json
gdelt-pp-cli tonechart <query> --json
gdelt-pp-cli doctor --json     # health check
gdelt-pp-cli agent-context     # full structured tree for agent discovery
```

### Output JSON shape

Every command emits the same envelope. **This is the shape the fetcher must parse:**

```json
{
  "meta": {"source": "live"},
  "results": {
    "pillar": "chatbot-failures",                    // only for pillar pull
    "query": "(\"chatbot\" OR \"AI agent\") AND ...", // only for pillar pull
    "articles": [
      {
        "url": "https://...",
        "url_mobile": "https://...",
        "title": "...",
        "seendate": "20260513T120000Z",      // YYYYMMDDTHHMMSSZ, UTC
        "socialimage": "https://...",
        "domain": "bctv.org",
        "language": "English",
        "sourcecountry": "United States",
        "copies": 2,                          // only set when dedup merged
        "also_in": ["wistv.com"]              // only set when dedup merged
      }
    ],
    "count": 8
  }
}
```

### Useful flags (same shape as `last30days`'s)

| Flag | Purpose |
|---|---|
| `--agent` | shorthand for `--json --compact --no-input --no-color --yes` |
| `--select <a,b,c>` | dotted-path projection (e.g. `--select results.articles.title,results.articles.url`) |
| `--compact` | trim to high-gravity fields only |
| `--country <FIPS-or-name>` | restrict to a source country |
| `--lang <name-or-code>` | restrict to a source language |
| `--dry-run` | show what would be requested without spending a rate-limit slot |
| `--print-query` (on `today` only) | print compiled GDELT query for debugging |
| `--timespan <window>` | override the default 24h on `today`/`pillar pull` |
| `--rate-limit 0` | disable client-side pacing (only if you have a custom GDELT quota) |

### Exit codes

`0` ok · `2` usage · `3` not found · `4` network · `5` auth (won't trigger — no auth) · `7` rate-limited (after 3 retries) · `10` server

### Pillar storage

Pillars persist as JSON at `~/.config/gdelt-pp-cli/pillars.json`. Override the path with `$GDELT_PILLARS_PATH` (useful for tests and for keeping signal-room pillars separate from any personal pillars).

---

## 4. Implementation plan — mirror the `/last30days` pattern exactly

The current `/last30days` integration is the reference. Match it:

```
config/last30days_backend.json    →   config/gdelt_backend.json
signal_room/fetchers/last30days.py →  signal_room/fetchers/gdelt.py
vendor/last30days-skill/           →  (skip — CLI is at ~/printing-press/library/gdelt/)
```

### Step 1 — `config/gdelt_backend.json`

```json
{
  "binary_path": "~/printing-press/library/gdelt/gdelt-pp-cli",
  "pillars_path": "~/.config/gdelt-pp-cli/pillars.json",
  "default_timespan": "1d",
  "default_max": 75,
  "rate_limit_rps": 0.2,
  "timeout_seconds": 60
}
```

Keep `binary_path` overrideable so we can point it at a vendored binary later (see §7).

### Step 2 — `signal_room/fetchers/gdelt.py`

Shape it like `last30days.py`:

- `class GdeltError(RuntimeError): pass`
- `fetch_gdelt(pillars: list[str] | None, timespan: str = "1d", max_records: int = 75, run_root: Path | None = None, output_path: Path | None = None) -> dict`
- Internally: for each pillar name, `subprocess.run([binary_path, "pillar", "pull", name, "--timespan", timespan, "--max", str(max_records), "--json"], capture_output=True, timeout=cfg.timeout_seconds)`.
- Parse stdout as JSON, pull `results.articles[]`, normalize to the same `discovered_items.json` row shape `last30days.py` already produces. Use `domain` as platform, `url` as the canonical id, `sourcecountry` and `language` as metadata.
- Honor `mock=True` by reading a fixture (write one at `fixtures/gdelt_sample.json` from a real `pillar pull` and check it in).
- `continue_on_error=True` semantics — one failed pillar shouldn't kill the run.
- Stamp `meta.source = "gdelt"` per row so the ranker can weight differently.
- Stderr lines that start with `rate limited, waiting Ns` are **informational** (the CLI's adaptive limiter), not errors. Filter them out before checking `returncode`.

### Step 3 — wire into the worker/pipeline

Mirror the four spots `last30days` is referenced:

```
signal_room/worker.py     → spawn fetch_gdelt alongside fetch_last30days
signal_room/pipeline.py   → consume gdelt rows in the same merge step
signal_room/cli.py        → add `signal-room fetch gdelt …` subcommand
signal_room/query_lab.py  → register gdelt as a queryable backend
```

The merge into `data/discovered_items.json` should dedup across sources by URL — GDELT and `/last30days` can both surface the same article; we want one row with both `meta.source` values, not two.

### Step 4 — pillar bootstrap from the Alice brief

`config/brands/alice/brief.yaml` already has the pillars (P1–P5) with keyword lists. Add a one-time bootstrap script at `scripts/bootstrap_gdelt_pillars_from_alice.py` that reads the YAML and `gdelt-pp-cli pillar add` each pillar. Use these mappings (start tight; expand once we see signal):

| Pillar id | name | query (start with these — refine after a week of pulls) |
|---|---|---|
| P1 | `chatbot-failures` | `("chatbot" OR "AI agent" OR "AI assistant") AND (lawsuit OR scandal OR fail OR harm OR sycophancy OR hallucination OR "customer service")` |
| P2 | `frontier-model-safety` | `("red team" OR jailbreak OR "prompt injection" OR "system card" OR "alignment research") AND (Anthropic OR OpenAI OR DeepMind OR "Irregular")` |
| P3 | `ai-security-tooling` | `("Palo Alto Networks" OR Zscaler OR Lakera OR "Robust Intelligence" OR HiddenLayer OR "Protect AI" OR Patronus OR CalypsoAI OR "Prompt Security") AND (security OR guardrails OR "red team" OR "prompt injection")` |
| P3b | `data-generalists` | `("Scale AI" OR "Surge AI" OR Prolific OR Toloka OR Mercor) AND (safety OR "red team" OR evaluation OR benchmark)` |
| P4 split (see §6) | `ai-reg-eu-act` / `ai-reg-ab-1988` / `ai-reg-iso-42001` / `ai-reg-nist-rmf` / `ai-reg-mitre` | each phrase as its own pillar (`"EU AI Act"`, `"AB 1988"`, etc.) — do NOT mega-OR them |
| P5 | `regulated-vertical-ai` | `("AI chatbot" OR "AI assistant" OR "AI agent") AND (healthcare OR patient OR fintech OR insurance OR "claims agent" OR pharmacy)` |

Make the script idempotent (re-running it updates queries in place, doesn't duplicate).

### Step 5 — tests

- Unit: `tests/test_fetchers_gdelt.py` — parse fixture JSON; assert normalization; assert rate-limit stderr lines are ignored; assert one failed pillar doesn't crash the run when `continue_on_error=True`.
- Integration (skipped by default with `@pytest.mark.live`): pull `chatbot-failures` against the real API and assert `>=1 article`, `language`/`sourcecountry` set, dedup metadata sane.

---

## 5. Cron / scheduled-pull plan

Once §3 is wired, the signal room should run on a schedule like:

```bash
# Every 30 min (matches GDELT's 15-min update cadence with room to breathe)
*/30 * * * *  cd <repo> && uv run signal-room fetch gdelt --pillars all --timespan 2h
# Once a day, deeper window
0    9 * * *  cd <repo> && uv run signal-room fetch gdelt --pillars all --timespan 24h
```

Per-call cost: ~one HTTP request per pillar, ~5 seconds rate-limit-paced. 6 pillars × 5s = ~30s wall-clock for a full sweep.

---

## 6. Known gotchas — bake handling into the fetcher

1. **GDELT rejects keywords <3 chars.** A bare `AI` token fails (returns a plain-text error, "Your search contained a keyword that was too short"). The CLI currently surfaces this as an empty article list, not an error. Workarounds in the query: quote the token (`"AI"`), expand to `"artificial intelligence"`, or always combine with longer terms (`AI AND chatbot`).
2. **Multi-phrase OR queries can silently return empty.** Combining several short/all-caps quoted phrases with `OR` (e.g. `"EU AI Act" OR "AB 1988" OR "ISO 42001"`) returns 0 even when each phrase alone returns hits. **Always split into one pillar per phrase** for short-acronym statutes; rejoin downstream. This is why P4 in the table above is split.
3. **Rate limit of 1 req per 5s** — `--rate-limit 0.2` is the default. The CLI prints `rate limited, waiting Ns (attempt M/3, rate adjusted to X req/s)` lines on stderr while pacing. **These are not errors** — filter them out before checking subprocess exit codes.
4. **3-month rolling window** — anything older returns empty. Don't use absolute `--since` dates older than ~85 days.
5. **Stale articles can leak through** — GDELT's `seendate` is when *GDELT* indexed the article, not the article's own publish date. For "what's new since last check" semantics, the fetcher should stamp `first_seen_at = now()` when ingesting and dedup by URL on subsequent pulls.
6. **No write side.** Every command is read-only. The `--dry-run` flag is for sanity, not safety.

---

## 7. Open question — vendor the binary or call the symlink?

The repo currently has a symlink at `./gdelt-pp-cli -> ~/printing-press/library/gdelt/gdelt-pp-cli`. That works on Dan's machine but **breaks on CI / Render / any other developer's checkout**. Pick one and tell me which:

- **(a) Vendor the binary into `vendor/gdelt-pp-cli/`** per OS/arch — fast, no Go toolchain needed downstream, but tracks 18 MB × N platforms in git.
- **(b) Vendor a `vendor/gdelt-pp-cli-src/` snapshot of the source and add a `make build-gdelt` target** that compiles to `bin/gdelt-pp-cli`. Needs Go ≥1.26.3 wherever the repo is checked out, but keeps the repo small.
- **(c) Add an install step (`scripts/install_gdelt.sh`) that runs `go install github.com/mvanhorn/cli-printing-press-library/library/gdelt/cmd/gdelt-pp-cli@<pinned-sha>`** once the CLI is published to that library. Same Go-required caveat; minimal repo footprint.

If unsure, default to **(b)** — Render already builds Python so we just need to add Go to the build image; it keeps the repo lean and reproducible. Make the binary path in `config/gdelt_backend.json` resolve via `bin/gdelt-pp-cli` first, falling back to `~/printing-press/library/gdelt/gdelt-pp-cli` for local development.

---

## 8. Acceptance criteria

- [ ] `signal_room/fetchers/gdelt.py` exists, mirrors the `last30days.py` shape, and has a passing fixture-based unit test.
- [ ] `config/gdelt_backend.json` exists and the fetcher reads from it.
- [ ] `scripts/bootstrap_gdelt_pillars_from_alice.py` exists and creates the 10 pillars listed in §4 from `config/brands/alice/brief.yaml`.
- [ ] `uv run signal-room fetch gdelt --pillars all --timespan 1d` runs end-to-end, writes rows to `data/discovered_items.json`, and the rows carry `meta.source = "gdelt"`.
- [ ] Merging with a parallel `/last30days` fetch dedups by URL.
- [ ] The §7 vendoring decision is implemented (option a / b / c).
- [ ] The 6 gotchas in §6 are each handled or explicitly noted as out-of-scope.
- [ ] A line in the repo README points to this brief and explains how to add a new pillar.

---

## 9. Reference material (open these before starting)

- **Live CLI** — run `~/printing-press/library/gdelt/gdelt-pp-cli --help` and `… today --help`, `… pillar --help`, `… agent-context` to see the full surface
- **Generated README** — `~/printing-press/library/gdelt/README.md` (has Quick Start, Agent Usage, MCP integration, Known Gaps, Troubleshooting)
- **Generated SKILL.md** — `~/printing-press/library/gdelt/SKILL.md` (the agent-facing tool description)
- **Alice brief** — `config/brands/alice/brief.yaml` (the pillar source-of-truth — fetcher should read pillar definitions from here, not duplicate them)
- **Existing `/last30days` integration** (the pattern to mirror):
  - `signal_room/fetchers/last30days.py`
  - `config/last30days_backend.json`
  - `signal_room/worker.py`, `signal_room/pipeline.py`, `signal_room/cli.py`, `signal_room/query_lab.py` (the four wire-up sites)
- **GDELT DOC 2.0 docs** — https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- **Upstream CLI generator** — https://github.com/mvanhorn/cli-printing-press

---

## 10. Deferred — don't build these in this pass

These were in the original Printing Press absorb manifest but didn't ship in the first cut. **Leave them for a follow-up** unless the signal room pipeline can't function without one:

- `gdelt-pp-cli since <window>` — first-seen diff (the fetcher should do its own URL-dedup against `discovered_items.json` for now)
- `gdelt-pp-cli sync --pillars` — bulk pull of all pillars in one call (the fetcher's loop replaces this for now)
- `gdelt-pp-cli brief <pillar>` — pre-baked digest with volume Δ / tone Δ / top source countries
- `gdelt-pp-cli heat` — rank pillars by volume change
- `gdelt-pp-cli search` — FTS5 offline search over cached articles

If any of these become blocking, file an issue against the printing-press build and I'll re-open the generator session — they're scoped, just deferred.

---

## 11. First-day smoke test the agent should run before opening a PR

```bash
# 1. Binary works
~/printing-press/library/gdelt/gdelt-pp-cli version
~/printing-press/library/gdelt/gdelt-pp-cli doctor --json

# 2. Live API works
~/printing-press/library/gdelt/gdelt-pp-cli today "ukraine" --max 5 --json

# 3. Pillar CRUD works
GDELT_PILLARS_PATH=/tmp/gdelt-smoke.json \
  ~/printing-press/library/gdelt/gdelt-pp-cli pillar add test-pillar '"customer service AI"'
GDELT_PILLARS_PATH=/tmp/gdelt-smoke.json \
  ~/printing-press/library/gdelt/gdelt-pp-cli pillar pull test-pillar --timespan 1d --max 3 --json
rm /tmp/gdelt-smoke.json
```

All three should return clean JSON with `meta.source = "live"` and a non-empty articles array (for any reasonable topic).
