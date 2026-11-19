"""Copy the local SQLite history into Postgres (Neon), once, before switching the server over.

Run on the server, with the garmin container stopped so nothing writes meanwhile:

    cd ~/garmin-claude/deploy
    sudo docker compose stop garmin
    sudo docker compose run --rm garmin python migrate_to_postgres.py
    # check the counts it prints, then add DATABASE_URL to deploy/.env and run update.sh

DATABASE_URL must be set (it is read from deploy/.env). Safe to re-run: rows are upserted.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from store import PgStore

BATCH = 200


def main() -> int:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set.")
        return 1
    db_path = Path(os.getenv("DATA_DIR", "./data")) / "garmin.db"
    if not db_path.exists():
        print(f"No SQLite file at {db_path}.")
        return 1
    print(f"SQLite file: {db_path} ({db_path.stat().st_size / 1e6:.0f} MB)")

    src = sqlite3.connect(db_path)
    pg = PgStore(url, os.getenv("DB_SCHEMA", "garmin"))
    s = pg.s

    def nul(text):  # jsonb rejects \u0000
        return None if text is None else text.replace("\\u0000", "")

    jobs = [
        ("daily_raw", "SELECT date, kind, data, fetched_at FROM daily_raw",
         f"INSERT INTO {s}.daily_raw(date, kind, data, fetched_at) VALUES (%s,%s,%s::jsonb,%s) "
         "ON CONFLICT (date, kind) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
         lambda r: (r[0], r[1], nul(r[2]), r[3])),
        ("daily_summary", "SELECT date, data, updated_at FROM daily_summary",
         f"INSERT INTO {s}.daily_summary(date, data, updated_at) VALUES (%s,%s::jsonb,%s) "
         "ON CONFLICT (date) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
         lambda r: (r[0], nul(r[1]), r[2])),
        ("activities", "SELECT id, start_local, type, summary, raw, details FROM activities",
         f"INSERT INTO {s}.activities(id, start_local, type, summary, raw, details) "
         "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb) ON CONFLICT (id) DO UPDATE SET "
         "start_local=excluded.start_local, type=excluded.type, summary=excluded.summary, "
         "raw=excluded.raw, details=excluded.details",
         lambda r: (r[0], r[1], r[2], nul(r[3]), nul(r[4]), nul(r[5]))),
        ("meta", "SELECT key, value FROM meta",
         f"INSERT INTO {s}.meta(key, value) VALUES (%s,%s) "
         "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
         lambda r: tuple(r)),
    ]

    for table, select, insert, row in jobs:
        cur = src.execute(select)
        n = 0
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            with pg._lock:
                with pg._connect().cursor() as c:
                    c.executemany(insert, [row(r) for r in rows])
            n += len(rows)
            print(f"  {table}: {n}", end="\r")
        print(f"  {table}: {n} rows copied")

    print("\nIn Postgres now:", pg.counts())
    size = pg._query(
        "SELECT pg_size_pretty(SUM(pg_total_relation_size(c.oid))) AS size FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'",
        (s,),
    )[0]["size"]
    print(f"Schema '{s}' size: {size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
