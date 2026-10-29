"""AI Hospital Digital Twin - agents + dashboard server.

    python app.py [--port 8000] [--data data] [--reset-credentials]     then open http://127.0.0.1:8000

Standard library only. Serves the dashboard (dashboard.html + static/) and a JSON API over the CSV
data pack. Every endpoint except /api/login needs a signed-in account; the role comes from the
account stored on the server, never from the request. Decisions, the sandbox, the audit log and
accounts are persisted in ./runtime; structured policies in ./policies. Nothing writes to data/.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from hospital_agents import Orchestrator, Settings, insights, llm
from hospital_agents.assistant import Assistant
from hospital_agents.audit import InMemoryAudit, JsonlAudit
from hospital_agents.auth import PERMISSIONS, Auth, AuthError, Principal
from hospital_agents.contracts import PoolAdjustment, Reschedule, action_from_dict, invert, to_jsonable
from hospital_agents.explain import TemplateExplainer
from hospital_agents.policies import CompositeRetriever, CorpusRetriever, FolderPolicyRetriever
from hospital_agents.ports import AuditEvent
from hospital_agents.store import DecisionStore
from hospital_agents.twin_data import BED_UNITS, Scenario, TwinData, apply_actions

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CONTENT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".woff2": "font/woff2"}
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class App:
    def __init__(self, data_dir: Path, runtime: Path = ROOT / "runtime", policy_dir: Path = ROOT / "policies") -> None:
        self.data_dir, self.runtime, self.policy_dir = Path(data_dir), Path(runtime), Path(policy_dir)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.policy_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.twin = TwinData(self.data_dir)
        corpus = self.data_dir / "combined.json"
        self.corpus = CorpusRetriever(corpus) if corpus.exists() else None
        self.audit = JsonlAudit(self.runtime / "audit.jsonl")
        self.auth = Auth(self.runtime / "users.json")
        self.store = DecisionStore(self.runtime)
        self.assistant = Assistant(self)
        self._alert_cache: dict[tuple, dict] = {}

    @property
    def applied(self):
        return self.store.applied

    # ───────────── helpers ─────────────
    def log(self, actor: str, action: str, version: str = "", **detail) -> None:
        self.audit.record(AuditEvent(actor, action, datetime.now(timezone.utc), version, to_jsonable(detail)))

    @staticmethod
    def _need(principal: Principal | None, permission: str) -> Principal:
        try:
            return Auth.require(principal, permission)
        except AuthError as e:
            raise ApiError(HTTPStatus(e.status), e.message) from None

    def parse_as_of(self, raw: str | None) -> datetime:
        try:
            as_of = datetime.fromisoformat(raw).replace(minute=0, second=0, microsecond=0) if raw else self.twin.default_as_of
        except (ValueError, TypeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "as_of must be an ISO datetime") from None
        lo, hi = datetime.fromisoformat(self.twin.first_day), datetime.fromisoformat(self.twin.last_day)
        if not lo <= as_of <= hi.replace(hour=23):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"as_of must lie within {lo.date()} and {hi.date()}")
        return as_of

    @staticmethod
    def parse_scenario(body: dict) -> Scenario:
        unit = body.get("unit", "ICU")
        try:
            mult = float(body.get("multiplier", 1.0))
        except (TypeError, ValueError):
            mult = math.nan
        if unit not in ("ICU", "WARD") or not (math.isfinite(mult) and 0.1 <= mult <= 50):
            raise ApiError(HTTPStatus.BAD_REQUEST, "unit must be ICU or WARD and multiplier within 0.1-50")
        return Scenario(unit, mult)

    def policy_fingerprint(self) -> str:
        """Changes whenever any structured policy file is added, edited or removed."""
        h = hashlib.sha256()
        for p in sorted(self.policy_dir.glob("*.md")):
            h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
        return h.hexdigest()

    def retriever(self):
        structured = FolderPolicyRetriever(self.policy_dir, extra=self.twin.policy_docs())
        return CompositeRetriever(structured, self.corpus) if self.corpus else structured

    def _orchestrator(self, language: str, audit, use_llm: bool = False) -> Orchestrator:
        """The LLM wording is used only when a user explicitly creates a decision. Dashboard refreshes, alerts
        and previews always use the deterministic template, so they never spend API quota."""
        return Orchestrator(Settings(language=language if language in ("ar", "en") else "ar"), retriever=self.retriever(),
                            explainer=llm.explainer() if use_llm else TemplateExplainer(), audit=audit)

    def _inputs(self, as_of: datetime, scenario: Scenario):
        return self.twin.inputs(as_of, scenario, [a for _, a in self.applied])

    # ───────────── read-only pages ─────────────
    def meta(self) -> dict:
        return {"first_day": self.twin.first_day, "last_day": self.twin.last_day, "default_as_of": self.twin.default_as_of.isoformat(),
                "units": list(BED_UNITS), "synthetic": True}

    def overview(self, q): return insights.overview(self.twin, self.parse_as_of(q.get("as_of", [None])[0]))
    def capacity(self, q): return insights.capacity(self.twin, self.parse_as_of(q.get("as_of", [None])[0]))
    def staff(self, q): return insights.staff(self.twin, self.parse_as_of(q.get("as_of", [None])[0]))
    def operating_rooms(self, q): return insights.operating_rooms(self.twin, self.parse_as_of(q.get("as_of", [None])[0]))
    def forecasts(self, q): return insights.forecasts(self.twin, self.parse_as_of(q.get("as_of", [None])[0]))

    def quality(self) -> dict:
        return insights.quality(self.twin, ROOT / "validation_report.json")

    def nurse_policy_units(self) -> set[str]:
        return {d.unit for d in FolderPolicyRetriever(self.policy_dir).docs if d.key == "nurse.max_patients_per_nurse"}

    def _alerts_for(self, as_of: datetime) -> dict:
        """Baseline agents run + alerts, cached per (moment, sandbox revision, policy revision). No LLM involved."""
        key = (as_of.isoformat(), self.store.revision, self.policy_fingerprint())
        with self.lock:
            if key not in self._alert_cache:
                decision = asyncio.run(self._orchestrator("en", InMemoryAudit()).run(self._inputs(as_of, Scenario())))
                items = insights.build_alerts(self.twin, as_of, decision, self.nurse_policy_units(), self.quality(), llm.config()["configured"])
                text = lambda a: (f"{a.pool.replace(':', ' ')} {a.delta:+d}" if isinstance(a, PoolAdjustment) else
                                  f"move {a.booking_id} to {a.to_slot.resource_id} on {a.to_slot.start:%d %b}" if isinstance(a, Reschedule) else "alert")
                self._alert_cache.clear() if len(self._alert_cache) > 32 else None
                self._alert_cache[key] = {
                    "as_of": as_of.isoformat(), "alerts": items, "needs_attention": sum(a["severity"] != "info" for a in items),
                    "unmet": dict(decision.card.unmet), "baseline": {"accepted": len(decision.accepted), "rejected": len(decision.rejected), "bottleneck": decision.card.critical_bottleneck},
                    "recommendations": [{"agent": p.agent, "summary": "; ".join(text(a) for a in p.actions), "actions": to_jsonable(p.actions), "rationale": list(p.rationale)} for p in decision.accepted],
                    "rejected": [{"agent": r.proposal.agent, "reasons": list(r.reasons)} for r in decision.rejected]}
            return self._alert_cache[key]

    def alerts(self, q: dict) -> dict:
        return self._alerts_for(self.parse_as_of(q.get("as_of", [None])[0]))

    def twin_map(self, q): return insights.twin_map(self.twin, self.parse_as_of(q.get("as_of", [None])[0]))

    def health(self) -> dict:
        return {"ok": True, "time": datetime.now(timezone.utc).isoformat(), "data": {"from": self.twin.first_day, "to": self.twin.last_day},
                "llm": {"configured": llm.config()["configured"]}, "sandbox_revision": self.store.revision,
                "pending": sum(1 for r in self.store.records.values() if r["status"] == "PENDING"), "synthetic": True}

    def llm_test(self, principal: Principal | None = None) -> dict:
        """A real connection check against the provider. Reads .env again first so a freshly added key works."""
        self._need(principal, "run")
        llm.load_env(ROOT / ".env")
        result = llm.client.test()
        self.log(principal.username, "llm.test", ok=result["ok"], category=result["category"])
        return result

    # ───────────── assistant ─────────────
    def assistant_ask(self, body: dict, principal: Principal | None = None) -> dict:
        p = self._need(principal, "read")
        message = str(body.get("message", "")).strip()
        if not message:
            raise ApiError(HTTPStatus.BAD_REQUEST, "message is required")
        as_of = self.parse_as_of(body.get("as_of"))
        with self.lock:
            out = self.assistant.ask(p.username, message, as_of, str(body.get("language", "ar")))
        self.log(p.username, "assistant.ask", intent=out["intent"], mode=out["mode"], notes=out["notes"])
        return out

    def assistant_history(self, principal: Principal | None = None) -> dict:
        p = self._need(principal, "read")
        return {"history": self.assistant.history(p.username), "mode": "llm" if llm.config()["configured"] else "local"}

    def assistant_reset(self, principal: Principal | None = None) -> dict:
        self.assistant.reset(self._need(principal, "read").username)
        return {"ok": True}

    def status(self) -> dict:
        q = self.quality()
        return {
            "llm": llm.status(),
            "features": [
                {"name": "Executive summary wording", "available": llm.config()["configured"],
                 "note": "LLM wording, accepted only if every sentence is supported by the evidence." if llm.config()["configured"] else "Needs GROQ_API_KEY. The deterministic template is used meanwhile."},
                {"name": "AI assistant: local answers", "available": True, "note": "Answers from the backend data by intent, with sources. Works without a key."},
                {"name": "AI assistant: LLM-assisted answers", "available": llm.config()["configured"], "note": "Re-words the same evidence; rejected if unsupported." if llm.config()["configured"] else "Needs GROQ_API_KEY."},
                {"name": "Natural-language what-if questions", "available": False, "note": "Not implemented. Scenarios are set with the controls."},
                {"name": "Simulation engine (waiting time, queues)", "available": False, "note": "No engine is connected; the pack's stored results are shown as reference only."},
                {"name": "Source search (BM25, Arabic and English)", "available": True, "note": "Runs locally on the corpus and the pack policies."},
                {"name": "Constraint checking and arbitration", "available": True, "note": "Deterministic; runs locally."}],
            "persistence": {"decisions": len(self.store.records), "applied_in_sandbox": len(self.applied), "sandbox_revision": self.store.revision, "repairs": self.store.repairs,
                            "audit_events": len(self.audit_tail(10_000)), "runtime_dir": str(self.runtime)},
            "corpus": getattr(self.corpus, "stats", None), "data": {"period": q["period"], "days": q["days"], "tables": q["tables"]},
            "quality": q["summary"], "generated_at": datetime.now(timezone.utc).isoformat()}

    def knowledge(self, q: dict) -> dict:
        query = (q.get("q", [""])[0] or "").strip()[:200]
        r = self.retriever()
        passages = asyncio.run(r.search(query, 8)) if query else []
        kind = lambda p: "policy value" if p.doc_id.startswith("dashboard-") else ("pack policy" if p.doc_id.startswith("POL-") else "corpus")
        sources = [{"id": s["id"], "title": s["title"], "version": s.get("version"), "country": s.get("country"),
                    "status": s.get("status"), "license": s.get("license_note") or s.get("license") or s.get("reuse") or None}
                   for s in (self.corpus.sources.values() if self.corpus else [])]
        values = [{"unit": d.unit, "key": d.key, "value": d.value, "source": d.source}
                  for d in FolderPolicyRetriever(self.policy_dir).docs if d.key]
        return {"query": query, "passages": [{"id": p.doc_id, "source": p.source, "text": p.text[:600], "score": p.score, "kind": kind(p)} for p in passages],
                "sources": sources, "corpus": getattr(self.corpus, "stats", None), "policy_values": values,
                "pack_policies": [{"id": d.id, "source": d.source, "text": d.text} for d in self.twin.policy_docs()]}

    # ───────────── what-if ─────────────
    def simulate(self, body: dict) -> dict:
        """Preview a scenario: run the agents on it, apply the accepted actions to a copy of the twin and
        report capacity arithmetic before/after. Nothing is stored. No queue/wait simulation exists."""
        as_of, scenario = self.parse_as_of(body.get("as_of")), self.parse_scenario(body)
        with self.lock:
            inputs = self._inputs(as_of, scenario)
            decision = asyncio.run(self._orchestrator(body.get("language", "en"), InMemoryAudit()).run(inputs))
            actions = [a for p in decision.accepted for a in p.actions]
            after = apply_actions(inputs.twin, actions)
        f = inputs.forecast

        def state(snap):
            rows = {}
            for unit in BED_UNITS:
                pool = snap.pools[f"{unit}:beds"]
                peak = f.peak(f"{unit}:beds:demand", "upper")
                rows[unit] = {"beds": pool.total, "occupied_now": pool.committed, "peak_demand_beds": None if peak is None else round(peak, 1),
                              "peak_utilisation_pct": None if peak is None or not pool.total else round(100 * peak / pool.total, 1),
                              "shortfall_beds": None if peak is None else max(0, math.ceil(peak - 1e-9) - pool.total)}
            return rows

        rates = {r["cost_category"]: float(r["rate_egp"]) for r in self.twin.t["cost_rates"]}
        day_rate = {"ICU": rates.get("ICU_BED_DAY"), "WARD": rates.get("BED_DAY")}
        b, a = state(inputs.twin), state(after)
        moved = {u: a[u]["beds"] - b[u]["beds"] for u in BED_UNITS}
        cost = None
        if all(day_rate.get(u) is not None for u in ("ICU", "WARD")):
            cost = round(sum(moved[u] * day_rate[u] for u in ("ICU", "WARD")), 0)
        rescheduled = [x for p in decision.accepted for x in p.actions if hasattr(x, "booking_id")]
        view = decision.to_dict()
        return {
            "as_of": as_of.isoformat(), "scenario": to_jsonable(scenario), "before": b, "after": a, "bed_change": moved,
            "capacity_cost_change_egp_per_day": cost,
            "cost_note": "Rate arithmetic from cost_rates: converted beds x the destination/source unit's daily bed rate. Assumes every converted bed is staffed; not a simulation.",
            "rescheduled_bookings": len(rescheduled),
            "unavailable": [
                {"metric": "Waiting time change", "reason": "No simulation engine is connected. The pack's stored ER queue results are not tied to these actions."},
                {"metric": "Revenue at risk change", "reason": "Needs a simulator to value each action; none is connected."},
                {"metric": "Overtime change", "reason": "No staffing demand forecast exists in the data pack."}],
            "pack_reference": [{"id": r["simulation_id"], "name": r["scenario_name"], "results": json.loads(r["results_json"])}
                               for r in self.twin.t["simulation_runs"]],
            "decision": {"accepted": view["accepted"], "rejected": view["rejected"], "card": view["card"], "reports": view["reports"]}}

    # ───────────── decisions ─────────────
    def run(self, body: dict, principal: Principal | None = None) -> dict:
        """Run the agents and store the result as a PENDING decision."""
        as_of, scenario = self.parse_as_of(body.get("as_of")), self.parse_scenario(body)
        actor = f"{principal.username} ({principal.label})" if principal else "system"
        with self.lock:
            fp, rev, seq = self.policy_fingerprint(), self.store.revision, self.store.next_seq()
            context = f"{scenario.unit}x{scenario.demand_multiplier:g}|policy:{fp[:12]}|run:{seq}"
            decision = asyncio.run(self._orchestrator(body.get("language", "ar"), self.audit, use_llm=True).run(self._inputs(as_of, scenario), context))
            view = decision.to_dict()
            key = (as_of.isoformat(), scenario.unit, scenario.demand_multiplier, rev, fp)
            for rec in self.store.records.values():       # an identical earlier pending run is replaced by this one
                if rec["status"] == "PENDING" and rec["key"] == list(key):
                    rec["status"] = "SUPERSEDED"
            view.update(status="PENDING", as_of=as_of.isoformat(), scenario=to_jsonable(scenario), sandbox_rev=rev, policy_fp=fp,
                        seq=seq, key=list(key), created_by=actor, decided_by=None, decided_at=None, note=None)
            self.store.records[decision.id] = view
            self.store.save()
            self.log(actor, "decision.created", decision=decision.id, scenario=scenario, accepted=len(view["accepted"]))
            return self.view(decision.id)

    def _record(self, decision_id: str) -> dict:
        rec = self.store.records.get(decision_id)
        if rec is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "unknown decision")
        return rec

    def _stale_reason(self, rec: dict) -> str | None:
        if rec["status"] != "PENDING":
            return None
        if rec["sandbox_rev"] != self.store.revision:
            return "the sandbox changed after this decision was computed"
        if rec["policy_fp"] != self.policy_fingerprint():
            return "a policy changed after this decision was computed"
        return None

    def view(self, decision_id: str) -> dict:
        rec = self._record(decision_id)
        reason = self._stale_reason(rec)
        return {**rec, "stale": reason is not None, "stale_reason": reason}

    def decisions(self) -> list[dict]:
        out = []
        for rec in sorted(self.store.records.values(), key=lambda r: -r["seq"]):
            reason = self._stale_reason(rec)
            out.append({"id": rec["id"], "status": rec["status"], "stale": reason is not None, "stale_reason": reason,
                        "created_at": rec["created_at"], "created_by": rec["created_by"], "decided_by": rec["decided_by"],
                        "decided_at": rec["decided_at"], "note": rec["note"], "as_of": rec["as_of"], "scenario": rec["scenario"],
                        "accepted": len(rec["accepted"]), "rejected": len(rec["rejected"]),
                        "bottleneck": rec["card"]["critical_bottleneck"], "summary": rec["explanation"]["text"]})
        return out

    def _decide(self, decision_id: str, principal, kind: str, note: str = "") -> dict:
        actor = self._need(principal, "decide")
        with self.lock:
            rec = self._record(decision_id)
            if rec["status"] != "PENDING":
                raise ApiError(HTTPStatus.CONFLICT, f"decision is already {rec['status']}")
            if kind == "approve":
                if (reason := self._stale_reason(rec)):
                    raise ApiError(HTTPStatus.CONFLICT, f"stale: {reason}; run the agents again")
                actions = [a for p in rec["accepted"] for a in map(action_from_dict, p["actions"])]
                if not any(isinstance(a, (PoolAdjustment, Reschedule)) for a in actions):
                    raise ApiError(HTTPStatus.CONFLICT, "nothing to apply: this decision has no accepted action that changes the sandbox. Reject it to close it")
                self.applied.append((decision_id, actions))
                self.store.bump()
                rec["status"] = "APPLIED"
            else:
                rec["status"] = "REJECTED"
            rec.update(decided_by=f"{actor.username} ({actor.label})", decided_at=datetime.now(timezone.utc).isoformat(), note=note[:300] or None)
            self.store.save()
            self.log(actor.username, "decision.approved_and_applied_in_sandbox" if kind == "approve" else "decision.rejected",
                     decision=decision_id, role=actor.role, note=note[:300])
            return self.view(decision_id)

    def approve(self, decision_id: str, body: dict, principal: Principal | None = None) -> dict:
        return self._decide(decision_id, principal, "approve", str(body.get("note", "")))

    def reject(self, decision_id: str, body: dict, principal: Principal | None = None) -> dict:
        return self._decide(decision_id, principal, "reject", str(body.get("reason", "")))

    def rollback(self, body: dict, principal: Principal | None = None) -> dict:
        """Undo the most recently applied decision (last in, first out keeps the sandbox consistent)."""
        actor = self._need(principal, "decide")
        with self.lock:
            if not self.applied:
                raise ApiError(HTTPStatus.CONFLICT, "nothing to roll back")
            decision_id, actions = self.applied.pop()
            self.store.bump()
            rec = self.store.records.get(decision_id)
            if rec:
                rec.update(status="ROLLED_BACK", decided_by=f"{actor.username} ({actor.label})",
                           decided_at=datetime.now(timezone.utc).isoformat(), note=str(body.get("note", ""))[:300] or None)
            self.store.save()
            self.log(actor.username, "decision.rolled_back", decision=decision_id, role=actor.role, inverse=invert(actions))
            return self.view(decision_id) if rec else {"id": decision_id, "status": "ROLLED_BACK"}

    def sandbox(self) -> dict:
        return {"applied": [{"decision_id": d, "actions": to_jsonable(a)} for d, a in self.applied]}

    def audit_tail(self, n: int = 200) -> list:
        f = self.runtime / "audit.jsonl"
        lines = f.read_text(encoding="utf-8").splitlines()[-n:] if f.exists() else []
        return [json.loads(l) for l in reversed(lines)]

    # ───────────── policies ─────────────
    # A structured nurse-ratio policy entered by an Operations Manager. It is stored as a policy file whose
    # source says so; it is NOT an approved hospital policy and the dashboard labels it accordingly.
    def policies(self) -> dict:
        r = FolderPolicyRetriever(self.policy_dir)
        return {"nurse_ratio": [{"unit": d.unit, "value": d.value, "source": d.source}
                                for d in r.docs if d.key == "nurse.max_patients_per_nurse"], "fingerprint": self.policy_fingerprint()[:12]}

    def set_policy(self, body: dict, principal: Principal | None = None) -> dict:
        actor = self._need(principal, "policy")
        unit = body.get("unit")
        if unit not in BED_UNITS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "unit must be ER, ICU or WARD")
        path = self.policy_dir / f"dashboard-nurse-ratio-{unit}.md"
        with self.lock:
            if body.get("value") in (None, ""):
                path.unlink(missing_ok=True)
                self.log(actor.username, "policy.removed", role=actor.role, unit=unit)
            else:
                try:
                    value = float(body["value"])
                except (TypeError, ValueError):
                    value = math.nan
                if not (math.isfinite(value) and value > 0):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "value must be a positive number (max patients per nurse)")
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                path.write_text(
                    f"---\nid: dashboard-nurse-ratio-{unit}\n"
                    f"source: Entered in the dashboard by {actor.username} ({actor.label}) at {stamp} - NOT an approved hospital policy\n"
                    f"key: nurse.max_patients_per_nurse\nunit: {unit}\nvalue: {value:g}\n---\n"
                    f"Maximum patients per nurse for {unit}, as entered by {actor.username}.\n", encoding="utf-8")
                self.log(actor.username, "policy.set", role=actor.role, unit=unit, value=value)
        return self.policies()


# ───────────────────────── HTTP layer ─────────────────────────

class Handler(BaseHTTPRequestHandler):
    app: App

    def _send(self, status: int, payload, ctype: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel: str) -> None:
        path = (STATIC / rel).resolve()
        if STATIC.resolve() not in path.parents or path.suffix not in CONTENT_TYPES or not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        self._send(200, path.read_bytes(), CONTENT_TYPES[path.suffix])

    def _principal(self) -> Principal | None:
        header = self.headers.get("Authorization", "")
        return self.app.auth.principal(header[7:] if header.startswith("Bearer ") else None)

    def _dispatch(self, method: str) -> None:
        url, a = urlparse(self.path), self.app
        parts = [p for p in url.path.split("/") if p]
        try:
            if method == "GET" and not parts:
                return self._send(200, (ROOT / "dashboard.html").read_bytes(), "text/html")
            if method == "GET" and parts[:1] == ["static"]:
                return self._static(unquote("/".join(parts[1:])))
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 100_000:
                    raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON") from None
                if not isinstance(body, dict):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "JSON object expected")
            if (method, parts) == ("POST", ["api", "login"]):
                try:
                    token, p = a.auth.login(str(body.get("username", "")), str(body.get("password", "")))
                except AuthError as e:
                    raise ApiError(HTTPStatus(e.status), e.message) from None
                a.log(p.username, "auth.login", role=p.role)
                return self._send(200, {"token": token, "user": self._user(p)})
            principal = self._principal()
            if principal is None:
                raise ApiError(HTTPStatus.UNAUTHORIZED, "sign in required")
            q = parse_qs(url.query)
            routes = {
                ("GET", "me"): lambda: self._user(principal), ("GET", "meta"): a.meta,
                ("GET", "overview"): lambda: a.overview(q), ("GET", "capacity"): lambda: a.capacity(q),
                ("GET", "staff"): lambda: a.staff(q), ("GET", "or"): lambda: a.operating_rooms(q),
                ("GET", "forecasts"): lambda: a.forecasts(q), ("GET", "alerts"): lambda: a.alerts(q),
                ("GET", "quality"): a.quality, ("GET", "status"): a.status, ("GET", "knowledge"): lambda: a.knowledge(q),
                ("GET", "health"): a.health, ("GET", "twin"): lambda: a.twin_map(q), ("GET", "assistant"): lambda: a.assistant_history(principal),
                ("POST", "assistant"): lambda: a.assistant_ask(body, principal), ("POST", "llm-test"): lambda: a.llm_test(principal),
                ("GET", "decisions"): a.decisions, ("GET", "sandbox"): a.sandbox, ("GET", "audit"): a.audit_tail,
                ("GET", "policies"): a.policies,
                ("POST", "simulate"): lambda: a.simulate(body) if a._need(principal, "run") else None,
                ("POST", "run"): lambda: a.run(body, a._need(principal, "run")),
                ("POST", "policies"): lambda: a.set_policy(body, principal),
                ("POST", "rollback"): lambda: a.rollback(body, principal),
                ("POST", "logout"): lambda: (a.auth.logout(self.headers.get("Authorization", "")[7:]), {"ok": True})[1],
            }
            if parts[:1] == ["api"] and len(parts) == 2 and (method, parts[1]) in routes:
                result = routes[(method, parts[1])]()
            elif (method, parts) == ("POST", ["api", "assistant", "reset"]):
                result = a.assistant_reset(principal)
            elif parts[:2] == ["api", "decisions"] and len(parts) == 3 and method == "GET":
                result = a.view(parts[2])
            elif method == "POST" and len(parts) == 4 and parts[:2] == ["api", "decisions"] and parts[3] in ("approve", "reject"):
                result = getattr(a, parts[3])(parts[2], body, principal)
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
            self._send(200, result)
        except ApiError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:                       # never leak a traceback to the browser
            self.log_error("unhandled: %r", e)
            self._send(500, {"error": "internal error"})

    @staticmethod
    def _user(p: Principal) -> dict:
        return {"username": p.username, "role": p.role, "role_label": p.label,
                "permissions": [k for k, roles in PERMISSIONS.items() if p.role in roles]}

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def log_message(self, fmt, *args): pass


def make_server(data_dir: Path, port: int = 8000, host: str = "127.0.0.1", *,
                runtime: Path = ROOT / "runtime", policy_dir: Path = ROOT / "policies") -> ThreadingHTTPServer:
    Handler.app = App(Path(data_dir), runtime, policy_dir)
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--reset-credentials", action="store_true", help="delete accounts and create new ones")
    args = ap.parse_args()
    loaded = llm.load_env(ROOT / ".env")                    # real environment variables always win over .env
    if args.reset_credentials:
        (ROOT / "runtime" / "users.json").unlink(missing_ok=True)
    server = make_server(Path(args.data), args.port)
    creds = Handler.app.auth.initial_credentials
    if creds:
        print("First run: accounts created (shown once; set HDT_MANAGER_PASSWORD etc. to choose your own):")
        for name, password in creds.items():
            print(f"  {name:<8} {password}")
    print(f"LLM: {'configured (' + llm.config()['model'] + ')' if llm.config()['configured'] else 'not configured, running without it (set GROQ_API_KEY in .env)'}")
    print(f"Dashboard: http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
