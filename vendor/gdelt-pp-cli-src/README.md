# GDELT CLI

**Search worldwide news on any topic and track named pillars — backed by GDELT's DOC 2.0 API and an MCP server.**

Wraps GDELT's keyless DOC 2.0 API (worldwide online news, 65+ languages, rolling 3-month window, 15-minute updates). The headline command is `today "<topic>"` — last 24 hours, worldwide, deduped, newest-first, agent-ready JSON. Pillars are named saved queries you can pull on demand (`pillar pull child-safety-ai`). Some deeper local-store features (`since`, `brief`, `heat`, `search`) are deferred to a follow-up — see the [Known Gaps](#known-gaps) section.

## Install

The recommended path installs both the `gdelt-pp-cli` binary and the `pp-gdelt` agent skill in one shot:

```bash
npx -y @mvanhorn/printing-press install gdelt
```

For CLI only (no skill):

```bash
npx -y @mvanhorn/printing-press install gdelt --cli-only
```


### Without Node

The generated install path is category-agnostic until this CLI is published. If `npx` is not available before publish, install Node or use the category-specific Go fallback from the public-library entry after publish.

### Pre-built binary

Download a pre-built binary for your platform from the [latest release](https://github.com/mvanhorn/printing-press-library/releases/tag/gdelt-current). On macOS, clear the Gatekeeper quarantine: `xattr -d com.apple.quarantine <binary>`. On Unix, mark it executable: `chmod +x <binary>`.

<!-- pp-hermes-install-anchor -->
## Install for Hermes

From the Hermes CLI:

```bash
hermes skills install mvanhorn/printing-press-library/cli-skills/pp-gdelt --force
```

Inside a Hermes chat session:

```bash
/skills install mvanhorn/printing-press-library/cli-skills/pp-gdelt --force
```

## Install for OpenClaw

Tell your OpenClaw agent (copy this):

```
Install the pp-gdelt skill from https://github.com/mvanhorn/printing-press-library/tree/main/cli-skills/pp-gdelt. The skill defines how its required CLI can be installed.
```

## Quick Start

```bash
# the headline use case: worldwide news on a topic from the last 24h, deduped, newest first
gdelt-pp-cli today "child safety AI" --json


# save a topic as a named, re-runnable pillar
gdelt-pp-cli pillar add child-safety-ai '("child safety" OR CSAM OR "age verification") AND (AI OR chatbot)'


# pull fresh hits for every saved pillar (cron entrypoint)
gdelt-pp-cli sync --last 24h


# only the articles first seen in the last day — the delta a feed ingests
gdelt-pp-cli since 1d --pillar child-safety-ai --json


# ingestible digest: top deduped articles + volume Δ + tone Δ + top source countries
gdelt-pp-cli brief child-safety-ai --last 7d --top 8 --json


# is this topic heating up? coverage-volume curve over 30 days
gdelt-pp-cli timeline "child safety AI" --last 30d --smooth 3

```

## Unique Features

These capabilities aren't available in any other tool for this API.

### Topic monitoring

- **`today`** — Get worldwide news on any topic from the last 24 hours — deduped, newest first, ready for an agent to ingest.

  _When an agent needs 'what's the world saying about X right now', this is the one call — clean JSON, no query-string assembly, no syndication noise._

  ```bash
  gdelt-pp-cli today "child safety AI" --json
  ```
- **`pillar add`** — Save a boolean query as a named pillar once, then pull it by name forever.

  _Lets a monitoring workflow refer to topics by stable names instead of re-typing fragile boolean queries every run._

  ```bash
  gdelt-pp-cli pillar add child-safety-ai '("child safety" OR CSAM OR "age verification") AND (AI OR chatbot)'
  ```
- **`since`** — Return only the articles first seen since a given window — the delta, not the whole list.

  _A monitoring agent should process each story once — 'since' guarantees no re-processing of articles already seen._

  ```bash
  gdelt-pp-cli since 1d --pillar child-safety-ai --json
  ```

### Local store that compounds

- **`sync`** — Pull fresh hits for every saved pillar in one command and stamp each pillar's last-pulled time.

  _One scheduled call refreshes every topic the signal room cares about; downstream 'since'/'brief' read from the freshly-synced store._

  ```bash
  gdelt-pp-cli sync --last 24h
  ```
- **`brief`** — A compact digest for a pillar: top-N deduped articles, volume change vs the prior window, mean-tone change, and the top source countries.

  _This is the ingestible unit — one JSON object per pillar that a signal UI or report can render directly without further processing._

  ```bash
  gdelt-pp-cli brief child-safety-ai --last 7d --top 8 --json
  ```
- **`heat`** — Rank all tracked pillars by how much their coverage volume changed this window vs last.

  _Tells a monitoring agent which topic to look at first instead of polling all of them blindly._

  ```bash
  gdelt-pp-cli heat --window 7d --json
  ```
- **`search`** — Full-text search over every article ever pulled, across all pillars, with no API round-trip.

  _Re-querying past coverage costs nothing and doesn't hit GDELT's courtesy rate limits._

  ```bash
  gdelt-pp-cli search "age verification" --json
  ```

## Usage

Run `gdelt-pp-cli --help` for the full command reference and flag list.

## Commands

### news

Search worldwide news articles (GDELT DOC 2.0 artlist)

- **`gdelt-pp-cli news search`** - Search news articles matching a query across 65+ languages over a rolling 3-month window

### timeline

Coverage-volume timelines for a query (GDELT DOC 2.0 timeline modes)

- **`gdelt-pp-cli timeline bycountry`** - Coverage volume over time broken down by source country
- **`gdelt-pp-cli timeline bylang`** - Coverage volume over time broken down by source language
- **`gdelt-pp-cli timeline tone`** - Average article tone (sentiment) over time
- **`gdelt-pp-cli timeline volraw`** - Coverage volume over time as raw article counts (and the all-articles denominator)
- **`gdelt-pp-cli timeline volume`** - Coverage volume over time as a percent of all monitored articles

### tonechart

Histogram of article tone for a query (GDELT DOC 2.0 tonechart)

- **`gdelt-pp-cli tonechart get`** - Distribution of article tone (how many articles fall in each tone bucket)


## Output Formats

```bash
# Human-readable table (default in terminal, JSON when piped)
gdelt-pp-cli news mock-value

# JSON for scripting and agents
gdelt-pp-cli news mock-value --json

# Filter to specific fields
gdelt-pp-cli news mock-value --json --select id,name,status

# Dry run — show the request without sending
gdelt-pp-cli news mock-value --dry-run

# Agent mode — JSON + compact + no prompts in one flag
gdelt-pp-cli news mock-value --agent
```

## Agent Usage

This CLI is designed for AI agent consumption:

- **Non-interactive** - never prompts, every input is a flag
- **Pipeable** - `--json` output to stdout, errors to stderr
- **Filterable** - `--select id,name` returns only fields you need
- **Previewable** - `--dry-run` shows the request without sending
- **Read-only by default** - this CLI does not create, update, delete, publish, send, or mutate remote resources
- **Offline-friendly** - sync/search commands can use the local SQLite store when available
- **Agent-safe by default** - no colors or formatting unless `--human-friendly` is set

Exit codes: `0` success, `2` usage error, `3` not found, `5` API error, `7` rate limited, `10` config error.

## Use with Claude Code

Install the focused skill — it auto-installs the CLI on first invocation:

```bash
npx skills add mvanhorn/printing-press-library/cli-skills/pp-gdelt -g
```

Then invoke `/pp-gdelt <query>` in Claude Code. The skill is the most efficient path — Claude Code drives the CLI directly without an MCP server in the middle.

<details>
<summary>Use as an MCP server in Claude Code (advanced)</summary>

If you'd rather register this CLI as an MCP server in Claude Code, install the MCP binary first:


Install the MCP binary from this CLI's published public-library entry or pre-built release.

Then register it:

```bash
claude mcp add gdelt gdelt-pp-mcp
```

</details>

## Use with Claude Desktop

This CLI ships an [MCPB](https://github.com/modelcontextprotocol/mcpb) bundle — Claude Desktop's standard format for one-click MCP extension installs (no JSON config required).

To install:

1. Download the `.mcpb` for your platform from the [latest release](https://github.com/mvanhorn/printing-press-library/releases/tag/gdelt-current).
2. Double-click the `.mcpb` file. Claude Desktop opens and walks you through the install.

Requires Claude Desktop 1.0.0 or later. Pre-built bundles ship for macOS Apple Silicon (`darwin-arm64`) and Windows (`amd64`, `arm64`); for other platforms, use the manual config below.

<details>
<summary>Manual JSON config (advanced)</summary>

If you can't use the MCPB bundle (older Claude Desktop, unsupported platform), install the MCP binary and configure it manually.


Install the MCP binary from this CLI's published public-library entry or pre-built release.

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "gdelt": {
      "command": "gdelt-pp-mcp"
    }
  }
}
```

</details>

## Health Check

```bash
gdelt-pp-cli doctor
```

Verifies configuration and connectivity to the API.

## Configuration

Config file: `~/.config/gdelt-pp-cli/config.toml`

Static request headers can be configured under `headers`; per-command header overrides take precedence.

## Troubleshooting
**Not found errors (exit code 3)**
- Check the resource ID is correct
- Run the `list` command to see available items

### API-specific

- **Empty results for a query you expect hits on** — GDELT only covers the last ~3 months; widen --last or check the query operators with --print-query. Very fresh stories can lag the 15-minute crawl.
- **Hundreds of near-duplicate rows of the same wire story** — use `today`/`brief` instead of raw `news` — they dedup syndicated copies and report also_in domains; or add --dedup to `news`.
- **HTTP 429 / slow responses** — GDELT's v2 APIs are interactive-scale and lightly rate-limited; space out calls, prefer `gdelt search` (offline, hits the local store) for re-queries, and let `sync` run on a modest interval.
- **startdatetime rejected** — use YYYYMMDDHHMMSS (or YYYYMMDD) and keep it within the last 3 months; you can't query older than that via this API.
- **`today` returns 0 articles, but other tools show recent coverage** — GDELT rejects keywords shorter than 3 characters (so a bare `AI` token fails). Quote it as a phrase (`"AI"`), expand to `"artificial intelligence"`, or combine with longer terms (`AI AND chatbot`). The CLI surfaces an empty list when this happens; running `--print-query` shows the compiled query for inspection.
- **Empty results from a multi-phrase OR pillar (e.g. `"EU AI Act" OR "AB 1988" OR "ISO 42001"`)** — GDELT silently rejects queries that OR several quoted short/all-caps phrases together, even when each phrase alone returns hits. Split into one pillar per phrase and pull them sequentially; the per-pillar volume is the same and downstream dedup/diff composes them.

---

## Known Gaps

This first cut intentionally ships the headline workflow end-to-end (the "news on a topic from today, around the world" use case) plus saved pillars, and defers the deeper local-store features to a follow-up:

- **`since <window>`** — returning only articles first seen since a given window. Requires `first_seen_at` per article in the SQLite store; today's pulls already cache articles via the generated `sync`/store layer, but the diff command isn't wired yet.
- **`sync` of saved pillars** — `sync` currently mirrors the API resources (the generator-emitted shape). A `sync --pillars` mode that walks every saved pillar in one shot is the next step.
- **`brief <pillar>`** — the compact JSON digest (top-N deduped articles + volume Δ + tone Δ + top source countries). Needs the per-pillar pull history that `sync --pillars` populates.
- **`heat`** — ranking tracked pillars by volume change this window vs last. Needs the same history.
- **`search`** — FTS5 over every article ever pulled. Schema is partially in place via the generated store; the command-level wiring is the work.

For now: use `gdelt-pp-cli today "<topic>"` for one-off pulls and `gdelt-pp-cli pillar pull <name>` for recurring topic monitoring. Both already produce the JSON shape downstream consumers expect.

---

## Sources & Inspiration

This CLI was built by studying these projects and resources:

- [**dipankar/gdelt-cli**](https://github.com/dipankar/gdelt-cli) — Go
- [**alex9smith/gdelt-doc-api**](https://github.com/alex9smith/gdelt-doc-api) — Python
- [**MissionSquad/mcp-gdelt**](https://github.com/MissionSquad/mcp-gdelt) — TypeScript
- [**linwoodc3/gdeltPyR**](https://github.com/linwoodc3/gdeltPyR) — Python

Generated by [CLI Printing Press](https://github.com/mvanhorn/cli-printing-press)
