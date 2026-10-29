"""HTTP API end to end with real sign-in: roles, lifecycle, staleness, persistence, validation."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import app as A  # noqa: E402
import hospital_agents.auth as _auth  # noqa: E402

_auth.ITERATIONS = 2000   # tests only: keep sign-ins fast; production uses 200 000

STRESS = {"unit": "ICU", "multiplier": 12, "language": "en"}
PASSWORDS = {"manager": "manager-pass-1", "analyst": "analyst-pass-1", "viewer": "viewer-pass-1"}


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.start()
        for name, role in (("manager", "operations_manager"), ("analyst", "analyst"), ("viewer", "viewer")):
            self.app.auth.users.pop(name, None)
            self.app.auth.create_user(name, PASSWORDS[name], role)
        self.tokens = {}

    def start(self):
        self.srv = A.make_server(ROOT / "data", 0, runtime=self.tmp / "rt", policy_dir=self.tmp / "pol")
        self.app, self.port = A.Handler.app, self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def tearDown(self):
        self.stop()
        shutil.rmtree(self.tmp)

    def call(self, path, body=None, who=None, raw=None, headers=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        h = dict(headers or {})
        if who:
            if who not in self.tokens:
                s, r = self.call("/api/login", {"username": who, "password": PASSWORDS[who]})
                self.tokens[who] = r["token"]
            h["Authorization"] = "Bearer " + self.tokens[who]
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="GET" if data is None else "POST", headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def set_policies(self):
        for u in ("ICU", "WARD"):
            self.assertEqual(self.call("/api/policies", {"unit": u, "value": 40}, "manager")[0], 200)

    # ── authentication / authorization ──
    def test_everything_but_login_needs_a_session(self):
        for path in ("/api/meta", "/api/overview", "/api/decisions", "/api/audit", "/api/status"):
            self.assertEqual(self.call(path)[0], 401, path)
        self.assertEqual(self.call("/api/run", STRESS)[0], 401)
        self.assertEqual(self.call("/api/meta", headers={"Authorization": "Bearer forged"})[0], 401)

    def test_role_in_the_request_body_is_ignored(self):
        self.set_policies()
        _, d = self.call("/api/run", STRESS, "analyst")
        s, e = self.call(f"/api/decisions/{d['id']}/approve", {"role": "Operations Manager"}, "analyst")
        self.assertEqual(s, 403)
        self.assertEqual(self.call(f"/api/decisions/{d['id']}/approve", {"role": "operations_manager"}, "viewer")[0], 403)
        self.assertEqual(self.call("/api/policies", {"unit": "ICU", "value": 3, "role": "Operations Manager"}, "analyst")[0], 403)
        self.assertEqual(self.call("/api/rollback", {"role": "Operations Manager"}, "viewer")[0], 403)
        self.assertEqual(self.call("/api/run", STRESS, "viewer")[0], 403)

    def test_wrong_password_and_lockout(self):
        self.assertEqual(self.call("/api/login", {"username": "manager", "password": "nope"})[0], 401)
        for _ in range(5):
            self.call("/api/login", {"username": "viewer", "password": "bad"})
        self.assertEqual(self.call("/api/login", {"username": "viewer", "password": PASSWORDS["viewer"]})[0], 429)

    def test_logout_ends_the_session(self):
        self.assertEqual(self.call("/api/meta", who="viewer")[0], 200)
        self.call("/api/logout", {}, "viewer")
        self.assertEqual(self.call("/api/meta", headers={"Authorization": "Bearer " + self.tokens["viewer"]})[0], 401)

    # ── lifecycle ──
    def test_full_decision_lifecycle(self):
        self.set_policies()
        _, d = self.call("/api/run", STRESS, "analyst")
        self.assertEqual((d["status"], d["stale"]), ("PENDING", False))
        self.assertTrue(any(p["agent"] == "bed" for p in d["accepted"]))
        s, a = self.call(f"/api/decisions/{d['id']}/approve", {}, "manager")
        self.assertEqual((s, a["status"], a["decided_by"]), (200, "APPLIED", "manager (Operations Manager)"))
        self.assertEqual(self.call(f"/api/decisions/{d['id']}/approve", {}, "manager")[0], 409)
        _, d2 = self.call("/api/run", STRESS, "analyst")
        self.assertFalse(any(p["agent"] == "bed" for p in d2["accepted"]))
        s, r = self.call("/api/rollback", {}, "manager")
        self.assertEqual((s, r["status"]), (200, "ROLLED_BACK"))
        self.assertEqual(self.call("/api/sandbox", who="viewer")[1]["applied"], [])
        actions = [e["action"] for e in self.call("/api/audit", who="viewer")[1]]
        for expected in ("decision.approved_and_applied_in_sandbox", "decision.rolled_back", "policy.set", "decision.created", "auth.login"):
            self.assertIn(expected, actions)
        self.assertEqual({e["actor"] for e in self.call("/api/audit", who="viewer")[1] if e["action"] == "decision.rolled_back"}, {"manager"})

    def test_decisions_survive_a_restart_and_can_be_rolled_back(self):
        self.set_policies()
        _, d = self.call("/api/run", STRESS, "analyst")
        self.call(f"/api/decisions/{d['id']}/approve", {}, "manager")
        self.stop()
        self.start()
        self.tokens.clear()
        listed = {x["id"]: x for x in self.call("/api/decisions", who="viewer")[1]}
        self.assertEqual(listed[d["id"]]["status"], "APPLIED")
        self.assertEqual(len(self.call("/api/sandbox", who="viewer")[1]["applied"]), 1)
        s, r = self.call("/api/rollback", {}, "manager")
        self.assertEqual((s, r["status"]), (200, "ROLLED_BACK"))
        self.assertEqual(self.call("/api/sandbox", who="viewer")[1]["applied"], [])

    def test_pending_decision_survives_a_restart_and_can_still_be_approved(self):
        self.set_policies()
        _, d = self.call("/api/run", STRESS, "analyst")
        self.stop()
        self.start()
        self.tokens.clear()
        self.assertEqual(self.call(f"/api/decisions/{d['id']}/approve", {}, "manager")[1]["status"], "APPLIED")

    def test_stale_after_a_sandbox_or_policy_change(self):
        self.set_policies()
        _, first = self.call("/api/run", STRESS, "analyst")
        _, other = self.call("/api/run", {**STRESS, "as_of": "2026-10-16T08:00"}, "analyst")
        self.assertNotEqual(first["id"], other["id"])
        self.call(f"/api/decisions/{other['id']}/approve", {}, "manager")
        s, err = self.call(f"/api/decisions/{first['id']}/approve", {}, "manager")
        self.assertEqual(s, 409)
        self.assertIn("sandbox changed", err["error"])
        self.call("/api/rollback", {}, "manager")
        _, third = self.call("/api/run", STRESS, "analyst")
        self.call("/api/policies", {"unit": "ICU", "value": 39}, "manager")
        s, err = self.call(f"/api/decisions/{third['id']}/approve", {}, "manager")
        self.assertEqual(s, 409)
        self.assertIn("policy changed", err["error"])
        self.assertTrue(self.call(f"/api/decisions/{third['id']}", who="viewer")[1]["stale"])

    def test_every_run_gets_its_own_id_and_identical_pending_runs_are_superseded(self):
        _, a = self.call("/api/run", STRESS, "analyst")
        _, b = self.call("/api/run", STRESS, "analyst")
        self.assertNotEqual(a["id"], b["id"])
        statuses = {x["id"]: x["status"] for x in self.call("/api/decisions", who="viewer")[1]}
        self.assertEqual((statuses[a["id"]], statuses[b["id"]]), ("SUPERSEDED", "PENDING"))
        self.assertEqual(self.call(f"/api/decisions/{a['id']}/approve", {}, "manager")[0], 409)

    def test_reject_records_reason_and_blocks_apply(self):
        _, d = self.call("/api/run", STRESS, "analyst")
        s, r = self.call(f"/api/decisions/{d['id']}/reject", {"reason": "not now"}, "manager")
        self.assertEqual((s, r["status"], r["note"]), (200, "REJECTED", "not now"))
        self.assertEqual(self.call(f"/api/decisions/{d['id']}/approve", {}, "manager")[0], 409)

    # ── simulation preview ──
    def test_simulation_preview_is_computed_and_does_not_store_anything(self):
        self.set_policies()
        s, sim = self.call("/api/simulate", STRESS, "analyst")
        self.assertEqual(s, 200)
        self.assertEqual(sim["bed_change"], {"ER": 0, "ICU": 15, "WARD": -15})
        self.assertGreater(sim["before"]["ICU"]["shortfall_beds"], sim["after"]["ICU"]["shortfall_beds"])
        self.assertEqual(sim["capacity_cost_change_egp_per_day"], 15 * 4200 - 15 * 900)
        self.assertEqual({u["metric"] for u in sim["unavailable"]}, {"Waiting time change", "Revenue at risk change", "Overtime change"})
        self.assertEqual(self.call("/api/decisions", who="viewer")[1], [])

    # ── pages return real data ──
    def test_pages(self):
        for path, key in (("/api/overview", "cards"), ("/api/capacity", "units"), ("/api/staff", "on_shift_now"),
                          ("/api/or", "by_room"), ("/api/forecasts", "series"), ("/api/quality", "checks"),
                          ("/api/alerts", "alerts"), ("/api/twin", "departments"), ("/api/health", "ok"), ("/api/status", "features"), ("/api/knowledge?q=escalation", "passages")):
            s, body = self.call(path, who="viewer")
            self.assertEqual(s, 200, path)
            self.assertIn(key, body, path)
        self.assertTrue(self.call("/api/knowledge?q=escalation", who="viewer")[1]["passages"])

    def test_alerts_report_the_missing_policy_then_clear_it(self):
        ids = lambda: {a["id"] for a in self.call("/api/alerts", who="viewer")[1]["alerts"]}
        self.assertIn("policy-missing", ids())
        for u in ("ER", "ICU", "WARD"):
            self.call("/api/policies", {"unit": u, "value": 6}, "manager")
        self.assertNotIn("policy-missing", ids())

    # ── decision rules from the review ──
    def test_a_decision_without_actions_can_never_be_applied(self):
        _, d = self.call("/api/run", {**STRESS, "multiplier": 1}, "analyst")
        self.assertEqual(d["accepted"], [])
        s, err = self.call(f"/api/decisions/{d['id']}/approve", {}, "manager")
        self.assertEqual(s, 409)
        self.assertIn("nothing to apply", err["error"])
        self.assertEqual(self.call("/api/sandbox", who="viewer")[1]["applied"], [])
        self.assertEqual(self.call(f"/api/decisions/{d['id']}/reject", {"reason": "nothing to do"}, "manager")[1]["status"], "REJECTED")

    def test_sandbox_revision_stales_decisions_even_when_the_applied_count_is_unchanged(self):
        self.set_policies()
        _, a = self.call("/api/run", STRESS, "analyst")
        self.call(f"/api/decisions/{a['id']}/approve", {}, "manager")
        _, pending = self.call("/api/run", {**STRESS, "multiplier": 11}, "analyst")
        self.call("/api/rollback", {}, "manager")
        _, c = self.call("/api/run", {"unit": "WARD", "multiplier": 10, "language": "en"}, "analyst")
        self.call(f"/api/decisions/{c['id']}/approve", {}, "manager")
        self.assertEqual(len(self.call("/api/sandbox", who="viewer")[1]["applied"]), 1)            # same length as when `pending` was made
        self.assertTrue(self.call(f"/api/decisions/{pending['id']}", who="viewer")[1]["stale"])
        self.assertEqual(self.call(f"/api/decisions/{pending['id']}/approve", {}, "manager")[0], 409)

    # ── LLM safety on the dashboard ──
    def test_refreshing_the_dashboard_never_calls_the_llm(self):
        boom = mock.Mock(side_effect=AssertionError("LLM called during a dashboard refresh"))
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "secret-key-123"}), mock.patch("hospital_agents.llm.client.chat", boom):
            for path in ("/api/overview", "/api/alerts", "/api/capacity", "/api/staff", "/api/or", "/api/forecasts", "/api/twin", "/api/status", "/api/health", "/api/decisions", "/api/quality"):
                self.assertEqual(self.call(path, who="viewer")[0], 200, path)
            self.assertEqual(self.call("/api/simulate", STRESS, "analyst")[0], 200)
        boom.assert_not_called()

    def test_the_api_key_never_appears_in_any_response_or_log(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "secret-key-123", "GROQ_BASE_URL": "http://127.0.0.1:1/v1"}):
            self.call("/api/llm-test", {}, "analyst")
            self.call("/api/assistant", {"message": "how is the ICU?"}, "viewer")
            self.call("/api/run", STRESS, "analyst")
            bodies = [json.dumps(self.call(p, who="manager")[1]) for p in ("/api/status", "/api/health", "/api/audit", "/api/decisions", "/api/assistant")]
        self.assertFalse([b for b in bodies if "secret-key-123" in b])
        self.assertNotIn("secret-key-123", (self.tmp / "rt" / "audit.jsonl").read_text())
        self.assertNotIn("secret-key-123", (self.tmp / "rt" / "state.json").read_text())

    def test_connection_test_reports_a_missing_key_and_is_not_open_to_viewers(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s, r = self.call("/api/llm-test", {}, "analyst")
            self.assertEqual((s, r["ok"], r["category"]), (200, False, "not_configured"))
            self.assertEqual(self.call("/api/llm-test", {}, "viewer")[0], 403)
            self.assertFalse(self.call("/api/status", who="viewer")[1]["llm"]["configured"])

    def test_assistant_endpoints_need_sign_in_and_keep_a_conversation(self):
        self.assertEqual(self.call("/api/assistant", {"message": "hi"})[0], 401)
        s, a = self.call("/api/assistant", {"message": "How is the ICU?", "language": "en"}, "viewer")
        self.assertEqual((s, a["mode"], a["intent"]), (200, "local", "beds"))
        self.assertEqual(len(self.call("/api/assistant", who="viewer")[1]["history"]), 2)
        self.call("/api/assistant/reset", {}, "viewer")
        self.assertEqual(self.call("/api/assistant", who="viewer")[1]["history"], [])
        self.assertEqual(self.call("/api/assistant", {"message": ""}, "viewer")[0], 400)

    # ── static files, headers, validation ──
    def test_dashboard_and_static_assets(self):
        base = f"http://127.0.0.1:{self.port}"
        with urllib.request.urlopen(base + "/") as r:
            html, csp = r.read().decode(), r.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertFalse([w for w in html.split() if w.startswith(("src=\"http", "href=\"http"))])
        for asset in ("app.css", "app.js", "charts.js", "i18n.js"):
            with urllib.request.urlopen(f"{base}/static/{asset}") as r:
                self.assertEqual(r.status, 200)
        for bad in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/missing.js", "/static/"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + bad)
            self.assertEqual(cm.exception.code, 404, bad)

    def test_validation_errors(self):
        bad = [("/api/run", {"multiplier": 999}), ("/api/run", {"unit": "OR"}), ("/api/run", {"as_of": "nope"}),
               ("/api/run", {"as_of": "2030-01-01T00:00"}), ("/api/simulate", {"multiplier": "x"}),
               ("/api/policies", {"unit": "ICU", "value": -1}), ("/api/policies", {"unit": "ICU", "value": "abc"}),
               ("/api/policies", {"unit": "XXX", "value": 2})]
        for path, body in bad:
            who = "manager" if "policies" in path else "analyst"
            self.assertEqual(self.call(path, body, who)[0], 400, (path, body))
        self.assertEqual(self.call("/api/run", raw=b"{not json", who="analyst")[0], 400)
        self.assertEqual(self.call("/api/run", raw=b"[1]", who="analyst")[0], 400)
        self.assertEqual(self.call("/api/nope", who="viewer")[0], 404)
        self.assertEqual(self.call("/api/decisions/DEC-x", who="viewer")[0], 404)
        self.assertEqual(self.call("/api/decisions/DEC-x/approve", {}, "manager")[0], 404)
        self.assertEqual(self.call("/api/rollback", {}, "manager")[0], 409)

    def test_policy_roundtrip_and_removal(self):
        self.call("/api/policies", {"unit": "ICU", "value": 2}, "manager")
        pol = self.call("/api/policies", who="viewer")[1]["nurse_ratio"]
        self.assertEqual((pol[0]["unit"], pol[0]["value"]), ("ICU", 2.0))
        self.assertIn("NOT an approved", pol[0]["source"])
        self.assertIn("manager", pol[0]["source"])
        self.call("/api/policies", {"unit": "ICU", "value": None}, "manager")
        self.assertEqual(self.call("/api/policies", who="viewer")[1]["nurse_ratio"], [])


if __name__ == "__main__":
    unittest.main()
