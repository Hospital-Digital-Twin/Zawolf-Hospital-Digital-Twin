"""Shared capacity-rebalancing logic used by the ER, Bed, Staff and Resource agents.

For every recipient pool the forecast peak demand is turned into a required total
(demand / target utilisation). The shortfall is covered from (1) headroom up to the pool's
known hard ceiling, then (2) donor units that still cover their *own* forecast demand.
Missing forecast or missing ceiling => no move, never a guess.
"""
from __future__ import annotations

from ..config import Settings
from ..contracts import PoolAdjustment, Proposal, SharedInputs, ceil_exact, make_id
from .base import AgentReport, Finding


def plan(agent: str, inputs: SharedInputs, settings: Settings, kind: str, *,
         recipients: set[str] | None = None, exclude: set[str] = frozenset(),
         expand: bool = False, donate: bool = True) -> AgentReport:
    pools = {p.unit: p for p in inputs.twin.pools.values() if p.kind == kind}
    target = settings.target_utilization.get(kind, 1.0)
    notes: list[str] = []

    def need(unit: str) -> tuple[float, int] | None:
        demand = inputs.forecast.peak(f"{unit}:{kind}:demand", settings.forecast_stat)
        return None if demand is None else (demand, ceil_exact(demand / target))

    surplus: dict[str, int] = {}
    for u, p in pools.items():
        n = need(u)
        if n is None:
            notes.append(f"{u}:{kind}: no demand forecast; unit excluded as donor")
        else:
            surplus[u] = max(0, p.total - max(p.committed, n[1]))

    proposals, findings = [], []
    for unit in sorted(pools):
        if (recipients is not None and unit not in recipients) or unit in exclude:
            continue
        n = need(unit)
        if n is None:
            continue
        demand, required = n
        pool = pools[unit]
        deficit = required - pool.total
        if deficit <= 0:
            continue
        findings.append(Finding(pool.key, pool.committed, demand, pool.total, deficit))

        remaining, donations = deficit, []
        if expand and pool.hard_max is not None:
            remaining -= min(remaining, max(0, pool.hard_max - pool.total))
        if donate and remaining:
            for d in sorted((u for u in surplus if u != unit and surplus[u] > 0),
                            key=lambda u: (-surplus[u], u)):
                take = min(remaining, surplus[d])
                donations.append(PoolAdjustment(pools[d].key, -take))
                surplus[d] -= take
                remaining -= take
                if not remaining:
                    break
        covered = deficit - remaining
        if not covered:
            notes.append(f"{pool.key}: deficit {deficit} but no headroom or donor surplus")
            continue
        actions = [PoolAdjustment(pool.key, covered), *donations]
        rationale = (f"{pool.key} forecast peak demand {demand:.1f} at target utilisation "
                     f"{target:g} requires {required}, current total {pool.total}",
                     f"deficit {deficit}, covered {covered}, unmet {remaining}")
        proposals.append(Proposal(
            id=make_id(agent, actions), agent=agent,
            title=f"{agent}: rebalance {pool.key} by +{covered}",
            actions=tuple(actions), rationale=rationale,
            evidence=(f"forecast:{inputs.forecast.model_version}",), unmet=remaining))
    return AgentReport(agent, tuple(proposals), tuple(findings), tuple(notes))

