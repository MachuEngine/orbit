"""ORACLE(gold 맥락 진단 기준) 단위 테스트. DB·LLM·검색 없이 합성 값만 쓴다."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orbit.assistant.tools import ToolOutcome
from orbit.evalrunner import cli as eval_cli
from orbit.evalrunner.rag_arch import config, diagnostics
from orbit.evalrunner.rag_arch.sut import RagArchSUT, oracle_message_ids
from orbit.evalrunner.trace import EpisodeTrace


class _Index:
    budget_error = None

    def mail_index_for(self, **kw: Any) -> Any:
        return object()

    def config_info(self) -> dict[str, Any]:
        return {"fingerprints": {"acct": "fp"}}


class _Reader:
    """`read_mail`만 흉내 낸다. 본문은 메일 ID로 정해지는 합성 값이다."""

    def __init__(self, *, workset: Any, **_: Any) -> None:
        self._ws = workset

    async def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        mid = self._ws.resolve_message(args["handle"])
        return ToolOutcome(json.dumps({"handle": args["handle"], "body": f"본문-{mid}"}))


class _LLM:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(text="답", tool_calls=(), provider_items=None)


@dataclass
class _Msg:
    message_id: str


def diag(out: Any) -> dict[str, Any]:
    assert out.rag_diag is not None
    return cast(dict[str, Any], out.rag_diag)


def _task(tid: str, split: str, ids: list[str] | None, kind: str = "message_set") -> Any:
    answer = SimpleNamespace(kind=kind, messages=[_Msg(i) for i in ids or []])
    return SimpleNamespace(task_id=tid, split=split, gold=SimpleNamespace(expected_answer=answer))


DATASET = SimpleNamespace(
    tasks=[
        _task("t1", "dev", ["m-b", "m-a", "m-b"]),
        _task("t2", "dev", None, kind="abstain"),
        _task("t3", "test", ["m-z"]),
    ]
)


def _episode(tid: str, llm: _LLM) -> Any:
    return SimpleNamespace(
        task=SimpleNamespace(
            task_id=tid,
            turns=(SimpleNamespace(role="user", content="질문?"),),
            as_of_time=datetime(2026, 1, 5, tzinfo=UTC),
            account_id="acct",
            time_zone="Asia/Seoul",
        ),
        database=SimpleNamespace(db=None, workspace_id="ws", ids=None),
        connectors=SimpleNamespace(mail=None, calendar=None),
        steps=SimpleNamespace(consume=lambda: None),
        llm=llm,
        trace=EpisodeTrace(),
    )


def _sut(monkeypatch: pytest.MonkeyPatch, cap: int = 6000) -> RagArchSUT:
    monkeypatch.setattr("orbit.evalrunner.rag_arch.sut.ReadOnlyTools", _Reader)

    async def no_search(*a: Any, **k: Any) -> Any:
        raise AssertionError("ORACLE은 검색하지 않는다")

    sut = RagArchSUT(
        rag_arch="ORACLE",
        index=cast(Any, _Index()),
        context_char_cap=cap,
        oracle_gold=oracle_message_ids(DATASET),
    )
    monkeypatch.setattr(sut, "_search", no_search)
    return sut


def test_oracle_message_ids_only_dev_in_gold_order_deduped() -> None:
    assert oracle_message_ids(DATASET) == {"t1": ("m-b", "m-a"), "t2": ()}


async def test_oracle_puts_only_gold_mails_in_order_without_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _LLM()
    ep = _episode("t1", llm)
    out = await _sut(monkeypatch).run(ep)
    user = llm.requests[0].messages[1].content
    assert user.index("본문-m-b") < user.index("본문-m-a")  # 맥락 순서는 gold 순서
    assert "본문-m-z" not in user
    system = llm.requests[0].messages[0].content
    assert system.count(config.RAG_ANSWER_NOTE) == 1 and system.endswith(config.RAG_ANSWER_NOTE)
    assert [t.name for t in ep.trace.tool_calls] == ["read_mail", "read_mail"]
    assert diag(out)["arch"] == "ORACLE" and diag(out)["search_calls"] == 0
    assert set(diag(out)["context_message_ids"]) == {"m-a", "m-b"}


async def test_oracle_cap_cuts_later_mails(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = _episode("t1", _LLM())
    out = await _sut(monkeypatch, cap=len("본문-m-b")).run(ep)  # 상한은 본문 글자 기준: 첫 메일만
    assert diag(out)["truncated"] is True
    assert diag(out)["context_message_ids"] == ["m-b"]  # gold 첫 ID만 남는다


async def test_oracle_without_gold_answers_with_empty_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _LLM()
    out = await _sut(monkeypatch).run(_episode("t2", llm))
    assert llm.requests[0].messages[1].content == "질문?"
    assert diag(out)["context_message_ids"] == [] and out.final_text == "답"


async def test_oracle_rejects_task_outside_dev_table(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        await _sut(monkeypatch).run(_episode("t3", _LLM()))  # test 과제는 표에 없다


def test_oracle_needs_gold_table_and_is_diagnostic_only() -> None:
    with pytest.raises(ValueError):
        RagArchSUT(rag_arch="ORACLE", index=cast(Any, _Index()))
    assert "ORACLE" in config.ARCHS and "ORACLE" not in config.MAIN_ARCHS
    assert config.DIAGNOSTIC_ARCHS == ("ORACLE",)
    assert "ORACLE" not in config.A3_ROUTES


def test_oracle_fingerprint_and_other_archs_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    sut = _sut(monkeypatch)
    info = sut.config_info(model_id="m")
    assert info["rag_arch"] == "ORACLE" and info["oracle_context_version"]
    assert info["prompt_version"] == config.A0_PROMPT_VERSION and info["context_char_cap"] == 6000
    assert (
        info["prompt_sha"] and "ORACLE" in sut.version and config.A0_PROMPT_VERSION in sut.version
    )
    a0 = RagArchSUT(rag_arch="A0", index=cast(Any, _Index())).config_info(model_id="m")
    assert "oracle_context_version" not in a0 and a0["top_k"] == config.TOP_K
    assert a0["condition_digest"] != info["condition_digest"]


def test_cli_rejects_oracle_on_test_split(capsys: pytest.CaptureFixture[str]) -> None:
    args = eval_cli._parser().parse_args(
        ["run", "--dataset", "d", "--split", "test", "--sut", "rag_arch", "--rag-arch", "ORACLE",
         "--index", "on", "--out", "o"]
    )  # fmt: skip
    assert eval_cli._run(args) == 2
    assert "dev 분할에서만" in capsys.readouterr().err


def test_rag_diag_accepts_oracle_results() -> None:
    from .test_rag_arch import GOLD, KINDS, _fixture, _rec

    by_arch = _fixture()
    by_arch["ORACLE"] = [
        _rec("t1", 1, True, "c1", "ORACLE", ctx=["m1"], cited=["m1"], search=0),
        _rec("t2", 1, True, "c2", "ORACLE", ctx=["m1", "m2"], cited=["m1", "m2"], search=0),
        _rec("t3", 1, True, "c3", "ORACLE", ctx=["m3"], cited=["m3"], search=0),
        _rec("t4", 1, True, "c4", "ORACLE", ctx=[], cited=[], search=0),
    ]
    report = diagnostics.diagnose(by_arch, GOLD, KINDS, k=1, resamples=50, seed=0)
    entry = report["archs"]["ORACLE"]
    assert entry["context_recall"] == 1.0 and entry["success"]["rate"] == 1.0
    assert entry["efficiency"]["search_calls_mean"] == 0


def test_rag_diag_excludes_oracle_from_paired_but_keeps_other_diagnostics() -> None:
    from .test_rag_arch import GOLD, KINDS, _fixture, _rec

    by_arch = _fixture()
    by_arch["ORACLE"] = [_rec("t1", 1, True, "c1", "ORACLE", ctx=["m1"], cited=["m1"], search=0)]
    report = diagnostics.diagnose(by_arch, GOLD, KINDS, k=1, resamples=50, seed=0)
    entry = report["archs"]["ORACLE"]
    assert "paired_vs_baseline" not in entry
    assert entry["by_type"]
    assert all("paired_vs_baseline" not in v for v in entry["by_type"].values())
    assert "paired_vs_baseline" in report["archs"]["A0"]  # 주 비교는 그대로
    assert "context_recall" in entry and "efficiency" in entry
