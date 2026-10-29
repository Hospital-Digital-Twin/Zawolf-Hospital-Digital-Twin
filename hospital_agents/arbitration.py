"""Arbitration: hard-constraint gate, then strict priority ranking, then an explicit
accept/reject verdict with a recorded reason for every proposal.

Priority order (from the architecture): 1 safety/hard constraints (a gate, never traded),
2 care continuity, 3 waiting time, 4 cost & revenue. Tiers 2-4 compare lexicographically on
simulator-verified impact; unverified impact counts as zero and is flagged downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .config import Settings
from .constraints import CheckContext, Constraint, Violation, default_constraints
from .contracts import PolicyBundle, Proposal, TwinSnapshot


@dataclass(frozen=True)
class Rejection:
    proposal: Proposal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    accepted: tuple[Proposal, ...]
    rejected: tuple[Rejection, ...]


class Arbitrator:
    def __init__(self, settings: Settings, constraints: Sequence[Constraint] | None = None):
        self.settings = settings
        self.constraints = list(constraints) if constraints is not None else default_constraints()

    def rank_key(self, p: Proposal):
        i = p.impact
        tiers = (i.continuity_gain, i.waiting_minutes_saved,
                 i.cost_saved_egp + i.revenue_protected_egp) if i else (0.0, 0.0, 0.0)
        bucketed = tuple(-(round(v / t) if t > 0 else v) for v, t in zip(tiers, self.settings.score_tolerance))
        rank = self.settings.agent_rank
        agent = rank.index(p.agent) if p.agent in rank else len(rank)
        return (*bucketed, agent, p.id)

    def _violations(self, ctx: CheckContext, props: Sequence[Proposal], joint: bool) -> list[Violation]:
        return [v for c in self.constraints if joint or not c.joint for v in c.check(ctx, props)]

    def resolve(self, snapshot: TwinSnapshot, policies: PolicyBundle,
                proposals: Sequence[Proposal]) -> Resolution:
        ctx = CheckContext(snapshot, policies)
        ordered = sorted(proposals, key=self.rank_key)
        accepted: list[Proposal] = []
        rejected: list[Rejection] = []

        for p in ordered:
            if not p.actions:
                rejected.append(Rejection(p, ("proposal has no actions",)))
                continue
            found = [v for v in self._violations(ctx, [*accepted, p], joint=False) if p.id in v.proposals]
            if found:
                rejected.append(Rejection(p, tuple(f"{v.constraint}: {v.message}" for v in found)))
            else:
                accepted.append(p)

        # Joint pass: drop the lowest-ranked implicated proposal until the final set is clean.
        while True:
            found = self._violations(ctx, accepted, joint=True)
            if not found:
                break
            implicated = {pid for v in found for pid in v.proposals}
            culprit = next((p for p in reversed(accepted) if p.id in implicated), None)
            if culprit is None:       # violation names no accepted proposal: refuse everything it covers
                break
            reasons = tuple(f"{v.constraint}: {v.message}" for v in found if culprit.id in v.proposals)
            accepted.remove(culprit)
            rejected.append(Rejection(culprit, ("removed in joint check - " + "; ".join(reasons),)))
        return Resolution(tuple(accepted), tuple(rejected))
