"""Projector: brand-audit brief.yaml -> signal-room config files.

Per SPEC §4c.1 — the contract between brand-audit (research orchestrator)
and signal-room (runtime). brand-audit produces `brief.yaml`; this script
consumes it and writes the JSON configs signal-room reads on startup.

v1 scope: writes discovery_queries.json + seed_sources.json. Keeps
PILLAR_KEYWORDS / SURF_KEYWORDS hardcoded in scoring.py (CE-only) until
the multi-tenant pillar refactor lands. That's the next step, not this one.

Usage:
    python3 -m signal_room.projector.from_brief --brief PATH [--out DIR]

If --out is omitted, writes to ./config/ (signal-room's default config dir).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required. pip install pyyaml", file=sys.stderr)
    sys.exit(2)


def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or url
        return host.replace("www.", "")
    except Exception:
        return url


def project_discovery_queries(projection: Dict[str, Any]) -> Dict[str, Any]:
    """projection.signal_room.discovery_queries -> discovery_queries.json shape."""
    queries = projection.get("signal_room", {}).get("discovery_queries", []) or []
    out_queries: List[Dict[str, Any]] = []
    for q in queries:
        if not isinstance(q, dict):
            continue
        out_queries.append({
            "id": q.get("id", ""),
            "priority": int(q.get("priority", 2)),
            "topic": q.get("topic", ""),
            "why": q.get("why", ""),
        })
    out_queries.sort(key=lambda q: (q["priority"], q["id"]))
    return {
        "daily_query_limit": min(6, len(out_queries)) or 6,
        "queries": out_queries,
    }


def project_pillar_keywords(projection: Dict[str, Any]) -> Dict[str, List[str]]:
    """projection.signal_room.pillars[].keywords -> pillar_keywords.json shape.

    Output: {"P1": ["phrase a", "phrase b"], ...} — the format scoring.py expects.
    Handles both bare-string keywords and {phrase, why} object keywords.
    """
    pillars = projection.get("signal_room", {}).get("pillars", []) or []
    out: Dict[str, List[str]] = {}
    for p in pillars:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if not pid:
            continue
        words: List[str] = []
        for k in p.get("keywords", []) or []:
            if isinstance(k, str):
                words.append(k.lower())
            elif isinstance(k, dict):
                phrase = k.get("phrase") or k.get("keyword") or ""
                if phrase:
                    words.append(str(phrase).lower())
        if words:
            out[pid] = words
    return out


def project_seed_sources(projection: Dict[str, Any]) -> Dict[str, Any]:
    """projection.signal_room.seed_sources -> seed_sources.json shape.

    Handles both shapes:
      - bare URL string (current CE brief)
      - {url, why} object (concise+argued shape)
      - {url, why, name, category} object (future canonical shape)
    """
    raw = projection.get("signal_room", {}).get("seed_sources", []) or []
    out: List[Dict[str, str]] = []
    for s in raw:
        if isinstance(s, str):
            url = s
            why = ""
            name = _domain(url)
            category = "uncategorized"
        elif isinstance(s, dict):
            url = s.get("url", "")
            why = s.get("why", "") or s.get("reason", "")
            name = s.get("name") or _domain(url)
            category = s.get("category", "uncategorized")
        else:
            continue
        if not url:
            continue
        out.append({
            "name": name,
            "category": category,
            "url": url,
            "reason": why,
        })
    return {"sources": out}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signal_room.projector.from_brief")
    parser.add_argument("--brief", required=True, type=Path, help="Path to brand-audit brief.yaml")
    parser.add_argument("--out", type=Path, default=Path("config"), help="Output config directory (default: ./config)")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout instead of writing files")
    args = parser.parse_args(argv)

    if not args.brief.exists():
        print(f"ERROR: brief not found: {args.brief}", file=sys.stderr)
        return 2

    brief = yaml.safe_load(args.brief.read_text(encoding="utf-8")) or {}
    projection = brief.get("projection", {}) or {}
    if not projection:
        print("ERROR: brief has no projection block — cannot produce signal-room config", file=sys.stderr)
        return 3

    discovery = project_discovery_queries(projection)
    seeds = project_seed_sources(projection)
    pillars = project_pillar_keywords(projection)

    if args.dry_run:
        print("=== discovery_queries.json ===")
        print(json.dumps(discovery, indent=2))
        print("=== seed_sources.json ===")
        print(json.dumps(seeds, indent=2))
        print("=== pillar_keywords.json ===")
        print(json.dumps(pillars, indent=2))
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    dq_path = args.out / "discovery_queries.json"
    ss_path = args.out / "seed_sources.json"
    pk_path = args.out / "pillar_keywords.json"
    dq_path.write_text(json.dumps(discovery, indent=2) + "\n", encoding="utf-8")
    ss_path.write_text(json.dumps(seeds, indent=2) + "\n", encoding="utf-8")
    pk_path.write_text(json.dumps(pillars, indent=2) + "\n", encoding="utf-8")

    print(f"[projector] wrote {dq_path} ({len(discovery['queries'])} queries)")
    print(f"[projector] wrote {ss_path} ({len(seeds['sources'])} sources)")
    print(f"[projector] wrote {pk_path} ({len(pillars)} pillars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
