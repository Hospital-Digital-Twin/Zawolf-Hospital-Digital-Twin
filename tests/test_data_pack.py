"""The CSV adapter against the real data pack in ./data (no mocks)."""
import asyncio
import csv
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from hospital_agents import Orchestrator, Settings
from hospital_agents.contracts import PoolAdjustment, PolicyBundle, PolicyValue, Reschedule
from hospital_agents.policies import FolderPolicyRetriever
from hospital_agents.twin_data import Scenario, TwinData, apply_actions

DATA = Path(__file__).parent.parent / "data"
td = TwinData(DATA)
AS_OF = td.default_as_of


def decide(scenario=Scenario(), policies=None, applied=()):
    inputs = td.inputs(AS_OF, scenario, applied, policies)
    return asyncio.run(Orchestrator(Settings(language="en")).run(inputs))


class Snapshot(unittest.TestCase):
    def test_bed_pools_match_the_bed_table(self):
        pools = td.pools(AS_OF)
        self.assertEqual((pools["ER:beds"].total, pools["ICU:beds"].total, pools["WARD:beds"].total), (12, 12, 36))

    def test_occupied_beds_match_daily_metrics_convention(self):
        # README: occupied beds = bed-stay intervals active at noon
        noon = datetime(2026, 10, 20, 12)
        row = td.metrics["2026-10-20"]
        for unit, dep in (("ICU", "DEP-ICU"), ("WARD", "DEP-WARD"), ("ER", "DEP-ER")):
            self.assertEqual(td.pools(noon)[f"{unit}:beds"].committed, int(row[dep]["occupied_beds"]), unit)

    def test_slot_load_equals_booked_minutes(self):
        slots, bookings = td.calendar(AS_OF)
        load = {}
        for b in bookings:
            load[b.slot] = load.get(b.slot, 0) + b.duration
        for s in slots:
            self.assertEqual(s.booked, load.get(s.key, 0))
        self.assertTrue(all(b.slot.start > AS_OF - timedelta(days=1) for b in bookings))

    def test_no_future_leakage_in_forecast(self):
        as_of = datetime(2026, 8, 14, 8)
        first = td.occupancy_forecast(as_of, "ICU")[0]
        wd = datetime.fromisoformat(first["date"]).weekday()
        history = [float(rows["DEP-ICU"]["occupancy_rate"]) for day, rows in td.metrics.items()
                   if day < as_of.date().isoformat() and datetime.fromisoformat(day).weekday() == wd]
        self.assertGreater(len(history), 1)
        self.assertAlmostEqual(first["predicted"], sum(history) / len(history), places=9)

    def test_forecast_reproduces_the_pack_forecast_file(self):
        pack = [r for r in td.t["forecast_results"] if r["target_metric"] == "bed_occupancy_rate"]
        for unit, dep in (("ICU", "DEP-ICU"), ("WARD", "DEP-WARD")):
            mine = {r["date"]: r["predicted"] for r in td.occupancy_forecast(datetime(2026, 10, 22, 8), unit)}
            for r in (p for p in pack if p["department_id"] == dep):
                self.assertAlmostEqual(mine[r["forecast_for"][:10]], float(r["predicted_value"]), delta=0.002)

    def test_overview_tiles_come_from_the_csvs(self):
        from hospital_agents import insights
        o = insights.overview(td, AS_OF)
        self.assertEqual(o["period"]["to"], "2026-10-16")            # complete days before the replay moment
        er = td.dept_by_type["ER"]
        self.assertEqual(o["er"]["arrivals"][-1], float(td.metrics["2026-10-16"][er]["arrivals_count"]))
        self.assertEqual(len(o["departments"]), 7)
        self.assertEqual(len(o["pack_simulations"]), 2)


class Agents(unittest.TestCase):
    def test_baseline_needs_no_action(self):
        d = decide()
        self.assertEqual((d.accepted, d.card.critical_bottleneck), ((), None))

    def test_stress_without_policy_is_rejected_as_unverifiable(self):
        d = decide(Scenario("ICU", 12))
        self.assertEqual(d.card.critical_bottleneck, "ICU:beds")
        beds = [r for r in d.rejected if r.proposal.agent == "bed"]
        self.assertTrue(beds and "unverifiable" in beds[0].reasons[0])

    def test_stress_with_policy_accepts_the_bed_move_and_reports_what_is_unmet(self):
        pol = PolicyBundle({f"nurse.max_patients_per_nurse:{u}": PolicyValue(40, "test") for u in ("ICU", "WARD")})
        d = decide(Scenario("ICU", 12), pol)
        bed = next(p for p in d.accepted if p.agent == "bed")
        self.assertEqual(sorted(a.delta for a in bed.actions), [-15, 15])
        self.assertEqual(d.card.unmet["ICU:beds"], 17)          # donors could only cover part of it

    def test_only_elective_surgeries_move(self):
        d = decide(Scenario("ICU", 12))
        urgent = {r["surgery_id"] for r in td.t["surgeries"] if r["urgency"] != "ELECTIVE"}
        moved = {a.booking_id for p in d.accepted for a in p.actions if isinstance(a, Reschedule)}
        self.assertTrue(moved)
        self.assertFalse(moved & urgent)

    def test_apply_then_rollback_restores_the_snapshot(self):
        pol = PolicyBundle({f"nurse.max_patients_per_nurse:{u}": PolicyValue(40, "test") for u in ("ICU", "WARD")})
        d = decide(Scenario("ICU", 12), pol)
        snap = td.snapshot(AS_OF)
        applied = apply_actions(snap, [a for p in d.accepted for a in p.actions])
        self.assertNotEqual(applied.pools["ICU:beds"].total, snap.pools["ICU:beds"].total)
        restored = apply_actions(applied, d.rollback_actions)
        self.assertEqual(restored.pools, snap.pools)
        self.assertEqual({s.key: s.booked for s in restored.slots}, {s.key: s.booked for s in snap.slots})
        self.assertEqual(restored.bookings, snap.bookings)

    def test_pack_policies_are_searchable_text_only(self):
        r = FolderPolicyRetriever(Path(__file__).parent / "fixtures" / "empty", extra=td.policy_docs())
        hits = asyncio.run(r.search("bed allocation"))
        self.assertEqual(hits[0].doc_id, "POL-002")
        self.assertIn("approved=False", hits[0].source)
        self.assertEqual(asyncio.run(r.values(["ICU"], AS_OF)).values, {})   # never a numeric constraint


class Pack(unittest.TestCase):
    def test_all_23_tables_present_and_consistent(self):
        files = sorted(p.stem for p in DATA.glob("*.csv"))
        self.assertEqual(len(files), 23)
        n = {}
        for p in DATA.glob("*.csv"):
            with p.open(encoding="utf-8") as f:
                n[p.stem] = sum(1 for _ in csv.DictReader(f))
        self.assertEqual((n["visits"], n["surgeries"]), (5356, 1260))      # matches validation_report.json


if __name__ == "__main__":
    unittest.main()
