"""RAG 아키텍처 비교(ADR-0036) 단위 테스트. DB·LLM 호출 없이 합성 값만 쓴다."""

import hashlib
import json
from typing import Any, cast

import pytest

from orbit.assistant.workset import WorkSet
from orbit.evalrunner import stats
from orbit.evalrunner.rag_arch import diagnostics
from orbit.evalrunner.rag_arch.config import (
    A0_PROMPT_VERSION,
    RETRIEVAL_K,
    TOP_K,
    build_a0_system,
)
from orbit.evalrunner.rag_arch.sut import RagArchSUT
from orbit.runflow.tool_limits import ToolLimits

ALLOWED = frozenset({"search_indexed_mail", "read_mail", "submit_answer"})


def _search(workset: WorkSet, excerpts: dict[str, str]) -> str:
    results = [
        {"handle": workset.message_handle(mid), "excerpt": text, "excerpt_truncated": False}
        for mid, text in excerpts.items()
    ]
    return json.dumps({"results": results}, ensure_ascii=False)


# ---------------------------------------------------------------- ToolLimits


def test_limits_share_one_cap_across_results_and_record_reach() -> None:
    ws = WorkSet()
    limits = ToolLimits(ALLOWED, char_cap=10)
    out = json.loads(
        limits.filter_result(
            "search_indexed_mail",
            _search(ws, {"m-a": "12345678", "m-b": "abcdef", "m-c": "zz"}),
            ws,
        )
    )
    texts = [r["excerpt"] for r in out["results"]]
    assert texts == ["12345678", "ab"]  # 합계가 상한 10자. 1자도 못 들어간 메일은 뺀다
    assert [r["excerpt_truncated"] for r in out["results"]] == [False, True]
    assert limits.truncated and limits.used_chars == 10
    assert limits.reached == {"m-a", "m-b"}  # 한 글자도 못 받은 m-c는 맥락에 들어가지 못했다
    body = json.dumps({"handle": "M1", "body": "0123456789"})  # 상한이 찬 뒤의 read_mail
    ws.resolve_message("M1")
    cut = json.loads(limits.filter_result("read_mail", body, ws))
    assert cut == {"error": "context_cap_reached"}
    assert limits.reached == {"m-a", "m-b"}
    assert limits.search_calls == 1 and limits.read_calls == 1


def test_limits_ignore_errors_and_other_tools_and_diag_has_no_text() -> None:
    ws = WorkSet()
    limits = ToolLimits(ALLOWED, char_cap=100)
    err = json.dumps({"error": "unknown_handle"})
    assert limits.filter_result("read_mail", err, ws) == err
    assert limits.filter_result("submit_answer", "x", ws) == "x"
    limits.deny("send_mail")
    diag = limits.diag("A2")
    assert diag["denied_tools"] == ["send_mail"] and diag["context_message_ids"] == []
    assert set(diag) == {
        "arch", "context_message_ids", "context_chars", "truncated", "char_cap",
        "search_calls", "read_calls", "denied_tools", "index_pending",
    }  # fmt: skip


# ---------------------------------------------------------------- 설정 지문


class _Index:
    def config_info(self) -> dict[str, Any]:
        return {"fingerprints": {"acct": "fp"}}


def _sut(arch: str, cap: int) -> RagArchSUT:
    return RagArchSUT(rag_arch=arch, index=cast(Any, _Index()), context_char_cap=cap)


def test_fingerprint_contains_arch_prompt_version_k_cap_and_model() -> None:
    a0 = _sut("A0", 1000).config_info(model_id="openai/gpt-6-luna")
    assert a0["rag_arch"] == "A0" and a0["prompt_version"] == A0_PROMPT_VERSION
    assert a0["retrieval_k"] == RETRIEVAL_K == 10
    assert a0["top_k"] == TOP_K == 5 and a0["context_char_cap"] == 1000
    assert a0["model_id"] == "openai/gpt-6-luna" and a0["prompt_sha"]
    assert (
        _sut("A0", 1001).config_info(model_id="openai/gpt-6-luna")["condition_digest"]
        != (a0["condition_digest"])
    )
    assert (
        _sut("A0", 1000).config_info(model_id="other")["condition_digest"]
        != (a0["condition_digest"])
    )
    a2 = _sut("A2", 1000).config_info(model_id="openai/gpt-6-luna")
    assert a2["allowed_tools"] == sorted(ALLOWED) and a2["context_char_cap"] == 1000
    assert a2["inner"]["runflow_fingerprint"] and a2["condition_digest"] != a0["condition_digest"]
    assert "cap1000" in _sut("A0", 1000).version and "A0" in _sut("A0", 1000).version


