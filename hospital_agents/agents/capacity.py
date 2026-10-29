"""Agents that rebalance countable pools: ER (surge headroom), Bed, Staff, Resource."""
from __future__ import annotations

from ..config import Settings
from ..contracts import SharedInputs
from . import rebalance
from .base import Agent, AgentReport


class ERAgent(Agent):
    """Owns ER bed pressure. Expands into known physical headroom only; it never takes
    capacity from inpatient units (that trade-off belongs to the Bed agent)."""
    name = "er"

    async def propose(self, inputs: SharedInputs, settings: Settings) -> AgentReport:
        return rebalance.plan(self.name, inputs, settings, "beds",
                              recipients={settings.er_unit}, expand=True, donate=False)


class BedAgent(Agent):
    """Reallocates inpatient beds between units; ER is handled by its own agent."""
    name = "bed"

    async def propose(self, inputs: SharedInputs, settings: Settings) -> AgentReport:
        return rebalance.plan(self.name, inputs, settings, "beds", exclude={settings.er_unit})


class StaffAgent(Agent):
    """Moves doctors and nurses toward forecast staff need. Ratio compliance is NOT decided
    here: the arbitration layer verifies it against sourced policy."""
    name = "staff"

    async def propose(self, inputs: SharedInputs, settings: Settings) -> AgentReport:
        reports = [rebalance.plan(self.name, inputs, settings, kind) for kind in ("doctors", "nurses")]
        return AgentReport(self.name,
                           tuple(p for r in reports for p in r.proposals),
                           tuple(f for r in reports for f in r.findings),
                           tuple(n for r in reports for n in r.notes))


class ResourceAgent(Agent):
    """Equipment and shared rooms. Kinds are configured; with none configured it abstains."""
    name = "resource"

    async def propose(self, inputs: SharedInputs, settings: Settings) -> AgentReport:
        if not settings.resource_kinds:
            return AgentReport(self.name, notes=("no resource_kinds configured; abstaining",))
        reports = [rebalance.plan(self.name, inputs, settings, k) for k in settings.resource_kinds]
        return AgentReport(self.name,
                           tuple(p for r in reports for p in r.proposals),
                           tuple(f for r in reports for f in r.findings),
                           tuple(n for r in reports for n in r.notes))
