"""실제 orbit_test/RLS/그래프와 가짜 임베딩·LLM으로 RAG opt-in을 검증한다."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from integration.runflow_support import Harness
from integration.test_runflow_eval import only
from integration.test_runflow_eval_index import (
    ALICE_MAIL,
    BASE,
    Env,
    Factory,
    call,
    make_world,
    task,
)
from integration.test_runflow_eval_index import ix as ix
from orbit.assistant.tools import ToolOutcome
from orbit.evaldata.schema import Turn
from orbit.evalrunner.episode import Budget
from orbit.evalrunner.runflow_env import RunflowEnvironment
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.evalrunner.runner import ExperimentSummary, run_experiment
from orbit.evalrunner.task_source import Dataset, load_dataset
from orbit.llm.interface import LLMRequestError
from orbit.llm.scripted import ScriptedLLM
from orbit.runflow.nodes import RunNodes
from orbit.runflow.rag import RagPolicy, initial_call_id
from orbit.runflow.state import RunState

Make = Callable[..., Awaitable[Harness]]


async def evaluate(
    ix: Env,
    pattern: str,
    script: Sequence[dict[str, Any]],
    *,
    turns: list[str] | None = None,
    budget: Budget | None = None,
) -> tuple[ExperimentSummary, Factory]:
    world = make_world()
    provider = await ix.build(world, ["acct-alice", "acct-bob"])
    t = task("t-rag")
    if turns is not None:
        t = t.model_copy(update={"turns": [Turn(role="user", content=s) for s in turns]})
    base = load_dataset(BASE)
    data = Dataset(base.name, base.version, base.manifest, world, (t,))
    factory = Factory({t.task_id: script})
    summary = await run_experiment(
        data,
        split="dev",
        repeats=1,
        sut_factory=lambda: RunflowV0SUT(index_mode="on", index=provider, rag_pattern=pattern),
        llm_factory=factory,
        database_env=RunflowEnvironment(ix.app_url),
        budget=budget,
    )
    return summary, factory


async def test_retrieve_once_uses_actual_index_handles_envelope_and_rejects_extra(ix: Env) -> None:
    summary, factory = await evaluate(
        ix,
        "retrieve_once",
        [
            {"tool_calls": [call("search_indexed_mail", query="다른질의", max_results=20)]},
            {"tool_calls": [call("read_mail", handle="M1")]},
            {"text": "확인했습니다."},
        ],
    )
    row = only(summary)
    assert row.failure_type is None
    llm = factory.made["t-rag"]
    assert all("search_indexed_mail" not in {s.name for s in r.tools} for r in llm.requests)
    payload = "\n".join(str(m.content) for m in llm.requests[-1].messages)
    assert ALICE_MAIL["body"] in payload
    assert "SYNTH-BOB" not in payload and "msg-synth-alice" not in payload
    assert 'source="search_indexed_mail"' in payload and "tool_not_allowed" in payload
    assert row.trace is not None
    calls = row.trace["tool_calls"]
    assert calls[0]["origin"] == "rag_initial"
    assert calls[0]["arguments"] == {"query": "견적서 납기를 찾아 주세요", "max_results": 5}
    assert calls[1]["error_kind"] == "tool_not_allowed" and "origin" not in calls[1]
    assert row.llm_calls == 3  # 합성 assistant는 사용량으로 세지 않는다.
    assert row.trace["pattern_steps"] == [{"kind": "rag_initial", "synthetic": True}]


async def test_rewrite_counts_usage_and_does_not_expose_future_or_grading(ix: Env) -> None:
    summary, factory = await evaluate(
        ix,
        "rewrite_retrieve_once",
        [
            {
                "text": '{"query":"견적서 납기"}',
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_usd": 0.001,
            },
            {"text": "첫 답변", "cost_usd": 0},
            {"text": "invalid JSON", "input_tokens": 20, "output_tokens": 7, "cost_usd": 0.002},
            {"text": "둘째 답변", "cost_usd": 0},
        ],
        turns=["현재 견적서 질문", "FUTURE-USER-MARK"],
    )
    row = only(summary)
    assert row.failure_type is None and row.llm_calls == 4
    assert row.llm_calls_by_kind == {"rag_rewrite": 2}
    assert row.input_tokens == 30 and row.output_tokens == 12
    assert row.cost_usd == 0.003
    requests = factory.made["t-rag"].requests
    first, second = requests[0], requests[2]
    assert "FUTURE-USER-MARK" not in repr(first)
    assert [m.content for m in first.messages[1:]] == ["현재 견적서 질문"]
    assert [m.content for m in second.messages[1:]] == [
        "현재 견적서 질문",
        "첫 답변",
        "FUTURE-USER-MARK",
    ]
    assert all(m.role != "tool" for m in second.messages)
    assert "scenario:" not in repr(first) and "gold" not in repr(first)
    assert row.trace is not None
    initial = [c for c in row.trace["tool_calls"] if c.get("origin") == "rag_initial"]
    assert [c["arguments"]["query"] for c in initial] == ["견적서 납기", "FUTURE-USER-MARK"]
    assert row.trace["pattern_steps"] == [
        {"kind": "rag_rewrite", "fallback": False},
        {"kind": "rag_initial", "synthetic": True},
        {"kind": "rag_rewrite", "fallback": True},
        {"kind": "rag_initial", "synthetic": True},
    ]


@pytest.mark.parametrize("pattern", ["retrieve_once", "rewrite_retrieve_once"])
async def test_recorded_initial_result_and_rewrite_are_reused_on_node_reentry(
    make_harness: Make,
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
) -> None:
    script = ([{"text": '{"query":"SYNTH-QUERY"}'}] if pattern.startswith("rewrite") else []) + [
        {"tool_calls": [call("conversation_context")]},
        {"text": "끝"},
    ]
    h = await make_harness(script)
    h.deps.rag = RagPolicy(pattern)
    searches = 0

    async def search(_self: RunNodes, *_args: Any) -> ToolOutcome:
        nonlocal searches
        searches += 1
        return ToolOutcome('{"results": []}')

    monkeypatch.setattr(RunNodes, "_search_indexed", search)
    run_id = await h.start("질문")
    state: RunState = {"workspace_id": str(h.ws), "run_id": str(run_id), "step": 0}
    node = RunNodes(h.deps)
    assert (await node.agent(state))["step"] == 1
    assert (await node.agent(state))["step"] == 1
    assert searches == 1 and len(h.llm.requests) == (2 if pattern.startswith("rewrite") else 1)
    transcript = await node._transcript(h.ws, run_id)
    assert transcript.assistant_for_step(-1)["origin"] == "rag_initial"  # type: ignore[index]
    assert transcript.tool_results(-1)[initial_call_id(str(run_id))]["is_error"] is False
    assert "SYNTH-QUERY" not in json.dumps(h.deps.rag.steps)


async def test_uncertain_initial_search_stops_reentry_without_rewrite_or_search_retry(
    make_harness: Make,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_harness([{"text": '{"query":"SYNTH-QUERY"}'}])
    h.deps.rag = RagPolicy("rewrite_retrieve_once")
    searches = 0

    async def search(_self: RunNodes, *_args: Any) -> ToolOutcome:
        nonlocal searches
        searches += 1
        raise RuntimeError("synthetic interrupted retrieval")

    monkeypatch.setattr(RunNodes, "_search_indexed", search)
    run_id = await h.start("질문")
    state: RunState = {"workspace_id": str(h.ws), "run_id": str(run_id), "step": 0}
    node = RunNodes(h.deps)
    with pytest.raises(RuntimeError):
        await node.agent(state)
    assert await node.agent(state) == {"next_node": "done"}
    assert searches == 1 and len(h.llm.requests) == 1
    assert (await h.run_row(run_id))["error_kind"] == "rag_search_interrupted"


@pytest.mark.parametrize("stage", ["rewrite", "search"])
async def test_episode_deadline_covers_initial_awaits(
    ix: Env,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    entered = False
    if stage == "search":

        async def search(_self: RunNodes, *_args: Any) -> ToolOutcome:
            nonlocal entered
            entered = True
            await asyncio.sleep(100)
            raise AssertionError("not cancelled")

        monkeypatch.setattr(RunNodes, "_search_indexed", search)
    else:
        original = ScriptedLLM.complete

        async def complete(self: ScriptedLLM, request: Any) -> Any:
            nonlocal entered
            if request.metadata.get("step_kind") == "rag_rewrite":
                entered = True
                await asyncio.sleep(100)
            return await original(self, request)

        monkeypatch.setattr(ScriptedLLM, "complete", complete)
    summary, _ = await evaluate(
        ix,
        "rewrite_retrieve_once",
        [{"text": '{"query":"견적"}'}, {"text": "끝"}],
        budget=Budget(max_seconds=0.3),
    )
    row = only(summary)
    assert entered and row.failure_type == "budget_exceeded"
    assert row.cost_usd is None  # 취소된 LLM 비용/일반 미상 비용을 0으로 꾸미지 않는다.


async def test_rewrite_consumes_existing_episode_step_budget(ix: Env) -> None:
    summary, factory = await evaluate(
        ix,
        "rewrite_retrieve_once",
        [{"text": '{"query":"견적"}'}, {"text": "호출되면 안 됨"}],
        budget=Budget(max_steps=1),
    )
    assert only(summary).failure_type == "budget_exceeded"
    assert len(factory.made["t-rag"].requests) == 1


async def test_rewrite_transport_error_fails_without_fallback_search(
    make_harness: Make,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_harness([])
    h.deps.llm = ScriptedLLM([LLMRequestError("synthetic transport failure")])
    h.deps.rag = RagPolicy("rewrite_retrieve_once")

    async def forbidden(*_args: Any) -> Any:
        pytest.fail("LLM transport failure reached retrieval")

    monkeypatch.setattr(RunNodes, "_search_indexed", forbidden)
    run_id = await h.start("질문")
    assert await RunNodes(h.deps).agent(
        {"workspace_id": str(h.ws), "run_id": str(run_id), "step": 0}
    ) == {"next_node": "done"}
    assert (await h.run_row(run_id))["error_kind"] == "LLMRequestError"


async def test_cancelled_rewrite_is_not_repeated_on_node_reentry(
    make_harness: Make,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_harness([])
    h.deps.rag = RagPolicy("rewrite_retrieve_once")
    entered = asyncio.Event()

    async def wait(self: ScriptedLLM, request: Any) -> Any:
        entered.set()
        await asyncio.sleep(100)

    monkeypatch.setattr(ScriptedLLM, "complete", wait)
    run_id = await h.start("질문")
    state: RunState = {"workspace_id": str(h.ws), "run_id": str(run_id), "step": 0}
    node = RunNodes(h.deps)
    pending = asyncio.create_task(node.agent(state))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    entered.clear()
    assert await node.agent(state) == {"next_node": "done"}
    assert not entered.is_set()
    assert (await h.run_row(run_id))["error_kind"] == "rag_rewrite_interrupted"
    assert h.deps.rag.steps == [{"kind": "rag_rewrite", "error_kind": "CancelledError"}]
