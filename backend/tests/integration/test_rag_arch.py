"""`rag_arch` A0·A1·A2 통합 테스트 (ADR-0036). 데이터는 모두 합성 값이다.

평가용 색인은 `FakeEmbedder`로 만들고 LLM은 `ScriptedLLM`이다. 유료 호출은 없다.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from integration import test_runflow_eval_index as _base
from integration.test_runflow_eval_index import (
    BASE,
    Env,
    Factory,
    call,
    make_world,
    task,
)
from orbit.assistant.envelope import boundary_for
from orbit.assistant.prompt import data_rule_lines
from orbit.evaldata.schema import Turn
from orbit.evalrunner.episode import Budget
from orbit.evalrunner.metrics_v1 import judge_inputs
from orbit.evalrunner.rag_arch import sut as sut_module
from orbit.evalrunner.rag_arch.config import (
    A0_PROMPT_VERSION,
    CANDIDATES,
    RAG_ANSWER_NOTE,
    RERANK_TOP_K,
    TOP_K,
)
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runflow_index import EvalIndexProvider
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.suts import create_sut
from orbit.evalrunner.task_source import Dataset, load_dataset
from orbit.runflow.index_tool import IndexedMailSearch

ix = _base.ix  # 같은 색인 픽스처를 이 모듈에서 쓴다(이름으로 등록된다)
QUERY = "견적서 납기를 찾아 주세요"
BODY_TAIL = "가" * 40
MAIL_COUNT = 7
STEPS = 6  # A0 첫 LLM 호출 전 커넥터 호출이 5단계라 첫 호출까지만 통과한다
SUBMIT = {"kind": "message_set", "messages": ["M1"]}


def note_count(request: Any) -> int:
    """요청의 시스템 메시지에 `RAG_ANSWER_NOTE`가 몇 번 들어 있는지."""
    return sum(
        str(m.content).count(RAG_ANSWER_NOTE) for m in request.messages if m.role == "system"
    )


def body_text(i: int) -> str:
    return f"SYNTH-BODY-{i}\n견적서 납기.\n{BODY_TAIL}."  # 문장 단위 압축 검증용


def world_with_mails() -> dict[str, Any]:
    world = make_world()
    for i in range(MAIL_COUNT):
        state = world["accounts"]["acct-alice"]
        mail_id = f"msg-synth-rag-{i}"
        state["threads"].append({"thread_id": f"thr-{mail_id}", "message_ids": [mail_id]})
        state["messages"].append(
            {
                "id": mail_id,
                "thread_id": f"thr-{mail_id}",
                "from": {"email": "sender@example.org", "name": "합성 발신자"},
                "to": [{"email": state["email"], "name": "수신자"}],
                "cc": [{"email": "cc@example.org", "name": "참조자"}],
                "subject": f"SYNTH-RAG-{i} 견적서 납기",
                "body_text": body_text(i),
                "snippet": "견적서 납기",
                "sent_at": "2026-11-05T09:00:00+00:00",
                "labels": ["INBOX"],
                "attachments": [],
                "headers": {},
            }
        )
    return world


async def run(
    ix: Env,
    arch: str,
    script: Sequence[Mapping[str, Any]],
    provider: EvalIndexProvider,
    world: dict[str, Any],
    cap: int = 6000,
    turns: int = 1,
    reranker: Any = None,
    budget: Budget | None = None,
) -> tuple[ExperimentSummary, Factory]:
    base = load_dataset(BASE)
    one = task("t-1")
    if turns > 1:
        extra = [Turn(role="user", content="그 메일도 알려 주세요")] * (turns - 1)
        one = one.model_copy(update={"turns": [*one.turns, *extra]})
    data = Dataset(base.name, base.version, base.manifest, world, (one,))
    factory = Factory({"t-1": script})
    summary = await run_experiment(
        data,
        split="dev",
        sut_factory=lambda: create_sut(
            "rag_arch",
            rag_arch=arch,
            index_mode="on",
            index=provider,
            context_char_cap=cap,
            **({"reranker": reranker} if reranker is not None else {}),
        ),
        llm_factory=factory,
        repeats=1,
        budget=budget,
        git_sha="test-sha",
        database_env=RunflowEnvironment(ix.app_url),
    )
    return summary, factory


async def test_a0_one_search_one_call_k5_headers_and_handles_only(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await run(
        ix, "A0", [{"structured": SUBMIT, "text": "확인했습니다."}], provider, world
    )
    row = summary.results[0].to_dict()
    assert row["outcome"]["failure_type"] is None
    requests = factory.made["t-1"].requests
    assert len(requests) == 1  # 답변 LLM 호출 1회
    assert [t.name for t in requests[0].tools] == ["submit_answer"]
    assert note_count(requests[0]) == 1
    prompt = "\n".join(str(m.content) for m in requests[0].messages)
    diag = row["rag_diag"]
    assert diag["search_calls"] == 1 and diag["arch"] == "A0"
    assert len(diag["context_message_ids"]) == TOP_K == 5
    assert diag["truncated"] is False
    for header in ('"from"', '"to"', '"cc"', '"date"', '"subject"', '"has_attachment"'):
        assert header in prompt
    assert "cc@example.org" in prompt and "SYNTH-BODY-" in prompt
    assert "M1" in prompt and "msg-synth" not in prompt  # 모델에는 핸들만 보인다
    assert row["usage"]["llm_calls"] == 1
    info = row["config"]["sut_config"]  # 지문에 들어가는 조건(상한·프롬프트 버전·k)
    assert info["context_char_cap"] == 6000 and info["top_k"] == 5
    assert info["prompt_version"] == A0_PROMPT_VERSION and info["rag_arch"] == "A0"
    cited = row["answer_ids"]["messages"]
    assert (
        cited[0]["message_id"] in diag["context_message_ids"]
    )  # 핸들이 제공자 ID로 바뀌어 기록된다


async def test_a0_cap_cuts_text_and_records_it(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    body = len(body_text(0))
    cap = body + 10  # 첫 메일은 온전히, 둘째 메일은 10자만, 나머지는 들어가지 못한다
    summary, factory = await run(
        ix, "A0", [{"structured": SUBMIT, "text": "ok"}], provider, world, cap
    )
    diag = summary.results[0].to_dict()["rag_diag"]
    assert diag["truncated"] is True and diag["context_chars"] == cap
    assert len(diag["context_message_ids"]) == 2
    prompt = str(factory.made["t-1"].requests[0].messages[-1].content)
    assert prompt.count("<orbit_data ") == 2  # 상한이 찬 뒤의 메일은 넣지 않는다
    # Judge 입력: 모델 입력에 실제 들어간 문맥(상한 적용 뒤)을 도구 결과로 읽는다
    row = summary.results[0].to_dict()
    (judged,) = judge_inputs([row])
    results = [t["result"] for t in judged["tool_results"] if t["name"] == "read_mail"]
    assert len(results) == 2 and all("SYNTH-" in r for r in results)
    assert "omitted" not in "".join(results)
    assert sum(len(json.loads(r)["body"]) for r in results) == cap


async def test_a2_limits_tool_list_and_execution_and_records_reach(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    script: list[dict[str, Any]] = [
        {
            "tool_calls": [
                call("search_indexed_mail", query=QUERY),
                call(
                    "list_events",
                    calendar_id="cal-alice-main",
                    time_min="2026-11-12T00:00:00+09:00",
                    time_max="2026-11-13T00:00:00+09:00",
                ),
            ]
        },
        {"tool_calls": [call("read_mail", handle="M1")]},
        {"structured": SUBMIT},
        {"text": "확인했습니다."},
    ]
    summary, factory = await run(ix, "A2", script, provider, world)
    row = summary.results[0].to_dict()
    first = factory.made["t-1"].requests[0]
    assert {t.name for t in first.tools} == {"search_indexed_mail", "read_mail", "submit_answer"}
    assert [note_count(r) for r in factory.made["t-1"].requests] == [
        1,
        1,
        1,
        1,
    ]  # 답 제출 확장 켠 모든 step
    results = factory.tool_results("t-1")
    assert any("tool_not_allowed" in r for r in results)  # 허용 밖 호출은 실행하지 않는다
    diag = row["rag_diag"]
    assert diag["denied_tools"] == ["list_events"]
    assert diag["search_calls"] == 1 and diag["read_calls"] == 1
    assert len(diag["context_message_ids"]) >= TOP_K  # 검색 결과(excerpt)와 읽은 메일의 합집합
    assert row["config"]["sut_config"]["rag_arch"] == "A2"


async def test_a2_cap_is_shared_by_tool_results(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    script: list[dict[str, Any]] = [
        {"tool_calls": [call("search_indexed_mail", query=QUERY)]},
        {"structured": SUBMIT},
        {"text": "ok"},
    ]
    summary, factory = await run(ix, "A2", script, provider, world, cap=60)
    diag = summary.results[0].to_dict()["rag_diag"]
    assert diag["truncated"] is True and diag["context_chars"] == 60
    found = json.loads(factory.tool_results("t-1")[0].split("\n")[1])
    assert any(r.get("excerpt_truncated") for r in found["results"])


async def test_a0_records_search_in_trace_without_excerpt(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, _ = await run(ix, "A0", [{"structured": SUBMIT, "text": "ok"}], provider, world)
    calls = summary.results[0].to_dict()["trace"]["tool_calls"]
    assert [c["name"] for c in calls][0] == "search_indexed_mail"
    found = json.loads(calls[0]["result"])
    assert len(found["results"]) == TOP_K and all(r["excerpt"] is None for r in found["results"])


async def test_a0_retrieves_10_but_top5_input_and_order_unchanged(
    ix: Env, monkeypatch: Any
) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    script = [{"structured": SUBMIT, "text": "ok"}]
    monkeypatch.setattr(sut_module, "RETRIEVAL_K", TOP_K)  # 옛 동작: 5개만 검색
    old, old_factory = await run(ix, "A0", script, provider, world)
    monkeypatch.undo()
    new, new_factory = await run(ix, "A0", script, provider, world)
    old_diag, new_diag = old.results[0].to_dict()["rag_diag"], new.results[0].to_dict()["rag_diag"]
    assert len(old_diag["ranked_message_ids"]) == TOP_K
    assert TOP_K < len(new_diag["ranked_message_ids"]) <= 10
    assert new_diag["ranked_message_ids"][:TOP_K] == old_diag["ranked_message_ids"]  # 순서 같음
    assert new_diag["context_message_ids"] == old_diag["context_message_ids"]
    old_msgs = [m.content for m in old_factory.made["t-1"].requests[0].messages]
    new_msgs = [m.content for m in new_factory.made["t-1"].requests[0].messages]
    assert new_msgs == old_msgs  # 답 입력 동일
    assert (
        new.results[0].to_dict()["trace"]["tool_calls"][0]
        == (old.results[0].to_dict()["trace"]["tool_calls"][0])
    )


async def test_a0_rejects_multi_turn_task(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await run(ix, "A0", [{"text": "ok"}], provider, world, turns=2)
    assert summary.results[0].to_dict()["outcome"]["failure_type"] is not None
    assert "t-1" not in factory.made or not factory.made["t-1"].requests  # LLM을 부르지 않는다


async def test_a0_fails_when_index_is_pending(ix: Env, monkeypatch: Any) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    real = IndexedMailSearch.execute

    async def pending(self: Any, *a: Any, **kw: Any) -> Any:
        out = await real(self, *a, **kw)
        return type(out)(json.dumps({**json.loads(out.content), "index_pending": True}))

    monkeypatch.setattr(IndexedMailSearch, "execute", pending)
    summary, factory = await run(ix, "A0", [{"text": "ok"}], provider, world)
    assert summary.results[0].to_dict()["outcome"]["failure_type"] is not None
    assert not factory.made["t-1"].requests  # 부분 맥락으로 답하지 않는다


# ---------------------------------------------------------------- A1

REWRITTEN = "REWRITTEN-QUERY 견적서 납기"
ALL_HANDLES = [f"M{i}" for i in range(1, 31)]  # 색인에는 기본 세계의 메일도 있다


class FakeReranker:
    """본문 번호가 클수록 점수가 높다. 질의와 문서를 기록한다."""

    model_id = "fake/reranker"
    revision = "r0"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, passages: Any) -> list[float]:
        self.calls.append((query, list(passages)))
        return [
            float(p.split("SYNTH-BODY-")[1].split()[0]) if "SYNTH-BODY-" in p else -1.0
            for p in passages
        ]


def a1_script(compress: Any, rewrite: str | None = None) -> list[dict[str, Any]]:
    text = compress if isinstance(compress, str) else json.dumps(compress, ensure_ascii=False)
    first = rewrite if rewrite is not None else json.dumps({"query": REWRITTEN}, ensure_ascii=False)
    return [{"text": first}, {"text": text}, {"structured": SUBMIT, "text": "ok"}]


KEEP = {h: ["견적서 납기."] for h in ALL_HANDLES}


async def run_a1(ix: Env, script: Any, cap: int = 6000) -> tuple[dict[str, Any], Factory, Any]:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    fake = FakeReranker()
    summary, factory = await run(ix, "A1", script, provider, world, cap, reranker=fake)
    return summary.results[0].to_dict(), factory, fake


def read_results(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(c["result"]) for c in row["trace"]["tool_calls"] if c["name"] == "read_mail"]


async def test_a1_three_calls_rerank_compress_and_trace_matches_model_input(ix: Env) -> None:
    row, factory, fake = await run_a1(ix, a1_script(KEEP))
    assert row["outcome"]["failure_type"] is None and row["usage"]["llm_calls"] == 3
    rewrite, compress, answer = factory.made["t-1"].requests
    assert [note_count(r) for r in (rewrite, compress, answer)] == [0, 0, 1]
    question = str(rewrite.messages[-1].content)
    assert fake.calls[0][0] == question  # reranker 질의는 질문 원문이다
    assert len(fake.calls[0][1]) >= MAIL_COUNT and any("SYNTH-RAG-" in p for p in fake.calls[0][1])
    names = [c["name"] for c in row["trace"]["tool_calls"]]
    assert names.count("search_indexed_mail") == 1 and names.count("read_mail") == RERANK_TOP_K
    search = next(c for c in row["trace"]["tool_calls"] if c["name"] == "search_indexed_mail")
    assert search["arguments"] == {"query": REWRITTEN, "max_results": CANDIDATES}
    prompt = str(answer.messages[-1].content)
    for result in read_results(row):
        assert result["body"] == "견적서 납기." and "cc" in result and "subject" in result
    for c in row["trace"]["tool_calls"]:
        if c["name"] == "read_mail":
            assert c["result"] in prompt  # Judge가 읽는 내용은 모델 입력과 같다
    assert [t.name for t in answer.tools] == ["submit_answer"]
    assert rewrite.max_output_tokens and compress.max_output_tokens
    diag = row["rag_diag"]
    assert diag["arch"] == "A1" and diag["rewrite_fallback"] is False
    first_stage = diag["first_stage_ranked_message_ids"]
    assert len(first_stage) >= MAIL_COUNT and len(fake.calls[0][1]) == len(first_stage)
    reranked = diag["reranked_message_ids"]
    assert len(reranked) == RERANK_TOP_K and reranked == sorted(reranked, reverse=True)
    ranked = diag["ranked_message_ids"]  # reranker 정렬 전체 순위. 상위 5개가 답 입력
    assert sorted(ranked) == sorted(diag["first_stage_ranked_message_ids"])
    assert ranked[:RERANK_TOP_K] == reranked
    assert diag["compress_fallback"] == 0
    assert diag["compressed_chars_after"] < diag["compressed_chars_before"]
    assert sorted(diag["context_message_ids"]) == sorted(reranked)
    dumped = json.dumps(diag, ensure_ascii=False)
    assert REWRITTEN not in dumped and "SYNTH-BODY" not in dumped  # ID와 수치만(ADR-0006)
    info = row["config"]["sut_config"]
    assert info["reranker"]["revision"] == "r0" and info["candidates"] == CANDIDATES


async def test_a1_rewrite_fallback_uses_original_question(ix: Env) -> None:
    for bad in ("not json", json.dumps({"query": "  "}), json.dumps({"query": "x" * 501})):
        row, factory, _ = await run_a1(ix, a1_script(KEEP, rewrite=bad))
        question = str(factory.made["t-1"].requests[0].messages[-1].content)
        search = next(c for c in row["trace"]["tool_calls"] if c["name"] == "search_indexed_mail")
        assert row["rag_diag"]["rewrite_fallback"] is True
        assert question in json.dumps(search["arguments"], ensure_ascii=False)


@pytest.mark.parametrize(
    ("compress", "fallbacks", "body"),
    [
        ("not json", 5, "orig"),
        ({}, 5, "orig"),  # 핸들 누락
        ({h: ["원문에 없는 문장"] for h in ALL_HANDLES}, 5, "orig"),
        ({h: "견적서 납기." for h in ALL_HANDLES}, 5, "orig"),  # 형식 오류
        ({h: ["견적서   납기."] for h in ALL_HANDLES}, 0, "견적서 납기."),  # 공백 정규화
        ({h: ["견적서 납기"] for h in ALL_HANDLES}, 5, "orig"),  # 문장 일부(마침표 없음)
        ({h: [] for h in ALL_HANDLES}, 0, ""),  # 관련 문장 없음: 머리글만 넣는다
    ],
)
async def test_a1_compress_validation_and_fallback(
    ix: Env, compress: Any, fallbacks: int, body: str
) -> None:
    row, factory, _ = await run_a1(ix, a1_script(compress))
    assert row["rag_diag"]["compress_fallback"] == fallbacks
    assert row["rag_diag"]["compressed_empty"] == (RERANK_TOP_K if body == "" else 0)
    results = read_results(row)
    # 압축 뒤 본문이 비어도 머리글이 들어갔으므로 도달로 센다
    assert len(results) == RERANK_TOP_K and len(row["rag_diag"]["context_message_ids"]) == 5
    answer = str(factory.made["t-1"].requests[-1].messages[-1].content)
    assert (
        "sender@example.org" in answer and "cc@example.org" in answer
    )  # 머리글이 모델 입력에 있다
    for r in results:
        assert r["body"].startswith("SYNTH-BODY-") if body == "orig" else r["body"] == body
        assert r["subject"] and r["from"]  # 머리글은 남는다


async def test_a1_one_bad_mail_reverts_only_that_mail(ix: Env) -> None:
    first, _, _ = await run_a1(ix, a1_script(KEEP))
    top = read_results(first)[0]["handle"]  # 재정렬 1위 메일. 핸들은 검색 순서로 정해져 결정적이다
    row, _, _ = await run_a1(ix, a1_script({**KEEP, top: ["없는 문장"]}))
    assert row["rag_diag"]["compress_fallback"] == 1
    bodies = [r["body"] for r in read_results(row)]
    assert bodies[0].startswith("SYNTH-BODY-") and bodies[1:] == ["견적서 납기."] * 4


async def test_a1_cap_applies_after_compression_in_rerank_order(ix: Env) -> None:
    row, _, _ = await run_a1(ix, a1_script(KEEP), cap=len("견적서 납기.") * 2 + 3)
    diag = row["rag_diag"]
    assert diag["truncated"] is True and diag["context_chars"] == len("견적서 납기.") * 2 + 3
    assert len(read_results(row)) == 3  # 셋째 메일은 3자만 들어간다


async def test_a1_rejects_multi_turn_task(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await run(
        ix, "A1", a1_script(KEEP), provider, world, turns=2, reranker=FakeReranker()
    )
    assert summary.results[0].to_dict()["outcome"]["failure_type"] is not None
    assert "t-1" not in factory.made or not factory.made["t-1"].requests


async def test_a1_search_failure_stops_before_compress_and_answer(
    ix: Env, monkeypatch: Any
) -> None:
    real = IndexedMailSearch.execute

    async def pending(self: Any, *a: Any, **kw: Any) -> Any:
        out = await real(self, *a, **kw)
        return type(out)(json.dumps({**json.loads(out.content), "index_pending": True}))

    monkeypatch.setattr(IndexedMailSearch, "execute", pending)
    row, factory, fake = await run_a1(ix, a1_script(KEEP))
    assert row["outcome"]["failure_type"] is not None
    assert len(factory.made["t-1"].requests) == 1 and not fake.calls


@pytest.fixture
def finish_length(monkeypatch: Any) -> None:
    """스크립트 항목의 `finish_reason`을 응답에 반영한다(스크립트 형식에는 없는 필드)."""
    import dataclasses

    from orbit.evalrunner import llm_script

    real = llm_script.build_response

    def build(item: Any) -> Any:
        response = real(item)
        if "finish_reason" not in item:  # tool_calls 응답은 원래 모양을 유지한다
            return response
        return dataclasses.replace(response, finish_reason=item["finish_reason"])

    monkeypatch.setattr(llm_script, "build_response", build)


async def test_a1_compress_request_wraps_bodies_and_has_data_rules(ix: Env) -> None:
    row, factory, _ = await run_a1(ix, a1_script(KEEP))
    _, compress, _ = factory.made["t-1"].requests
    system, user = (str(m.content) for m in compress.messages)
    boundary = boundary_for("a1:t-1")
    assert all(line in system for line in data_rule_lines(boundary))
    assert user.count("mail_body") >= RERANK_TOP_K and boundary in user
    assert "SYNTH-BODY-" in user and "cc@example.org" not in user  # 본문만 넣는다


@pytest.mark.parametrize(
    "extra", [{"finish_reason": "length"}, {"tool_calls": [call("read_mail")]}]
)
async def test_a1_rewrite_format_failure_falls_back(
    ix: Env, finish_length: None, extra: dict[str, Any]
) -> None:
    script = a1_script(KEEP)
    script[0] = {**script[0], **extra}  # 내용은 올바른 JSON이어도 형식 실패로 본다
    row, factory, _ = await run_a1(ix, script)
    question = str(factory.made["t-1"].requests[0].messages[-1].content)
    search = next(c for c in row["trace"]["tool_calls"] if c["name"] == "search_indexed_mail")
    assert row["rag_diag"]["rewrite_fallback"] is True
    assert question in json.dumps(search["arguments"], ensure_ascii=False)


@pytest.mark.parametrize(
    "extra", [{"finish_reason": "length"}, {"tool_calls": [call("read_mail")]}]
)
async def test_a1_compress_format_failure_reverts_all(
    ix: Env, finish_length: None, extra: dict[str, Any]
) -> None:
    script = a1_script(KEEP)
    script[1] = {**script[1], **extra}
    row, _, _ = await run_a1(ix, script)
    assert row["rag_diag"]["compress_fallback"] == RERANK_TOP_K
    assert all(r["body"].startswith("SYNTH-BODY-") for r in read_results(row))


async def test_a1_reranker_score_count_mismatch_fails_episode(ix: Env) -> None:
    class Short(FakeReranker):
        def score(self, query: str, passages: Any) -> list[float]:
            return [0.0]

    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await run(ix, "A1", a1_script(KEEP), provider, world, reranker=Short())
    assert summary.results[0].to_dict()["outcome"]["failure_type"] is not None
    assert len(factory.made["t-1"].requests) == 1  # 재작성만 불렀고 압축·답은 부르지 않았다


@pytest.mark.parametrize("failing", [0, 1])
async def test_a1_provider_error_fails_episode_without_fallback_count(
    ix: Env, failing: int
) -> None:
    script = a1_script(KEEP)[:failing]  # 해당 호출에서 ScriptExhaustedError가 난다
    row, factory, _ = await run_a1(ix, script)
    assert row["outcome"]["failure_type"] is not None
    assert not row.get("rag_diag")  # 폴백 횟수가 집계되지 않는다
    assert len(factory.made["t-1"].requests) == failing + 1


async def test_a1_zero_candidates_skips_rerank_and_compress(ix: Env, monkeypatch: Any) -> None:
    real = IndexedMailSearch.execute

    async def empty(self: Any, *a: Any, **kw: Any) -> Any:
        out = await real(self, *a, **kw)
        return type(out)(json.dumps({**json.loads(out.content), "results": []}))

    monkeypatch.setattr(IndexedMailSearch, "execute", empty)
    script = [a1_script(KEEP)[0], a1_script(KEEP)[2]]  # 재작성, 답
    row, factory, fake = await run_a1(ix, script)
    assert row["outcome"]["failure_type"] is None and not fake.calls
    assert len(factory.made["t-1"].requests) == 2  # 압축 호출 없이 답 1회
    assert row["rag_diag"]["reranked_message_ids"] == []
    assert row["rag_diag"]["compress_fallback"] == 0


# ------------------------------------------------ 답 후속 호출(answer flow v2)


def _flow_script(arch: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return items if arch == "A0" else [*a1_script(KEEP)[:2], *items]


async def _flow(
    ix: Env, arch: str, items: list[dict[str, Any]], budget: Budget | None = None
) -> tuple[dict[str, Any], Factory]:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await run(
        ix, arch, _flow_script(arch, items), provider, world, reranker=FakeReranker(), budget=budget
    )
    return summary.results[0].to_dict(), factory


@pytest.mark.parametrize("arch", ["A0", "A1"])
async def test_answer_followup_when_tool_only(ix: Env, arch: str) -> None:
    row, factory = await _flow(ix, arch, [{"structured": SUBMIT}, {"text": "최종 답입니다."}])
    assert row["outcome"]["failure_type"] is None
    assert row["rag_diag"]["answer_followup"] is True
    assert row["trace"]["final_text"] == "최종 답입니다."
    assert row["usage"]["llm_calls"] == (2 if arch == "A0" else 4)
    first, second = factory.made["t-1"].requests[-2:]
    assert [m.role for m in second.messages[-2:]] == ["assistant", "tool"]
    assistant, tool = second.messages[-2:]
    assert assistant.tool_calls and tool.tool_call_id == assistant.tool_calls[0].id
    assert second.messages[:2] == first.messages and second.tool_choice == "none"
    assert row["answer_ids"]["messages"]  # 구조화 답은 첫 호출 것을 유지한다


@pytest.mark.parametrize("items", [[{"structured": SUBMIT, "text": "답"}], [{"text": ""}]])
async def test_no_followup_with_text_or_without_tool(ix: Env, items: list[dict[str, Any]]) -> None:
    row, factory = await _flow(ix, "A0", items)
    assert row["rag_diag"]["answer_followup"] is False
    assert len(factory.made["t-1"].requests) == 1


@pytest.mark.parametrize("arch", ["A0", "A1"])
async def test_followup_provider_error_fails_episode(ix: Env, arch: str) -> None:
    row, factory = await _flow(ix, arch, [{"structured": SUBMIT}])  # 두 번째 호출에서 스크립트 소진
    assert row["outcome"]["failure_type"] is not None


async def test_followup_stops_at_step_budget_after_first_call(ix: Env) -> None:
    # 첫 호출까지 steps가 STEPS가 되도록 상한을 맞춘다. 후속 consume()이 넘는다
    row, factory = await _flow(
        ix, "A0", [{"structured": SUBMIT}, {"text": "호출되면 안 됨"}], Budget(max_steps=STEPS)
    )
    assert row["outcome"]["failure_type"] == "budget_exceeded"
    assert len(factory.made["t-1"].requests) == 1


async def test_followup_stops_at_token_budget_after_first_call(ix: Env) -> None:
    items: list[dict[str, Any]] = [
        {"structured": SUBMIT, "input_tokens": 10},
        {"text": "호출되면 안 됨"},
    ]
    row, factory = await _flow(ix, "A0", items, Budget(max_tokens=5))
    assert row["outcome"]["failure_type"] == "budget_exceeded"
    assert len(factory.made["t-1"].requests) == 1


async def test_a0_disallowed_tool_with_submit_gets_two_tool_messages_and_followup(
    ix: Env,
) -> None:
    other = {"name": "read_mail", "arguments": {}}
    row, factory = await _flow(
        ix, "A0", [{"structured": SUBMIT, "tool_calls": [other]}, {"text": "최종 답입니다."}]
    )
    assert row["rag_diag"]["answer_followup"] is True
    requests = factory.made["t-1"].requests
    assert len(requests) == 2
    tools = [m for m in requests[1].messages if m.role == "tool"]
    assert len(tools) == 2
    assert any("tool_not_allowed" in str(m.content) for m in tools)


# ---------------------------------------------------------------- A3


def _route(route: str | None) -> dict[str, Any]:
    return {"text": json.dumps({"route": route})}


async def _a3(ix: Env, script: list[dict[str, Any]]) -> tuple[dict[str, Any], Factory]:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await run(ix, "A3", script, provider, world, reranker=FakeReranker())
    return summary.results[0].to_dict(), factory


async def test_a3_none_route_answers_once_without_search(ix: Env) -> None:
    row, factory = await _a3(ix, [_route("none"), {"structured": SUBMIT, "text": "답"}])
    assert row["outcome"]["failure_type"] is None
    diag = row["rag_diag"]
    assert diag["arch"] == "A3" and diag["a3_route"] == "none" and diag["route_fallback"] is False
    assert diag["search_calls"] == 0 and diag["context_message_ids"] == []
    assert row["usage"]["llm_calls"] == 2  # 라우터 포함
    route_req, answer_req = factory.made["t-1"].requests
    assert [t.name for t in answer_req.tools] == ["submit_answer"]
    assert (note_count(route_req), note_count(answer_req)) == (0, 1)
    assert "SYNTH-BODY-" not in "\n".join(str(m.content) for m in answer_req.messages)
    # 라우터 입력은 시스템 프롬프트와 질문 문장뿐이다
    assert [m.role for m in route_req.messages] == ["system", "user"]
    assert not route_req.tools


async def test_a3_a1_route_runs_a1_flow(ix: Env) -> None:
    row, factory = await _a3(ix, [_route("A1"), *a1_script(KEEP)])
    diag = row["rag_diag"]
    assert diag["a3_route"] == "A1" and diag["arch"] == "A3"
    assert "rewrite_fallback" in diag and diag["reranked_message_ids"]
    assert row["usage"]["llm_calls"] == 4  # 라우터 + 재작성 + 압축 + 답
    assert [note_count(r) for r in factory.made["t-1"].requests] == [0, 0, 0, 1]


async def test_a3_a2_route_runs_react_with_limited_tools(ix: Env) -> None:
    script: list[dict[str, Any]] = [
        _route("A2"),
        {"tool_calls": [call("search_indexed_mail", query=QUERY)]},
        {"structured": SUBMIT},
        {"text": "확인했습니다."},
    ]
    row, factory = await _a3(ix, script)
    diag = row["rag_diag"]
    assert diag["a3_route"] == "A2" and diag["arch"] == "A3" and diag["search_calls"] == 1
    assert {t.name for t in factory.made["t-1"].requests[1].tools} == {
        "search_indexed_mail",
        "read_mail",
        "submit_answer",
    }
    assert [note_count(r) for r in factory.made["t-1"].requests] == [0, 1, 1, 1]  # 라우터 제외


@pytest.mark.parametrize("bad", ["not json", json.dumps({"route": "A9"}), json.dumps(["A1"])])
async def test_a3_bad_route_output_falls_back_to_a2_and_is_recorded(ix: Env, bad: str) -> None:
    script: list[dict[str, Any]] = [{"text": bad}, {"structured": SUBMIT}, {"text": "답"}]
    row, factory = await _a3(ix, script)
    diag = row["rag_diag"]
    assert diag["a3_route"] == "A2" and diag["route_fallback"] is True
    assert len(factory.made["t-1"].requests) == 3  # 라우터 1회 + A2 2회. 라우터는 재시도하지 않는다


async def test_a3_rejects_multi_turn_task_before_routing(ix: Env) -> None:
    world = world_with_mails()
    provider = await ix.build(world, ["acct-alice"])
    summary, factory = await run(
        ix, "A3", [_route("none")], provider, world, turns=2, reranker=FakeReranker()
    )
    assert summary.results[0].to_dict()["outcome"]["failure_type"] is not None
    assert not factory.made["t-1"].requests
