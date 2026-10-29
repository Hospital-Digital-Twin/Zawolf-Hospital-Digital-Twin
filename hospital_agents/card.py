from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from .agents import AgentReport, Finding
from .arbitration import Rejection
from .config import Settings
from .contracts import (Action, Alert, ImpactEstimate, PoolAdjustment, Proposal, Reschedule,
                        SharedInputs, to_jsonable)
from .ports import Explanation, Passage


@dataclass(frozen=True)
class ExecutiveCard:
    decision_id: str
    critical_bottleneck: str | None
    expected_demand_change_pct: float | None
    waiting_minutes_saved: float | None
    revenue_at_risk_before_egp: float | None
    revenue_at_risk_after_egp: float | None
    recommended_actions: tuple[str, ...]
    rejected_count: int
    confidence: float | None
    assumptions: tuple[str, ...]
    unverified: tuple[str, ...]
    degraded_agents: tuple[str, ...]
    unmet: Mapping[str, int]            # pool -> deficit still open after the accepted actions
    approval_required_from: str = "Operations Manager"
    rollback_prepared: bool = False


@dataclass(frozen=True)
class Decision:
    id: str
    created_at: datetime
    versions: Mapping[str, str]
    accepted: tuple[Proposal, ...]
    rejected: tuple[Rejection, ...]
    reports: tuple[AgentReport, ...]
    joint_impact: ImpactEstimate | None
    rollback_actions: tuple[Action, ...]
    card: ExecutiveCard
    explanation: Explanation
    policy_passages: tuple[Passage, ...] = ()

    def to_dict(self) -> dict:
        return to_jsonable(self)


def describe(a: Action) -> str:
    if isinstance(a, PoolAdjustment):
        return f"{a.pool} {a.delta:+d}"
    if isinstance(a, Reschedule):
        return (f"move {a.booking_id} from {a.from_slot.resource_id}@{a.from_slot.start:%Y-%m-%d %H:%M} "
                f"to {a.to_slot.resource_id}@{a.to_slot.start:%Y-%m-%d %H:%M}")
    return f"alert: {a.detail}"


def build_card(decision_id: str, inputs: SharedInputs, settings: Settings,
               reports: Sequence[AgentReport], accepted: Sequence[Proposal],
               rejected: Sequence[Rejection], joint: ImpactEstimate | None) -> ExecutiveCard:
    findings: list[Finding] = [f for r in reports for f in r.findings]
    worst = max(findings, key=lambda f: (f.deficit / max(f.total, 1), f.pool.endswith(":beds"), f.pool), default=None)
    demand_pct = None
    if worst and worst.current > 0:
        demand_pct = round((worst.projected / worst.current - 1) * 100, 1)

    base = inputs.simulation.metrics.get("revenue_at_risk_egp") if inputs.simulation else None
    after = None if base is None or joint is None else base - joint.revenue_protected_egp

    verified = sum(1 for p in accepted if p.impact is not None)
    fc = inputs.forecast.confidence
    confidence = None
    if accepted and fc is not None and verified:
        confidence = round(fc * verified / len(accepted), 2)

    assumptions = [f"forecast {inputs.forecast.model_version}, planned on its {settings.forecast_stat} value",
                   f"twin snapshot {inputs.twin.version}"]
    assumptions += [f"target utilisation {k}={v:g}" for k, v in sorted(settings.target_utilization.items())]
    if any(p.agent == "or" for p in accepted):
        assumptions.append(f"each rescheduled procedure assumed to free {settings.beds_per_procedure:g} bed(s)")
    units = {a.pool.split(":", 1)[0] for p in accepted for a in p.actions if isinstance(a, PoolAdjustment)}
    assumptions += sorted(f"policy {k} = {v.value:g} (source {v.source})"
                          for k, v in inputs.policies.values.items() if k.rsplit(":", 1)[-1] in units)

    unverified = []
    if len(accepted) and verified < len(accepted):
        unverified.append(f"{len(accepted) - verified} accepted proposal(s) have no simulator-verified impact")
    if joint is None and accepted:
        unverified.append("combined impact of the accepted set was not simulated")
    if fc is None:
        unverified.append("forecast confidence not provided")

    covered: dict[str, int] = {}
    for p in accepted:
        for a in p.actions:
            if isinstance(a, PoolAdjustment) and a.delta > 0:
                covered[a.pool] = covered.get(a.pool, 0) + a.delta
    unmet = {f.pool: f.deficit - covered.get(f.pool, 0) for f in findings if f.deficit > covered.get(f.pool, 0)}

    return ExecutiveCard(
        decision_id=decision_id,
        critical_bottleneck=worst.pool if worst else None,
        expected_demand_change_pct=demand_pct,
        waiting_minutes_saved=joint.waiting_minutes_saved if joint else None,
        revenue_at_risk_before_egp=base, revenue_at_risk_after_egp=after,
        recommended_actions=tuple(f"{p.agent}: " + "; ".join(describe(a) for a in p.actions) for p in accepted),
        rejected_count=len(rejected), confidence=confidence,
        assumptions=tuple(assumptions), unverified=tuple(unverified),
        degraded_agents=tuple(r.agent for r in reports if r.error), unmet=unmet,
        rollback_prepared=bool(accepted))
