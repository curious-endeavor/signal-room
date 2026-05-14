#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LAST30DAYS = ROOT / "vendor" / "last30days-skill" / "scripts" / "last30days.py"
PYTHON = ROOT / ".venvs" / "last30days" / "bin" / "python"


@dataclass(frozen=True)
class PlatformProfile:
    source: str
    label: str
    templates: tuple[tuple[str, str], ...]
    likely: tuple[str, ...]
    warnings: tuple[str, ...]
    best_for: str


PROFILES = (
    PlatformProfile(
        source="reddit",
        label="Reddit",
        templates=(
            ("Workflow evidence", "{query} workflow"),
            ("Operator threads", "using {query} workflows"),
            ("Problems and limits", "{query} problems workflow"),
        ),
        likely=("practitioner threads", "complaints", "tool stacks", "workflow detail"),
        warnings=("advice threads can be repetitive", "some answers are anecdotal"),
        best_for="finding first-person usage, objections, and messy implementation details",
    ),
    PlatformProfile(
        source="x",
        label="X",
        templates=(
            ("Recent launches", "{query} launch workflow"),
            ("Operator posts", "{query} building in public"),
            ("Screenshots and examples", "{query} screenshot example"),
        ),
        likely=("launches", "hot takes", "screenshots", "founder/operator posts"),
        warnings=("high self-promotion", "thin context unless posts link out"),
        best_for="recent signals, product announcements, and visible market momentum",
    ),
    PlatformProfile(
        source="youtube",
        label="YouTube",
        templates=(
            ("Walkthroughs", "{query} walkthrough"),
            ("Tutorials", "{query} tutorial workflow"),
            ("Demos", "{query} demo examples"),
        ),
        likely=("tutorials", "tool demos", "explainers", "long-form walkthroughs"),
        warnings=("creator funnel content", "may skew toward beginner material"),
        best_for="seeing workflows, UI patterns, and step-by-step implementation",
    ),
    PlatformProfile(
        source="instagram",
        label="Instagram",
        templates=(
            ("Visual examples", "{query} examples"),
            ("Creator posts", "{query} creator workflow"),
            ("Before/after", "{query} before after"),
        ),
        likely=("visual examples", "creator/business posts", "portfolio-style proof"),
        warnings=("captions can be shallow", "harder to judge substance from snippets"),
        best_for="visual categories, brand/design examples, and creator behavior",
    ),
    PlatformProfile(
        source="github",
        label="GitHub",
        templates=(
            ("Open-source tools", "{query} open source"),
            ("Repos and agents", "{query} agent workflow"),
            ("Implementation proof", "{query} automation repo"),
        ),
        likely=("repositories", "tools", "automation scripts", "implementation artifacts"),
        warnings=("weak for market discussion", "many repos are abandoned or toy projects"),
        best_for="checking whether people are building tools, not just talking about them",
    ),
    PlatformProfile(
        source="hackernews",
        label="HN",
        templates=(
            ("Technical discussion", "{query} workflow"),
            ("Skeptical takes", "{query} problems"),
            ("Product reactions", "{query} launch"),
        ),
        likely=("technical discussion", "skepticism", "startup/operator reactions"),
        warnings=("less coverage for mainstream marketing topics", "discussion can be narrow"),
        best_for="technical skepticism, product reactions, and operational tradeoffs",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="POC social-first query preview.")
    parser.add_argument("query", help="Rough user query to improve.")
    parser.add_argument("--emit", choices=["text", "json"], default="text")
    parser.add_argument("--live", action="store_true", help="Run shallow live probes for selected suggestions.")
    parser.add_argument("--sources", default="reddit,x,youtube,github,hackernews", help="Comma-separated sources to include.")
    parser.add_argument("--probe-limit", type=int, default=3, help="Maximum suggestions to live-probe.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window for live probes.")
    args = parser.parse_args()

    selected = {source.strip() for source in args.sources.split(",") if source.strip()}
    suggestions = build_suggestions(args.query, selected)
    if args.live:
        for suggestion in suggestions[: max(0, args.probe_limit)]:
            suggestion["probe"] = run_probe(suggestion["source"], suggestion["query"], args.days)

    payload = {
        "query": args.query.strip(),
        "diagnosis": diagnose_query(args.query),
        "suggestions": suggestions,
    }
    if args.emit == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(payload))
    return 0


