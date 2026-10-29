"""Adapter: the synthetic CSV data pack  ->  the agents' SharedInputs, plus the read-only
queries the dashboard needs. This is the only module that knows the CSV schema.

Replay model: pick any moment `as_of` inside the 90-day history. The twin is rebuilt as it
stood then (beds occupied, staff on shift, future bookings) and the forecast is computed only
from data *before* that moment, so the days after it can be used to check the forecast.

Documented simplifications (all synthetic-data conveniences, none clinical):
  * Bed demand forecast = weekday-mean occupancy rate x physical beds (the pack's own method);
    the upper bound is mean + 1 sample std (descriptive, not a calibrated interval).
  * Staff pools = non-absent shifts covering `as_of`, by role (NURSE, DOCTOR). Staff demand has
    no forecast in the pack, so the Staff agent abstains.
  * OR slot = one OR-day, capacity = room hours x 60 minutes; clinic slot = one cardiology
    clinic hour, capacity = rostered clinic doctors x 60 minutes. Durations are minutes.
  * Elective surgeries and FOLLOW_UP appointments are the only movable bookings; the pack has
    no urgency field for appointments, so NEW_CONSULT is treated as not movable.
  * For bookings after `as_of`, the final outcome status is unknown at that time and ignored,
    except CANCELLED/RESCHEDULED which are excluded.
  * needs_icu_bed = the surgery's visit has an ICU stay somewhere in the data.
"""
from __future__ import annotations

import csv
import dataclasses
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from .contracts import (Action, Booking, Forecast, ForecastPoint, PoolAdjustment, Pool,
                        Reschedule, SharedInputs, SimulationBaseline, Slot, SlotKey, TwinSnapshot, Urgency)
from .policies import PolicyDoc

BED_UNITS = ("ER", "ICU", "WARD")
FORECAST_DAYS = 7
CLINIC_HOURS = range(8, 17)
STAFF_ROLES = {"NURSE": "nurses", "DOCTOR": "doctors"}
NOT_BOOKED = {"CANCELLED", "RESCHEDULED"}


