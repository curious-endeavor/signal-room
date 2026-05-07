# Curious Endeavor Signal Room MVP

Signal Room is a local Python MVP for finding mechanism-rich signals that Curious Endeavor can use for content, client thinking, and pitch references.

The v1 is intentionally simple: it runs manually, uses local fixture/search-candidate data, scores items through the CE lens, stores JSON artifacts, and generates a shareable static HTML digest with the top 10 signals.

## Setup

The base Signal Room MVP uses only the Python standard library.

The optional `/last30days` adapter reuses a local `last30days` installation for live discovery.

```bash
python3 --version
```

Python 3.9+ is enough.

The `last30days` adapter reads its runtime from `config/last30days_backend.json`.
The current default points to a dedicated local venv at `.venvs/last30days/bin/python`.

## Installable CLI

You can run Signal Room directly as a module:

```bash
python3 -m signal_room run
```

Or install it into a local environment and use the console script:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/signal-room queries --emit json
```

## Run The Digest

```bash
python3 -m signal_room run
```

The command:

- reads source configuration from `config/seed_sources.json`
- reads scoring weights from `config/scoring_weights.json`
- reads source feedback weights from `config/source_feedback_weights.json`
- reads sample/search-candidate items from `fixtures/sample_items.json`
- writes local data into `data/`
- writes the digest into `output/signal-room-digest-YYYY-MM-DD.html`

Open the generated HTML file in a browser and review the top 10 surfaced signals.

## Fetch Discovery

Fetch discovery items from the `last30days` adapter:

```bash
python3 -m signal_room fetch --backend last30days --mock
```

Then build the digest using both fixtures and discovered items:

```bash
python3 -m signal_room run
```

Or do both in one step:

```bash
python3 -m signal_room run --fetch last30days --fetch-mock
```

Flags:

- `--fetch last30days`: fetch discovery items before scoring
- `--fetch-mock`: run `last30days` in mock mode for testing
- `--fetch-query-limit N`: limit how many discovery queries run during fetch
- `--fixtures-only`: ignore discovered items and score fixtures only
- `--emit json`: return machine-readable JSON for CLI consumers

## Queryable Commands

These commands are designed to be easier for agents and other tools to consume:

```bash
signal-room queries --emit json
signal-room items --limit 10 --emit json
signal-room item --item-id owner-grader-cmo --emit json
signal-room feedback-log --limit 20 --emit json
signal-room fetch --backend last30days --mock --emit json
signal-room run --fetch last30days --fetch-mock --emit json
```

## Query Lab

Use the query lab to test multiple `last30days` phrasings in parallel and review which combinations produce higher-signal CE candidates.

Example:

```bash
signal-room lab run \
  --query "restaurant marketing ai case study" \
  --query "cmo as software restaurant marketing" \
  --query "ai restaurant marketing workflow" \
  --sources grounding,reddit \
  --parallelism 3 \
  --lookback-days 2
```

That creates a batch summary under `data/query_lab/batches/<batch-id>/` with:

- `summary.json`: machine-readable per-query metrics and top items
- `summary.md`: human-readable review report
- `discovered_items.json`: normalized raw items for the batch
- `runs/<query-id>/`: preserved `last30days` artifacts per query

To reopen the latest batch:

```bash
signal-room lab show --batch-id latest
```

Freshness controls:

- backend default lookback lives in `config/last30days_backend.json`
- override per fetch:

```bash
signal-room fetch --backend last30days --lookback-days 2
```

- override per combined run:

```bash
signal-room run --fetch last30days --fetch-lookback-days 2
```

- override per query-lab batch:

```bash
signal-room lab run --query "ai-first marketing team" --sources x --lookback-days 1
```

## Local Web UI

The repo includes a plain Google-style UI preview for reviewing CE signals.

Static preview, no Python server needed:

```bash
open ui-preview/index.html
```

FastAPI preview:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/uvicorn signal_room.web:app --reload
```

Open `http://127.0.0.1:8000/sample` to review the UI using the latest local `data/enriched_items.json`.

To test queued searches locally:

```bash
SIGNAL_ROOM_FETCH_MOCK=1 .venv/bin/python -m signal_room.worker
```

In another terminal:

```bash
.venv/bin/uvicorn signal_room.web:app --reload
```

Submitted searches are stored in local SQLite at `data/signal_room_web.sqlite3`. On Render, the same store uses Postgres via `DATABASE_URL`.

## Render Deployment

The `render.yaml` blueprint defines:

- `ce-signal-room-web`: FastAPI web service
- `ce-signal-room-worker`: background worker that processes queued `last30days` searches
- `ce-signal-room-db`: Render Postgres database

