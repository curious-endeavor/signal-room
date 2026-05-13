---
name: pp-gdelt
description: "Search worldwide news on any topic, track named pillars, and see what's new since last check — backed by a local... Trigger phrases: `news on a topic today`, `what's the world saying about`, `pull live gdelt`, `any news on`, `monitor this pillar`, `what's new since last check on`, `use gdelt`, `run gdelt`."
author: "142"
license: "Apache-2.0"
argument-hint: "<command> [args] | install cli|mcp"
allowed-tools: "Read Bash"
metadata:
  openclaw:
    requires:
      bins:
        - gdelt-pp-cli
---

# GDELT — Printing Press CLI

## Prerequisites: Install the CLI

This skill drives the `gdelt-pp-cli` binary. **You must verify the CLI is installed before invoking any command from this skill.** If it is missing, install it first:

1. Install via the Printing Press installer:
   ```bash
   npx -y @mvanhorn/printing-press install gdelt --cli-only
   ```
2. Verify: `gdelt-pp-cli --version`
3. Ensure `$GOPATH/bin` (or `$HOME/go/bin`) is on `$PATH`.

If the `npx` install fails before this CLI has a public-library category, install Node or use the category-specific Go fallback after publish.

If `--version` reports "command not found" after install, the install step did not put the binary on `$PATH`. Do not proceed with skill commands until verification succeeds.

Wraps GDELT's keyless DOC 2.0 API (worldwide online news, 65+ languages, rolling 3-month window, 15-minute updates) and adds the part GDELT doesn't have: persistence. Every pull lands in a local SQLite store, so `today` gives you a topic's last 24 hours deduped and agent-ready, `since` returns only what's new, `pillar`/`sync` turn topics into named saved queries you refresh on a cron, and `brief`/`heat` turn the store into the digest a signal feed actually ingests.

## When to Use This CLI

Reach for this CLI when you need worldwide news coverage on a topic — broader than English-language Google News, across 65+ languages — and especially when the job is recurring topic monitoring: defining pillars, refreshing them on a schedule, and feeding only the new items into a signal feed or brief. It is an additional source alongside social-search tools, not a replacement; use it when 'who in the world is reporting on X, and what's new since last time' is the question.

## When Not to Use This CLI

Do not activate this CLI for requests that require creating, updating, deleting, publishing, commenting, upvoting, inviting, ordering, sending messages, booking, purchasing, or changing remote state. This printed CLI exposes read-only commands for inspection, export, sync, and analysis.

## Unique Capabilities

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

## Command Reference

**news** — Search worldwide news articles (GDELT DOC 2.0 artlist)

- `gdelt-pp-cli news <query>` — Search news articles matching a query across 65+ languages over a rolling 3-month window

**timeline** — Coverage-volume timelines for a query (GDELT DOC 2.0 timeline modes)

- `gdelt-pp-cli timeline bycountry` — Coverage volume over time broken down by source country
- `gdelt-pp-cli timeline bylang` — Coverage volume over time broken down by source language
- `gdelt-pp-cli timeline tone` — Average article tone (sentiment) over time
- `gdelt-pp-cli timeline volraw` — Coverage volume over time as raw article counts (and the all-articles denominator)
- `gdelt-pp-cli timeline volume` — Coverage volume over time as a percent of all monitored articles

**tonechart** — Histogram of article tone for a query (GDELT DOC 2.0 tonechart)

- `gdelt-pp-cli tonechart <query>` — Distribution of article tone (how many articles fall in each tone bucket)


### Finding the right command

When you know what you want to do but not which command does it, ask the CLI directly:

```bash
gdelt-pp-cli which "<capability in your own words>"
```

`which` resolves a natural-language capability query to the best matching command from this CLI's curated feature index. Exit code `0` means at least one match; exit code `2` means no confident match — fall back to `--help` or use a narrower query.

## Recipes


### Daily pillar pull for a signal feed

```bash
gdelt-pp-cli sync --last 24h && gdelt-pp-cli since 1d --json --select articles.title,articles.url,articles.domain,articles.sourcecountry,articles.seendate
```

Refresh every pillar, then emit just the new articles with the fields a feed needs — run this on a cron.

### Brief one pillar for a report

```bash
gdelt-pp-cli brief child-safety-ai --last 7d --top 10 --json
```

One JSON object: top deduped articles, volume change, tone change, top source countries — drop it straight into a PR brief.

### Where is a story breaking?

```bash
gdelt-pp-cli countries "ai chatbot regulation" --last 7d --json
```

Per-country coverage volume — see which countries are driving the story before it hits the US wires.

### Narrow a verbose response for an agent

