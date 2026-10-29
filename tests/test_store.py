"""State durability: one file, monotonic revision, crash safety, migration, reconciliation."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hospital_agents.contracts import PoolAdjustment
from hospital_agents.store import DecisionStore


def rec(did, status="PENDING", seq=1):
    return {"id": did, "status": status, "seq": seq}


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_decisions_sandbox_and_revision_are_saved_together(self):
        s = DecisionStore(self.dir)
        s.records["d1"] = rec("d1", "APPLIED")
        s.applied.append(("d1", [PoolAdjustment("ICU:beds", 2)]))
        s.bump()
        s.save()
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])             # one file, no temp leftovers
        again = DecisionStore(self.dir)
        self.assertEqual((again.revision, again.applied), (1, [("d1", [PoolAdjustment("ICU:beds", 2)])]))

    def test_revision_never_goes_back_even_when_the_applied_list_does(self):
        s = DecisionStore(self.dir)
        seen = []
        for step in range(3):
            s.applied.append((f"d{step}", []))
            seen.append(s.bump())
            s.applied.pop()
            seen.append(s.bump())
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(len(s.applied), 0)

    def test_a_crash_while_writing_leaves_the_previous_state_intact(self):
        s = DecisionStore(self.dir)
        s.records["d1"] = rec("d1")
        s.save()
        s.records["d2"] = rec("d2")
        with mock.patch("hospital_agents.store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                s.save()
        self.assertEqual(set(DecisionStore(self.dir).records), {"d1"})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])             # the temp file was cleaned up

    def test_legacy_two_file_state_is_migrated_once(self):
        (self.dir / "decisions.json").write_text(json.dumps({"d1": rec("d1", "APPLIED")}))
        (self.dir / "sandbox.json").write_text(json.dumps([{"decision_id": "d1", "actions": [{"pool": "ICU:beds", "delta": 1}]}]))
        s = DecisionStore(self.dir)
        self.assertEqual((s.revision, len(s.applied), s.records["d1"]["status"]), (1, 1, "APPLIED"))
        self.assertTrue((self.dir / "state.json").exists())

    def test_reconcile_repairs_statuses_that_disagree_with_the_sandbox(self):
        (self.dir / "state.json").write_text(json.dumps({
            "revision": 2, "seq": 2, "records": {"a": rec("a", "PENDING"), "b": rec("b", "APPLIED")},
            "applied": [{"decision_id": "a", "actions": [{"pool": "ICU:beds", "delta": 1}]}]}))
        s = DecisionStore(self.dir)
        self.assertEqual((s.records["a"]["status"], s.records["b"]["status"]), ("APPLIED", "ROLLED_BACK"))
        self.assertEqual(len(s.repairs), 2)
        self.assertEqual(json.loads((self.dir / "state.json").read_text())["records"]["b"]["status"], "ROLLED_BACK")

    def test_old_records_are_pruned_but_applied_ones_never(self):
        s = DecisionStore(self.dir)
        for i in range(520):
            s.records[f"d{i}"] = rec(f"d{i}", "REJECTED", i)
        s.records["d0"]["status"] = "APPLIED"
        s.applied.append(("d0", []))
        s.save()
        self.assertLessEqual(len(s.records), 500)
        self.assertIn("d0", s.records)


if __name__ == "__main__":
    unittest.main()
