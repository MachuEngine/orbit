"""`--search-variant advanced`(ADR-0041)의 CLI 검증, 지문, 도구 제한 없음 동등성."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from orbit.assistant.workset import WorkSet
from orbit.evalrunner.cli import main
from orbit.evalrunner.rag_arch.a1_steps import compress_mails, rewrite_query
from orbit.evalrunner.rag_arch.config import (
    A2X_TOOL_NOTE,
    A2X_TOOL_NOTE_VERSION,
    CANDIDATES,
    RERANK_TOP_K,
)
from orbit.evalrunner.rag_arch.sut import RagArchSUT
from orbit.evalrunner.runflow_sut import UNCAPPED_CHARS, RunflowV0SUT, _AllTools
from orbit.llm.types import LLMResponse, LLMUsage
from orbit.runflow.prompt import build_system_prompt
from orbit.runflow.tool_limits import ToolLimits
from orbit.runflow.write_tools import run_tool_specs

from .conftest import DATASET_DIR


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


def _sut(**kw: Any) -> RunflowV0SUT:
    return RunflowV0SUT(index_mode="on", index=cast(Any, _Index()), **kw)


def _advanced() -> RunflowV0SUT:
    return _sut(search_variant="advanced", reranker=_Reranker())


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run", "--dataset", str(DATASET_DIR), "--split", "dev", "--out", str(tmp_path / "o.jsonl"),
        "--model-id", "openai/gpt-6-luna", *extra,
    ]  # fmt: skip


@pytest.mark.parametrize(
    "extra",
    [
        ["--sut", "noop", "--search-variant", "advanced"],
        ["--sut", "runflow_v0", "--search-variant", "advanced"],  # index off
        ["--sut", "runflow_v0", "--index", "on", "--pattern", "plan_execute",
         "--search-variant", "advanced"],
        ["--sut", "runflow_v0", "--index", "on", "--runflow-variant", "facts",
         "--search-variant", "advanced"],
    ],
)  # fmt: skip
def test_cli_rejects_advanced_outside_runflow_index_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    assert main(_args(tmp_path, *extra)) == 2
    assert "--search-variant advanced" in capsys.readouterr().err
    assert not (tmp_path / "o.jsonl").exists()


def test_cli_rejects_unknown_variant(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(_args(tmp_path, "--sut", "noop", "--search-variant", "x"))


def test_cli_allows_combo_and_stops_before_run_when_dependency_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("orbit.evalrunner.cli.importlib.util.find_spec", lambda name: None)
    args = _args(tmp_path, "--sut", "runflow_v0", "--index", "on", "--search-variant", "advanced")
    assert main(args) == 2
    err = capsys.readouterr().err
    assert "uv sync --group rag-eval" in err and "--search-variant advanced는 runflow" not in err
    assert not (tmp_path / "o.jsonl").exists()


# 변경 전 코드(HEAD a155227)에서 같은 입력으로 실제 계산한 값이다. 의도한 변경일 때만 갱신한다.
_HEAD_VERSION = "0+runflow-v0.8+eval-answer-v2+index-on"
_HEAD_RUNFLOW_FP_M = "4a7863994dcf7bd59c14358512de1d743dcd27f429864f9edd141ed13bcb2585"
_HEAD_RUN_FP = "a963b8c011e868e890da54987aa9fcf721c57273ba11595a267c2ffcb73330ea"
_HEAD_INFO = {
    "answer_extension": "eval-answer-v2",
    "index": {"stub": True},
    "index_mode": "on",
    "max_approvals_per_run": 5,
    "runflow_fingerprint": _HEAD_RUNFLOW_FP_M,
    "variants": [],
}
_HEAD_A2 = {
    "A2R": (
        "rag-arch-v1+A2R+runflow-v0.8+eval-answer-v2+rag-a2r-search-v2+rag-answer-note-v1"
        "+cap6000+index-on",
        "222ecae328280373",
    ),
    "A2X": (
        "rag-arch-v1+A2X+runflow-v0.8+eval-answer-v2+rag-a2x-search-v2+rag-a2x-tool-note-v1"
        "+rag-a1-rewrite-v1+rag-a1-compress-v1+rag-answer-note-v1+cap6000+index-on",
        "bad71bbbafa02b55",
    ),
}


@pytest.mark.parametrize("explicit", [False, True])
def test_default_fingerprint_is_unchanged(explicit: bool) -> None:
    sut = _sut(search_variant="default") if explicit else _sut()
    assert sut.version == _HEAD_VERSION
    assert sut.config_info(model_id="m") == _HEAD_INFO
    assert sut._run_fingerprint() == _HEAD_RUN_FP  # noqa: SLF001


@pytest.mark.parametrize("arch", ["A2R", "A2X"])
def test_rag_arch_a2_fingerprint_unchanged_by_refactor(arch: str) -> None:
    sut = RagArchSUT(rag_arch=arch, index=cast(Any, _Index()), reranker=_Reranker())
    version, digest = _HEAD_A2[arch]
    info = sut.config_info(model_id="m")
    assert sut.version == version and info["condition_digest"] == digest
    assert info["inner"]["runflow_fingerprint"] == _HEAD_RUNFLOW_FP_M


def test_advanced_run_fingerprint_differs_and_tracks_search_config() -> None:
    advanced = _advanced()
    fp = advanced._run_fingerprint()  # noqa: SLF001
    assert fp != _HEAD_RUN_FP and fp == _advanced()._run_fingerprint()  # noqa: SLF001
    other = _Reranker()
    other.revision = "def"
    changed = _sut(search_variant="advanced", reranker=other)
    assert changed._run_fingerprint() != fp  # noqa: SLF001


def test_cli_rejects_advanced_with_other_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = ["--sut", "runflow_v0", "--index", "on", "--search-variant", "advanced"]
    argv = _args(tmp_path, *extra)
    argv[argv.index("openai/gpt-6-luna")] = "other/model"
    assert main(argv) == 2
    assert "Luna" in capsys.readouterr().err
    assert not (tmp_path / "o.jsonl").exists()


def test_all_tools_supports_only_membership() -> None:
    tools = _AllTools()
    assert repr(ToolLimits(tools, UNCAPPED_CHARS)).startswith("ToolLimits(allowed=_AllTools()")
    assert "anything" in tools
    with pytest.raises(TypeError):
        iter(tools)
    with pytest.raises(TypeError):
        len(tools)


def test_advanced_rag_diag_merges_limits_and_search() -> None:
    limits = ToolLimits(_AllTools(), UNCAPPED_CHARS)
    limits.search_calls = 2
    search = cast(Any, type("S", (), {"diag": lambda self: {"search_variant": "advanced"}})())
    diag = RunflowV0SUT._rag_diag(search, limits)  # noqa: SLF001
    assert diag is not None
    assert diag["search_variant"] == "advanced" and diag["search_calls"] == 2
    assert {"read_calls", "context_chars", "truncated", "index_pending", "arch"} <= set(diag)
    assert diag["arch"] == "A2X"
    assert RunflowV0SUT._rag_diag(None, None) is None  # noqa: SLF001


class _LLM:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response

    async def complete(self, request: Any) -> LLMResponse:
        return self.response


def _episode(response: LLMResponse) -> Any:
    steps = type("St", (), {"consume": lambda self: None})()
    task = type("T", (), {"task_id": "t-1"})()
    return type("E", (), {"steps": steps, "llm": _LLM(response), "task": task})()


def _resp(text: str | None, finish: str = "stop") -> LLMResponse:
    usage = LLMUsage(input_tokens=0, output_tokens=0)
    return LLMResponse(
        text=text, tool_calls=(), finish_reason=cast(Any, finish), usage=usage,
        cost_usd=0.0, latency_ms=0, model_id=None, provider=None,
    )  # fmt: skip


async def test_rewrite_falls_back_to_question_on_bad_format_or_length() -> None:
    assert await rewrite_query(_episode(_resp("not json")), "원래 질문") == ("원래 질문", True)
    ok = _resp(json.dumps({"query": "새 질의"}, ensure_ascii=False))
    assert await rewrite_query(_episode(ok), "원래 질문") == ("새 질의", False)
    cut = _resp(json.dumps({"query": "새 질의"}, ensure_ascii=False), "length")
    assert await rewrite_query(_episode(cut), "원래 질문") == ("원래 질문", True)


async def test_compress_falls_back_to_original_body() -> None:
    mails = {"M1": {"body": "원문 하나"}, "M2": {"body": "원문 둘"}}
    out, fallbacks = await compress_mails(
        _episode(_resp("not json")), "q", ["M1", "M2"], mails, "a" * 12
    )
    assert out == {"M1": "원문 하나", "M2": "원문 둘"} and fallbacks == 2
    partial = _resp(json.dumps({"M1": ["원문 하나"]}, ensure_ascii=False))
    out, fallbacks = await compress_mails(_episode(partial), "q", ["M1", "M2"], mails, "a" * 12)
    assert out["M2"] == "원문 둘" and fallbacks >= 1  # M2는 응답에 없어 원문으로 되돌린다


def test_advanced_fingerprint_items() -> None:
    plain, advanced = _sut(), _advanced()
    assert advanced.version != plain.version and "rag-a2x-search" in advanced.version
    info = advanced.config_info(model_id="m")
    assert {k: v for k, v in info.items() if k != "search"} == plain.config_info(model_id="m")
    search = info["search"]
    assert search["search_variant"] == "advanced"
    assert search["reranker"] == {
        "model_id": "fake/r", "revision": "abc", "max_length": 768, "device": "cpu",
    }  # fmt: skip
    assert search["candidates"] == CANDIDATES and search["rerank_top_k"] == RERANK_TOP_K
    assert search["tool_note_version"] == A2X_TOOL_NOTE_VERSION
    for key in ("rewrite_version", "compress_version", "rewrite_prompt_sha", "compress_prompt_sha"):
        assert search[key]


def test_advanced_constructor_guards() -> None:
    with pytest.raises(ValueError):
        _sut(search_variant="advanced")  # reranker 없음
    with pytest.raises(ValueError):
        RunflowV0SUT(search_variant="advanced", reranker=_Reranker())  # index off
    with pytest.raises(ValueError):
        _sut(search_variant="nope")


def test_advanced_limits_change_nothing_but_search_and_note() -> None:
    sut = _advanced()
    episode = cast(Any, type("E", (), {"task": type("T", (), {"time_zone": "UTC"})()})())
    limits, search = sut._advanced_limits(episode, None)  # noqa: SLF001
    assert limits.search_override is search and limits.search_note == A2X_TOOL_NOTE
    names = [spec.name for spec in run_tool_specs()] + ["submit_answer", "unknown_xyz"]
    assert all(n in limits.allowed for n in names)
    now = datetime(2026, 11, 11, tzinfo=UTC)
    system = build_system_prompt(now, ZoneInfo("UTC"), "b" * 12)
    assert build_system_prompt(now, ZoneInfo("UTC"), "b" * 12, limits.allowed) == system
    assert limits.filter_prompt(system, names) == system
    # 결과 본문은 길이와 상관없이 잘리지 않고 거부 기록도 없다
    workset = WorkSet()
    handle = workset.message_handle("msg-1")
    big = "가" * 50_000
    search_result = json.dumps(
        {"results": [{"handle": handle, "excerpt": big, "subject": "s"}]},
        ensure_ascii=False,
        sort_keys=True,
    )
    read_result = json.dumps({"handle": handle, "body": big}, ensure_ascii=False, sort_keys=True)
    assert limits.filter_result("search_indexed_mail", search_result, workset) == search_result
    assert limits.filter_result("read_mail", read_result, workset) == read_result
    assert not limits.truncated and not limits.denied and limits.char_cap == UNCAPPED_CHARS