def test_sut_rejects_unknown_arch_missing_index_and_bad_cap() -> None:
    with pytest.raises(ValueError):
        _sut("A9", 10)
    with pytest.raises(ValueError):
        RagArchSUT(rag_arch="A0", index=None)
    with pytest.raises(ValueError):
        _sut("A0", 0)


def test_a0_prompt_has_data_rules_and_no_provider_ids() -> None:
    from datetime import UTC, datetime

    text = build_a0_system(datetime(2026, 11, 11, tzinfo=UTC), UTC, "abcdef012345")
    assert "abcdef012345" in text and "submit_answer" in text and "msg-" not in text


def _read(workset: WorkSet, mid: str, body: str) -> str:
    return json.dumps({"handle": workset.message_handle(mid), "subject": "s", "body": body})


def test_reach_definition_is_same_for_search_and_read_and_drops_empty_blocks() -> None:
    texts = {"m1": "a" * 6, "m2": "b" * 6, "m3": "", "m4": "c" * 6}
    ws1, ws2 = WorkSet(), WorkSet()
    a2 = ToolLimits(ALLOWED, 8)
    a0 = ToolLimits(frozenset(), 8)
    found = json.loads(a2.filter_result("search_indexed_mail", _search(ws1, texts), ws1))
    read = [a0.filter_result("read_mail", _read(ws2, m, t), ws2) for m, t in texts.items()]
    assert a2.reached == a0.reached == {"m1", "m2", "m3"}  # m4는 1자도 못 들어갔다(m3은 빈 본문)
    assert len(found["results"]) == 3  # A2: 못 들어간 메일의 블록(머리글 포함)을 뺀다
    assert json.loads(read[3]) == {"error": "context_cap_reached"}  # A0도 같은 규칙


def test_index_pending_flag_is_recorded_and_counted() -> None:
    ws = WorkSet()
    limits = ToolLimits(ALLOWED, 100)
    body = json.loads(_search(ws, {"m1": "abc"}))
    limits.filter_result("search_indexed_mail", json.dumps({**body, "index_pending": True}), ws)
    assert limits.diag("A2")["index_pending"] is True
    rows = _fixture()["A2"]
    rows[0]["rag_diag"]["index_pending"] = True
    assert diagnostics.efficiency(rows)["n_index_pending"] == 1


PRODUCT_PROMPT_SHA = "ca84b7abbd43853b779f29018bd99e20afdd608daef552934236d7e304d33085"


def test_product_prompt_is_byte_identical_and_a2_prompt_has_no_unavailable_features() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orbit.runflow.answer import with_answer_prompt
    from orbit.runflow.prompt import build_system_prompt
    from orbit.runflow.tool_limits import ToolLimits as TL

    tz = ZoneInfo("Asia/Seoul")
    now = datetime(2026, 11, 12, 9, 0, tzinfo=tz)
    # 승인 없는 리마인더(S0) 문구는 평가 재현을 위해 바이트 단위로 고정한다(ADR-0033 G1 반영 전 값)
    product = build_system_prompt(now, tz, "0123456789ab", reminder_approval=False)
    assert hashlib.sha256(product.encode()).hexdigest() == PRODUCT_PROMPT_SHA
    names = ["send_mail", "create_reminder", "list_events", "search_mail", *ALLOWED]
    system = build_system_prompt(now, tz, "0123456789ab", ALLOWED)
    system = with_answer_prompt(TL(ALLOWED, 1).filter_prompt(system, names))
    for word in ("리마인더", "발송", "승인", "time_in_past"):
        assert word not in system


