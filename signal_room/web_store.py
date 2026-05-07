from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .storage import DATA_DIR, ensure_dirs


DEFAULT_SQLITE_PATH = DATA_DIR / "signal_room_web.sqlite3"


class SignalRoomStore:
    def __init__(self, database_url: str = "") -> None:
        self.database_url = database_url or os.environ.get("DATABASE_URL", "")
        self.sqlite_path = Path(os.environ.get("SIGNAL_ROOM_SQLITE_PATH", DEFAULT_SQLITE_PATH))
        self.is_postgres = self.database_url.startswith(("postgres://", "postgresql://"))

    def initialize(self) -> None:
        if self.is_postgres:
            self._initialize_postgres()
            return
        self._initialize_sqlite()

    def create_run(self, query: str, sources: list[str], lookback_days: int = 30) -> str:
        run_id = uuid.uuid4().hex[:12]
        now = _now()
        self.execute(
            """
            insert into runs (id, query, status, lookback_days, sources_json, created_at, updated_at, error, item_count)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, query, "queued", lookback_days, json.dumps(sources), now, now, "", 0),
        )
        return run_id

    def find_active_run(self, query: str, sources: list[str], lookback_days: int) -> dict[str, Any]:
        rows = self.fetchall(
            """
            select * from runs
            where query = ?
              and lookback_days = ?
              and sources_json = ?
              and status in ('queued', 'running')
            order by created_at desc
            limit 1
            """,
            (query, lookback_days, json.dumps(sources)),
        )
        return _decode_run(rows[0]) if rows else {}

    def get_run(self, run_id: str) -> dict[str, Any]:
        rows = self.fetchall("select * from runs where id = ?", (run_id,))
        return _decode_run(rows[0]) if rows else {}

    def list_runs(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.fetchall("select * from runs order by created_at desc limit ?", (limit,))
        return [_decode_run(row) for row in rows]

    def next_queued_run(self) -> dict[str, Any]:
        rows = self.fetchall("select * from runs where status = ? order by created_at asc limit 1", ("queued",))
        return _decode_run(rows[0]) if rows else {}

    def mark_run_status(self, run_id: str, status: str, error: str = "", item_count: int = 0) -> None:
        self.execute(
            "update runs set status = ?, error = ?, item_count = ?, updated_at = ? where id = ?",
            (status, error, item_count, _now(), run_id),
        )

    def record_run_event(
        self,
        run_id: str,
        message: str,
        kind: str = "info",
        source: str = "",
        item_count: int = 0,
    ) -> None:
        self.execute(
            """
            insert into run_events (run_id, kind, source, message, item_count, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (run_id, kind, source, message, item_count, _now()),
        )

    def list_run_events(self, run_id: str, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.fetchall(
            """
            select * from run_events
            where run_id = ?
            order by id desc
            limit ?
            """,
            (run_id, limit),
        )
        return [_decode_event(row) for row in reversed(rows)]

    def replace_run_items(self, run_id: str, items: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            self._execute_conn(conn, "delete from items where run_id = ?", (run_id,))
            for rank, item in enumerate(items, start=1):
                self._execute_conn(
                    conn,
                    """
                    insert into items (
                      run_id, item_id, rank, title, source, source_url, date, score, summary,
                      suggested_ce_angle, pillar, follow_up_query, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(item.get("id", "")),
                        rank,
                        str(item.get("title", "")),
                        str(item.get("source", "")),
                        str(item.get("source_url", "")),
                        str(item.get("date", "")),
                        float(item.get("score", 0.0) or 0.0),
                        str(item.get("summary", "")),
                        str(item.get("suggested_ce_angle", "")),
                        _primary_pillar(item),
                        str(item.get("follow_up_search_query", "")),
                        json.dumps(item, sort_keys=True),
                    ),
                )

    def get_run_items(self, run_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            rows = self.fetchall("select * from items where run_id = ? order by rank asc", (run_id,))
        else:
            rows = self.fetchall("select * from items where run_id = ? order by rank asc limit ?", (run_id, limit))
        return [_decode_item(row) for row in rows]

    def latest_items(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.fetchall(
            """
            select * from items
            where run_id = (select id from runs where status = 'complete' order by updated_at desc limit 1)
            order by rank asc
            limit ?
            """,
            (limit,),
        )
        return [_decode_item(row) for row in rows]

    def record_feedback(self, run_id: str, item_id: str, action: str, note: str = "") -> None:
        self.execute(
            """
            insert into feedback_events (run_id, item_id, action, note, created_at)
            values (?, ?, ?, ?, ?)
            """,
            (run_id, item_id, action, note, _now()),
        )

    def feedback_counts(self) -> dict[str, int]:
        rows = self.fetchall("select action, count(*) as count from feedback_events group by action", ())
        return {str(row["action"]): int(row["count"]) for row in rows}

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.transaction() as conn:
            self._execute_conn(conn, sql, params)

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        translated = _translate_sql(sql, self.is_postgres)
        with self.connect() as conn:
            cursor = conn.execute(translated, params)
            rows = cursor.fetchall()
            if self.is_postgres:
                columns = [column.name for column in cursor.description or []]
                return [dict(zip(columns, row)) for row in rows]
            return [dict(row) for row in rows]

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.connect() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.is_postgres:
            import psycopg

            with psycopg.connect(self.database_url) as conn:
                yield conn
            return
        ensure_dirs()
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    def _execute_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> None:
        conn.execute(_translate_sql(sql, self.is_postgres), params)

    def _initialize_sqlite(self) -> None:
        schema = _schema_sql(id_type="integer primary key autoincrement")
        with self.transaction() as conn:
            for statement in schema:
                conn.execute(statement)

    def _initialize_postgres(self) -> None:
        schema = _schema_sql(id_type="bigserial primary key")
        with self.transaction() as conn:
            for statement in schema:
                conn.execute(statement)


def _schema_sql(id_type: str) -> list[str]:
    return [
        """
        create table if not exists runs (
          id text primary key,
          query text not null,
          status text not null,
          lookback_days integer not null,
          sources_json text not null,
          created_at text not null,
          updated_at text not null,
          error text not null default '',
          item_count integer not null default 0
        )
        """,
        """
        create table if not exists items (
          run_id text not null,
          item_id text not null,
          rank integer not null,
          title text not null,
          source text not null,
          source_url text not null,
          date text not null,
          score real not null,
          summary text not null,
          suggested_ce_angle text not null,
          pillar text not null,
          follow_up_query text not null,
          payload_json text not null,
          primary key (run_id, rank)
        )
        """,
        f"""
        create table if not exists feedback_events (
          id {id_type},
          run_id text not null,
          item_id text not null,
          action text not null,
          note text not null default '',
          created_at text not null
        )
        """,
        f"""
        create table if not exists run_events (
          id {id_type},
          run_id text not null,
          kind text not null,
          source text not null default '',
          message text not null,
          item_count integer not null default 0,
          created_at text not null
        )
        """,
    ]


def _translate_sql(sql: str, is_postgres: bool) -> str:
    if not is_postgres:
        return sql
    out = []
    param_index = 0
    for char in sql:
        if char == "?":
            param_index += 1
            out.append(f"%s")
        else:
            out.append(char)
    return "".join(out)


def _decode_run(row: dict[str, Any]) -> dict[str, Any]:
    run = dict(row)
    run["sources"] = json.loads(str(run.pop("sources_json", "[]") or "[]"))
    return run


def _decode_item(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    payload = json.loads(str(item.get("payload_json") or "{}"))
    payload.update(
        {
            "id": item["item_id"],
            "rank": item["rank"],
            "title": item["title"],
            "source": item["source"],
            "source_url": item["source_url"],
            "date": item["date"],
            "score": item["score"],
            "summary": item["summary"],
            "suggested_ce_angle": item["suggested_ce_angle"],
            "pillar": item["pillar"],
            "follow_up_search_query": item["follow_up_query"],
        }
    )
    return payload


def _decode_event(row: dict[str, Any]) -> dict[str, Any]:
    event = dict(row)
    event["item_count"] = int(event.get("item_count") or 0)
    return event


def _primary_pillar(item: dict[str, Any]) -> str:
    pillars = item.get("pillar_fit") or []
    if isinstance(pillars, list) and pillars:
        return str(pillars[0])
    return "Unsorted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
