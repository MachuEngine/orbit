from typing import Any

import pytest

from orbit.evalrunner.retrieval_metrics import mrr, ndcg_at_k, recall_at_k


def test_recall_thresholds_deduplicate_before_cutoff() -> None:
    qrels = {"a": 2, "b": 2, "c": 2, "d": 2, "e": 1, "f": 1}
    ranked = [
        "a",
        "a",
        "x1",
        "x2",
        "b",
        "x3",
        "x4",
        "c",
        "e",
        "x5",
        "x6",
        "d",
        "x7",
        "x8",
        "x9",
        "x10",
        "x11",
        "f",
    ]
    assert recall_at_k(qrels, ranked, 5, min_grade=2) == pytest.approx(0.5)
    assert recall_at_k(qrels, ranked, 10, min_grade=2) == pytest.approx(0.75)
    assert recall_at_k(qrels, ranked, 20, min_grade=2) == 1
    assert recall_at_k(qrels, ranked, 5, min_grade=1) == pytest.approx(1 / 3)
    assert recall_at_k(qrels, ranked, 10, min_grade=1) == pytest.approx(4 / 6)
    assert recall_at_k(qrels, ranked, 20, min_grade=1) == 1


def test_recall_empty_relevance_and_short_ranking_score_zero() -> None:
    assert recall_at_k({}, ["a"], 10) == 0
    assert recall_at_k({"a": 2}, [], 10) == 0


def test_mrr_duplicate_rank_and_no_grade_two() -> None:
    assert mrr({"a": 2}, ["x", "x", "a"]) == pytest.approx(1 / 2)
    assert mrr({"a": 1}, ["a"]) == 0


def test_ndcg_linear_gain_hand_calculation() -> None:
    # DCG = 1 + 2/log2(3), ideal = 2 + 1/log2(3).
    expected = (1 + 2 / 1.584962500721156) / (2 + 1 / 1.584962500721156)
    assert ndcg_at_k({"a": 2, "b": 1}, ["b", "a"], 10) == pytest.approx(expected)
    assert ndcg_at_k({}, [], 10) == 0
    duplicate_expected = (1 / 1.584962500721156 + 2 / 2) / (2 + 1 / 1.584962500721156)
    assert ndcg_at_k({"a": 2, "b": 1}, ["x", "x", "b", "a"], 10) == pytest.approx(
        duplicate_expected
    )
    assert ndcg_at_k({"a": 2, "b": 1}, ["a"], 10) == pytest.approx(2 / (2 + 1 / 1.584962500721156))


@pytest.mark.parametrize(
    ("qrels", "ranking", "k", "min_grade"),
    [
        ({"a": 3}, ["a"], 5, 2),
        ({"a": True}, ["a"], 5, 2),
        ({}, [], 0, 2),
        ({}, [], 5, 0),
        ({}, [], True, 2),
        ({}, [], 1.5, 2),
        ({}, [], 5, True),
    ],
)
def test_invalid_metric_arguments_raise(
    qrels: dict[str, Any], ranking: list[str], k: Any, min_grade: Any
) -> None:
    with pytest.raises(ValueError):
        recall_at_k(qrels, ranking, k, min_grade)
