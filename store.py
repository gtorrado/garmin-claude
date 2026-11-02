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
