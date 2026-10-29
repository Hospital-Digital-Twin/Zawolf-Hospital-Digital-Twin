"""RAG tests. Policy files are PLACEHOLDERS (tests/fixtures/policies), not hospital policy."""
import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hospital_agents import Orchestrator, Settings, SharedInputs
from hospital_agents.audit import InMemoryAudit
from hospital_agents.policies import FolderPolicyRetriever, PolicyStoreError

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 1, 16, 8, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


def inputs_without_policies() -> SharedInputs:
    d = json.loads((FIX / "scenario.json").read_text())
    d["policies"] = {}
    return SharedInputs.from_dict(d)


def decide(folder, inp=None, **kw):
    orch = Orchestrator(Settings(language="en"), retriever=FolderPolicyRetriever(folder), **kw)
    return run(orch.run(inp or inputs_without_policies()))


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        shutil.copytree(FIX / "policies", self.dir / "p")
        self.p = self.dir / "p"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, body):
        (self.p / name).write_text(body, encoding="utf-8")

    def test_values_carry_their_source(self):
        b = run(FolderPolicyRetriever(self.p).values(["ICU"], NOW))
        v = b.get("nurse.max_patients_per_nurse:ICU")
        self.assertEqual(v.value, 2.0)
        self.assertIn("nurse-ratio-icu", v.source)
        self.assertIsNone(b.get("nurse.max_patients_per_nurse:WARD"))   # only requested units

    def test_policies_from_rag_drive_the_same_decision(self):
        d = decide(self.p)
        agents = {p.agent for p in d.accepted}
        self.assertTrue({"bed", "staff"} <= agents)
        self.assertTrue(any("nurse-ratio-icu" in a for a in d.card.assumptions))

    def test_removing_the_policy_blocks_the_change(self):
        (self.p / "nurse-ratio-icu.md").unlink()
        d = decide(self.p)
        self.assertNotIn("bed", {p.agent for p in d.accepted})
        self.assertTrue(any("unverifiable" in r for rej in d.rejected for r in rej.reasons))

    def test_expired_policy_is_ignored(self):
        self.write("nurse-ratio-icu.md", (self.p / "nurse-ratio-icu.md").read_text()
                   .replace("value: 2", "value: 2\nexpires: 2025-12-31"))
        self.assertNotIn("bed", {p.agent for p in decide(self.p).accepted})

    def test_malformed_and_contradictory_stores_fail_loudly(self):
        self.write("bad.md", "no header here")
        with self.assertRaises(PolicyStoreError):
            FolderPolicyRetriever(self.p)
        (self.p / "bad.md").unlink()
        self.write("dup.md", "---\nid: dup\nsource: s\nkey: nurse.max_patients_per_nurse\nunit: ICU\nvalue: 5\n---\nx")
        with self.assertRaises(PolicyStoreError):
            FolderPolicyRetriever(self.p)

    def test_policy_without_source_is_refused(self):
        self.write("nosrc.md", "---\nid: x\nkey: k\nunit: ICU\nvalue: 1\n---\nx")
        with self.assertRaises(PolicyStoreError):
            FolderPolicyRetriever(self.p)

    def test_search_english_and_arabic(self):
        r = FolderPolicyRetriever(self.p)
        self.assertEqual(run(r.search("ICU nurse ratio"))[0].doc_id, "nurse-ratio-icu")
        self.assertEqual(run(r.search("تخصيص الأسرة"))[0].doc_id, "bed-allocation-sop")
        self.assertEqual(run(r.search("zzz unrelated")), [])

    def test_decision_carries_citations_for_the_bottleneck(self):
        d = decide(self.p)
        self.assertEqual(d.card.critical_bottleneck, "ICU:beds")
        self.assertTrue(d.policy_passages)

    def test_retrieved_value_overrides_input_and_conflict_is_audited(self):
        raw = json.loads((FIX / "scenario.json").read_text())
        raw["policies"]["nurse.max_patients_per_nurse:ICU"] = {"value": 99, "source": "stale-input"}
        audit = InMemoryAudit()
        d = decide(self.p, SharedInputs.from_dict(raw), audit=audit)
        self.assertIn("policy.conflict", [e.action for e in audit.events])
        self.assertTrue(any("= 2 " in a for a in d.card.assumptions))


if __name__ == "__main__":
    unittest.main()
