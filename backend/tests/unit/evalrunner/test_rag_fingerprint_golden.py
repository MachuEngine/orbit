"""RAG 아키텍처별 version·condition_digest 골든. 주 구성의 지문 불변을 고정한다."""

from typing import Any, cast

import pytest

from orbit.evalrunner.rag_arch.config import (
    RAG_ANSWER_NOTE,
    RAG_ANSWER_NOTE_VERSION,
    prompt_sha,
)
from orbit.evalrunner.rag_arch.sut import RagArchSUT
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.runflow.fingerprint import config_fingerprint


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


# 값이 바뀌면 dev/test 결과와 비교 불가가 되니 의도한 변경일 때만 갱신한다.
GOLDEN = [
    ("A0", {}, "rag-arch-v1+A0+rag-a0-v1+rag-answer-note-v1+cap6000+index-on", "659dc0427a720da3"),
    (
        "A1",
        {"reranker": _Reranker()},
        "rag-arch-v1+A1+rag-a1-v1+rag-a1-rewrite-v1+rag-a1-compress-v1+rag-answer-note-v1+cap6000+index-on",
        "2998a01825fe1d07",
    ),
    (
        "A2",
        {},
        "rag-arch-v1+A2+runflow-v0.8+eval-answer-v2+rag-answer-note-v1+cap6000+index-on",
        "7a90a842ee10c082",
    ),
    (
        "A3",
        {"reranker": _Reranker()},
        "rag-arch-v1+A3+rag-a3-route-v1+none-rag-a0-v1+rag-a1-v1+rag-a1-rewrite-v1"
        "+rag-a1-compress-v1+runflow-v0.8+eval-answer-v2+rag-answer-note-v1+cap6000+index-on",
        "538008d37e1b4fba",
    ),
    (
        "A4",
        {"graph_digests": {"a": "d1"}},
        "rag-arch-v1+A4+rag-a4-v1+rag-a4-route-v1+mode-auto+rag-answer-note-v1+cap6000+index-on",
        "50b3dd7b3fa4e684",
    ),
    (
        "A4",
        {"graph_digests": {"a": "d1"}, "a4_mode": "local"},
        "rag-arch-v1+A4+rag-a4-v1+rag-a4-route-v1+mode-local+rag-answer-note-v1+cap6000+index-on",
        "99d161c7063ad2e5",
    ),
    (
        "A4",
        {"graph_digests": {"a": "d1"}, "a4_mode": "global"},
        "rag-arch-v1+A4+rag-a4-v1+rag-a4-route-v1+mode-global+rag-answer-note-v1+cap6000+index-on",
        "82d5adcd6cae3169",
    ),
]


@pytest.mark.parametrize(("arch", "kw", "version", "digest"), GOLDEN)
def test_main_arch_fingerprint_golden(
    arch: str, kw: dict[str, Any], version: str, digest: str
) -> None:
    sut = RagArchSUT(rag_arch=arch, index=cast(Any, _Index()), **kw)
    assert sut.version == version
    assert sut.config_info(model_id="m")["condition_digest"] == digest


# config_info의 지문 밖 항목도 현재 값으로 고정한다(프롬프트 본문·도구 집합·내부 runflow 지문).
_A0_PROMPT_SHA = "4e4f0cb00288ed32"
_RUNFLOW_FP = "4a7863994dcf7bd59c14358512de1d743dcd27f429864f9edd141ed13bcb2585"
EXTRA_GOLDEN = [
    ("A0", {}, {"prompt_sha": _A0_PROMPT_SHA}),
    ("A1", {"reranker": _Reranker()}, {"prompt_sha": _A0_PROMPT_SHA}),
    (
        "A2",
        {},
        {
            "allowed_tools": ["read_mail", "search_indexed_mail", "submit_answer"],
            "inner.runflow_fingerprint": _RUNFLOW_FP,
        },
    ),
    (
        "A3",
        {"reranker": _Reranker()},
        {"prompt_sha": _A0_PROMPT_SHA, "inner.runflow_fingerprint": _RUNFLOW_FP},
    ),
    ("A4", {"graph_digests": {"a": "d1"}}, {"prompt_sha": _A0_PROMPT_SHA}),
    ("A4", {"graph_digests": {"a": "d1"}, "a4_mode": "local"}, {"prompt_sha": _A0_PROMPT_SHA}),
    ("A4", {"graph_digests": {"a": "d1"}, "a4_mode": "global"}, {"prompt_sha": _A0_PROMPT_SHA}),
]


@pytest.mark.parametrize(("arch", "kw", "expected"), EXTRA_GOLDEN)
def test_main_arch_config_info_extras_golden(
    arch: str, kw: dict[str, Any], expected: dict[str, Any]
) -> None:
    info = RagArchSUT(rag_arch=arch, index=cast(Any, _Index()), **kw).config_info(model_id="m")
    for key, value in expected.items():
        head, _, tail = key.partition(".")
        assert (info[head][tail] if tail else info[head]) == value
    if arch in ("A0", "A1", "A3", "A4"):
        assert "allowed_tools" not in info
    if arch == "A2":
        assert "prompt_sha" not in info


def test_non_rag_runflow_fingerprint_is_unchanged_by_answer_note() -> None:
    """RunflowV0SUT 기본 경로(제품·기존 평가)의 version·지문은 답 안내 단락과 무관하다."""
    sut = RunflowV0SUT(index_mode="off")
    assert sut.version == "0+runflow-v0.8+eval-answer-v2+index-off"
    info = sut.config_info(model_id="m")
    assert info["runflow_fingerprint"] == config_fingerprint(
        "m",
        None,
        answer_extension=True,
        reminder_approval=False,  # S0: 승인 없는 리마인더
    )
    assert "rag_answer_note_sha" not in info
    note_sut = RunflowV0SUT(index_mode="off", answer_note=RAG_ANSWER_NOTE)
    assert note_sut.version != sut.version  # note가 있으면 지문이 다르다
    note_info = note_sut.config_info(model_id="m")
    assert note_info != info and note_info["answer_note_sha"] == prompt_sha(RAG_ANSWER_NOTE)
    assert note_info["runflow_fingerprint"] == info["runflow_fingerprint"]


def test_oracle_and_a2_config_carry_answer_note_sha() -> None:
    cases: list[tuple[str, dict[str, Any]]] = [("A2", {}), ("ORACLE", {"oracle_gold": {}})]
    for arch, kw in cases:
        sut = RagArchSUT(rag_arch=arch, index=cast(Any, _Index()), **kw)
        assert RAG_ANSWER_NOTE_VERSION in sut.version
        assert sut.config_info(model_id="m")["rag_answer_note_sha"] == prompt_sha(RAG_ANSWER_NOTE)


def test_answer_note_is_only_in_answer_system_prompt_not_in_helper_prompts() -> None:
    from datetime import UTC, datetime

    from orbit.evalrunner.rag_arch import config
    from orbit.evalrunner.rag_arch.graph import prompts as gp

    system = config.build_a0_system(datetime(2000, 1, 3, tzinfo=UTC), UTC, "0" * 12)
    assert system.count(RAG_ANSWER_NOTE) == 1
    helpers = (
        config.A1_REWRITE_PROMPT,
        config.A1_COMPRESS_PROMPT,
        config.A3_ROUTE_PROMPT,
        gp.ROUTE_PROMPT,
        gp.EXTRACT_PROMPT,
        gp.SUMMARY_PROMPT,
    )
    assert all(RAG_ANSWER_NOTE not in text for text in helpers)
