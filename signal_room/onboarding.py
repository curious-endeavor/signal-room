"""Brand onboarding: URL crawl + Claude interview + brief finalization.

Stdlib + requests only (no httpx / bs4) so the Render build stays lean.

Flow:
  1. crawl_brand(url) → str (concatenated text from homepage + 5-10 internal links)
  2. build_system_prompt(brand, brand_context) → str (interview script for Claude)
  3. next_turn(session_messages, system_prompt) → assistant_msg, ready_to_generate
  4. finalize_brief(brand, brand_context, transcript) → dict (BrandBrief-shaped)
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable
from urllib.parse import urlparse, urljoin

import requests


# ---- Crawler ----

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SignalRoomCrawler/1.0"
)

# Heuristic paths worth visiting if linked from the homepage.
RELEVANT_PATH_HINTS = (
    "about", "company", "team", "story",
    "pricing", "plans",
    "product", "products", "platform", "features", "solutions",
    "case-studies", "customers", "stories", "case", "studies",
    "press", "news", "blog", "resources",
    "faq", "manifesto", "values", "principles",
)

PER_PAGE_TIMEOUT = 10
OVERALL_TIMEOUT = 30
MAX_PAGES = 10
MAX_CONTEXT_CHARS = 30_000


def _looks_relevant(path: str) -> bool:
    """Cheap heuristic: does this URL path probably hold brand context?"""
    p = path.lower().strip("/").split("/", 1)[0]
    return any(hint in p for hint in RELEVANT_PATH_HINTS)


def _strip_html(raw: str) -> str:
    """Drop tags + scripts/styles. Stdlib-only, hostile to fancy HTML
    but adequate for marketing pages."""
    # Drop script/style/nav/footer blocks entirely.
    raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<noscript[\s\S]*?</noscript>", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<!--[\s\S]*?-->", " ", raw)
    # Drop nav/footer (rough)
    raw = re.sub(r"<(nav|footer|header)[\s\S]*?</\1>", " ", raw, flags=re.IGNORECASE)
    # Replace block tags with newlines so paragraphs stay separated.
    raw = re.sub(r"</(p|div|li|h[1-6]|section|article|br)>", "\n", raw, flags=re.IGNORECASE)
    # Strip remaining tags.
    raw = re.sub(r"<[^>]+>", " ", raw)
    # Decode HTML entities.
    raw = html.unescape(raw)
    # Collapse whitespace.
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _extract_links(html_text: str, base_url: str) -> list[str]:
    """Find same-host anchor hrefs that look brand-relevant."""
    base = urlparse(base_url)
    if not base.scheme or not base.netloc:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a\s[^>]*href=["\'](?P<href>[^"\']+)["\']', html_text, flags=re.IGNORECASE):
        href = match.group("href").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != base.netloc:
            continue
        if not parsed.scheme.startswith("http"):
            continue
        # Drop query strings + fragments for relevance match.
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        if clean == base_url.rstrip("/"):
            continue
        if clean in seen:
            continue
        if not _looks_relevant(parsed.path):
            continue
        seen.add(clean)
        found.append(clean)
        if len(found) >= MAX_PAGES:
            break
    return found


def _fetch(url: str, timeout: int = PER_PAGE_TIMEOUT) -> tuple[str, str]:
    """Returns (url, text_content). Empty text on failure."""
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            allow_redirects=True,
        )
        if r.status_code >= 400:
            return url, f"[fetch error: HTTP {r.status_code}]"
        ct = r.headers.get("content-type", "").lower()
        if "html" not in ct and "text" not in ct:
            return url, "[skipped: non-HTML content-type]"
        return url, _strip_html(r.text)
    except requests.RequestException as exc:
        return url, f"[fetch error: {type(exc).__name__}]"
    except Exception as exc:
        return url, f"[fetch error: {exc}]"


def crawl_brand(url: str) -> dict[str, Any]:
    """Returns {'context': concatenated text, 'pages': [url, ...], 'errors': [...]}.

    Walks homepage + up to 9 relevant internal pages. Hard 30s overall cap.
    Never raises — partial results on timeout / errors.
    """
    start = time.time()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"context": "", "pages": [], "errors": [f"invalid URL: {url}"]}

    pages: list[str] = []
    errors: list[str] = []
    sections: list[str] = []

    # Step 1: homepage
    home_url, home_text = _fetch(url)
    pages.append(home_url)
    if home_text.startswith("[fetch error"):
        errors.append(f"{home_url}: {home_text}")
        sections.append(f"## {home_url}\n\n{home_text}")
    else:
        sections.append(f"## {home_url}\n\n{home_text[:6000]}")

        # Step 2: discover internal links
        try:
            link_resp = requests.get(
                url,
                timeout=PER_PAGE_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            internal_links = _extract_links(link_resp.text, link_resp.url or url)
        except Exception:
            internal_links = []

        # Step 3: fetch internal pages in parallel within remaining budget
        remaining = max(1, int(OVERALL_TIMEOUT - (time.time() - start)) - 1)
        if internal_links and remaining > 1:
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_map = {executor.submit(_fetch, link, min(remaining, PER_PAGE_TIMEOUT)): link for link in internal_links[:MAX_PAGES - 1]}
                try:
                    for future in as_completed(future_map, timeout=remaining):
                        u, text = future.result()
                        pages.append(u)
                        if text.startswith("[fetch error") or text.startswith("[skipped"):
                            errors.append(f"{u}: {text}")
                            continue
                        sections.append(f"## {u}\n\n{text[:4000]}")
                except Exception:
                    # Timeout reached; whatever we have so far is what we keep.
                    pass

    context = "\n\n".join(sections)[:MAX_CONTEXT_CHARS]
    return {"context": context, "pages": pages, "errors": errors, "duration_s": round(time.time() - start, 2)}


# ---- Claude interview ----

INTERVIEW_SYSTEM_PROMPT = """You are a brand researcher conducting a focused 5-6 question interview to build a Signal Room brief for {brand_name} ({brand_url}).

