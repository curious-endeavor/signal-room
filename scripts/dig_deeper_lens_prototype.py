#!/usr/bin/env python3
"""
Prototype the Dig Deeper four-lens query suggester against fixture results.

Validates whether the LLM produces genuinely abstracted reframings (e.g., the
Character AI / children result yielding `children chatbots` in the Abstract
slot) before any UI is built.

Usage:
    OPENAI_API_KEY=sk-... python3 scripts/dig_deeper_lens_prototype.py
    OPENAI_API_KEY=sk-... python3 scripts/dig_deeper_lens_prototype.py --only character_ai
    OPENAI_API_KEY=sk-... python3 scripts/dig_deeper_lens_prototype.py --json

Reads:  hardcoded FIXTURES below (no network for ingest).
Writes: prints lensed suggestions for each fixture.
Calls:  OpenAI Responses API, mirroring signal_room/title_enrichment.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import requests


DEFAULT_MODEL = "gpt-4.1-mini"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

LENS_DEFINITIONS = [
    {
        "key": "abstract",
        "label": "Abstract",
        "intent": (
            "The broader category this result is an instance of. "
            "Pull out the underlying mechanism or pattern, not the literal "
            "entities. Example: a result about Character AI bots and children "
            "should yield `children chatbots`, NOT `Character AI researchers`."
        ),
    },
    {
        "key": "narrow",
        "label": "Narrow",
        "intent": (
            "Zoom in on this specific platform, timeframe, or incident. "
            "Add a constraint that makes the search more precise to this "
            "exact case (platform name + year, specific incident type, etc)."
        ),
    },
    {
        "key": "sideways",
        "label": "Sideways",
        "intent": (
            "Same underlying mechanism, but applied to a DIFFERENT platform, "
            "population, or context. Keep the dynamic, swap the surface."
        ),
    },
    {
        "key": "counter",
        "label": "Counter",
        "intent": (
            "Find evidence for the opposing position. If the result is about "
            "harms, suggest a query for benefits, or vice versa. Aim for a "
            "query that would surface the steelman of the other side."
        ),
    },
]


@dataclass(frozen=True)
class Fixture:
    key: str
    parent_query: str
    title: str
    summary: str
    source: str


FIXTURES = (
    Fixture(
        key="character_ai",
        parent_query="AI safety this week",
        title=(
            "A group of researchers spent 50 hours talking to 50 Character AI "
            "bots posing as children … let's talk about the findings"
        ),
        summary=(
            "A group of researchers spent 50 hours talking to Character AI bots "
            "posing as children. What they found will shock you. Across the 50 "
            "hours of interaction with 50 different bots, researchers logged "
            "669 harmful interactions."
        ),
        source="Instagram",
    ),
    Fixture(
        key="solo_founder_burnout",
        parent_query="indie hacker mental health",
        title="I hit $12K MRR solo and I have never been more depressed",
        summary=(
            "Posting anonymously. Built my SaaS for 3 years, finally cracked "
            "$12K MRR last month. I should be celebrating but I cannot get out "
            "of bed. Talking to founder friends, this seems weirdly common at "
            "this stage. No one warned me about this."
        ),
        source="Reddit r/indiehackers",
    ),
    Fixture(
        key="ai_coding_layoffs",
        parent_query="ai jobs market 2026",
        title="Big tech laid off 40% of junior devs in Q1 — internal memo leaked",
        summary=(
            "Internal memo from a FAANG company suggests aggressive cuts to "
            "junior engineering headcount, citing Copilot and Cursor "
            "productivity gains. Senior IC and staff+ roles untouched. Memo "
            "circulating on Blind."
        ),
        source="X",
    ),
)


SYSTEM_PROMPT = (
    "You generate follow-up search query suggestions for a discovery tool "
    "called Signal Room. The user has just spotted an interesting result "
    "during a search and wants to drill deeper into the underlying thread.\n\n"
    "Given a single result (title + snippet) and the originating query, "
    "produce ONE query suggestion per lens, where each lens has a distinct "
    "intent.\n\n"
    "CRITICAL RULES:\n"
    "1. Suggestions are SEARCH QUERIES, not questions or sentences. Aim for "
    "2-6 words. Concrete, googleable phrases.\n"
    "2. The Abstract lens is the most important. It must surface the "
    "BROADER CATEGORY, not the literal entities. If you only restate the "
    "specific platform or person, you have failed the Abstract lens.\n"
    "3. Do not repeat the originating query. Each suggestion must open a "
    "different angle.\n"
    "4. Avoid jargon-heavy or speculative phrasings. Use the language a "
    "thoughtful operator would actually type into a search box.\n"
    "5. Return ONLY valid JSON in the exact shape requested."
)


def build_user_payload(fixture: Fixture) -> dict[str, Any]:
    return {
        "originating_query": fixture.parent_query,
        "result": {
            "title": fixture.title,
            "summary": fixture.summary,
            "source": fixture.source,
        },
        "lenses": [
            {"key": lens["key"], "label": lens["label"], "intent": lens["intent"]}
            for lens in LENS_DEFINITIONS
        ],
        "return_shape": {
            "abstract": "search query string",
            "narrow": "search query string",
            "sideways": "search query string",
            "counter": "search query string",
        },
    }


def request_lens_suggestions(api_key: str, fixture: Fixture, model: str) -> dict[str, str]:
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(build_user_payload(fixture), ensure_ascii=True),
                    }
                ],
            },
        ],
        "temperature": 0.4,
    }

    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=int(os.environ.get("DIG_DEEPER_TIMEOUT", "35")),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenAI returned HTTP {response.status_code}: {response.text[:300]}"
        )

    body = response.json()
    text = _response_text(body)
    if not text:
        raise RuntimeError("OpenAI returned no text")

    parsed = json.loads(_strip_json_fence(text))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected object, got {type(parsed).__name__}")

    suggestions: dict[str, str] = {}
    for lens in LENS_DEFINITIONS:
        value = parsed.get(lens["key"])
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Missing or empty suggestion for lens '{lens['key']}'")
        suggestions[lens["key"]] = value.strip()
    return suggestions


def _response_text(body: dict[str, Any]) -> str:
    output_text = body.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()

    chunks: list[str] = []
    for output in body.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1).strip() if match else stripped


LENS_ICONS = {"abstract": "⤴", "narrow": "🔬", "sideways": "↔", "counter": "⇄"}


def render_pretty(fixture: Fixture, suggestions: dict[str, str]) -> str:
    lines: list[str] = []
    rule = "─" * 68
    lines.append(rule)
    lines.append(f"FIXTURE: {fixture.key}")
    lines.append(f"  parent query : {fixture.parent_query}")
    lines.append(f"  source       : {fixture.source}")
    lines.append(f"  title        : {fixture.title}")
    lines.append(rule)
    for lens in LENS_DEFINITIONS:
        icon = LENS_ICONS[lens["key"]]
        suggestion = suggestions[lens["key"]]
        lines.append(f"  {icon} {lens['label']:<9} {suggestion}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        help="Run a single fixture by key (e.g., character_ai)",
        default=None,
    )
    parser.add_argument(
        "--model",
        help="Override the OpenAI model (defaults to gpt-4.1-mini, or $SIGNAL_ROOM_LENS_MODEL)",
        default=None,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a pretty report",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("error: OPENAI_API_KEY is not set", file=sys.stderr)
        return 2

    model = (
        args.model
        or os.environ.get("SIGNAL_ROOM_LENS_MODEL")
        or os.environ.get("OPENAI_MODEL_PIN")
        or DEFAULT_MODEL
    )

    fixtures = FIXTURES
    if args.only:
        fixtures = tuple(f for f in FIXTURES if f.key == args.only)
        if not fixtures:
            print(f"error: no fixture with key '{args.only}'", file=sys.stderr)
            print(f"available: {', '.join(f.key for f in FIXTURES)}", file=sys.stderr)
            return 2

    results: list[dict[str, Any]] = []
    failed = 0
    for fixture in fixtures:
        try:
            suggestions = request_lens_suggestions(api_key, fixture, model)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if args.json:
                results.append({"fixture": fixture.key, "error": str(exc)})
            else:
                print(f"FIXTURE {fixture.key}: FAILED — {exc}\n", file=sys.stderr)
            continue

        if args.json:
            results.append(
                {
                    "fixture": fixture.key,
                    "parent_query": fixture.parent_query,
                    "title": fixture.title,
                    "suggestions": suggestions,
                }
            )
        else:
            print(render_pretty(fixture, suggestions))

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
