"""Build Garmin strength workouts (from Hevy or from a plain description) and schedule them."""

from __future__ import annotations

import difflib
import json
import os
import re
from typing import Any

import requests
from garminconnect import exercises as gx
from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    StepType,
    StrengthWorkout,
    TargetType,
    WorkoutSegment,
    create_repeat_group,
    create_strength_exercise_step,
    create_strength_rest_step,
    create_strength_timed_step,
)

HEVY_API = "https://api.hevyapp.com/v1"
_BY_LOWER = {e["name"].lower(): e for e in gx.EXERCISES}
_NAMES = list(_BY_LOWER)
_CATEGORIES = sorted({e["category"] for e in gx.EXERCISES})


# ---------------------------------------------------------------------------
# Exercise matching: "Bench Press (Barbell)" → Garmin BENCH_PRESS / BARBELL_BENCH_PRESS
# ---------------------------------------------------------------------------
# Common gym names Garmin spells differently (first one that exists in the catalogue wins).
ALIASES: dict[str, list[str]] = {
    "triceps pushdown": ["Triceps Press-down"], "tricep pushdown": ["Triceps Press-down"],
    "triceps rope pushdown": ["Rope Press-down"], "rope pushdown": ["Rope Press-down"],
    "squat (barbell)": ["Barbell Back Squat"], "back squat": ["Barbell Back Squat"],
    "front squat (barbell)": ["Barbell Front Squat", "Front Squat"],
    "skullcrusher": ["Lying EZ-Bar Triceps Extension", "Dumbbell Lying Triceps Extension",
                     ],
    "skull crusher": ["Lying EZ-Bar Triceps Extension", "Dumbbell Lying Triceps Extension",
                      ],
    "triceps dip": ["Body-weight Dip"], "tricep dip": ["Body-weight Dip"], "dip": ["Body-weight Dip"],
    "chest dip": ["Body-weight Dip"], "chin up": ["Chin-up"], "pull up": ["Pull-up"],
    "push up": ["Push-up"], "hip thrust (barbell)": ["Barbell Hip Thrust with Bench", "Barbell Hip Thrust on Floor"],
    "copenhagen plank": ["Side Plank"], "ab wheel": ["Ab Wheel Rollout", "Kneeling Ab Wheel"],
}


def _alias(name: str) -> dict | None:
    key = name.strip().lower()
    base = re.sub(r"\s*\(.*?\)\s*$", "", key)
    for k in (key, base):
        for cand in ALIASES.get(k, []):
            e = _BY_LOWER.get(cand.lower())
            if e:
                return e
    return None


def _variants(name: str) -> list[str]:
    n = name.strip()
    out = [n]
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", n)  # Hevy style: "Squat (Barbell)"
    if m:
        base, equip = m.group(1).strip(), m.group(2).strip()
        out += [f"{equip} {base}", base]
    out += [re.sub(r"[^\w\s-]", " ", v) for v in list(out)]
    more = []
    for v in out:
        more += [v.rstrip("s"), v + "s", v.replace("-", " "), v.replace("Dumbbells", "Dumbbell")]
    return [re.sub(r"\s+", " ", v).strip() for v in out + more if v.strip()]


def _tokens(s: str) -> set[str]:
    return {t.rstrip("s") for t in re.findall(r"[a-z0-9]+", s.lower())}


