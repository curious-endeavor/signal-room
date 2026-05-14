"""Admin CLI for brand rows in the Signal Room store.

The web onboarding flow can create rows but offers no way to inspect or remove
them. This CLI fills that gap. Hits the same Postgres/PGLite that the web app
uses (via signal_room.web_store).

Usage:
  python3 scripts/admin_brands.py list
  python3 scripts/admin_brands.py show <slug>
  python3 scripts/admin_brands.py delete <slug> [--yes]
  python3 scripts/admin_brands.py reset-brief <slug> [--yes]
      Clear brief_json back to the minimal {name, url} placeholder so the
      brand can be re-onboarded from a clean slate without losing the slug.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Run from anywhere — make sure the repo root is on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from signal_room.web_store import SignalRoomStore  # noqa: E402


def _store() -> SignalRoomStore:
    s = SignalRoomStore()
    s.initialize()
    return s


def cmd_list(_args) -> int:
    rows = _store().list_brands(limit=500)
    if not rows:
        print("(no brands)")
        return 0
    for r in rows:
        print(f"{r['slug']:24} {r.get('name',''):28} {r.get('url','')}")
    return 0


def cmd_show(args) -> int:
    row = _store().get_brand(args.slug)
    if not row:
        print(f"no brand with slug={args.slug!r}", file=sys.stderr)
        return 2
    brief = row.get("brief_json") or {}
    queries = [(q.get("id"), q.get("topic")) for q in brief.get("discovery_queries", [])]
    out = {
        "slug": row.get("slug"),
        "name": row.get("name"),
        "url": row.get("url"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "brief_name": brief.get("name"),
        "brief_url": brief.get("url"),
        "discovery_query_count": len(queries),
        "discovery_query_ids": [q[0] for q in queries],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def _confirm(prompt: str, force: bool) -> bool:
    if force:
        return True
    sys.stderr.write(prompt + " [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


def cmd_delete(args) -> int:
    store = _store()
    row = store.get_brand(args.slug)
    if not row:
        print(f"no brand with slug={args.slug!r}", file=sys.stderr)
        return 2
    if not _confirm(
        f"Delete brand {args.slug!r} ({row.get('url','')}) and all chat sessions, "
        f"messages, and brand_runs?",
        args.yes,
    ):
        print("aborted")
        return 1
    counts = store.delete_brand(args.slug)
    print(json.dumps({"deleted": args.slug, "counts": counts}, indent=2))
    return 0


def cmd_reset_brief(args) -> int:
    store = _store()
    row = store.get_brand(args.slug)
    if not row:
        print(f"no brand with slug={args.slug!r}", file=sys.stderr)
        return 2
    if not _confirm(
        f"Reset brief_json for {args.slug!r} to the placeholder shape "
        f"(keeps the slug + passcode + run history)?",
        args.yes,
    ):
        print("aborted")
        return 1
    placeholder = {"name": row.get("name") or args.slug, "url": row.get("url") or ""}
    store.update_brand_brief(args.slug, placeholder)
    print(json.dumps({"reset": args.slug, "brief_json": placeholder}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or clean up Signal Room brand rows.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List all brands.").set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show a single brand row (truncated).")
    p_show.add_argument("slug")
    p_show.set_defaults(func=cmd_show)

    p_del = sub.add_parser("delete", help="Delete a brand row + its chat/run history.")
    p_del.add_argument("slug")
    p_del.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    p_del.set_defaults(func=cmd_delete)

    p_reset = sub.add_parser("reset-brief", help="Wipe brief_json back to placeholder.")
    p_reset.add_argument("slug")
    p_reset.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    p_reset.set_defaults(func=cmd_reset_brief)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
