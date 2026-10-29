"""Data contracts.

Inbound  : what the other components hand to the agents (twin snapshot, forecast,
           simulation baseline, policies).  This is a *proposed* contract (v0): the
           Data Engineer's real Data Contract replaces `from_dict` parsers only; nothing
           downstream changes.
Outbound : what the agents produce (actions, proposals, impact estimates).

Naming conventions the contract relies on
  pool key        "<UNIT>:<kind>"           e.g. "ICU:beds", "ICU:nurses", "WARD:doctors"
  forecast metric "<UNIT>:<kind>:demand"    e.g. "ICU:beds:demand"
  policy key      "nurse.max_patients_per_nurse:<UNIT>"
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, NamedTuple, Sequence


# ───────────────────────────── inbound ─────────────────────────────

class Urgency(str, Enum):
    URGENT = "urgent"
    NON_URGENT = "non_urgent"   # classification comes from the source system, never from us


@dataclass(frozen=True)
class Pool:
    """A countable, movable stock (beds, doctors, nurses, equipment) inside one unit."""
    key: str
    total: int
    committed: int = 0              # part that cannot be released (occupied / busy / mandatory)
    hard_max: int | None = None     # physical ceiling; None = unknown (treated as 'no headroom')

    @property
    def unit(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def kind(self) -> str:
        return self.key.split(":", 1)[1]


class SlotKey(NamedTuple):
    domain: str            # "OR" | "CLINIC"
    resource_id: str       # room / clinic
    start: datetime


@dataclass(frozen=True)
class Slot:
    key: SlotKey
    end: datetime
    capacity: int                        # units are the adapter's choice (minutes for OR/clinic slots)
    booked: int
    provider_ids: tuple[str, ...] = ()   # providers verified available for this slot

    @property
    def free(self) -> int:
        return self.capacity - self.booked


@dataclass(frozen=True)
class Booking:
    id: str
    slot: SlotKey
    urgency: Urgency
    provider_id: str | None = None
    needs_icu_bed: bool = False
    duration: int = 1                    # same unit as Slot.capacity


@dataclass(frozen=True)
class TwinSnapshot:
    as_of: datetime
    version: str
    pools: Mapping[str, Pool]
    slots: tuple[Slot, ...] = ()
    bookings: tuple[Booking, ...] = ()

    def slot(self, key: SlotKey) -> Slot | None:
        return next((s for s in self.slots if s.key == key), None)

    def booking(self, booking_id: str) -> Booking | None:
        return next((b for b in self.bookings if b.id == booking_id), None)


@dataclass(frozen=True)
class ForecastPoint:
    ts: datetime
    value: float
    lo: float | None = None
    hi: float | None = None


@dataclass(frozen=True)
class Forecast:
    model_version: str
    series: Mapping[str, tuple[ForecastPoint, ...]]
    confidence: float | None = None     # produced by the forecasting component

    @property
    def horizon_end(self) -> datetime | None:
        ends = [pts[-1].ts for pts in self.series.values() if pts]
        return max(ends) if ends else None

    def peak(self, metric: str, stat: str = "upper") -> float | None:
        pts = self.series.get(metric)
        if not pts:
            return None
        if stat == "upper":
            return max(p.hi if p.hi is not None else p.value for p in pts)
        return max(p.value for p in pts)


@dataclass(frozen=True)
class SimulationBaseline:
    run_id: str
    seed: int | None
    metrics: Mapping[str, float] = field(default_factory=dict)   # e.g. revenue_at_risk_egp


@dataclass(frozen=True)
class PolicyValue:
    value: float
    source: str            # RAG citation id; a number without a source is never accepted


@dataclass(frozen=True)
class PolicyBundle:
    values: Mapping[str, PolicyValue] = field(default_factory=dict)

    def get(self, key: str) -> PolicyValue | None:
        return self.values.get(key)


@dataclass(frozen=True)
class SharedInputs:
    twin: TwinSnapshot
    forecast: Forecast
    simulation: SimulationBaseline | None = None
    policies: PolicyBundle = field(default_factory=PolicyBundle)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SharedInputs":
        return cls(
            twin=_twin(d["twin"]),
            forecast=_forecast(d["forecast"]),
            simulation=_baseline(d["simulation"]) if d.get("simulation") else None,
            policies=PolicyBundle({k: PolicyValue(float(v["value"]), v["source"])
                                   for k, v in d.get("policies", {}).items()}),
        )


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _slot_key(d: Mapping[str, Any]) -> SlotKey:
    return SlotKey(d["domain"], d["resource_id"], _dt(d["start"]))


def _twin(d: Mapping[str, Any]) -> TwinSnapshot:
    pools = {k: Pool(k, int(v["total"]), int(v.get("committed", 0)), v.get("hard_max"))
             for k, v in d["pools"].items()}
    slots = tuple(Slot(_slot_key(s), _dt(s["end"]), int(s["capacity"]), int(s["booked"]),
                       tuple(s.get("provider_ids", ()))) for s in d.get("slots", ()))
    bookings = tuple(Booking(b["id"], _slot_key(b["slot"]), Urgency(b["urgency"]),
                             b.get("provider_id"), bool(b.get("needs_icu_bed", False)),
                             int(b.get("duration", 1)))
                     for b in d.get("bookings", ()))
    return TwinSnapshot(_dt(d["as_of"]), d["version"], pools, slots, bookings)


def _forecast(d: Mapping[str, Any]) -> Forecast:
    series = {m: tuple(ForecastPoint(_dt(p["ts"]), float(p["value"]), p.get("lo"), p.get("hi"))
                       for p in pts) for m, pts in d["series"].items()}
    return Forecast(d["model_version"], series, d.get("confidence"))


def _baseline(d: Mapping[str, Any]) -> SimulationBaseline:
    return SimulationBaseline(d["run_id"], d.get("seed"), dict(d.get("metrics", {})))


# ───────────────────────────── outbound ─────────────────────────────
# Actions are a closed, operational-only vocabulary. There is deliberately no action type
# that can express a diagnosis, triage or treatment decision.

@dataclass(frozen=True)
class PoolAdjustment:
    pool: str
    delta: int


@dataclass(frozen=True)
class Reschedule:
    booking_id: str
    from_slot: SlotKey
    to_slot: SlotKey


class AlertCode(str, Enum):
    REVENUE_AT_RISK = "revenue_at_risk"


@dataclass(frozen=True)
class Alert:
    code: AlertCode
    detail: str


Action = PoolAdjustment | Reschedule | Alert


@dataclass(frozen=True)
class ImpactEstimate:
    """Simulator-verified effect of a proposal (positive = improvement)."""
    run_id: str
    seed: int | None = None
    continuity_gain: float = 0.0        # disrupted-care events avoided (cancellations, boarding...)
    waiting_minutes_saved: float = 0.0
    cost_saved_egp: float = 0.0
    revenue_protected_egp: float = 0.0


@dataclass(frozen=True)
class Proposal:
    id: str
    agent: str
    title: str
    actions: tuple[Action, ...]
    rationale: tuple[str, ...]          # every number here was computed from the inputs
    evidence: tuple[str, ...] = ()      # forecast model version, policy sources
    unmet: int = 0                      # deficit this proposal could not cover
    impact: ImpactEstimate | None = None

    def with_impact(self, impact: ImpactEstimate | None) -> "Proposal":
        return dataclasses.replace(self, impact=impact)

    def inverse(self) -> tuple[Action, ...]:
        return invert(self.actions)


def action_from_dict(d: Mapping[str, Any]) -> Action:
    """Inverse of to_jsonable for actions (used to restore the persisted sandbox)."""
    if "pool" in d:
        return PoolAdjustment(d["pool"], int(d["delta"]))
    if "booking_id" in d:
        return Reschedule(d["booking_id"], _slot_key(d["from_slot"]), _slot_key(d["to_slot"]))
    return Alert(AlertCode(d["code"]), d["detail"])


def invert(actions: Sequence[Action]) -> tuple[Action, ...]:
    """Exact undo of a list of actions (alerts have no effect and no inverse)."""
    out: list[Action] = []
    for a in reversed(actions):
        if isinstance(a, PoolAdjustment):
            out.append(PoolAdjustment(a.pool, -a.delta))
        elif isinstance(a, Reschedule):
            out.append(Reschedule(a.booking_id, a.to_slot, a.from_slot))
    return tuple(out)


def make_id(agent: str, actions: Sequence[Action]) -> str:
    """Deterministic id: identical inputs always yield identical proposal ids."""
    return f"{agent}-{hashlib.sha1(repr(tuple(actions)).encode()).hexdigest()[:10]}"


def ceil_exact(x: float) -> int:
    return math.ceil(x - 1e-9)


def to_jsonable(o: Any) -> Any:
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: to_jsonable(getattr(o, f.name)) for f in dataclasses.fields(o)}
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, tuple) and hasattr(o, "_fields"):
        return {k: to_jsonable(v) for k, v in o._asdict().items()}
    if isinstance(o, Mapping):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in o]
    return o