def match_exercise(name: str, overrides: dict[str, str] | None = None) -> dict:
    """Return {garmin_name, category, exercise, confidence}. Confidence: saved | exact |
    close | category | unmatched."""
    overrides = overrides or {}
    key = name.strip().lower()
    if key in overrides:
        e = resolve_garmin(overrides[key])
        if e:
            return {**e, "confidence": "saved"}
    e = _alias(name)
    if e:
        return {"garmin_name": e["name"], "category": e["category"],
                "exercise": e["exercise"], "confidence": "exact"}
    for v in _variants(name):
        if v.lower() in _BY_LOWER:
            e = _BY_LOWER[v.lower()]
            return {"garmin_name": e["name"], "category": e["category"],
                    "exercise": e["exercise"], "confidence": "exact"}
    # Fuzzy: best of string similarity and token overlap.
    want = _tokens(" ".join(_variants(name)[:2]))
    best, best_score = None, 0.0
    for v in _variants(name)[:3]:
        for cand in difflib.get_close_matches(v.lower(), _NAMES, n=5, cutoff=0.6):
            ct = _tokens(cand)
            score = difflib.SequenceMatcher(None, v.lower(), cand).ratio() * 0.6 \
                + (len(want & ct) / max(len(want | ct), 1)) * 0.4
            if score > best_score:
                best, best_score = cand, score
    if best and best_score >= 0.62:
        e = _BY_LOWER[best]
        return {"garmin_name": e["name"], "category": e["category"],
                "exercise": e["exercise"], "confidence": "close"}
    # Category only (watch shows e.g. "Row"), keep the original name as a note.
    for cat in sorted(_CATEGORIES, key=len, reverse=True):
        if _tokens(cat.replace("_", " ")) <= want:
            return {"garmin_name": cat.replace("_", " ").title(), "category": cat,
                    "exercise": "", "confidence": "category"}
    return {"garmin_name": None, "category": None, "exercise": None, "confidence": "unmatched"}


def resolve_garmin(spec: str) -> dict | None:
    """Accept a Garmin display name ('Barbell Bench Press') or 'CATEGORY/EXERCISE'."""
    if "/" in spec:
        cat, _, ex = spec.partition("/")
        cat, ex = cat.strip().upper(), ex.strip().upper()
        for e in gx.EXERCISES:
            if e["category"] == cat and (e["exercise"] == ex or not ex):
                return {"garmin_name": e["name"], "category": cat, "exercise": ex}
        return None
    e = _BY_LOWER.get(spec.strip().lower())
    return {"garmin_name": e["name"], "category": e["category"], "exercise": e["exercise"]} if e else None


def search_exercises(query: str, limit: int = 25) -> list[dict]:
    q = _tokens(query)
    hits = [e for e in gx.EXERCISES if q <= _tokens(e["name"] + " " + e["category"].replace("_", " "))]
    if not hits:
        hits = [_BY_LOWER[n] for n in difflib.get_close_matches(query.lower(), _NAMES, n=limit, cutoff=0.4)]
    return [{"name": e["name"], "id": f"{e['category']}/{e['exercise']}"} for e in hits[:limit]]


# ---------------------------------------------------------------------------
# Generic workout description → Garmin steps
# ---------------------------------------------------------------------------
def _lap_rest(order: int) -> ExecutableStep:
    return ExecutableStep(
        stepOrder=order,
        stepType={"stepTypeId": StepType.REST, "stepTypeKey": "rest", "displayOrder": 5},
        endCondition={"conditionTypeId": ConditionType.LAP_BUTTON, "conditionTypeKey": "lap.button",
                      "displayOrder": 1, "displayable": True},
        targetType={"workoutTargetTypeId": TargetType.NO_TARGET, "workoutTargetTypeKey": "no.target",
                    "displayOrder": 1},
    )


def _expand_sets(ex: dict) -> list[dict]:
    sets = ex.get("sets")
    if isinstance(sets, int) or sets is None:  # shorthand {"sets": 4, "reps": 8, "weight_kg": 60}
        n = sets or 1
        return [{k: ex.get(k) for k in ("reps", "weight_kg", "duration_seconds", "rest_seconds", "type")}
                for _ in range(n)]
    return list(sets)


def _set_key(s: dict, ex: dict) -> tuple:
    rest = s.get("rest_seconds", ex.get("rest_seconds"))
    w = s.get("weight_kg")
    return (s.get("reps"), round(w, 2) if isinstance(w, (int, float)) else None,
            s.get("duration_seconds"), rest, (s.get("type") or "normal").lower())


