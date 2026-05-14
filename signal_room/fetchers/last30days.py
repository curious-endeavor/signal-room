import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from ..storage import CONFIG_DIR, LAST30DAYS_RUNS_DIR, ROOT, ensure_dirs, write_json
from ..tracer import tracer, _get_tracer

# Vendor stderr lines worth surfacing as live events. Skip ANSI spinners,
# progress bars, and other noise that just clutters the live view.
_VENDOR_LOG_INTERESTING = re.compile(
    r"^\[(YouTube|GitHub|Reddit|X|Instagram|HackerNews|Hacker News|Grounding|Brave|Planner|Last30Days)\]"
    r"|Found \d|Searching|fetched|error|Error|WARNING|Failed|timeout",
    re.IGNORECASE,
)
_VENDOR_LOG_ANSI = re.compile(r"\x1b\[[0-9;]*m|⏳")


DISCOVERY_QUERIES_PATH = CONFIG_DIR / "discovery_queries.json"
BACKEND_CONFIG_PATH = CONFIG_DIR / "last30days_backend.json"
DISCOVERED_ITEMS_PATH = Path(__file__).resolve().parents[2] / "data" / "discovered_items.json"
META_KEYS = {
    "topic",
    "range",
    "generated_at",
    "mode",
    "openai_model_used",
    "xai_model_used",
    "artifacts",
    "clusters",
    "errors_by_source",
    "provider_runtime",
    "query_plan",
    "range_from",
    "range_to",
    "ranked_candidates",
}
LAST30DAYS_CONFIG_ENV_KEYS = {
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "SCRAPECREATORS_API_KEY",
    "APIFY_API_TOKEN",
    "AUTH_TOKEN",
    "CT0",
    "BSKY_HANDLE",
    "BSKY_APP_PASSWORD",
    "TRUTHSOCIAL_TOKEN",
    "BRAVE_API_KEY",
    "EXA_API_KEY",
    "SERPER_API_KEY",
    "OPENROUTER_API_KEY",
    "PARALLEL_API_KEY",
    "XQUIK_API_KEY",
    "FROM_BROWSER",
    "INCLUDE_SOURCES",
    "LAST30DAYS_REASONING_PROVIDER",
    "LAST30DAYS_PLANNER_MODEL",
    "LAST30DAYS_RERANK_MODEL",
    "LAST30DAYS_X_MODEL",
    "LAST30DAYS_X_BACKEND",
    "OPENAI_MODEL_PIN",
    "XAI_MODEL_PIN",
}


class Last30DaysError(RuntimeError):
    pass


