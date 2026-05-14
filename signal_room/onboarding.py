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


SOCIAL_DOMAINS: dict[str, tuple[str, ...]] = {
    "twitter": ("twitter.com", "x.com"),
    "linkedin": ("linkedin.com",),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "facebook": ("facebook.com",),
    "tiktok": ("tiktok.com",),
    "github": ("github.com",),
}

# Paths that aren't real handles — share intents, generic landing pages, etc.
_SOCIAL_PATH_BLOCKLIST = ("/share", "/intent", "/sharer", "/dialog/share", "/login", "/signup")


def _extract_socials(html_text: str) -> dict[str, str]:
    """Pull official social handles out of raw HTML (before nav/footer strip).

    Returns {platform: canonical_url}. First link per platform wins, which
    works well because brand sites almost always link their own handles from
    a footer that appears once.
    """
    found: dict[str, str] = {}
    for match in re.finditer(r'<a\s[^>]*href=["\'](?P<href>[^"\']+)["\']', html_text, flags=re.IGNORECASE):
        href = match.group("href").strip()
        if not href.lower().startswith("http"):
            continue
        try:
            parsed = urlparse(href)
        except ValueError:
            continue
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path_lower = parsed.path.lower()
        if any(b in path_lower for b in _SOCIAL_PATH_BLOCKLIST):
            continue
        # A bare social-host root (e.g. https://twitter.com/) is not a handle.
        if not parsed.path.strip("/"):
            continue
        for platform, domains in SOCIAL_DOMAINS.items():
            if any(host == d or host.endswith("." + d) for d in domains):
                if platform not in found:
                    canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
                    found[platform] = canonical
                break
    return found


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
    socials: dict[str, str] = {}

    # Step 1: homepage
    home_url, home_text = _fetch(url)
    pages.append(home_url)
    if home_text.startswith("[fetch error"):
        errors.append(f"{home_url}: {home_text}")
        sections.append(f"## {home_url}\n\n{home_text}")
    else:
        sections.append(f"## {home_url}\n\n{home_text[:6000]}")

        # Step 2: discover internal links AND social handles. We need raw HTML
        # (with nav/footer intact) so _fetch's stripped text won't do — socials
        # almost always live in the footer that _strip_html drops.
        try:
            link_resp = requests.get(
                url,
                timeout=PER_PAGE_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            internal_links = _extract_links(link_resp.text, link_resp.url or url)
            socials = _extract_socials(link_resp.text)
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
    return {
        "context": context,
        "pages": pages,
        "errors": errors,
        "socials": socials,
        "duration_s": round(time.time() - start, 2),
    }


# ---- Claude interview ----

INTERVIEW_SYSTEM_PROMPT = """You are a brand researcher conducting a focused 5-6 question interview to build a Signal Room brief for {brand_name} ({brand_url}).

You already crawled the brand's site. Here's what you found:

<brand_crawl_context>
{brand_context}
</brand_crawl_context>

Separate research passes have already produced these:

<discovered_competitors>
{discovered_competitors}
</discovered_competitors>

<discovered_socials>
{discovered_socials}
</discovered_socials>

<inferred_voice>
{inferred_voice}
</inferred_voice>

# Your job
1. PROPOSE, don't ask. For every topic below, draw a specific answer from the crawl context and the pre-researched sections, then ask the user to confirm or correct it. One topic per turn.
2. Acknowledge the user's answer in one sentence (or just "Got it"), then move to the next topic.
3. After 5–6 turns, write `READY_TO_GENERATE` on its own line, then a short paragraph summarizing what you learned.
4. Do NOT generate the brief yourself. Just signal readiness.

# When you don't know — search first, then ask for links
You have the `web_search` tool available on every turn (up to 2 searches per turn). The goal is a magical onboarding where the user feels you really get them. That means:

1. If the crawl context + pre-researched sections don't tell you something you need, USE web_search before asking the user. Search for things like:
   - "<brand> founder LinkedIn" or "<brand> CEO"
   - "<brand> case study <industry>"
   - "<brand> press release 2025/2026"
   - "<brand> review G2 / Capterra / Product Hunt"
   - Specific personas, partnerships, or named features you saw on the site
2. After searching, integrate what you found INTO a confident proposal in the same turn. Cite a fact briefly ("Saw the 2025 Roast Magazine piece — sounds like Y is your core buyer"). Don't dump search results raw.
3. If searches still leave a gap, ask the user for ONE specific link that would close it — not for an open answer. Examples:
   - "Got a link to the founder's LinkedIn? It'll help me read how you talk about the company externally."
   - "Drop me your most recent case study or a customer quote — I want to pin down the 'we changed THIS for them' story."
   - "Got a recent press release or launch post? I want to hear how you describe the product when you're being bold."
   The user can paste any URL — the system auto-fetches it and you'll see the content as a `<fetched url="...">...</fetched>` block in their next message.
4. Only as a last resort, ask the open question — and say plainly that you tried.

# Voice — be confident, not deferential
You are the researcher reporting findings, not an assistant fishing for answers. Lead every turn with a confident claim grounded in the crawl + any search you ran, then a short closing check. Examples of the right register:

- "From the site, this looks like it's built for <X> and <Y> — head roasters at mid-size production roasteries especially. Right call, or am I missing the real buyer?"
- "Your three core pillars look like A, B, and C. Sound right, or would you swap one?"
- "The signals you'd want surfaced are probably <X> and <Y>, based on what you're tracking on the site. Add anything?"

Avoid:
- "Who is your primary target audience?" (open, deferential — never do this when the site has clues)
- "Can you tell me…"
- "I'd like to understand…"
- Multi-option menus where you list 4 possibilities and ask the user to pick.

If the crawl genuinely has no signal on a topic, then and only then ask an open question — but say so plainly: "The site doesn't tell me much about X. What's the real answer?"

# Topics to cover (in any natural order, propose-and-confirm style):
- Primary audience (specific roles, industries, named personas) — propose from the crawl.
- 3–5 positioning pillars — propose from the crawl's own headline phrases.
- Categories of news/signal the team most wants surfaced — propose based on what the brand seems to compete on.
- Sensitive areas / things the brand does NOT want surfaced — this one is hard to infer; here a direct question is fine ("Anything off-limits we should never surface?").

# Pre-researched items — CONFIRM, DON'T RE-ASK
For competitors, socials, and voice, you already have researched output above. For each:

- If the section is non-empty, briefly show the user what you found in plain language and ask one closing question like "anything missing or wrong?". Accept short corrections. Treat "looks right" / "yes" / "no changes" as full confirmation and move on.
- If the section is empty or marked unavailable, propose from the crawl if you can; otherwise ask directly.
- Spend at most ONE turn on each.

Competitors: don't drill into per-competitor differentiators — the discovery research already captured those.
Socials: show the platforms+handles back so the user can correct typos or add missing channels.
Voice: paraphrase the inferred voice in 1–2 sentences (don't dump the whole JSON). Ask if it sounds right.

You may group socials + voice into a single turn if both are non-empty and seem accurate — phrase it as one combined confirmation rather than two consecutive yes/no questions.

# Rules
- Don't lecture. Don't add disclaimers. Don't explain what you'll do next.
- Use the crawl context aggressively. The brand wrote the site — quote it back at them as confident inference.
- If the user says "I don't know" or "skip", move to the next topic.
- Maximum 7 turns including yours. After turn 6 you MUST output READY_TO_GENERATE on the next turn even if questions remain.
- Speak like a person, not a chatbot. Match the brand's tone if it's clear from the crawl.

# Reading URLs the user pastes
If the user pastes a URL into a message, the system fetches it server-side and appends the page's stripped text as a `<fetched url="...">...</fetched>` block right after their message. Treat that text as authoritative content the user wants you to read. Do not claim you cannot browse — when you see a `<fetched>` block, you have already read the page. If a `<fetched ... error="..." />` self-closing block appears, the fetch failed; tell the user what went wrong and ask them to paste the content directly.
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


_ENRICHMENT_MARKER = "<!--SR_ENRICHMENT_v1-->"


def embed_enrichment(brand_context: str, enrichment: dict[str, Any]) -> str:
    """Prepend a JSON enrichment blob (competitors, socials, voice, …) to
    brand_context behind a marker so it round-trips through DB persistence
    without a schema change. Stored at the START of the string so a
    downstream length cap (the 30k cut applied before persistence) can never
    chop the structured enrichment off — it would only ever truncate raw
    crawl tail text."""
    base = (brand_context or "").lstrip()
    if not enrichment:
        return base
    blob = json.dumps(enrichment, ensure_ascii=False)
    return f"{blob}\n{_ENRICHMENT_MARKER}\n{base}"


def split_enrichment(brand_context: str) -> tuple[str, dict[str, Any]]:
    """Inverse of embed_enrichment. Returns (raw_context, enrichment_dict).

    Tolerates both layouts: blob-first (current) and blob-trailing (older
    sessions persisted before the layout flipped).
    """
    if not brand_context or _ENRICHMENT_MARKER not in brand_context:
        return brand_context or "", {}
    head, _, tail = brand_context.partition(_ENRICHMENT_MARKER)
    head_stripped = head.strip()
    # Blob-first layout: marker comes right after the JSON blob.
    if head_stripped.startswith("{") and head_stripped.endswith("}"):
        try:
            data = json.loads(head_stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return tail.lstrip(), data
    # Blob-trailing (legacy) layout: marker comes after the crawl context.
    tail_stripped = tail.strip()
    if tail_stripped.startswith("{") and tail_stripped.endswith("}"):
        try:
            data = json.loads(tail_stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return head.rstrip(), data
    return brand_context, {}


def _format_competitors(competitors: list[dict[str, str]]) -> str:
    if not competitors:
        return ""
    lines = []
    for i, c in enumerate(competitors, 1):
        url = f" ({c['url']})" if c.get("url") else ""
        diff = f" — {c['differentiator']}" if c.get("differentiator") else ""
        lines.append(f"{i}. {c.get('name', '?')}{url}{diff}")
    return "\n".join(lines)


def _format_socials(socials: dict[str, str]) -> str:
    if not socials:
        return ""
    return "\n".join(f"- {platform}: {url}" for platform, url in socials.items() if url)


def _format_voice(voice: dict[str, Any]) -> str:
    if not voice:
        return ""
    lines: list[str] = []
    if voice.get("summary"):
        lines.append(f"Summary: {voice['summary']}")
    if voice.get("adjectives"):
        lines.append("Adjectives: " + ", ".join(voice["adjectives"]))
    if voice.get("do"):
        lines.append("Do:")
        lines.extend(f"  - {d}" for d in voice["do"])
    if voice.get("dont"):
        lines.append("Avoid:")
        lines.extend(f"  - {d}" for d in voice["dont"])
    if voice.get("sample_phrases"):
        lines.append("Sample phrases from their own copy:")
        lines.extend(f"  - \"{p}\"" for p in voice["sample_phrases"])
    return "\n".join(lines)


# Back-compat shim — the old name is still referenced by tests / callers
# that haven't been migrated yet.
def format_competitors_block(competitors: list[dict[str, str]]) -> str:
    return _format_competitors(competitors)


def build_system_prompt(brand_name: str, brand_url: str, brand_context: str) -> str:
    ctx, enrichment = split_enrichment(brand_context)
    competitors_str = _format_competitors(enrichment.get("competitors") or [])
    socials_str = _format_socials(enrichment.get("socials") or {})
    voice_str = _format_voice(enrichment.get("voice") or {})
    return INTERVIEW_SYSTEM_PROMPT.format(
        brand_name=brand_name or brand_url,
        brand_url=brand_url,
        brand_context=ctx or "(crawl returned no content — interview the user from scratch)",
        discovered_competitors=competitors_str or "(competitor discovery unavailable — ask the user to name 3–5 competitors)",
        discovered_socials=socials_str or "(no social handles found — ask the user to share them)",
        inferred_voice=voice_str or "(voice analysis unavailable — ask the user to describe the brand's tone)",
    )


def is_ready_to_generate(assistant_msg: str) -> bool:
    return "READY_TO_GENERATE" in (assistant_msg or "")


# Minimal markdown → HTML renderer for the assistant chat bubble. Dependency-
# free; covers exactly what the interview prompt uses (bold, italic, paragraph
# breaks, blockquotes, simple "- " bullets, and inline links). Everything is
# HTML-escaped first so this is safe to dump into innerHTML.
_MD_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_INLINE_ITALIC = re.compile(r"(?<![\*\w])\*(?!\s)([^\*\n]+?)(?<!\s)\*(?!\*)")
_MD_INLINE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _md_inline(text: str) -> str:
    text = _MD_INLINE_LINK.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        text,
    )
    text = _MD_INLINE_BOLD.sub(r"<strong>\1</strong>", text)
    text = _MD_INLINE_ITALIC.sub(r"<em>\1</em>", text)
    return text


def render_assistant_markdown(text: str) -> str:
    """Render a small subset of markdown to safe HTML for chat display.

    Supports: **bold**, *italic*, [text](url), blockquotes (`> `), and
    "- " bullets. Paragraph breaks come from blank lines; single newlines
    become <br>. Strips out the READY_TO_GENERATE control sentinel so it
    never reaches the user.
    """
    if not text:
        return ""
    text = text.replace("READY_TO_GENERATE", "").strip()
    text = html.escape(text, quote=False)

    paragraphs = re.split(r"\n\s*\n", text)
    rendered: list[str] = []
    for raw_para in paragraphs:
        para = raw_para.strip("\n")
        if not para:
            continue
        lines = para.split("\n")
        # All-blockquote paragraph
        if all(ln.startswith("&gt; ") or ln.startswith("&gt;") for ln in lines):
            body = "<br>".join(_md_inline(ln.lstrip("&gt;").lstrip()) for ln in lines)
            rendered.append(f"<blockquote>{body}</blockquote>")
            continue
        # All-bullet paragraph (lines starting with "- " or "* ")
        if all(re.match(r"^[-*]\s+", ln) for ln in lines):
            bullet_re = re.compile(r"^[-*]\s+")
            items = "".join(
                "<li>" + _md_inline(bullet_re.sub("", ln)) + "</li>" for ln in lines
            )
            rendered.append(f"<ul>{items}</ul>")
            continue
        # Default: paragraph with <br> for single line breaks
        body = "<br>".join(_md_inline(ln) for ln in lines)
        rendered.append(f"<p>{body}</p>")
    return "\n".join(rendered)


# Regex pulled out so we can test it cheaply; matches http(s) URLs up to the
# first whitespace, ), >, ", or ' character. Trailing punctuation we trim by
# hand so "...read https://x.com/foo." doesn't keep the dot.
_URL_RE = re.compile(r"https?://[^\s)>\"']+", re.IGNORECASE)
_URL_TRAILING_PUNCT = ".,;:!?)]}>"

# Caps so a chat turn can't fan out into a crawl. Keep these small — the
# onboarding interview is meant to be a conversation, not a research pass.
MAX_URLS_PER_TURN = 3
MAX_BYTES_PER_FETCHED_URL = 6000


def extract_urls(message: str) -> list[str]:
    """Pull http(s) URLs out of a chat message, deduped, in first-seen order."""
    if not message:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in _URL_RE.findall(message):
        url = raw.rstrip(_URL_TRAILING_PUNCT)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def fetch_urls_for_chat(message: str, limit: int = MAX_URLS_PER_TURN) -> list[dict[str, str]]:
    """Fetch up to `limit` URLs pasted into a chat turn. Returns a list of
    {url, text, error} dicts so callers can choose how to render failures.
    Never raises — empty list when nothing usable.
    """
    urls = extract_urls(message)[:limit]
    out: list[dict[str, str]] = []
    for url in urls:
        fetched_url, text = _fetch(url)
        if text.startswith("[fetch error") or text.startswith("[skipped"):
            out.append({"url": fetched_url, "text": "", "error": text.strip("[]")})
            continue
        out.append({"url": fetched_url, "text": text[:MAX_BYTES_PER_FETCHED_URL], "error": ""})
    return out


def augment_user_message_with_fetches(message: str, fetched: list[dict[str, str]]) -> str:
    """Append fetched-URL bodies to a user message in a Claude-readable shape.
    The original message is preserved verbatim so the transcript stored in the
    DB still matches what the user typed; only the in-flight content sent to
    Claude grows.
    """
    if not fetched:
        return message
    blocks = []
    for entry in fetched:
        if entry.get("error"):
            blocks.append(f"<fetched url=\"{entry['url']}\" error=\"{entry['error']}\" />")
            continue
        blocks.append(
            f"<fetched url=\"{entry['url']}\">\n{entry['text']}\n</fetched>"
        )
    return message + "\n\n" + "\n\n".join(blocks)


INTERVIEW_WEB_SEARCH_MAX_USES = 2


def call_claude(system: str, messages: list[dict[str, str]], model: str = "claude-sonnet-4-6",
                max_tokens: int = 1200, temperature: float = 0.4,
                enable_web_search: bool = False, max_searches: int = INTERVIEW_WEB_SEARCH_MAX_USES,
                timeout: int = 120) -> str:
    """One Claude turn. messages = [{role: 'user'/'assistant', content: ...}, ...].

    If enable_web_search=True, Claude can issue server-side web_search calls
    mid-turn (capped at max_searches per turn). We return the concatenated
    text of the final assistant turn — tool-use blocks are stripped.
    """
    from .llm_scoring import _get_api_key
    api_key = _get_api_key()
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # The interview system prompt embeds the crawled brand context and
        # repeats verbatim across every turn of the chat (and across the
        # brief-generation finalize call, which uses a different but also
        # stable system). Cache so a 7-turn chat pays full input price
        # only on the first turn.
        "system": [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": messages,
    }
    if enable_web_search:
        body["tools"] = [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_searches,
        }]
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    # With web_search enabled the response can interleave server_tool_use,
    # web_search_tool_result, and text blocks. Concatenate all final-turn
    # text blocks in order — that's what the user is meant to read.
    text_parts: list[str] = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    return "\n".join(text_parts).strip()


COMPETITOR_DISCOVERY_SYSTEM_PROMPT = """You research B2B/SaaS brand competitors. Given a brand's name, URL, and crawled site context, use the web_search tool to identify the 3–5 companies this brand is most often compared to in the market.

Search for phrases like "<brand> vs", "<brand> alternatives", "<brand> competitors", and category-level terms drawn from the crawl context. Cross-check at least two independent sources before committing a name. Prefer competitors that show up across comparison roundups, review-site category pages, or the brand's own positioning, over speculative matches.

Output ONLY a JSON array — no prose, no markdown fences — matching this shape:

[
  {
    "name": "<competitor name>",
    "url": "<homepage URL if known, else empty string>",
    "differentiator": "<one-sentence statement of how the subject brand differs from this competitor, grounded in the crawled positioning>",
    "evidence": "<one short phrase naming where this competitor surfaced, e.g. 'appears on G2 category page, named in 2 vs-pages'>"
  },
  ...
]

Hard rules:
- 3 to 5 entries.
- Real companies only. If you cannot verify a name across sources, drop it.
- If you genuinely cannot find any competitors after searching, return [].
- Return ONLY the JSON array. No leading text, no trailing commentary, no code fences.
"""


def discover_competitors(brand_name: str, brand_url: str, brand_context: str,
                          max_searches: int = 5) -> list[dict[str, str]]:
    """Use Claude's web_search tool to identify 3–5 likely competitors.

    Returns a list of {name, url, differentiator, evidence} dicts. Returns []
    on any failure — onboarding falls back to asking the user.
    """
    user_msg = (
        f"BRAND: {brand_name or brand_url} ({brand_url})\n\n"
        f"CRAWLED SITE CONTEXT (truncated):\n{(brand_context or '')[:12000]}\n\n"
        f"Find this brand's 3–5 most-compared competitors. In each entry's "
        f"`differentiator` field, describe how '{brand_name or brand_url}' differs from "
        f"that competitor. Return ONLY the JSON array."
    )
    system = COMPETITOR_DISCOVERY_SYSTEM_PROMPT
    text = _call_claude_with_web_search(system, user_msg, max_searches=max_searches)
    parsed = _extract_json(text, "[", "]")
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        out.append({
            "name": name,
            "url": str(entry.get("url", "")).strip(),
            "differentiator": str(entry.get("differentiator", "")).strip(),
            "evidence": str(entry.get("evidence", "")).strip(),
        })
        if len(out) >= 5:
            break
    return out


SOCIALS_DISCOVERY_SYSTEM_PROMPT = """You find official social handles for a brand. Use the web_search tool. Search for "<brand> linkedin", "<brand> twitter", "<brand> instagram", etc.

Only include platforms where you can verify the handle is the brand's official, primary account (cross-checking the brand's own site or a press release where possible). When in doubt, leave it out.