def test_filter_prompt_drops_only_lines_naming_denied_tools() -> None:
    limits = ToolLimits(ALLOWED, 10)
    system = "keep line\n- send_mail을 부른다\n- search_indexed_mail을 쓴다\n- search_mail 말고"
    out = limits.filter_prompt(system, ["send_mail", "search_mail", *ALLOWED])
    assert out == "keep line\n- search_indexed_mail을 쓴다"


# ---------------------------------------------------------------- 진단 집계


def _rec(
    task: str, repeat: int, ok: bool, cluster: str, arch: str, *, ctx: list[str], cited: list[str],
    chars: int = 10, calls: int = 1, cost: float | None = 0.01, latency: int = 100,
    search: int = 1, truncated: bool = False, followup: bool | None = None,
) -> dict[str, Any]:  # fmt: skip
    return {
        "task_id": task,
        "repeat": repeat,
        "cluster_id": cluster,
        "config": {
            "dataset": "d", "split": "dev", "repeats": 1, "n_tasks": 4,
            "sut_config": {"rag_arch": arch},
        },
        "config_fingerprint": arch,
        "outcome": {"success": ok},
        "answer_ids": {"kind": "message_set", "messages": [{"message_id": m} for m in cited]},
        "usage": {"llm_calls": calls, "input_tokens": 100, "output_tokens": 20, "cost_usd": cost},
        "latency_ms": latency,
        "rag_diag": {
            "context_message_ids": ctx, "context_chars": chars, "truncated": truncated,
            "search_calls": search, "denied_tools": [],
            **({} if followup is None else {"answer_followup": followup}),
        },
    }  # fmt: skip


GOLD = {
    "t1": frozenset({"m1"}),
    "t2": frozenset({"m1", "m2"}),
    "t3": frozenset({"m3"}),
    "t4": frozenset(),
}
KINDS = {"t1": "rag:single", "t2": "rag:multihop", "t3": "rag:single", "t4": "rag:unanswerable"}


def _fixture() -> dict[str, list[dict[str, Any]]]:
    a0 = [
        _rec("t1", 1, True, "c1", "A0", ctx=["m1"], cited=["m1"], truncated=True, latency=100),
        _rec("t2", 1, False, "c2", "A0", ctx=["m1"], cited=["m1", "mx"], latency=200),
        _rec("t3", 1, False, "c3", "A0", ctx=[], cited=[], latency=300),
        _rec("t4", 1, True, "c4", "A0", ctx=[], cited=[], latency=400),
    ]
    a2 = [
        _rec("t1", 1, True, "c1", "A2", ctx=["m1"], cited=["m1"], calls=3, cost=None, search=2),
        _rec("t2", 1, True, "c2", "A2", ctx=["m1", "m2"], cited=["m1", "m2"], calls=5, search=3),
        _rec("t3", 1, True, "c3", "A2", ctx=["m3"], cited=["m3"], calls=3, search=1),
        _rec("t4", 1, False, "c4", "A2", ctx=[], cited=["m9"], calls=2, search=1),
    ]
    return {"A0": a0, "A2": a2}


def test_diagnose_values_on_small_fixed_input() -> None:
    report = diagnostics.diagnose(_fixture(), GOLD, KINDS, k=1, resamples=200, seed=0)
    a0, a2 = report["archs"]["A0"], report["archs"]["A2"]
    assert a0["success"]["rate"] == 0.5 and a2["success"]["rate"] == 0.75
    assert a0["pass_k"]["pass_k"] == 0.5 and a0["pass_k"]["k"] == 1
    paired = a0["paired_vs_baseline"]
    assert paired["difference"] == pytest.approx(-0.25) and paired["n_tasks"] == 4
    assert "paired_vs_baseline" not in a2
    assert a0["by_type"]["rag:single"]["success"]["rate"] == 0.5  # t1 성공, t3 실패
    assert a0["by_type"]["rag:single"]["paired_vs_baseline"]["difference"] == pytest.approx(-0.5)
    assert a0["by_type"]["rag:unanswerable"]["success"]["rate"] == 1.0
    # 도달 재현율: t1 1, t2 0.5, t3 0 (t4는 정답 메일이 없어 뺀다)
    assert a0["context_recall"] == pytest.approx(0.5) and a0["n_context_recall_episodes"] == 3
    assert a2["context_recall"] == pytest.approx(1.0)
    # 인용: A0 정밀도 t1 1, t2 0.5(t3·t4는 인용 없음) / 재현율 t1 1, t2 0.5, t3 0
    assert a0["citation_precision"] == pytest.approx(0.75) and a0["n_precision_episodes"] == 2
    assert a0["citation_recall"] == pytest.approx(0.5)
    assert a2["citation_precision"] == pytest.approx((1 + 1 + 1 + 0) / 4)
    eff0, eff2 = a0["efficiency"], a2["efficiency"]
    assert eff0["llm_calls_mean"] == 1 and eff2["llm_calls_mean"] == 3.25
    assert eff0["tokens_mean"] == 120 and eff2["n_cost_unknown"] == 1
    assert eff0["latency_ms_p50"] == 200 and eff0["latency_ms_p95"] == 400
    assert eff2["search_calls_mean"] == 1.75 and eff0["truncated_rate"] == 0.25


