"""Tunable behaviour. Nothing here is a clinical or regulatory number: nurse ratios,
overtime limits etc. arrive as sourced policies (see contracts.PolicyBundle)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Settings:
    agent_timeout_s: float = 10.0
    # "upper" plans against the forecast's upper bound (safer); "point" against the point value.
    forecast_stat: str = "upper"
    # Target utilisation per pool kind (demand / capacity). 1.0 = no buffer, the neutral value.
    target_utilization: Mapping[str, float] = field(default_factory=dict)
    # Pool kinds handled by the Resource agent (equipment, rooms...). Empty = agent abstains.
    resource_kinds: tuple[str, ...] = ()
    er_unit: str = "ER"
    # OR agent: which bed pool its reschedules are meant to relieve, and an ASSUMPTION on how
    # many beds one rescheduled procedure frees. Calibrate against simulation.
    or_relief_pool: str = "ICU:beds"
    beds_per_procedure: float = 1.0
    # Finance agent alerts only above an explicit threshold supplied by the business.
    revenue_alert_threshold_egp: float | None = None
    # Tolerance per priority tier (continuity, waiting, financial) under which scores tie.
    score_tolerance: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # Deterministic tie-break between agents (earlier = preferred).
    agent_rank: tuple[str, ...] = ("er", "bed", "staff", "or", "appointment", "resource", "finance")
    language: str = "ar"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")
        d = dict(d)
        for k in ("resource_kinds", "score_tolerance", "agent_rank"):
            if k in d:
                d[k] = tuple(d[k])
        return cls(**d)

    @classmethod
    def from_file(cls, path: str | Path) -> "Settings":
        return cls.from_dict(json.loads(Path(path).read_text()))
