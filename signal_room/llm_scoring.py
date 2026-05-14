"""LLM-based scorer.

Drop-in alternative to scoring.score_items. Reads a brand brief and a list of
RawItems, asks Claude to judge each item against the brief, and returns
ScoredItems shaped exactly like the keyword scorer's output so the rest of
the pipeline (digest rendering, storage) doesn't need to change.

Why this exists: keyword matching can't see when a story matches the SPIRIT
of a pillar without using the literal words. The brief uses brand-specific
phrases ("Make · Run", "hard cap", "CFO scenario") that real news rarely
uses verbatim. Claude reads the brief and the story and judges fit.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from .models import RawItem, ScoredItem
from .tracer import tracer


def _get_api_key() -> str:
    if k := os.environ.get("ANTHROPIC_API_KEY"):
        return k
    home = Path(os.path.expanduser("~"))
    # last30days env file — cross-platform: $HOME/.config first, then /root for server runs.
    env_candidates = [
        home / ".config" / "last30days" / ".env",
        Path("/root/.config/last30days/.env"),
        home / ".config" / "anthropic" / ".env",
        home / ".anthropic.env",
    ]
    for env_path in env_candidates:
        try:
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except (OSError, PermissionError):
            continue
    # Fallback: brand-audit secrets
    secrets_candidates = [
        home / "ce-research" / "secrets" / "secrets.env",
        Path("/root/ce-research/secrets/secrets.env"),
    ]
    for secrets_path in secrets_candidates:
        try:
            if secrets_path.exists():
                for line in secrets_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("ANTHROPIC_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except (OSError, PermissionError):
            continue
    # Last resort: OAuth token from Claude desktop (often expires)
    auth_candidates = [
        home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
        Path("/root/.openclaw/agents/main/agent/auth-profiles.json"),
    ]
    for auth_path in auth_candidates:
        if auth_path.exists():
            try:
                d = json.loads(auth_path.read_text(encoding="utf-8"))
                return d["profiles"]["anthropic:default"]["token"]
            except Exception:
                pass
    raise RuntimeError(
        "ANTHROPIC_API_KEY not found. Set it in env, or add a line "
        "'ANTHROPIC_API_KEY=sk-ant-...' to ~/.config/last30days/.env"
    )


def _build_system_prompt(brief_text: str) -> str:
    return f"""You are the triage scorer for a brand-tuned signal room. You receive a brand brief and a single news/content signal. Your job: judge fit, decide the right action, draft it.

# Brief

{brief_text}

# Voice — match these specimens, do not invent

The brand is Curious Endeavor. The named speaker is Assaf Dagan. Every drafted line must sound like the specimens below. Do not generalize from these — match the rhythm, length, register, and personal anchoring.

**Real specimens from Assaf's writing (essay mode):**

> "I was good at running the agency. I did not love it. I was not the only one who felt it. I was just stubborn enough to leave."

> "Eight arms. Ten times the battery."

> "Not brilliant. Average. The version that pleases everyone and surprises no one. That is exactly where I want to start."

> "Freeing my mind also made my hands more idle. I am not sure yet what to make of that."

> "The point is not that AI made it. The point is that a subject matter expert made it, and the system kept up."

> "Erotic art Flash ActionScript websites in Barcelona, through Audi, Diesel, Orangina and the Élysée's digital strategy in Paris to my own dream brand agency in New York City."

> "A designer is an improver, not a salesperson."

> "Creativity is a curious and unexpected endeavor."

**Real specimens from Assaf's correspondence mode (DMs, emails):**

> "Hey there! I am good here, life is pretty amazing in portugal."

> "Morning — this is the reminder you asked for yesterday. Three framings to pick from: 1. … 2. … 3. … Pick one and we can build it."

> "my time has become a commodity I can no longer share with [project]. I am proud of what I've done for you this far. <3"

> "It was a riveting conversation and super on point. In addition: please fill out this questionnaire."

**Pattern moves to use:**

- **First-person ownership.** "I was good at... I did not love it." Assaf speaks as himself.
- **Two-beat pair.** Short sentence, sharper sentence. "Eight arms. Ten times the battery."
- **Concrete proper nouns.** Audi, Diesel, Claude, Figma, Stripe, PUNX, Distyl. Never "global brands" or "leading platforms."
- **Aphorism by inversion** as a CLOSE: "The point is not X. The point is Y." Use sparingly, only as an ending.
- **Self-deprecation that is genuinely uncertain.** Not winking. "I am not sure yet what to make of that."
- **Trust the reader.** Land flat, leave.