def test_failed_episode_without_diag_counts_as_zero_reach() -> None:
    rows = _fixture()["A0"]
    for r in rows:
        if r["task_id"] == "t1":
            del r["rag_diag"]  # t1은 오류로 끝나 진단이 없다
    out = diagnostics.context_recall_and_citation(rows, GOLD)
    assert out["n_episodes_without_diag"] == 1 and out["n_context_recall_episodes"] == 3
    assert out["context_recall"] == pytest.approx((0 + 0.5 + 0) / 3)
    assert out["n_context_recall_episodes"] == out["n_recall_episodes"]


def test_ranked_recall_at_k_partial_match_and_exclusions() -> None:
    def rec(task: str, ranked: list[str] | None, first: list[str] | None = None) -> dict[str, Any]:
        diag: dict[str, Any] = {}
        if ranked is not None:
            diag["ranked_message_ids"] = ranked
        if first is not None:
            diag["first_stage_ranked_message_ids"] = first
        return {"task_id": task, "rag_diag": diag}

    rows = [
        rec("t1", ["m9", "m1"], ["m1", "m9"]),  # 정답 m1: @1 0, @3 1 / 1차 @1 1
        rec("t2", ["m1", "m8", "m7", "m6", "m2"], ["m8", "m1", "m2"]),  # 정답 m1,m2
        rec("t4", ["m1"], ["m1"]),  # 정답 메일 없음: 분모에서 뺀다
        {"task_id": "t3", "rag_diag": {"context_message_ids": []}},  # 순위 키 없음: 뺀다
        {"task_id": "t3"},  # 진단 없음: 뺀다
    ]
    out = diagnostics.ranked_recall(rows, GOLD)
    assert out["n_ranked_episodes"] == 2
    assert out["recall@1"] == pytest.approx((0 + 0.5) / 2)
    assert out["recall@3"] == pytest.approx((1 + 0.5) / 2)
    assert out["recall@5"] == pytest.approx((1 + 1) / 2)
    assert out["recall@10"] == out["recall@5"]
    assert out["n_first_stage_episodes"] == 2
    assert out["first_stage_recall@1"] == pytest.approx((1 + 0) / 2)
    assert out["first_stage_recall@3"] == pytest.approx((1 + 1) / 2)
    assert diagnostics.ranked_recall([{"task_id": "t1", "rag_diag": {}}], GOLD) == {}  # A2 등


def test_ranked_recall_dedups_ids_before_cut() -> None:
    rows = [{"task_id": "t2", "rag_diag": {"ranked_message_ids": ["m1", "m1", "m1", "m2"]}}]
    out = diagnostics.ranked_recall(rows, GOLD)  # 정답 m1,m2: 중복을 빼면 @2에서 둘 다 나온다
    assert out["recall@1"] == 0.5 and out["recall@3"] == 1.0


def test_diagnose_emits_recall_at_k_only_with_ranked_ids() -> None:
    report = diagnostics.diagnose(_fixture(), GOLD, KINDS, k=1, resamples=50, seed=0)
    assert not any(k.startswith(("recall@", "first_stage_")) for k in report["archs"]["A0"])
    fx = _fixture()
    for r in fx["A0"]:
        r["rag_diag"]["ranked_message_ids"] = ["m1", "m2", "m3"]
    a0 = diagnostics.diagnose(fx, GOLD, KINDS, k=1, resamples=50, seed=0)["archs"]["A0"]
    assert a0["recall@10"] == pytest.approx(1.0) and a0["n_ranked_episodes"] == 3
    assert (
        "recall@5"
        not in diagnostics.diagnose(fx, GOLD, KINDS, k=1, resamples=50, seed=0)["archs"]["A2"]
    )


