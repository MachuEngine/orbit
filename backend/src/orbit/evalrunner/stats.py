"""결과 JSONL만 입력으로 받는 통계 (ADR-0009 §3, D3-3). 러너와 분리돼 있고 표준 라이브러리만 쓴다.

- 단위는 과제다. 같은 과제의 반복은 독립 표본이 아니므로 과제별 성공률(반복 평균)을 먼저 구하고,
  과제를 복원추출해서 bootstrap한다(percentile 방식). 점추정은 과제별 성공률의 평균이다.
- 신뢰구간: 재표본 통계 B개를 정렬해 `floor(a/2 * B)`번째와
  `ceil((1 - a/2) * B) - 1`번째 값을 쓴다(a는 유의수준).
- 시드를 고정하면 결과가 같다(`random.Random(seed)`).
"""

import json
import math
import random
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Record = Mapping[str, Any]

DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 0
DEFAULT_ALPHA = 0.05


class ResultsError(ValueError):
    """결과 JSONL을 읽을 수 없거나 통계에 쓸 수 없는 내용이다."""


def load_results(path: str | Path, *, require_single_config: bool = True) -> list[Record]:
    """결과 JSONL을 읽는다. 기본은 설정 지문이 하나가 아니면 거부한다(다른 실험이 섞인 파일)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ResultsError(f"결과 파일을 읽을 수 없다: {exc}") from exc
    records: list[Record] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResultsError(f"{n}번째 줄이 JSON이 아니다") from exc
        if not isinstance(rec, dict) or "task_id" not in rec or "outcome" not in rec:
            raise ResultsError(f"{n}번째 줄이 결과 형식이 아니다")
        records.append(rec)
    if not records:
        raise ResultsError("결과가 비었다")
    if require_single_config and len({r.get("config_fingerprint") for r in records}) > 1:
        raise ResultsError("설정 지문이 서로 다른 결과가 섞여 있다")
    return records


def _per_task(records: Sequence[Record]) -> dict[str, list[bool]]:
    grouped: dict[str, list[tuple[int, bool]]] = {}
    for r in records:
        grouped.setdefault(str(r["task_id"]), []).append(
            (int(r.get("repeat", 0)), bool(r["outcome"]["success"]))
        )
    return {t: [ok for _, ok in sorted(v)] for t, v in grouped.items()}


def _task_rates(records: Sequence[Record]) -> dict[str, float]:
    return {t: sum(v) / len(v) for t, v in _per_task(records).items()}


def _bootstrap_ci(
    values: Sequence[float], *, resamples: int, seed: int, alpha: float
) -> tuple[float, float]:
    if resamples < 1:
        raise ValueError("resamples는 1 이상이어야 한다")
    if not 0 < alpha < 1:
        raise ValueError("alpha는 0과 1 사이여야 한다")
    rng = random.Random(seed)  # noqa: S311  재현성용 의사난수
    n = len(values)
    means = sorted(
        statistics.fmean(values[rng.randrange(n)] for _ in range(n)) for _ in range(resamples)
    )
    lo = math.floor(alpha / 2 * resamples)
    hi = math.ceil((1 - alpha / 2) * resamples) - 1
    return means[lo], means[min(max(hi, lo), resamples - 1)]


@dataclass(frozen=True)
class RateSummary:
    rate: float  # 과제별 성공률의 평균
    ci_low: float
    ci_high: float
    n_tasks: int
    n_episodes: int


def success_rate(
    records: Sequence[Record],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> RateSummary:
    rates = _task_rates(records)
    values = [rates[t] for t in sorted(rates)]
    lo, hi = _bootstrap_ci(values, resamples=resamples, seed=seed, alpha=alpha)
    return RateSummary(statistics.fmean(values), lo, hi, len(values), len(records))


@dataclass(frozen=True)
class PairedSummary:
    """`a - b`의 과제별 성공률 차이. 같은 task_id만 짝짓는다."""

    difference: float
    ci_low: float
    ci_high: float
    n_tasks: int
    n_unpaired_a: int
    n_unpaired_b: int


def paired_difference(
    a: Sequence[Record],
    b: Sequence[Record],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> PairedSummary:
    ra, rb = _task_rates(a), _task_rates(b)
    common = sorted(set(ra) & set(rb))
    if not common:
        raise ResultsError("두 결과에 공통 task_id가 없다")
    diffs = [ra[t] - rb[t] for t in common]
    lo, hi = _bootstrap_ci(diffs, resamples=resamples, seed=seed, alpha=alpha)
    return PairedSummary(
        statistics.fmean(diffs), lo, hi, len(common), len(set(ra) - set(rb)), len(set(rb) - set(ra))
    )


@dataclass(frozen=True)
class PassKSummary:
    k: int
    pass_k: float  # k회 모두 성공한 과제 비율
    n_tasks: int
    n_excluded: int  # 반복이 k회보다 적어 제외한 과제 수


def pass_k(records: Sequence[Record], k: int | None = None) -> PassKSummary:
    """과제별로 반복 번호 순 앞 k회가 모두 성공한 과제의 비율. k 기본값은 최대 반복 횟수다."""
    per_task = _per_task(records)
    kk = k if k is not None else max(len(v) for v in per_task.values())
    if kk < 1:
        raise ValueError("k는 1 이상이어야 한다")
    eligible = {t: v for t, v in per_task.items() if len(v) >= kk}
    if not eligible:
        raise ResultsError(f"반복이 {kk}회 이상인 과제가 없다")
    passed = sum(all(v[:kk]) for v in eligible.values())
    return PassKSummary(kk, passed / len(eligible), len(eligible), len(per_task) - len(eligible))


def failure_distribution(records: Sequence[Record]) -> dict[str, int]:
    """실패한 에피소드의 `failure_type`별 개수(키 정렬)."""
    counts = Counter(
        str(r["outcome"].get("failure_type") or "unknown")
        for r in records
        if not r["outcome"]["success"]
    )
    return dict(sorted(counts.items()))
