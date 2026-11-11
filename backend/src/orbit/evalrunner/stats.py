"""결과 JSONL만 입력으로 받는 통계 (ADR-0009 §3, D3-3). 러너와 분리돼 있고 표준 라이브러리만 쓴다.

- 단위는 과제다. 같은 과제의 반복은 독립 표본이 아니므로 과제별 성공률(반복 평균)을 먼저 구하고,
  과제를 복원추출해서 bootstrap한다(percentile 방식). 점추정은 과제별 성공률의 평균이다.
- 신뢰구간: 재표본 통계 B개를 정렬해 `floor(a/2 * B)`번째와
  `ceil((1 - a/2) * B) - 1`번째 값을 쓴다(a는 유의수준).
- 시드를 고정하면 결과가 같다(`random.Random(seed)`).
- `outcome.success`가 `None`인 에피소드(채점 불가)는 성공률 분모에서 빼고 개수를 따로 보고한다.
- 에피소드 수가 가장 많은 과제의 수(기대 반복 수)보다 적은 과제는 기본적으로 성공률·CI에서
  제외하고 개수를 보고한다(`include_incomplete=True`로 포함). 누적 상한 중단 같은 부분 실행을
  완주한 과제와 섞지 않으려는 것이다.
- `(task_id, repeat)`가 겹치는 결과는 오류다.
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


def load_results(
    path: str | Path,
    *,
    require_single_config: bool = True,
    reject_model_mismatch: bool = False,
) -> list[Record]:
    """결과 JSONL을 읽는다. 기본은 설정 지문이 하나가 아니면 거부한다(다른 실험이 섞인 파일).

    `reject_model_mismatch=True`이면 선언 모델과 실제 응답 모델이 다른 에피소드가 있을 때
    거부한다.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ResultsError(f"결과 파일을 읽을 수 없다: {type(exc).__name__}") from exc
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
    if reject_model_mismatch and model_mismatch_count(records):
        raise ResultsError("선언한 모델과 실제 응답 모델이 다른 에피소드가 있다")
    _check_duplicates(records)
    return records


def _check_duplicates(records: Sequence[Record]) -> None:
    seen: set[tuple[str, int]] = set()
    for r in records:
        key = (str(r["task_id"]), int(r.get("repeat", 0)))
        if key in seen:
            raise ResultsError(f"(task_id, repeat)가 겹친다: {key[0]} #{key[1]}")
        seen.add(key)


def _per_task(records: Sequence[Record]) -> dict[str, list[bool | None]]:
    """과제별 성공 여부(반복 번호 순). 채점 불가 에피소드는 None."""
    _check_duplicates(records)
    grouped: dict[str, list[tuple[int, bool | None]]] = {}
    for r in records:
        success = r["outcome"]["success"]
        grouped.setdefault(str(r["task_id"]), []).append(
            (int(r.get("repeat", 0)), None if success is None else bool(success))
        )
    return {t: [ok for _, ok in sorted(v)] for t, v in grouped.items()}


@dataclass(frozen=True)
class TaskRates:
    rates: dict[str, float]
    n_incomplete_tasks: int  # 반복 수가 부족해 제외한 과제
    n_unscored_tasks: int  # 채점된 에피소드가 하나도 없는 과제
    n_unscored_episodes: int


def _task_rates(records: Sequence[Record], include_incomplete: bool) -> TaskRates:
    per_task = _per_task(records)
    expected = max(len(v) for v in per_task.values())
    rates: dict[str, float] = {}
    incomplete = unscored_tasks = 0
    for t, v in per_task.items():
        if len(v) < expected and not include_incomplete:
            incomplete += 1
            continue
        scored = [ok for ok in v if ok is not None]
        if not scored:
            unscored_tasks += 1
            continue
        rates[t] = sum(scored) / len(scored)
    n_unscored = sum(ok is None for v in per_task.values() for ok in v)
    return TaskRates(rates, incomplete, unscored_tasks, n_unscored)


