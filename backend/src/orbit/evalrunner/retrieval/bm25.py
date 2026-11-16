"""BM25 (ADR-0028 §3). 운영 색인과 같은 Kiwi 토큰을 쓰고, 메일함 하나의 메일을 문서로 센다.

점수: `sum_t idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))`,
`idf = ln(1 + (N - df + 0.5) / (df + 0.5))`(음수가 되지 않는 형태). 질의 토큰은 운영 키워드 검색과
같이 정규화·중복 제거하고 앞 `MAX_QUERY_TOKENS`개만 쓴다. 점수가 0인 문서는 순위에 넣지 않고,
동점은 문서 ID 순이다.
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence

from orbit.mailindex.limits import MAX_QUERY_TOKENS
from orbit.mailindex.tokenize import normalize_token

K1 = 1.2
B = 0.75


def query_terms(tokens: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for token in tokens:
        clean = normalize_token(token)
        if clean:
            seen[clean] = None
    return list(seen)[:MAX_QUERY_TOKENS]


class Bm25Index:
    def __init__(
        self, documents: Mapping[str, Sequence[str]], *, k1: float = K1, b: float = B
    ) -> None:
        self._k1 = k1
        self._b = b
        self._tf = {doc_id: Counter(tokens) for doc_id, tokens in documents.items()}
        self._len = {doc_id: len(tokens) for doc_id, tokens in documents.items()}
        self.n_docs = len(documents)
        self._avgdl = sum(self._len.values()) / self.n_docs if self.n_docs else 0.0
        df: Counter[str] = Counter()
        for counter in self._tf.values():
            df.update(counter.keys())
        self._df = df
        self._postings: dict[str, list[str]] = {}
        for doc_id, counter in self._tf.items():
            for term in counter:
                self._postings.setdefault(term, []).append(doc_id)

    def idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def score(self, doc_id: str, terms: Sequence[str]) -> float:
        counter = self._tf[doc_id]
        norm = 1.0 - self._b + self._b * (self._len[doc_id] / self._avgdl if self._avgdl else 0.0)
        total = 0.0
        for term in terms:
            tf = counter.get(term, 0)
            if tf:
                total += self.idf(term) * tf * (self._k1 + 1.0) / (tf + self._k1 * norm)
        return total

    def search(self, tokens: Sequence[str], limit: int) -> list[tuple[str, float]]:
        terms = query_terms(tokens)
        candidates = {doc for term in terms for doc in self._postings.get(term, ())}
        scored = [(doc, self.score(doc, terms)) for doc in candidates]
        scored = [(doc, s) for doc, s in scored if s > 0.0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]
