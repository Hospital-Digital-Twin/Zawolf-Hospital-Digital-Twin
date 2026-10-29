"""AI assistant: local evidence-based answers, conversation context, refusals, optional grounded LLM path."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import ROOT, App
import hospital_agents.auth as _auth

_auth.ITERATIONS = 2000   # tests only
from hospital_agents import llm
from hospital_agents.auth import Principal

AS_OF = "2026-10-17T08:00"


class AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = App(ROOT / "data", Path(cls.tmp) / "rt", Path(cls.tmp) / "pol")
        cls.user = Principal("tester", "viewer")

    def setUp(self):
        self.app.assistant.reset("tester")
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def ask(self, text, lang="en"):
        return self.app.assistant_ask({"message": text, "as_of": AS_OF, "language": lang}, self.user)

    def test_works_without_a_key_and_says_where_every_number_comes_from(self):
        out = self.ask("How is ICU occupancy?")
        self.assertEqual((out["mode"], out["intent"], out["unit"]), ("local", "beds", "ICU"))
        self.assertIn("ICU: 0 of 12 beds are occupied", out["answer"])
        self.assertTrue(out["evidence"] and all(e["table"] and e["id"] in out["answer"] for e in out["evidence"]))
        self.assertNotIn("WARD:", out["answer"])                                      # asked about ICU only

    def test_numbers_match_the_backend_not_the_text_of_the_question(self):
        out = self.ask("ward occupancy")
        pools = self.app.twin.pools(self.app.parse_as_of(AS_OF))
        self.assertIn(f"{pools['WARD:beds'].committed} of {pools['WARD:beds'].total}", out["answer"])

    def test_follow_up_keeps_the_topic_and_the_unit(self):
        self.ask("How is the ICU?")
        follow = self.ask("and the ward?")
        self.assertEqual((follow["intent"], follow["unit"]), ("beds", "WARD"))
        topic = self.ask("what about it?")
        self.assertEqual(topic["intent"], "beds")
        self.assertEqual([h["role"] for h in self.app.assistant_history(self.user)["history"]], ["user", "assistant"] * 3)

    def test_arabic_questions_get_arabic_answers(self):
        out = self.ask("كيف حال إشغال الأسرة في العناية المركزة؟", "ar")
        self.assertEqual((out["intent"], out["unit"]), ("beds", "ICU"))
        self.assertIn("أسرة", out["answer"])
        self.assertEqual(self.ask("ماذا يعني WAPE؟", "ar")["intent"], "glossary")

    def test_each_topic_is_answered_from_data(self):
        for text, intent in (("what needs attention?", "attention"), ("how is the ER waiting time", "er"), ("staff and overtime", "staff"),
                             ("operating rooms delays", "or"), ("how accurate are the forecasts", "forecast"), ("what do the agents recommend", "recommend"),
                             ("pending decisions", "decisions"), ("data quality", "quality"), ("escalation policy for bed capacity", "sources")):
            out = self.ask(text)
            self.assertEqual(out["intent"], intent, text)
            self.assertTrue(out["evidence"], text)

    def test_glossary_explains_a_metric_without_inventing_numbers(self):
        out = self.ask("What does WAPE mean?")
        self.assertEqual(out["intent"], "glossary")
        self.assertIn("absolute", out["answer"])

    def test_it_refuses_actions_and_clinical_advice(self):
        act = self.ask("approve the pending decision now")
        self.assertEqual((act["intent"], act["evidence"]), ("actions", []))
        self.assertIn("cannot approve", act["answer"])
        self.assertEqual(len(self.app.store.records), 0)                              # nothing was created or changed
        clin = self.ask("what medication dose should this patient get")
        self.assertEqual(clin["intent"], "clinical")
        self.assertIn("cannot give clinical advice", clin["answer"])

    def test_unknown_questions_say_so_and_suggest_what_can_be_asked(self):
        out = self.ask("tell me a joke about the weather in Paris")
        self.assertEqual((out["intent"], out["evidence"]), ("unknown", []))
        self.assertTrue(out["suggestions"])

    def test_empty_message_is_rejected_and_conversations_are_per_user(self):
        from app import ApiError
        with self.assertRaises(ApiError):
            self.app.assistant_ask({"message": "  "}, self.user)
        with self.assertRaises(ApiError):
            self.app.assistant_ask({"message": "hi"}, None)
        self.ask("icu")
        other = Principal("someone-else", "viewer")
        self.assertEqual(self.app.assistant_history(other)["history"], [])
        self.app.assistant_reset(self.user)
        self.assertEqual(self.app.assistant_history(self.user)["history"], [])

    # ── optional LLM path ──
    def with_llm(self, reply):
        async def fake(system, user):
            fake.calls.append(json.loads(user))
            return reply(json.loads(user)) if callable(reply) else reply
        fake.calls = []
        return fake, mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}), mock.patch.object(llm, "completion", return_value=fake)

    def test_supported_llm_answer_is_used_and_receives_history_and_evidence_only(self):
        self.ask("How is the ICU?")
        fake, env, patch = self.with_llm(lambda p: json.dumps({"sentences": [{"text": p["evidence"][0]["text"], "evidence": ["E1"]}]}))
        with env, patch:
            out = self.ask("and now the ward?")
        self.assertEqual(out["mode"], "llm")
        self.assertEqual(fake.calls[0]["conversation"][0]["role"], "user")             # context was passed
        self.assertTrue(all(set(e) == {"id", "text"} for e in fake.calls[0]["evidence"]))
        self.assertIn("[E1]", out["answer"])

    def test_unsupported_llm_answers_fall_back_to_the_local_answer(self):
        for reply in (json.dumps({"sentences": [{"text": "Everything is fine and all patients are safe.", "evidence": ["E1"]}]}),
                      json.dumps({"sentences": [{"text": "ICU is full.", "evidence": []}]}), "just chatting"):
            fake, env, patch = self.with_llm(reply)
            with env, patch:
                out = self.ask("How is the ICU?")
            self.assertEqual((out["mode"], out["notes"]), ("llm_fallback", ["llm_unsupported"]))
            self.assertIn("ICU: 0 of 12", out["answer"])

    def test_provider_errors_fall_back_and_name_the_category(self):
        async def broken(system, user):
            raise llm.LLMError("rate_limit")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}), mock.patch.object(llm, "completion", return_value=broken):
            out = self.ask("How is the ICU?")
        self.assertEqual((out["mode"], out["notes"]), ("llm_fallback", ["llm_rate_limit"]))

    def test_no_llm_call_for_refusals_glossary_or_unknown_questions(self):
        fake, env, patch = self.with_llm("{}")
        with env, patch:
            for text in ("approve it", "what does WAPE mean", "tell me a joke about Paris", "what medication dose"):
                self.ask(text)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
