"""LLM-generated GDELT pillar queries.

The naive `project_gdelt_pillars` in `from_brief.py` produces literal-phrase
queries like:
    ("ai branding" OR "brand strategy ai" OR "ai-native brand")

GDELT only matches articles containing those exact strings. Marketing /
creative-industry briefs that use jargon ("ai-native brand", "post-agency")
won't match anything because real news doesn't use those phrases.

This module asks Claude to translate each pillar into a GDELT-shaped boolean
query that combines:
  - Plain news vocabulary (lawsuit, layoff, launch, ruling, fine, fires)
  - Named entities the brief cares about (companies, regulations, products)
  - Broader category words required, narrower differentiators optional

Cached by content hash so we only pay once per brief change.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import requests

# The system prompt isn't huge but caching it is still cheaper than not.
GDELT_QUERY_SYSTEM_PROMPT = """You are translating a brand's pillar keyword lists into GDELT-shaped boolean queries that maximize the chance of matching real news articles in GDELT DOC 2.0.

# GDELT query syntax
- Phrase quoting: "exact phrase" requires the exact string to appear
- AND, OR (uppercase only)
- Parentheses for grouping
- You can also use raw single-word tokens unquoted (they match as substrings)

# Heuristic: every good GDELT query has THREE layers
1. A REQUIRED entity layer — broad category words OR named entities the brand cares about. Examples: ("agency" OR "creative studio" OR "Monks" OR "DixonBaxi"), or ("chatbot" OR "AI agent" OR "AI assistant").
2. A REQUIRED modifier layer — the technology / context that makes it relevant. Examples: ("AI" OR "generative" OR "agentic"), or ("EU AI Act" OR "Article 12" OR "AB 1988").
3. An OPTIONAL action / news verb layer — what would make this NEWS not commentary. Examples: ("lawsuit" OR "sues" OR "fine" OR "ruling" OR "launch" OR "announces" OR "layoff" OR "restructure" OR "breach" OR "incident").

Combine with AND between layers, OR within each layer.

# Rules
- Lean toward broader category words over the brand's jargon. "agency" + "AI" + "layoff" matches dozens of news articles per week. "ai-native brand strategy" matches zero.
- Include named entities the brand explicitly cares about (competitors, regulations, products from the pillar's keywords/why).
- Each query should be 30-150 characters. Too short = noisy; too long = matches nothing.
- News verbs in the third layer are what separate "story" from "blog post". Always include at least 4 of them.
- Name the pillar with a kebab-case slug — alphanumerics and hyphens only.

# Output schema
Return ONLY this JSON object. No markdown fences, no commentary.

{
  "pillars": [
    {"name": "<kebab-case-slug>", "query": "<GDELT boolean query>"},
    ...
  ]
}

One pillar per pillar in the input brief. Preserve input order."""


def _stable_hash(payload: dict) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _cache_signature(brief: dict, brand: str) -> dict:
    """The minimum set of inputs that, when they change, mean the generated
    queries are stale. We deliberately EXCLUDE discovery_queries +
    seed_sources — they don't change what GDELT should look for."""
    sr = ((brief.get("projection") or {}).get("signal_room") or {})
    return {
        "brand": brand,
        "name": (brief.get("brand") or {}).get("name") if isinstance(brief.get("brand"), dict) else brief.get("name"),
        "url": (brief.get("brand") or {}).get("url") if isinstance(brief.get("brand"), dict) else brief.get("url"),
        "one_liner": (brief.get("brand") or {}).get("one_liner") if isinstance(brief.get("brand"), dict) else brief.get("one_liner"),
        "pillars": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "why": p.get("why", "")[:400],
                "keywords": p.get("keywords") or [],
            }
            for p in (sr.get("pillars") or [])
            if isinstance(p, dict)
        ],
    }


def _build_user_message(brief: dict, brand: str) -> str:
    sig = _cache_signature(brief, brand)
    pillars_text = "\n\n".join(
        f"Pillar {i + 1}: {p.get('name') or p.get('id') or 'unnamed'}\n"
        f"  why: {p.get('why', '')}\n"
        f"  brief keywords: {', '.join(p.get('keywords') or [])}"
        for i, p in enumerate(sig["pillars"])
    )
    return (
        f"BRAND: {sig.get('name') or brand} ({sig.get('url') or ''})\n"
        f"One-liner: {sig.get('one_liner') or '(none)'}\n\n"
        f"PILLARS:\n{pillars_text}\n\n"
        f"Return the JSON object now."
    )


def _call_claude(system_prompt: str, user_message: str, model: str = "claude-sonnet-4-6") -> dict:
    from ..llm_scoring import _get_api_key
    api_key = _get_api_key()
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
            # The system prompt repeats verbatim across pillars / brands and is
            # the bulkiest input — cache it.
            "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=90,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    j_start = text.find("{")
    j_end = text.rfind("}")
    if j_start == -1 or j_end == -1:
        raise ValueError(f"no JSON object in GDELT-query response: {text[:300]}")
    return json.loads(text[j_start:j_end + 1])


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _sanitize(payload: dict) -> dict:
    """Lock down the shape + slug-ify names so the result is safe to drop on
    disk and feed to gdelt-pp-cli."""
    out = []
    for entry in (payload.get("pillars") or []):
        if not isinstance(entry, dict):
            continue
        name = _SLUG_RE.sub("-", (entry.get("name") or "").lower()).strip("-")
        query = (entry.get("query") or "").strip()
        if not name or not query:
            continue
        out.append({"name": name[:64], "query": query})
    return {"pillars": out}


def generate_gdelt_pillars(
    brief: dict,
    brand: str,
    cache_path: Optional[Path] = None,
    model: str = "claude-sonnet-4-6",
    force: bool = False,
) -> Dict[str, Any]:
    """Returns {pillars: [{name, query}, ...]}. Caches by content hash so we
    only call Claude when the brief's pillars actually change.

    Raises on Claude failure — caller decides whether to fall back to the
    keyword projection. (Worker today logs a warning and uses the dumb
    projection as a safety net.)
    """
    sig = _cache_signature(brief, brand)
    sig_hash = _stable_hash(sig)
    if cache_path is not None and not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("sig_hash") == sig_hash and cached.get("pillars"):
                return {"pillars": cached["pillars"]}
        except Exception:
            # Corrupt cache — fall through to regenerate.
            pass

    user_message = _build_user_message(brief, brand)
    raw = _call_claude(GDELT_QUERY_SYSTEM_PROMPT, user_message, model=model)
    sanitized = _sanitize(raw)

    if cache_path is not None and sanitized.get("pillars"):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"sig_hash": sig_hash, "model": model, "pillars": sanitized["pillars"]},
                indent=2,
            ),
            encoding="utf-8",
        )
    return sanitized
