"""`runflow_v0 --search-variant advanced` 통합 테스트(ADR-0041). 합성 데이터, 유료 호출 없음.

실제 orbit_test DB와 `FakeEmbedder` 색인, 가짜 reranker, `ScriptedLLM`으로 에피소드를 실행한다.
"""

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from integration import test_runflow_eval_index as _base
from integration.test_rag_arch import (
    ALL_HANDLES,
    QUERY,
    SUBMIT,
    FakeReranker,
    world_with_mails,
)
from integration.test_runflow_eval_index import BASE, Env, Factory, call, task
from orbit.evalrunner.episode import Budget
from orbit.evalrunner.rag_arch.config import A2X_TOOL_NOTE, CANDIDATES, RERANK_TOP_K
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.evalrunner.runner import run_experiment
from orbit.evalrunner.task_source import Dataset, load_dataset

ix = _base.ix
KEEP = {h: ["견적서 납기."] for h in ALL_HANDLES}
SEARCH = {"tool_calls": [call("search_indexed_mail", query=QUERY)]}
FINAL = [{"structured": SUBMIT}, {"text": "확인했습니다."}]
ADVANCED_SCRIPT = [
    SEARCH,
    {"text": json.dumps({"query": "견적서 납기"}, ensure_ascii=False)},  # 재작성
    {"text": json.dumps(KEEP, ensure_ascii=False)},  # 압축
    *FINAL,
]


async def episode(
    ix: Env, script: Sequence[Any], *, advanced: bool
) -> tuple[dict[str, Any], Factory, FakeReranker]:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    fake = FakeReranker()
    base = load_dataset(BASE)
    data = Dataset(base.name, base.version, base.manifest, world, (task("t-1"),))
    factory = Factory({"t-1": script})
    options: dict[str, Any] = {"search_variant": "advanced", "reranker": fake} if advanced else {}
    summary = await run_experiment(
        data,
        split="dev",
        sut_factory=lambda: RunflowV0SUT(index_mode="on", index=provider, **options),
        llm_factory=factory,
        repeats=1,
        budget=Budget(150),
        git_sha="test-sha",
        database_env=RunflowEnvironment(ix.app_url),
    )
    return summary.results[0].to_dict(), factory, fake


async def test_advanced_runs_a2x_search_with_default_tools_and_prompt(ix: Env) -> None:
    row, factory, fake = await episode(ix, ADVANCED_SCRIPT, advanced=True)
    base_row, base_factory, _ = await episode(ix, [SEARCH, *FINAL], advanced=False)
    assert row["outcome"]["failure_type"] is None
    assert row["usage"]["llm_calls"] == 5  # 에이전트 3(검색·제출·답) + 재작성 1 + 압축 1
    requests = factory.made["t-1"].requests
    first, last = requests[0], requests[-1]
    default_requests = base_factory.made["t-1"].requests
    default_first, default_last = default_requests[0], default_requests[-1]
    # 시스템 프롬프트와 도구 목록은 default와 같고, 검색 도구 설명 끝에 노트 한 문장만 다르다
    assert first.messages[0] == default_first.messages[0]
    assert [t.name for t in first.tools] == [t.name for t in default_first.tools]
    for ours, theirs in zip(first.tools, default_first.tools, strict=True):
        expected = (
            f"{theirs.description} {A2X_TOOL_NOTE}"
            if ours.name == "search_indexed_mail"
            else theirs.description
        )
        assert ours.description == expected
        assert dataclasses.replace(ours, description="") == dataclasses.replace(
            theirs, description=""
        )
    assert [t.name for t in last.tools] == [t.name for t in default_last.tools]
    # 검색은 재작성 질의로 후보 CANDIDATES통을 가져오고 reranker 질의는 에이전트 원래 질의다
    assert len(fake.calls) == 1 and fake.calls[0][0] == QUERY
    search_call = next(c for c in row["trace"]["tool_calls"] if c["name"] == "search_indexed_mail")
    assert search_call["arguments"] == {"query": QUERY}
    # 압축 본문이 결과 excerpt에 들어가고 원문 마커는 모델 입력에 없다
    result = factory.tool_results("t-1")[0]
    assert "견적서 납기." in result and "SYNTH-BODY-" not in result
    diag = row["rag_diag"]
    assert diag["search_variant"] == "advanced" and len(diag["search_log"]) == 1
    assert diag["arch"] == "A2X"
    # ToolLimits 진단도 함께 나온다(검색 1회, 상한 없음)
    assert diag["search_calls"] == 1 and diag["truncated"] is False and "index_pending" in diag
    log = diag["search_log"][0]
    assert len(log["returned_message_ids"]) == RERANK_TOP_K
    assert len(log["candidate_message_ids"]) <= CANDIDATES
    assert log["rewrite_fallback"] is False and log["compress_fallback"] == 0


async def test_default_variant_has_no_diag_and_no_note(ix: Env) -> None:
    row, factory, _ = await episode(ix, [SEARCH, *FINAL], advanced=False)
    assert "rag_diag" not in row
    descriptions = " ".join(t.description for t in factory.made["t-1"].requests[0].tools)
    assert A2X_TOOL_NOTE not in descriptions
