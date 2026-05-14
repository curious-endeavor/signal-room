"""Brand-aware query planner for /last30days.

The vendor's internal planner (grok-4-1-fast) frequently returns malformed JSON
and falls back to a deterministic single-subquery plan with uniform 1/7 source
weights and no entity resolution. That noise-prone path is what brand-chatbot
hit on Alice's 2026-05-13 run.

This module is Signal Room's own planner: it reads the brand brief (full file)
plus a single discovery-query (topic + why) and calls Claude to emit a
QueryPlan-shaped dict that /last30days will accept via `--plan <json>` and
skip its internal planner entirely.

QueryPlan schema (from vendor/last30days-skill/scripts/lib/schema.py):
  intent        : str ("concept" | "comparison" | "person" | "entity" | ...)
  freshness_mode: str ("strict_recent" | "evergreen_ok" | ...)
  cluster_mode  : str ("none" | "comparison" | ...)
  raw_topic     : str
  subqueries    : [{label, search_query, ranking_query, sources, weight}]
  source_weights: {source: weight}
  notes         : [str]

Usage:
  python3 -m signal_room.planner \\
      --brief config/brands/alice/brief.yaml \\
      --query brand-chatbot-incidents
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml


AVAILABLE_SOURCES = ["grounding", "x", "youtube", "instagram", "github", "reddit", "hackernews"]


def _get_api_key() -> str:
    if k := os.environ.get("ANTHROPIC_API_KEY"):
        return k
    home = Path(os.path.expanduser("~"))
    for env_path in [
        home / ".config" / "last30days" / ".env",
        home / ".config" / "anthropic" / ".env",
    ]:
        try:
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except (OSError, PermissionError):
            # File exists but unreadable (e.g. Render's vendor mount).
            # Don't crash the planner — try the next fallback / raise below.
            continue
    raise RuntimeError(
        "ANTHROPIC_API_KEY not found. Set it as an environment variable in the "
        "service settings (both web and worker on Render) or in ~/.config/last30days/.env"
    )


def _build_system_prompt(brief_text: str, available_sources: List[str]) -> str:
    return f"""You are the query planner for a brand-tuned news/social discovery pipeline. You read the brand brief and a single discovery query, and you emit a structured JSON plan that the downstream fetcher uses to search 7 providers (grounding, x, youtube, instagram, github, reddit, hackernews).

Your job is to MAXIMIZE press-worthy, on-territory signal for THIS brand on THIS query — not generic recall.

# Brand brief (full file)

{brief_text}

# Available sources

{', '.join(available_sources)}

# QueryPlan schema you must return

Return ONLY a JSON object with this exact shape. No markdown fences, no commentary.

{{
  "intent": "concept" | "comparison" | "entity" | "person" | "event",
  "freshness_mode": "strict_recent" | "evergreen_ok",
  "cluster_mode": "none" | "comparison",
  "raw_topic": <verbatim original topic string>,
  "subqueries": [
    {{
      "label": "<short descriptor, e.g. 'lawsuit-news' or 'enterprise-deployments'>",
      "search_query": "<the exact string sent to search engines — short, keyword-shaped>",
      "ranking_query": "<the question used to rerank results — longer, natural language>",
      "sources": ["grounding", "reddit", ...],
      "weight": <float, 0–1, total across subqueries roughly 1.0>
    }}
  ],
  "source_weights": {{"grounding": 0.30, "reddit": 0.15, ...}},
  "notes": ["<short note about the planning decisions>"]
}}

# Rules

1. **3–4 subqueries.** Decompose the topic into distinct angles that surface different signal. Don't generate 1 subquery (that's the noisy fallback path). 4 is the sweet spot; 5 is fine if the topic genuinely benefits.

2. **Different `search_query` per subquery.** Each should keyword-rephrase the topic for a different lens (news-cycle, technical, community discourse, lawsuits, regulatory). The `ranking_query` is a richer prose version used internally for reranking.

3. **Per-subquery `sources` is a SUBSET that fits.** Lawsuit news → grounding + x + reddit. Technical research → github + grounding + hackernews. Community discourse → reddit + x + youtube. NEVER include all 7 unless the query genuinely fits all 7.

4. **`source_weights` should reflect where the brand's audience actually reads this kind of signal.** Read the brief to figure that out. For press-worthy brand news in an enterprise category: grounding (news/web) and x usually dominate. For developer-shaped signals: github + hackernews. Reddit for community sentiment. youtube/instagram only when there's a real video/visual story.

5. **Weights sum approximately to 1.0** in both `subqueries[].weight` and `source_weights`. Don't worry about exact arithmetic — the vendor normalizes.

6. **`intent`** — pick carefully, it controls the vendor's subquery cap. Use:
   - `breaking_news` for news-cycle topics: lawsuits, incidents, funding rounds, enforcement deadlines, product launches, layoffs, IPOs, regulatory actions. **Unlocks up to 5 subqueries.**
   - `opinion` for discourse / sentiment tracking ("what people are saying about X"). Unlocks 5.
   - `product` for tracking a specific named product or tool. Unlocks 5.
   - `prediction` for forecasts and "what's coming" stories. Unlocks 5.
   - `entity` / `person` for tracking a specific named org or person. Unlocks 5.
   - `comparison` ONLY when explicitly comparing two named things — it triggers comparison-clustering side effects. Unlocks 4.
   - `concept` / `factual` are **LAST RESORT** — the vendor **caps these at 2 subqueries**. Avoid unless the topic genuinely has no news, discourse, product, or entity angle. Most brand-tracking queries are not concept-shaped — pick `breaking_news` or `opinion` instead.

7. **`freshness_mode`**: `strict_recent` when the question is time-bound (e.g., "this week", "in 2026"). `evergreen_ok` when older background content is acceptable.