def build_suggestions(query: str, selected_sources: set[str]) -> list[dict[str, Any]]:
    clean = normalize_query(query)
    suggestions: list[dict[str, Any]] = []
    for profile in PROFILES:
        if profile.source not in selected_sources:
            continue
        for label, template in profile.templates[:2]:
            rendered = normalize_query(template.format(query=clean))
            suggestions.append(
                {
                    "source": profile.source,
                    "platform": profile.label,
                    "label": label,
                    "query": rendered,
                    "best_for": profile.best_for,
                    "likely_results": list(profile.likely),
                    "warnings": list(profile.warnings),
                    "checks": planned_checks(profile.source, rendered),
                }
            )
    return rank_suggestions(suggestions, clean)


def rank_suggestions(suggestions: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    query_terms = content_terms(query)
    for suggestion in suggestions:
        score = 0
        text = suggestion["query"].lower()
        if suggestion["source"] in {"reddit", "x", "youtube"}:
            score += 3
        if any(term in text for term in ("workflow", "using", "use cases", "problems", "demo", "launch")):
            score += 2
        if len(query_terms) <= 2 and suggestion["source"] == "reddit":
            score += 2
        if suggestion["source"] == "github" and not any(term in text for term in ("repo", "open source", "agent", "automation")):
            score -= 1
        suggestion["preview_priority"] = score
    return sorted(suggestions, key=lambda row: row["preview_priority"], reverse=True)


def planned_checks(source: str, query: str) -> list[str]:
    checks = ["result_count", "freshness", "engagement"]
    if source == "reddit":
        checks.extend(["comment_count", "first_person_language", "subreddit_diversity"])
    elif source == "x":
        checks.extend(["author_diversity", "launch_language", "self_promo_rate"])
    elif source == "youtube":
        checks.extend(["demo_or_tutorial_language", "channel_diversity"])
    elif source == "instagram":
        checks.extend(["visual_example_language", "creator_or_brand_context"])
    elif source == "github":
        checks.extend(["repo_activity", "stars_or_forks", "implementation_language"])
    elif source == "hackernews":
        checks.extend(["comment_discussion", "skepticism_language"])
    return checks


def run_probe(source: str, query: str, days: int) -> dict[str, Any]:
    if not LAST30DAYS.exists():
        return {"ok": False, "error": f"Missing last30days script: {LAST30DAYS}"}
    if not PYTHON.exists():
        return {"ok": False, "error": f"Missing last30days python: {PYTHON}"}
    command = [
        str(PYTHON),
        str(LAST30DAYS),
        query,
        "--emit",
        "json",
        "--search",
        source,
        "--quick",
        "--days",
        str(days),
    ]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Probe timed out after 90s."}
    payload = parse_json_payload(result.stdout)
    if not payload:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()[-700:]}
    items = list((payload.get("items_by_source") or {}).get(source, []))
    return {
        "ok": result.returncode == 0,
        "item_count": len(items),
        "warnings": payload.get("warnings", []),
        "source_errors": payload.get("errors_by_source", {}),
        "shape": classify_items(items),
        "top_results": [summarize_item(item) for item in items[:3]],
    }


def parse_json_payload(output: str) -> dict[str, Any]:
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        return json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return {}


