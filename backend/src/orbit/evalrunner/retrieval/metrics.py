"""질의별 지표와 집계 (ADR-0028 §3).

- 질의별: recall@5·10·20(등급 2, 등급 1 이상), MRR(등급 2 첫 순위), nDCG@10.
- 집계: 메일함 크기·방식별, 질의 형태별 평균. 신뢰구간은 주제 묶음(`cluster_id`) 단위 bootstrap이다
  (`orbit.evalrunner.stats`의 묶음 복원추출을 그대로 쓴다. 통계량은 뽑힌 묶음의 질의 값 평균).
- 방식 간 차이는 같은 질의의 짝 차이(a - b)이고 같은 묶음을 함께 뽑는다.
"""

import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Any

from orbit.evalrunner import retrieval_metrics as rm
from orbit.evalrunner import stats

KS = (5, 10, 20)
METRICS = (
    *(f"recall@{k}" for k in KS),
    *(f"recall_ge1@{k}" for k in KS),
    "mrr",
    "ndcg@10",
)
PAIRED_METRICS = ("recall@10", "mrr", "ndcg@10")
Row = Mapping[str, Any]


def query_metrics(relevance: Mapping[str, int], ranked: Sequence[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in KS:
        out[f"recall@{k}"] = rm.recall_at_k(relevance, ranked, k, 2)
        out[f"recall_ge1@{k}"] = rm.recall_at_k(relevance, ranked, k, 1)
    out["mrr"] = rm.mrr(relevance, ranked)
    out["ndcg@10"] = rm.ndcg_at_k(relevance, ranked, 10)
    return out


def _clusters(rows: Sequence[Row], values: Sequence[float]) -> list[list[float]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for row, value in zip(rows, values, strict=True):
        groups[str(row["cluster_id"])].append(value)
    return [groups[c] for c in sorted(groups)]


def _mean_ci(
    rows: Sequence[Row], values: Sequence[float], *, resamples: int, seed: int
) -> dict[str, Any]:
    if not values:
        return {"mean": None, "ci_low": None, "ci_high": None, "n": 0, "n_clusters": 0}
    groups = _clusters(rows, values)
    lo, hi = stats._bootstrap_ci(groups, resamples=resamples, seed=seed, alpha=stats.DEFAULT_ALPHA)  # noqa: SLF001
    return {
        "mean": statistics.fmean(values),
        "ci_low": lo,
        "ci_high": hi,
        "n": len(values),
        "n_clusters": len(groups),
    }


def summarize(
    rows: Sequence[Row], size: str, method: str, *, resamples: int, seed: int
) -> dict[str, Any]:
    """`rows`는 질의별 결과 `{query_id, form, cluster_id, metrics: {size: {method: {...}}}}`."""
    summary: dict[str, Any] = {"n": len(rows), "metrics": {}, "by_form": {}}
    for metric in METRICS:
        values = [float(r["metrics"][size][method][metric]) for r in rows]
        summary["metrics"][metric] = _mean_ci(rows, values, resamples=resamples, seed=seed)
    for form in sorted({str(r["form"]) for r in rows}):
        subset = [r for r in rows if r["form"] == form]
        summary["by_form"][form] = {
            "n": len(subset),
            "metrics": {
                metric: statistics.fmean(float(r["metrics"][size][method][metric]) for r in subset)
                for metric in METRICS
            },
        }
    return summary


def paired(
    rows: Sequence[Row],
    size: str,
    a: str,
    b: str,
    *,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric in PAIRED_METRICS:
        diffs = [
            float(r["metrics"][size][a][metric]) - float(r["metrics"][size][b][metric])
            for r in rows
        ]
        entry = _mean_ci(rows, diffs, resamples=resamples, seed=seed)
        out[metric] = {
            "difference": entry["mean"],
            "ci_low": entry["ci_low"],
            "ci_high": entry["ci_high"],
            "n": entry["n"],
            "n_clusters": entry["n_clusters"],
        }
    return out


def method_pairs(methods: Sequence[str]) -> list[tuple[str, str]]:
    return list(combinations(methods, 2))


def build_report(
    rows: Sequence[Row],
    sizes: Sequence[str],
    methods: Sequence[str],
    *,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    summary = {
        size: {m: summarize(rows, size, m, resamples=resamples, seed=seed) for m in methods}
        for size in sizes
    }
    pairs = {
        size: {
            f"{a}-{b}": paired(rows, size, a, b, resamples=resamples, seed=seed)
            for a, b in method_pairs(methods)
        }
        for size in sizes
    }
    warnings = []
    n_clusters = len({str(r["cluster_id"]) for r in rows})
    if n_clusters < stats.MIN_CLUSTERS:
        warnings.append(
            f"주제 묶음이 {n_clusters}개로 {stats.MIN_CLUSTERS}개 미만이라 신뢰구간이 불안정하다"
        )
    return {
        "summary": summary,
        "paired_a_minus_b": pairs,
        "warnings": warnings,
        "n_clusters": n_clusters,
    }
