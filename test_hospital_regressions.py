"""Regression tests defining REQUIRED fixes for AI Hospital Digital Twin.

These four tests are EXPECTED TO FAIL on the submitted prototype.
Copy this file into the project's tests/ directory, then run:
    python -m unittest discover -s tests -p 'test_hospital_regressions.py' -v
Do not 'fix' these tests by changing assertions; fix the application.
Authentication design may be adapted to your finalized session/JWT API.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = next((p for p in (HERE, HERE.parent, HERE / 'hospital_digital_twin_starter') if (p / 'app.py').exists()), HERE)
sys.path.insert(0, str(ROOT))

from app import App, ApiError


class HospitalCriticalRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = App(ROOT / 'data', self.root / 'runtime', self.root / 'policies')

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, app=None):
        """A real authenticated identity: a stored account that signs in with a password."""
        app = app or self.app
        if 'qa-manager' not in app.auth.users:
            app.auth.create_user('qa-manager', 'regression-pw-1', 'operations_manager')
        return app.auth.principal(app.auth.login('qa-manager', 'regression-pw-1')[0])

    def set_safe_test_policies(self):
        # TEST-ONLY policy; not hospital-approved or clinical guidance.
        for unit in ('ICU', 'WARD'):
            self.app.set_policy({'unit': unit, 'value': 40}, self.manager())

    def test_distinct_scenarios_must_not_overwrite_same_decision_id(self):
        icu = self.app.run({'unit': 'ICU', 'multiplier': 1})
        ward = self.app.run({'unit': 'WARD', 'multiplier': 1})
        self.assertNotEqual(icu['id'], ward['id'], 'Decision ID must include scenario identity')

    def test_policy_changes_invalidate_pending_decision(self):
        self.set_safe_test_policies()
        decision = self.app.run({'unit': 'ICU', 'multiplier': 12})
        self.assertTrue(decision['accepted'])
        self.app.set_policy({'unit': 'ICU', 'value': None}, self.manager())
        self.assertTrue(self.app.view(decision['id'])['stale'],
                        'A policy revision must make a pending decision stale')

    def test_role_string_alone_cannot_authorize_approval(self):
        self.set_safe_test_policies()
        decision = self.app.run({'unit': 'ICU', 'multiplier': 12})
        # No authenticated session or bearer credential is supplied.
        with self.assertRaises(ApiError, msg='User-controlled role strings are not credentials'):
            self.app.approve(decision['id'], {'role': 'Operations Manager'})

    def test_applied_decision_can_be_rolled_back_after_restart(self):
        self.set_safe_test_policies()
        decision = self.app.run({'unit': 'ICU', 'multiplier': 12})
        self.app.approve(decision['id'], {}, self.manager())
        self.assertEqual(len(self.app.applied), 1)
        restarted = App(ROOT / 'data', self.root / 'runtime', self.root / 'policies')
        # After implementing real auth, adapt the caller to use a valid test identity.
        restarted.rollback({}, self.manager(restarted))  # accounts persist; sessions do not, so sign in again
        self.assertFalse(restarted.applied, 'Rollback must survive process restarts')


if __name__ == '__main__':
    unittest.main()
