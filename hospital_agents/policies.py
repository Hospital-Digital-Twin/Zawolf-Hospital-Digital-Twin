"""Folder-based policy store + retriever (stdlib only).

Each policy is a Markdown file with a small header:

    ---
    id: nurse-ratio-icu               # unique
    source: <document, section, approval reference>    # required: no source, no policy
    key: nurse.max_patients_per_nurse # optional: makes the policy a numeric constraint
    unit: ICU                         # required with key
    value: 2                          # required with key
    expires: 2027-01-01               # optional; expired policies are ignored
    ---
    Free text of the policy (Arabic or English).

Numeric constraints come from the header only (deterministic, auditable). The free text is
indexed with BM25 for `search`. A malformed or contradictory store fails loudly at load time
instead of silently dropping a rule. Swap in a vector store by implementing PolicyRetriever.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from .contracts import PolicyBundle, PolicyValue
from .ports import Passage


class PolicyStoreError(ValueError):
    pass


@dataclass(frozen=True)
class PolicyDoc:
    id: str
    source: str
    text: str
    key: str | None = None
    unit: str | None = None
    value: float | None = None
    expires: date | None = None


def parse_policy(path: Path) -> PolicyDoc:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", raw, re.S)
    if not m:
        raise PolicyStoreError(f"{path.name}: missing '---' header")
    head: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip()
        if line.strip():
            k, sep, v = line.partition(":")
            if not sep:
                raise PolicyStoreError(f"{path.name}: bad header line {line!r}")
            head[k.strip()] = v.strip()
    for req in ("id", "source"):
        if not head.get(req):
            raise PolicyStoreError(f"{path.name}: '{req}' is required")
    numeric = {"key", "unit", "value"} & head.keys()
    if numeric and numeric != {"key", "unit", "value"}:
        raise PolicyStoreError(f"{path.name}: key, unit and value must be given together")
    try:
        value = float(head["value"]) if "value" in head else None
        expires = date.fromisoformat(head["expires"]) if "expires" in head else None
    except ValueError as e:
        raise PolicyStoreError(f"{path.name}: {e}") from None
    return PolicyDoc(head["id"], head["source"], m.group(2).strip(),
                     head.get("key"), head.get("unit"), value, expires)


_DIACRITICS = re.compile(r"[\u064B-\u0652\u0640]")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def tokenize(text: str) -> list[str]:
    t = _DIACRITICS.sub("", text.lower().translate(_AR_DIGITS))
    t = re.sub("[أإآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")
    return re.findall(r"\w+", t)


class BM25:
    K1, B = 1.5, 0.75

    def __init__(self, texts: Sequence[str]) -> None:
        self._tf = [Counter(tokenize(t)) for t in texts]
        self._len = [sum(c.values()) for c in self._tf]
        self._avg = sum(self._len) / len(texts) if texts else 0.0
        self._df = Counter(t for c in self._tf for t in c)

    def rank(self, query: str, k: int) -> list[tuple[int, float]]:
        n, terms, out = len(self._tf), set(tokenize(query)), []
        for i, tf in enumerate(self._tf):
            score = 0.0
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                    score += idf * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * self._len[i] / self._avg))
            if score > 0:
                out.append((i, round(score, 4)))
        return sorted(out, key=lambda x: (-x[1], x[0]))[:k]


class FolderPolicyRetriever:
    """Structured policies: the only source of numeric constraints."""

    def __init__(self, folder: str | Path, extra: Sequence[PolicyDoc] = ()) -> None:
        self.docs = [parse_policy(p) for p in sorted(Path(folder).glob("*.md"))] + list(extra)
        ids = Counter(d.id for d in self.docs)
        if dup := [i for i, n in ids.items() if n > 1]:
            raise PolicyStoreError(f"duplicate policy ids: {dup}")
        by_key: dict[tuple[str, str], PolicyDoc] = {}
        for d in self.docs:
            if d.key is None:
                continue
            prev = by_key.setdefault((d.key, d.unit), d)
            if prev is not d and prev.value != d.value and not (prev.expires or d.expires):
                raise PolicyStoreError(f"conflicting values for {d.key}:{d.unit} in {prev.id} and {d.id}")
        self._index = BM25([f"{d.id} {d.source} {d.text}" for d in self.docs])

    async def values(self, units: Sequence[str], as_of: datetime) -> PolicyBundle:
        today, wanted, out = as_of.date(), set(units), {}
        for d in self.docs:
            if d.key is None or d.unit not in wanted or (d.expires and d.expires <= today):
                continue
            k = f"{d.key}:{d.unit}"
            if k in out and out[k].value != d.value:
                raise PolicyStoreError(f"conflicting values in force for {k}")
            out[k] = PolicyValue(d.value, d.id + " | " + d.source)
        return PolicyBundle(out)

    async def search(self, query: str, k: int = 3) -> list[Passage]:
        return [Passage(self.docs[i].id, self.docs[i].source, self.docs[i].text, sc)
                for i, sc in self._index.rank(query, k)]


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class CorpusRetriever:
    """Text corpus produced by the ingestion pipeline (`combined.json`: sources + chunks).

    Free text only: it feeds citations and SOP lookup, never numeric constraints (the corpus
    carries no structured values, and numbers are not extracted from prose). Chunks whose
    status is not allowed, table-of-contents chunks and near-empty chunks are dropped at load;
    `stats` reports what was kept so ingestion problems are visible rather than silent.
    """

    def __init__(self, path: str | Path, statuses: Sequence[str] = ("official",), min_words: int = 20) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        try:
            self.sources = {s["id"]: s for s in data["sources"]}
            chunks = data["chunks"]
            declared = data["meta"]["counts"]["chunks"]
        except (KeyError, TypeError) as e:
            raise PolicyStoreError(f"{Path(path).name}: unexpected corpus schema ({e})") from None
        if declared != len(chunks):
            raise PolicyStoreError(f"corpus declares {declared} chunks but contains {len(chunks)}")
        if len({c["id"] for c in chunks}) != len(chunks):
            raise PolicyStoreError("duplicate chunk ids in corpus")
        dropped = Counter()
        self.chunks = []
        for c in chunks:
            if c["source_id"] not in self.sources:
                raise PolicyStoreError(f"chunk {c['id']} references unknown source {c['source_id']}")
            text = _CTRL.sub(" ", c["text"])
            if c["status"] not in statuses:
                dropped["status"] += 1
            elif text.count(".") / max(len(text), 1) > 0.15:      # dotted-leader table of contents
                dropped["toc"] += 1
            elif len(text.split()) < min_words:
                dropped["short"] += 1
            else:
                self.chunks.append({**c, "text": " ".join(text.split())})
        self.stats = {"loaded": len(self.chunks), "dropped": dict(dropped), "sources": len(self.sources)}
        self._index = BM25([f"{c['heading']} {c['text']}" for c in self.chunks])

    def _cite(self, c: dict) -> str:
        s = self.sources[c["source_id"]]
        return (f"{s['title']} ({s.get('version', 'n/a')}), p.{c['page']}, "
                f"{c['heading'][:60].strip()} [{s.get('country', '?')}, {c['status']}]")

    async def values(self, units: Sequence[str], as_of: datetime) -> PolicyBundle:
        return PolicyBundle()

    async def search(self, query: str, k: int = 3) -> list[Passage]:
        return [Passage(self.chunks[i]["id"], self._cite(self.chunks[i]), self.chunks[i]["text"], sc)
                for i, sc in self._index.rank(query, k)]


class CompositeRetriever:
    """Numeric constraints from the structured store; supporting text from both."""

    def __init__(self, structured: FolderPolicyRetriever, corpus: CorpusRetriever) -> None:
        self.structured, self.corpus = structured, corpus

    async def values(self, units: Sequence[str], as_of: datetime) -> PolicyBundle:
        return await self.structured.values(units, as_of)

    async def search(self, query: str, k: int = 3) -> list[Passage]:
        return (await self.structured.search(query, k) + await self.corpus.search(query, k))[:k]