8. **`cluster_mode`** is almost always `none`. Only use `comparison` when intent is `comparison`.

9. **`notes`** is a 1-line explanation of WHY you chose this decomposition. The downstream user reads this to understand your reasoning.

10. **Use the BRAND brief's vocabulary.** Pull specific phrases, named competitors, regulatory frameworks, and proper nouns from the brief into the search queries. Generic queries return generic results.

Return ONLY the JSON. Nothing else."""


def _ask_claude(system: str, user: str, api_key: str, model: str, max_retries: int = 4) -> Dict[str, Any]:
    backoff = 5
    last_text = ""
    for _ in range(max_retries):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1500,
                "temperature": 0.2,
                # System prompt embeds the full brief and is identical across
                # every discovery_query planned in a run. Cache it.
                "system": [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                "messages": [{"role": "user", "content": user}],
            },
            timeout=90,
        )
        if r.status_code == 429 or r.status_code >= 500:
            wait = int(r.headers.get("retry-after", backoff))
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
            continue
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        last_text = text
        break
    else:
        raise RuntimeError(f"max retries exceeded; last text:\n{last_text[:400]}")
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    j_start = text.find("{")
    j_end = text.rfind("}")
    if j_start == -1 or j_end == -1:
        raise ValueError(f"no JSON object found in response: {text[:300]}")
    return json.loads(text[j_start:j_end + 1])


def _validate_and_normalize(plan: Dict[str, Any], raw_topic: str) -> Dict[str, Any]:
    """Ensure the plan matches the vendor's QueryPlan schema. Coerce gently."""
    plan = dict(plan)
    plan.setdefault("raw_topic", raw_topic)
    plan.setdefault("intent", "concept")
    plan.setdefault("freshness_mode", "evergreen_ok")
    plan.setdefault("cluster_mode", "none")
    plan.setdefault("notes", [])

    subqueries = plan.get("subqueries") or []
    if not isinstance(subqueries, list) or not subqueries:
        raise ValueError("Plan must include at least one subquery; got none.")
    cleaned: List[Dict[str, Any]] = []
    for sq in subqueries:
        if not isinstance(sq, dict):
            continue
        label = str(sq.get("label") or "primary")
        sq_search = str(sq.get("search_query") or sq.get("ranking_query") or raw_topic)
        sq_ranking = str(sq.get("ranking_query") or sq_search)
        sources = sq.get("sources") or AVAILABLE_SOURCES
        sources = [s for s in sources if s in AVAILABLE_SOURCES]
        if not sources:
            sources = list(AVAILABLE_SOURCES)
        weight = float(sq.get("weight") or 1.0 / len(subqueries))
        if weight <= 0:
            weight = 1.0 / len(subqueries)
        cleaned.append({
            "label": label,
            "search_query": sq_search,
            "ranking_query": sq_ranking,
            "sources": sources,
            "weight": weight,
        })
    plan["subqueries"] = cleaned

    sw = plan.get("source_weights") or {}
    if not isinstance(sw, dict) or not sw:
        # Derive from subqueries if missing.
        sw = {s: 0.0 for s in AVAILABLE_SOURCES}
        for sq in cleaned:
            share = sq["weight"] / max(len(sq["sources"]), 1)
            for s in sq["sources"]:
                sw[s] = sw.get(s, 0.0) + share
    # Drop zeros and unknown keys; keep what the vendor expects.
    plan["source_weights"] = {k: float(v) for k, v in sw.items() if k in AVAILABLE_SOURCES and float(v) > 0}

    return plan


def plan_query(brief_path: Path, query: Dict[str, Any], model: str = "claude-sonnet-4-6") -> Dict[str, Any]:
    """Generate a QueryPlan dict for one discovery query, brand-aware."""
    brief_text = Path(brief_path).read_text(encoding="utf-8")
    system = _build_system_prompt(brief_text, AVAILABLE_SOURCES)
    raw_topic = str(query.get("topic") or query.get("search_text") or "")
    why = query.get("why", "")
    qid = query.get("id", "")
    user = (
        f"Discovery query to plan:\n\n"
        f"id: {qid}\n"
        f"topic: {raw_topic}\n"
        f"why we care: {why}\n\n"
        f"Return the JSON plan."
    )
    api_key = _get_api_key()
    plan = _ask_claude(system, user, api_key, model)
    plan = _validate_and_normalize(plan, raw_topic)
    plan.setdefault("notes", []).append(f"planner=signal-room ({model})")
    return plan


def _load_query_from_brief(brief_path: Path, query_id: str) -> Dict[str, Any]:
    brief = yaml.safe_load(Path(brief_path).read_text(encoding="utf-8")) or {}
    queries = (((brief.get("projection") or {}).get("signal_room") or {}).get("discovery_queries") or [])
    for q in queries:
        if isinstance(q, dict) and q.get("id") == query_id:
            return dict(q)
    raise SystemExit(f"query id {query_id!r} not found in {brief_path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signal_room.planner",
        description="Brand-aware QueryPlan generator for /last30days --plan.")
    parser.add_argument("--brief", required=True, type=Path, help="Path to brief.yaml")
    parser.add_argument("--query", required=True, help="Discovery query id (from brief)")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Planner model")
    parser.add_argument("--out", type=Path, default=None, help="Write JSON plan here (default: stdout)")
    args = parser.parse_args(argv)

    query = _load_query_from_brief(args.brief, args.query)
    plan = plan_query(args.brief, query, model=args.model)
    out_json = json.dumps(plan, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(out_json + "\n", encoding="utf-8")
        print(f"wrote plan → {args.out}", file=sys.stderr)
    else:
        print(out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
