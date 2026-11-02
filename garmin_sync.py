"""Garmin Connect login, fetching, compact extraction and syncing into the Store."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from store import Store

log = logging.getLogger("garmin")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def g(d: Any, *path: Any, default: Any = None) -> Any:
    """Safe nested get: g(x, 'a', 0, 'b')."""
    cur = d
    for p in path:
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            return default
        if cur is None:
            return default
    return cur


def clean(d: dict) -> dict:
    """Drop None values so the output Claude reads stays compact."""
    return {k: v for k, v in d.items() if v is not None and v != {} and v != []}


def mins(seconds: Any) -> float | None:
    return round(seconds / 60, 1) if isinstance(seconds, (int, float)) else None


def hours(seconds: Any) -> float | None:
    return round(seconds / 3600, 2) if isinstance(seconds, (int, float)) else None


def pace_min_per_km(speed_mps: Any) -> str | None:
    if not isinstance(speed_mps, (int, float)) or speed_mps <= 0:
        return None
    secs = 1000 / speed_mps
    return f"{int(secs // 60)}:{int(round(secs % 60)):02d}/km"


def first_device_value(d: Any) -> Any:
    """Garmin often nests per-device data as {deviceId: {...}}; take the primary one."""
    if isinstance(d, dict) and d:
        for v in d.values():
            if isinstance(v, dict) and v.get("primaryTrainingDevice", True):
                return v
        return next(iter(d.values()))
    return None


def trim(obj: Any, max_list: int = 40, depth: int = 0) -> Any:
    """Shrink raw Garmin JSON (long time series) so it fits in a chat."""
    if depth > 8:
        return "…"
    if isinstance(obj, dict):
        return {k: trim(v, max_list, depth + 1) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        if len(obj) > max_list:
            return f"[{len(obj)} items omitted – time series]"
        return [trim(v, max_list, depth + 1) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Extraction: raw Garmin JSON -> compact per-day summary
# ---------------------------------------------------------------------------
def extract_day(raw: dict[str, Any]) -> dict:
    stats = raw.get("stats") or {}
    sleep = raw.get("sleep") or {}
    hrv = raw.get("hrv") or {}
    readiness = raw.get("readiness") or []
    status = raw.get("training_status") or {}
    weigh = raw.get("weight") or {}

    s = g(sleep, "dailySleepDTO", default={})
    sleep_out = clean({
        "total_h": hours(s.get("sleepTimeSeconds")),
        "deep_min": mins(s.get("deepSleepSeconds")),
        "light_min": mins(s.get("lightSleepSeconds")),
        "rem_min": mins(s.get("remSleepSeconds")),
        "awake_min": mins(s.get("awakeSleepSeconds")),
        "score": g(s, "sleepScores", "overall", "value"),
        "quality": g(s, "sleepScores", "overall", "qualifierKey"),
        "avg_respiration": s.get("averageRespirationValue"),
        "avg_sleep_stress": s.get("avgSleepStress"),
        "avg_overnight_hrv": sleep.get("avgOvernightHrv"),
        "body_battery_change": sleep.get("bodyBatteryChange"),
        "feedback": g(s, "sleepScoreFeedback"),
    })

    hs = hrv.get("hrvSummary") or {}
    hrv_out = clean({
        "last_night_avg_ms": hs.get("lastNightAvg"),
        "last_night_5min_high_ms": hs.get("lastNight5MinHigh"),
        "weekly_avg_ms": hs.get("weeklyAvg"),
        "status": hs.get("status"),
        "baseline_low_ms": g(hs, "baseline", "balancedLow"),
        "baseline_high_ms": g(hs, "baseline", "balancedUpper"),
        "feedback": hs.get("feedbackPhrase"),
    })

    tr = None
    if isinstance(readiness, list) and readiness:
        # Prefer the morning ("after wake-up") reading, else the latest.
        tr = next((r for r in readiness if r.get("inputContext") == "AFTER_WAKEUP_RESET"), readiness[0])
    elif isinstance(readiness, dict):
        tr = readiness
    readiness_out = clean({
        "score": g(tr, "score"),
        "level": g(tr, "level"),
        "feedback": g(tr, "feedbackShort"),
        "recovery_time_h": round(tr["recoveryTime"] / 60, 1) if isinstance(g(tr, "recoveryTime"), (int, float)) else None,
        "sleep_factor_pct": g(tr, "sleepScoreFactorPercent"),
        "hrv_factor_pct": g(tr, "hrvFactorPercent"),
        "recovery_factor_pct": g(tr, "recoveryTimeFactorPercent"),
        "acute_load_factor_pct": g(tr, "acwrFactorPercent"),
        "stress_history_factor_pct": g(tr, "stressHistoryFactorPercent"),
    }) if tr else {}

    ts = first_device_value(g(status, "mostRecentTrainingStatus", "latestTrainingStatusData")) or {}
    load = ts.get("acuteTrainingLoadDTO") or {}
    bal = first_device_value(g(status, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap")) or {}
    training_out = clean({
        "status": ts.get("trainingStatusFeedbackPhrase") or ts.get("trainingStatus"),
        "acute_load": load.get("dailyTrainingLoadAcute"),
        "chronic_load": load.get("dailyTrainingLoadChronic"),
        "acwr_ratio": load.get("dailyAcuteChronicWorkloadRatio"),
        "acwr_status": load.get("acwrStatus"),
        "vo2max_running": g(status, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue")
        or g(status, "mostRecentVO2Max", "generic", "vo2MaxValue"),
        "vo2max_cycling": g(status, "mostRecentVO2Max", "cycling", "vo2MaxPreciseValue"),
        "load_balance": bal.get("trainingBalanceFeedbackPhrase"),
        "monthly_load_aerobic_low": bal.get("monthlyLoadAerobicLow"),
        "monthly_load_aerobic_high": bal.get("monthlyLoadAerobicHigh"),
        "monthly_load_anaerobic": bal.get("monthlyLoadAnaerobic"),
    })

    weight_g = g(weigh, "totalAverage", "weight") or g(weigh, "dateWeightList", 0, "weight")
    body_out = clean({
        "weight_kg": round(weight_g / 1000, 2) if isinstance(weight_g, (int, float)) else None,
        "body_fat_pct": g(weigh, "totalAverage", "bodyFat"),
    })

    daily_out = clean({
        "steps": stats.get("totalSteps"),
        "step_goal": stats.get("dailyStepGoal"),
        "resting_hr": stats.get("restingHeartRate") or sleep.get("restingHeartRate"),
        "min_hr": stats.get("minHeartRate"),
        "max_hr": stats.get("maxHeartRate"),
        "avg_stress": stats.get("averageStressLevel"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "body_battery_now": stats.get("bodyBatteryMostRecentValue"),
        "body_battery_at_wake": stats.get("bodyBatteryAtWakeTime"),
        "body_battery_charged": stats.get("bodyBatteryChargedValue"),
        "body_battery_drained": stats.get("bodyBatteryDrainedValue"),
        "calories_total": stats.get("totalKilocalories"),
        "calories_active": stats.get("activeKilocalories"),
        "intensity_min_moderate": stats.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": stats.get("vigorousIntensityMinutes"),
        "floors": round(stats["floorsAscended"]) if isinstance(stats.get("floorsAscended"), (int, float)) else None,
        "avg_spo2": stats.get("averageSpo2"),
        "lowest_spo2": stats.get("lowestSpo2"),
        "avg_waking_respiration": stats.get("avgWakingRespirationValue"),
    })

    return clean({
        "daily": daily_out,
        "sleep": sleep_out,
        "hrv": hrv_out,
        "training_readiness": readiness_out,
        "training": training_out,
        "body": body_out,
    })


def extract_activity(a: dict) -> dict:
    zones = {f"z{i}_min": mins(a.get(f"hrTimeInZone_{i}")) for i in range(1, 6)}
    return clean({
        "id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": g(a, "activityType", "typeKey"),
        "start_local": a.get("startTimeLocal"),
        "duration_min": mins(a.get("duration")),
        "moving_min": mins(a.get("movingDuration")),
        "distance_km": round(a["distance"] / 1000, 2) if isinstance(a.get("distance"), (int, float)) else None,
        "avg_pace": pace_min_per_km(a.get("averageSpeed")),
        "avg_speed_kmh": round(a["averageSpeed"] * 3.6, 1) if isinstance(a.get("averageSpeed"), (int, float)) else None,
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "hr_zones": clean(zones),
        "calories": a.get("calories"),
        "elevation_gain_m": a.get("elevationGain"),
        "avg_cadence": a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageBikingCadenceInRevPerMinute"),
        "avg_power_w": a.get("avgPower"),
        "normalized_power_w": a.get("normPower"),
        "aerobic_te": a.get("aerobicTrainingEffect"),
        "anaerobic_te": a.get("anaerobicTrainingEffect"),
        "te_label": a.get("trainingEffectLabel"),
        "training_load": round(a["activityTrainingLoad"], 1) if isinstance(a.get("activityTrainingLoad"), (int, float)) else None,
        "vo2max": a.get("vO2MaxValue"),
        "sets": a.get("activeSets") or a.get("totalSets"),
        "reps": a.get("totalReps"),
        "description": a.get("description"),
    })


def extract_laps(splits: Any) -> list[dict]:
    laps = g(splits, "lapDTOs", default=[]) or []
    out = []
    for i, lap in enumerate(laps, 1):
        out.append(clean({
            "lap": i,
            "distance_km": round(lap["distance"] / 1000, 2) if isinstance(lap.get("distance"), (int, float)) else None,
            "duration_min": mins(lap.get("duration")),
            "pace": pace_min_per_km(lap.get("averageSpeed")),
            "avg_hr": lap.get("averageHR"),
            "max_hr": lap.get("maxHR"),
            "elevation_gain_m": lap.get("elevationGain"),
            "avg_cadence": lap.get("averageRunCadence"),
            "avg_power_w": lap.get("averagePower"),
            "intensity": lap.get("intensityType"),
        }))
    return out


# ---------------------------------------------------------------------------
# Syncer
# ---------------------------------------------------------------------------
class GarminSyncer:
    DAILY_FETCHERS: dict[str, Callable[[Garmin, str], Any]] = {
        "stats": lambda c, d: c.get_stats(d),
        "sleep": lambda c, d: c.get_sleep_data(d),
        "hrv": lambda c, d: c.get_hrv_data(d),
        "readiness": lambda c, d: c.get_training_readiness(d),
        "training_status": lambda c, d: c.get_training_status(d),
        "weight": lambda c, d: c.get_daily_weigh_ins(d),
    }

    def __init__(self, store: Store, data_dir: Path, tz: str):
        self.store = store
        self.token_path = str(data_dir / "garmin_tokens.json")
        self.tz = ZoneInfo(tz)
        self._client: Garmin | None = None
        self._lock = threading.RLock()  # one Garmin call at a time
        self.last_error: str | None = None
        self.call_delay = float(os.getenv("GARMIN_CALL_DELAY", "0.4"))

    # -- auth ------------------------------------------------------------
    def today(self) -> date:
        return datetime.now(self.tz).date()

    def client(self) -> Garmin:
        with self._lock:
            if self._client is not None:
                return self._client
            c = Garmin(
                email=os.getenv("GARMIN_EMAIL"),
                password=os.getenv("GARMIN_PASSWORD"),
            )
            if Path(self.token_path).exists():
                c.login(self.token_path)
            elif os.getenv("GARMIN_TOKENS"):
                # First boot: tokens pasted as an env var (from login.py).
                c.login(os.environ["GARMIN_TOKENS"])
                c.client.dump(self.token_path)
            else:
                # Credentials only (works if your account has no 2FA).
                c.login(self.token_path)
            self._client = c
            log.info("Logged in to Garmin Connect")
            return c

    def _save_tokens(self) -> None:
        if self._client is not None:
            try:
                self._client.client.dump(self.token_path)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not persist tokens: %s", e)

    def _call(self, fn: Callable[[Garmin], Any]) -> Any:
        with self._lock:
            c = self.client()
            for attempt in range(3):
                try:
                    out = fn(c)
                    time.sleep(self.call_delay)
                    return out
                except GarminConnectTooManyRequestsError:
                    wait = 30 * (attempt + 1)
                    log.warning("Garmin rate limit hit, waiting %ss", wait)
                    time.sleep(wait)
                except GarminConnectAuthenticationError:
                    self._client = None
                    raise
            raise GarminConnectTooManyRequestsError("Rate limited by Garmin, try again later")

    # -- syncing ---------------------------------------------------------
    def sync_day(self, day: date) -> dict:
        ds = day.isoformat()
        raw: dict[str, Any] = {}
        for kind, fetch in self.DAILY_FETCHERS.items():
            try:
                raw[kind] = self._call(lambda c, f=fetch: f(c, ds))
                self.store.put_raw(ds, kind, raw[kind])
            except GarminConnectAuthenticationError:
                raise
            except Exception as e:  # noqa: BLE001 - one missing metric shouldn't stop the day
                log.info("%s %s unavailable: %s", ds, kind, e)
        summary = extract_day(raw)
        self.store.put_summary(ds, summary)
        return summary

    def sync_activities(self, since: date) -> int:
        acts = self._call(lambda c: c.get_activities_by_date(since.isoformat(), self.today().isoformat())) or []
        for a in acts:
            s = extract_activity(a)
            if s.get("id") is None:
                continue
            self.store.put_activity(s["id"], s.get("start_local", ""), s.get("type", ""), s, a)
        return len(acts)

    def fetch_activity_details(self, act_id: int) -> dict:
        splits = hr = None
        try:
            splits = self._call(lambda c: c.get_activity_splits(str(act_id)))
        except Exception as e:  # noqa: BLE001
            log.info("splits %s unavailable: %s", act_id, e)
        try:
            hr = self._call(lambda c: c.get_activity_hr_in_timezones(str(act_id)))
        except Exception as e:  # noqa: BLE001
            log.info("hr zones %s unavailable: %s", act_id, e)
        details = clean({
            "laps": extract_laps(splits),
            "hr_zones": [
                clean({"zone": z.get("zoneNumber"), "min_bpm": z.get("zoneLowBoundary"),
                       "minutes": mins(z.get("secsInZone"))})
                for z in (hr or []) if isinstance(z, dict)
            ],
        })
        self.store.put_activity_details(act_id, details)
        return details

    def sync_recent(self, days: int = 2, detail_limit: int = 10) -> dict:
        """Refresh the last `days` days + new activities. Used by the scheduler and sync_now."""
        with self._lock:
            today = self.today()
            for i in range(days):
                self.sync_day(today - timedelta(days=i))
            latest = self.store.latest_activity_start()
            since = today - timedelta(days=days)
            if latest:
                since = min(since, datetime.fromisoformat(latest[:10]).date())
            n = self.sync_activities(since)
            for act_id in self.store.activity_ids_without_details(detail_limit):
                self.fetch_activity_details(act_id)
            self._save_tokens()
            self.store.set_meta("last_sync_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self.last_error = None
            return {"days_refreshed": days, "activities_seen": n}

    def backfill(self, days: int) -> None:
        """Fill history for days we don't have yet (oldest last, so recent data lands first)."""
        today = self.today()
        for i in range(days):
            day = today - timedelta(days=i)
            if i >= 2 and self.store.has_day(day.isoformat()):
                continue
            self.sync_day(day)
        self.sync_activities(today - timedelta(days=days))
        self._save_tokens()
        self.store.set_meta("backfill_done_days", str(days))

    def run_forever(self, interval_min: int, backfill_days: int) -> None:
        """Background loop: initial backfill, then refresh every `interval_min` minutes."""
        try:
            self.sync_recent()
            if int(self.store.get_meta("backfill_done_days") or 0) < backfill_days:
                log.info("Backfilling %s days of history…", backfill_days)
                self.backfill(backfill_days)
                log.info("Backfill complete")
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("Initial sync failed")
        while True:
            time.sleep(interval_min * 60)
            try:
                self.sync_recent()
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Scheduled sync failed")