@dataclass(frozen=True)
class Scenario:
    """What-if knob: multiply forecast bed demand of one unit (1.0 = no stress)."""
    unit: str = "ICU"
    demand_multiplier: float = 1.0


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class TwinData:
    def __init__(self, data_dir: str | Path) -> None:
        d = Path(data_dir)
        t = {p.stem: _read(p) for p in d.glob("*.csv")}
        self.t = t
        self.dept = {r["department_id"]: r for r in t["departments"]}
        self.dept_by_type = {r["department_type"]: r["department_id"] for r in t["departments"]}
        room_dept = {r["room_id"]: r["department_id"] for r in t["rooms"]}
        self.beds = [(b["bed_id"], self.dept[room_dept[b["room_id"]]]["department_type"])
                     for b in t["beds"] if b["status"] == "AVAILABLE"]
        self.stays = [(s["bed_id"], _dt(s["start_time"]), _dt(s["end_time"])) for s in t["bed_stays"]]
        self.icu_visits = {s["visit_id"] for s in t["bed_stays"] if s["bed_id"].startswith("BED-ICU")}
        self.staff_role = {s["staff_id"]: (s["department_id"], s["role"]) for s in t["staff"]}
        self.shifts = [(_dt(s["shift_start"]), _dt(s["shift_end"]), s["staff_id"], s["status"])
                       for s in t["staff_shifts"]]
        self.metrics = defaultdict(dict)                     # date -> dept_id -> row
        for r in t["daily_metrics"]:
            self.metrics[r["metric_date"]][r["department_id"]] = r
        self.finance = defaultdict(dict)
        for r in t["financial_daily"]:
            self.finance[r["metric_date"]][r["department_id"]] = r
        self.first_day, self.last_day = min(self.metrics), max(self.metrics)
        # Three days before the end: leaves 3 days of actuals for the forecast check and a calendar
        # of future bookings. (Shift coverage in the pack is uneven: 10-18 has 2 shifts, 10-19 none.)
        self.default_as_of = _dt(self.last_day) - timedelta(days=3) + timedelta(hours=8)
        self.beds_total = defaultdict(int)
        for _, unit in self.beds:
            self.beds_total[unit] += 1

    # ───────────────────────── twin snapshot ─────────────────────────

    def pools(self, as_of: datetime) -> dict[str, Pool]:
        out: dict[str, Pool] = {}
        occupied = defaultdict(int)
        bed_unit = dict(self.beds)
        for bed, start, end in self.stays:
            if start <= as_of < end and bed in bed_unit:
                occupied[bed_unit[bed]] += 1
        for unit in BED_UNITS:
            n = self.beds_total[unit]
            # hard_max stays None: the pack gives no ceiling for converting beds between units, so
            # a unit may only grow inside a capacity-neutral (donor) move.
            out[f"{unit}:beds"] = Pool(f"{unit}:beds", n, occupied[unit])
        on_shift = defaultdict(int)
        for start, end, staff, status in self.shifts:
            if start <= as_of < end and status != "ABSENT":
                dept, role = self.staff_role[staff]
                if role in STAFF_ROLES:
                    on_shift[(self.dept[dept]["department_type"], STAFF_ROLES[role])] += 1
        for unit in {r["department_type"] for r in self.dept.values()}:
            for kind in STAFF_ROLES.values():
                if any(self.staff_role[s][0] == self.dept_by_type[unit] and STAFF_ROLES.get(self.staff_role[s][1]) == kind
                       for s in self.staff_role):
                    out[f"{unit}:{kind}"] = Pool(f"{unit}:{kind}", on_shift[(unit, kind)], 0)
        return out

    def calendar(self, as_of: datetime) -> tuple[tuple[Slot, ...], tuple[Booking, ...]]:
        end_day = (_dt(self.last_day) + timedelta(days=FORECAST_DAYS + 14)).date()
        days = [as_of.date() + timedelta(days=i) for i in range((end_day - as_of.date()).days + 1)]
        booked: dict[SlotKey, int] = defaultdict(int)
        bookings: list[Booking] = []
        for r in self.t["surgeries"]:
            start = _dt(r["planned_start"])
            if start <= as_of or r["status"] in NOT_BOOKED:
                continue
            key = SlotKey("OR", r["or_id"], datetime.combine(start.date(), time.min))
            dur = int(r["planned_duration_minutes"])
            booked[key] += dur
            bookings.append(Booking(
                r["surgery_id"], key, Urgency.NON_URGENT if r["urgency"] == "ELECTIVE" else Urgency.URGENT,
                None, r["visit_id"] in self.icu_visits, dur))
        clinic = self.dept_by_type.get("CLINIC")
        doctors = sum(1 for dep, role in self.staff_role.values() if dep == clinic and role == "DOCTOR")
        for r in self.t["appointments"]:
            start = _dt(r["scheduled_start"])
            if r["department_id"] != clinic or start <= as_of or r["status"] in NOT_BOOKED:
                continue
            key = SlotKey("CLINIC", clinic, start.replace(minute=0, second=0))
            dur = int(r["duration_minutes"])
            booked[key] += dur
            bookings.append(Booking(
                r["appointment_id"], key,
                Urgency.NON_URGENT if r["appointment_type"] == "FOLLOW_UP" else Urgency.URGENT,
                None, False, dur))
        slots = []
        for day in days:
            midnight = datetime.combine(day, time.min)
            for r in self.t["operating_rooms"]:
                if r["is_active"] == "True":
                    key = SlotKey("OR", r["or_id"], midnight)
                    slots.append(Slot(key, midnight + timedelta(days=1),
                                      int(float(r["daily_capacity_hours"]) * 60), booked[key]))
            for h in CLINIC_HOURS:
                if clinic:
                    key = SlotKey("CLINIC", clinic, midnight + timedelta(hours=h))
                    slots.append(Slot(key, key.start + timedelta(hours=1), doctors * 60, booked[key]))
        return tuple(slots), tuple(sorted(bookings, key=lambda b: b.id))

    def snapshot(self, as_of: datetime, version_suffix: str = "") -> TwinSnapshot:
        slots, bookings = self.calendar(as_of)
        return TwinSnapshot(as_of, f"replay@{as_of.isoformat()}{version_suffix}", self.pools(as_of), slots, bookings)

    # ───────────────────────── forecast ─────────────────────────

    def occupancy_forecast(self, as_of: datetime, unit: str, days: int = FORECAST_DAYS) -> list[dict]:
        """Weekday-mean occupancy from history strictly before `as_of`'s day, with actuals if known."""
        dep = self.dept_by_type[unit]
        by_wd = defaultdict(list)
        for day, rows in self.metrics.items():
            if _dt(day).date() < as_of.date() and rows[dep]["occupancy_rate"]:
                by_wd[_dt(day).weekday()].append(float(rows[dep]["occupancy_rate"]))
        out = []
        for i in range(1, days + 1):
            d = as_of.date() + timedelta(days=i)
            vals = by_wd.get(d.weekday(), [])
            if not vals:
                continue
            mean = statistics.fmean(vals)
            std = statistics.stdev(vals) if len(vals) > 1 else 0.0
            actual = self.metrics.get(d.isoformat(), {}).get(dep, {}).get("occupancy_rate")
            out.append({"date": d.isoformat(), "predicted": mean, "lower": max(0.0, mean - std),
                        "upper": min(1.0, mean + std), "actual": float(actual) if actual else None})
        return out

    def forecast(self, as_of: datetime, scenario: Scenario = Scenario()) -> Forecast:
        series = {}
        for unit in ("ICU", "WARD"):
            k = scenario.demand_multiplier if unit == scenario.unit else 1.0
            n = self.beds_total[unit]
            series[f"{unit}:beds:demand"] = tuple(
                ForecastPoint(_dt(p["date"] + "T08:00:00"), p["predicted"] * n * k,
                              p["lower"] * n * k, p["upper"] * n * k)
                for p in self.occupancy_forecast(as_of, unit))
        series = {m: pts for m, pts in series.items() if pts}
        return Forecast("weekday_mean_baseline/replay", series, confidence=None)

    def backtest(self, as_of: datetime) -> dict:
        """WAPE/MAPE of the occupancy forecast on the days after `as_of` that exist in the data."""
        out = {}
        for unit in ("ICU", "WARD"):
            rows = [r for r in self.occupancy_forecast(as_of, unit) if r["actual"] is not None]
            tot = sum(r["actual"] for r in rows)
            out[unit] = {
                "days": len(rows),
                "wape": round(sum(abs(r["predicted"] - r["actual"]) for r in rows) / tot, 3) if tot else None,
                "mape": round(statistics.fmean(abs(r["predicted"] - r["actual"]) / r["actual"]
                                               for r in rows if r["actual"]) , 3)
                        if any(r["actual"] for r in rows) else None}
        return out

    # ───────────────────────── shared inputs ─────────────────────────

    def revenue_at_risk_7d(self, as_of: datetime) -> float:
        total = 0.0
        for i in range(1, 8):
            day = (as_of.date() - timedelta(days=i)).isoformat()
            total += sum(float(r["cancelled_procedure_risk_egp"] or 0) for r in self.finance.get(day, {}).values())
        return round(total, 2)

    def inputs(self, as_of: datetime, scenario: Scenario = Scenario(), applied: Sequence[Sequence[Action]] = (),
               policies=None) -> SharedInputs:
        snap = self.snapshot(as_of, f"|sandbox:{len(applied)}")
        for actions in applied:
            snap = apply_actions(snap, actions)
        sim = SimulationBaseline(f"history-7d-before-{as_of.date()}", None,
                                 {"revenue_at_risk_egp": self.revenue_at_risk_7d(as_of)})
        kw = {"policies": policies} if policies is not None else {}
        return SharedInputs(snap, self.forecast(as_of, scenario), sim, **kw)

    def policy_docs(self) -> list[PolicyDoc]:
        """Text-only policies from the pack; never numeric constraints (they are test examples)."""
        return [PolicyDoc(r["document_id"], f"{r['title']} [{r['source_label']}, approved={r['approved']}]",
                          r["content"]) for r in self.t["policy_documents"]]


# ───────────────────────── sandbox overlay ─────────────────────────

def apply_actions(snap: TwinSnapshot, actions: Iterable[Action]) -> TwinSnapshot:
    """Apply approved actions to a snapshot (sandbox execution). Alerts change nothing."""
    pools, slots, bookings = dict(snap.pools), {s.key: s for s in snap.slots}, {b.id: b for b in snap.bookings}
    for a in actions:
        if isinstance(a, PoolAdjustment):
            p = pools[a.pool]
            pools[a.pool] = dataclasses.replace(p, total=p.total + a.delta)
        elif isinstance(a, Reschedule):
            b = bookings[a.booking_id]
            for key, sign in ((a.from_slot, -1), (a.to_slot, +1)):
                slots[key] = dataclasses.replace(slots[key], booked=slots[key].booked + sign * b.duration) \
                    if key in slots else slots.get(key)
            bookings[b.id] = dataclasses.replace(b, slot=a.to_slot)
    return dataclasses.replace(snap, pools=pools, slots=tuple(s for s in slots.values() if s),
                               bookings=tuple(sorted(bookings.values(), key=lambda b: b.id)))
