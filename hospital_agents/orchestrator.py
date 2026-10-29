"""Run all agents concurrently, score proposals through the simulator, arbitrate, and
assemble the decision. The orchestrator owns no domain logic."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from datetime import datetime, timezone
from typing import Callable, Sequence

from . import __version__
from .agents import (Agent, AgentReport, AppointmentAgent, BedAgent, ERAgent, FinanceAgent,
                     ORAgent, ResourceAgent, StaffAgent)
from .arbitration import Arbitrator
from .audit import InMemoryAudit
from .card import Decision, build_card
from .config import Settings
from .contracts import PolicyBundle, Proposal, SharedInputs, to_jsonable
from .explain import TemplateExplainer
from .ports import AuditEvent, AuditSink, Explainer, Passage, PolicyRetriever, Simulator


# Pool kind -> search topic. English on purpose: it is matched against the corpus language,
# whatever language the operator's UI uses.
_TOPIC = {"beds": "bed capacity escalation", "nurses": "nursing staffing", "doctors": "medical staffing"}


def default_agents() -> list[Agent]:
    return [ERAgent(), BedAgent(), StaffAgent(), ORAgent(), AppointmentAgent(), ResourceAgent(), FinanceAgent()]


class Orchestrator:
    def __init__(self, settings: Settings | None = None, *, agents: Sequence[Agent] | None = None,
                 simulator: Simulator | None = None, explainer: Explainer | None = None,
                 retriever: PolicyRetriever | None = None,
                 audit: AuditSink | None = None, arbitrator: Arbitrator | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.settings = settings or Settings()
        self.agents = list(agents) if agents is not None else default_agents()
        self.simulator = simulator
        self.retriever = retriever
        self.explainer = explainer or TemplateExplainer()
        self.audit = audit or InMemoryAudit()
        self.arbitrator = arbitrator or Arbitrator(self.settings)
        self.clock = clock

    def _log(self, action: str, version: str, **detail) -> None:
        self.audit.record(AuditEvent("agent-orchestrator", action, self.clock(), version, to_jsonable(detail)))

    async def _run_agent(self, agent: Agent, inputs: SharedInputs) -> AgentReport:
        try:
            return await asyncio.wait_for(agent.propose(inputs, self.settings), self.settings.agent_timeout_s)
        except Exception as e:          # one failing agent degrades the decision, never aborts it
            return AgentReport(agent.name, error=f"{type(e).__name__}: {e}" or "timeout")

    async def _score(self, inputs: SharedInputs, p: Proposal) -> Proposal:
        if self.simulator is None:
            return p
        try:
            return p.with_impact(await self.simulator.evaluate(inputs.twin, [p]))
        except Exception:
            return p

    async def _policies(self, inputs: SharedInputs, version: str) -> PolicyBundle:
        """Inputs' policies plus retrieved ones; on conflict the documented (retrieved) value wins."""
        if self.retriever is None:
            return inputs.policies
        units = sorted({p.unit for p in inputs.twin.pools.values()})
        try:
            found = await self.retriever.values(units, inputs.twin.as_of)
        except Exception as e:        # no policy => constraints fail closed downstream
            self._log("policy.retrieval_failed", version, error=f"{type(e).__name__}: {e}")
            return inputs.policies
        merged = dict(inputs.policies.values)
        for k, v in found.values.items():
            if k in merged and merged[k].value != v.value:
                self._log("policy.conflict", version, key=k, input=merged[k], retrieved=v)
            merged[k] = v
        return PolicyBundle(merged)

    async def _passages(self, card_pool: str | None) -> tuple[Passage, ...]:
        if self.retriever is None or card_pool is None:
            return ()
        unit, kind = card_pool.split(":", 1)
        try:
            return tuple(await self.retriever.search(f"{unit} {_TOPIC.get(kind, kind)}", k=3))
        except Exception:
            return ()

    async def run(self, inputs: SharedInputs, context: str = "") -> Decision:
        """`context` (scenario, policy revision, run number...) is part of the decision id."""
        version = f"{__version__}|twin:{inputs.twin.version}|forecast:{inputs.forecast.model_version}"
        self._log("run.started", version)
        inputs = dataclasses.replace(inputs, policies=await self._policies(inputs, version))

        reports = tuple(await asyncio.gather(*(self._run_agent(a, inputs) for a in self.agents)))
        for r in reports:
            self._log("agent.completed" if not r.error else "agent.failed", version, agent=r.agent,
                      proposals=[p.id for p in r.proposals], notes=r.notes, error=r.error)

        unique = {p.id: p for r in reports for p in r.proposals}
        scored = await asyncio.gather(*(self._score(inputs, p) for p in unique.values()))
        resolution = self.arbitrator.resolve(inputs.twin, inputs.policies, scored)

        joint = None
        if self.simulator and resolution.accepted:
            try:
                joint = await self.simulator.evaluate(inputs.twin, resolution.accepted)
            except Exception:
                joint = None

        decision_id = "dec-" + hashlib.sha1(
            (inputs.twin.version + "|" + context + "|" + ",".join(sorted(p.id for p in resolution.accepted))).encode()
        ).hexdigest()[:10]
        card = build_card(decision_id, inputs, self.settings, reports,
                          resolution.accepted, resolution.rejected, joint)
        explanation = await self.explainer.explain(to_jsonable(card), self.settings.language)

        sim_run = inputs.simulation.run_id if inputs.simulation else "none"
        decision = Decision(
            id=decision_id, created_at=self.clock(),
            versions={"engine": __version__, "twin": inputs.twin.version,
                      "forecast_model": inputs.forecast.model_version, "simulation_baseline": sim_run},
            accepted=resolution.accepted, rejected=resolution.rejected, reports=reports,
            joint_impact=joint,
            rollback_actions=tuple(a for p in reversed(resolution.accepted) for a in p.inverse()),
            card=card, explanation=explanation,
            policy_passages=await self._passages(card.critical_bottleneck))
        self._log("arbitration.completed", version, decision=decision_id,
                  accepted=[p.id for p in resolution.accepted],
                  rejected={r.proposal.id: r.reasons for r in resolution.rejected})
        return decision
