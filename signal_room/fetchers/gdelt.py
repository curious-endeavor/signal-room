"""GDELT DOC 2.0 fetcher.

Mirrors the shape of `signal_room/fetchers/last30days.py`: a subprocess
wrapper around an agent-native CLI that returns a normalized row stream
for the signal-room digest pipeline.

The CLI itself (`gdelt-pp-cli`) is built outside this repo; see
`docs/plans/2026-05-13-integrate-gdelt-source.md` for the contract.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..storage import CONFIG_DIR, DATA_DIR, FIXTURES_DIR, ROOT, write_json

try:  # tracer ships on the brief-driven-pipeline branch; tolerate its absence.
    from ..tracer import tracer  # type: ignore
except ImportError:
    class _NoopTracer:
        def record(self, *_args, **_kwargs) -> None:
            return None

    tracer = _NoopTracer()  # type: ignore


BACKEND_CONFIG_PATH = CONFIG_DIR / "gdelt_backend.json"
DISCOVERED_ITEMS_PATH = DATA_DIR / "discovered_items.json"
FIXTURE_PATH = FIXTURES_DIR / "gdelt_sample.json"
GDELT_RUNS_DIR = DATA_DIR / "gdelt" / "runs"

# Resolver fallback chain.
_LOCAL_DEV_BINARY = Path.home() / "printing-press" / "library" / "gdelt" / "gdelt-pp-cli"
_REPO_BUILT_BINARY = ROOT / "bin" / "gdelt-pp-cli"

# Exit codes per the gdelt-pp-cli contract (origin §3).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_NETWORK = 4
EXIT_RATE_LIMITED = 7
EXIT_SERVER = 10

_RATE_LIMIT_NOISE_RE = re.compile(r"^rate limited, waiting \d+s")


class GdeltError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Config / binary resolution
# ---------------------------------------------------------------------------


def _load_backend_config() -> Dict[str, Any]:
    if not BACKEND_CONFIG_PATH.exists():
        return {}
    return json.loads(BACKEND_CONFIG_PATH.read_text(encoding="utf-8"))


def _resolve_binary() -> str:
    """Locate the gdelt-pp-cli binary.

    Resolution order:
      1. $GDELT_PP_CLI env var (if set, must exist).
      2. config/gdelt_backend.json#binary_path (if non-null, must exist).
      3. bin/gdelt-pp-cli (repo-relative).
      4. ~/printing-press/library/gdelt/gdelt-pp-cli (local-dev fallback).
    """
    env_path = (os.environ.get("GDELT_PP_CLI") or "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.exists():
            return str(candidate)
        raise GdeltError(f"$GDELT_PP_CLI is set but does not exist: {env_path}")

    config = _load_backend_config()
    configured = config.get("binary_path")
    if configured:
        candidate = Path(str(configured)).expanduser()
        if candidate.exists():
            return str(candidate)
        raise GdeltError(
            f"config/gdelt_backend.json#binary_path does not exist: {configured}"
        )

    for candidate in (_REPO_BUILT_BINARY, _LOCAL_DEV_BINARY):
        if candidate.exists():
            return str(candidate)

    raise GdeltError(
        "Could not locate gdelt-pp-cli. Tried $GDELT_PP_CLI, "
        "config/gdelt_backend.json#binary_path, bin/gdelt-pp-cli, and "
        f"{_LOCAL_DEV_BINARY}. Run `make build-gdelt` or set $GDELT_PP_CLI."
    )


def _resolve_timeout() -> int:
    raw = _load_backend_config().get("timeout_seconds", 60)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 60
    return max(10, value)


def _resolve_default_timespan() -> str:
    return str(_load_backend_config().get("default_timespan", "1d"))


def _resolve_default_max() -> int:
    raw = _load_backend_config().get("default_max", 75)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 75
    return max(1, value)


def _resolve_pillars_path() -> Optional[str]:
    raw = _load_backend_config().get("pillars_path")
    if not raw:
        return None
    return str(Path(str(raw)).expanduser())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_gdelt(
    pillars: Optional[List[str]] = None,
    timespan: Optional[str] = None,
    max_records: Optional[int] = None,
    mock: bool = False,
    run_root: Optional[Path] = None,
    output_path: Optional[Path] = DISCOVERED_ITEMS_PATH,
    continue_on_error: bool = True,
    pillars_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Pull one or more GDELT pillars and normalize into signal-room rows.

    Args:
      pillars: list of pillar names. None → fetch every pillar from `pillar list`.
      timespan: GDELT timespan (e.g. "1d", "7d"). Defaults to backend config.
      max_records: cap per pillar. Defaults to backend config.
      mock: if True, read `fixtures/gdelt_sample.json` instead of spawning the CLI.
      run_root: where to write per-pillar manifests/stdout/stderr.
      output_path: where to persist the discovered-items payload. Pass `None`
          when combining backends — the caller is responsible for the merge
          (see `signal_room/discovery_store.py`).
      continue_on_error: if True, one failed pillar does not abort the run.
    """
    timespan = timespan or _resolve_default_timespan()
    max_records = max_records or _resolve_default_max()
    run_root = run_root or (GDELT_RUNS_DIR / date.today().isoformat())

    # Graceful degradation: if the gdelt-pp-cli binary isn't on this machine
    # (e.g. Render build skipped it because no Go toolchain), don't crash the
    # whole pipeline — return an empty payload with a clear error note.
    # The caller (pipeline.run_pipeline with fetch_backend="both") will still
    # have last30days items to work with.
    if not mock:
        try:
            _resolve_binary()
        except GdeltError as exc:
            tracer.record("gdelt_unavailable", {"reason": str(exc)})
            payload: Dict[str, Any] = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backend": "gdelt",
                "pillar_count": 0,
                "item_count": 0,
                "items": [],
                "errors": [{"pillar": "*", "error": f"binary unavailable: {exc}"}],
                "runs": [],
            }
            if output_path:
                write_json(output_path, payload)
            return {
                "backend": "gdelt",
                "pillar_count": 0,
                "item_count": 0,
                "error_count": 1,
                "errors": payload["errors"],
                "run_root": str(run_root),
                "runs": [],
                "items": [],
                "skipped": True,
                "skip_reason": str(exc),
            }

    pillar_names = list(pillars) if pillars else _list_pillars(
        mock=mock,
        pillars_path=str(pillars_path) if pillars_path else None,
    )

    tracer.record("gdelt_started", {
        "pillar_count": len(pillar_names),
        "pillars": pillar_names,
        "timespan": timespan,
        "max_records": max_records,
        "mock": mock,
    })

    all_items: List[Dict[str, Any]] = []
    runs: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    # GDELT public API rate-limits at one request per 5s. Honour the configured
    # rate_limit_rps so multi-pillar runs don't 429 themselves. Note that
    # `_list_pillars` (if called) already burned one GDELT call moments before
    # this loop — so we initialize last_call_ts to "now" and sleep before the
    # FIRST pull too. Previous code only paused from idx=1 onward, which made
    # the first pillar pull race against the pillar list call and trip 429.
    rate_rps = float(_load_backend_config().get("rate_limit_rps", 0.2) or 0)
    min_interval = (1.0 / rate_rps) if rate_rps > 0 else 0.0
    last_call_ts = time.time()

    pillars_path_str = str(pillars_path) if pillars_path else None
    for pillar in pillar_names:
        if min_interval > 0:
            wait = min_interval - (time.time() - last_call_ts)
            if wait > 0:
                time.sleep(wait)
        try:
            run = _pull_pillar(
                pillar=pillar,
                timespan=timespan,
                max_records=max_records,
                mock=mock,
                run_root=run_root,
                pillars_path=pillars_path_str,
            )
            last_call_ts = time.time()
        except GdeltError as exc:
            errors.append({"pillar": pillar, "error": str(exc)})
            tracer.record("gdelt_pillar_error", {"pillar": pillar, "error": str(exc)})
            if not continue_on_error:
                raise
            continue
        runs.append(run)
        all_items.extend(run["items"])
        tracer.record("gdelt_pillar_items_returned", {
            "pillar": run["pillar"],
            "item_count": len(run["items"]),
            "sample_items": [
                {
                    "title": (it.get("title") or "")[:160],
                    "source": it.get("source") or "gdelt",
                    "source_url": it.get("source_url") or it.get("url") or "",
                    "date": it.get("date") or "",
                }
                for it in (run["items"] or [])[:25]
            ],
        })

    payload: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": "gdelt",
        "pillar_count": len(runs),
        "item_count": len(all_items),
        "items": all_items,
        "errors": errors,
        "runs": [
            {
                "pillar": run["pillar"],
                "query": run.get("query"),
                "item_count": len(run["items"]),
                "run_dir": run["run_dir"],
                "manifest_path": run["manifest_path"],
                "timespan": timespan,
                "max_records": max_records,
            }
            for run in runs
        ],
    }
    if output_path:
        write_json(output_path, payload)
        payload["discovered_items_path"] = str(output_path)

    tracer.record("gdelt_complete", {
        "pillar_count": len(runs),
        "item_count": len(all_items),
        "error_count": len(errors),
    })
    return payload