You already crawled the brand's site. Here's what you found:

<brand_crawl_context>
{brand_context}
</brand_crawl_context>

# Your job
1. Ask ONE focused question per turn. No multi-part questions.
2. Acknowledge the user's answer in one sentence, then ask the next question.
3. After 5–6 questions, write `READY_TO_GENERATE` on its own line, then a short paragraph summarizing what you learned.
4. Do NOT generate the brief yourself. Just signal readiness.

# Questions to cover (in any natural order):
- Primary audience (specific roles, industries, named personas)
- 3–5 positioning pillars (what the brand wants to be known for)
- Voice / tone (formal, playful, technical, opinionated, restrained, etc.)
- 3–5 named competitors and how the brand differs from each
- Categories of news/signal the team most wants surfaced
- Sensitive areas / things the brand does NOT want surfaced

# Rules
- Don't lecture. Don't add disclaimers. Don't explain what you'll do next.
- Use the crawl context to skip questions whose answers are obvious from the site. Confirm rather than re-ask.
- If the user says "I don't know" or "skip", move to the next question.
- Maximum 7 turns including yours. After turn 6 you MUST output READY_TO_GENERATE on the next turn even if questions remain.
- Speak like a person, not a chatbot. Match the brand's tone if it's clear from the crawl.
"""


BRIEF_GENERATION_SYSTEM_PROMPT = """You are generating a Signal Room brand brief from a crawl + a 5-6 turn interview transcript.

Output ONLY a JSON object matching this schema. No markdown, no commentary.