**Words and phrases hard-banned (do not use any of these in any field):**

leverage, ideate, synergize, circle back, touch base, deep dive, unlock, transform, ignite, empower, level up, double down, lean in, optimize, scale (as a verb), navigate, unpack, move the needle, validate, surface (as a verb), holistic, end-to-end, best-in-class, world-class, next-level, game-changer, paradigm shift, mission-critical, value-add, north star, thrilled, excited to share, looking forward, at your earliest convenience, hope this finds you well, just wanted to, robust, delve, supercharge, disruptive, cutting-edge, breakthrough, innovative solution, in today's landscape, in a world where.

**Patterns hard-banned:**

- Six-noun list stacks ("strategy, design, content, copy, voice, system").
- Two-beat pairs that sound like VC tweets ("X wins. Y loses." "X is over. Y is here.").
- "We believe..." preambles. "Imagine if..." openers. Manifesto ALL CAPS.
- Three-item lists. Use two or one.
- Em-dashes for emphasis (use them only as breath, like the specimens).
- Adjective stacks ("transformative, innovative, scalable platform").
- Pundit declarations that strip away "I" / "we" — Assaf speaks as a person, not as a brand mouthpiece.

# Tone per action_type

- **comment** — peer-to-peer reply, conversational, adds one specific thing. Reads like a Slack response, not a manifesto.
- **dm** — correspondence mode. Open with "Hi [Name] —" or "Hey [Name]!". Plain ask in 2-3 sentences. Close with "<3" or "Talk soon!" or just "Assaf". Use Assaf's actual greetings and closers.
- **quote_post** — essay mode. One observation, two-beat pair welcome. Concrete proper nouns. Lands flat.
- **publish_original** — essay mode. Title is flat declarative ("The Shift", "The Point", "Where X Lands"). One-paragraph premise. First-person where natural.
- **op_ed_pitch** — first paragraph IS the pitch. Includes the publication target, the hook, the angle in Assaf's voice. Treat the editor as intelligent.
- **partnership_reach** — DM-style. Name the specific overlap. Propose a small concrete next step (a 20-min call, a co-bylined post). No "synergy" framing.
- **retweet** — one short line of context if any, often empty.

If a draft could come from any AI brand studio, it is wrong. The fingerprint is: first-person, specific names, mild uncertainty welcome, no consultancy mouthpiece tone.

# Action types

You pick ONE action_type per signal. The product supports more than "publish original." Pick the action that fits the signal type and CE's posture.

- **comment** (~5 min) — reply directly under the post/article. Adds a CE perspective in the existing conversation. Good for posts where CE has a sharp counter or a useful add.
- **retweet** (~1 min) — boost without commentary. Good when the source already does the work and CE wants to associate publicly.
- **quote_post** (~10 min) — quote the original on X/LinkedIn with CE's framing on top. Good when content is solid but CE has a sharper angle.
- **dm** (~15 min) — private outreach to the author. Good for relationship building, sourcing, or proposing collab when public engagement would be wrong.
- **op_ed_pitch** (~2 hr) — pitch a piece to a named publication. Good when the topic plus CE's take is publication-worthy and the brand benefits from third-party endorsement.
- **partnership_reach** (~30 min) — outreach proposing collaboration (joint piece, panel, podcast trade). Good when the source is a peer brand or operator CE could co-create with.
- **publish_original** (~3 hr) — CE writes its own piece. Good when CE has a unique take that warrants a standalone deliverable.
- **skip** — nothing to do. Off-territory, duplicative, or low-quality.

# Priority

- **1 = must-act this week.** Time-sensitive (news cycle, deadline) or core territory plant. CE should not let this pass.
- **2 = should-act this month.** Adjacent, useful, sharpens positioning. Worth doing when there's room.
- **3 = watch.** Track for later. No action now.
- **0 = skip.**

Priority is independent of score. A 78-core item could be priority 1 (act now) or priority 2 (act when room opens) depending on time-sensitivity and CE's existing queue.

# Decision schema (return ONLY this JSON object — no markdown fences, no commentary)

Every text field obeys the voice and stop-slop rules above. Be concise. Be specific.