def test_rag_diag_rejects_incomparable_results() -> None:
    fx = _fixture()
    for r in fx["A0"]:
        r["config"].update(model_id="m", split="dev")
        r["config"]["sut_config"]["context_char_cap"] = 100
    for r in fx["A2"]:
        r["config"].update(model_id="m", split="dev")
        r["config"]["sut_config"]["context_char_cap"] = 100
    diagnostics.check_comparable(fx)  # 같으면 통과
    for key, value in (("model_id", "x"), ("split", "train"), ("dataset", "other")):
        bad = {a: [{**r, "config": dict(r["config"])} for r in rows] for a, rows in fx.items()}
        for r in bad["A2"]:
            r["config"][key] = value
        with pytest.raises(stats.ResultsError, match=key):
            diagnostics.check_comparable(bad)
    capped = {a: [{**r, "config": dict(r["config"])} for r in rows] for a, rows in fx.items()}
    for r in capped["A2"]:
        r["config"]["sut_config"] = {**r["config"]["sut_config"], "context_char_cap": 5}
    with pytest.raises(stats.ResultsError, match="context_char_cap"):
        diagnostics.check_comparable(capped)
    indexed = {a: [{**r, "config": dict(r["config"])} for r in rows] for a, rows in fx.items()}
    for r in indexed["A0"]:
        r["config"]["sut_config"] = {**r["config"]["sut_config"], "index": {"embedder": "x"}}
    with pytest.raises(stats.ResultsError, match="index"):
        diagnostics.check_comparable(indexed)
    with pytest.raises(stats.ResultsError, match="tasks"):
        diagnostics.check_comparable({"A0": fx["A0"], "A2": fx["A2"][:3]})


def test_diagnose_without_baseline_and_arch_from_results() -> None:
    only = {"A0": _fixture()["A0"]}
    report = diagnostics.diagnose(only, GOLD, KINDS, k=1, resamples=50)
    assert "paired_vs_baseline" not in report["archs"]["A0"]
    assert diagnostics._arch(only["A0"]) == "A0"
    with pytest.raises(stats.ResultsError):
        diagnostics._arch([{"config": {"sut_config": {}}}])


# ---------------------------------------------------------------- CLI


def test_cli_requires_rag_arch_only_for_rag_arch_sut(tmp_path: Any) -> None:
    from orbit.evalrunner.cli import main

    base = ["run", "--dataset", "unused", "--split", "dev", "--out", str(tmp_path / "o.jsonl")]
    assert main([*base, "--sut", "rag_arch", "--index", "on"]) == 2  # --rag-arch 없음
    assert main([*base, "--sut", "runflow_v0", "--rag-arch", "A0"]) == 2
    assert main([*base, "--sut", "rag_arch", "--rag-arch", "A0"]) == 2  # --index off
    off_model = [*base, "--sut", "rag_arch", "--rag-arch", "A0", "--index", "on"]
    assert main([*off_model, "--model-id", "other-model"]) == 2  # Luna가 아니다


# ---------------------------------------------------------------- A1


def test_rerank_order_breaks_ties_by_search_rank() -> None:
    from orbit.evalrunner.rag_arch.a1 import rerank_order

    assert rerank_order([0.1, 0.9, 0.5, 0.9, 0.5], 4) == [1, 3, 2, 4]
    assert rerank_order([1.0, 1.0], 5) == [0, 1]