Output ONLY a JSON object — no prose, no fences — mapping platform names to canonical URLs:

{
  "linkedin": "https://www.linkedin.com/company/<handle>",
  "twitter": "https://twitter.com/<handle>",
  "instagram": "https://www.instagram.com/<handle>",
  "youtube": "https://www.youtube.com/@<handle>",
  "facebook": "https://www.facebook.com/<handle>",
  "tiktok": "https://www.tiktok.com/@<handle>"
}

Use only the platform keys listed above. Omit platforms with no verified handle. Return {} if you can't verify any.
"""


VOICE_ANALYSIS_SYSTEM_PROMPT = """You characterize how a brand communicates. You have the brand's crawled site copy below; you may also use the web_search tool to sample recent posts from the provided social handles (search e.g. "site:linkedin.com/company/<handle>" or quoted recent post snippets) if it sharpens the read.

Look at: formality, energy, sentence length, vocabulary, point of view (we/you/neutral), humor, jargon density, emotional register, opinion vs neutral stance, signature phrases.

Output ONLY a JSON object — no prose, no fences:

{
  "summary": "<2-3 sentence description of how this brand sounds, grounded in the actual copy>",
  "adjectives": ["<adj 1>", "<adj 2>", ...],
  "do": ["<concrete do 1>", "<concrete do 2>", ...],
  "dont": ["<concrete avoid 1>", "<concrete avoid 2>", ...],
  "sample_phrases": ["<verbatim snippet 1>", "<verbatim snippet 2>", ...]
}

