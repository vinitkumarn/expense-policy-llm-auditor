"""Policy search: plain keyword BM25 in pure Python. No embeddings, no vector store.

search(query, k=3) returns [{clause_id, title, text}], best first. A query that is exactly a
clause id ("Appendix C", "4.3") returns that clause first, then the best BM25 matches.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from expaudit import config
from expaudit.policy import Clause, load_policy

K1 = 1.5     # how fast repeated words stop adding score
B = 0.75     # how much long clauses are penalised

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Lowercase words and numbers; keeps clause ids like "4.2" as one token."""
    return _TOKEN.findall(text.lower())


class PolicyIndex:
    """BM25 over each clause's title + text, built once."""

    def __init__(self, clauses: list[Clause]):
        self.clauses = list(clauses)
        self.docs = [Counter(tokenize(f"{c.title} {c.text}")) for c in self.clauses]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / len(self.lengths)
        n = len(self.docs)
        df = Counter(word for d in self.docs for word in d)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self.by_id = {c.clause_id.lower(): i for i, c in enumerate(self.clauses)}

    def score(self, query_tokens: list[str], i: int) -> float:
        doc, length, total = self.docs[i], self.lengths[i], 0.0
        for word in query_tokens:
            tf = doc.get(word, 0)
            if tf:
                total += self.idf[word] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self.avg_len))
        return total

    def search(self, query: str, k: int = config.RETRIEVAL_TOP_K) -> list[dict]:
        tokens = tokenize(query)
        scores = [self.score(tokens, i) for i in range(len(self.clauses))]
        # Highest score first; ties keep file order (deterministic).
        order = sorted(range(len(self.clauses)), key=lambda i: (-scores[i], i))
        order = [i for i in order if scores[i] > 0]
        exact = self.by_id.get(" ".join(query.split()).lower())
        if exact is not None:
            order = [exact] + [i for i in order if i != exact]
        return [_as_dict(self.clauses[i]) for i in order[:k]]


def _as_dict(c: Clause) -> dict:
    return {"clause_id": c.clause_id, "title": c.title, "text": c.text}


@lru_cache(maxsize=4)
def _index(path: str) -> PolicyIndex:
    return PolicyIndex(load_policy(Path(path)))


def search(query: str, k: int = config.RETRIEVAL_TOP_K,
           policy_path: Path = config.POLICY_PATH) -> list[dict]:
    return _index(str(policy_path)).search(query, k)
