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

import hashlib
import json

import calendar_tools as cal
import strength_tools as st
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
        "sync_now if the user just finished a workout and it isn't there yet. "
        "Write tools (schedule_strength_workout, schedule_hevy_workout, remove_created_workout) "
        "change the user's Garmin calendar: in a conversation, run them with dry_run=true first, "
        "show the preview (especially exercise matches marked close/category/unmatched) and only "
        "call again with dry_run=false after the user confirms. The user's own scheduled "
        "automation task may call them directly with dry_run=false."
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


# ---------------------------------------------------------------------------
# Strength workouts → Garmin calendar
# ---------------------------------------------------------------------------
def _created() -> list[dict]:
    return json.loads(store.get_meta("created_workouts") or "[]")


def _remove_created(rec: dict) -> None:
    if rec.get("scheduled_workout_id"):
        try:
            syncer._call(lambda c: c.unschedule_workout(rec["scheduled_workout_id"]))
        except Exception:  # noqa: BLE001 - deleting the template removes it anyway
            pass
    try:
        syncer._call(lambda c: c.delete_workout(rec["workout_id"]))
    except Exception as e:  # noqa: BLE001
        log.warning("delete workout %s failed: %s", rec.get("workout_id"), e)
    store.set_meta("created_workouts", json.dumps([r for r in _created() if r != rec]))


def _upload_and_schedule(name: str, day: str, exercises: list[dict], description: str | None,
                         dry_run: bool, source_event_id: str | None = None) -> dict:
    d = _resolve_date(day)
    fingerprint = hashlib.sha1(json.dumps([name, d, exercises, description], sort_keys=True,
                                          default=str).encode()).hexdigest()[:16]
    previous = next((r for r in _created() if source_event_id
                     and r.get("source_event_id") == source_event_id), None)
    if previous and previous.get("fingerprint") == fingerprint:
        return {"unchanged": True, **previous,
                "note": "Already in the Garmin calendar exactly like this; nothing to do."}
    workout, report, est = st.build_workout(name, exercises, description, st.load_overrides(store))
    preview = {
        "name": name, "date": d, "estimated_duration_min": round(est / 60),
        "exercises": st.preview_lines(exercises, report),
        "needs_attention": [r for r in report if r["confidence"] in ("category", "unmatched", "close")],
    }
    if previous:
        preview["replaces"] = previous
    if dry_run:
        return {"dry_run": True, **preview,
                "next": "Show this to the user. Fix matches with garmin_exercise (see "
                        "find_garmin_exercises) or save_exercise_mapping, then call again with "
                        "dry_run=false."}
    if previous:  # the source event changed: replace the old Garmin workout
        _remove_created(previous)
    up = syncer._call(lambda c: c.upload_strength_workout(workout))
    wid = up.get("workoutId")
    sch = syncer._call(lambda c: c.schedule_workout(wid, d)) or {}
    sid = sch.get("workoutScheduleId") or g_id(sch)
    rec = {"workout_id": wid, "scheduled_workout_id": sid, "date": d, "name": name,
           "source_event_id": source_event_id, "fingerprint": fingerprint}
    rec = {k: v for k, v in rec.items() if v is not None}
    store.set_meta("created_workouts", json.dumps(_created() + [rec]))
    return {"created": True, **rec, **preview,
            "note": "It's in the Garmin calendar now; the watch picks it up on its next sync."}


def g_id(sch: dict) -> Any:
    for k in ("id", "scheduleId", "calendarItemId"):
        if sch.get(k):
            return sch[k]
    return None


@mcp.tool()
async def schedule_strength_workout(name: str, date: str, exercises: list[dict],
                                    description: str | None = None,
                                    dry_run: bool = True,
                                    source_event_id: str | None = None) -> dict:
    """Create a structured strength workout in Garmin Connect and put it on the calendar
    (it then syncs to the watch, which guides each set/rep/weight/rest).
    exercises: [{"name": "Bench Press (Barbell)", "rest_seconds": 120, "notes": "...",
                 "superset": "A" (optional: consecutive exercises with the same key alternate),
                 "garmin_exercise": "Barbell Bench Press" or "BENCH_PRESS/BARBELL_BENCH_PRESS" (optional),
                 "sets": [{"reps": 8, "weight_kg": 60, "type": "warmup|normal"}, ...]
                         or shorthand "sets": 4, "reps": 8, "weight_kg": 60}]
    Timed sets: {"duration_seconds": 45}. Names are auto-matched to Garmin's exercise list.
    Follow the naming style of the user's other calendar workouts when sensible.
    source_event_id: the Google Calendar event this comes from. Makes it idempotent: same
    content → 'unchanged'; changed event (moved/edited) → the old Garmin workout is replaced.
    In a chat: ALWAYS dry_run=true first and get the user's OK."""
    return await anyio.to_thread.run_sync(_upload_and_schedule, name, date, exercises,
                                          description, dry_run, source_event_id)


