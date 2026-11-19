"""SQLite storage for synced Garmin data (the "local history")."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_raw (
    date       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    data       TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, kind)
);
CREATE TABLE IF NOT EXISTS daily_summary (
    date       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY,
    start_local TEXT,
    type        TEXT,
    summary     TEXT NOT NULL,
    raw         TEXT NOT NULL,
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_local);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- generic helpers -------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- meta ------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # -- daily -----------------------------------------------------------
    def put_raw(self, date: str, kind: str, data: Any) -> None:
        self._exec(
            "INSERT OR REPLACE INTO daily_raw(date, kind, data, fetched_at) VALUES (?,?,?,?)",
            (date, kind, json.dumps(data), _now()),
        )

    def get_raw(self, date: str, kind: str) -> Any:
        rows = self._query("SELECT data FROM daily_raw WHERE date=? AND kind=?", (date, kind))
        return json.loads(rows[0]["data"]) if rows else None

    def raw_kinds(self, date: str) -> list[str]:
        return [r["kind"] for r in self._query("SELECT kind FROM daily_raw WHERE date=?", (date,))]

    def put_summary(self, date: str, summary: dict) -> None:
        self._exec(
            "INSERT OR REPLACE INTO daily_summary(date, data, updated_at) VALUES (?,?,?)",
            (date, json.dumps(summary), _now()),
        )

    def get_summary(self, date: str) -> dict | None:
        rows = self._query("SELECT data, updated_at FROM daily_summary WHERE date=?", (date,))
        if not rows:
            return None
        out = json.loads(rows[0]["data"])
        out["_synced_at_utc"] = rows[0]["updated_at"]
        return out

    def has_day(self, date: str) -> bool:
        return bool(self._query("SELECT 1 FROM daily_summary WHERE date=?", (date,)))

    def summaries_between(self, start: str, end: str) -> list[dict]:
        rows = self._query(
            "SELECT date, data FROM daily_summary WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        )
        return [{"date": r["date"], **json.loads(r["data"])} for r in rows]

    # -- activities ------------------------------------------------------
    def put_activity(self, act_id: int, start_local: str, type_: str, summary: dict, raw: dict) -> None:
        # Keep any details already fetched.
        self._exec(
            """INSERT INTO activities(id, start_local, type, summary, raw) VALUES (?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET start_local=excluded.start_local,
               type=excluded.type, summary=excluded.summary, raw=excluded.raw""",
            (act_id, start_local, type_, json.dumps(summary), json.dumps(raw)),
        )

    def put_activity_details(self, act_id: int, details: dict) -> None:
        self._exec("UPDATE activities SET details=? WHERE id=?", (json.dumps(details), act_id))

    def activity_ids_without_details(self, limit: int) -> list[int]:
        rows = self._query(
            "SELECT id FROM activities WHERE details IS NULL ORDER BY start_local DESC LIMIT ?",
            (limit,),
        )
        return [r["id"] for r in rows]

    def count_missing_details(self) -> int:
        return self._query("SELECT COUNT(*) c FROM activities WHERE details IS NULL")[0]["c"]

    def activity_ids_between(self, start: str, end: str) -> set[int]:
        """IDs of stored activities whose local start date is in [start, end] (YYYY-MM-DD)."""
        rows = self._query(
            "SELECT id FROM activities WHERE substr(start_local, 1, 10) BETWEEN ? AND ?",
            (start, end),
        )
        return {r["id"] for r in rows}

    def delete_activities(self, ids: set[int]) -> None:
        for act_id in ids:
            self._exec("DELETE FROM activities WHERE id=?", (act_id,))

    def recent_activities(self, limit: int, type_filter: str | None = None,
                          since: str | None = None) -> list[dict]:
        sql = "SELECT summary FROM activities WHERE 1=1"
        params: list[Any] = []
        if type_filter:
            sql += " AND type LIKE ?"
            params.append(f"%{type_filter}%")
        if since:
            sql += " AND start_local >= ?"
            params.append(since)
        sql += " ORDER BY start_local DESC LIMIT ?"
        params.append(limit)
        return [json.loads(r["summary"]) for r in self._query(sql, tuple(params))]

    def get_activity(self, act_id: int) -> dict | None:
        rows = self._query("SELECT summary, raw, details FROM activities WHERE id=?", (act_id,))
        if not rows:
            return None
        r = rows[0]
        return {
            "summary": json.loads(r["summary"]),
            "raw": json.loads(r["raw"]),
            "details": json.loads(r["details"]) if r["details"] else None,
        }

    def latest_activity_start(self) -> str | None:
        rows = self._query("SELECT MAX(start_local) AS m FROM activities")
        return rows[0]["m"] if rows else None

    def counts(self) -> dict:
        d = self._query("SELECT COUNT(*) c, MIN(date) mn, MAX(date) mx FROM daily_summary")[0]
        a = self._query("SELECT COUNT(*) c, MIN(start_local) mn, MAX(start_local) mx FROM activities")[0]
        return {
            "days_stored": d["c"], "first_day": d["mn"], "last_day": d["mx"],
            "activities_stored": a["c"], "first_activity": a["mn"], "last_activity": a["mx"],
        }


# ---------------------------------------------------------------------------
# Postgres (Neon) backend. Same interface as Store; used when DATABASE_URL is
# set, so the coach app can read the same history. Tables live in their own
# schema ("garmin" by default) so they never collide with the app's tables.
# Values stay JSON (jsonb) and dates/times stay ISO text, exactly as in SQLite.
# ---------------------------------------------------------------------------

def _pg_schema_sql(schema: str) -> str:
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.daily_raw (
    date       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    data       JSONB,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, kind)
);
CREATE TABLE IF NOT EXISTS {schema}.daily_summary (
    date       TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {schema}.activities (
    id          BIGINT PRIMARY KEY,
    start_local TEXT,
    type        TEXT,
    summary     JSONB NOT NULL,
    raw         JSONB NOT NULL,
    details     JSONB
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON {schema}.activities(start_local);
CREATE TABLE IF NOT EXISTS {schema}.meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _pg_json(value: Any) -> str:
    # Postgres jsonb rejects \u0000; Garmin occasionally sends it in free text.
    return json.dumps(value).replace("\\u0000", "")


class PgStore:
    """Postgres twin of Store. One autocommit connection, reopened if Neon drops it."""

    def __init__(self, url: str, schema: str = "garmin"):
        import psycopg  # imported here so SQLite-only installs don't need it
        from psycopg.rows import dict_row

        if not schema.isidentifier():
            raise ValueError(f"Bad schema name: {schema!r}")
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._url = url
        self.s = schema
        self._lock = threading.Lock()
        self._conn = None
        with self._lock:
            self._connect().execute(_pg_schema_sql(schema))

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg.connect(self._url, autocommit=True, row_factory=self._dict_row)
        return self._conn

    def _run(self, sql: str, params: tuple, fetch: bool):
        with self._lock:
            for attempt in (1, 2):
                try:
                    cur = self._connect().execute(sql.replace("{s}", self.s), params)
                    return cur.fetchall() if fetch else None
                except self._psycopg.OperationalError:
                    # Neon closes idle connections when the compute suspends; reconnect once.
                    if self._conn is not None:
                        self._conn.close()
                    self._conn = None
                    if attempt == 2:
                        raise

    def _exec(self, sql: str, params: tuple = ()) -> None:
        self._run(sql, params, fetch=False)

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        return self._run(sql, params, fetch=True)

    # -- meta ------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM {s}.meta WHERE key=%s", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO {s}.meta(key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- daily -----------------------------------------------------------
    def put_raw(self, date: str, kind: str, data: Any) -> None:
        self._exec(
            "INSERT INTO {s}.daily_raw(date, kind, data, fetched_at) VALUES (%s,%s,%s::jsonb,%s) "
            "ON CONFLICT (date, kind) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
            (date, kind, _pg_json(data), _now()),
        )

    def get_raw(self, date: str, kind: str) -> Any:
        rows = self._query("SELECT data FROM {s}.daily_raw WHERE date=%s AND kind=%s", (date, kind))
        return rows[0]["data"] if rows else None

    def raw_kinds(self, date: str) -> list[str]:
        return [r["kind"] for r in self._query("SELECT kind FROM {s}.daily_raw WHERE date=%s", (date,))]

    def put_summary(self, date: str, summary: dict) -> None:
        self._exec(
            "INSERT INTO {s}.daily_summary(date, data, updated_at) VALUES (%s,%s::jsonb,%s) "
            "ON CONFLICT (date) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (date, _pg_json(summary), _now()),
        )

    def get_summary(self, date: str) -> dict | None:
        rows = self._query("SELECT data, updated_at FROM {s}.daily_summary WHERE date=%s", (date,))
        if not rows:
            return None
        out = dict(rows[0]["data"])
        out["_synced_at_utc"] = rows[0]["updated_at"]
        return out

    def has_day(self, date: str) -> bool:
        return bool(self._query("SELECT 1 FROM {s}.daily_summary WHERE date=%s", (date,)))

    def summaries_between(self, start: str, end: str) -> list[dict]:
        rows = self._query(
            "SELECT date, data FROM {s}.daily_summary WHERE date BETWEEN %s AND %s ORDER BY date",
            (start, end),
        )
        return [{"date": r["date"], **r["data"]} for r in rows]

    # -- activities ------------------------------------------------------
    def put_activity(self, act_id: int, start_local: str, type_: str, summary: dict, raw: dict) -> None:
        self._exec(
            """INSERT INTO {s}.activities(id, start_local, type, summary, raw)
               VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)
               ON CONFLICT (id) DO UPDATE SET start_local=excluded.start_local,
               type=excluded.type, summary=excluded.summary, raw=excluded.raw""",
            (act_id, start_local, type_, _pg_json(summary), _pg_json(raw)),
        )

    def put_activity_details(self, act_id: int, details: dict) -> None:
        self._exec("UPDATE {s}.activities SET details=%s::jsonb WHERE id=%s", (_pg_json(details), act_id))

    def activity_ids_without_details(self, limit: int) -> list[int]:
        rows = self._query(
            "SELECT id FROM {s}.activities WHERE details IS NULL ORDER BY start_local DESC LIMIT %s",
            (limit,),
        )
        return [r["id"] for r in rows]

    def count_missing_details(self) -> int:
        return self._query("SELECT COUNT(*) c FROM {s}.activities WHERE details IS NULL")[0]["c"]

    def activity_ids_between(self, start: str, end: str) -> set[int]:
        rows = self._query(
            "SELECT id FROM {s}.activities WHERE substr(start_local, 1, 10) BETWEEN %s AND %s",
            (start, end),
        )
        return {r["id"] for r in rows}

    def delete_activities(self, ids: set[int]) -> None:
        if ids:
            self._exec("DELETE FROM {s}.activities WHERE id = ANY(%s)", (list(ids),))

    def recent_activities(self, limit: int, type_filter: str | None = None,
                          since: str | None = None) -> list[dict]:
        sql = "SELECT summary FROM {s}.activities WHERE 1=1"
        params: list[Any] = []
        if type_filter:
            sql += " AND type ILIKE %s"  # SQLite LIKE is case-insensitive too
            params.append(f"%{type_filter}%")
        if since:
            sql += " AND start_local >= %s"
            params.append(since)
        sql += " ORDER BY start_local DESC LIMIT %s"
        params.append(limit)
        return [r["summary"] for r in self._query(sql, tuple(params))]

    def get_activity(self, act_id: int) -> dict | None:
        rows = self._query("SELECT summary, raw, details FROM {s}.activities WHERE id=%s", (act_id,))
        if not rows:
            return None
        r = rows[0]
        return {"summary": r["summary"], "raw": r["raw"], "details": r["details"]}

    def latest_activity_start(self) -> str | None:
        rows = self._query("SELECT MAX(start_local) AS m FROM {s}.activities")
        return rows[0]["m"] if rows else None

    def counts(self) -> dict:
        d = self._query("SELECT COUNT(*) c, MIN(date) mn, MAX(date) mx FROM {s}.daily_summary")[0]
        a = self._query("SELECT COUNT(*) c, MIN(start_local) mn, MAX(start_local) mx FROM {s}.activities")[0]
        return {
            "days_stored": d["c"], "first_day": d["mn"], "last_day": d["mx"],
            "activities_stored": a["c"], "first_activity": a["mn"], "last_activity": a["mx"],
        }


def open_store(data_dir: Path):
    """Postgres when DATABASE_URL is set, otherwise the local SQLite file."""
    import os

    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return PgStore(url, os.getenv("DB_SCHEMA", "garmin"))
    return Store(data_dir / "garmin.db")
