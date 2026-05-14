# Dig Deeper — Fractal Search From an Interesting Result

**Date:** 2026-05-07
**Status:** Requirements draft
**Owner:** Dan
**Related:** `signal_room/web.py`, `signal_room/worker.py`, `signal_room/web_store.py`

## Problem

Today, when a Signal Room result catches the user's eye (e.g., the Instagram post about "researchers spending 50 hours talking to Character AI bots posing as children"), there is no path forward. The user has to manually open a new tab, type a fresh query into Signal Room, and guess at what reframing would surface more on the underlying thread. The interesting result is a dead end instead of an entry point.

The user wants to "go down the path" — from a single juicy result, fan out into adjacent queries that explore the same fractal of meaning, without losing where they came from.

## Goal

Let any result on a run page become the seed for one or more new runs, with high-quality follow-up query suggestions surfaced in-place. The original run stays open; new runs spawn as new tabs; each new run remembers its parent.

## Users and Use

The user is Dan, running Signal Room interactively in the web UI to find mechanism-rich signals for Curious Endeavor content, client thinking, and pitch references. The drilling pattern is:

1. Run a query, scan results.
2. Spot one that hints at a broader category, an adjacent population, or an opposing angle.
3. Want 3–5 follow-up query ideas — not literal entity extractions, but **abstracted reframings** (the canonical example: a result about Character AI + children should suggest `children chatbots` as a broader category).
4. Pick one (or several), explore in parallel, keep digging.

## Success Criteria

- From any result card on a run page, the user can open a "Dig deeper" drawer in one click.
- The drawer presents exactly four typed query suggestions (one per lens) generated from the result's title + snippet + the originating run's query.
- Each suggestion is editable inline before firing.
- Clicking "Run →" on a suggestion spawns a **new browser tab** with a new run; the original tab is untouched.
- The new run's page shows a back-link breadcrumb (`↳ from: <parent result title> · lens: <lens name>`) at the top.
- For runs 2+ levels deep, the breadcrumb also shows the root run.
- The drawer can fire multiple lenses from the same result (each into its own new tab).
- The Character AI / children example produces `children chatbots` (or near-equivalent) in the Abstract slot when tested against the canonical fixture.

## Behavior

### The "Dig deeper" affordance

On every result card on a run page, alongside `Create Content` and `Thumbs down`, add a third button: `🔎 Dig deeper`.

Clicking it expands a drawer **inline** under the result card (does not navigate, does not open a modal). If suggestions are not yet generated for this item, the drawer shows a loading state and fires a request to the suggestion endpoint. Once received, suggestions render. Subsequent clicks on the same item's button re-open the drawer with cached suggestions instantly.

### The four lenses

| Lens | Intent | Example for the Character AI result |
| --- | --- | --- |
| **Abstract** ⤴ | The broader category this is an instance of | `children chatbots` |
| **Narrow** 🔬 | Zoom in on this specific platform/timeframe/incident | `Character AI safety incidents 2025` |
| **Sideways** ↔ | Same mechanism, adjacent population or platform | `AI companions and teen mental health` |
| **Counter** ⇄ | Find the opposing evidence | `AI chatbot education benefits for kids` |

Each lens displays:
- The lens label and icon
- The suggested query (editable text input, pre-filled)
- A short one-line description of the lens intent
- A `Run →` button

### Spawning a new run

Clicking `Run →` on a lens:

1. POSTs to `/runs` with the (possibly edited) query, the existing default sources, the same `lookback_days` as the parent run, plus `parent_run_id`, `parent_item_id`, `parent_lens`.
2. Receives the new run id.
3. Opens `/runs/<new_run_id>` in a new browser tab via `window.open(url, '_blank')`.
4. Drawer stays open in the original tab so the user can immediately fire another lens.

No confirmation toast, no focus change.

### The breadcrumb on a child run

At the top of any run page where `parent_run_id` is set, render a breadcrumb strip:

```
↳ from:  <parent item title>
  lens:  <ABSTRACT | NARROW | SIDEWAYS | COUNTER>
  ← back to "<parent run query>"
```

If the parent run itself has a parent (depth ≥ 2), also show:

```
⤴ root: "<root run query>"
```

The "back to" and "root" links navigate within the same tab.

## Scope Boundaries

### In scope (v1)

- Per-result suggestion drawer with four fixed lenses
- One LLM call per result on first drawer open; cached for the run's lifetime
- New runs open in a new browser tab via `target="_blank"` / `window.open`
- Parent run / item / lens stored on each run; breadcrumb rendered when present
- Edit-before-run on each lens suggestion

### Deferred for later

- Full tree / graph navigation UI (browser tabs serve as the tree for v1)
- Sharing or exporting a fractal path as a single artifact
- "Run all four lenses at once" button
- User-configurable lens definitions
- Suggestion quality feedback (thumbs up/down on individual suggestions)
- Cross-run result deduplication

### Outside this product's identity

- General-purpose web search query suggester (Signal Room is for mechanism-rich social signals, not arbitrary research)
- Conversational chat with the result content
- Summarization of result content (the existing snippet is the snippet)

## Dependencies and Assumptions

- An LLM is callable from the worker / web layer with a JSON-mode response. The existing `title_enrichment.clean_result_titles` call shows OpenAI integration is already in the project — assume the same client and model tier are available for the suggester. **Unverified:** confirm the OpenAI client wrapper is reachable from the web request path, not only from the worker.
- The existing `web_store.SignalRoomStore` schema can be extended to add `parent_run_id`, `parent_item_id`, `parent_lens` columns to runs. **Unverified:** confirm runs are stored in a writable schema (sqlite or similar) and there is a migration pattern to follow.
- The existing run-creation endpoint accepts `query`, `sources`, `lookback_days`. New parent fields should be additive optional fields on the same endpoint, not a separate endpoint.
- Result items have a stable id within a run (`item_id`) that survives page reloads.
- The Signal Room web UI is a single user (Dan), so suggestion caching can live in process / per-run JSON without any per-user partitioning.

## Open Questions

1. **Caching scope:** should the suggestions for a given result be cached forever (once generated, never regenerated) or invalidated after some window? Default v1 = forever per run; if the user wants fresher angles later, they can fire from a re-run.
2. **Lens prompt brittleness:** if the LLM returns a sub-quality suggestion for one lens (e.g., literal entity extraction in the Abstract slot), is there a re-roll affordance, or does the user just edit it inline? Default v1 = edit inline; add re-roll only if it becomes a pain point.
3. **Sources for child runs:** should child runs inherit the parent's sources, or always use defaults? Default v1 = inherit parent's sources.

## What This Document Does Not Cover

- Database column types, migration files, exact endpoint paths and payload shapes
- Frontend component file layout, CSS, drawer animation
- LLM prompt text and model selection
- Test plan

Those belong in the planning step (`/ce-plan`), which can use this document as its product input without needing to invent any user-facing behavior.
