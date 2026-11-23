"""A3(Adaptive) 단위 테스트(ADR-0036). 합성 값만 쓰고 LLM 호출은 없다."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from orbit.evalrunner.rag_arch import a3, diagnostics
from orbit.evalrunner.rag_arch.config import (
    A1_COMPRESS_VERSION,
    A1_REWRITE_VERSION,
    A3_ROUTE_PROMPT,
    A3_ROUTE_VERSION,
    ARCHS,
)
from orbit.evalrunner.rag_arch.sut import RagArchSUT
from orbit.llm.types import LLMResponse, LLMUsage


class _Index:
    budget_error = None

    def mail_index_for(self, **kw: Any) -> Any:
        return object()

    def config_info(self) -> dict[str, Any]:
        return {"fingerprints": {"acct": "fp"}}


def _sut() -> RagArchSUT:
    return RagArchSUT(rag_arch="A3", index=cast(Any, _Index()))


def test_a3_is_registered_and_version_has_route_and_subflow_prompts() -> None:
    assert "A3" in ARCHS
    version = _sut().version
    assert A3_ROUTE_VERSION in version and A1_REWRITE_VERSION in version
    assert A1_COMPRESS_VERSION in version and "A3" in version


def test_a3_fingerprint_has_route_a1_and_a2_conditions() -> None:
    info = _sut().config_info(model_id="m")
    assert info["rag_arch"] == "A3" and info["route_version"] == A3_ROUTE_VERSION
    assert info["route_prompt_sha"] and info["reranker"]["revision"]
    assert info["candidates"] and info["rerank_top_k"] and info["compress_prompt_sha"]
    assert info["a2_allowed_tools"] and info["a2_default_results"]
    assert info["inner"]["runflow_fingerprint"] and info["condition_digest"]
    assert info["condition_digest"] != _sut().config_info(model_id="x")["condition_digest"]


def test_route_prompt_has_no_task_labels_or_gold_words() -> None:
    for word in (*diagnostics.RAG_TYPES, "rag:", "gold", "task_id", "tag", "정답", "단일 조회"):
        assert word not in A3_ROUTE_PROMPT
    assert "다중 홉" not in A3_ROUTE_PROMPT and "전역 집계" not in A3_ROUTE_PROMPT


def test_expected_route_matches_adr_table() -> None:
    adr = Path(__file__).parents[4] / "docs/adr/ADR-0036-rag-architecture-comparison.md"
    text = adr.read_text(encoding="utf-8").split("### A3 라우팅 기대표")[1]
    names = {"단일 조회": "rag:single", "다중 홉": "rag:multihop",
             "전역 집계": "rag:aggregate", "답할 수 없음": "rag:unanswerable"}  # fmt: skip
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] in names:
            rows[names[cells[0]]] = cells[1]
    assert rows == a3.EXPECTED_ROUTE


@pytest.mark.parametrize(
    ("text", "route"),
    [('{"route":"none"}', "none"), (' {"route":"A1"} ', "A1"), ('{"route":"A2"}', "A2"),
     ('{"route":"A3"}', None), ('{"mode":"A1"}', None), ("A1", None), (None, None), ("[]", None)],
)  # fmt: skip
def test_parse_route_accepts_only_three_values(text: str | None, route: str | None) -> None:
    assert a3.parse_route(text) == route


class _LLM:
    def __init__(self, text: str, finish: str = "stop") -> None:
        self.text, self.finish, self.requests = text, finish, []  # type: ignore[var-annotated]

    async def complete(self, request: Any) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            text=self.text, tool_calls=(), finish_reason=cast(Any, self.finish),
            usage=LLMUsage(input_tokens=7, output_tokens=3), cost_usd=0.001, latency_ms=12,
            model_id=None, provider=None,
        )  # fmt: skip


def test_route_question_sends_only_prompt_and_question_and_records_usage() -> None:
    llm = _LLM('{"route":"A1"}')
    route, diag = asyncio.run(a3.route_question(cast(Any, llm), "질문 문장", "t-9"))
    req = llm.requests[0]
    assert [(m.role, m.content) for m in req.messages] == [
        ("system", A3_ROUTE_PROMPT), ("user", "질문 문장")
    ]  # fmt: skip
    assert route == "A1" and diag == {
        "a3_route": "A1", "route_fallback": False, "route_input_tokens": 7,
        "route_output_tokens": 3, "route_cost_usd": 0.001, "route_latency_ms": 12,
    }  # fmt: skip


@pytest.mark.parametrize("text, finish", [("garbage", "stop"), ('{"route":"A1"}', "length")])
def test_route_question_format_failure_falls_back_to_a2(text: str, finish: str) -> None:
    route, diag = asyncio.run(a3.route_question(cast(Any, _LLM(text, finish)), "q", "t"))
    assert route == "A2" and diag["a3_route"] == "A2" and diag["route_fallback"] is True


def _rec(task: str, route: str, fallback: bool = False) -> dict[str, Any]:
    return {"task_id": task, "rag_diag": {"a3_route": route, "route_fallback": fallback,
                                          "route_cost_usd": 0.002}}  # fmt: skip


def test_a3_stats_distribution_accuracy_none_rate_and_fallback() -> None:
    kinds = {"s1": "rag:single", "s2": "rag:single", "m1": "rag:multihop",
             "g1": "rag:aggregate", "u1": "rag:unanswerable"}  # fmt: skip
    records = [
        _rec("s1", "A1"), _rec("s2", "none"), _rec("m1", "A2"), _rec("g1", "A1"),
        _rec("u1", "A2", fallback=True), {"task_id": "s1", "rag_diag": None},
        {"task_id": "m1", "rag_diag": {"search_calls": 1}},
    ]  # fmt: skip
    out = diagnostics.a3_stats(records, kinds)
    assert out is not None
    assert out["n_episodes"] == 7 and out["n_without_diag"] == 2 and out["n_fallback"] == 1
    assert out["routes"] == {"A1": 2, "A2": 2, "none": 1}  # fallback은 A2 칸에 든다
    assert out["none_rate"] == pytest.approx(1 / 7)  # 분모는 전체 에피소드
    acc = out["routing_accuracy"]  # 주 수치는 fallback·none·진단 없음을 오답으로 센다
    assert (acc["accuracy"], acc["n"]) == (pytest.approx(2 / 7), 7)
    assert (acc["accuracy_excl_fallback"], acc["n_excl_fallback"]) == (0.5, 4)
    assert (acc["n_fallback"], acc["n_without_diag"]) == (1, 2)
    single = out["by_type"]["rag:single"]
    assert single["routes"] == {"A1": 1, "none": 1}
    assert single["routing_accuracy"]["accuracy"] == pytest.approx(1 / 3)
    assert single["routing_accuracy"]["n"] == 3
    assert out["by_type"]["rag:aggregate"]["routing_accuracy"]["accuracy"] == 0.0
    unans = out["by_type"]["rag:unanswerable"]["routing_accuracy"]
    assert unans["accuracy"] == 0.0 and unans["n_excl_fallback"] == 0 and unans["n_fallback"] == 1
    assert unans["accuracy_excl_fallback"] is None
    assert out["route_cost_usd_mean"] == 0.002
    json.dumps(out)


def test_a3_accuracy_main_counts_fallback_as_wrong_and_excl_does_not() -> None:
    kinds = {f"s{i}": "rag:single" for i in range(10)} | {
        f"m{i}": "rag:multihop" for i in range(10)
    }
    records = [_rec(f"s{i}", "A2", fallback=True) for i in range(10)]
    records += [_rec(f"m{i}", "A2") for i in range(10)]
    acc = diagnostics.a3_stats(records, kinds)["routing_accuracy"]  # type: ignore[index]
    assert (acc["accuracy"], acc["accuracy_excl_fallback"]) == (0.5, 1.0)
    assert (acc["n"], acc["n_excl_fallback"], acc["n_fallback"]) == (20, 10, 10)


def test_a3_context_recall_by_route_and_none_counts_as_zero_overall() -> None:
    gold = {"a": frozenset({"m1"}), "b": frozenset({"m1", "m2"}), "c": frozenset({"m3"})}
    kinds = {"a": "rag:single", "b": "rag:multihop", "c": "rag:single"}
    recs = [
        {"task_id": "a", "rag_diag": {"a3_route": "A1", "route_fallback": False,
                                      "context_message_ids": ["m1"]}},
        {"task_id": "b", "rag_diag": {"a3_route": "A2", "route_fallback": False,
                                      "context_message_ids": ["m1"]}},
        {"task_id": "c", "rag_diag": {"a3_route": "none", "route_fallback": False,
                                      "context_message_ids": []}},
    ]  # fmt: skip
    by_route = diagnostics.a3_stats(recs, kinds, gold)["context_recall_by_route"]  # type: ignore[index]
    assert by_route == {"A1": {"context_recall": 1.0, "n": 1},
                        "A2": {"context_recall": 0.5, "n": 1},
                        "none": {"context_recall": 0.0, "n": 1}}  # fmt: skip
    overall = diagnostics.context_recall_and_citation(recs, gold)  # none도 0으로 분모에 든다
    assert (
        overall["context_recall"] == pytest.approx(0.5)
        and overall["n_context_recall_episodes"] == 3
    )


def test_ranked_recall_counts_only_a1_route_episodes_of_a3() -> None:
    gold = {"a": frozenset({"m1"}), "b": frozenset({"m1"}), "c": frozenset({"m1"})}
    diag = {"a3_route": "A1", "route_fallback": False}
    recs = [
        {"task_id": "a", "rag_diag": {**diag, "ranked_message_ids": ["m1"]}},
        {"task_id": "b", "rag_diag": {"a3_route": "A2", "route_fallback": False}},
        {"task_id": "c", "rag_diag": {"a3_route": "none", "route_fallback": False}},
    ]
    out = diagnostics.ranked_recall(recs, gold)
    assert out["n_ranked_episodes"] == 1 and out["recall@1"] == 1.0


def test_a3_stats_is_none_without_route_diag() -> None:
    assert diagnostics.a3_stats([{"task_id": "s1", "rag_diag": {"search_calls": 1}}], {}) is None


# ---------------------------------------------------------------- _run_a3 분기(DB 없이)


def _episode(llm: Any, turns: int = 1) -> Any:
    from types import SimpleNamespace

    task = SimpleNamespace(
        task_id="t-1",
        turns=[SimpleNamespace(role="user", content=f"질문{i}") for i in range(turns)],
    )
    return SimpleNamespace(task=task, llm=llm, steps=SimpleNamespace(consume=lambda: None))


def _stubbed(monkeypatch: pytest.MonkeyPatch) -> tuple[RagArchSUT, list[str]]:
    from orbit.evalrunner.episode import EpisodeOutput

    sut = _sut()
    calls: list[str] = []

    async def direct(episode: Any, path: str | None = None) -> EpisodeOutput:
        calls.append(f"direct:{path}")
        return EpisodeOutput(final_text="d", rag_diag={"arch": "A3", "search_calls": 0})

    async def inner(episode: Any) -> EpisodeOutput:
        calls.append("inner")
        return EpisodeOutput(final_text="i", rag_diag={"arch": "A2", "search_calls": 1})

    monkeypatch.setattr(sut, "_run_direct", direct)
    monkeypatch.setattr(sut._inner, "run", inner)
    return sut, calls


@pytest.mark.parametrize(
    ("text", "calls", "route", "fallback"),
    [('{"route":"none"}', "direct:none", "none", False),
     ('{"route":"A1"}', "direct:A1", "A1", False),
     ('{"route":"A2"}', "inner", "A2", False),
     ("garbage", "inner", "A2", True)],
)  # fmt: skip
def test_run_a3_dispatches_each_route_and_merges_route_diag(
    monkeypatch: pytest.MonkeyPatch, text: str, calls: str, route: str, fallback: bool
) -> None:
    sut, seen = _stubbed(monkeypatch)
    llm = _LLM(text)
    out = asyncio.run(sut.run(_episode(llm)))
    assert seen == [calls] and len(llm.requests) == 1  # 라우터는 한 번, 재시도 없음
    assert out.rag_diag is not None
    assert out.rag_diag["arch"] == "A3" and out.rag_diag["a3_route"] == route
    assert out.rag_diag["route_fallback"] is fallback and out.rag_diag["route_input_tokens"] == 7


def test_run_a3_rejects_multi_turn_before_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    sut, seen = _stubbed(monkeypatch)
    llm = _LLM('{"route":"A1"}')
    with pytest.raises(RuntimeError):
        asyncio.run(sut.run(_episode(llm, turns=2)))
    assert not llm.requests and not seen


def test_a3_stats_counts_missing_diag_as_wrong_and_groups_it_as_no_route() -> None:
    gold = {"a": frozenset({"m1"}), "b": frozenset({"m2"})}
    kinds = {"a": "rag:single", "b": "rag:single"}
    recs: list[dict[str, Any]] = [
        {"task_id": "a", "rag_diag": {"a3_route": "A1", "route_fallback": False,
                                      "context_message_ids": ["m1"]}},
        {"task_id": "b", "rag_diag": None},
    ]  # fmt: skip
    out = diagnostics.a3_stats(recs, kinds, gold)
    assert out is not None and out["n_without_diag"] == 1
    assert out["routing_accuracy"]["accuracy"] == 0.5 and out["routing_accuracy"]["n"] == 2
    assert out["context_recall_by_route"]["no_route"] == {"context_recall": 0.0, "n": 1}


def test_none_route_never_searches_or_reads_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    sut = _sut()

    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("none 경로는 검색·맥락 읽기를 호출하지 않는다")

    async def aboom(*a: Any, **kw: Any) -> Any:
        boom()

    monkeypatch.setattr(sut, "_search", aboom)
    monkeypatch.setattr(sut, "_context", aboom)
    answer = LLMResponse(
        text="답", tool_calls=(), finish_reason="stop", usage=LLMUsage(), cost_usd=None,
        latency_ms=1, model_id=None, provider=None,
    )  # fmt: skip
    requests: list[Any] = []

    class LLM:
        async def complete(self, request: Any) -> LLMResponse:
            requests.append(request)
            return answer

    from datetime import UTC, datetime

    trace = SimpleNamespace(
        record_tool=boom, record_handles=lambda h: None, record_turn_structured=lambda i, s: None
    )
    episode = SimpleNamespace(
        task=SimpleNamespace(
            task_id="t-1", account_id="acct", as_of_time=datetime(2026, 11, 5, tzinfo=UTC),
            time_zone="UTC", turns=[SimpleNamespace(role="user", content="질문")],
        ),
        llm=LLM(), steps=SimpleNamespace(consume=lambda: None), trace=trace,
        database=SimpleNamespace(db=None, ids=None, workspace_id="ws"),
        connectors=SimpleNamespace(mail=None, calendar=None),
    )  # fmt: skip
    out = asyncio.run(sut._run_direct(cast(Any, episode), "none"))
    assert len(requests) == 1 and out.final_text == "답"
    assert out.rag_diag is not None and out.rag_diag["search_calls"] == 0
    assert out.rag_diag["context_message_ids"] == []


def test_diagnose_adds_a3_block_only_to_a3_entry() -> None:
    def rec(arch: str, task: str, diag: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task, "repeat": 0, "cluster_id": task,
            "config": {"dataset": "d", "split": "dev", "repeats": 1, "n_tasks": 2,
                       "sut_config": {"rag_arch": arch}},
            "config_fingerprint": arch, "outcome": {"success": True},
            "answer_ids": {"kind": "message_set", "messages": []},
            "usage": {"llm_calls": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0},
            "latency_ms": 1,
            "rag_diag": {"context_message_ids": [], "context_chars": 0, "truncated": False,
                         "search_calls": 0, "denied_tools": [], **diag},
        }  # fmt: skip

    gold = {"t1": frozenset({"m1"}), "t2": frozenset({"m2"})}
    kinds = {"t1": "rag:single", "t2": "rag:multihop"}
    route = {"route_fallback": False, "context_message_ids": ["m1"]}
    by_arch = {
        "A2": [rec("A2", "t1", {}), rec("A2", "t2", {})],
        "A3": [rec("A3", "t1", {"a3_route": "A1", **route}),
               rec("A3", "t2", {"a3_route": "none", "route_fallback": False})],
    }  # fmt: skip
    out = diagnostics.diagnose(by_arch, gold, kinds, resamples=50)
    assert "a3" not in out["archs"]["A2"]
    a3_entry = out["archs"]["A3"]["a3"]
    assert a3_entry["context_recall_by_route"]["A1"]["context_recall"] == 1.0
    assert a3_entry["context_recall_by_route"]["none"]["context_recall"] == 0.0
