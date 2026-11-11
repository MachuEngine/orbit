import json
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner import stats


def rec(
    task: str, repeat: int, ok: bool, failure: str | None = None, fp: str = "fp"
) -> dict[str, Any]:
    return {
        "task_id": task,
        "repeat": repeat,
        "config_fingerprint": fp,
        "outcome": {"success": ok, "failure_type": None if ok else (failure or "wrong_answer")},
    }


def make(pattern: dict[str, list[bool]]) -> list[dict[str, Any]]:
    return [rec(t, i + 1, ok) for t, v in pattern.items() for i, ok in enumerate(v)]


def test_success_rate_point_estimate_is_mean_of_task_rates() -> None:
    records = make({"a": [True, True, True], "b": [True, False, False], "c": [False]})
    s = stats.success_rate(records, resamples=200, seed=1)
    assert s.rate == pytest.approx((1 + 1 / 3 + 0) / 3)
    assert (s.n_tasks, s.n_episodes) == (3, 7)
    assert 0 <= s.ci_low <= s.rate <= s.ci_high <= 1


def test_ci_is_degenerate_when_all_tasks_equal() -> None:
    s = stats.success_rate(make({"a": [True], "b": [True], "c": [True]}), resamples=100, seed=0)
    assert (s.rate, s.ci_low, s.ci_high) == (1.0, 1.0, 1.0)


def test_ci_is_reproducible_with_seed_and_depends_on_it() -> None:
    records = make({f"t{i}": [i % 3 != 0] for i in range(20)})
    a = stats.success_rate(records, resamples=500, seed=3)
    b = stats.success_rate(records, resamples=500, seed=3)
    c = stats.success_rate(records, resamples=500, seed=4)
    assert a == b
    assert (a.ci_low, a.ci_high) != (c.ci_low, c.ci_high) or a.ci_low < a.ci_high


def test_ci_resamples_tasks_not_episodes() -> None:
    # 과제 2개: 하나는 항상 성공, 다른 하나는 항상 실패.
    # 과제 단위 재표본이면 CI가 [0, 1]에 닿는다.
    records = make({"a": [True] * 5, "b": [False] * 5})
    s = stats.success_rate(records, resamples=2000, seed=0)
    assert s.rate == 0.5
    assert (s.ci_low, s.ci_high) == (0.0, 1.0)


def test_paired_difference_known_values() -> None:
    a = make({"x": [True, True], "y": [True, False], "z": [True, True], "only_a": [True]})
    b = make({"x": [False, False], "y": [True, False], "z": [True, False], "only_b": [True]})
    p = stats.paired_difference(a, b, resamples=500, seed=0)
    # 과제별 차이: x 1.0, y 0.0, z 0.5
    assert p.difference == pytest.approx(0.5)
    assert (p.n_tasks, p.n_unpaired_a, p.n_unpaired_b) == (3, 1, 1)
    assert p.ci_low <= p.difference <= p.ci_high
    assert stats.paired_difference(a, b, resamples=500, seed=0) == p


def test_paired_difference_all_same_sign_has_tight_ci() -> None:
    a = make({f"t{i}": [True] for i in range(5)})
    b = make({f"t{i}": [False] for i in range(5)})
    p = stats.paired_difference(a, b, resamples=200, seed=0)
    assert (p.difference, p.ci_low, p.ci_high) == (1.0, 1.0, 1.0)


def test_paired_difference_requires_common_tasks() -> None:
    with pytest.raises(stats.ResultsError):
        stats.paired_difference(make({"a": [True]}), make({"b": [True]}))


def test_pass_k() -> None:
    records = make({"a": [True] * 3, "b": [True, True, False], "c": [False] * 3, "d": [True] * 3})
    s = stats.pass_k(records)
    assert (s.k, s.pass_k, s.n_tasks, s.n_excluded) == (3, 0.5, 4, 0)
    s2 = stats.pass_k(records, k=2)
    assert s2.pass_k == 0.75  # a, b, d


def test_pass_k_excludes_tasks_with_fewer_repeats() -> None:
    records = make({"a": [True] * 3, "b": [True]})
    s = stats.pass_k(records)
    assert (s.k, s.pass_k, s.n_tasks, s.n_excluded) == (3, 1.0, 1, 1)


def test_failure_distribution() -> None:
    records = [
        rec("a", 1, False, "wrong_answer"),
        rec("a", 2, False, "forbidden_tool"),
        rec("b", 1, False, "wrong_answer"),
        rec("b", 2, True),
    ]
    assert stats.failure_distribution(records) == {"forbidden_tool": 1, "wrong_answer": 2}


def test_load_results_rejects_mixed_configs(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in [rec("a", 1, True, fp="x"), rec("a", 2, True, fp="y")]),
        encoding="utf-8",
    )
    with pytest.raises(stats.ResultsError, match="지문"):
        stats.load_results(path)
    assert len(stats.load_results(path, require_single_config=False)) == 2
