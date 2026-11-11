"""결과 JSONL만 입력으로 받는 통계 (ADR-0009 §3, D3-3). 러너와 분리돼 있고 표준 라이브러리만 쓴다.

- 같은 과제의 반복은 독립 표본이 아니므로 과제별 성공률(반복 평균)을 먼저 구한다. 근거를 공유하는
  과제도 독립이 아니므로(ADR-0010 §2) bootstrap은 결과 행의 `cluster_id` 단위로 클러스터를
  복원추출한다(percentile 방식). 재표본마다 뽑힌 클러스터에 속한 과제들의 성공률 평균을 낸다.
  점추정은 모든 과제의 성공률 평균이다. 짝지은 차이도 같은 클러스터를 함께 뽑는다.
- 클러스터 수가 `MIN_CLUSTERS`(10) 미만이면 신뢰구간이 불안정하다는 경고를 `warnings`에 남긴다.
  보고에는 클러스터 수와 클러스터별 값(성공률, 짝지은 차이)이 들어간다.
- 신뢰구간: 재표본 통계 B개를 정렬해 `floor(a/2 * B)`번째와
  `ceil((1 - a/2) * B) - 1`번째 값을 쓴다(a는 유의수준).
- 시드를 고정하면 결과가 같다(`random.Random(seed)`).
- `outcome.success`가 `None`인 에피소드(채점 불가)는 성공률 분모에서 빼고 개수를 따로 보고한다.
- 중단된 실험(누적 상한 등)은 "모든 과제가 완료한 최대 반복 라운드"까지만 잘라서 쓴다. 기대 반복
  수는 결과 행의 `config.repeats`(행마다 다르거나 없으면 오류), 과제 수는 `config.n_tasks`(없으면
  파일에 나온 과제 수)다. 잘랐다는 사실과 원래 `config.repeats`는 `RateSummary`에 남는다.
  남은 과제가 하나도 없으면 오류이고 메시지에 `--include-incomplete` 안내가 있다. 이 옵션
  (`include_incomplete=True`)은 자르지 않고 있는 에피소드를 모두 쓴다. `pass_k`도 같은 규칙으로
  자른다(`k` 기본값은 `used_repeats`).
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


MIN_CLUSTERS = 10


def _expected_repeats(records: Sequence[Record]) -> int:
    values = {(r.get("config") or {}).get("repeats") for r in records}
    only = next(iter(values))
    if len(values) != 1 or not isinstance(only, int):
        raise ResultsError("config.repeats가 없거나 행마다 다르다")
    return only


def _planned_tasks(records: Sequence[Record]) -> int | None:
    values = {(r.get("config") or {}).get("n_tasks") for r in records}
    only = next(iter(values))
    return only if len(values) == 1 and isinstance(only, int) else None


def _cluster_map(records: Sequence[Record]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in records:
        cid = r.get("cluster_id")
        if not isinstance(cid, str) or not cid:
            raise ResultsError("결과 행에 cluster_id가 없다")
        task = str(r["task_id"])
        if out.setdefault(task, cid) != cid:
            raise ResultsError(f"과제 {task}의 cluster_id가 행마다 다르다")
    return out


@dataclass(frozen=True)
class _Rounds:
    by_task: dict[str, dict[int, bool | None]]  # 과제 -> 반복 번호 -> 성공(None: 채점 불가)
    expected: int  # config.repeats
    used: int  # 통계에 쓸 반복 라운드 수
    truncated: bool  # 중단된 실험이라 라운드를 잘라 썼는가


def _rounds(records: Sequence[Record], include_incomplete: bool) -> _Rounds:
    """모든 과제가 완료한 최대 반복 라운드(1회차부터 끊김 없이)를 구한다."""
    _check_duplicates(records)
    expected = _expected_repeats(records)
    by_task: dict[str, dict[int, bool | None]] = {}
    per_round: dict[int, int] = {}
    for r in records:
        success = r["outcome"]["success"]
        rnd = int(r.get("repeat", 0))
        by_task.setdefault(str(r["task_id"]), {})[rnd] = None if success is None else bool(success)
        per_round[rnd] = per_round.get(rnd, 0) + 1
    if include_incomplete:
        return _Rounds(by_task, expected, expected, False)
    planned = _planned_tasks(records) or len(by_task)
    used = 0
    for rnd in range(1, expected + 1):
        if per_round.get(rnd, 0) < planned:
            break
        used = rnd
    return _Rounds(by_task, expected, used, used < expected)


@dataclass(frozen=True)
class TaskRates:
    rates: dict[str, float]
    clusters: dict[str, str]  # 통계에 쓴 과제 -> cluster_id
    n_incomplete_tasks: int  # 사용할 라운드에 에피소드가 부족해 제외한 과제
    n_unscored_tasks: int  # 채점된 에피소드가 하나도 없는 과제
    n_unscored_episodes: int
    config_repeats: int
    used_repeats: int  # 통계에 쓴 반복 라운드 수
    truncated: bool  # 중단된 실험이라 라운드를 잘라 썼는가


def _task_rates(records: Sequence[Record], include_incomplete: bool) -> TaskRates:
    rd = _rounds(records, include_incomplete)
    cluster_of = _cluster_map(records)
    rates: dict[str, float] = {}
    incomplete = unscored_tasks = n_unscored = 0
    for t, rounds in rd.by_task.items():
        picked = [rounds[k] for k in sorted(rounds) if include_incomplete or k <= rd.used]
        n_unscored += sum(ok is None for ok in picked)
        if not include_incomplete and len(picked) < rd.used:
            incomplete += 1
            continue
        scored = [ok for ok in picked if ok is not None]
        if not scored:
            unscored_tasks += 1
            continue
        rates[t] = sum(scored) / len(scored)
    return TaskRates(
        rates,
        {t: cluster_of[t] for t in rates},
        incomplete,
        unscored_tasks,
        n_unscored,
        rd.expected,
        rd.used,
        rd.truncated,
    )


def _group_by_cluster(
    values: Mapping[str, float], cluster_of: Mapping[str, str]
) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = {}
    for task in sorted(values):
        groups.setdefault(cluster_of[task], []).append(values[task])
    return groups


def _bootstrap_ci(
    groups: Sequence[Sequence[float]], *, resamples: int, seed: int, alpha: float
) -> tuple[float, float]:
    """클러스터를 복원추출한다. 통계는 뽑힌 클러스터에 속한 모든 과제 값의 평균이다."""
    if resamples < 1:
        raise ValueError("resamples는 1 이상이어야 한다")
    if not 0 < alpha < 1:
        raise ValueError("alpha는 0과 1 사이여야 한다")
    if not groups:
        raise ResultsError(
            "통계에 쓸 수 있는 과제가 없다(완료한 반복 라운드가 없거나 모두 채점 불가다). "
            "부분 실행 결과를 그대로 쓰려면 --include-incomplete를 쓴다"
        )
    rng = random.Random(seed)  # noqa: S311  재현성용 의사난수
    n = len(groups)
    sums = [sum(g) for g in groups]
    counts = [len(g) for g in groups]
    means: list[float] = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in range(n)]
        means.append(sum(sums[i] for i in picks) / sum(counts[i] for i in picks))
    means.sort()
    lo = math.floor(alpha / 2 * resamples)
    hi = math.ceil((1 - alpha / 2) * resamples) - 1
    return means[lo], means[min(max(hi, lo), resamples - 1)]


def _few_clusters_warning(n: int) -> list[str]:
    if n < MIN_CLUSTERS:
        return [f"클러스터 수가 {n}개로 {MIN_CLUSTERS}개 미만이라 신뢰구간이 불안정하다"]
    return []


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
    config_repeats: int  # 원래 설정의 반복 수
    used_repeats: int  # 통계에 쓴 반복 라운드 수
    truncated: bool  # 중단된 실험이라 라운드를 잘라 썼다
    n_clusters: int
    cluster_rates: dict[str, float]  # 클러스터별 과제 성공률 평균
    warnings: tuple[str, ...]


def success_rate(
    records: Sequence[Record],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
    include_incomplete: bool = False,
) -> RateSummary:
    tr = _task_rates(records, include_incomplete)
    groups = _group_by_cluster(tr.rates, tr.clusters)
    lo, hi = _bootstrap_ci(list(groups.values()), resamples=resamples, seed=seed, alpha=alpha)
    return RateSummary(
        statistics.fmean(tr.rates.values()),
        lo,
        hi,
        len(tr.rates),
        len(records),
        tr.n_unscored_episodes,
        tr.n_unscored_tasks,
        tr.n_incomplete_tasks,
        tr.config_repeats,
        tr.used_repeats,
        tr.truncated,
        len(groups),
        {c: statistics.fmean(v) for c, v in sorted(groups.items())},
        tuple(_few_clusters_warning(len(groups))),
    )


@dataclass(frozen=True)
class PairedSummary:
    """`a - b`의 과제별 성공률 차이. 같은 task_id만 짝짓고 같은 클러스터를 함께 뽑는다."""

    difference: float
    ci_low: float
    ci_high: float
    n_tasks: int
    n_unpaired_a: int
    n_unpaired_b: int
    used_repeats_a: int
    used_repeats_b: int
    n_clusters: int
    cluster_differences: dict[str, float]  # 클러스터별 과제 차이 평균
    warnings: tuple[str, ...]


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
    """두 결과의 `config.dataset`과 `config.split`이 같아야 한다(다르면 `ResultsError`).

    두 설정의 `used_repeats`가 다르면(한쪽이 중단돼 잘렸다) 경고한다.
    """
    for key in ("dataset", "split"):
        if _config_value(a, key) != _config_value(b, key):
            raise ResultsError(f"두 결과의 config.{key}가 다르다")
    tra, trb = _task_rates(a, include_incomplete), _task_rates(b, include_incomplete)
    ra, rb = tra.rates, trb.rates
    common = sorted(set(ra) & set(rb))
    if not common:
        raise ResultsError("두 결과에 공통 task_id가 없다")
    for t in common:
        if tra.clusters[t] != trb.clusters[t]:
            raise ResultsError(f"과제 {t}의 cluster_id가 두 결과에서 다르다")
    diffs = {t: ra[t] - rb[t] for t in common}
    groups = _group_by_cluster(diffs, tra.clusters)
    lo, hi = _bootstrap_ci(list(groups.values()), resamples=resamples, seed=seed, alpha=alpha)
    warnings = _few_clusters_warning(len(groups))
    if tra.used_repeats != trb.used_repeats:
        warnings.append(
            f"두 설정이 쓴 반복 라운드 수가 다르다(a={tra.used_repeats}, b={trb.used_repeats})"
        )
    return PairedSummary(
        statistics.fmean(diffs.values()),
        lo,
        hi,
        len(common),
        len(set(ra) - set(rb)),
        len(set(rb) - set(ra)),
        tra.used_repeats,
        trb.used_repeats,
        len(groups),
        {c: statistics.fmean(v) for c, v in sorted(groups.items())},
        tuple(warnings),
    )


@dataclass(frozen=True)
class PassKSummary:
    k: int
    pass_k: float  # k회 모두 성공한 과제 비율
    n_tasks: int
    n_excluded: int  # 반복이 k회보다 적거나 채점 불가가 있어 제외한 과제 수
    config_repeats: int
    used_repeats: int
    truncated: bool


def pass_k(
    records: Sequence[Record], k: int | None = None, *, include_incomplete: bool = False
) -> PassKSummary:
    """과제별로 반복 1..k회가 모두 있고 모두 성공한 과제의 비율.

    `k` 기본값은 `used_repeats`(중단된 실험은 완료한 라운드까지)이고, `include_incomplete`이면
    `config.repeats`다.
    """
    rd = _rounds(records, include_incomplete)
    kk = k if k is not None else rd.used
    if kk < 1:
        raise ResultsError("완료한 반복 라운드가 없다. --include-incomplete를 쓰거나 k를 지정한다")
    eligible = {
        t: [rounds[i] for i in range(1, kk + 1)]
        for t, rounds in rd.by_task.items()
        if all(i in rounds and rounds[i] is not None for i in range(1, kk + 1))
    }
    if not eligible:
        raise ResultsError(f"반복 {kk}회가 모두 채점된 과제가 없다")
    passed = sum(all(v) for v in eligible.values())
    return PassKSummary(
        kk,
        passed / len(eligible),
        len(eligible),
        len(rd.by_task) - len(eligible),
        rd.expected,
        rd.used,
        rd.truncated,
    )


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