def test_compress_body_whole_sentence_match_and_original_order() -> None:
    from orbit.evalrunner.rag_arch.a1 import compress_body, parse_compression

    body = "첫 문장.  둘째   문장. 셋째 문장?\n넷째"
    assert compress_body(body, ["셋째 문장?", "첫 문장."]) == "첫 문장. 셋째 문장?"
    assert compress_body(body, ["둘째 문장."]) == "둘째 문장."  # 공백 정규화
    assert compress_body(body, ["넷째"]) == "넷째"  # 줄바꿈도 경계
    assert compress_body(body, []) == ""
    for bad in (["없는 문장"], ["", "첫 문장."], [1], "첫 문장.", None, ["첫 문장. 둘째 문장."]):
        assert compress_body(body, bad) is None
    assert parse_compression("[]") is None and parse_compression(None) is None


def test_compress_body_rejects_partial_sentence_and_keeps_duplicate_once() -> None:
    from orbit.evalrunner.rag_arch.a1 import compress_body

    # 문장 일부를 고르면 부정어가 잘린다
    assert compress_body("마감은 금요일이 아니다.", ["마감은 금요일"]) is None
    assert compress_body("마감은 금요일이 아니다.", ["가"]) is None
    assert compress_body("마감은 금요일이 아니다.", ["."]) is None
    assert compress_body("마감은 금요일이 아니다.", ["마감은 금요일이 아니다."]) == (
        "마감은 금요일이 아니다."
    )
    assert compress_body("가. 나. 가.", ["가.", "가.", "나."]) == "가. 나."  # 중복은 한 번만
    assert compress_body("총 3.5배다. 다음.", ["총 3.5배다."]) == "총 3.5배다."


def test_a1_fingerprint_has_reranker_prompts_and_limits() -> None:
    from orbit.evalrunner.rag_arch.config import CANDIDATES, RERANK_TOP_K, RERANKER_REVISION

    class Fake:
        model_id = "fake/r"
        revision = "abc"
        max_length = 768
        device = "cpu"

        def score(self, query: str, passages: Any) -> list[float]:
            return []

    def make(reranker: Any) -> RagArchSUT:
        return RagArchSUT(rag_arch="A1", index=cast(Any, _Index()), reranker=reranker)

    info = make(Fake()).config_info(model_id="m")
    assert info["rag_arch"] == "A1" and info["candidates"] == CANDIDATES == 20
    assert info["rerank_top_k"] == RERANK_TOP_K == 5 and info["context_char_cap"] == 6000
    assert info["reranker"] == {
        "model_id": "fake/r",
        "revision": "abc",
        "max_length": 768,
        "device": "cpu",
    }
    assert info["rewrite_prompt_sha"] and info["compress_prompt_sha"] and info["prompt_sha"]
    assert info["rewrite_version"] == "rag-a1-rewrite-v1"
    assert info["compress_version"] == "rag-a1-compress-v1"
    other = make(None).config_info(model_id="m")  # 실제 reranker(지연 로드, 모델을 읽지 않는다)
    assert other["reranker"]["revision"] == RERANKER_REVISION
    assert other["condition_digest"] != info["condition_digest"]
    assert "A1" in make(None).version


def test_reranker_needs_group_only_on_score_and_import_is_lazy(monkeypatch: Any) -> None:
    import sys
    import types

    from orbit.evalrunner.rag_arch.reranker import LocalCrossEncoderReranker

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", None
    )  # 설치 여부와 무관하게 없는 것으로
    rer = LocalCrossEncoderReranker()  # 만들 때는 모델도 라이브러리도 읽지 않는다
    assert rer._model is None
    with pytest.raises(RuntimeError, match="rag-eval"):
        rer.score("q", ["p"])

    seen: dict[str, Any] = {}

    class FakeModel:
        model = types.SimpleNamespace(eval=lambda: None)

        def __init__(self, name: str, **kw: Any) -> None:
            seen.update(name=name, **kw)

        def predict(self, pairs: Any, **kw: Any) -> list[float]:
            return [1.0 for _ in pairs]

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(CrossEncoder=FakeModel)
    )
    assert LocalCrossEncoderReranker().score("q", ["a", "b"]) == [1.0, 1.0]
    assert seen["device"] == "cpu" and seen["max_length"] == 768  # 장치와 길이를 고정한다