def fetch_last30days(
    query_limit: Optional[int] = None,
    mock: bool = False,
    search_sources: Optional[List[str]] = None,
    queries: Optional[List[Dict[str, Any]]] = None,
    run_root: Optional[Path] = None,
    output_path: Optional[Path] = DISCOVERED_ITEMS_PATH,
    parallelism: int = 1,
    continue_on_error: bool = False,
    lookback_days: Optional[int] = None,
) -> Dict[str, Any]:
    ensure_dirs()
    queries = list(queries or _load_queries())
    if lookback_days:
        queries = [dict(query, lookback_days=lookback_days) for query in queries]
    if query_limit:
        queries = queries[:query_limit]
    run_root = run_root or (LAST30DAYS_RUNS_DIR / date.today().isoformat())
    parallelism = max(1, int(parallelism))

    tracer.record("last30days_started", {
        "query_count": len(queries),
        "mock": mock,
        "lookback_days": lookback_days,
        "parallelism": parallelism,
        "queries": [
            {"id": q.get("id"), "topic": q.get("topic"), "priority": q.get("priority"),
             "search_text": q.get("search_text"), "why": q.get("why")}
            for q in queries
        ],
    })

    all_items: List[Dict[str, Any]] = []
    runs: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    if parallelism == 1:
        for query in queries:
            tracer.record("query_fired", {
                "query_id": str(query.get("id", "")),
                "topic": query.get("topic"),
                "search_text": query.get("search_text") or query.get("topic"),
                "priority": query.get("priority"),
            })
            try:
                run = _run_query(query, mock=mock, search_sources=search_sources, run_root=run_root)
            except Last30DaysError as exc:
                if not continue_on_error:
                    raise
                errors.append({"query_id": str(query.get("id", "")), "error": str(exc)})
                tracer.record("query_error", {"query_id": str(query.get("id", "")), "error": str(exc)})
                continue
            runs.append(run)
            all_items.extend(run["items"])
            tracer.record("items_returned", {
                "query_id": run["query_id"],
                "topic": run["topic"],
                "item_count": len(run["items"]),
                "sample_items": [
                    {
                        "title": (it.get("title") or "")[:400],
                        "source": it.get("source", ""),
                        "source_url": it.get("source_url", ""),
                        "date": it.get("date", ""),
                    }
                    for it in run["items"][:25]
                ],
            })
    else:
        for query in queries:
            tracer.record("query_fired", {
                "query_id": str(query.get("id", "")),
                "topic": query.get("topic"),
                "search_text": query.get("search_text") or query.get("topic"),
                "priority": query.get("priority"),
            })
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            future_map = {
                executor.submit(_run_query, query, mock, search_sources, run_root): query
                for query in queries
            }
            for future in as_completed(future_map):
                query = future_map[future]
                try:
                    run = future.result()
                except Last30DaysError as exc:
                    if not continue_on_error:
                        raise
                    errors.append({"query_id": str(query.get("id", "")), "error": str(exc)})
                    tracer.record("query_error", {"query_id": str(query.get("id", "")), "error": str(exc)})
                    continue
                runs.append(run)
                all_items.extend(run["items"])
                tracer.record("items_returned", {
                    "query_id": run["query_id"],
                    "topic": run["topic"],
                    "item_count": len(run["items"]),
                    "sample_items": [
                        {
                            "title": (it.get("title") or "")[:400],
                            "source": it.get("source", ""),
                            "source_url": it.get("source_url", ""),
                            "date": it.get("date", ""),
                        }
                        for it in run["items"][:25]
                    ],
                })

    runs.sort(key=lambda run: (int(run.get("priority", 999)), run["query_id"]))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": "last30days",
        "query_count": len(runs),
        "item_count": len(all_items),
        "items": all_items,
        "errors": errors,
        "runs": [
            {
                "query_id": run["query_id"],
                "topic": run["topic"],
                "search_text": run["search_text"],
                "run_dir": run["run_dir"],
                "item_count": len(run["items"]),
                "report_path": run["report_path"],
                "manifest_path": run["manifest_path"],
                "search_sources": run["search_sources"],
                "priority": run.get("priority"),
            }
            for run in runs
        ],
    }
    if output_path:
        write_json(output_path, payload)
    summary = {
        "backend": "last30days",
        "query_count": len(runs),
        "item_count": len(all_items),
        "error_count": len(errors),
        "errors": errors,
        "run_root": str(run_root),
        "runs": payload["runs"],
        "items": all_items,
    }
    if output_path:
        summary["discovered_items_path"] = str(output_path)
    tracer.record("last30days_complete", {
        "query_count": len(runs),
        "total_item_count": len(all_items),
        "error_count": len(errors),
    })
    return summary


PLANS_DIR = CONFIG_DIR / "plans"


def _load_queries() -> List[Dict[str, Any]]:
    if not DISCOVERY_QUERIES_PATH.exists():
        raise Last30DaysError(f"Missing discovery query config: {DISCOVERY_QUERIES_PATH}")
    payload = json.loads(DISCOVERY_QUERIES_PATH.read_text(encoding="utf-8"))
    queries = list(payload.get("queries", []))
    queries.sort(key=lambda item: (int(item.get("priority", 999)), item.get("id", "")))
    # Attach Signal-Room-generated plan paths when available. The fetcher passes
    # the file as `--plan <path>` to /last30days, which skips its (frequently
    # broken) internal grok planner. See signal_room/planner.py.
    if PLANS_DIR.exists():
        for q in queries:
            qid = str(q.get("id", ""))
            if not qid:
                continue
            candidate = PLANS_DIR / f"{qid}.json"
            if candidate.exists():
                q["plan_path"] = str(candidate)
    return queries