# ---------------------------------------------------------------------------
# Per-pillar plumbing
# ---------------------------------------------------------------------------


def _list_pillars(mock: bool, pillars_path: Optional[str] = None) -> List[str]:
    if mock:
        # In mock mode the fixture stands in for a single pillar pull.
        return ["chatbot-failures"]
    binary = _resolve_binary()
    # Critical: pass the per-brand pillars file to `pillar list` too. Without
    # it the CLI reads ~/.config/gdelt-pp-cli/pillars.json (a global file that
    # holds whatever brand was last used) and returns the wrong brand's pillar
    # names — which then trip "no pillar named X" errors when _pull_pillar
    # tries those names against the correct brand file.
    env = dict(os.environ)
    effective_pillars_path = pillars_path or _resolve_pillars_path()
    if effective_pillars_path:
        env["GDELT_PILLARS_PATH"] = str(effective_pillars_path)
    result = subprocess.run(
        [binary, "pillar", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=_resolve_timeout(),
    )
    if result.returncode != EXIT_OK:
        stderr = _filter_rate_limit_noise(result.stderr)
        raise GdeltError(
            f"`gdelt-pp-cli pillar list` failed (exit {result.returncode}): {stderr.strip() or 'no stderr'}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GdeltError(f"Could not parse `pillar list` JSON: {exc}") from exc
    pillars = payload.get("pillars") or []
    return [str(p.get("name", "")).strip() for p in pillars if p.get("name")]


def _pull_pillar(
    pillar: str,
    timespan: str,
    max_records: int,
    mock: bool,
    run_root: Path,
    pillars_path: Optional[str] = None,
) -> Dict[str, Any]:
    run_dir = run_root / pillar
    run_dir.mkdir(parents=True, exist_ok=True)

    tracer.record("gdelt_pillar_fired", {
        "pillar": pillar,
        "timespan": timespan,
        "max_records": max_records,
    })

    if mock:
        payload = _load_fixture()
        manifest = {
            "pillar": pillar,
            "timespan": timespan,
            "max_records": max_records,
            "mock": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        articles = (payload.get("results") or {}).get("articles") or []
        query = (payload.get("results") or {}).get("query")
        items = [_normalize_article(article, pillar) for article in articles]
        items = [item for item in items if item is not None]
        manifest["item_count"] = len(items)
        write_json(run_dir / "manifest.json", manifest)
        write_json(run_dir / "report.json", payload)
        return {
            "pillar": pillar,
            "query": query,
            "run_dir": str(run_dir),
            "manifest_path": str(run_dir / "manifest.json"),
            "items": items,
        }

    binary = _resolve_binary()
    command = [
        binary,
        "pillar",
        "pull",
        pillar,
        "--timespan",
        timespan,
        "--max",
        str(max_records),
        "--json",
    ]
    env = dict(os.environ)
    # Per-run pillars_path (passed down from fetch_gdelt) wins over both env
    # var and global backend-config default. This is how brand-specific GDELT
    # pillars override the home-dir global file.
    effective_pillars_path = pillars_path or _resolve_pillars_path()
    if effective_pillars_path:
        env["GDELT_PILLARS_PATH"] = str(effective_pillars_path)

    timeout_seconds = _resolve_timeout()
    manifest: Dict[str, Any] = {
        "pillar": pillar,
        "timespan": timespan,
        "max_records": max_records,
        "mock": False,
        "command": command,
        "timeout_seconds": timeout_seconds,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

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
        manifest["error"] = f"gdelt-pp-cli timed out after {timeout_seconds} seconds"
        write_json(run_dir / "manifest.json", manifest)
        raise GdeltError(
            f"gdelt-pp-cli timed out for pillar {pillar} after {timeout_seconds} seconds"
        ) from exc

    real_stderr = _filter_rate_limit_noise(result.stderr)
    (run_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    manifest["exit_code"] = result.returncode
    manifest["stdout_path"] = str(run_dir / "stdout.txt")
    manifest["stderr_path"] = str(run_dir / "stderr.txt")

    if result.returncode != EXIT_OK:
        manifest["error"] = real_stderr.strip() or result.stdout.strip()
        write_json(run_dir / "manifest.json", manifest)
        message = (
            f"gdelt-pp-cli pillar pull {pillar} failed "
            f"(exit {result.returncode}): {manifest['error'] or 'no output'}"
        )
        raise GdeltError(message)

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        manifest["error"] = f"Could not parse gdelt-pp-cli JSON output: {exc}"
        write_json(run_dir / "manifest.json", manifest)
        raise GdeltError(
            f"gdelt-pp-cli returned non-JSON output for pillar {pillar}: {exc}"
        ) from exc

    results = report.get("results") or {}
    articles = results.get("articles") or []
    query = results.get("query")
    items = [_normalize_article(article, pillar) for article in articles]
    items = [item for item in items if item is not None]

    if not items:
        # Empty result is a valid outcome (origin §6 gotchas 1, 2) but
        # worth surfacing so silently-empty short-acronym OR queries
        # are noticed.
        tracer.record("gdelt_pillar_empty", {
            "pillar": pillar,
            "query": query,
            "timespan": timespan,
        })

    write_json(run_dir / "report.json", report)
    manifest["item_count"] = len(items)
    manifest["query"] = query
    write_json(run_dir / "manifest.json", manifest)

    return {
        "pillar": pillar,
        "query": query,
        "run_dir": str(run_dir),
        "manifest_path": str(run_dir / "manifest.json"),
        "items": items,
    }


def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise GdeltError(f"Missing GDELT fixture: {FIXTURE_PATH}")
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Normalization helpers (pure functions — easy to test)
# ---------------------------------------------------------------------------


def _filter_rate_limit_noise(stderr: str) -> str:
    """Strip `rate limited, waiting Ns ...` lines.

    The CLI's adaptive limiter prints these informational lines on stderr
    while pacing; they are not errors. Returns the remaining stderr text.
    """
    if not stderr:
        return ""
    kept = [
        line for line in stderr.splitlines()
        if line.strip() and not _RATE_LIMIT_NOISE_RE.match(line.strip())
    ]
    return "\n".join(kept)


def _normalize_article(article: Dict[str, Any], pillar: str) -> Optional[Dict[str, Any]]:
    if not isinstance(article, dict):
        return None
    url = str(article.get("url") or "").strip()
    if not url:
        return None
    title = str(article.get("title") or "").strip() or url
    return {
        "id": _stable_id(url),
        "title": title[:280],
        "source": str(article.get("domain") or "").strip(),
        "source_url": url,
        "date": _parse_seendate(str(article.get("seendate") or "").strip()),
        "summary": "",
        "content": "",
        "engagement": {},
        "metadata": {
            "language": article.get("language"),
            "sourcecountry": article.get("sourcecountry"),
            "domain": article.get("domain"),
            "copies": article.get("copies", 1),
            "also_in": list(article.get("also_in") or []),
            "socialimage": article.get("socialimage"),
            "url_mobile": article.get("url_mobile"),
        },
        "discovery_method": "gdelt",
        "candidate_source": True,
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "tags": [f"pillar:{pillar}", "platform:news"],
        "meta": {"source": "gdelt"},
    }


def _parse_seendate(raw: str) -> str:
    """Convert `YYYYMMDDTHHMMSSZ` (GDELT's `seendate`) to ISO `YYYY-MM-DD`.

    Falls back to today on malformed input — matches the lenient behavior
    of `last30days._entry_date`.
    """
    if not raw:
        return date.today().isoformat()
    try:
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return parsed.date().isoformat()
    except ValueError:
        return date.today().isoformat()


def _stable_id(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"gdelt-{digest}"