Constraints:
- 4–7 adjectives, 3–5 do's, 2–4 don'ts, 2–5 sample_phrases.
- adjectives must be specific (e.g. "wry", "matter-of-fact", "consultant-formal" — not "professional", "friendly", "modern").
- sample_phrases must be REAL verbatim snippets pulled from the crawl or surfaced social posts. No paraphrasing.
- Return ONLY the JSON object.
"""


def _call_claude_with_web_search(system: str, user_msg: str, max_searches: int = 5,
                                  timeout: int = 90, max_tokens: int = 2500) -> str:
    """Single-call helper for the discovery passes. Returns concatenated final
    text. Returns "" on any error so callers can fall back gracefully."""
    from .llm_scoring import _get_api_key
    try:
        api_key = _get_api_key()
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "system": system,
                "tools": [{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max_searches,
                }],
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return ""
    parts: list[str] = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _extract_json(text: str, opener: str, closer: str) -> Any:
    """Strip optional code fences, locate the outermost JSON node, parse it.
    Returns None on failure."""
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def discover_socials_via_search(brand_name: str, brand_url: str,
                                  known: dict[str, str] | None = None,
                                  max_searches: int = 4) -> dict[str, str]:
    """Use web_search to find official handles when crawl extraction missed
    them. Pass `known` (handles already extracted from the site) so Claude
    doesn't waste budget re-discovering them. Returns the merged map."""
    known = dict(known or {})
    user_msg = (
        f"BRAND: {brand_name} ({brand_url})\n\n"
        f"ALREADY KNOWN HANDLES (do not re-search these):\n"
        f"{json.dumps(known) if known else '{}'}\n\n"
        f"Find any official handles that are still missing. Return ONLY the JSON object "
        f"of NEWLY-DISCOVERED handles (don't repeat the known ones)."
    )
    text = _call_claude_with_web_search(SOCIALS_DISCOVERY_SYSTEM_PROMPT, user_msg, max_searches=max_searches)
    parsed = _extract_json(text, "{", "}")
    if not isinstance(parsed, dict):
        return known
    for platform, url in parsed.items():
        platform = str(platform).strip().lower()
        url = str(url).strip()
        if not platform or not url or platform in known:
            continue
        if platform not in SOCIAL_DOMAINS:
            continue
        known[platform] = url
    return known


