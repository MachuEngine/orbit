"""A2R·A2X(ADR-0038)의 지문 골든, 검색 호출별 recall@5, 평가 전용 훅의 기본값."""

from typing import Any, cast

import pytest

from orbit.evalrunner.episode import Budget
from orbit.evalrunner.rag_arch import diagnostics as dg
from orbit.evalrunner.rag_arch.config import ARCHS, CANDIDATES
from orbit.evalrunner.rag_arch.sut import RagArchSUT
from orbit.runflow.tool_limits import ToolLimits


class _Index:
    def config_info(self) -> dict[str, Any]:
        return {"stub": True}


class _Reranker:
    model_id = "fake/r"
    revision = "abc"
    max_length = 768
    device = "cpu"

    def score(self, query: str, passages: Any) -> list[float]:
        return []


GOLDEN = [
    (
        "A2R",
        "rag-arch-v1+A2R+runflow-v0.8+eval-answer-v2+rag-a2r-search-v2+rag-answer-note-v1"
        "+cap6000+index-on",
        "222ecae328280373",
    ),
    (
        "A2X",
        "rag-arch-v1+A2X+runflow-v0.8+eval-answer-v2+rag-a2x-search-v2+rag-a2x-tool-note-v1"
        "+rag-a1-rewrite-v1+rag-a1-compress-v1+rag-answer-note-v1+cap6000+index-on",
        "bad71bbbafa02b55",
    ),
]


def _info(arch: str, **kw: Any) -> tuple[RagArchSUT, dict[str, Any]]:
    sut = RagArchSUT(rag_arch=arch, index=cast(Any, _Index()), reranker=_Reranker(), **kw)
    return sut, sut.config_info(model_id="m")


@pytest.mark.parametrize(("arch", "version", "digest"), GOLDEN)
def test_new_arch_fingerprint_golden(arch: str, version: str, digest: str) -> None:
    sut, info = _info(arch)
    assert sut.version == version and info["condition_digest"] == digest
    assert arch in ARCHS


def test_new_arch_fingerprint_has_search_items_and_a2_items() -> None:
    for arch, variant in (("A2R", "rerank"), ("A2X", "advanced")):
        _, info = _info(arch)
        assert info["search_variant"] == variant and info["candidates"] == CANDIDATES
        assert info["reranker"] == {
            "model_id": "fake/r", "revision": "abc", "max_length": 768, "device": "cpu",
        }  # fmt: skip
        assert info["allowed_tools"] == ["read_mail", "search_indexed_mail", "submit_answer"]
        assert "inner" in info
    _, rerank = _info("A2R")
    _, advanced = _info("A2X")
    assert "rerank_top_k" not in rerank and advanced["rerank_top_k"] == 5
    assert advanced["rewrite_prompt_sha"] and advanced["compress_prompt_sha"]
    assert advanced["tool_note_sha"] and "tool_note_sha" not in rerank


def test_a2_fingerprint_unchanged_by_new_archs() -> None:
    sut, info = _info("A2")
    assert info["condition_digest"] == "7a90a842ee10c082"
    assert "search_variant" not in info and "reranker" not in info
    assert sut.version.endswith("rag-answer-note-v1+cap6000+index-on")


def test_max_steps_goes_into_budget_fingerprint() -> None:
    assert Budget(150).to_dict()["max_steps"] == 150


def test_tool_limits_hooks_default_to_product_behaviour() -> None:
    limits = ToolLimits(frozenset({"read_mail"}), 10)
    assert limits.search_override is None and limits.search_note is None


def _row(task: str, logs: list[list[str]] | None) -> dict[str, Any]:
    diag = None if logs is None else {"search_log": [{"returned_message_ids": r} for r in logs]}
    return {"task_id": task, "rag_diag": diag}


def test_search_call_recall_at_5_uses_top5_per_call_then_episode_mean() -> None:
    gold = {"a": frozenset({"m1", "m2"}), "b": frozenset({"m9"}), "c": frozenset()}
    records = [
        # 호출 1: m1만(0.5), 호출 2: m1·m2 모두 6번째 이후는 제외 -> m2는 6번째라 빠져 0.5
        _row("a", [["m1", "x1", "x2", "x3", "x4"], ["m1", "x1", "x2", "x3", "x4", "m2"]]),
        _row("b", [["m9", "m9"], []]),  # 중복은 한 번만 센다. 빈 호출은 0 -> (1 + 0) / 2
        _row("c", [["m1"]]),  # 정답 없음: 뺀다
        _row("a", []),  # 검색 없음: 뺀다
        _row("a", None),  # 진단 없음: 뺀다
    ]
    out = dg.search_call_recall(records, gold)
    assert out["search_call_recall@5"] == pytest.approx((0.5 + 0.5) / 2)
    assert out["n_search_call_recall_episodes"] == 2 and out["n_search_calls_scored"] == 4


def test_search_call_recall_absent_without_search_log_keeps_old_output() -> None:
    assert dg.search_call_recall([{"task_id": "a", "rag_diag": {"search_calls": 1}}], {}) == {}
