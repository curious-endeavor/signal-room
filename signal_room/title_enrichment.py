from __future__ import annotations

import json
import os
import re
from typing import Any

import requests


DEFAULT_MODEL = "gpt-4.1-mini"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


class TitleEnrichmentError(RuntimeError):
    pass


def clean_result_titles(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if not items:
        return items, ""

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _with_original_titles(items), "Title cleanup skipped: OPENAI_API_KEY is not configured"

    limit = _cleanup_limit()
    target_items = items[:limit]
    try:
        title_map = _request_clean_titles(api_key, target_items)
    except TitleEnrichmentError as exc:
        return _with_original_titles(items), f"Title cleanup skipped: {exc}"

    cleaned = []
    for item in items:
        row = dict(item)
        original_title = str(row.get("original_title") or row.get("title") or "").strip()
        row["original_title"] = original_title
        proposed_title = _clean_title_value(title_map.get(str(row.get("id", ""))))
        if proposed_title:
            row["title"] = proposed_title
        cleaned.append(row)
    return cleaned, ""


def _request_clean_titles(api_key: str, items: list[dict[str, Any]]) -> dict[str, str]:
    payload = {
        "model": os.environ.get("SIGNAL_ROOM_TITLE_MODEL")
        or os.environ.get("OPENAI_MODEL_PIN")
        or DEFAULT_MODEL,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "You rewrite noisy social/search result titles into clean, factual, skim-friendly titles. "
                            "Preserve factual claims, proper nouns, and numbers. Remove emojis, hashtags, duplicated phrases, "
                            "engagement bait, boilerplate, and truncation artifacts. Do not invent facts. "
                            "Target 8-16 words when possible. Return only valid JSON."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "items": [
                                    {
                                        "id": str(item.get("id", "")),
                                        "title": str(item.get("title", ""))[:500],
                                        "source": str(item.get("source", ""))[:120],
                                        "summary": str(item.get("summary", ""))[:400],
                                    }
                                    for item in items
                                ],
                                "return_shape": {"titles": [{"id": "same item id", "title": "clean display title"}]},
                            },
                            ensure_ascii=True,
                        ),
                    }
                ],
            },
        ],
        "temperature": 0.2,
    }
    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=_timeout_seconds(),
        )
    except requests.RequestException as exc:
        raise TitleEnrichmentError(str(exc)) from exc

    if response.status_code >= 400:
        raise TitleEnrichmentError(f"OpenAI returned HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise TitleEnrichmentError("OpenAI returned non-JSON response") from exc

    text = _response_text(body)
    if not text:
        raise TitleEnrichmentError("OpenAI returned no text")
    try:
        parsed = json.loads(_strip_json_fence(text))
    except ValueError as exc:
        raise TitleEnrichmentError("OpenAI returned malformed title JSON") from exc

    rows = parsed.get("titles") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise TitleEnrichmentError("OpenAI title JSON did not include titles")

    title_map: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id", "")).strip()
        title = _clean_title_value(row.get("title"))
        if item_id and title:
            title_map[item_id] = title
    if not title_map:
        raise TitleEnrichmentError("OpenAI returned no usable titles")
    return title_map


def _response_text(body: dict[str, Any]) -> str:
    output_text = body.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()

    chunks = []
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


def _clean_title_value(value: Any) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    title = title.strip(" -|")
    if len(title) < 4:
        return ""
    return title[:160]


def _with_original_titles(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        row = dict(item)
        row["original_title"] = str(row.get("original_title") or row.get("title") or "").strip()
        rows.append(row)
    return rows


def _cleanup_limit() -> int:
    try:
        return max(1, int(os.environ.get("SIGNAL_ROOM_TITLE_CLEANUP_LIMIT", "40")))
    except ValueError:
        return 40


def _timeout_seconds() -> int:
    try:
        return max(5, int(os.environ.get("SIGNAL_ROOM_TITLE_CLEANUP_TIMEOUT", "35")))
    except ValueError:
        return 35
