"""`rag_arch` A2R·A2X 통합 테스트 (ADR-0038). 데이터는 모두 합성 값이고 LLM·reranker는 가짜다."""

import json
from typing import Any

from integration import test_runflow_eval_index as _base
from integration.test_rag_arch import (
    ALL_HANDLES,
    KEEP,
    QUERY,
    REWRITTEN,
    SUBMIT,
    FakeReranker,
    run,
    world_with_mails,
)
from integration.test_runflow_eval_index import Env, call
from orbit.assistant.tools import ToolOutcome
from orbit.evalrunner.rag_arch import agentic_search
from orbit.evalrunner.rag_arch.config import A2X_TOOL_NOTE, CANDIDATES, RERANK_TOP_K

ix = _base.ix  # 같은 색인 픽스처를 이 모듈에서 쓴다


def search_results(factory: Any) -> list[dict[str, Any]]:
    """모델이 받은 첫 검색 결과의 results."""
    results: list[dict[str, Any]] = json.loads(factory.tool_results("t-1")[0].split("\n")[1])[
        "results"
    ]
    return results


def a2r_script(**args: Any) -> list[dict[str, Any]]:
    return [
        {"tool_calls": [call("search_indexed_mail", query=QUERY, **args)]},
        {"structured": SUBMIT},
        {"text": "ok"},
    ]


def a2x_script(compress: Any, **args: Any) -> list[dict[str, Any]]:
    text = compress if isinstance(compress, str) else json.dumps(compress, ensure_ascii=False)
    return [
        {"tool_calls": [call("search_indexed_mail", query=QUERY, **args)]},
        {"text": json.dumps({"query": REWRITTEN}, ensure_ascii=False)},
        {"text": text},
        {"structured": SUBMIT},
        {"text": "ok"},
    ]


async def go(
    ix: Env,
    arch: str,
    script: Any,
    accounts: tuple[str, ...] = ("acct-alice",),
    world: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any, FakeReranker]:
    world = world or world_with_mails()
    provider = await ix.build(world, list(accounts))
    fake = FakeReranker()
    summary, factory = await run(ix, arch, script, provider, world, reranker=fake)
    return summary.results[0].to_dict(), factory, fake


async def test_a2r_reranks_20_candidates_and_returns_max_results(ix: Env) -> None:
    row, factory, fake = await go(ix, "A2R", a2r_script(max_results=3))
    assert row["outcome"]["failure_type"] is None and row["usage"]["llm_calls"] == 3
    results = search_results(factory)
    nums = [int(r["subject"].split("SYNTH-RAG-")[1].split()[0]) for r in results]
    assert nums == sorted(nums, reverse=True) and len(nums) == 3  # reranker 순위, 에이전트 값
    assert fake.calls[0][0] == QUERY  # reranker 질의는 에이전트 query다
    assert all(r["excerpt"] for r in results)  # 발췌 형식은 A2와 같다
    log = row["rag_diag"]["search_log"]
    assert len(log) == 1 and row["rag_diag"]["arch"] == "A2R"
    assert len(log[0]["returned_message_ids"]) == 3
    assert len(log[0]["candidate_message_ids"]) > 3 and len(fake.calls[0][1]) == len(
        log[0]["candidate_message_ids"]
    )
    assert log[0]["rerank_ms"] >= 0 and row["rag_diag"]["search_calls"] == 1
    first = factory.made["t-1"].requests[0]
    spec = next(t for t in first.tools if t.name == "search_indexed_mail")
    assert A2X_TOOL_NOTE not in spec.description
    # 후보 읽기는 Connector 호출이므로 step으로 센다(Sim 호출 기록 = StepCounter 입력)
    reads = [c for c in row["calls"] if str(c["tool"]).endswith("get_message")]
    assert len(reads) >= len(log[0]["candidate_message_ids"])


async def test_a2x_rewrites_reranks_top5_compresses_and_ignores_max_results(ix: Env) -> None:
    row, factory, fake = await go(ix, "A2X", a2x_script(KEEP, max_results=2))
    assert row["outcome"]["failure_type"] is None and row["usage"]["llm_calls"] == 5
    requests = factory.made["t-1"].requests
    spec = next(t for t in requests[0].tools if t.name == "search_indexed_mail")
    assert spec.description.endswith(A2X_TOOL_NOTE)
    assert fake.calls[0][0] == QUERY
    results = search_results(factory)
    assert len(results) == RERANK_TOP_K
    assert all(r["excerpt"] == "견적서 납기." for r in results)  # 압축 본문
    diag = row["rag_diag"]
    assert diag["ignored_max_results"] == 1 and diag["rewrite_fallback_calls"] == 0
    assert diag["compress_fallback_mails"] == 0 and diag["search_variant"] == "advanced"
    call_log = diag["search_log"][0]
    assert call_log["rewrite_fallback"] is False and call_log["compress_fallback"] == 0
    assert len(call_log["returned_message_ids"]) == RERANK_TOP_K
    assert len(call_log["candidate_message_ids"]) <= CANDIDATES
    rewrite = requests[1]
    assert str(rewrite.messages[-1].content) == QUERY  # 재작성 입력은 에이전트 query
    dumped = json.dumps(diag, ensure_ascii=False)
    assert REWRITTEN not in dumped and QUERY not in dumped and "SYNTH-BODY" not in dumped