{{
  "tldr": "<max 12 words, active voice, plain declarative. What happened. Lead with the specific number or name.>",
  "score": <integer 0-100>,
  "pillar": "P1" | "P2" | "P3" | "P4" | "P5" | null,
  "fit": "core" | "adjacent" | "tangential" | "off-territory",
  "good_for_brand_because": "<max 14 words. Just the reason — NO 'Good for [BRAND] because' prefix. Plain claim about why this lands on the brand's territory.>",
  "action_type": "comment" | "retweet" | "quote_post" | "dm" | "op_ed_pitch" | "partnership_reach" | "publish_original" | "skip",
  "priority": <integer 0|1|2|3>,
  "effort_minutes": <integer>,
  "action_text": "<the actual draft, in Assaf's voice, ready to publish or send. Length depends on action_type: comment=2-3 sentences, quote_post=1-2 sentences plus the boost, dm=3-5 sentences, op_ed_pitch=a one-paragraph pitch with title and angle, publish_original=a one-line title plus 2-sentence premise, retweet='RT — boost as-is' or one-line context, skip=''. Lead with the promise. No agency words. Active voice.>",
  "follow_up_query": "<one search query that would surface more like this>"
}}

# Scoring rules

- 80-100 = core territory. Lands directly on a pillar's purpose, references positioning claims or ICP pain points. Worth drafting a public move now.
- 60-79 = adjacent. Useful background or context. Worth tracking, not drafting.
- 40-59 = tangential. Shares vocabulary but not the brand's specific angle.
- 0-39 = off-territory. No defensible connection.

# Judgment rules

- Read the SPIRIT of the brief, not the literal words. A signal can match a pillar without using its exact phrases — what matters is whether the story is about the same thing the brand cares about.
- If the signal triggers any forbidden_phrases or anti_patterns, score it lower regardless of topical fit.
- Self-check: if your why_score could apply to any AI/marketing brand, lower the score. Anchor in THIS brand's specific territory.

