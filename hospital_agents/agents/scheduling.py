"""Agents that move bookings: OR (relieve downstream bed pressure) and Appointment
(rebalance overbooked clinic slots). Both touch NON_URGENT bookings only."""
from __future__ import annotations

from ..config import Settings
from ..contracts import (Booking, Proposal, Reschedule, SharedInputs, Slot, SlotKey,
                         Urgency, ceil_exact, make_id)
from .base import Agent, AgentReport, Finding


def _target(slots: list[Slot], used: dict[SlotKey, int], booking: Booking, *,
            after, resource_id: str | None = None) -> Slot | None:
    """Earliest free slot after `after` where the booking's provider is verified available."""
    for s in sorted(slots, key=lambda s: s.key.start):
        if s.key.start <= after or s.key == booking.slot:
            continue
        if resource_id is not None and s.key.resource_id != resource_id:
            continue
        if s.free - used.get(s.key, 0) < booking.duration:
            continue
        if booking.provider_id and booking.provider_id not in s.provider_ids:
            continue
        return s
    return None


def _proposal(agent: str, b: Booking, to: SlotKey, why: tuple[str, ...], evidence: tuple[str, ...]) -> Proposal:
    actions = (Reschedule(b.id, b.slot, to),)
    return Proposal(make_id(agent, actions), agent, f"{agent}: move booking {b.id}",
                    actions, why, evidence)


class ORAgent(Agent):
    """When the relief pool (default ICU beds) is forecast short, postpone non-urgent
    procedures that need a bed there into free OR slots beyond the forecast horizon."""
    name = "or"

    async def propose(self, inputs: SharedInputs, settings: Settings) -> AgentReport:
        key = settings.or_relief_pool
        pool = inputs.twin.pools.get(key)
        unit, kind = key.split(":", 1)
        demand = inputs.forecast.peak(f"{key}:demand", settings.forecast_stat)
        horizon = inputs.forecast.horizon_end
        if pool is None or demand is None or horizon is None:
            return AgentReport(self.name, notes=(f"{key}: pool or forecast missing; abstaining",))
        required = ceil_exact(demand / settings.target_utilization.get(kind, 1.0))
        deficit = required - pool.total
        if deficit <= 0:
            return AgentReport(self.name)
        finding = Finding(key, pool.committed, demand, pool.total, deficit)

        as_of = inputs.twin.as_of
        candidates = sorted((b for b in inputs.twin.bookings
                             if b.slot.domain == "OR" and b.urgency is Urgency.NON_URGENT
                             and b.needs_icu_bed and as_of < b.slot.start <= horizon),
                            key=lambda b: (b.slot.start, b.id))
        limit = ceil_exact(deficit / settings.beds_per_procedure)
        free = [s for s in inputs.twin.slots if s.key.domain == "OR"]
        used: dict[SlotKey, int] = {}
        proposals, notes = [], []
        for b in candidates[:limit]:
            slot = _target(free, used, b, after=horizon)
            if slot is None:
                notes.append(f"booking {b.id}: no free OR slot after horizon with provider available")
                continue
            used[slot.key] = used.get(slot.key, 0) + b.duration
            proposals.append(_proposal(
                self.name, b, slot.key,
                (f"{key} forecast peak demand {demand:.1f} exceeds capacity {pool.total} (deficit {deficit})",
                 f"booking {b.id} is non_urgent and flagged needs_icu_bed",
                 f"assumed beds freed per procedure: {settings.beds_per_procedure:g}"),
                (f"forecast:{inputs.forecast.model_version}",)))
        return AgentReport(self.name, tuple(proposals), (finding,), tuple(notes))


class AppointmentAgent(Agent):
    """Moves non-urgent bookings out of overbooked clinic slots into the nearest later free
    slot of the same clinic where their provider is available."""
    name = "appointment"

    async def propose(self, inputs: SharedInputs, settings: Settings) -> AgentReport:
        slots = [s for s in inputs.twin.slots if s.key.domain == "CLINIC"]
        used: dict[SlotKey, int] = {}
        proposals, notes = [], []
        for s in sorted(slots, key=lambda s: s.key.start):
            excess = s.booked - s.capacity
            if excess <= 0:
                continue
            movable = sorted((b for b in inputs.twin.bookings
                              if b.slot == s.key and b.urgency is Urgency.NON_URGENT),
                             key=lambda b: b.id)
            to_move, freed = [], 0
            for b in movable:
                if freed >= excess:
                    break
                to_move.append(b)
                freed += b.duration
            if freed < excess:
                notes.append(f"slot {s.key.resource_id}@{s.key.start.isoformat()}: overbooked by "
                             f"{excess} but only {freed} is movable (non_urgent)")
            for b in to_move:
                to = _target(slots, used, b, after=s.key.start, resource_id=s.key.resource_id)
                if to is None:
                    notes.append(f"booking {b.id}: no later free slot in {s.key.resource_id}")
                    continue
                used[to.key] = used.get(to.key, 0) + b.duration
                proposals.append(_proposal(
                    self.name, b, to.key,
                    (f"slot {s.key.resource_id}@{s.key.start.isoformat()} booked {s.booked} "
                     f"over capacity {s.capacity}",),
                    (f"twin:{inputs.twin.version}",)))
        return AgentReport(self.name, tuple(proposals), notes=tuple(notes))
