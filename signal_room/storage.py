import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
FIXTURES_DIR = ROOT / "fixtures"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
LAST30DAYS_RUNS_DIR = DATA_DIR / "last30days" / "runs"
QUERY_LAB_DIR = DATA_DIR / "query_lab"


def ensure_dirs() -> None:
    for path in (CONFIG_DIR, FIXTURES_DIR, DATA_DIR, OUTPUT_DIR, LAST30DAYS_RUNS_DIR, QUERY_LAB_DIR):
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    ensure_dirs()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    ensure_dirs()
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")
