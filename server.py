"""Garmin → Claude remote MCP server.

Exposes your synced Garmin data as tools Claude can call from claude.ai (web + mobile).
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

import json

import calendar_tools as cal
from garmin_sync import GarminSyncer, trim
from store import Store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
TZ = os.getenv("TZ_NAME", "Europe/Lisbon")
SECRET = os.getenv("MCP_SECRET", "")
SYNC_INTERVAL_MIN = int(os.getenv("SYNC_INTERVAL_MIN", "60"))
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "730"))
POLL_MIN = int(os.getenv("WORKOUT_POLL_MIN", "10"))

store = Store(DATA_DIR / "garmin.db")
syncer = GarminSyncer(store, DATA_DIR, TZ)

mcp = MCPServer(
    name="garmin",
    title="My Garmin data",
    instructions=(
        "Personal Garmin Connect data (health metrics, sleep, HRV, training readiness, "
        "training load, workouts, training calendar/plan, races), synced to a local history. For any question about the "
        "user's health, recovery, sleep or training, start with get_coach_briefing, then drill "
        "down with the other tools. Dates are YYYY-MM-DD in the user's timezone. Health data refreshes "
        "hourly and new workouts are pulled within ~10 minutes of syncing the watch; call "
        "sync_now if the user just finished a workout and it isn't there yet."
    ),
)


def _resolve_date(d: str | None) -> str:
    if not d or d.lower() == "today":
        return syncer.today().isoformat()
    if d.lower() == "yesterday":
        return (syncer.today() - timedelta(days=1)).isoformat()
    return date.fromisoformat(d).isoformat()


def _flatten(summary: dict) -> dict[str, Any]:
    """{'sleep': {'score': 80}} -> {'sleep.score': 80} (numbers only) for trends."""
    out = {}
    for section, vals in summary.items():
        if isinstance(vals, dict):
            for k, v in vals.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[f"{section}.{k}"] = v
    return out


def _status() -> dict:
    return {
        "last_sync_utc": store.get_meta("last_sync_utc"),
        "last_sync_error": syncer.last_error,
        "timezone": TZ,
        "history_target_days": BACKFILL_DAYS,
        "history_backfill_complete": int(store.get_meta("backfill_done_days") or 0) >= BACKFILL_DAYS,
        "workouts_awaiting_details": store.count_missing_details(),
        **store.counts(),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_coach_briefing() -> dict:
    """Best starting point. Today's + last night's health metrics, training readiness and
    status, a 7-day vs 28-day baseline comparison of key metrics, and the last 5 workouts."""
    today = syncer.today()
    t, y = today.isoformat(), (today - timedelta(days=1)).isoformat()
    week = store.summaries_between((today - timedelta(days=6)).isoformat(), t)
    month = store.summaries_between((today - timedelta(days=27)).isoformat(), t)

    key = ["daily.resting_hr", "hrv.last_night_avg_ms", "sleep.score", "sleep.total_h",
           "training_readiness.score", "daily.avg_stress", "daily.body_battery_at_wake",
           "training.acute_load", "daily.steps"]

    def avg(rows: list[dict], k: str) -> float | None:
        vals = [_flatten(r)[k] for r in rows if k in _flatten(r)]
        return round(statistics.mean(vals), 1) if vals else None

    return {
        "today": t,
        "today_metrics": store.get_summary(t),
        "yesterday_metrics": store.get_summary(y),
        "baseline_comparison": {k: {"avg_7d": avg(week, k), "avg_28d": avg(month, k)} for k in key},
        "recent_workouts": store.recent_activities(5),
        "next_7_days_plan": json.loads(store.get_meta("calendar_next7") or "null"),
        "next_race": json.loads(store.get_meta("primary_race") or "null"),
        "sync_status": _status(),
    }


@mcp.tool()
def get_daily_metrics(day: str = "today") -> dict:
    """All health metrics for one day: steps, resting HR, stress, Body Battery, sleep
    (stages/score), HRV, training readiness, training status/load, VO2 max, weight.
    `day` is 'today', 'yesterday' or YYYY-MM-DD. Sleep is the night ending that morning."""
    d = _resolve_date(day)
    s = store.get_summary(d)
    if s is None:
        return {"date": d, "error": "No data stored for this day. Try sync_day first."}
    return {"date": d, **s}


@mcp.tool()
def get_trends(days: int = 30, metrics: list[str] | None = None) -> dict:
    """Day-by-day values of numeric metrics over the last `days` days (max 730), plus
    average/min/max per metric. `metrics` are names like 'sleep.score',
    'hrv.last_night_avg_ms', 'daily.resting_hr', 'training_readiness.score',
    'training.acute_load', 'training.vo2max_running', 'body.weight_kg', 'daily.steps'.
    Omit `metrics` for a default set; call with metrics=['?'] to list all names."""
    days = max(1, min(days, 730))
    end = syncer.today()
    rows = store.summaries_between((end - timedelta(days=days - 1)).isoformat(), end.isoformat())
    flat = [{"date": r["date"], **_flatten(r)} for r in rows]
    available = sorted({k for r in flat for k in r if k != "date"})
    if metrics == ["?"]:
        return {"available_metrics": available}
    metrics = metrics or ["sleep.score", "sleep.total_h", "hrv.last_night_avg_ms",
                          "daily.resting_hr", "training_readiness.score", "daily.avg_stress",
                          "daily.body_battery_at_wake", "training.acute_load"]
    series = [{"date": r["date"], **{m: r.get(m) for m in metrics}} for r in flat]
    stats = {}
    for m in metrics:
        vals = [r[m] for r in flat if r.get(m) is not None]
        if vals:
            stats[m] = {"avg": round(statistics.mean(vals), 2), "min": min(vals),
                        "max": max(vals), "days_with_data": len(vals)}
    unknown = [m for m in metrics if m not in available]
    return {"days": days, "summary": stats, "daily": series,
            **({"unknown_metrics": unknown, "available_metrics": available} if unknown else {})}


@mcp.tool()
def get_recent_activities(limit: int = 10, activity_type: str | None = None,
                          since: str | None = None) -> list[dict]:
    """Recent workouts, newest first: type, distance, duration, pace, HR, HR zones,
    training effect and load. Filter with activity_type (e.g. 'running', 'cycling',
    'strength') and/or since (YYYY-MM-DD)."""
    return store.recent_activities(max(1, min(limit, 200)), activity_type,
                                   _resolve_date(since) if since else None)


@mcp.tool()
async def get_activity_details(activity_id: int, include_raw: bool = False) -> dict:
    """Everything about one workout: summary, per-lap splits (pace/HR/cadence/power),
    time in HR and power zones, running dynamics, stamina, RPE/feel, training-effect
    messages, strength sets (exercise/reps/weight), weather, personal records.
    include_raw=True adds Garmin's raw JSON (incl. interval/workout-step splits).
    Get IDs from get_recent_activities."""
    a = store.get_activity(activity_id)
    if a is None:
        return {"error": f"Activity {activity_id} not found. Try sync_now."}
    details = a["details"]
    if details is None:
        details = await anyio.to_thread.run_sync(syncer.fetch_activity_details, activity_id)
    raw = details.pop("_raw", None)
    out = {"summary": a["summary"], **details}
    if include_raw and raw:
        out["raw"] = trim(raw)
    return out


def _refresh_calendar_cache() -> None:
    today = syncer.today()
    c = cal.build_calendar(syncer, today, today + timedelta(days=6), include_completed=False)
    store.set_meta("calendar_next7", json.dumps(c["days"]))
    race = cal.upcoming_races(syncer).get("primary_race")
    store.set_meta("primary_race", json.dumps(race or None))


syncer.after_sync.append(_refresh_calendar_cache)


@mcp.tool()
async def get_training_calendar(start: str = "today", days: int = 14) -> dict:
    """The Garmin Connect calendar, day by day: planned workouts (incl. Garmin Coach /
    adaptive training plans, with step-by-step structure where available), races and
    events, plus workouts already completed. `start` is 'today', 'yesterday' or
    YYYY-MM-DD; use a past start to compare plan vs. what was actually done.
    `days` max 92. Fetched live from Garmin."""
    s = date.fromisoformat(_resolve_date(start))
    e = s + timedelta(days=max(1, min(days, 92)) - 1)
    return await anyio.to_thread.run_sync(cal.build_calendar, syncer, s, e)


@mcp.tool()
async def get_planned_workout(workout_id: int | None = None,
                              scheduled_workout_id: int | None = None) -> dict:
    """Full step-by-step structure of a planned workout (warm-up, intervals with pace /
    HR / power targets, recoveries, cool-down). IDs come from get_training_calendar."""
    return await anyio.to_thread.run_sync(cal.planned_workout, syncer, workout_id,
                                          scheduled_workout_id)


@mcp.tool()
async def get_training_plan() -> dict:
    """Active Garmin training plan(s): name, goal/race, start-end dates, phase and progress.
    Use get_training_calendar for the individual planned sessions."""
    return await anyio.to_thread.run_sync(cal.training_plans, syncer, syncer.today())


@mcp.tool()
async def get_races() -> dict:
    """Upcoming races/events from the Garmin calendar (incl. the primary 'focus' race,
    goal times) and Garmin's current race-time predictions (5K, 10K, half, marathon)."""
    return await anyio.to_thread.run_sync(cal.upcoming_races, syncer)


