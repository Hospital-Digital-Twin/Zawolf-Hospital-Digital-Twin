"""Additional regression cases for Hospital Digital Twin.
Place next to app.py and run: python -m unittest test_additional_findings -v
These four tests intentionally FAIL with the supplied 2026-10-25 version.
They use temporary runtime/policy folders and do not call a real LLM or hospital.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import App, ApiError, ROOT
from hospital_agents.auth import Principal
from hospital_agents import llm
from hospital_agents.explain import is_grounded


class MoreRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(ROOT / 'data', Path(self.tmp.name) / 'runtime', Path(self.tmp.name) / 'policies')
        self.manager = Principal('regression_manager', 'operations_manager')

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, unit='ICU', mult=12):
        return self.app.run({'as_of':'2026-10-17T08:00', 'unit':unit, 'multiplier':mult}, self.manager)

    def set_test_policies(self):
        # TEST-ONLY policy (not hospital-approved). Added so that scenario C below contains a real action:
        # without any nurse-ratio policy every bed move is rejected as unverifiable, C would be an empty decision,
        # and approving an empty decision is now (correctly) refused by test_empty_decision_must_not_be_marked_applied.
        for unit in ('ICU', 'WARD'):
            self.app.set_policy({'unit': unit, 'value': 40}, self.manager)

    def test_sandbox_swap_same_count_stales_prior_pending_decision(self):
        self.set_test_policies()
        a = self.run_scenario('ICU', 12)
        self.app.approve(a['id'], {}, self.manager)
        b = self.run_scenario('ICU', 20)
        self.app.rollback({}, self.manager)
        c = self.run_scenario('WARD', 10)
        self.assertGreater(len(c['accepted']), 0)
        self.app.approve(c['id'], {}, self.manager)
        self.assertTrue(self.app.view(b['id'])['stale'], 'Pending decision cannot remain valid after a different sandbox state')
        with self.assertRaises(ApiError):
            self.app.approve(b['id'], {}, self.manager)

    def test_empty_decision_must_not_be_marked_applied(self):
        d = self.run_scenario('ICU', 1)
        self.assertEqual(len(d['accepted']), 0)
        # Either reject an approval attempt, or implement a status such as NO_ACTION.
        with self.assertRaises(ApiError):
            self.app.approve(d['id'], {}, self.manager)

    def test_unsupported_nonnumeric_llm_claim_is_not_grounded(self):
        text = 'All patients can now be safely discharged. No clinical risks remain.'
        facts = {'decision_id':'dec-example','recommended_actions':[],'rejected_count':0}
        self.assertFalse(is_grounded(text, facts), 'Number matching alone does not verify medical or operational claims')

    def test_fetching_alerts_does_not_call_llm(self):
        calls = []
        async def fake_complete(system, user):
            calls.append((system, user))
            return 'Requires human approval.'
        with patch.object(llm, 'completion', return_value=fake_complete):
            self.app.alerts({})
            self.app.alerts({})
        self.assertEqual(calls, [], 'Simple alerts refresh should not spend Groq tokens')


if __name__ == '__main__':
    unittest.main()