Deploy from Render using the repository blueprint, then add the source credentials needed by `last30days` as environment variables. Useful keys include `BRAVE_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `SCRAPECREATORS_API_KEY`, `GITHUB_TOKEN`, `AUTH_TOKEN`, and `CT0`.

The blueprint prompts for the production secret values with `sync: false`; do not commit real API keys. For Render, `XAI_API_KEY` is the preferred X source because `render.yaml` pins `LAST30DAYS_X_BACKEND=xai`. `SCRAPECREATORS_API_KEY` powers Instagram and YouTube enrichment, `BRAVE_API_KEY` powers web grounding, and `GITHUB_TOKEN` powers GitHub results.

During build, `scripts/render-build.sh` clones the pinned `last30days-skill` version into `vendor/last30days-skill`, then installs both packages. The local `vendor/last30days-skill/` checkout is intentionally ignored by git.

The web service runs:

```bash
uvicorn signal_room.web:app --host 0.0.0.0 --port $PORT
```

The worker runs:

```bash
python -m signal_room.worker
```

## Local Data

The pipeline writes:

- `data/raw_items.json`: deduped raw items from seed and search/discovery fixture input
- `data/discovered_items.json`: normalized items fetched from live discovery backends
- `data/enriched_items.json`: all enriched/scored items with CE lens fields
- `data/source_candidates.json`: trusted seed sources plus untrusted daily candidate sources
- `data/feedback.jsonl`: feedback events recorded from review
- `data/last30days/runs/YYYY-MM-DD/<query-id>/`: preserved raw fetch outputs, manifests, and normalized per-query artifacts
- `config/scoring_weights.json`: global scoring weights
- `config/source_feedback_weights.json`: local source-level feedback weights

This keeps v1 reviewable and easy to reset. Delete generated files in `data/` and `output/` if you want a clean run.

## Review And Mark Feedback

Each HTML card includes a feedback command. Use one of these actions:

- `useful`
- `not_useful`
- `wrong_pillar`
- `too_generic`
- `source_worth_following`
- `turned_into_content`

Example:

```bash
python3 -m signal_room feedback --item-id owner-grader-cmo --action useful --note "Strong SMB CMO-as-software signal"
python3 -m signal_room run
```

Feedback is appended to `data/feedback.jsonl`. Source-level feedback also updates `config/source_feedback_weights.json`, so future runs can lift or suppress sources based on review history.

## CE Lens

Pillars:

- P1. Creating content with AI
- P2. Designing with AI
- P3. Turning a marketing team into AI-first
- P4. Selecting which workflows are AI-ready and which are not
- P5. Assessing good vs. bad in human-in-the-loop AI work

Surf:

- S1. AI-native brand-building in the wild
- S2. Excellent classic branding reference material

The scorer rewards mechanism-rich items: workflows, methods, implementation detail, failure modes, human-in-the-loop judgment, and "do not use AI for this yet" lessons. It penalizes generic AI takes and raw vendor/model announcements without a CE-relevant workflow or marketing angle.

## Fixture Coverage

`fixtures/sample_items.json` includes the three required exemplars:

- Owner/Grader "AI CMO for restaurants"
- Ramp Glass "every employee gets an AI coworker"
- Anthropic/OpenAI-style verticalized service-company / consulting-arm news

It also includes noisy generic/model-news examples so the scorer can demonstrate filtering behavior.

## Missing Real-World Credentials

This repo now includes a `last30days` fetch adapter, but live discovery still depends on local runtime prerequisites and any relevant third-party auth that `last30days` itself uses.

Current adapter behavior:

- mock mode is verified locally
- live mode is verified with a dedicated venv-backed interpreter
- the adapter reads its interpreter and runtime knobs from `config/last30days_backend.json`
- the current backend config uses `--quick`, defaults to `reddit,hackernews,grounding`, and enforces a timeout

The repo still does not include its own live web search keys, RSS/API credentials, paid publisher access, or scraping permissions. Signal Room expects fetchers to write items matching this schema:

```json
{
  "id": "stable-id",
  "title": "Signal title",
  "source": "Source name",
  "source_url": "https://...",
  "date": "YYYY-MM-DD",
  "summary": "Short summary",
  "content": "Mechanism details or extracted article text",
  "discovery_method": "seed or search",
  "candidate_source": false,
  "tags": ["workflow", "brand", "AI"]
}
```

## Known v1 Limitations

- No polished app UI
- No post editor
- No image generation
- No scheduling or publishing
- No multi-client support
- No live search integration yet
- Scoring is heuristic and local, not an LLM enrichment pass
- Static HTML cannot save feedback directly; feedback is recorded through the CLI command shown on each card

The goal of v1 is not automation. It is to produce a digest CE can review and use as a source for content, client references, or pitch material.