Return ONLY the JSON object. Nothing else."""


def _ask_claude(system_prompt: str, user: str, api_key: str, model: str, max_retries: int = 4) -> Dict[str, Any]:
    backoff = 5
    for attempt in range(max_retries):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 600,
                "temperature": 0.2,
                # The system prompt is the entire brief + scoring rubric and is
                # identical across every item in this run. Mark it cacheable so
                # only the first call pays full input price; the rest hit the
                # 5-minute ephemeral cache at ~10% of input cost.
                "system": [{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                "messages": [{"role": "user", "content": user}],
            },
            timeout=90,
        )
        if r.status_code == 429 or r.status_code >= 500:
            wait = int(r.headers.get("retry-after", backoff))
            print(f"    rate-limited (HTTP {r.status_code}), sleeping {wait}s", flush=True)
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
            continue
        r.raise_for_status()
        payload = r.json()
        # Track cache hit-rate so we can verify ephemeral caching is actually
        # paying off across a run. usage["cache_read_input_tokens"] should be
        # ~brief_prompt_size for every call after the first within 5 minutes.
        usage = payload.get("usage") or {}
        if usage.get("cache_read_input_tokens") or usage.get("cache_creation_input_tokens"):
            tracer.record("llm_cache_usage", {
                "input_tokens": usage.get("input_tokens"),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            })
        text = payload["content"][0]["text"].strip()
        break
    else:
        raise RuntimeError(f"max retries exceeded")
    # Strip optional code fences
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
    j_start = text.find("{")
    j_end = text.rfind("}")
    return json.loads(text[j_start:j_end + 1])


def score_items_with_brief(
    raw_items: Iterable[RawItem],
    brief_path: Path,
    model: str = "claude-sonnet-4-6",
    feedback_events: Optional[List[Dict[str, Any]]] = None,
) -> List[ScoredItem]:
    """Returns ScoredItems judged by Claude against the brief, sorted by score."""
    api_key = _get_api_key()
    brief_text = Path(brief_path).read_text(encoding="utf-8")
    system_prompt = _build_system_prompt(brief_text)

    items_list = list(raw_items)
    print(f"[llm-scoring] {len(items_list)} items × {model}  (brief: {brief_path})", flush=True)

    tracer.record("llm_scoring_started", {
        "brief_path": str(brief_path),
        "model": model,
        "item_count": len(items_list),
        "system_prompt_chars": len(system_prompt),
        "system_prompt_excerpt_first_800": system_prompt[:800],
    })

    scored: List[ScoredItem] = []
    for i, item in enumerate(items_list, 1):
        user = (
            f"Signal:\n\n"
            f"source: {item.source}\n"
            f"title: {item.title}\n"
            f"date: {item.date}\n"
            f"summary: {item.summary}\n"
            f"content: {(item.content or '')[:2000]}\n"
            f"tags: {', '.join(item.tags or [])}\n\n"
            f"Return the JSON decision."
        )

        # No inter-item sleep: _ask_claude already handles 429s with
        # retry-after + exponential backoff, and a fixed pause here just
        # padded the run past the 5-minute prompt-cache TTL, defeating the
        # cache. If you ever hit a tier ceiling, raise it in the handler.
        claude_response_raw = None
        error_msg = None
        try:
            d = _ask_claude(system_prompt, user, api_key, model)
            claude_response_raw = d
            score = float(max(0, min(100, int(d.get("score") or 0))))
            pillar = d.get("pillar")
            pillar_fit = [pillar] if pillar in {"P1", "P2", "P3", "P4", "P5"} else []
            fit = d.get("fit") or "off-territory"
            tldr = d.get("tldr") or ""
            why_score = d.get("why_score") or ""
            why_care = d.get("good_for_brand_because") or d.get("why_brand_should_care") or ""
            action_type = d.get("action_type") or "skip"
            priority = int(d.get("priority") or 0)
            effort_min = int(d.get("effort_minutes") or 0)
            action_text = d.get("action_text") or ""
            angle = action_text  # reuse field for back-compat
            take = action_text
            follow_up = d.get("follow_up_query") or ""
        except Exception as e:
            print(f"  [{i}] ERROR: {e}", flush=True)
            error_msg = str(e)
            score = 0.0
            pillar_fit, fit = [], "off-territory"
            tldr = ""
            why_score = f"PARSE_ERROR: {e}"
            why_care = ""
            action_type = "skip"
            priority = 0
            effort_min = 0
            action_text = ""
            angle = ""
            take = ""
            follow_up = ""

        print(f"  [{i}/{len(items_list)}] {int(score):3d} {fit:13s} {pillar_fit or '[—]'} · {item.title[:60]}", flush=True)

        tracer.record("llm_score", {
            "index": i,
            "total": len(items_list),
            "item": {
                "id": item.id,
                "title": item.title,
                "source": item.source,
                "source_url": item.source_url,
                "date": item.date,
                "summary": item.summary,
                "content_excerpt_first_500": (item.content or "")[:500],
                "tags": item.tags or [],
                "discovery_method": item.discovery_method,
            },
            "user_message": user,
            "claude_response_raw": claude_response_raw,
            "error": error_msg,
            "parsed": {
                "score": score,
                "fit": fit,
                "pillar_fit": pillar_fit,
                "tldr": tldr,
                "action_type": action_type,
                "priority": priority,
                "effort_minutes": effort_min,
                "action_text": action_text,
                "why_score": why_score,
                "why_care": why_care,
                "follow_up_query": follow_up,
            },
        })

        scored.append(
            ScoredItem(
                id=item.id,
                title=item.title,
                source=item.source,
                source_url=item.source_url,
                date=item.date,
                summary=item.summary,
                pillar_fit=pillar_fit,
                surf_fit=[],
                mechanism_present=fit in {"core", "adjacent"},
                score=score,
                reason_for_score=f"{fit}: {why_score}",
                why_ce_should_care=why_care,
                suggested_ce_angle=angle,
                possible_ce_take=take,
                follow_up_search_query=follow_up,
                discovery_method=item.discovery_method,
                candidate_source=item.candidate_source,
                feedback_counts={},
                source_weight=0.0,
                engagement=item.engagement,
                metadata={**(item.metadata or {}), "scoring_method": "llm", "fit": fit, "tldr": tldr, "action_type": action_type, "priority": priority, "effort_minutes": effort_min, "action_text": action_text},
                engagement_score=item.engagement_score,
                local_rank_score=item.local_rank_score,
                local_relevance=item.local_relevance,
                freshness=item.freshness,
                traction_label="",
            )
        )

    sorted_items = sorted(scored, key=lambda s: s.score, reverse=True)
    tracer.record("llm_scoring_complete", {
        "scored_count": len(sorted_items),
        "score_distribution": {
            "80-100": sum(1 for s in sorted_items if s.score >= 80),
            "60-79": sum(1 for s in sorted_items if 60 <= s.score < 80),
            "40-59": sum(1 for s in sorted_items if 40 <= s.score < 60),
            "0-39": sum(1 for s in sorted_items if s.score < 40),
        },
    })
    return sorted_items