{{
  "name": "<brand name>",
  "url": "<homepage URL>",
  "one_liner": "<single sentence positioning, max 200 chars>",
  "audience": ["<audience description 1>", "<audience description 2>", ...],
  "pillars": [
    {{
      "id": "P1",
      "name": "<short pillar name>",
      "why": "<1-2 sentence explanation of what this pillar tracks>",
      "keywords": ["<keyword 1>", "<keyword 2>", ... 6-12 lowercase keywords]
    }},
    ... 3-5 pillars total ...
  ],
  "discovery_queries": [
    {{
      "id": "<kebab-case-id>",
      "priority": 1,
      "topic": "<the search string sent to APIs — keyword-shaped, 6-12 words>",
      "why": "<one sentence explaining what signal this surfaces>"
    }},
    ... 5-10 queries total. Priorities 1 (3-4 queries), 2 (2-3 queries), 3 (0-3 queries) ...
  ],
  "seed_sources": [
    {{
      "url": "https://...",
      "name": "<short name>",
      "category": "<tier1_press|peer_research|brand_benchmark|competitor|community|other>",
      "why": "<one sentence on why this source matters>"
    }},
    ... 4-10 seeds total ...
  ]
}}

# Rules
- Pull specific phrases from the brand's own language (crawl + interview).
- Discovery queries should be brand-specific, NOT generic ("AI security 2026" is bad; "brand chatbot lawsuit AI hallucination 2026" is good).
- Use the user's stated competitors, audiences, and positioning words.
- Mark the topic with `intent` hints in the why field when it's a news-cycle topic so the planner can choose `breaking_news` later.
- Return ONLY JSON. No prose before or after.
"""


def build_system_prompt(brand_name: str, brand_url: str, brand_context: str) -> str:
    return INTERVIEW_SYSTEM_PROMPT.format(
        brand_name=brand_name or brand_url,
        brand_url=brand_url,
        brand_context=brand_context or "(crawl returned no content — interview the user from scratch)",
    )


def is_ready_to_generate(assistant_msg: str) -> bool:
    return "READY_TO_GENERATE" in (assistant_msg or "")


def call_claude(system: str, messages: list[dict[str, str]], model: str = "claude-sonnet-4-6",
                max_tokens: int = 1200, temperature: float = 0.4) -> str:
    """One Claude turn. messages = [{role: 'user'/'assistant', content: ...}, ...]."""
    from .llm_scoring import _get_api_key
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
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        },
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()


def generate_initial_assistant_turn(brand_name: str, brand_url: str, brand_context: str) -> str:
    """Kick off the interview — Claude opens with the first question."""
    system = build_system_prompt(brand_name, brand_url, brand_context)
    messages = [{"role": "user", "content": "Begin the interview. Ask your first question."}]
    return call_claude(system, messages)


def next_assistant_turn(brand_name: str, brand_url: str, brand_context: str,
                         history: list[dict[str, str]]) -> str:
    """Given full history (user + assistant), produce the next assistant turn."""
    system = build_system_prompt(brand_name, brand_url, brand_context)
    return call_claude(system, history)


def generate_brief(brand_name: str, brand_url: str, brand_context: str,
                    transcript: list[dict[str, str]]) -> dict[str, Any]:
    """Final call: given the interview, output a structured brief dict."""
    transcript_str = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in transcript)
    user_msg = (
        f"BRAND: {brand_name} ({brand_url})\n\n"
        f"CRAWL CONTEXT (truncated):\n{brand_context[:15000]}\n\n"
        f"INTERVIEW TRANSCRIPT:\n{transcript_str}\n\n"
        f"Generate the JSON brief now."
    )
    response = call_claude(
        BRIEF_GENERATION_SYSTEM_PROMPT,
        [{"role": "user", "content": user_msg}],
        max_tokens=4000,
        temperature=0.2,
    )
    # Unwrap markdown fence if present
    text = response.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    j_start = text.find("{")
    j_end = text.rfind("}")
    if j_start == -1 or j_end == -1:
        raise ValueError(f"no JSON object in brief response: {response[:300]}")
    return json.loads(text[j_start:j_end + 1])
