"""Groq connection against a local fake provider (no network, no real key)."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from hospital_agents import llm
from hospital_agents.explain import (LLMExplainer, TemplateExplainer, build_evidence, claim_problem, is_grounded,
                                     parse_structured, validate_structured)

FACTS = {"decision_id": "dec-1", "recommended_actions": ["a"], "rejected_count": 2, "critical_bottleneck": "ICU:beds",
         "expected_demand_change_pct": None, "unmet": {"ICU:beds": 32}, "waiting_minutes_saved": None, "confidence": None}


class Provider(BaseHTTPRequestHandler):
    """Scripted provider: each POST pops the next scripted (status, headers, body); GET /models lists models."""
    script: list = []
    seen: list = []
    models = ["openai/gpt-oss-120b", "other"]

    def _reply(self, status, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Provider.seen.append({"path": self.path, "auth": self.headers["Authorization"], "body": body})
        status, headers, out = Provider.script.pop(0) if Provider.script else (200, {}, {"choices": [{"message": {"content": "{}"}}]})
        self._reply(status, out, headers)

    def do_GET(self):
        Provider.seen.append({"path": self.path, "auth": self.headers["Authorization"]})
        self._reply(200, {"data": [{"id": m} for m in Provider.models]})

    def log_message(self, *a): pass


def ok(text):
    return (200, {}, {"choices": [{"message": {"content": text}}]})


def structured(*pairs):
    return json.dumps({"sentences": [{"text": t, "evidence": e} for t, e in pairs]})


class Groq(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        Provider.script, Provider.seen = [], []
        self.env = {"GROQ_API_KEY": "test-key", "GROQ_BASE_URL": f"http://127.0.0.1:{self.srv.server_port}/openai/v1"}
        self.sleeps = []
        self.client = llm.Client(sleep=self.sleeps.append)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    # ── configuration ──
    def test_without_a_key_nothing_is_configured_and_the_template_is_used(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(llm.config()["configured"])
            self.assertIsNone(llm.completion())
            self.assertIsInstance(llm.explainer(), TemplateExplainer)
            self.assertEqual(llm.status()["model"], "openai/gpt-oss-120b")
            self.assertEqual(self.client.test()["category"], "not_configured")
            with self.assertRaises(llm.LLMError) as cm:
                self.client.chat("s", "u")
            self.assertEqual(cm.exception.category, "not_configured")

    def test_env_file_is_loaded_without_overriding_real_variables(self):
        f = Path(tempfile.mkdtemp()) / ".env"
        f.write_text('# comment\nGROQ_API_KEY="from-file"\nGROQ_MODEL=file-model\nEMPTY=\n')
        with mock.patch.dict(os.environ, {"GROQ_MODEL": "from-real-env"}, clear=True):
            loaded = llm.load_env(f)
            self.assertEqual(loaded, ["GROQ_API_KEY"])
            self.assertEqual((os.environ["GROQ_API_KEY"], os.environ["GROQ_MODEL"]), ("from-file", "from-real-env"))
            self.assertNotIn("EMPTY", os.environ)
        self.assertEqual(llm.load_env(f.parent / "missing.env"), [])

    def test_shipped_env_files_contain_no_key(self):
        root = Path(__file__).parent.parent
        for name in (".env", ".env.example"):
            text = (root / name).read_text()
            self.assertRegex(text, r"(?m)^GROQ_API_KEY=\s*$", name)
        self.assertIn(".env", (root / ".gitignore").read_text().split())

    # ── requests, retries, errors ──
    def test_request_uses_the_configured_model_and_bearer_key(self):
        Provider.script = [ok("hello")]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(self.client.chat("sys", "user"), "hello")
        self.assertEqual(Provider.seen[0]["path"], "/openai/v1/chat/completions")
        self.assertEqual((Provider.seen[0]["auth"], Provider.seen[0]["body"]["model"]), ("Bearer test-key", "openai/gpt-oss-120b"))
        self.assertLessEqual(Provider.seen[0]["body"]["max_tokens"], 500)
        self.assertNotIn("test-key", json.dumps(llm.status()))

    def test_rate_limit_is_retried_honouring_retry_after_then_succeeds(self):
        Provider.script = [(429, {"Retry-After": "3"}, {"error": "slow"}), (503, {}, {}), ok("fine")]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(self.client.chat("s", "u"), "fine")
        self.assertEqual(self.sleeps, [3.0, 2.0])                                    # Retry-After first, then exponential backoff
        self.assertEqual((self.client.stats["retries"], len(Provider.seen)), (2, 3))

    def test_retries_are_bounded_and_the_failure_is_a_safe_category(self):
        Provider.script = [(500, {}, {"secret": "internal detail"})] * 5
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(llm.LLMError) as cm:
                self.client.chat("s", "u")
        self.assertEqual((cm.exception.category, len(Provider.seen)), ("server", 3))
        self.assertNotIn("internal detail", cm.exception.message)
        self.assertEqual(self.client.stats["last_error"], "server")

    def test_auth_and_model_errors_are_not_retried(self):
        for status, category in ((401, "auth"), (403, "auth"), (404, "model")):
            Provider.script, Provider.seen = [(status, {}, {})], []
            with mock.patch.dict(os.environ, self.env, clear=True):
                with self.assertRaises(llm.LLMError) as cm:
                    self.client.chat("s", f"u{status}")
            self.assertEqual((cm.exception.category, len(Provider.seen)), (category, 1))

    def test_unreachable_provider_and_timeouts_are_reported(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k", "GROQ_BASE_URL": "http://127.0.0.1:1/v1"}, clear=True):
            with self.assertRaises(llm.LLMError) as cm:
                self.client.chat("s", "u")
        self.assertEqual(cm.exception.category, "network")
        self.assertEqual(len(self.sleeps), 2)

    def test_identical_requests_are_cached_and_the_minute_budget_is_enforced(self):
        Provider.script = [ok("one"), ok("two"), ok("three")]
        env = {**self.env, "GROQ_MAX_CALLS_PER_MIN": "2"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.client.chat("s", "same"), "one")
            self.assertEqual(self.client.chat("s", "same"), "one")                    # cache: no second provider call
            self.assertEqual(self.client.chat("s", "other"), "two")
            with self.assertRaises(llm.LLMError) as cm:
                self.client.chat("s", "third")                                        # 2 calls used this minute
        self.assertEqual((cm.exception.category, len(Provider.seen), self.client.stats["cache_hits"]), ("budget", 2, 1))

    def test_oversized_prompts_are_truncated(self):
        Provider.script = [ok("x")]
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.client.chat("s", "y" * 50_000)
        self.assertLessEqual(len(Provider.seen[0]["body"]["messages"][1]["content"]), llm.MAX_INPUT_CHARS)

    # ── test connection ──
    def test_connection_test_reports_success_and_each_failure_reason(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            good = self.client.test()
            self.assertEqual((good["ok"], good["category"], good["models_available"]), (True, "ok", 2))
            self.assertEqual(Provider.seen[-1]["path"], "/openai/v1/models")
        with mock.patch.dict(os.environ, {**self.env, "GROQ_MODEL": "missing-model"}, clear=True):
            self.assertEqual(self.client.test()["category"], "model")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k", "GROQ_BASE_URL": "http://127.0.0.1:1/v1"}, clear=True):
            bad = self.client.test()
            self.assertEqual((bad["ok"], bad["category"]), (False, "network"))
            self.assertNotIn("127.0.0.1", json.dumps(bad))

    # ── explanations through the structured protocol ──
    def test_structured_answer_is_used_and_always_ends_with_the_approval_sentence(self):
        ev = build_evidence(FACTS, "en")
        Provider.script = [ok(structured((ev["E2"], ["E2"]), ("The waiting-time effect is unavailable.", ["E5"])))]
        with mock.patch.dict(os.environ, self.env, clear=True):
            out = asyncio.run(LLMExplainer(llm.completion()).explain(FACTS, "en"))
        self.assertEqual(out.source, "llm")
        self.assertTrue(out.text.endswith(ev["E7"]))

    def test_provider_outage_and_garbage_fall_back_to_the_template(self):
        for script in ([(500, {}, {})] * 3, [ok("not json at all")], [ok(structured(("Waiting drops by 77 minutes.", ["E5"])))]):
            Provider.script = list(script)
            client = llm.Client(sleep=lambda s: None)
            with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(llm, "client", client):
                out = asyncio.run(llm.explainer().explain(FACTS, "en"))
            self.assertEqual(out.source, "template")


class Grounding(unittest.TestCase):
    EV = build_evidence(FACTS, "en")

    def test_fabricated_claims_without_numbers_are_rejected(self):
        for text in ("All patients can now be safely discharged. No clinical risks remain.", "The ICU crisis is over.", "This decision guarantees shorter queues."):
            self.assertFalse(is_grounded(text, FACTS), text)

    def test_supported_paraphrases_pass(self):
        self.assertIsNone(claim_problem("The critical bottleneck is ICU:beds.", self.EV["E2"]))
        self.assertIsNone(claim_problem("Human approval is required before any execution.", self.EV["E7"]))

    def test_validation_requires_valid_cited_supported_sentences(self):
        ok_items = [{"text": self.EV["E2"], "evidence": ["E2"]}]
        self.assertEqual(validate_structured(ok_items, self.EV), [self.EV["E2"]])
        bad = [
            [{"text": self.EV["E2"], "evidence": []}],                                   # no citation
            [{"text": self.EV["E2"], "evidence": ["E99"]}],                              # unknown evidence id
            [{"text": "ICU:beds are short by 99 beds.", "evidence": ["E4"]}],            # number not in the cited evidence
            [{"text": "Waiting will drop sharply.", "evidence": ["E5"]}],                # claim not in the evidence
            [{"text": "Patients are safe.", "evidence": ["E2"]}],                        # assurance language
            [{"text": "x", "evidence": ["E2"]}] * 5,                                     # too many sentences
            [{"text": 7, "evidence": ["E2"]}], [], None]
        for items in bad:
            self.assertIsNone(validate_structured(items, self.EV) if items is not None else None, items)

    def test_a_number_must_come_from_the_evidence_it_cites_not_from_any_evidence(self):
        self.assertIsNotNone(claim_problem("ICU:beds 32 unmet.", self.EV["E2"]))            # 32 lives in E4, not E2
        self.assertIsNone(claim_problem("ICU:beds 32 unmet.", self.EV["E4"]))

    def test_parse_structured_accepts_fenced_json_only(self):
        self.assertEqual(len(parse_structured("```json\n" + structured(("a", ["E1"])) + "\n```")), 1)
        self.assertIsNone(parse_structured("plain text"))
        self.assertIsNone(parse_structured('{"other": 1}'))

    def test_arabic_evidence_supports_arabic_paraphrase(self):
        ev = build_evidence(FACTS, "ar")
        self.assertIsNone(claim_problem("نقطة الاختناق الحرجة: ICU:beds.", ev["E2"]))
        self.assertIsNotNone(claim_problem("جميع المرضى يمكن إخراجهم بأمان.", ev["E2"]))


if __name__ == "__main__":
    unittest.main()