```bash
gdelt-pp-cli today "ai safety" --json --select articles.title,articles.url,articles.domain,articles.sourcecountry --max 50
```

GDELT artlist rows carry fields an agent rarely needs; --select keeps token cost down by projecting just the columns that matter.

### Check tone trajectory

```bash
gdelt-pp-cli tone "character.ai" --last 30d --smooth 5
```

Average article tone over 30 days with smoothing — spot when sentiment turned.

## Auth Setup

No authentication required.

Run `gdelt-pp-cli doctor` to verify setup.

## Agent Mode

Add `--agent` to any command. Expands to: `--json --compact --no-input --no-color --yes`.

- **Pipeable** — JSON on stdout, errors on stderr
- **Filterable** — `--select` keeps a subset of fields. Dotted paths descend into nested structures; arrays traverse element-wise. Critical for keeping context small on verbose APIs:

  ```bash
  gdelt-pp-cli news mock-value --agent --select id,name,status
  ```
- **Previewable** — `--dry-run` shows the request without sending
- **Offline-friendly** — sync/search commands can use the local SQLite store when available
- **Non-interactive** — never prompts, every input is a flag
- **Read-only** — do not use this CLI for create, update, delete, publish, comment, upvote, invite, order, send, or other mutating requests

### Response envelope

Commands that read from the local store or the API wrap output in a provenance envelope:

```json
{
  "meta": {"source": "live" | "local", "synced_at": "...", "reason": "..."},
  "results": <data>
}
```

Parse `.results` for data and `.meta.source` to know whether it's live or local. A human-readable `N results (live)` summary is printed to stderr only when stdout is a terminal — piped/agent consumers get pure JSON on stdout.

## Agent Feedback

When you (or the agent) notice something off about this CLI, record it:

```
gdelt-pp-cli feedback "the --since flag is inclusive but docs say exclusive"
gdelt-pp-cli feedback --stdin < notes.txt
gdelt-pp-cli feedback list --json --limit 10
```

Entries are stored locally at `~/.gdelt-pp-cli/feedback.jsonl`. They are never POSTed unless `GDELT_FEEDBACK_ENDPOINT` is set AND either `--send` is passed or `GDELT_FEEDBACK_AUTO_SEND=true`. Default behavior is local-only.

Write what *surprised* you, not a bug report. Short, specific, one line: that is the part that compounds.

## Output Delivery

Every command accepts `--deliver <sink>`. The output goes to the named sink in addition to (or instead of) stdout, so agents can route command results without hand-piping. Three sinks are supported:

| Sink | Effect |
|------|--------|
| `stdout` | Default; write to stdout only |
| `file:<path>` | Atomically write output to `<path>` (tmp + rename) |
| `webhook:<url>` | POST the output body to the URL (`application/json` or `application/x-ndjson` when `--compact`) |

Unknown schemes are refused with a structured error naming the supported set. Webhook failures return non-zero and log the URL + HTTP status on stderr.

## Named Profiles

A profile is a saved set of flag values, reused across invocations. Use it when a scheduled agent calls the same command every run with the same configuration - HeyGen's "Beacon" pattern.

```
gdelt-pp-cli profile save briefing --json
gdelt-pp-cli --profile briefing news mock-value
gdelt-pp-cli profile list --json
gdelt-pp-cli profile show briefing
gdelt-pp-cli profile delete briefing --yes
```

Explicit flags always win over profile values; profile values win over defaults. `agent-context` lists all available profiles under `available_profiles` so introspecting agents discover them at runtime.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Usage error (wrong arguments) |
| 3 | Resource not found |
| 5 | API error (upstream issue) |
| 7 | Rate limited (wait and retry) |
| 10 | Config error |

## Argument Parsing

Parse `$ARGUMENTS`:

1. **Empty, `help`, or `--help`** → show `gdelt-pp-cli --help` output
2. **Starts with `install`** → ends with `mcp` → MCP installation; otherwise → see Prerequisites above
3. **Anything else** → Direct Use (execute as CLI command with `--agent`)

## MCP Server Installation

Install the MCP binary from this CLI's published public-library entry or pre-built release, then register it:

```bash
claude mcp add gdelt-pp-mcp -- gdelt-pp-mcp
```

Verify: `claude mcp list`

## Direct Use

1. Check if installed: `which gdelt-pp-cli`
   If not found, offer to install (see Prerequisites at the top of this skill).
2. Match the user query to the best command from the Unique Capabilities and Command Reference above.
3. Execute with the `--agent` flag:
   ```bash
   gdelt-pp-cli <command> [subcommand] [args] --agent
   ```
4. If ambiguous, drill into subcommand help: `gdelt-pp-cli <command> --help`.
