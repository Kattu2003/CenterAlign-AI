"""BM25 retrieval over three separate memory layers (plan §11.12):
authored handbook chunks, active learned facts, and past task episodes.

BM25Plus (from rank-bm25) instead of BM25Okapi: Okapi's IDF goes to zero or
below for terms that appear in most documents, which on a tiny corpus (one
learned fact!) means "no match". A document must also share at least one
non-stopword token with the query to be returned (see DECISIONS.md D11).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from rank_bm25 import BM25Plus

from karya.company import Company, HandbookChunk
from karya.memory.store import Episode, Fact, list_episodes, list_facts

STOPWORDS = set(
    "a an and are as at be by do does for from has have i in into is it its me my of on or our please so that the their them then "
    "there this to was we what when which who will with you your can could would should may might must just also any all".split()
)
ENTITY_BOOST = 2.0


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


@dataclass
class Hit:
    kind: str          # handbook | fact | episode
    id: str            # ap-01#5 | F-0001 | T-0004
    title: str
    text: str
    score: float
    provenance: str = ""


@dataclass
class Retrieved:
    handbook: list[Hit] = field(default_factory=list)
    facts: list[Hit] = field(default_factory=list)
    episodes: list[Hit] = field(default_factory=list)

    def ids(self) -> list[str]:
        return [h.id for h in self.handbook + self.facts + self.episodes]


def _rank(query_tokens: list[str], docs: list[list[str]], boosts: list[float] | None = None, k: int = 4) -> list[tuple[int, float]]:
    if not docs or not query_tokens:
        return []
    scores = BM25Plus(docs).get_scores(query_tokens)
    query_set = set(query_tokens)
    ranked = []
    for i, doc in enumerate(docs):
        if not query_set & set(doc):
            continue  # require real overlap
        ranked.append((i, float(scores[i]) + (boosts[i] if boosts else 0.0)))
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked[:k]


class Retriever:
    def __init__(self, company: Company, conn: sqlite3.Connection) -> None:
        self.company = company
        self.conn = conn
        self._chunks: list[HandbookChunk] = company.chunks()
        self._chunk_tokens = [tokenize(f"{c.doc_title} {c.heading} {c.text}") for c in self._chunks]

    def handbook(self, query: str, k: int = 4) -> list[Hit]:
        ranked = _rank(tokenize(query), self._chunk_tokens, k=k)
        return [Hit("handbook", self._chunks[i].id, f"{self._chunks[i].doc_title} — {self._chunks[i].heading}", self._chunks[i].text, s) for i, s in ranked]

    def facts(self, query: str, k: int = 3) -> list[Hit]:
        facts: list[Fact] = list_facts(self.conn)
        q = tokenize(query)
        docs = [tokenize(f.statement + " " + " ".join(f.entities)) for f in facts]
        # Facts whose entities overlap the request get a boost (plan §11.12).
        boosts = [ENTITY_BOOST if set(q) & set(tokenize(" ".join(f.entities))) else 0.0 for f in facts]
        return [Hit("fact", facts[i].id, "learned fact", facts[i].statement, s, facts[i].provenance()) for i, s in _rank(q, docs, boosts, k)]

    def episodes(self, query: str, k: int = 2) -> list[Hit]:
        episodes: list[Episode] = list_episodes(self.conn)
        docs = [tokenize(f"{e.request} {e.summary}") for e in episodes]
        return [
            Hit("episode", episodes[i].task_id, f"past task ({episodes[i].outcome})", f"Request: {episodes[i].request}\nOutcome: {episodes[i].summary}", s)
            for i, s in _rank(tokenize(query), docs, k=k)
        ]

    def expand_primary(self, hits: list[Hit]) -> list[Hit]:
        """The whole best-matching procedure, then the other top chunks. A model that sees
        only fragments of an SOP can skip a step (e.g. "label the email Processed")."""
        if not hits:
            return hits
        primary = self._chunks[next(i for i, c in enumerate(self._chunks) if c.id == hits[0].id)].doc_id
        full = [Hit("handbook", c.id, f"{c.doc_title} — {c.heading}", c.text, hits[0].score) for c in self._chunks if c.doc_id == primary]
        return full + [h for h in hits if not h.id.startswith(primary + "#")]

    def retrieve(self, query: str, k_handbook: int = 4, k_facts: int = 3, k_episodes: int = 2, expand: bool = False) -> Retrieved:
        handbook = self.handbook(query, k_handbook)
        if expand:
            handbook = self.expand_primary(handbook)
        return Retrieved(handbook, self.facts(query, k_facts), self.episodes(query, k_episodes))
