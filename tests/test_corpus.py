"""Corpus retriever tests. Synthetic corpus follows the ingestion schema; the real-file
tests run only if data/combined.json is present."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from hospital_agents import Orchestrator, Settings, SharedInputs
from hospital_agents.policies import (CompositeRetriever, CorpusRetriever, FolderPolicyRetriever,
                                      PolicyStoreError)

FIX = Path(__file__).parent / "fixtures"
REAL = Path(__file__).parent.parent / "data" / "combined.json"
run = asyncio.run
LONG = " ".join(["word"] * 25)


def corpus_dict(chunks):
    return {"meta": {"counts": {"chunks": len(chunks)}},
            "sources": [{"id": "s1", "title": "Doc One", "version": "1", "country": "EG"}],
            "chunks": chunks}


def chunk(i, text, status="official", source="s1"):
    return {"id": f"c{i}", "source_id": source, "section_id": "x", "heading": "H", "page": i,
            "language": "en", "status": status, "text": text}


class Tmp(unittest.TestCase):
    def corpus(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, f)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name


class Loading(Tmp):
    def test_filters_and_stats(self):
        r = CorpusRetriever(self.corpus(corpus_dict([
            chunk(1, "ICU escalation " + LONG),
            chunk(2, "Draft thing " + LONG, status="draft"),
            chunk(3, "Contents " + "........ 12 " * 30),
            chunk(4, "too short"),
            chunk(5, "bed\x07 capacity " + LONG)])))
        self.assertEqual(r.stats, {"loaded": 2, "dropped": {"status": 1, "toc": 1, "short": 1}, "sources": 1})
        self.assertNotIn("\x07", r.chunks[1]["text"])

    def test_bad_corpora_fail_loudly(self):
        with self.assertRaises(PolicyStoreError):
            CorpusRetriever(self.corpus({"nope": 1}))
        d = corpus_dict([chunk(1, LONG)]); d["meta"]["counts"]["chunks"] = 2
        with self.assertRaises(PolicyStoreError):
            CorpusRetriever(self.corpus(d))
        with self.assertRaises(PolicyStoreError):
            CorpusRetriever(self.corpus(corpus_dict([chunk(1, LONG, source="missing")])))

    def test_citation_carries_source_page_and_country(self):
        r = CorpusRetriever(self.corpus(corpus_dict([chunk(7, "ICU escalation " + LONG)])))
        p = run(r.search("ICU escalation"))[0]
        self.assertEqual(p.doc_id, "c7")
        self.assertIn("Doc One", p.source)
        self.assertIn("p.7", p.source)
        self.assertIn("EG", p.source)


class Wiring(Tmp):
    def inputs(self):
        d = json.loads((FIX / "scenario.json").read_text())
        d["policies"] = {}
        return SharedInputs.from_dict(d)

    def decide(self, retriever):
        return run(Orchestrator(Settings(language="en"), retriever=retriever).run(self.inputs()))

    def test_corpus_alone_gives_citations_but_never_numeric_constraints(self):
        corpus = CorpusRetriever(self.corpus(corpus_dict([chunk(1, "ICU bed capacity escalation " + LONG)])))
        d = self.decide(corpus)
        self.assertTrue(d.policy_passages)
        self.assertNotIn("bed", {p.agent for p in d.accepted})     # no structured ratio => fail closed

    def test_composite_uses_structured_numbers_and_corpus_text(self):
        corpus = CorpusRetriever(self.corpus(corpus_dict([chunk(1, "ICU bed capacity escalation " + LONG)])))
        d = self.decide(CompositeRetriever(FolderPolicyRetriever(FIX / "policies"), corpus))
        self.assertTrue({"bed", "staff"} <= {p.agent for p in d.accepted})
        self.assertTrue(any(p.doc_id == "c1" for p in d.policy_passages))


@unittest.skipUnless(REAL.exists(), "data/combined.json not present")
class RealCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = CorpusRetriever(REAL)

    def test_loads_and_reports(self):
        self.assertGreater(self.r.stats["loaded"], 900)
        self.assertEqual(self.r.stats["sources"], 2)

    def test_escalation_query_hits_patient_flow_policy(self):
        top = run(self.r.search("escalation bed capacity", 3))
        self.assertTrue(all(p.doc_id.startswith("src002") for p in top))

    def test_staffing_query_hits_gahar(self):
        top = run(self.r.search("hospital workforce staffing nurses skill mix", 5))
        self.assertTrue(any(p.doc_id.startswith("src001") for p in top))


if __name__ == "__main__":
    unittest.main()
