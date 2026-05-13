#!/usr/bin/env python3
"""Bootstrap GDELT pillars for the Alice brand.

Reads `config/brands/alice/brief.yaml` to confirm the expected Alice
pillar IDs still exist (so a refactor of the brief surfaces clearly),
then creates/updates the 10 GDELT pillars defined in origin §4 Step 4
via `gdelt-pp-cli pillar add`.

The inline `ALICE_PILLARS` map is the source of truth for GDELT boolean
query syntax — `brief.yaml` carries human topic descriptions, not ready-
to-run GDELT strings. See:
  docs/plans/2026-05-13-integrate-gdelt-source.md  (origin brief)
  docs/plans/2026-05-13-002-feat-gdelt-fetcher-plan.md  (this work)

Idempotent: re-running this script is safe — pillars whose query already
matches are skipped; pillars whose query differs are removed and re-added.

Honors $GDELT_PILLARS_PATH so test runs do not mutate the user's
~/.config/gdelt-pp-cli/pillars.json file.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIEF = REPO_ROOT / "config" / "brands" / "alice" / "brief.yaml"


# Pillar name → GDELT boolean query.
#
# Origin §4 Step 4 is the source of truth. Short-acronym statutes (EU AI Act,
# AB 1988, ISO 42001, NIST AI RMF, MITRE ATLAS) are split into separate
# pillars because GDELT silently returns empty results when those phrases
# are mega-OR'd together (origin §6 gotcha 2).
ALICE_PILLARS: Dict[str, str] = {
    "chatbot-failures": (
        '("chatbot" OR "AI agent" OR "AI assistant") AND '
        "(lawsuit OR scandal OR fail OR harm OR sycophancy OR hallucination OR \"customer service\")"
    ),
    "frontier-model-safety": (
        '("red team" OR jailbreak OR "prompt injection" OR "system card" OR "alignment research") AND '
        "(Anthropic OR OpenAI OR DeepMind OR \"Irregular\")"
    ),
    "ai-security-tooling": (
        '("Palo Alto Networks" OR Zscaler OR Lakera OR "Robust Intelligence" OR HiddenLayer OR '
        '"Protect AI" OR Patronus OR CalypsoAI OR "Prompt Security") AND '
        "(security OR guardrails OR \"red team\" OR \"prompt injection\")"
    ),
    "data-generalists": (
        '("Scale AI" OR "Surge AI" OR Prolific OR Toloka OR Mercor) AND '
        "(safety OR \"red team\" OR evaluation OR benchmark)"
    ),
    # P4 — one pillar per phrase (do NOT mega-OR these short acronyms).
    "ai-reg-eu-act": '"EU AI Act"',
    "ai-reg-ab-1988": '"AB 1988"',
    "ai-reg-iso-42001": '"ISO 42001"',
    "ai-reg-nist-rmf": '"NIST AI RMF"',
    "ai-reg-mitre": '"MITRE ATLAS"',
    "regulated-vertical-ai": (
        '("AI chatbot" OR "AI assistant" OR "AI agent") AND '
        "(healthcare OR patient OR fintech OR insurance OR \"claims agent\" OR pharmacy)"
    ),
}

# Pillar IDs we expect to find in brief.yaml. If any of these go missing,
# the brief shape has changed and this script's mapping may be stale.
EXPECTED_BRIEF_PILLAR_IDS = {"P1", "P2", "P3", "P3b", "P4", "P5"}


class BootstrapError(RuntimeError):
    pass


def _resolve_binary(override: Optional[str]) -> str:
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise BootstrapError(f"--binary does not exist: {override}")
        return str(path)
    # Reuse the fetcher's resolver chain.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from signal_room.fetchers.gdelt import _resolve_binary, GdeltError
        try:
            return _resolve_binary()
        except GdeltError as exc:
            raise BootstrapError(str(exc)) from exc
    finally:
        sys.path.pop(0)


def _parse_brief_pillar_ids(brief_path: Path) -> List[str]:
    """Light-touch parse: find every `- id: PX` line under pillars.

    Avoids a hard PyYAML dependency for a one-shot bootstrap script.
    """
    if not brief_path.exists():
        raise BootstrapError(f"brief.yaml not found: {brief_path}")
    text = brief_path.read_text(encoding="utf-8")
    return re.findall(r"^\s*-\s*id:\s*(\S+)\s*$", text, flags=re.MULTILINE)


def _list_pillars(binary: str, pillars_path: Optional[str]) -> Dict[str, str]:
    env = dict(os.environ)
    if pillars_path:
        env["GDELT_PILLARS_PATH"] = pillars_path
    result = subprocess.run(
        [binary, "pillar", "list", "--json"],
        capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        raise BootstrapError(
            f"`gdelt-pp-cli pillar list` failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"Could not parse `pillar list` JSON: {exc}") from exc
    out: Dict[str, str] = {}
    for entry in payload.get("pillars") or []:
        name = str(entry.get("name", "")).strip()
        query = str(entry.get("query", ""))
        if name:
            out[name] = query
    return out


def _add_pillar(binary: str, pillars_path: Optional[str], name: str, query: str, dry_run: bool) -> None:
    cmd = [binary, "pillar", "add", name, query]
    if dry_run:
        print(f"  + add {name!r}: {query}")
        return
    env = dict(os.environ)
    if pillars_path:
        env["GDELT_PILLARS_PATH"] = pillars_path
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise BootstrapError(
            f"`pillar add {name}` failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def _rm_pillar(binary: str, pillars_path: Optional[str], name: str, dry_run: bool) -> None:
    cmd = [binary, "pillar", "rm", name]
    if dry_run:
        print(f"  - rm {name!r}")
        return
    env = dict(os.environ)
    if pillars_path:
        env["GDELT_PILLARS_PATH"] = pillars_path
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise BootstrapError(
            f"`pillar rm {name}` failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def bootstrap(
    brief_path: Path,
    binary: str,
    pillars_path: Optional[str],
    dry_run: bool,
) -> Dict[str, int]:
    brief_ids = set(_parse_brief_pillar_ids(brief_path))
    missing = EXPECTED_BRIEF_PILLAR_IDS - brief_ids
    if missing:
        raise BootstrapError(
            f"brief.yaml is missing expected pillar IDs: {sorted(missing)}. "
            "The brief shape may have changed — update ALICE_PILLARS or "
            "EXPECTED_BRIEF_PILLAR_IDS accordingly."
        )

    existing = _list_pillars(binary, pillars_path)
    added = updated = skipped = 0
    for name, query in ALICE_PILLARS.items():
        if name not in existing:
            print(f"[+] add {name}")
            _add_pillar(binary, pillars_path, name, query, dry_run)
            added += 1
        elif existing[name].strip() != query.strip():
            print(f"[~] update {name} (query changed)")
            _rm_pillar(binary, pillars_path, name, dry_run)
            _add_pillar(binary, pillars_path, name, query, dry_run)
            updated += 1
        else:
            print(f"[=] skip {name} (already up to date)")
            skipped += 1
    return {"added": added, "updated": updated, "skipped": skipped}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap GDELT pillars for the Alice brand from brief.yaml.",
    )
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF, help="Path to brief.yaml")
    parser.add_argument("--binary", default="", help="Override gdelt-pp-cli binary path")
    parser.add_argument("--pillars-path", default="", help="Override $GDELT_PILLARS_PATH")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    args = parser.parse_args(argv)

    try:
        binary = _resolve_binary(args.binary or None)
        pillars_path = args.pillars_path or os.environ.get("GDELT_PILLARS_PATH")
        summary = bootstrap(
            brief_path=args.brief,
            binary=binary,
            pillars_path=pillars_path,
            dry_run=args.dry_run,
        )
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    total = sum(summary.values())
    print(
        f"\ndone — {total} pillar(s) processed "
        f"(added={summary['added']}, updated={summary['updated']}, skipped={summary['skipped']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
