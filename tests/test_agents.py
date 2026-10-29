"""Run with:  PYTHONPATH=src python -m unittest discover -s tests
Fixture numbers are invented to exercise the logic; they are not hospital data."""
import asyncio
import dataclasses
import json
import unittest
from datetime import datetime
from pathlib import Path

from hospital_agents import Orchestrator, Settings, SharedInputs
from hospital_agents.agents import Agent, AgentReport, AppointmentAgent, FinanceAgent, ORAgent
from hospital_agents.arbitration import Arbitrator
from hospital_agents.contracts import (Alert, AlertCode, ImpactEstimate, PoolAdjustment, Proposal,
                                       Reschedule, SlotKey, make_id)
from hospital_agents.explain import LLMExplainer, is_grounded

RAW = json.loads((Path(__file__).parent / "fixtures" / "scenario.json").read_text())


def inputs(mutate=None) -> SharedInputs:
    d = json.loads(json.dumps(RAW))
    if mutate:
        mutate(d)
    return SharedInputs.from_dict(d)


def run(coro):
    return asyncio.run(coro)


def decide(inp=None, settings=None, **kw):
    return run(Orchestrator(settings or Settings(language="en"), **kw).run(inp or inputs()))


def actions(decision):
    return {p.agent: p.actions for p in decision.accepted}


def prop(agent, *acts, impact=None):
    return Proposal(make_id(agent, acts), agent, "t", tuple(acts), ("r",), impact=impact)


class FakeSimulator:
    """Stand-in for the real engine: impact is looked up, never computed here."""
    def __init__(self, table=None):
        self.table = table or {}

    async def evaluate(self, snapshot, proposals):
        ids = tuple(p.id for p in proposals)
        return self.table.get(ids, ImpactEstimate("sim-fake", 1, waiting_minutes_saved=5.0 * len(ids),
                                                  revenue_protected_egp=1000.0 * len(ids)))


class Pipeline(unittest.TestCase):
    def test_bed_and_nurse_bundle_accepted_jointly(self):
        d = decide()
        acts = actions(d)
        self.assertEqual(acts["bed"], (PoolAdjustment("ICU:beds", 4), PoolAdjustment("WARD:beds", -4)))
        self.assertEqual(acts["staff"], (PoolAdjustment("ICU:nurses", 2), PoolAdjustment("WARD:nurses", -2)))

    def test_er_fails_closed_without_nurse_pool(self):
        d = decide()
        self.assertNotIn("er", actions(d))
        reasons = [r for rej in d.rejected for r in rej.reasons]
        self.assertTrue(any("unverifiable" in r for r in reasons))

    def test_missing_ratio_policy_blocks_every_capacity_change(self):
        d = decide(inputs(lambda x: x["policies"].pop("nurse.max_patients_per_nurse:ICU")))
        self.assertNotIn("bed", actions(d))
        self.assertNotIn("staff", actions(d))

    def test_beds_dropped_when_supporting_nurses_unavailable(self):
        def no_nurse_need(x):
            del x["forecast"]["series"]["ICU:nurses:demand"]
        d = decide(inputs(no_nurse_need))
        self.assertNotIn("bed", actions(d))      # 14 beds / 5 nurses > 2.0
        self.assertTrue(any("removed in joint check" in r.reasons[0] for r in d.rejected))

    def test_only_non_urgent_bookings_move(self):
        moved = {a.booking_id for p in decide().accepted for a in p.actions if isinstance(a, Reschedule)}
        self.assertEqual(moved, {"or-1", "cl-1"})

    def test_deterministic(self):
        a, b = decide(), decide()
        self.assertEqual(a.id, b.id)
        self.assertEqual([p.id for p in a.accepted], [p.id for p in b.accepted])

    def test_no_data_no_proposals_and_clear_unverified_flags(self):
        d = decide()
        self.assertIsNone(d.card.confidence)
        self.assertTrue(d.card.unverified)
        self.assertEqual(d.card.critical_bottleneck, "ICU:beds")

    def test_rollback_is_exact_inverse(self):
        d = decide()
        self.assertIn(PoolAdjustment("ICU:beds", -4), d.rollback_actions)
        moves = [a for a in d.rollback_actions if isinstance(a, Reschedule) and a.booking_id == "cl-1"]
        fwd = next(a for p in d.accepted for a in p.actions if isinstance(a, Reschedule) and a.booking_id == "cl-1")
        self.assertEqual((moves[0].from_slot, moves[0].to_slot), (fwd.to_slot, fwd.from_slot))

    def test_simulator_adds_confidence_and_impact(self):
        d = decide(simulator=FakeSimulator())
        self.assertEqual(d.card.confidence, 0.8)
        self.assertEqual(d.card.waiting_minutes_saved, 5.0 * len(d.accepted))
        self.assertEqual(d.card.revenue_at_risk_after_egp, 400000 - 1000.0 * len(d.accepted))

    def test_failing_agent_degrades_instead_of_aborting(self):
        class Boom(Agent):
            name = "boom"
            async def propose(self, inputs, settings):
                raise RuntimeError("x")
        d = decide(agents=[Boom(), AppointmentAgent()])
        self.assertEqual(d.card.degraded_agents, ("boom",))
        self.assertTrue(d.accepted)

    def test_audit_trail_records_every_stage(self):
        from hospital_agents.audit import InMemoryAudit
        audit = InMemoryAudit()
        decide(audit=audit)
        actions_logged = [e.action for e in audit.events]
        self.assertEqual(actions_logged[0], "run.started")
        self.assertEqual(actions_logged[-1], "arbitration.completed")
        self.assertEqual(actions_logged.count("agent.completed"), 7)