class Builder:
    def __init__(self, overrides: dict[str, str]):
        self.order = 0
        self.overrides = overrides
        self.report: list[dict] = []
        self.est_secs = 0.0

    def _next(self) -> int:
        self.order += 1
        return self.order

    def _exercise_step(self, ex: dict, m: dict, s: dict) -> ExecutableStep:
        notes = []
        if (s.get("type") or "normal").lower() not in ("normal", ""):
            notes.append(f"{s['type']} set")
        if m["confidence"] in ("category", "unmatched") or m.get("exercise") == "":
            notes.append(ex["name"])
        if ex.get("notes"):
            notes.append(ex["notes"])
        if s.get("reps_note"):
            notes.append(s["reps_note"])
        desc = " · ".join(notes)[:250] or None
        cat = m["category"] or "TOTAL_BODY"
        w = s.get("weight_kg") if isinstance(s.get("weight_kg"), (int, float)) and s["weight_kg"] > 0 else None
        if s.get("duration_seconds") and not s.get("reps"):
            self.est_secs += s["duration_seconds"]
            return create_strength_timed_step(cat, self._next(), float(s["duration_seconds"]),
                                              exercise_name=m["exercise"] or "", weight_kg=w, description=desc)
        reps = int(s.get("reps") or 0)
        if reps <= 0:  # unknown reps → lap button
            step = create_strength_exercise_step(cat, self._next(), 1, exercise_name=m["exercise"] or "",
                                                 weight_kg=w, description=desc)
            step.endCondition = {"conditionTypeId": ConditionType.LAP_BUTTON,
                                 "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
            step.endConditionValue = None
            self.est_secs += 45
            return step
        self.est_secs += reps * 3.5
        return create_strength_exercise_step(cat, self._next(), reps, exercise_name=m["exercise"] or "",
                                             weight_kg=w, description=desc)

    def _rest_step(self, seconds: Any) -> ExecutableStep:
        if isinstance(seconds, (int, float)) and seconds > 0:
            self.est_secs += seconds
            return create_strength_rest_step(float(seconds), self._next())
        self.est_secs += 60
        return _lap_rest(self._next())

    def _match(self, ex: dict) -> dict:
        if ex.get("garmin_exercise"):
            r = resolve_garmin(ex["garmin_exercise"])
            m = {**r, "confidence": "given"} if r else match_exercise(ex["name"], self.overrides)
        else:
            m = match_exercise(ex["name"], self.overrides)
        self.report.append({"source": ex["name"], **m})
        return m

    def exercise_blocks(self, ex: dict) -> list:
        """Consecutive identical sets → one repeat block ("4 sets"); differing sets → single steps."""
        m = self._match(ex)
        sets = _expand_sets(ex)
        steps: list = []
        i = 0
        while i < len(sets):
            j = i
            while j + 1 < len(sets) and _set_key(sets[j + 1], ex) == _set_key(sets[i], ex):
                j += 1
            s = sets[i]
            rest = s.get("rest_seconds", ex.get("rest_seconds"))
            n = j - i + 1
            if n > 1:
                group_order = self._next()
                inner = [self._exercise_step(ex, m, s), self._rest_step(rest)]
                # Duration estimate counted once above; add the remaining repeats.
                self.est_secs += (n - 1) * ((s.get("duration_seconds") or (s.get("reps") or 10) * 3.5)
                                            + (rest if isinstance(rest, (int, float)) and rest else 60))
                steps.append(create_repeat_group(n, inner, group_order))
            else:
                steps += [self._exercise_step(ex, m, s), self._rest_step(rest)]
            i = j + 1
        return steps

    def superset_block(self, exs: list[dict]) -> list:
        """A1/B1 rest, A2/B2 rest… If set schemes differ, fall back to one exercise after another."""
        per = [_expand_sets(e) for e in exs]
        uniform = len({len(p) for p in per}) == 1 and all(
            len({_set_key(s, e) for s in p}) == 1 for p, e in zip(per, exs))
        if not uniform:
            out: list = []
            for e in exs:
                out += self.exercise_blocks(e)
            return out
        n = len(per[0])
        group_order = self._next()
        before = self.est_secs
        inner = []
        for e, p in zip(exs, per):
            inner.append(self._exercise_step(e, self._match(e), p[0]))
        inner.append(self._rest_step(exs[-1].get("rest_seconds") or per[-1][0].get("rest_seconds")))
        self.est_secs += (n - 1) * (self.est_secs - before)  # remaining rounds
        return [create_repeat_group(n, inner, group_order)]


def build_workout(name: str, exercises: list[dict], description: str | None,
                  overrides: dict[str, str]) -> tuple[StrengthWorkout, list[dict], int]:
    b = Builder(overrides)
    steps: list = []
    i = 0
    while i < len(exercises):
        ex = exercises[i]
        sid = ex.get("superset")
        if sid is not None and sid != "" and sid is not False:
            group = [ex]
            while i + 1 < len(exercises) and exercises[i + 1].get("superset") == sid:
                i += 1
                group.append(exercises[i])
            steps += b.superset_block(group) if len(group) > 1 else b.exercise_blocks(ex)
        else:
            steps += b.exercise_blocks(ex)
        i += 1
    est = int(b.est_secs)
    w = StrengthWorkout(
        workoutName=name[:80],
        description=(description or "")[:500] or None,
        estimatedDurationInSecs=est,
        workoutSegments=[WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
            workoutSteps=steps)],
    )
    return w, b.report, est


