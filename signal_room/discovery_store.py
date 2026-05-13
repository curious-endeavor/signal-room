"""Merge-and-persist helper for the discovered-items payload.

Multiple fetchers (currently `last30days` and `gdelt`) emit rows into one
shared file (`data/discovered_items.json`). When more than one fetcher
runs in the same pipeline invocation, this helper normalizes URLs, merges
overlapping rows so a single article appears once with both source markers,
and preserves the earliest `first_seen_at` stamp across re-fetches.

Public surface:
    write_merged_discovered_items(path, payloads, generated_at=None) -> dict
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .storage import read_json, write_json


# Tracking params we drop from URL keys so the same article surfacing with
# different campaign tags collapses into one row.
_DROPPED_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


def normalize_url(url: str) -> str:
    """Return a canonical key for the given URL.

    Rules:
      - lowercase scheme and host
      - drop fragments
      - drop tracking query params (utm_*, fbclid, gclid, ...)
      - strip a trailing slash on the path (but preserve "/")
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip()

    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _DROPPED_QUERY_KEYS
    ]
    query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def _infer_source(row: Dict[str, Any]) -> Optional[str]:
    """Infer the source marker for a row that predates meta.source stamping."""
    meta = row.get("meta") or {}
    if isinstance(meta, dict):
        existing = meta.get("source")
        if existing:
            return existing
    discovery = row.get("discovery_method")
    if discovery in ("last30days", "gdelt"):
        return discovery
    return None


def _source_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _merge_rows(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two rows that share a normalized URL.

    Preserves the earliest first_seen_at, unions meta.source, prefers
    non-empty summary/content/engagement from whichever side has them,
    and merges metadata dicts (incoming wins on conflict for raw fields).
    """
    merged = dict(existing)

    # first_seen_at — keep earliest
    e_first = existing.get("first_seen_at")
    i_first = incoming.get("first_seen_at")
    if e_first and i_first:
        merged["first_seen_at"] = min(e_first, i_first)
    elif e_first:
        merged["first_seen_at"] = e_first
    else:
        merged["first_seen_at"] = i_first

    # meta.source — sorted unique union, inferring from discovery_method when absent
    sources = set()
    sources.update(_source_list((existing.get("meta") or {}).get("source")))
    if not sources:
        inferred = _infer_source(existing)
        if inferred:
            sources.add(inferred)
    sources.update(_source_list((incoming.get("meta") or {}).get("source")))
    if not (incoming.get("meta") or {}).get("source"):
        inferred = _infer_source(incoming)
        if inferred:
            sources.add(inferred)
    meta = dict(existing.get("meta") or {})
    meta.update({k: v for k, v in (incoming.get("meta") or {}).items() if k != "source"})
    meta["source"] = sorted(sources)
    merged["meta"] = meta

    # Prefer non-empty richer fields
    for field in ("summary", "content"):
        if not (existing.get(field) or "").strip() and (incoming.get(field) or "").strip():
            merged[field] = incoming[field]

    # engagement — keep whichever has more keys
    e_eng = existing.get("engagement") or {}
    i_eng = incoming.get("engagement") or {}
    if len(i_eng) > len(e_eng):
        merged["engagement"] = i_eng

    # metadata — shallow merge, incoming wins on conflicting keys
    e_md = dict(existing.get("metadata") or {})
    e_md.update(incoming.get("metadata") or {})
    merged["metadata"] = e_md

    # tags — union preserving order, existing first
    seen = set()
    out_tags: List[str] = []
    for tag in list(existing.get("tags") or []) + list(incoming.get("tags") or []):
        if tag not in seen:
            seen.add(tag)
            out_tags.append(tag)
    if out_tags:
        merged["tags"] = out_tags

    return merged


def _iter_rows(payload: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                yield row
        return
    if isinstance(payload, dict):
        rows = payload.get("items")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    yield row


def _stamp_first_seen(row: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
    if not row.get("first_seen_at"):
        row = dict(row)
        row["first_seen_at"] = now_iso
    return row


def write_merged_discovered_items(
    path: Path,
    payloads: List[Dict[str, Any]],
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge backend payloads with the existing on-disk file and persist.

    Args:
      path: target file (typically `data/discovered_items.json`).
      payloads: list of per-backend payloads. Each payload may be:
        - a dict shaped like `{"items": [...], ...}` (the shape emitted by
          `fetch_gdelt` and `fetch_last30days`), or
        - a raw list of row dicts (legacy fixture shape).
      generated_at: optional override for the output's generated_at field.

    Returns the persisted merged payload.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # Seed the merge with whatever is already on disk so re-fetches preserve
    # first_seen_at and don't lose prior backends' rows.
    existing_payload = read_json(path, {"items": []}) if path.exists() else {"items": []}
    merged: Dict[str, Dict[str, Any]] = {}
    backends_seen: List[str] = []
    errors: List[Dict[str, Any]] = []
    runs: List[Dict[str, Any]] = []

    for row in _iter_rows(existing_payload):
        url = row.get("source_url") or ""
        key = normalize_url(url)
        if not key:
            continue
        merged[key] = _stamp_first_seen(row, now_iso)

    for payload in payloads:
        if isinstance(payload, dict):
            backend = payload.get("backend")
            if backend and backend not in backends_seen:
                backends_seen.append(backend)
            payload_errors = payload.get("errors") or []
            if isinstance(payload_errors, list):
                errors.extend(payload_errors)
            payload_runs = payload.get("runs") or []
            if isinstance(payload_runs, list):
                runs.extend(payload_runs)
        for row in _iter_rows(payload):
            url = row.get("source_url") or ""
            key = normalize_url(url)
            if not key:
                continue
            row = _stamp_first_seen(row, now_iso)
            if key in merged:
                merged[key] = _merge_rows(merged[key], row)
            else:
                merged[key] = row

    items = list(merged.values())
    output: Dict[str, Any] = {
        "generated_at": generated_at or now_iso,
        "backend": "merged" if len(backends_seen) != 1 else backends_seen[0],
        "backends": backends_seen,
        "item_count": len(items),
        "items": items,
        "errors": errors,
        "runs": runs,
    }
    write_json(path, output)
    output["discovered_items_path"] = str(path)
    return output
