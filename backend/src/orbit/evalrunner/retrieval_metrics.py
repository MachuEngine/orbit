"""Pure ranking metrics used by retrieval evaluations."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def _ranked(ranked_ids: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(ranked_ids))


def _validate(relevance: Mapping[str, int], k: int, min_grade: int = 0) -> None:
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError("k must be a positive integer")
    if isinstance(min_grade, bool) or min_grade not in (1, 2):
        raise ValueError("min_grade must be 1 or 2")
    if any(
        isinstance(g, bool) or not isinstance(g, int) or g not in (0, 1, 2)
        for g in relevance.values()
    ):
        raise ValueError("relevance grades must be integers from 0 to 2")


def recall_at_k(
    relevance: Mapping[str, int], ranked_ids: Sequence[str], k: int, min_grade: int = 2
) -> float:
    """Recall of IDs at or above min_grade; empty gold sets score zero."""
    _validate(relevance, k, min_grade)
    relevant = {item for item, grade in relevance.items() if grade >= min_grade}
    if not relevant:
        return 0.0
    return len(relevant.intersection(_ranked(ranked_ids)[:k])) / len(relevant)


def mrr(relevance: Mapping[str, int], ranked_ids: Sequence[str]) -> float:
    """Reciprocal rank of the first grade-2 result; no grade-2 result scores zero."""
    _validate(relevance, 1, 2)
    for rank, item in enumerate(_ranked(ranked_ids), start=1):
        if relevance.get(item, 0) == 2:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(relevance: Mapping[str, int], ranked_ids: Sequence[str], k: int = 10) -> float:
    """nDCG with linear relevance gain and log2(rank + 1) discount."""
    _validate(relevance, k, 1)
    ranked = _ranked(ranked_ids)[:k]
    dcg = sum(relevance.get(item, 0) / math.log2(rank + 1) for rank, item in enumerate(ranked, 1))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(grade / math.log2(rank + 1) for rank, grade in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0