def preview_lines(exercises: list[dict], report: list[dict]) -> list[str]:
    by_src: dict[str, dict] = {}
    for r in report:
        by_src.setdefault(r["source"], r)
    lines = []
    for ex in exercises:
        sets = _expand_sets(ex)
        desc = ", ".join(
            (f"{s.get('reps') or '?'}" if not s.get("duration_seconds") or s.get("reps")
             else f"{s['duration_seconds']}s")
            + (f"×{s['weight_kg']:g}kg" if isinstance(s.get("weight_kg"), (int, float)) and s["weight_kg"] else "")
            + (" (warm-up)" if (s.get("type") or "").lower() == "warmup" else "")
            for s in sets)
        m = by_src.get(ex["name"], {})
        tag = m.get("garmin_name") or "⚠ NOT MATCHED"
        conf = m.get("confidence")
        lines.append(f"{ex['name']} → {tag} [{conf}]: {len(sets)} sets: {desc}"
                     + (f" · rest {ex.get('rest_seconds')}s" if ex.get("rest_seconds") else "")
                     + (f" · superset {ex.get('superset')}" if ex.get("superset") is not None and ex.get("superset") != "" else ""))
    return lines


# ---------------------------------------------------------------------------
# Hevy
# ---------------------------------------------------------------------------
def _hevy(path: str, params: dict | None = None) -> Any:
    key = os.getenv("HEVY_API_KEY")
    if not key:
        raise RuntimeError("HEVY_API_KEY is not set on the server (Hevy Pro → Settings → Developer).")
    r = requests.get(f"{HEVY_API}{path}", headers={"api-key": key, "accept": "application/json"},
                     params=params, timeout=20)
    if r.status_code == 401:
        raise RuntimeError("Hevy rejected the API key (needs Hevy Pro; check the key).")
    r.raise_for_status()
    return r.json()


def _hevy_summary(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "title": r.get("title"),
        "date": (r.get("start_time") or r.get("updated_at") or "")[:10] or None,
        "exercises": [f"{e.get('title')} ({len(e.get('sets') or [])} sets)" for e in r.get("exercises") or []],
    }


def hevy_list(kind: str, page: int) -> dict:
    data = _hevy(f"/{kind}", {"page": page, "pageSize": 10})
    items = data.get(kind) or []
    return {"page": data.get("page", page), "page_count": data.get("page_count"),
            kind: [_hevy_summary(x) for x in items]}


def hevy_get(kind: str, item_id: str) -> dict:
    data = _hevy(f"/{kind}/{item_id}")
    return data.get(kind[:-1]) or data  # {"routine": {...}} / {"workout": {...}}


def hevy_to_exercises(h: dict) -> list[dict]:
    out = []
    for e in h.get("exercises") or []:
        sets = []
        for s in e.get("sets") or []:
            reps = s.get("reps")
            rr = s.get("rep_range") or {}
            note = None
            if not reps and rr.get("start"):
                reps = rr.get("end") or rr.get("start")
                note = f"{rr.get('start')}-{rr.get('end')} reps" if rr.get("end") else None
            sets.append({"reps": reps, "weight_kg": s.get("weight_kg"),
                         "duration_seconds": s.get("duration_seconds"),
                         "type": s.get("type") or "normal", "reps_note": note})
        out.append({"name": e.get("title") or "Exercise", "notes": e.get("notes") or None,
                    "rest_seconds": e.get("rest_seconds"), "superset": e.get("superset_id"),
                    "sets": sets})
    return out


def load_overrides(store) -> dict[str, str]:
    try:
        return json.loads(store.get_meta("exercise_map") or "{}")
    except ValueError:
        return {}
