"""Hard constraints. Each check sees a *set* of proposals and returns violations naming the
proposals involved. Unverifiable means violated: a missing policy or missing pool never passes.

`joint=True` constraints only make sense for the final set (a bed increase and the nurse
increase that supports it are individually non-compliant but jointly fine); they are checked
after the greedy pass, with the lowest-ranked implicated proposals dropped until clean.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol, Sequence

from .contracts import (PoolAdjustment, PolicyBundle, Proposal, Reschedule, SlotKey,
                        TwinSnapshot, Urgency)


@dataclass(frozen=True)
class Violation:
    constraint: str
    proposals: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class CheckContext:
    snapshot: TwinSnapshot
    policies: PolicyBundle


class Constraint(Protocol):
    name: str
    joint: bool

    def check(self, ctx: CheckContext, proposals: Sequence[Proposal]) -> list[Violation]: ...


def _pool_deltas(proposals: Sequence[Proposal]) -> tuple[dict[str, int], dict[str, set[str]]]:
    delta: dict[str, int] = defaultdict(int)
    touched: dict[str, set[str]] = defaultdict(set)
    for p in proposals:
        for a in p.actions:
            if isinstance(a, PoolAdjustment):
                delta[a.pool] += a.delta
                touched[a.pool].add(p.id)
    return delta, touched


class PoolBounds:
    name, joint = "pool_bounds", False

    def check(self, ctx, proposals):
        out = []
        delta, touched = _pool_deltas(proposals)
        for key, d in delta.items():
            ids = tuple(sorted(touched[key]))
            pool = ctx.snapshot.pools.get(key)
            if pool is None:
                out.append(Violation(self.name, ids, f"{key}: unknown pool"))
                continue
            new = pool.total + d
            if new < max(0, pool.committed):
                out.append(Violation(self.name, ids,
                                     f"{key}: total would drop to {new}, below committed {pool.committed}"))
            if pool.hard_max is not None and new > pool.hard_max:
                out.append(Violation(self.name, ids, f"{key}: total {new} exceeds hard max {pool.hard_max}"))
        # Growth beyond a known ceiling is never implied: a pool without hard_max may only
        # grow inside a proposal that is capacity-neutral for that kind.
        for p in proposals:
            by_kind: dict[str, int] = defaultdict(int)
            for a in p.actions:
                if isinstance(a, PoolAdjustment):
                    by_kind[a.pool.split(":", 1)[1]] += a.delta
            for a in p.actions:
                if isinstance(a, PoolAdjustment) and a.delta > 0:
                    pool = ctx.snapshot.pools.get(a.pool)
                    if pool and pool.hard_max is None and by_kind[a.pool.split(":", 1)[1]] > 0:
                        out.append(Violation(self.name, (p.id,),
                                             f"{a.pool}: net growth without a known hard max"))
        return out


class SlotAvailability:
    name, joint = "slot_availability", False

    def check(self, ctx, proposals):
        out, claims, moved = [], defaultdict(set), defaultdict(set)
        snap = ctx.snapshot
        for p in proposals:
            for a in p.actions:
                if not isinstance(a, Reschedule):
                    continue
                b, dst = snap.booking(a.booking_id), snap.slot(a.to_slot)

                def bad(msg, p=p):
                    out.append(Violation(self.name, (p.id,), f"booking {a.booking_id}: {msg}"))

                if b is None:
                    bad("unknown booking")
                elif b.urgency is not Urgency.NON_URGENT:
                    bad("only non_urgent bookings may be moved")
                elif b.slot != a.from_slot:
                    bad("from_slot does not match the twin")
                elif dst is None:
                    bad("target slot does not exist")
                elif a.to_slot.domain != a.from_slot.domain or a.to_slot.start <= snap.as_of:
                    bad("target slot is in another domain or in the past")
                elif b.provider_id and b.provider_id not in dst.provider_ids:
                    bad(f"provider {b.provider_id} not verified available for target slot")
                else:
                    claims[a.to_slot].add(p.id)
                    moved[a.booking_id].add(p.id)
        for key, ids in claims.items():
            n = sum(snap.booking(a.booking_id).duration for p in proposals if p.id in ids
                    for a in p.actions if isinstance(a, Reschedule) and a.to_slot == key)
            if n > snap.slot(key).free:
                out.append(Violation(self.name, tuple(sorted(ids)),
                                     f"slot {key.resource_id}@{key.start.isoformat()}: "
                                     f"{n} claimed for {snap.slot(key).free} free"))
        for bid, ids in moved.items():
            if len(ids) > 1:
                out.append(Violation(self.name, tuple(sorted(ids)), f"booking {bid} moved by several proposals"))
        return out


class NurseRatio:
    """Joint check of staffed beds per nurse against a *sourced* policy; no policy, no change."""
    name, joint = "nurse_ratio", True
    POLICY = "nurse.max_patients_per_nurse"

    def check(self, ctx, proposals):
        out = []
        delta, touched = _pool_deltas(proposals)
        units = {k.split(":", 1)[0] for k in delta if k.split(":", 1)[1] in ("beds", "nurses")}
        for unit in sorted(units):
            ids = tuple(sorted(touched.get(f"{unit}:beds", set()) | touched.get(f"{unit}:nurses", set())))
            policy = ctx.policies.get(f"{self.POLICY}:{unit}")
            beds, nurses = ctx.snapshot.pools.get(f"{unit}:beds"), ctx.snapshot.pools.get(f"{unit}:nurses")
            if policy is None or beds is None or nurses is None:
                out.append(Violation(self.name, ids,
                                     f"{unit}: nurse-ratio policy or pools unavailable; change is unverifiable"))
                continue
            b, n = beds.total + delta.get(beds.key, 0), nurses.total + delta.get(nurses.key, 0)
            if n <= 0 or b / n > policy.value:
                out.append(Violation(self.name, ids,
                                     f"{unit}: {b} beds / {n} nurses exceeds {policy.value:g} "
                                     f"patients per nurse (policy {policy.source})"))
        return out


def default_constraints() -> list[Constraint]:
    return [PoolBounds(), SlotAvailability(), NurseRatio()]
