import json
from pathlib import Path
from typing import Any

import pytest

from orbit.evalrunner import stats


def rec(
    task: str,
    repeat: int,
    ok: bool | None,
    failure: str | None = None,
    fp: str = "fp",
    dataset: str = "d@1",
    split: str = "dev",
) -> dict[str, Any]:
    return {
        "task_id": task,
        "repeat": repeat,
        "config_fingerprint": fp,
        "config": {"dataset": dataset, "split": split},
        "outcome": {"success": ok, "failure_type": None if ok else (failure or "wrong_answer")},
    }


def make(pattern: dict[str, list[bool]]) -> list[dict[str, Any]]:
    return [rec(t, i + 1, ok) for t, v in pattern.items() for i, ok in enumerate(v)]


def test_success_rate_point_estimate_is_mean_of_task_rates() -> None:
    records = make({"a": [True, True, True], "b": [True, False, False], "c": [False] * 3})
    s = stats.success_rate(records, resamples=200, seed=1)
    assert s.rate == pytest.approx((1 + 1 / 3 + 0) / 3)
    assert (s.n_tasks, s.n_episodes) == (3, 9)
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
    a = make({"x": [True, True], "y": [True, False], "z": [True, True], "only_a": [True] * 2})
    b = make({"x": [False, False], "y": [True, False], "z": [True, False], "only_b": [True] * 2})
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


def test_duplicate_task_repeat_is_error(tmp_path: Path) -> None:
    dup = [rec("a", 1, True), rec("a", 1, False)]
    with pytest.raises(stats.ResultsError, match="겹친"):
        stats.success_rate(dup)
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in dup), encoding="utf-8")
    with pytest.raises(stats.ResultsError, match="겹친"):
        stats.load_results(path)


def test_incomplete_tasks_are_excluded_and_counted() -> None:
    records = make({"a": [True] * 3, "b": [False] * 3, "c": [True]})
    s = stats.success_rate(records, resamples=50, seed=0)
    assert (s.n_tasks, s.n_incomplete_tasks, s.rate) == (2, 1, 0.5)
    s2 = stats.success_rate(records, resamples=50, seed=0, include_incomplete=True)
    assert (s2.n_tasks, s2.n_incomplete_tasks) == (3, 0)
    assert s2.rate == pytest.approx(2 / 3)


def test_unscored_episodes_are_not_in_denominator() -> None:
    records = [
        rec("a", 1, True),
        rec("a", 2, True),
        rec("b", 1, None, failure="unscored"),
        rec("b", 2, None, failure="unscored"),
        rec("c", 1, False),
        rec("c", 2, None, failure="unscored"),
    ]
    s = stats.success_rate(records, resamples=50, seed=0)
    assert (s.n_tasks, s.n_unscored_tasks, s.n_unscored_episodes) == (2, 1, 3)
    assert s.rate == 0.5  # a 1.0, c 0.0
    assert stats.failure_distribution(records) == {"wrong_answer": 1}
    assert stats.pass_k(records).n_tasks == 1  # a만 채점된 k회가 있다


def test_paired_difference_requires_same_dataset_and_split() -> None:
    a = make({"x": [True]})
    b = [rec("x", 1, False, dataset="other@1")]
    with pytest.raises(stats.ResultsError, match="dataset"):
        stats.paired_difference(a, b)
    c = [rec("x", 1, False, split="test")]
    with pytest.raises(stats.ResultsError, match="split"):
        stats.paired_difference(a, c)


def test_state_diff_mismatch_count_and_model_mismatch_rejection(tmp_path: Path) -> None:
    rows = [rec("a", 1, False), rec("a", 2, True)]
    rows[0]["state_diff_match"] = False
    rows[1]["state_diff_match"] = True
    rows[1]["model_mismatch"] = True
    assert stats.state_diff_mismatch_count(rows) == 1
    assert stats.model_mismatch_count(rows) == 1
    path = tmp_path / "m.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert len(stats.load_results(path)) == 2
    with pytest.raises(stats.ResultsError, match="모델"):
        stats.load_results(path, reject_model_mismatch=True)
