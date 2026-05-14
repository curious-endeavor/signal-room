"""Rescore a brand's last fetched items without re-running the fetch.

When iterating on slim sampling, scoring rubric, or digest layout, paying
~$3–5 + ~5 minutes for a full fetch is wasteful — the raw items haven't
changed. This script reuses `data/<brand>/discovered_items.json` (the merged
last30days + GDELT output) and re-runs only:

  dedup → slim cap (stratified) → LLM score (or keyword) → digest HTML

Usage:
  python3 scripts/rescore.py <brand> [--cap N] [--no-llm] [--open]

Examples:
  python3 scripts/rescore.py alice                  # full LLM rescore at default cap
  python3 scripts/rescore.py alice --cap 30         # cheaper, 30 items
  python3 scripts/rescore.py alice --no-llm         # skip Claude, use keyword scorer
  python3 scripts/rescore.py alice --open           # open the digest in $BROWSER when done

The brief is resolved DB-first (matching the worker), with a disk fallback.
Outputs:
  data/<brand>/enriched_items.json        (overwritten)
  output/signal-room-digest-<brand>-rescore-<ts>.html  (timestamped, never overwrites)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from signal_room.ingest import load_raw_items  # noqa: E402
from signal_room.digest import render_digest  # noqa: E402
from signal_room.scoring import score_items as _keyword_score  # noqa: E402
from signal_room.storage import DATA_DIR, OUTPUT_DIR  # noqa: E402
from signal_room.web_store import SignalRoomStore  # noqa: E402


def _stratified_slim(raw_items, cap):
    """Same allocation logic as pipeline.run_pipeline, factored for reuse."""
    if cap <= 0 or len(raw_items) <= cap:
        return raw_items, {"unsliced": len(raw_items)}
    buckets = {}
    for item in raw_items:
        key = getattr(item, "discovery_method", None) or "unknown"
        buckets.setdefault(key, []).append(item)
    if len(buckets) <= 1:
        return raw_items[:cap], {next(iter(buckets), "unknown"): cap}
    total = sum(len(v) for v in buckets.values())
    allocation = {k: max(1, (cap * len(v)) // total) for k, v in buckets.items()}
    diff = cap - sum(allocation.values())
    order = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))
    i = 0
    step = 1 if diff > 0 else -1
    while diff != 0 and i < len(order) * 4:
        k = order[i % len(order)]
        if allocation[k] + step >= 1 and allocation[k] + step <= len(buckets[k]):
            allocation[k] += step
            diff -= step
        i += 1
    kept = []
    for k, n in allocation.items():
        kept.extend(buckets[k][:n])
    return kept, allocation


def _resolve_brief_path(brand: str) -> Path:
    """DB-first, like the worker. Materialize a temp brief.yaml when only the
    DB row has pillars; fall back to the repo's config/brands/<brand>/brief.yaml
    when both are empty."""
    store = SignalRoomStore()
    store.initialize()
    row = store.get_brand(brand) or {}
    brief_dict = row.get("brief_json") or {}
    repo_brief = _REPO_ROOT / "config" / "brands" / brand / "brief.yaml"
    if brief_dict.get("pillars") or brief_dict.get("discovery_queries"):
        wrapped = brief_dict if "projection" in brief_dict else {
            "brand": {
                "name": brief_dict.get("name", brand),
                "slug": brand,
                "url": brief_dict.get("url", ""),
                "one_liner": brief_dict.get("one_liner", ""),
                "audience": brief_dict.get("audience", []),
            },
            "projection": {
                "signal_room": {
                    "pillars": brief_dict.get("pillars", []),
                    "discovery_queries": brief_dict.get("discovery_queries", []),
                    "seed_sources": brief_dict.get("seed_sources", []),
                },
            },
        }
        tmp_dir = Path(tempfile.gettempdir()) / f"sr-rescore-{brand}-{int(time.time())}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        brief_path = tmp_dir / "brief.yaml"
        brief_path.write_text(yaml.safe_dump(wrapped, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return brief_path
    if repo_brief.exists():
        return repo_brief
    raise FileNotFoundError(f"no brief found for brand={brand!r} in DB or at {repo_brief}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("brand", help="Brand slug (e.g. alice, curiousendeavor-2).")
    parser.add_argument("--cap", type=int, default=None,
                        help="Slim cap. Default: 10 × pillar_count (matches slim-run mode).")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip Claude scoring; use the keyword scorer instead. ~free.")
    parser.add_argument("--open", action="store_true",
                        help="Open the rendered digest in the default browser when done.")
    args = parser.parse_args(argv)

    brand = args.brand
    brand_data_dir = DATA_DIR / brand
    discovered_path = brand_data_dir / "discovered_items.json"
    if not discovered_path.exists():
        print(f"ERROR: no cached fetch for {brand!r} at {discovered_path}", file=sys.stderr)
        return 2

    discovered = json.loads(discovered_path.read_text(encoding="utf-8"))
    raw_items = load_raw_items({"sources": []}, [discovered])
    print(f"[rescore] {brand} · loaded {len(raw_items)} items from cache")

    # Show what we have to work with
    methods = Counter(i.discovery_method for i in raw_items)
    print(f"[rescore] discovery_method mix: {dict(methods)}")

    brief_path = _resolve_brief_path(brand)
    brief = yaml.safe_load(brief_path.read_text(encoding="utf-8")) or {}
    pillars = ((brief.get("projection") or {}).get("signal_room") or {}).get("pillars") or []
    cap = args.cap if args.cap is not None else max(10, 10 * len(pillars))
    print(f"[rescore] brief={brief_path.name} pillars={len(pillars)} cap={cap}")

    kept, allocation = _stratified_slim(raw_items, cap)
    print(f"[rescore] stratified slim: {dict(allocation)}  → {len(kept)} items to score")

    if args.no_llm:
        from signal_room.scoring import DEFAULT_WEIGHTS  # avoid earlier import while os.environ is fresh
        scored = _keyword_score(kept, DEFAULT_WEIGHTS, [], {})
        print(f"[rescore] keyword-scored {len(scored)} items (no Claude calls)")
    else:
        from signal_room.llm_scoring import score_items_with_brief
        scored = score_items_with_brief(kept, brief_path)
        print(f"[rescore] LLM-scored {len(scored)} items via Claude")

    enriched_path = brand_data_dir / "enriched_items.json"
    enriched_path.write_text(
        json.dumps([item.to_dict() for item in scored], indent=2) + "\n",
        encoding="utf-8",
    )
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    digest_path = OUTPUT_DIR / f"signal-room-digest-{brand}-rescore-{ts}.html"
    render_digest(scored[:10], digest_path)

    print(f"[rescore] wrote {enriched_path}")
    print(f"[rescore] wrote {digest_path}")
    print("---top 10 scored---")
    for i, item in enumerate(scored[:10], 1):
        fit = (item.metadata or {}).get("fit", "?")
        print(f"  {i:2d}. {int(item.score):3d} {fit:13s} · {item.title[:90]}")

    if args.open:
        webbrowser.open(f"file://{digest_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
