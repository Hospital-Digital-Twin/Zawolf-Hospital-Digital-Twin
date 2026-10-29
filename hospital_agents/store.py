"""Durable state: decisions, the sandbox and its revision live in ONE file (state.json).

A single atomic replace means the decisions and the sandbox can never disagree after a crash:
either the whole transition is on disk or none of it is. `revision` increases on every sandbox
change (apply or roll back) and never goes back, so "the sandbox looks the same as before"
cannot be confused with "the sandbox is unchanged" (apply A, roll back A, apply C all reach
the same list length with a different history).

Older installs wrote decisions.json and sandbox.json separately; they are migrated once.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .contracts import Action, action_from_dict, to_jsonable

MAX_RECORDS = 500
FORMAT = 1


def atomic_write(path: Path, text: str, private: bool = False) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


class DecisionStore:
    """records: decision id -> JSON dict. applied: the sandbox, oldest first. revision: sandbox change counter."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.file = self.folder / "state.json"
        self.records: dict[str, dict[str, Any]]
        self.applied: list[tuple[str, list[Action]]]
        self.revision: int
        self.seq: int
        self.repairs: list[str] = []
        if self.file.exists():
            self._load(_read(self.file, {}))
        else:
            self._migrate_legacy()
        self.reconcile()

    # ── loading ──
    def _load(self, d: dict) -> None:
        self.records = d.get("records", {})
        self.applied = [(i["decision_id"], [action_from_dict(a) for a in i["actions"]]) for i in d.get("applied", [])]
        self.revision = int(d.get("revision", len(self.applied)))
        self.seq = max([int(d.get("seq", 0)), *(r.get("seq", 0) for r in self.records.values())])

    def _migrate_legacy(self) -> None:
        records = _read(self.folder / "decisions.json", {})
        applied = _read(self.folder / "sandbox.json", [])
        if isinstance(applied, dict):                       # tolerate an intermediate format
            applied = applied.get("applied", [])
        self._load({"records": records, "applied": applied, "revision": len(applied)})
        if records or applied:
            self.save()

    def reconcile(self) -> None:
        """The sandbox is the source of truth; make the decision statuses agree with it."""
        applied_ids = {d for d, _ in self.applied}
        for did, rec in self.records.items():
            if did in applied_ids and rec["status"] != "APPLIED":
                self.repairs.append(f"{did}: {rec['status']} -> APPLIED (it is in the sandbox)")
                rec["status"] = "APPLIED"
            elif did not in applied_ids and rec["status"] == "APPLIED":
                self.repairs.append(f"{did}: APPLIED -> ROLLED_BACK (it is not in the sandbox)")
                rec["status"] = "ROLLED_BACK"
        if self.repairs:
            self.save()

    # ── mutation ──
    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def bump(self) -> int:
        self.revision += 1
        return self.revision

    def save(self) -> None:
        if len(self.records) > MAX_RECORDS:                 # keep applied history, drop the oldest others
            keep = {d for d, _ in self.applied}
            for did in sorted(self.records, key=lambda d: self.records[d].get("seq", 0)):
                if len(self.records) <= MAX_RECORDS:
                    break
                if did not in keep:
                    del self.records[did]
        atomic_write(self.file, json.dumps({
            "format": FORMAT, "revision": self.revision, "seq": self.seq, "records": self.records,
            "applied": [{"decision_id": d, "actions": to_jsonable(a)} for d, a in self.applied]}, ensure_ascii=False))
