"""Seams to the rest of the platform. Each is a one-method Protocol so the real component
(simulation engine, LLM gateway, audit store) plugs in without touching agent logic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from .contracts import ImpactEstimate, PolicyBundle, Proposal, TwinSnapshot


class Simulator(Protocol):
    async def evaluate(self, snapshot: TwinSnapshot, proposals: Sequence[Proposal]) -> ImpactEstimate | None:
        """Before/after effect of applying `proposals` together to `snapshot`, with a fixed
        seed. Return None when the run cannot be completed."""


@dataclass(frozen=True)
class Explanation:
    text: str
    source: str        # "template" | "llm"
    grounded: bool


class Explainer(Protocol):
    async def explain(self, facts: Mapping[str, Any], language: str) -> Explanation: ...


@dataclass(frozen=True)
class AuditEvent:
    actor: str
    action: str
    time: datetime
    version: str
    detail: Mapping[str, Any]


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


@dataclass(frozen=True)
class Passage:
    """A retrieved policy/SOP text with its citation."""
    doc_id: str
    source: str
    text: str
    score: float


class PolicyRetriever(Protocol):
    async def values(self, units: Sequence[str], as_of: datetime) -> PolicyBundle:
        """Numeric constraints in force at `as_of`, each with its source. Exact lookup by
        key and unit: numbers are never extracted from free text by a model."""

    async def search(self, query: str, k: int = 3) -> list[Passage]:
        """Ranked passages for free-text questions (SOPs, escalation rules, explanations)."""