def test_real_reranker_orders_relevant_passage_first() -> None:
    from pathlib import Path

    from orbit.evalrunner.rag_arch.config import RERANKER_REVISION
    from orbit.evalrunner.rag_arch.reranker import LocalCrossEncoderReranker

    pytest.importorskip("sentence_transformers")
    cache = Path.home() / ".cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3/snapshots"
    if not (cache / RERANKER_REVISION).exists():
        pytest.skip("고정 리비전의 reranker 모델이 캐시에 없다")
    scores = LocalCrossEncoderReranker().score(
        "견적서 납기가 언제인가", ["점심 메뉴를 정했다", "견적서 납기는 다음 주 금요일이다"]
    )
    assert scores[1] > scores[0]


def test_a1_diagnostics_aggregate_and_old_records_still_work() -> None:
    rows = _fixture()["A0"]
    assert diagnostics.a1_stats(rows) is None
    a1 = [
        {
            "rag_diag": {
                "rewrite_fallback": fb, "compress_fallback": cf,
                "compressed_chars_before": 100, "compressed_chars_after": after,
                "compressed_empty": empty,
            }
        }
        for fb, cf, after, empty in ((True, 2, 50, 3), (False, 0, 30, 0), (False, 1, 20, 0))
    ]  # fmt: skip
    out = diagnostics.a1_stats(a1)
    assert out == {
        "n_episodes": 3, "rewrite_fallback_rate": pytest.approx(1 / 3),
        "compress_fallback_total": 3, "compressed_empty_total": 3,
        "compressed_empty_mean": pytest.approx(1.0), "compression_ratio": pytest.approx(100 / 300),
    }  # fmt: skip
    assert diagnostics.a1_stats([{"rag_diag": {"compressed_chars_before": 0}}]) is None


def test_reranker_max_length_covers_rag_v1_corpus() -> None:
    """토크나이저가 HF 캐시에 있을 때만 실행한다. 질문 64 + 특수 4 = 68을 더해 비교한다."""
    from pathlib import Path

    from orbit.evalrunner.rag_arch.config import RERANKER_MAX_LENGTH, RERANKER_REVISION

    pytest.importorskip("transformers")
    cache = Path.home() / ".cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3/snapshots"
    if not (cache / RERANKER_REVISION).exists():
        pytest.skip("고정 리비전의 토크나이저가 캐시에 없다")
    import importlib.util

    script = Path(__file__).parents[4] / "scripts/rag_v1_reranker_tokens.py"
    spec = importlib.util.spec_from_file_location("rag_v1_reranker_tokens", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    world_dir = Path(__file__).parents[4] / "eval/datasets/orbit-eval/rag-v1"
    best, count = module.max_passage_tokens(world_dir)
    assert count == 1600 and best + 68 <= RERANKER_MAX_LENGTH


def test_empty_compressed_skips_originally_blank_bodies() -> None:
    from orbit.evalrunner.rag_arch.sut import empty_compressed

    mails: dict[str, dict[str, Any]] = {
        "M1": {"body": "본문"},
        "M2": {"body": "  \n"},
        "M3": {"body": None},
        "M4": {"body": "x"},
    }
    compressed = {"M1": "", "M2": "", "M3": "", "M4": "x"}
    assert empty_compressed(list(mails), mails, compressed) == ["M1"]


def test_answer_flow_version_in_fingerprint() -> None:
    from orbit.evalrunner.rag_arch.config import ANSWER_FLOW_VERSION

    assert _sut("A0", 1000).config_info(model_id="m")["answer_flow_version"] == ANSWER_FLOW_VERSION
    assert "answer_flow_version" not in _sut("A2", 1000).config_info(model_id="m")
    assert ANSWER_FLOW_VERSION == "rag-answer-flow-v2"


def test_answer_followup_rate_ignores_records_without_the_key() -> None:
    rows = [
        _rec("t1", 1, True, "c1", "A0", ctx=[], cited=[], followup=True),
        _rec("t2", 1, True, "c2", "A0", ctx=[], cited=[], followup=False),
        _rec("t3", 1, True, "c3", "A0", ctx=[], cited=[], followup=False),
        _rec("t4", 1, True, "c4", "A0", ctx=[], cited=[]),  # A2 기록처럼 키가 없다
    ]
    assert diagnostics.efficiency(rows)["answer_followup_rate"] == pytest.approx(1 / 3)
    assert diagnostics.efficiency(rows[3:])["answer_followup_rate"] is None