@mcp.tool()
def find_garmin_exercises(query: str) -> list[dict]:
    """Search Garmin's strength-exercise list (1,500+ exercises), e.g. 'hip thrust',
    'cable row'. Use the returned name or id as garmin_exercise."""
    return st.search_exercises(query)


@mcp.tool()
def save_exercise_mapping(source_name: str, garmin_exercise: str) -> dict:
    """Remember that an exercise name (e.g. Hevy's 'Hip Thrust (Barbell)') should always map
    to a Garmin exercise (display name or CATEGORY/EXERCISE id)."""
    r = st.resolve_garmin(garmin_exercise)
    if not r:
        return {"error": "Unknown Garmin exercise — use find_garmin_exercises."}
    m = st.load_overrides(store)
    m[source_name.strip().lower()] = garmin_exercise
    store.set_meta("exercise_map", json.dumps(m))
    return {"saved": True, "source": source_name, **r}


@mcp.tool()
async def list_hevy(kind: str = "routines", page: int = 1) -> dict:
    """List the user's Hevy routines (kind='routines') or logged workouts (kind='workouts'),
    10 per page, with their exercises. Requires HEVY_API_KEY on the server (Hevy Pro)."""
    if kind not in ("routines", "workouts"):
        return {"error": "kind must be 'routines' or 'workouts'"}
    try:
        return await anyio.to_thread.run_sync(st.hevy_list, kind, page)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@mcp.tool()
async def schedule_hevy_workout(hevy_id: str, date: str, kind: str = "routines",
                                name: str | None = None, dry_run: bool = True) -> dict:
    """Convert a Hevy routine (or a past logged Hevy workout, kind='workouts') into a Garmin
    strength workout and schedule it on `date` — sets, reps, weights, rest and supersets kept.
    IDs from list_hevy. ALWAYS dry_run=true first and get the user's OK."""
    if kind not in ("routines", "workouts"):
        return {"error": "kind must be 'routines' or 'workouts'"}
    try:
        h = await anyio.to_thread.run_sync(st.hevy_get, kind, hevy_id)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    exs = st.hevy_to_exercises(h)
    return await anyio.to_thread.run_sync(_upload_and_schedule, name or h.get("title") or "Strength",
                                          date, exs, h.get("notes") or h.get("description"), dry_run)


@mcp.tool()
async def remove_created_workout(scheduled_workout_id: int | None = None,
                                 workout_id: int | None = None, dry_run: bool = True) -> dict:
    """Remove a workout that THIS connector created (unschedule + delete its template).
    Workouts from Runna, Garmin Coach etc. are never touched. Lists created workouts when
    called without IDs."""
    created = _created()
    if not scheduled_workout_id and not workout_id:
        return {"created_workouts": created}
    rec = next((r for r in created if (scheduled_workout_id and r.get("scheduled_workout_id") == scheduled_workout_id)
                or (workout_id and r.get("workout_id") == workout_id)), None)
    if not rec:
        return {"error": "Not a workout created by this connector.", "created_workouts": created}
    if dry_run:
        return {"dry_run": True, "would_remove": rec}
    await anyio.to_thread.run_sync(_remove_created, rec)
    return {"removed": rec}


@mcp.tool()
def get_logged_sessions(since: str | None = None) -> list[dict]:
    """Strength sessions the automation already logged to Google Calendar (so they're not
    logged twice): activity ids, Hevy ids, calendar event ids."""
    rows = json.loads(store.get_meta("logged_sessions") or "[]")
    return [r for r in rows if not since or (r.get("date") or "") >= since]


@mcp.tool()
def mark_session_logged(date: str, google_event_id: str, garmin_activity_id: int | None = None,
                        hevy_workout_id: str | None = None, note: str | None = None) -> dict:
    """Record that a completed session was written into its Google Calendar event."""
    rows = json.loads(store.get_meta("logged_sessions") or "[]")
    rec = {k: v for k, v in {"date": _resolve_date(date), "google_event_id": google_event_id,
                             "garmin_activity_id": garmin_activity_id,
                             "hevy_workout_id": hevy_workout_id, "note": note}.items() if v is not None}
    rows = [r for r in rows if r.get("google_event_id") != google_event_id] + [rec]
    store.set_meta("logged_sessions", json.dumps(rows[-500:]))
    return {"saved": True, **rec}


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