def _bootstrap_ci(
    values: Sequence[float], *, resamples: int, seed: int, alpha: float
) -> tuple[float, float]:
    if resamples < 1:
        raise ValueError("resamples는 1 이상이어야 한다")
    if not 0 < alpha < 1:
        raise ValueError("alpha는 0과 1 사이여야 한다")
    if not values:
        raise ResultsError("통계에 쓸 수 있는 과제가 없다")
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
    n_unscored_episodes: int
    n_unscored_tasks: int
    n_incomplete_tasks: int


def success_rate(
    records: Sequence[Record],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
    include_incomplete: bool = False,
) -> RateSummary:
    tr = _task_rates(records, include_incomplete)
    values = [tr.rates[t] for t in sorted(tr.rates)]
    lo, hi = _bootstrap_ci(values, resamples=resamples, seed=seed, alpha=alpha)
    return RateSummary(
        statistics.fmean(values),
        lo,
        hi,
        len(values),
        len(records),
        tr.n_unscored_episodes,
        tr.n_unscored_tasks,
        tr.n_incomplete_tasks,
    )


@dataclass(frozen=True)
class PairedSummary:
    """`a - b`의 과제별 성공률 차이. 같은 task_id만 짝짓는다."""

    difference: float
    ci_low: float
    ci_high: float
    n_tasks: int
    n_unpaired_a: int
    n_unpaired_b: int


def _config_value(records: Sequence[Record], key: str) -> str:
    values = {str((r.get("config") or {}).get(key)) for r in records}
    if len(values) != 1 or values == {"None"}:
        raise ResultsError(f"config.{key}가 없거나 하나가 아니다")
    return next(iter(values))


def paired_difference(
    a: Sequence[Record],
    b: Sequence[Record],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
    include_incomplete: bool = False,
) -> PairedSummary:
    """두 결과의 `config.dataset`과 `config.split`이 같아야 한다(다르면 `ResultsError`)."""
    for key in ("dataset", "split"):
        if _config_value(a, key) != _config_value(b, key):
            raise ResultsError(f"두 결과의 config.{key}가 다르다")
    ra = _task_rates(a, include_incomplete).rates
    rb = _task_rates(b, include_incomplete).rates
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
    n_excluded: int  # 반복이 k회보다 적거나 채점 불가가 있어 제외한 과제 수


def pass_k(records: Sequence[Record], k: int | None = None) -> PassKSummary:
    """과제별로 반복 번호 순 앞 k회가 모두 성공한 과제의 비율. k 기본값은 최대 반복 횟수다."""
    per_task = _per_task(records)
    kk = k if k is not None else max(len(v) for v in per_task.values())
    if kk < 1:
        raise ValueError("k는 1 이상이어야 한다")
    eligible = {
        t: v for t, v in per_task.items() if len(v) >= kk and all(ok is not None for ok in v[:kk])
    }
    if not eligible:
        raise ResultsError(f"반복이 {kk}회 이상이고 채점된 과제가 없다")
    passed = sum(all(v[:kk]) for v in eligible.values())
    return PassKSummary(kk, passed / len(eligible), len(eligible), len(per_task) - len(eligible))


def failure_distribution(records: Sequence[Record]) -> dict[str, int]:
    """실패한 에피소드의 `failure_type`별 개수(키 정렬). 채점 불가(`success` None)는 세지 않는다."""
    counts = Counter(
        str(r["outcome"].get("failure_type") or "unknown")
        for r in records
        if r["outcome"]["success"] is False
    )
    return dict(sorted(counts.items()))


def state_diff_mismatch_count(records: Sequence[Record]) -> int:
    """상태 변화 기대와 달랐던(`state_diff_match` False) 에피소드 수. 실패 유형과 별개로 센다."""
    return sum(r.get("state_diff_match") is False for r in records)


def model_mismatch_count(records: Sequence[Record]) -> int:
    return sum(bool(r.get("model_mismatch")) for r in records)
