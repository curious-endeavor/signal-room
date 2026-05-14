"""Pipeline trace recorder.

When enabled, captures the I/O of each pipeline stage so a renderer can show
exactly how information flowed through a run. When disabled, every method is
a cheap no-op so production runs aren't burdened.

Usage:
    from .tracer import tracer

    tracer.enable("ce", run_dir=Path("data/traces"))
    tracer.record("brief_loaded", {"path": "...", "pillars": 5, "queries": 12})
    ...
    tracer.flush()   # writes <brand>-<timestamp>.jsonl
    tracer.flush_html()  # writes accompanying .html
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class _Tracer:
    def __init__(self) -> None:
        self._enabled: bool = False
        self._brand: str = "unknown"
        self._run_dir: Optional[Path] = None
        self._records: List[Dict[str, Any]] = []
        self._t0: float = 0.0
        self._started_at_iso: str = ""
        # Hooks fired on every record() call. Used by the web worker to bridge
        # tracer events into the run_events table so the terminal-style live
        # view on /{brand}/runs/{run_id} can stream pipeline progress. Stored
        # as a list so callers can add multiple listeners; cleared on disable.
        self._listeners: List[Any] = []

    def add_listener(self, callback) -> None:
        """Register a callable invoked synchronously on each record().
        callback(entry: dict) — `entry` matches the JSONL row shape.
        Exceptions raised by listeners are swallowed so a buggy listener
        can't take down the pipeline."""
        self._listeners.append(callback)

    def clear_listeners(self) -> None:
        self._listeners = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self, brand: str, run_dir: Path) -> None:
        self._enabled = True
        self._brand = brand or "unknown"
        self._run_dir = Path(run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._records = []
        self._t0 = time.time()
        self._started_at_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self.record("run_started", {"brand": self._brand, "started_at": self._started_at_iso})

    def disable(self) -> None:
        self._enabled = False
        self._records = []
        self._listeners = []

    def record(self, stage: str, payload: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        entry = {
            "stage": stage,
            "t_ms": int((time.time() - self._t0) * 1000),
            "payload": payload,
        }
        self._records.append(entry)
        for cb in self._listeners:
            try:
                cb(entry)
            except Exception:
                # A listener must never crash the pipeline. Swallow and move on.
                pass

    @property
    def records(self) -> List[Dict[str, Any]]:
        return list(self._records)

    @property
    def brand(self) -> str:
        return self._brand

    @property
    def started_at(self) -> str:
        return self._started_at_iso

    def _slug(self) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return f"{self._brand}-{ts}"

    def flush(self) -> Optional[Path]:
        """Write trace.jsonl to disk. Returns the path, or None if disabled."""
        if not self._enabled or not self._run_dir:
            return None
        self.record("run_finished", {"record_count": len(self._records)})
        path = self._run_dir / f"{self._slug()}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for entry in self._records:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    def flush_html(self, jsonl_path: Optional[Path] = None) -> Optional[Path]:
        """Write the rendered HTML alongside the jsonl. Returns the path."""
        if not self._enabled or not self._run_dir:
            return None
        from .render_trace import render_trace_html

        if jsonl_path is None:
            # Caller wants HTML but didn't pass a jsonl; persist first.
            jsonl_path = self.flush()
        html_path = jsonl_path.with_suffix(".html")
        render_trace_html(jsonl_path, html_path, brand=self._brand, started_at=self._started_at_iso)
        return html_path


# Per-thread tracer. Each ThreadPoolExecutor worker (parallel brand pipelines)
# gets its own _Tracer instance — `enable()`, `record()`, `flush()` calls from
# thread A never clobber thread B's records or listeners. The original single-
# threaded API (`from .tracer import tracer; tracer.record(...)`) keeps working
# because every attribute access resolves to the calling thread's instance.
_local = threading.local()


def _get_tracer() -> "_Tracer":
    inst = getattr(_local, "tracer", None)
    if inst is None:
        inst = _Tracer()
        _local.tracer = inst
    return inst


class _TracerProxy:
    """Thin attribute-forwarding shim so `tracer.xxx` always resolves to the
    current thread's `_Tracer` instance. Keeps the import API stable."""

    def __getattr__(self, name):
        return getattr(_get_tracer(), name)


tracer = _TracerProxy()