async def test_a2x_compress_failure_falls_back_to_original_body(ix: Env) -> None:
    row, factory, _ = await go(ix, "A2X", a2x_script("not json"))
    assert all("SYNTH-BODY" in r["excerpt"] for r in search_results(factory))
    diag = row["rag_diag"]
    assert diag["compress_fallback_mails"] == RERANK_TOP_K and diag["ignored_max_results"] == 0
    assert diag["search_log"][0]["compress_fallback"] == RERANK_TOP_K


async def test_a2x_rewrite_failure_uses_agent_query(ix: Env) -> None:
    script = a2x_script(KEEP)
    script[1] = {"text": "not json"}
    row, _, _ = await go(ix, "A2X", script)
    assert row["rag_diag"]["rewrite_fallback_calls"] == 1
    assert row["rag_diag"]["search_log"][0]["rewrite_fallback"] is True


async def test_a2x_second_search_and_read_mail_allowed(ix: Env) -> None:
    script = a2x_script(KEEP)
    script[3:3] = [
        {"tool_calls": [call("read_mail", handle="M1")]},
        {"tool_calls": [call("search_indexed_mail", query=QUERY)]},
        {"text": json.dumps({"query": REWRITTEN}, ensure_ascii=False)},
        {"text": json.dumps(KEEP, ensure_ascii=False)},
    ]
    row, _, _ = await go(ix, "A2X", script)
    assert row["outcome"]["failure_type"] is None
    assert len(row["rag_diag"]["search_log"]) == 2 and row["rag_diag"]["read_calls"] == 1


async def test_other_workspace_mail_never_becomes_candidate(ix: Env) -> None:
    for arch, script in (("A2R", a2r_script()), ("A2X", a2x_script(KEEP))):
        script[0] = {"tool_calls": [call("search_indexed_mail", query="SYNTH-BOB 점심 김밥 우동")]}
        row, _, _ = await go(ix, arch, script, accounts=("acct-alice", "acct-bob"))
        log = row["rag_diag"]["search_log"][0]
        ids = [*log["candidate_message_ids"], *log["returned_message_ids"]]
        assert ids and not any("bob" in i for i in ids)


async def test_invalid_arguments_return_tool_error_without_search(ix: Env) -> None:
    script: list[dict[str, Any]] = [
        {"tool_calls": [call("search_indexed_mail", query="  ")]},
        {"structured": SUBMIT},
        {"text": "ok"},
    ]
    row, factory, fake = await go(ix, "A2R", script)
    assert "invalid_arguments" in factory.tool_results("t-1")[0] and fake.calls == []
    assert row["rag_diag"]["search_log"] == []


def test_keep_covers_all_handles() -> None:
    assert set(KEEP) == set(ALL_HANDLES)


async def test_unreturned_candidates_are_not_registered_and_cannot_be_read(ix: Env) -> None:
    script: list[dict[str, Any]] = [
        {"tool_calls": [call("search_indexed_mail", query=QUERY, max_results=2)]},
        {"tool_calls": [call("read_mail", handle="M3"), call("read_mail", handle="M2")]},
        {"structured": SUBMIT},
        {"text": "ok"},
    ]
    row, factory, _ = await go(ix, "A2R", script)
    results = factory.tool_results("t-1")
    assert [r["handle"] for r in search_results_from(results[0])] == ["M1", "M2"]
    assert "error" in results[1] and "error" not in results[2]  # 후보였지만 보이지 않은 M3
    assert len(row["rag_diag"]["search_log"][0]["candidate_message_ids"]) > 2


def search_results_from(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = json.loads(text.split("\n")[1])["results"]
    return found


async def test_empty_compression_keeps_header_like_a1_and_is_counted(ix: Env) -> None:
    row, factory, _ = await go(ix, "A2X", a2x_script({h: [] for h in ALL_HANDLES}))
    results = search_results(factory)
    assert len(results) == RERANK_TOP_K and all(r["excerpt"] == "" for r in results)
    diag = row["rag_diag"]
    assert diag["empty_compressed_mails"] == RERANK_TOP_K
    assert diag["search_log"][0]["empty_compressed"] == RERANK_TOP_K
    assert diag["compress_fallback_mails"] == 0
    assert len(diag["context_message_ids"]) == RERANK_TOP_K  # A1과 같이 머리글만 도달로 센다


async def test_candidate_read_failure_returns_tool_error(ix: Env, monkeypatch: Any) -> None:
    class Failing:
        def __init__(self, **_: Any) -> None:
            pass

        async def execute(self, name: str, args: Any) -> ToolOutcome:
            return ToolOutcome(json.dumps({"error": "x"}), is_error=True)

    monkeypatch.setattr(agentic_search, "ReadOnlyTools", Failing)
    row, factory, fake = await go(ix, "A2R", a2r_script())
    assert "read_failed" in factory.tool_results("t-1")[0] and fake.calls == []
    assert row["rag_diag"]["search_log"] == []


async def test_empty_compression_skips_originally_blank_body(ix: Env) -> None:
    world = world_with_mails()
    blank = next(m for m in world["accounts"]["acct-alice"]["messages"] if m["id"].endswith("-6"))
    blank["body_text"] = ""
    blank["subject"] = "SYNTH-BODY-9 견적서 납기"  # FakeReranker 점수가 가장 높아 상위 5통에 든다
    row, _, _ = await go(ix, "A2X", a2x_script({h: [] for h in ALL_HANDLES}), world=world)
    log = row["rag_diag"]["search_log"][0]
    assert "msg-synth-rag-6" in log["returned_message_ids"]
    assert row["rag_diag"]["empty_compressed_mails"] == RERANK_TOP_K - 1
    assert log["empty_compressed"] == RERANK_TOP_K - 1
