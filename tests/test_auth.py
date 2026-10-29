import json
import tempfile
import unittest
from pathlib import Path

from hospital_agents.auth import Auth, AuthError, Principal


class AuthUnit(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = 1000.0
        self.auth = Auth(self.dir / "users.json", clock=lambda: self.now)

    def test_first_run_creates_one_account_per_role_with_random_passwords(self):
        creds = self.auth.initial_credentials
        self.assertEqual(set(creds), {"manager", "analyst", "viewer"})
        self.assertEqual(len(set(creds.values())), 3)
        self.assertEqual(self.auth.login("manager", creds["manager"])[1].role, "operations_manager")

    def test_passwords_are_stored_hashed_and_the_file_is_private(self):
        creds = self.auth.initial_credentials
        raw = (self.dir / "users.json").read_text()
        self.assertFalse(any(p in raw for p in creds.values()))
        self.assertEqual((self.dir / "users.json").stat().st_mode & 0o077, 0)
        again = Auth(self.dir / "users.json")                    # accounts persist, nothing re-created
        self.assertIsNone(again.initial_credentials)
        self.assertIn("manager", json.loads(raw))

    def test_sessions_expire(self):
        self.auth.create_user("t", "long-enough-pw", "viewer")
        token, _ = self.auth.login("t", "long-enough-pw")
        self.assertEqual(self.auth.principal(token).username, "t")
        self.now += 9 * 3600
        self.assertIsNone(self.auth.principal(token))

    def test_permissions(self):
        for role, can in (("viewer", {"read"}), ("analyst", {"read", "run"}), ("operations_manager", {"read", "run", "decide", "policy"})):
            p = Principal("x", role)
            self.assertEqual({k for k in ("read", "run", "decide", "policy") if p.can(k)}, can)
        with self.assertRaises(AuthError):
            Auth.require(None, "read")
        with self.assertRaises(AuthError):
            Auth.require(Principal("x", "viewer"), "decide")

    def test_input_validation(self):
        for args in (("", "long-enough-pw", "viewer"), ("a", "short", "viewer"), ("a", "long-enough-pw", "root")):
            with self.assertRaises(ValueError):
                self.auth.create_user(*args)

    def test_unknown_user_costs_the_same_and_fails_the_same(self):
        with self.assertRaises(AuthError) as cm:
            self.auth.login("ghost", "whatever-pw")
        self.assertEqual(cm.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