def analyze_voice(brand_name: str, brand_url: str, brand_context: str,
                   socials: dict[str, str] | None = None,
                   max_searches: int = 4) -> dict[str, Any]:
    """Use web_search to characterize the brand's voice from site + socials.
    Returns {summary, adjectives, do, dont, sample_phrases} or {} on failure."""
    socials = socials or {}
    socials_block = "\n".join(f"- {p}: {u}" for p, u in socials.items()) or "(none extracted)"
    user_msg = (
        f"BRAND: {brand_name} ({brand_url})\n\n"
        f"SOCIAL HANDLES:\n{socials_block}\n\n"
        f"CRAWLED SITE COPY (truncated):\n{(brand_context or '')[:15000]}\n\n"
        f"Characterize the voice. Return ONLY the JSON object."
    )
    text = _call_claude_with_web_search(VOICE_ANALYSIS_SYSTEM_PROMPT, user_msg, max_searches=max_searches)
    parsed = _extract_json(text, "{", "}")
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(parsed.get("summary"), str):
        out["summary"] = parsed["summary"].strip()
    for list_key in ("adjectives", "do", "dont", "sample_phrases"):
        val = parsed.get(list_key)
        if isinstance(val, list):
            out[list_key] = [str(x).strip() for x in val if str(x).strip()]
    return out


def generate_initial_assistant_turn(brand_name: str, brand_url: str, brand_context: str) -> str:
    """Kick off the interview — Claude opens with the first proposal."""
    system = build_system_prompt(brand_name, brand_url, brand_context)
    messages = [{"role": "user", "content": "Begin the interview. Open with your first confident proposal grounded in the crawl."}]
    return call_claude(system, messages, enable_web_search=True)


def next_assistant_turn(brand_name: str, brand_url: str, brand_context: str,
                         history: list[dict[str, str]]) -> str:
    """Given full history (user + assistant), produce the next assistant turn."""
    system = build_system_prompt(brand_name, brand_url, brand_context)
    return call_claude(system, history, enable_web_search=True)


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