def _run_query(
    query: Dict[str, Any],
    mock: bool,
    search_sources: Optional[List[str]],
    run_root: Path,
) -> Dict[str, Any]:
    topic = str(query["topic"])
    search_text = str(query.get("search_text") or topic)
    query_id = str(query["id"])
    run_dir = run_root / query_id
    run_dir.mkdir(parents=True, exist_ok=True)

    selected_sources = search_sources or _query_search_sources(query) or _default_search_sources()
    command = _build_command(
        search_text,
        mock=mock,
        search_sources=selected_sources,
        lookback_days=_query_lookback_days(query),
        plan_path=query.get("plan_path"),
    )
    env = _subprocess_env()
    timeout_seconds = _timeout_seconds()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        manifest = {
            "query_id": query_id,
            "topic": topic,
            "search_text": search_text,
            "why": query.get("why", ""),
            "priority": query.get("priority"),
            "search_sources": selected_sources,
            "lookback_days": _query_lookback_days(query),
            "mock": mock,
            "command": command,
            "timeout_seconds": timeout_seconds,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "error": f"last30days timed out after {timeout_seconds} seconds",
        }
        write_json(run_dir / "manifest.json", manifest)
        raise Last30DaysError(f"last30days timed out for {query_id} after {timeout_seconds} seconds") from exc
    manifest = {
        "query_id": query_id,
        "topic": topic,
        "search_text": search_text,
        "why": query.get("why", ""),
        "priority": query.get("priority"),
        "search_sources": selected_sources,
        "lookback_days": _query_lookback_days(query),
        "mock": mock,
        "command": command,
        "exit_code": result.returncode,
        "stdout_path": str(run_dir / "stdout.txt"),
        "stderr_path": str(run_dir / "stderr.txt"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")

    if result.returncode != 0:
        manifest["error"] = (result.stderr or result.stdout).strip()
        write_json(run_dir / "manifest.json", manifest)
        raise Last30DaysError(
            f"last30days fetch failed for {query_id} (exit {result.returncode}): {(result.stderr or result.stdout).strip()}"
        )

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        manifest["error"] = f"Could not parse last30days JSON output: {exc}"
        write_json(run_dir / "manifest.json", manifest)
        raise Last30DaysError(f"last30days returned non-JSON output for {query_id}: {exc}")

    report_path = run_dir / "report.json"
    write_json(report_path, report)
    items = _normalize_report(report, query)
    normalized_path = run_dir / "normalized_items.json"
    write_json(normalized_path, {"items": items})
    manifest["report_path"] = str(report_path)
    manifest["normalized_items_path"] = str(normalized_path)
    manifest["normalized_item_count"] = len(items)
    write_json(run_dir / "manifest.json", manifest)
    return {
        "query_id": query_id,
        "topic": topic,
        "search_text": search_text,
        "priority": query.get("priority"),
        "search_sources": selected_sources,
        "lookback_days": _query_lookback_days(query),
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "manifest_path": str(run_dir / "manifest.json"),
        "items": items,
    }


def _build_command(
    topic: str,
    mock: bool,
    search_sources: Optional[List[str]],
    lookback_days: Optional[int],
    plan_path: Optional[str] = None,
) -> List[str]:
    last30days_home = _resolve_last30days_home()
    script_path = last30days_home / "scripts" / "last30days.py"
    if not script_path.exists():
        raise Last30DaysError(f"Missing last30days CLI script: {script_path}")

    python_command = _resolve_python_command()
    if python_command:
        if not mock and not _python_has_module(python_command, "requests"):
            raise Last30DaysError(
                f"{python_command} is available for last30days, but it does not have the `requests` package installed. "
                "Either install the last30days dependencies into that interpreter or run through an aligned Python 3.12+ environment."
            )
        command = [python_command, str(script_path), topic, "--emit", "json"]
    else:
        command = [shutil.which("uv") or "uv", "run", "--project", str(last30days_home), "python", str(script_path), topic, "--emit", "json"]

    if _load_backend_config().get("quick", True):
        command.append("--quick")
    if mock:
        command.append("--mock")
    selected_sources = search_sources or _default_search_sources()
    if selected_sources:
        command.extend(["--search", ",".join(selected_sources)])
    if lookback_days:
        command.extend(["--lookback-days", str(lookback_days)])
    if plan_path:
        command.extend(["--plan", str(plan_path)])
    return command


def _resolve_last30days_home() -> Path:
    env_home = str(os.environ.get("LAST30DAYS_HOME") or os.environ.get("SIGNAL_ROOM_LAST30DAYS_HOME") or "").strip()
    if env_home:
        candidate = Path(env_home).expanduser()
        if candidate.exists():
            return candidate
        raise Last30DaysError(f"Configured LAST30DAYS_HOME does not exist: {env_home}")

    backend_config = _load_backend_config()
    configured_home = str(backend_config.get("last30days_home", "")).strip()
    if configured_home:
        candidate = Path(configured_home).expanduser()
        if candidate.exists():
            return candidate
    candidates = [
        ROOT / "vendor" / "last30days-skill",
        Path.home() / ".claude" / "skills" / "last30days",
        Path.home() / "clawd" / "skills" / "last30days",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise Last30DaysError("Could not find a local last30days installation.")


def _resolve_python_command() -> str:
    env_python = str(os.environ.get("SIGNAL_ROOM_LAST30DAYS_PYTHON") or "").strip()
    if env_python:
        candidate = Path(env_python).expanduser()
        if candidate.exists():
            return str(candidate)
        raise Last30DaysError(f"Configured SIGNAL_ROOM_LAST30DAYS_PYTHON does not exist: {env_python}")
    if os.environ.get("RENDER"):
        return sys.executable

    backend_config = _load_backend_config()
    configured_python = str(backend_config.get("python_command", "")).strip()
    if configured_python:
        candidate = Path(configured_python).expanduser()
        if candidate.exists():
            return str(candidate)
        if os.environ.get("SIGNAL_ROOM_STRICT_BACKEND_CONFIG", "").lower() in {"1", "true", "yes"}:
            raise Last30DaysError(f"Configured last30days python does not exist: {configured_python}")

    return shutil.which("python3.13") or shutil.which("python3.12") or ""


def _load_backend_config() -> Dict[str, Any]:
    if not BACKEND_CONFIG_PATH.exists():
        return {}
    return json.loads(BACKEND_CONFIG_PATH.read_text(encoding="utf-8"))


def _default_search_sources() -> List[str]:
    backend_config = _load_backend_config()
    raw_sources = backend_config.get("default_search_sources", [])
    if not isinstance(raw_sources, list):
        return []
    return [str(source).strip() for source in raw_sources if str(source).strip()]


def _query_search_sources(query: Dict[str, Any]) -> List[str]:
    raw_sources = query.get("search_sources", [])
    if not isinstance(raw_sources, list):
        return []
    return [str(source).strip() for source in raw_sources if str(source).strip()]


def _query_lookback_days(query: Dict[str, Any]) -> int:
    raw_value = query.get("lookback_days", _default_lookback_days())
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = _default_lookback_days()
    return max(1, value)


def _default_lookback_days() -> int:
    backend_config = _load_backend_config()
    raw_value = backend_config.get("lookback_days", 30)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 30
    return max(1, value)


def _timeout_seconds() -> int:
    backend_config = _load_backend_config()
    raw_timeout = backend_config.get("timeout_seconds", 120)
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        timeout = 120
    return max(10, timeout)


def _python_has_module(python_command: str, module_name: str) -> bool:
    result = subprocess.run(
        [python_command, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _subprocess_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(ROOT / ".uv-cache")
    if os.environ.get("RENDER") or os.environ.get("SIGNAL_ROOM_KEEP_LAST30DAYS_ENV"):
        return env
    # last30days resolves process env before ~/.config/last30days/.env.
    # Clear config-bearing vars here so the user's last30days config file
    # stays authoritative even when the parent shell has stale secrets.
    for key in LAST30DAYS_CONFIG_ENV_KEYS:
        env.pop(key, None)
    return env


def _normalize_report(report: Dict[str, Any], query: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    query_id = str(query["id"])
    items_by_source = report.get("items_by_source")
    if isinstance(items_by_source, dict):
        for platform, entries in items_by_source.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = _normalize_entry(platform, entry, query_id, report.get("topic", query["topic"]))
                if item:
                    items.append(item)
        return items

    for platform, entries in report.items():
        if platform in META_KEYS or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = _normalize_entry(platform, entry, query_id, report.get("topic", query["topic"]))
            if item:
                items.append(item)
    return items


def _normalize_entry(platform: str, entry: Dict[str, Any], query_id: str, topic: str) -> Optional[Dict[str, Any]]:
    source_url = str(entry.get("url", "")).strip()
    if not source_url:
        return None
    raw_title = str(entry.get("title") or entry.get("text") or entry.get("body") or "").strip()
    title = raw_title or source_url
    summary = _first_non_empty(
        entry.get("why_relevant"),
        entry.get("snippet"),
        entry.get("commentary"),
        entry.get("transcript_snippet"),
        entry.get("selftext"),
        entry.get("body"),
        entry.get("text"),
        "",
    )
    content = "\n\n".join(
        value
        for value in [
            _stringify(entry.get("text")),
            _stringify(entry.get("body")),
            _stringify(entry.get("selftext")),
            _stringify(entry.get("transcript_snippet")),
            _stringify(entry.get("snippet")),
            _stringify(entry.get("comment_insights")),
            _stringify(entry.get("metadata")),
            _stringify(entry.get("top_comments")),
        ]
        if value
    )
    entry_id = str(entry.get("id") or entry.get("item_id") or entry.get("reddit_id") or source_url)
    return {
        "id": _stable_id(platform, entry_id),
        "title": title[:280],
        "source": _source_name(platform, entry),
        "source_url": source_url,
        "date": _entry_date(entry),
        "summary": summary[:500],
        "content": content[:8000],
        "engagement": dict(entry.get("engagement") or {}),
        "metadata": dict(entry.get("metadata") or {}),
        "engagement_score": _optional_float(entry.get("engagement_score")),
        "local_rank_score": _optional_float(entry.get("local_rank_score")),
        "local_relevance": _optional_float(entry.get("local_relevance")),
        "freshness": _optional_float(entry.get("freshness")),
        "discovery_method": "last30days",
        "candidate_source": True,
        "tags": [
            f"platform:{platform}",
            f"query:{query_id}",
            f"topic:{_slug(topic)}",
        ],
    }


def _source_name(platform: str, entry: Dict[str, Any]) -> str:
    if platform == "reddit":
        subreddit = str(entry.get("subreddit", "")).strip()
        return f"Reddit / r/{subreddit}" if subreddit else "Reddit"
    if platform == "x":
        handle = str(entry.get("author_handle") or entry.get("author") or "").strip()
        return f"X / @{handle}" if handle else "X"
    if platform == "youtube":
        channel = str(entry.get("channel_name", "")).strip()
        return f"YouTube / {channel}" if channel else "YouTube"
    if platform == "github":
        repo = str(entry.get("repo", "")).strip()
        return f"GitHub / {repo}" if repo else "GitHub"
    parsed = urlparse(str(entry.get("url", "")).strip())
    if parsed.netloc:
        return parsed.netloc
    return platform.replace("_", " ").title()


def _entry_date(entry: Dict[str, Any]) -> str:
    raw_value = _first_non_empty(
        entry.get("date"),
        entry.get("published_at"),
        entry.get("published"),
        entry.get("created_at"),
        entry.get("created"),
        entry.get("timestamp"),
        entry.get("time"),
    )
    if not raw_value:
        return date.today().isoformat()
    text = str(raw_value).strip()
    if not text:
        return date.today().isoformat()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return date.today().isoformat()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else date.today().isoformat()


def _stable_id(platform: str, entry_id: str) -> str:
    token = f"{platform}:{entry_id}"
    return "".join(ch if ch.isalnum() else "-" for ch in token.lower())[:64].strip("-")


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _stringify(value).strip()
        if text:
            return text
    return ""


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_stringify(item).strip() for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