def classify_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    counters = {
        "questions": 0,
        "first_person": 0,
        "workflow_language": 0,
        "launch_or_promo": 0,
        "tutorial_or_demo": 0,
        "problem_language": 0,
    }
    containers: dict[str, int] = {}
    for item in items:
        text = f"{item.get('title', '')} {item.get('snippet', '')} {item.get('body', '')}".lower()
        if "?" in str(item.get("title", "")) or any(phrase in text for phrase in ("how are", "how do", "what are")):
            counters["questions"] += 1
        if re.search(r"\b(i|we|my|our|i'm|we're|using|used)\b", text):
            counters["first_person"] += 1
        if any(word in text for word in ("workflow", "process", "system", "pipeline")):
            counters["workflow_language"] += 1
        if any(word in text for word in ("launch", "announcing", "new tool", "product hunt")):
            counters["launch_or_promo"] += 1
        if any(word in text for word in ("tutorial", "walkthrough", "demo", "guide")):
            counters["tutorial_or_demo"] += 1
        if any(word in text for word in ("problem", "issue", "failed", "terrible", "cannot", "slop")):
            counters["problem_language"] += 1
        container = str(item.get("container") or item.get("author") or item.get("source") or "").strip()
        if container:
            containers[container] = containers.get(container, 0) + 1
    return {
        **counters,
        "containers": sorted(containers, key=containers.get, reverse=True)[:5],
    }


def summarize_item(item: dict[str, Any]) -> dict[str, Any]:
    engagement = item.get("engagement") if isinstance(item.get("engagement"), dict) else {}
    return {
        "title": str(item.get("title", ""))[:180],
        "container": item.get("container") or item.get("author") or "",
        "published_at": item.get("published_at") or item.get("date") or "",
        "score": engagement.get("score", item.get("score", "")),
        "comments": engagement.get("num_comments", ""),
        "url": item.get("url", ""),
    }


def diagnose_query(query: str) -> str:
    terms = content_terms(query)
    if len(terms) <= 2:
        return "Broad query. Prefer platform-specific variants that add evidence type: workflow, problems, demo, launch, or real use cases."
    if any(term in terms for term in ("best", "tools", "software", "platform")):
        return "Likely to attract vendor/tool content. Social probes should look for user discussion and implementation evidence."
    if any(term in terms for term in ("workflow", "case", "example", "problems", "using")):
        return "Already has useful evidence-type language. Probe across social platforms to choose where the real discussion is."
    return "Moderately specific query. Add a social evidence type before running the full search."


def content_terms(query: str) -> list[str]:
    stop = {"a", "an", "and", "are", "for", "how", "in", "of", "on", "the", "to", "with"}
    return [term for term in re.findall(r"[a-z0-9]+", query.lower()) if term not in stop]


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip())


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Query: {payload['query']}",
        f"Diagnosis: {payload['diagnosis']}",
        "",
        "Suggested social probes:",
    ]
    for index, suggestion in enumerate(payload["suggestions"], start=1):
        lines.append("")
        lines.append(f"{index}. {suggestion['platform']} - {suggestion['label']}")
        lines.append(f"   Query: {suggestion['query']}")
        lines.append(f"   Best for: {suggestion['best_for']}")
        lines.append(f"   Likely: {', '.join(suggestion['likely_results'])}")
        lines.append(f"   Watch: {', '.join(suggestion['warnings'])}")
        probe = suggestion.get("probe")
        if probe:
            if not probe.get("ok"):
                lines.append(f"   Probe failed: {probe.get('error', 'unknown error')}")
            else:
                shape = probe.get("shape", {})
                lines.append(
                    "   Live check: "
                    f"{probe.get('item_count', 0)} results; "
                    f"{shape.get('first_person', 0)} first-person; "
                    f"{shape.get('workflow_language', 0)} workflow/process; "
                    f"{shape.get('problem_language', 0)} problem/limit"
                )
                warnings = probe.get("warnings") or []
                if warnings:
                    lines.append(f"   Live warnings: {', '.join(map(str, warnings))}")
                source_errors = probe.get("source_errors") or {}
                if source_errors:
                    lines.append(f"   Source errors: {source_errors}")
                for item in probe.get("top_results", []):
                    detail = []
                    if item.get("container"):
                        detail.append(str(item["container"]))
                    if item.get("comments") != "":
                        detail.append(f"{item['comments']} comments")
                    if item.get("score") != "":
                        detail.append(f"{item['score']} score")
                    lines.append(f"   - {item['title']} ({', '.join(detail)})")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