class Arbitration(unittest.TestCase):
    def setUp(self):
        self.inp = inputs()
        self.arb = Arbitrator(Settings())
        self.free = SlotKey("OR", "OR-1", datetime.fromisoformat("2026-01-18T09:00:00+00:00"))
        self.src = lambda bid: self.inp.twin.booking(bid).slot

    def resolve(self, *props):
        return self.arb.resolve(self.inp.twin, self.inp.policies, props)

    def test_urgent_booking_is_never_moved(self):
        r = self.resolve(prop("or", Reschedule("or-2", self.src("or-2"), self.free)))
        self.assertFalse(r.accepted)
        self.assertIn("non_urgent", r.rejected[0].reasons[0])

    def test_slot_conflict_goes_to_higher_impact(self):
        a = prop("or", Reschedule("or-1", self.src("or-1"), self.free))
        # a second, different booking claims the same single free slot
        twin = dataclasses.replace(self.inp.twin, bookings=self.inp.twin.bookings + (
            dataclasses.replace(self.inp.twin.booking("or-1"), id="or-3"),))
        b = prop("or", Reschedule("or-3", self.src("or-1"), self.free),
                 impact=ImpactEstimate("s", waiting_minutes_saved=9))
        r = self.arb.resolve(twin, self.inp.policies, [a, b])
        self.assertEqual([p.id for p in r.accepted], [b.id])
        self.assertIn("claimed", r.rejected[0].reasons[0])

    def test_pool_cannot_drop_below_committed(self):
        r = self.resolve(prop("bed", PoolAdjustment("ICU:beds", -3)))
        self.assertFalse(r.accepted)

    def test_priority_order_continuity_beats_money(self):
        money = prop("finance", Alert(AlertCode.REVENUE_AT_RISK, "m"),
                     impact=ImpactEstimate("s", revenue_protected_egp=1e9))
        care = prop("or", Reschedule("or-1", self.src("or-1"), self.free),
                    impact=ImpactEstimate("s", continuity_gain=1))
        self.assertLess(self.arb.rank_key(care), self.arb.rank_key(money))

    def test_proposal_without_actions_rejected(self):
        self.assertFalse(self.resolve(prop("bed")).accepted)


class Agents(unittest.TestCase):
    def test_finance_abstains_without_threshold_and_alerts_above_it(self):
        self.assertFalse(run(FinanceAgent().propose(inputs(), Settings())).proposals)
        rep = run(FinanceAgent().propose(inputs(), Settings(revenue_alert_threshold_egp=100000)))
        self.assertEqual(len(rep.proposals), 1)

    def test_or_agent_abstains_without_forecast(self):
        inp = inputs(lambda x: x["forecast"]["series"].pop("ICU:beds:demand"))
        rep = run(ORAgent().propose(inp, Settings()))
        self.assertFalse(rep.proposals)
        self.assertTrue(rep.notes)

    def test_target_utilization_raises_requirement(self):
        d = decide(settings=Settings(language="en", target_utilization={"beds": 0.5}))
        self.assertTrue(d.card.critical_bottleneck)


class Explanations(unittest.TestCase):
    def test_grounding_check(self):
        facts = {"a": 24, "b": "ICU:beds"}
        self.assertTrue(is_grounded("demand rises 24 percent in ICU", facts))
        self.assertFalse(is_grounded("demand rises 31 percent", facts))
        self.assertTrue(is_grounded("الطلب يرتفع ٢٤ في المئة", facts))

    def test_llm_output_with_invented_number_falls_back(self):
        async def liar(system, user):
            return json.dumps({"sentences": [{"text": "Waiting drops by 99 minutes.", "evidence": ["E5"]}]})
        d = decide(explainer=LLMExplainer(liar))
        self.assertEqual(d.explanation.source, "template")

    def test_plain_text_llm_output_is_not_accepted(self):
        async def chatty(system, user):
            return "1 proposal rejected; human approval required."
        self.assertEqual(decide(explainer=LLMExplainer(chatty)).explanation.source, "template")

    def test_grounded_llm_output_is_used(self):
        async def good(system, user):
            ev = {e["id"]: e["text"] for e in json.loads(user)["evidence"]}
            return json.dumps({"sentences": [{"text": ev["E2"], "evidence": ["E2"]}, {"text": ev["E5"], "evidence": ["E5"]}]})
        d = decide(explainer=LLMExplainer(good))
        self.assertEqual(d.explanation.source, "llm")
        self.assertIn("human approval", d.explanation.text.lower())          # the approval sentence is always appended

    def test_arabic_template(self):
        d = decide(settings=Settings(language="ar"))
        self.assertIn("القرار", d.explanation.text)


if __name__ == "__main__":
    unittest.main()
