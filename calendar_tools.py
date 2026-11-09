"""Garmin calendar: planned workouts, training plans, races. Fetched live from Garmin."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from garmin_sync import g, pace_min_per_km

NOISE = {
    "ownerId", "userProfilePk", "userProfileId", "createdDate", "updatedDate", "updateDate",
    "createDate", "iconUrl", "shareable", "sharedWithUsers", "consumer", "consumerImageURL",
    "consumerName", "consumerWebsiteURL", "author", "atpPlanId", "workoutProvider",
    "workoutSourceId", "uploadTimestamp", "poolLength", "poolLengthUnit", "avgTrainingSpeed",
    "estimateType", "estimatedDistanceUnit", "isWheelchair", "isSessionTransitionEnabled",
    "subscribed", "providerId", "lastUpdated", "trainingPlanVersion",
}


def compact(obj: Any, depth: int = 0) -> Any:
    """Generic shrink of Garmin JSON: drop noise/empty fields, round floats, cap lists."""
    if depth > 7:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in NOISE or k in ("workoutSegments", "steps"):
                continue
            v = compact(v, depth + 1)
            if v in (None, "", [], {}, False):
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        if len(obj) > 80:
            return [compact(x, depth + 1) for x in obj[:80]] + [f"… {len(obj) - 80} more"]
        return [compact(x, depth + 1) for x in obj]
    if isinstance(obj, float):
        return round(obj, 2)
    return obj


# ---------------------------------------------------------------------------
# Workout steps → readable lines
# ---------------------------------------------------------------------------
def _fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d} min" if s >= 60 else f"{s} s"


def _end(step: dict) -> str:
    kind = g(step, "endCondition", "conditionTypeKey") or ""
    val = step.get("endConditionValue")
    if kind == "time" and isinstance(val, (int, float)):
        return _fmt_secs(val)
    if kind == "distance" and isinstance(val, (int, float)):
        return f"{val / 1000:g} km" if val >= 1000 else f"{val:g} m"
    if kind == "lap.button":
        return "until lap button"
    if kind == "iterations":
        return ""
    if kind and val is not None:
        return f"{kind} {val:g}" if isinstance(val, (int, float)) else f"{kind} {val}"
    return kind


def _target(step: dict) -> str:
    kind = g(step, "targetType", "workoutTargetTypeKey") or ""
    lo, hi = step.get("targetValueOne"), step.get("targetValueTwo")
    zone = step.get("zoneNumber")
    if kind in ("", "no.target"):
        return ""
    if kind in ("pace.zone", "speed.zone") and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        fast, slow = max(lo, hi), min(lo, hi)
        return f"@ {pace_min_per_km(fast).replace('/km', '')}–{pace_min_per_km(slow)}"
    if zone:
        return f"@ {kind.replace('.zone', '').replace('.', ' ')} zone {zone}"
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        unit = {"heart.rate.zone": " bpm", "power.zone": " W", "cadence": " spm"}.get(kind, "")
        return f"@ {lo:g}–{hi:g}{unit}"
    return f"@ {kind}"


def describe_steps(steps: list | None, indent: str = "") -> list[str]:
    lines: list[str] = []
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        inner = st.get("workoutSteps")
        if inner:  # repeat group
            n = st.get("numberOfIterations") or st.get("endConditionValue") or "?"
            n = int(n) if isinstance(n, float) else n
            lines.append(f"{indent}{n}x:")
            lines += describe_steps(inner, indent + "  ")
            continue
        kind = g(st, "stepType", "stepTypeKey") or "step"
        parts = [kind, _end(st), _target(st)]
        if st.get("description"):
            parts.append(f"({st['description']})")
        if g(st, "exerciseName"):
            parts.append(f"[{st.get('category', '')}/{st['exerciseName']}]".replace("[/", "["))
        lines.append(indent + " ".join(p for p in parts if p))
    return lines


def summarize_workout(w: dict | None) -> dict:
    if not isinstance(w, dict):
        return {}
    steps: list[str] = []
    for seg in w.get("workoutSegments") or []:
        steps += describe_steps(seg.get("workoutSteps"))
    dur = w.get("estimatedDurationInSecs")
    dist = w.get("estimatedDistanceInMeters")
    out = {
        "workout_id": w.get("workoutId"),
        "name": w.get("workoutName"),
        "sport": g(w, "sportType", "sportTypeKey"),
        "description": w.get("description"),
        "estimated_duration_min": round(dur / 60) if isinstance(dur, (int, float)) and dur else None,
        "estimated_distance_km": round(dist / 1000, 2) if isinstance(dist, (int, float)) and dist else None,
        "training_plan_id": w.get("trainingPlanId"),
        "steps": steps,
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


# ---------------------------------------------------------------------------
# Calendar assembly
# ---------------------------------------------------------------------------
def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build_calendar(syncer, start: date, end: date, include_completed: bool = True) -> dict:
    """Planned workouts + events (+ completed workouts from the local history), by day."""
    days: dict[str, dict[str, list]] = {}
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []

    def add(day: str, bucket: str, item: dict, title: str | None) -> None:
        if not day or not (start.isoformat() <= day[:10] <= end.isoformat()):
            return
        key = (day[:10], bucket, (title or "").strip().lower())
        if title and key in seen:
            return
        seen.add(key)
        days.setdefault(day[:10], {}).setdefault(bucket, []).append(item)

    # 1) Plans incl. Garmin Coach / adaptive plans.
    try:
        sched = syncer._call(lambda c: c.get_scheduled_workouts_by_period(start.isoformat(), end.isoformat())) or []
        if isinstance(sched, dict):
            sched = sched.get("workoutScheduleList") or sched.get("calendarItems") or []
        for s in sched:
            w = s.get("workout") if isinstance(s.get("workout"), dict) else None
            item = summarize_workout(w) if w else compact(s)
            if w:
                item = {**{k: v for k, v in compact(s).items() if not isinstance(v, (dict, list))}, **item}
            day = s.get("date") or s.get("calendarDate") or s.get("scheduledDate") or ""
            add(day, "planned", item, item.get("name") or item.get("title"))
    except Exception as e:  # noqa: BLE001
        errors.append(f"scheduled workouts: {e}")

    # 2) Calendar months: manually scheduled workouts, races, events, notes.
    for y, m in _months(start, end):
        try:
            cal = syncer._call(lambda c, y=y, m=m: c.get_scheduled_workouts(y, m)) or {}
        except Exception as e:  # noqa: BLE001
            errors.append(f"calendar {y}-{m:02d}: {e}")
            continue
        for it in cal.get("calendarItems") or []:
            kind = (it.get("itemType") or "").lower()
            if kind == "activity":
                continue  # completed workouts come from the local history (richer)
            bucket = "planned" if kind == "workout" else "events"
            item = compact(it)
            add(it.get("date") or "", bucket, item, it.get("title"))

    # 3) Completed workouts from local history.
    if include_completed:
        for a in syncer.store.recent_activities(500, since=start.isoformat()):
            if (a.get("start_local") or "")[:10] <= end.isoformat():
                add(a["start_local"][:10], "completed", {
                    k: a.get(k) for k in ("id", "name", "type", "distance_km", "duration_min",
                                          "avg_pace", "avg_hr", "te_label", "training_load")
                    if a.get(k) is not None}, None)

    out = {"from": start.isoformat(), "to": end.isoformat(),
           "days": [{"date": d, **days[d]} for d in sorted(days)]}
    if errors:
        out["partial_errors"] = errors
    return out


def upcoming_races(syncer, days_forward: int = 365) -> dict:
    out: dict[str, Any] = {}
    for key, fn in (
        ("primary_race", lambda c: c.get_primary_event()),
        ("upcoming_events", lambda c: c.get_upcoming_events(days_forward, 20)),
        ("race_predictions", lambda c: c.get_race_predictions()),
    ):
        try:
            out[key] = compact(syncer._call(fn))
        except Exception as e:  # noqa: BLE001
            out[f"{key}_error"] = str(e)
    preds = out.get("race_predictions")
    if isinstance(preds, dict):
        for k in ("time5K", "time10K", "timeHalfMarathon", "timeMarathon"):
            if isinstance(preds.get(k), (int, float)):
                s = int(preds[k])
                preds[k] = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return out


def training_plans(syncer, today: date) -> dict:
    out: dict[str, Any] = {}
    try:
        active = syncer._call(lambda c: c.get_active_training_plans(today.isoformat())) or {}
        plans = active.get("trainingPlanList") if isinstance(active, dict) else active
        out["active_plans"] = compact(plans or [])
    except Exception as e:  # noqa: BLE001
        out["active_plans_error"] = str(e)
        plans = []
    details = []
    for p in plans or []:
        pid = p.get("trainingPlanId") or p.get("id")
        if not pid:
            continue
        cat = (p.get("trainingPlanCategory") or p.get("planCategory") or "").upper()
        fn = (lambda c, pid=pid: c.get_adaptive_training_plan_by_id(pid)) if "ADAPT" in cat or "FBT" in cat \
            else (lambda c, pid=pid: c.get_training_plan_by_id(pid))
        try:
            details.append(compact(syncer._call(fn)))
        except Exception:  # noqa: BLE001
            try:
                details.append(compact(syncer._call(lambda c, pid=pid: c.get_adaptive_training_plan_by_id(pid))))
            except Exception as e:  # noqa: BLE001
                details.append({"plan_id": pid, "error": str(e)})
    if details:
        out["plan_details"] = details
    return out


def planned_workout(syncer, workout_id: int | None, scheduled_id: int | None) -> dict:
    if scheduled_id:
        s = syncer._call(lambda c: c.get_scheduled_workout_by_id(scheduled_id)) or {}
        w = s.get("workout") if isinstance(s, dict) else None
        if w and w.get("workoutSegments"):
            return {"date": s.get("date"), **summarize_workout(w)}
        workout_id = workout_id or (w or {}).get("workoutId")
    if not workout_id:
        return {"error": "Give workout_id or scheduled_workout_id (from get_training_calendar)."}
    return summarize_workout(syncer._call(lambda c: c.get_workout_by_id(workout_id)))
