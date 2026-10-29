from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from ..config import Settings
from ..contracts import Proposal, SharedInputs


@dataclass(frozen=True)
class Finding:
    """A detected shortfall; the orchestrator picks the critical bottleneck from these."""
    pool: str
    current: int          # committed today
    projected: float      # forecast peak demand
    total: int
    deficit: int


@dataclass(frozen=True)
class AgentReport:
    agent: str
    proposals: tuple[Proposal, ...] = ()
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()      # abstentions and missing-input explanations
    error: str | None = None


class Agent(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def propose(self, inputs: SharedInputs, settings: Settings) -> AgentReport:
        """Pure function of its inputs: same inputs, same proposals."""