@mcp.tool()
def get_raw_garmin_data(day: str, kind: str) -> Any:
    """Escape hatch: the raw Garmin JSON for a day when the compact metrics lack something.
    kind: stats | sleep | hrv | readiness | training_status | weight. Long time series are
    shortened."""
    d = _resolve_date(day)
    data = store.get_raw(d, kind)
    if data is None:
        return {"error": f"No raw '{kind}' for {d}", "available_kinds": store.raw_kinds(d)}
    return trim(data)


@mcp.tool()
async def sync_now() -> dict:
    """Pull the latest data from Garmin right now (today, yesterday, new workouts with
    full details). New workouts are also picked up automatically within ~10 minutes."""
    try:
        res = await anyio.to_thread.run_sync(syncer.sync_recent)
        return {"ok": True, **res, "status": _status()}
    except Exception as e:  # noqa: BLE001
        syncer.last_error = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": syncer.last_error}


@mcp.tool()
async def sync_day(day: str) -> dict:
    """Fetch (or re-fetch) one specific day from Garmin, e.g. older than the stored history."""
    d = date.fromisoformat(_resolve_date(day))
    try:
        summary = await anyio.to_thread.run_sync(syncer.sync_day, d)
        return {"ok": True, "date": d.isoformat(), **summary}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def get_sync_status() -> dict:
    """How much history is stored, when the last sync ran, and any sync error."""
    return _status()


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "last_sync_utc": store.get_meta("last_sync_utc"),
                         "error": syncer.last_error})


def main() -> None:
    if len(SECRET) < 24:
        raise SystemExit("Set MCP_SECRET to a long random string (>= 24 chars). "
                         "It becomes part of your private connector URL.")
    path = f"/mcp/{SECRET}"
    app = mcp.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        # The secret URL is the access control; Host/Origin checks would block claude.ai.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    if os.getenv("DISABLE_SYNC") != "1":
        threading.Thread(target=syncer.run_forever, args=(SYNC_INTERVAL_MIN, POLL_MIN, BACKFILL_DAYS),
                         daemon=True).start()
    port = int(os.getenv("PORT", "8000"))
    log.info("MCP endpoint ready at http://0.0.0.0:%s/mcp/<MCP_SECRET>", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
