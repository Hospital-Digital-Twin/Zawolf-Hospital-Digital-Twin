from __future__ import annotations

import json
from pathlib import Path

from .contracts import to_jsonable
from .ports import AuditEvent


class InMemoryAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class JsonlAudit:
    """Append-only: one JSON object per line (actor, action, time, version, detail)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(self, event: AuditEvent) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(to_jsonable(event), ensure_ascii=False) + "\n")
