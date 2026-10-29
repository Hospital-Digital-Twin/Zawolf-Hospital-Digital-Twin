"""Read-only analytics behind the dashboard pages. Every number is computed from the CSV data pack
(or from the agents' own output); nothing is estimated or hard-coded. Each payload says which
table it came from and which period it covers so the UI can print the provenance.

Observed days are always the complete days *before* the replay moment.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .twin_data import STAFF_ROLES, TwinData, _dt

HISTORY_DAYS = 14


def _days(as_of: datetime, n: int) -> list[str]:
    return [(as_of.date() - timedelta(days=i)).isoformat() for i in range(n, 0, -1)]


def _num(v):
    return float(v) if v not in (None, "") else None


def _period(days: list[str]) -> dict:
    return {"from": days[0], "to": days[-1]} if days else {"from": None, "to": None}


# ───────────────────────── forecasts ─────────────────────────

# key -> (department type, daily_metrics column, scale, unit, title)
FORECASTS = {
    "er_arrivals": ("ER", "arrivals_count", 1, "patients/day", "ER arrivals"),
    "er_wait": ("ER", "avg_wait_minutes", 1, "minutes", "ER average wait"),
    "icu_occ": ("ICU", "occupancy_rate", 100, "% of beds", "ICU occupancy"),
    "ward_occ": ("WARD", "occupancy_rate", 100, "% of beds", "Ward occupancy"),
    "or_util": ("OR", "or_utilization_rate", 100, "% of room time", "OR utilisation"),
}


def weekday_forecast(td: TwinData, as_of: datetime, dep_type: str, column: str, scale: float, days: int = 7) -> list[dict]:
    """Weekday-mean baseline (the pack's own method) from history strictly before the replay day,
    plus the actual value when the data contains it. Band = mean +/- 1 sample std (descriptive)."""
    dep = td.dept_by_type[dep_type]
    by_wd = defaultdict(list)
    for day, rows in td.metrics.items():
        v = _num(rows[dep][column])
        if day < as_of.date().isoformat() and v is not None:
            by_wd[_dt(day).weekday()].append(v * scale)
    out = []
    for i in range(0, days):
        d = as_of.date() + timedelta(days=i)
        vals = by_wd.get(d.weekday(), [])
        if not vals:
            continue
        mean = statistics.fmean(vals)
        std = statistics.stdev(vals) if len(vals) > 1 else 0.0
        actual = _num(td.metrics.get(d.isoformat(), {}).get(dep, {}).get(column))
        out.append({"date": d.isoformat(), "predicted": round(mean, 3), "lower": round(max(0.0, mean - std), 3),
                    "upper": round(mean + std, 3), "actual": None if actual is None else round(actual * scale, 3)})
    return out


def _backtest(points: list[dict]) -> dict:
    rows = [p for p in points if p["actual"] is not None]
    total = sum(abs(p["actual"]) for p in rows)
    nz = [p for p in rows if p["actual"]]
    return {"days": len(rows),
            "wape": round(sum(abs(p["predicted"] - p["actual"]) for p in rows) / total, 3) if total else None,
            "mape": round(statistics.fmean(abs(p["predicted"] - p["actual"]) / p["actual"] for p in nz), 3) if nz else None}


BACKTEST_DAYS, BACKTEST_HORIZON = 28, 3


def rolling_backtest(td: TwinData, as_of: datetime, dep_type: str, column: str, scale: float) -> dict:
    """Fair comparison over many forecast origins: for each of the 28 days before the replay moment, forecast
    1-3 days ahead using only earlier data, and score against what happened (only days before the replay moment).
    Methods: weekday mean (what the dashboard uses), seasonal naive (same weekday last week), persistence (yesterday)."""
    dep = td.dept_by_type[dep_type]
    val = {d: v * scale for d, rows in td.metrics.items() if (v := _num(rows[dep][column])) is not None}
    stop = as_of.date()
    methods = {"weekday_mean": [], "seasonal_naive": [], "persistence": []}
    for k in range(BACKTEST_DAYS, 0, -1):
        origin = stop - timedelta(days=k)
        hist = {d: v for d, v in val.items() if d < origin.isoformat()}
        if len(hist) < 14:
            continue
        by_wd = defaultdict(list)
        for d, v in hist.items():
            by_wd[_dt(d).weekday()].append(v)
        last = hist.get((origin - timedelta(days=1)).isoformat())
        for h in range(BACKTEST_HORIZON):
            day = origin + timedelta(days=h)
            actual = val.get(day.isoformat())
            if actual is None or day >= stop or not by_wd.get(day.weekday()):
                continue
            methods["weekday_mean"].append((statistics.fmean(by_wd[day.weekday()]), actual))
            if (past := hist.get((day - timedelta(days=7)).isoformat())) is not None:
                methods["seasonal_naive"].append((past, actual))
            if last is not None:
                methods["persistence"].append((last, actual))
    out = {}
    for name, pairs in methods.items():
        total = sum(abs(a) for _, a in pairs)
        out[name] = {"n": len(pairs), "mae": round(statistics.fmean(abs(f - a) for f, a in pairs), 3) if pairs else None,
                     "wape": round(sum(abs(f - a) for f, a in pairs) / total, 3) if total else None,
                     "mean_actual": round(statistics.fmean(a for _, a in pairs), 3) if pairs else None}
    ref = out["seasonal_naive"]["mae"]
    for name, m in out.items():
        m["skill_vs_seasonal_naive"] = round(1 - m["mae"] / ref, 3) if m["mae"] is not None and ref else None
    best = min((n for n in out if out[n]["mae"] is not None), key=lambda n: out[n]["mae"], default=None)
    return {"window": {"from": (stop - timedelta(days=BACKTEST_DAYS)).isoformat(), "to": (stop - timedelta(days=1)).isoformat()},
            "horizon_days": BACKTEST_HORIZON, "methods": out, "best_by_mae": best}


def forecasts(td: TwinData, as_of: datetime) -> dict:
    hist = _days(as_of, HISTORY_DAYS)
    out = []
    for key, (dep_type, col, scale, unit, title) in FORECASTS.items():
        dep = td.dept_by_type[dep_type]
        points = weekday_forecast(td, as_of, dep_type, col, scale)
        out.append({
            "key": key, "title": title, "unit": unit, "table": "daily_metrics",
            "history": {"dates": hist, "values": [None if (v := _num(td.metrics.get(d, {}).get(dep, {}).get(col))) is None
                                                  else round(v * scale, 3) for d in hist]},
            "forecast": points, "backtest": _backtest(points), "rolling": rolling_backtest(td, as_of, dep_type, col, scale)})
    return {"as_of": as_of.isoformat(), "method": "weekday_mean_baseline", "calibrated": False, "series": out}


# ───────────────────────── capacity: ER / ICU / ward ─────────────────────────

def _unit_history(td: TwinData, unit: str, hist: list[str], col: str, scale: float = 1):
    dep = td.dept_by_type[unit]
    return [None if (v := _num(td.metrics.get(d, {}).get(dep, {}).get(col))) is None else round(v * scale, 2) for d in hist]


def capacity(td: TwinData, as_of: datetime) -> dict:
    hist = _days(as_of, HISTORY_DAYS)
    pools = td.pools(as_of)
    units = {}
    for unit in ("ER", "ICU", "WARD"):
        beds = pools[f"{unit}:beds"]
        nurses = pools.get(f"{unit}:nurses")
        occupied = beds.committed
        series = weekday_forecast(td, as_of, unit, "occupancy_rate", 100)
        units[unit] = {
            "beds_total": beds.total, "occupied_now": occupied,
            "occupancy_now_pct": round(100 * occupied / beds.total, 1) if beds.total else None,
            "nurses_on_shift": nurses.total if nurses else None,
            "beds_occupied_per_nurse": round(occupied / nurses.total, 2) if nurses and nurses.total else None,
            "occupancy_history_pct": _unit_history(td, unit, hist, "occupancy_rate", 100),
            "forecast_pct": series, "backtest": _backtest(series),
            "peak_forecast_upper_pct": max((p["upper"] for p in series), default=None)}
    # ER flow comes from the visits table
    since = (as_of.date() - timedelta(days=28)).isoformat()
    until = as_of.date().isoformat()
    visits = [v for v in td.t["visits"] if since <= v["arrival_time"][:10] < until]
    by_hour = Counter(_dt(v["arrival_time"]).hour for v in visits)
    n_days = max(1, (as_of.date() - _dt(max(since, td.first_day)).date()).days)
    dispositions = Counter(v["disposition"] for v in visits)
    acuity = Counter(v["acuity_level"] for v in visits)
    units["ER"]["flow"] = {
        "arrivals_history": _unit_history(td, "ER", hist, "arrivals_count"),
        "wait_avg_history": _unit_history(td, "ER", hist, "avg_wait_minutes"),
        "wait_p90_history": _unit_history(td, "ER", hist, "p90_wait_minutes"),
        "queue_peak_history": _unit_history(td, "ER", hist, "er_queue_peak"),
        "arrivals_by_hour": [round(by_hour.get(h, 0) / n_days, 2) for h in range(24)],
        "dispositions": dict(dispositions), "acuity": dict(sorted(acuity.items())),
        "left_without_being_seen_pct": round(100 * dispositions.get("LEFT_WITHOUT_BEING_SEEN", 0) / len(visits), 1) if visits else None,
        "visits_in_window": len(visits), "window": {"from": max(since, td.first_day), "to": (as_of.date() - timedelta(days=1)).isoformat()}}
    return {"as_of": as_of.isoformat(), "history_period": _period(hist), "units": units,
            "sources": ["daily_metrics", "beds", "bed_stays", "visits", "staff_shifts"]}


# ───────────────────────── staff & nursing ─────────────────────────

def staff(td: TwinData, as_of: datetime) -> dict:
    hist = _days(as_of, HISTORY_DAYS)
    pools = td.pools(as_of)
    on_shift = [{"unit": k.split(":")[0], "kind": k.split(":")[1], "count": p.total}
                for k, p in sorted(pools.items()) if k.split(":")[1] in STAFF_ROLES.values()]
    shifts_per_day = Counter(s[0].date().isoformat() for s in td.shifts)
    absent_per_day = Counter(s[0].date().isoformat() for s in td.shifts if s[3] == "ABSENT")
    # weekly workload = scheduled (non-absent) hours in the 7 days before the replay moment / contract maximum
    lo, hi = as_of - timedelta(days=7), as_of
    hours = defaultdict(float)
    for start, end, sid, status in td.shifts:
        if status != "ABSENT" and end > lo and start < hi:
            hours[sid] += (min(end, hi) - max(start, lo)).total_seconds() / 3600
    rows = []
    for s in td.t["staff"]:
        if s["is_active"] == "True" and s["role"] in ("NURSE", "DOCTOR", "SURGEON", "ANESTHETIST"):
            cap = float(s["max_hours_per_week"])
            rows.append((td.dept[s["department_id"]]["department_type"], s["role"], hours.get(s["staff_id"], 0.0) / cap * 100 if cap else 0))
    buckets = ["0-25", "25-50", "50-75", "75-100", ">100"]
    edges = [25, 50, 75, 100]
    hist_counts = Counter(buckets[sum(1 for e in edges if pct >= e)] for _, _, pct in rows)
    by_unit = defaultdict(list)
    for unit, role, pct in rows:
        by_unit[unit].append(pct)
    return {
        "as_of": as_of.isoformat(), "history_period": _period(hist), "on_shift_now": on_shift,
        "observed_beds_per_nurse": {u: capacity_ratio for u, capacity_ratio in
                                    ((u, capacity(td, as_of)["units"][u]["beds_occupied_per_nurse"]) for u in ("ER", "ICU", "WARD"))},
        "shifts_per_day": {"dates": hist, "values": [shifts_per_day.get(d, 0) for d in hist]},
        "absent_shifts_per_day": {"dates": hist, "values": [absent_per_day.get(d, 0) for d in hist]},
        "overtime_hours": {"dates": hist, "values": [round(sum(_num(r["overtime_hours"]) or 0 for r in td.metrics.get(d, {}).values()), 1) for d in hist]},
        "overtime_cost_egp": {"dates": hist, "values": [round(sum(_num(r["overtime_cost_egp"]) or 0 for r in td.finance.get(d, {}).values()), 0) for d in hist]},
        "workload_distribution": {"buckets": buckets, "staff": [hist_counts.get(b, 0) for b in buckets], "staff_total": len(rows)},
        "workload_by_unit": [{"unit": u, "mean_pct": round(statistics.fmean(v), 1), "staff": len(v),
                              "at_or_above_90_pct": sum(1 for x in v if x >= 90)} for u, v in sorted(by_unit.items())],
        "sources": ["staff", "staff_shifts", "daily_metrics", "financial_daily"]}


# ───────────────────────── operating rooms ─────────────────────────

def operating_rooms(td: TwinData, as_of: datetime) -> dict:
    hist = _days(as_of, HISTORY_DAYS)
    ors = [r for r in td.t["operating_rooms"] if r["is_active"] == "True"]
    cap = {r["or_id"]: float(r["daily_capacity_hours"]) * 60 for r in ors}
    done = [s for s in td.t["surgeries"] if hist[0] <= s["planned_start"][:10] <= hist[-1]]
    planned = defaultdict(float)
    for s in done:
        if s["status"] != "CANCELLED":
            planned[s["or_id"]] += float(s["planned_duration_minutes"])
    by_room = [{"or_id": r["or_id"], "name": r["room_name"], "specialty": r["specialty"],
                "planned_utilisation_pct": round(100 * planned[r["or_id"]] / (cap[r["or_id"]] * len(hist)), 1)} for r in ors]
    status = Counter(s["status"] for s in done)
    cancelled = [s for s in done if s["status"] == "CANCELLED"]
    # booked load per upcoming day (no calendar exists beyond the last day of the data)
    upcoming = defaultdict(lambda: {"minutes": 0.0, "bookings": 0, "elective": 0})
    for s in td.t["surgeries"]:
        start = _dt(s["planned_start"])
        if start > as_of and s["status"] != "CANCELLED":
            u = upcoming[start.date().isoformat()]
            u["minutes"] += float(s["planned_duration_minutes"])
            u["bookings"] += 1
            u["elective"] += s["urgency"] == "ELECTIVE"
    total_cap = sum(cap.values())
    days = sorted(upcoming)
    return {
        "as_of": as_of.isoformat(), "history_period": _period(hist), "rooms": len(ors),
        "utilisation_history_pct": [None if (v := _num(td.metrics.get(d, {}).get(td.dept_by_type["OR"], {}).get("or_utilization_rate"))) is None else round(v * 100, 1) for d in hist],
        "dates": hist, "by_room": by_room,
        "status_counts": dict(status), "surgeries_in_window": len(done),
        "delay_rate_pct": round(100 * status.get("DELAYED", 0) / len(done), 1) if done else None,
        "cancel_rate_pct": round(100 * status.get("CANCELLED", 0) / len(done), 1) if done else None,
        "cancelled_expected_margin_egp": round(sum(float(s["expected_contribution_margin_egp"] or 0) for s in cancelled), 0),
        "upcoming": {"dates": days, "load_pct": [round(100 * upcoming[d]["minutes"] / total_cap, 1) for d in days],
                     "bookings": [upcoming[d]["bookings"] for d in days], "movable_elective": [upcoming[d]["elective"] for d in days]},
        "sources": ["surgeries", "operating_rooms", "daily_metrics"]}


# ───────────────────────── data quality ─────────────────────────

def _overlaps(intervals: dict[str, list[tuple[datetime, datetime]]]) -> int:
    n = 0
    for spans in intervals.values():
        spans.sort()
        n += sum(1 for a, b in zip(spans, spans[1:]) if b[0] < a[1])
    return n


def quality(td: TwinData, report_path: Path | None = None) -> dict:
    """Re-computes integrity checks from the CSVs instead of trusting validation_report.json."""
    t = td.t
    checks = []

    def add(name, value, ok, detail, warn=False):
        checks.append({"name": name, "value": value, "status": "pass" if ok else ("warn" if warn else "fail"), "detail": detail})

    stays = defaultdict(list)
    for s in t["bed_stays"]:
        stays[s["bed_id"]].append((_dt(s["start_time"]), _dt(s["end_time"])))
    add("Overlapping bed stays", _overlaps(stays), _overlaps(stays) == 0, "Same bed used by two stays at once")
    by_or = defaultdict(list)
    for s in t["surgeries"]:
        by_or[s["or_id"]].append((_dt(s["planned_start"]), _dt(s["planned_start"]) + timedelta(minutes=int(s["planned_duration_minutes"]))))
    add("Overlapping planned surgeries", _overlaps(by_or), _overlaps(by_or) == 0, "Same operating room booked twice")
    wait_gt_los = sum(1 for v in t["visits"] if float(v["wait_minutes"]) > float(v["length_of_stay_hours"]) * 60 + 1e-6)
    add("Wait longer than length of stay", wait_gt_los, wait_gt_los == 0, "Visits whose wait exceeds their whole stay")
    bad_margin = sum(1 for s in t["surgeries"] if s["status"] == "CANCELLED" and float(s["contribution_margin_egp"] or 0) != 0)
    add("Cancelled surgeries with realised margin", bad_margin, bad_margin == 0, "Cancelled cases must have zero realised margin")
    er = td.dept_by_type["ER"]
    arrivals = Counter(v["arrival_time"][:10] for v in t["visits"])
    mismatch = [d for d, rows in td.metrics.items() if int(rows[er]["arrivals_count"]) != arrivals.get(d, 0)]
    add("ER arrivals match visits", len(mismatch), not mismatch, "Days where daily_metrics disagrees with the visits table")
    # staff coverage per day relative to the median day
    per_day = Counter(s[0].date().isoformat() for s in td.shifts)
    days = [d for d in td.metrics]
    med = statistics.median(per_day.get(d, 0) for d in days)
    odd = sorted((d, per_day.get(d, 0)) for d in days if per_day.get(d, 0) < 0.5 * med or per_day.get(d, 0) > 2 * med)
    add("Days with unusual staff coverage", len(odd), not odd,
        f"Shifts per day vs median {med:g}: " + ", ".join(f"{d} has {n}" for d, n in odd[:6]) if odd else f"Median {med:g} shifts per day", warn=True)
    nurse_days = {}
    for unit in ("ER", "ICU", "WARD"):
        dep = td.dept_by_type[unit]
        covered = {s[0].date().isoformat() for s in td.shifts if td.staff_role[s[2]] == (dep, "NURSE") and s[3] != "ABSENT"}
        nurse_days[unit] = sum(1 for d in days if d not in covered)
    add("Days without any nurse shift", sum(nurse_days.values()), sum(nurse_days.values()) == 0,
        "Days with no present nurse per unit: " + ", ".join(f"{u} {n}" for u, n in nurse_days.items()), warn=True)
    unapproved = [p["document_id"] for p in t["policy_documents"] if p["approved"] != "True"]
    add("Approved policies", len(t["policy_documents"]) - len(unapproved), not unapproved,
        "Policies in the pack are synthetic examples and none is approved" if unapproved else "All approved", warn=True)
    blank_conf = sum(1 for r in t["decisions"] if not r["confidence_score"])
    add("Decision confidence calibrated", len(t["decisions"]) - blank_conf, blank_conf == 0, "Confidence is blank until a model is calibrated", warn=True)

    report = {}
    if report_path and Path(report_path).exists():
        report = json.loads(Path(report_path).read_text())
    claims = [("visits", "visit_count", len(t["visits"])), ("surgeries", "surgery_count", len(t["surgeries"])),
              ("bed_stays", "bed_stay_count", len(t["bed_stays"]))]
    for label, key, actual in claims:
        if key in report:
            add(f"{label} count matches validation_report.json", actual, report[key] == actual, f"report says {report[key]}, CSV has {actual}")
    return {"tables": {k: len(v) for k, v in sorted(t.items())}, "days": len(td.metrics),
            "period": {"from": td.first_day, "to": td.last_day}, "checks": checks,
            "summary": dict(Counter(c["status"] for c in checks))}


# ───────────────────────── overview ─────────────────────────

def _tile(key, label, unit, values, source, higher_is_worse=None, fmt=1):
    """One KPI: latest observed day, change against the mean of the 7 days before it, 14-day sparkline."""
    obs = [v for v in values if v is not None]
    latest = values[-1] if values else None
    prev = [v for v in values[-8:-1] if v is not None]
    base = statistics.fmean(prev) if prev else None
    delta = round(100 * (latest - base) / base, 1) if latest is not None and base else None
    tone = None
    if delta is not None and higher_is_worse is not None and abs(delta) >= 1:
        tone = "bad" if (delta > 0) == higher_is_worse else "good"
    return {"key": key, "label": label, "unit": unit, "value": None if latest is None else round(latest, fmt),
            "delta_pct": delta, "tone": tone, "spark": values, "source": source, "points": len(obs)}


PRESSURE_METRIC = {"ER": ("avg_wait_minutes", 1, "min"), "ICU": ("occupancy_rate", 100, "%"), "WARD": ("occupancy_rate", 100, "%"),
                   "OR": ("or_utilization_rate", 100, "%"), "CLINIC": ("arrivals_count", 1, "patients"),
                   "IMAGING": ("arrivals_count", 1, "patients"), "LAB": ("arrivals_count", 1, "patients")}


def overview(td: TwinData, as_of: datetime) -> dict:
    hist = _days(as_of, HISTORY_DAYS)
    col = lambda unit, c, k=1: _unit_history(td, unit, hist, c, k)
    total = lambda src, f: [round(sum(_num(r[f]) or 0 for r in src.get(d, {}).values()), 2) for d in hist]
    pools = td.pools(as_of)
    beds_total = sum(pools[f"{u}:beds"].total for u in ("ER", "ICU", "WARD"))
    used = sum(pools[f"{u}:beds"].committed for u in ("ER", "ICU", "WARD"))
    occ_hist = [None if not (st := sum(_num(td.metrics.get(d, {}).get(td.dept_by_type[u], {}).get("staffed_beds")) or 0 for u in ("ER", "ICU", "WARD")))
                else round(100 * sum(_num(td.metrics[d][td.dept_by_type[u]]["occupied_beds"]) or 0 for u in ("ER", "ICU", "WARD")) / st, 1) for d in hist]
    patients_hist = [sum(_num(td.metrics.get(d, {}).get(td.dept_by_type[u], {}).get("occupied_beds")) or 0 for u in ("ER", "ICU", "WARD")) for d in hist]
    shifts_day = Counter(s[0].date().isoformat() for s in td.shifts)
    staff_now = sum(p.total for k, p in pools.items() if k.split(":")[1] in STAFF_ROLES.values())
    nurses_now = sum(p.total for k, p in pools.items() if k.endswith(":nurses"))
    ahead = [s for s in td.t["surgeries"] if _dt(s["planned_start"]) > as_of and s["status"] != "CANCELLED"]
    cap, orx = capacity(td, as_of), operating_rooms(td, as_of)
    flow = cap["units"]["ER"]["flow"]
    cards = [
        _tile("beds", "Total beds", "beds", occ_hist, "bed_stays", None, 1) | {"value": beds_total, "sub": {"occupied": used, "occupancy_pct": round(100 * used / beds_total, 1) if beds_total else None}},
        _tile("patients", "Current patients", "patients", patients_hist, "daily_metrics", None, 0) | {"value": used, "sub": {"per_unit": {u: pools[f"{u}:beds"].committed for u in ("ER", "ICU", "WARD")}}},
        _tile("icu_er", "ICU and ER", "beds occupied", col("ICU", "occupied_beds"), "bed_stays", None, 0) | {"value": pools["ICU:beds"].committed,
              "sub": {"icu_total": pools["ICU:beds"].total, "er_occupied": pools["ER:beds"].committed, "er_total": pools["ER:beds"].total}},
        _tile("staff", "Clinical staff on shift", "staff", [shifts_day.get(d, 0) for d in hist], "staff_shifts", None, 0) | {"value": staff_now,
              "sub": {"nurses": nurses_now, "doctors": staff_now - nurses_now}},
        _tile("surgeries", "Surgeries booked ahead", "cases", col("OR", "surgeries_scheduled"), "surgeries", None, 0) | {"value": len(ahead), "delta_pct": None, "tone": None,
              "sub": {"elective": sum(s["urgency"] == "ELECTIVE" for s in ahead)}},
        _tile("er_wait", "ER average wait", "min", col("ER", "avg_wait_minutes"), "daily_metrics", True),
        _tile("or_util", "OR utilisation", "%", col("OR", "or_utilization_rate", 100), "daily_metrics", False),
        _tile("overtime", "Overtime", "hours/day", total(td.metrics, "overtime_hours"), "daily_metrics", True),
    ]
    departments, pressure = [], []
    day = hist[-1]
    for dep, r in td.metrics.get(day, {}).items():
        d = td.dept[dep]
        departments.append({"id": dep, "type": d["department_type"], "name": d["department_name"], "name_ar": d["department_name_ar"],
                            "arrivals": _num(r["arrivals_count"]), "occupancy_pct": None if _num(r["occupancy_rate"]) is None else round(100 * _num(r["occupancy_rate"]), 1),
                            "avg_wait_min": _num(r["avg_wait_minutes"]), "overtime_hours": _num(r["overtime_hours"]), "absences": _num(r["staff_absence_count"])})
        column, scale, unit = PRESSURE_METRIC[d["department_type"]]
        series = [None if (v := _num(td.metrics.get(x, {}).get(dep, {}).get(column))) is None else v * scale for x in hist]
        prev = [v for v in series[:-1] if v is not None]
        latest = series[-1]
        base = statistics.fmean(prev) if prev else None
        pressure.append({"id": dep, "type": d["department_type"], "name": d["department_name"], "name_ar": d["department_name_ar"], "metric": column, "unit": unit,
                         "latest": None if latest is None else round(latest, 1), "mean_prior": None if base is None else round(base, 1),
                         "delta_pct": round(100 * (latest - base) / base, 1) if latest is not None and base else None})
    return {
        "as_of": as_of.isoformat(), "period": _period(hist), "latest_day": day, "cards": cards,
        "inpatient": {"beds_total": beds_total, "occupied_now": used, "occupancy_now_pct": round(100 * used / beds_total, 1) if beds_total else None},
        "occupancy": {"dates": hist, "icu_pct": col("ICU", "occupancy_rate", 100), "ward_pct": col("WARD", "occupancy_rate", 100), "er_pct": col("ER", "occupancy_rate", 100)},
        "er": {"dates": hist, "arrivals": col("ER", "arrivals_count"), "wait_avg": col("ER", "avg_wait_minutes"), "wait_p90": col("ER", "p90_wait_minutes")},
        "er_hourly": {"values": flow["arrivals_by_hour"], "window": flow["window"]},
        "finance": {"dates": hist, "revenue_egp": total(td.finance, "revenue_egp"), "risk_egp": total(td.finance, "cancelled_procedure_risk_egp")},
        "bed_use": {"used": used, "available": beds_total - used, "closed": sum(1 for b in td.t["beds"] if b["status"] != "AVAILABLE")},
        "performance": {"er_wait_min": cards[5]["value"], "lwbs_pct": flow["left_without_being_seen_pct"], "or_delay_pct": orx["delay_rate_pct"], "or_cancel_pct": orx["cancel_rate_pct"],
                        "windows": {"er": flow["window"], "or": orx["history_period"]}},
        "departments": departments, "pressure": pressure,
        "forecast_occupancy": {u: weekday_forecast(td, as_of, u, "occupancy_rate", 100) for u in ("ICU", "WARD")},
        "pack_simulations": [{"id": r["simulation_id"], "name": r["scenario_name"], "type": r["scenario_type"],
                              "results": json.loads(r["results_json"]), "notes": r["notes"]} for r in td.t["simulation_runs"]],
    }


# ───────────────────────── alerts ─────────────────────────

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


def build_alerts(td: TwinData, as_of: datetime, decision, nurse_policy_units: set[str], quality_report: dict,
                 llm_configured: bool) -> list[dict]:
    """Alerts are facts derived from the agents' baseline run, the data and the system state (never from an LLM)."""
    out = []

    def add(id_, severity, source, page, title, title_ar, detail, detail_ar, evidence):
        out.append({"id": id_, "severity": severity, "source": source, "page": page, "title": title, "title_ar": title_ar,
                    "detail": detail, "detail_ar": detail_ar, "evidence": evidence})

    card = decision.card
    pool_ar = lambda pool: {"ICU": "العناية المركزة", "WARD": "القسم الداخلي", "ER": "الطوارئ"}.get(pool.split(":")[0], pool.split(":")[0]) + " " + {"beds": "الأسرة", "nurses": "التمريض", "doctors": "الأطباء"}.get(pool.split(":")[1], pool.split(":")[1])
    for pool, deficit in sorted(card.unmet.items()):
        f = next((f for r in decision.reports for f in r.findings if f.pool == pool), None)
        peak, cap = (f"{f.projected:.1f}", f.total) if f else ("?", "?")
        add(f"unmet-{pool}", "critical", "agent", "agents", f"{pool.replace(':', ' ')} are forecast to run short", f"{pool_ar(pool)}: نقص متوقع",
            f"Forecast peak demand {peak} against {cap} available; {deficit} still unmet after the accepted actions.",
            f"ذروة الطلب المتوقع {peak} مقابل {cap} متاح؛ ما زال {deficit} غير مغطى بعد الإجراءات المقبولة.", {"pool": pool, "deficit": deficit})
    if decision.accepted:
        n = len(decision.accepted)
        add("agents-proposals", "high", "agent", "decisions", f"{n} agent proposal(s) waiting for a decision", f"{n} اقتراح من الوكلاء ينتظر قرارًا",
            "The agents found actions that satisfy every hard constraint.", "وجد الوكلاء إجراءات تستوفي كل القيود الصارمة.", {"proposals": [p.id for p in decision.accepted]})
    missing = sorted({"ER", "ICU", "WARD"} - nurse_policy_units)
    if missing:
        add("policy-missing", "high", "system", "system", "No nurse-ratio policy for " + ", ".join(missing), "لا توجد سياسة لنسبة التمريض في: " + "، ".join(missing),
            "Bed and nurse changes in these units are rejected as unverifiable until an Operations Manager enters a ratio.",
            "تُرفض تغييرات الأسرة والتمريض في هذه الأقسام لأنها غير قابلة للتحقق حتى يُدخل مدير التشغيل نسبة.", {"units": missing})
    pools = td.pools(as_of)
    no_nurses = [u for u in ("ER", "ICU", "WARD") if pools[f"{u}:nurses"].total == 0]
    if no_nurses:
        add("no-nurse-data", "medium", "data", "staff", "No nurse in the shift data for " + ", ".join(no_nurses), "لا يوجد ممرض في بيانات الورديات في: " + "، ".join(no_nurses),
            "At this replay moment the shift table lists no non-absent nurse there. Treat staffing conclusions with care.",
            "في هذه اللحظة لا يظهر في جدول الورديات أي ممرض حاضر هناك. تعامل بحذر مع استنتاجات الطاقم.", {"units": no_nurses})
    odd = next((c for c in quality_report["checks"] if c["name"] == "Days with unusual staff coverage" and c["value"]), None)
    if odd:
        add("coverage", "medium", "data", "system", f"{odd['value']} days have unreliable staff coverage in the data", f"{odd['value']} أيام بتغطية طاقم غير موثوقة في البيانات",
            odd["detail"], "جدول الورديات يتأرجح بين أيام شبه فارغة وأخرى ممتلئة جدًا.", {"days": odd["value"]})
    add("no-simulation", "info", "system", "simulator", "No simulation engine connected", "لا يوجد محرك محاكاة متصل",
        "Waiting time and revenue effects of a decision cannot be computed; they are shown as unavailable.",
        "لا يمكن حساب أثر القرار على الانتظار والإيراد، ويُعرض على أنه غير متاح.", {})
    if not llm_configured:
        add("no-llm", "info", "system", "system", "LLM not configured", "النموذج اللغوي غير متصل",
            "Summaries use the deterministic template. Set GROQ_API_KEY to enable LLM wording.", "الملخصات تستخدم القالب المدمج. اضبط GROQ_API_KEY لتفعيل صياغة النموذج اللغوي.", {})
    return sorted(out, key=lambda a: SEVERITY_ORDER[a["severity"]])


# ───────────────────────── digital twin map (schematic) ─────────────────────────

def twin_map(td: TwinData, as_of: datetime) -> dict:
    """Departments with their beds/rooms at the replay moment. The data pack has no building or floor
    information, so this is a schematic of the departments, not a floor plan."""
    pools, occupied = td.pools(as_of), set()
    for bed, start, end in td.stays:
        if start <= as_of < end:
            occupied.add(bed)
    room_name = {r["room_id"]: r["room_name"] for r in td.t["rooms"]}
    room_dept = {r["room_id"]: r["department_id"] for r in td.t["rooms"]}
    beds_by_unit = defaultdict(list)
    closed = 0
    for b in td.t["beds"]:
        if b["status"] != "AVAILABLE":
            closed += 1
            continue
        unit = td.dept[room_dept[b["room_id"]]]["department_type"]
        beds_by_unit[unit].append({"id": b["bed_id"], "room": room_name[b["room_id"]], "occupied": b["bed_id"] in occupied})
    day = (as_of.date() - timedelta(days=1)).isoformat()
    booked = defaultdict(float)
    for s in td.t["surgeries"]:
        if _dt(s["planned_start"]).date() == as_of.date() and s["status"] != "CANCELLED":
            booked[s["or_id"]] += float(s["planned_duration_minutes"])
    depts = []
    for dep_id, d in td.dept.items():
        unit, m = d["department_type"], td.metrics.get(day, {}).get(dep_id, {})
        item = {"id": dep_id, "type": unit, "name": d["department_name"], "name_ar": d["department_name_ar"],
                "nurses": pools[f"{unit}:nurses"].total if f"{unit}:nurses" in pools else None,
                "doctors": pools[f"{unit}:doctors"].total if f"{unit}:doctors" in pools else None,
                "arrivals": _num(m.get("arrivals_count")), "avg_wait_min": _num(m.get("avg_wait_minutes")), "day": day}
        if unit in beds_by_unit:
            beds = sorted(beds_by_unit[unit], key=lambda b: b["id"])
            item.update(beds=beds, beds_total=len(beds), occupied=sum(b["occupied"] for b in beds))
        if unit == "OR":
            item["rooms"] = [{"id": r["or_id"], "name": r["room_name"], "booked_min_on_day": booked.get(r["or_id"], 0.0),
                              "capacity_min": float(r["daily_capacity_hours"]) * 60} for r in td.t["operating_rooms"] if r["is_active"] == "True"]
        depts.append(item)
    return {"as_of": as_of.isoformat(), "departments": depts, "closed_beds": closed, "schematic": True,
            "note": "Schematic of the departments in the data pack. The pack has no buildings, floors or coordinates.",
            "sources": ["beds", "rooms", "bed_stays", "staff_shifts", "daily_metrics", "surgeries", "operating_rooms"]}
