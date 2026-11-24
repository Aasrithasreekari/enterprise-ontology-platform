"""Okapi BM25 lexical retrieval with identifier-aware tokenization, plus a per-tenant index cache."""

from __future__ import annotations

import math
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "is", "it", "of", "on", "or",
    "that", "the", "this", "to", "was", "what", "when", "where", "which", "who", "why", "with", "does", "do", "i",
}


# Light suffix stripping (longest first). The same rules apply to documents and queries, so word
# forms meet: authenticates / authenticated / authentication → "authentic"; services → "servic".
_SUFFIXES = ("ational", "ation", "ating", "ated", "ates", "ing", "ed", "es", "s")
# Common enterprise abbreviations, mapped onto the stem of their full form (applied after stemming).
_ABBREVIATIONS = {"auth": "authentic", "authn": "authentic", "k8": "kubernet", "k8s": "kubernet", "db": "databas",
                  "prod": "production", "repo": "repository", "doc": "document", "env": "environment"}


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3 and not (suffix == "s" and token.endswith("ss")):
            token = token[: -len(suffix)]
            break
    if len(token) > 4 and token.endswith("e"):  # service/services, database/databases
        token = token[:-1]
    return _ABBREVIATIONS.get(token, token)


def tokenize(text: str) -> list[str]:
    """'payment-processor posts journals' → ['payment', 'processor', 'post', 'journal']."""
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


@dataclass(frozen=True)
class Hit:
    doc_id: Hashable
    score: float


class BM25Index:
    def __init__(self, documents: Iterable[tuple[Hashable, str]], k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._ids: list[Hashable] = []
        self._tfs: list[Counter[str]] = []
        self._lens: list[int] = []
        df: Counter[str] = Counter()
        for doc_id, text in documents:
            tf = Counter(tokenize(text))
            self._ids.append(doc_id)
            self._tfs.append(tf)
            self._lens.append(sum(tf.values()))
            df.update(tf.keys())
        n = len(self._ids)
        self._avgdl = (sum(self._lens) / n) if n else 0.0
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self._postings: dict[str, list[int]] = {}
        for i, tf in enumerate(self._tfs):
            for t in tf:
                self._postings.setdefault(t, []).append(i)

    def __len__(self) -> int:
        return len(self._ids)

    def search(self, query: str, limit: int = 10, allowed: Callable[[Hashable], bool] | None = None) -> list[Hit]:
        terms = tokenize(query)
        if not terms or not self._ids:
            return []
        scores: dict[int, float] = {}
        for term in set(terms):
            idf = self._idf.get(term)
            if idf is None:
                continue
            for i in self._postings[term]:
                tf = self._tfs[i][term]
                denom = tf + self.k1 * (1 - self.b + self.b * self._lens[i] / (self._avgdl or 1))
                scores[i] = scores.get(i, 0.0) + idf * tf * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out: list[Hit] = []
        for i, score in ranked:
            if allowed is not None and not allowed(self._ids[i]):
                continue
            out.append(Hit(self._ids[i], round(score, 4)))
            if len(out) >= limit:
                break
        return out


class IndexCache:
    """LRU cache of built indexes keyed by (tenant, graph revision, visibility, kind)."""

    def __init__(self, max_entries: int = 32) -> None:
        self._data: OrderedDict[Hashable, object] = OrderedDict()
        self._lock = threading.Lock()
        self.max_entries = max_entries

    def get_or_build(self, key: Hashable, builder: Callable[[], object]) -> object:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        value = builder()
        with self._lock:
            self._data[key] = value
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
