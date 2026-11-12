"""문서 도구 `search_docs`·`read_doc`과 D 핸들 (ADR-0020 §3). DB 없이 실행. 데이터는 합성 값이다."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orbit.assistant.tools import MAX_EXTERNAL_CHARS, tool_specs
from orbit.assistant.workset import UnknownHandleError, WorkSet
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.records.runs import RunMessageRecord
from orbit.runflow.context import ConversationContext, context_tool_spec
from orbit.runflow.deps import ScopeMissingDrive
from orbit.runflow.docs_tool import DocTools, doc_tool_specs
from orbit.runflow.fingerprint import config_fingerprint
from orbit.runflow.prompt import build_system_prompt
from orbit.runflow.transcript import Transcript
from orbit.runflow.write_tools import READ_TOOLS, run_tool_specs

WORLD_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sim" / "drive_world.json"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
B = "0123456789ab"


def _world() -> SimWorld:
    return SimWorld.load(WORLD_PATH, FixedClock(NOW))


def _tools(plan: dict[str, Any] | None = None, workset: WorkSet | None = None) -> DocTools:
    conns = build_sim_connectors(
        _world(), ["acct-alice"], FaultPlan.from_dict(plan) if plan else None
    )
    return DocTools(conns.drive["acct-alice"], workset or WorkSet(), UTC)


async def _run(tools: DocTools, name: str, args: object) -> tuple[dict[str, Any], Any]:
    outcome = await tools.execute(name, args)
    return json.loads(outcome.content), outcome


# ---------------------------------------------------------------- 작업 집합


def test_document_handles_follow_first_seen_order_and_resolve() -> None:
    ws = WorkSet()
    assert (ws.document_handle("f-1"), ws.document_handle("f-2"), ws.document_handle("f-1")) == (
        "D1",
        "D2",
        "D1",
    )
    assert ws.resolve_document(" d2 ") == "f-2"
    for bad in ("D3", "E1", "M1", "D0", "", 3, None):
        with pytest.raises(UnknownHandleError):
            ws.resolve_document(bad)


def test_export_and_restore_round_trip_with_documents() -> None:
    ws = WorkSet()
    ws.event_handle("c", "e")
    ws.message_handle("m")
    ws.document_handle("f-1")
    ws.document_handle("f-2")
    data = ws.export()
    assert data["documents"] == {"D1": "f-1", "D2": "f-2"}
    restored = WorkSet.restore(json.loads(json.dumps(data)))
    assert restored.export() == data
    assert restored.document_handle("f-3") == "D3"  # 번호가 이어진다
    with pytest.raises(ValueError, match="이어지지"):
        WorkSet.restore({"documents": {"D2": "f-2"}})


def test_export_without_documents_keeps_the_old_storage_format() -> None:
    ws = WorkSet()
    ws.event_handle("c", "e")
    ws.message_handle("m")
    assert set(ws.export()) == {"events", "messages"}
    assert WorkSet.restore({"events": {"E1": ["c", "e"]}, "messages": {}}).export() == {
        "events": {"E1": ["c", "e"]},
        "messages": {},
    }


# ---------------------------------------------------------------- search_docs


async def test_search_docs_registers_handles_and_hides_provider_ids() -> None:
    ws = WorkSet()
    body, outcome = await _run(_tools(workset=ws), "search_docs", {"text": "예산"})
    assert not outcome.is_error
    assert [f["handle"] for f in body["files"]] == ["D1", "D2"]
    first = next(
        f for f in body["files"] if f["name"] == "분기 회의록"
    )  # 텍스트 검색은 순서 비가정
    assert first["kind"] == "document"
    assert first["modified"] == "2026-11-10T09:00:00+00:00"
    assert first["owners"] == [{"name": "앨리스", "email": "alice@example.com"}]
    assert "f-doc-1" not in outcome.content and "f-sheet-1" not in outcome.content
    assert {ws.resolve_document("D1"), ws.resolve_document("D2")} == {"f-doc-1", "f-sheet-1"}
    assert body["has_more"] is False


async def test_search_docs_conditions_and_has_more() -> None:
    tools = _tools()
    body, _ = await _run(
        tools,
        "search_docs",
        {"kind": "document", "modified_after": "2026-11-05T00:00:00+09:00", "max_results": 1},
    )
    assert len(body["files"]) == 1 and body["has_more"] is True
    names, _ = await _run(tools, "search_docs", {})
    assert len(names["files"]) == 8  # 휴지통·폴더 제외


@pytest.mark.parametrize(
    "args",
    [
        {"unknown": 1},
        {"text": ""},
        {"text": 5},
        {"kind": "folder"},
        {"max_results": 0},
        {"max_results": 21},
        {"max_results": True},
        {"modified_after": "2026-11-05"},  # 오프셋 없음
        {"modified_after": "2026-11-09T00:00:00Z", "modified_before": "2026-11-08T00:00:00Z"},
        {"query": "name contains 'x'"},  # 모델은 Drive 문법을 쓰지 못한다
    ],
)
async def test_search_docs_rejects_bad_arguments(args: dict[str, Any]) -> None:
    body, outcome = await _run(_tools(), "search_docs", args)
    assert outcome.is_error and set(body) == {"error"}
    assert outcome.connector_error is None


async def test_non_dict_arguments_are_invalid() -> None:
    body, _ = await _run(_tools(), "search_docs", None)
    assert body == {"error": "invalid_arguments"}


# ---------------------------------------------------------------- read_doc


async def test_read_doc_returns_body_for_a_handle() -> None:
    tools = _tools()
    await tools.execute("search_docs", {"text": "회의록"})
    body, outcome = await _run(tools, "read_doc", {"handle": "d1"})
    assert not outcome.is_error
    assert body["handle"] == "D1" and body["name"] == "분기 회의록"
    assert body["body"].startswith("분기 회의록 본문입니다") and body["body_truncated"] is False
    assert "f-doc-1" not in outcome.content


async def test_read_doc_truncates_with_max_chars_and_global_cap() -> None:
    ws = WorkSet()
    ws.document_handle("f-long-1")
    body, _ = await _run(_tools(workset=ws), "read_doc", {"handle": "D1", "max_chars": 200})
    assert body["body"] == "0123456789ABCDEFGHIJ0123456789ABCDEFGHIJ"  # 40자라 200자 안에 든다
    assert body["body_truncated"] is False

    big = _world()
    big.account("acct-alice").files["f-long-1"] = (
        big.account("acct-alice")
        .files["f-long-1"]
        .model_copy(update={"text": "가" * (MAX_EXTERNAL_CHARS + 500)})
    )
    conns = build_sim_connectors(big, ["acct-alice"])
    capped = DocTools(conns.drive["acct-alice"], ws, UTC)
    body, _ = await _run(capped, "read_doc", {"handle": "D1"})
    assert len(body["body"]) == MAX_EXTERNAL_CHARS and body["body_truncated"] is True
    short, _ = await _run(capped, "read_doc", {"handle": "D1", "max_chars": 300})
    assert len(short["body"]) == 300 and short["body_truncated"] is True


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"handle": "D1", "extra": 1},
        {"handle": "D1", "max_chars": 10},
        {"handle": "D1", "max_chars": MAX_EXTERNAL_CHARS + 1},
        {"handle": "D1", "max_chars": "100"},
    ],
)
async def test_read_doc_rejects_bad_arguments(args: dict[str, Any]) -> None:
    ws = WorkSet()
    ws.document_handle("f-doc-1")
    body, outcome = await _run(_tools(workset=ws), "read_doc", args)
    assert body == {"error": "invalid_arguments"} and outcome.is_error


async def test_read_doc_only_accepts_document_handles_known_to_the_workset() -> None:
    ws = WorkSet()
    ws.event_handle("cal", "ev")
    ws.message_handle("msg")
    for handle in ("D1", "E1", "M1", "f-doc-1"):
        body, outcome = await _run(_tools(workset=ws), "read_doc", {"handle": handle})
        assert body == {"error": "unknown_handle"} and outcome.connector_error is None


async def test_read_doc_unsupported_and_not_found_kinds() -> None:
    ws = WorkSet()
    for fid in ("f-pdf-1", "f-big-1", "f-trash-1"):
        ws.document_handle(fid)
    tools = _tools(workset=ws)
    assert (await _run(tools, "read_doc", {"handle": "D1"}))[0] == {"error": "unsupported_file"}
    assert (await _run(tools, "read_doc", {"handle": "D2"}))[0] == {"error": "unsupported_file"}
    body, outcome = await _run(tools, "read_doc", {"handle": "D3"})
    assert body == {"error": "not_found"} and outcome.connector_error == ("not_found", "")


async def test_scope_missing_is_reported_with_connector_error_for_the_notice() -> None:
    plan = {"rules": [{"tool": "search_files", "error": "scope_missing"}]}
    body, outcome = await _run(_tools(plan), "search_docs", {"text": "예산"})
    assert body == {"error": "scope_missing"} and outcome.is_error
    assert outcome.connector_error == ("scope_missing", "scope_missing")
    # 권한이 없는 연결(리졸버가 주는 커넥터)도 외부 호출 없이 같은 결과다.
    ws = WorkSet()
    ws.document_handle("f-1")
    stub = DocTools(ScopeMissingDrive(), ws, UTC)
    for name, args in (("search_docs", {}), ("read_doc", {"handle": "D1"})):
        body, outcome = await _run(stub, name, args)
        assert body == {"error": "scope_missing"}
        assert outcome.connector_error == ("scope_missing", "scope_missing")


async def test_unknown_tool_name() -> None:
    body, _ = await _run(_tools(), "delete_doc", {})
    assert body == {"error": "unknown_tool"}


# ---------------------------------------------------------------- 도구 정의·프롬프트·지문


def test_doc_tools_are_read_tools_for_runs_only_not_for_evaluation() -> None:
    assert {"search_docs", "read_doc"} <= READ_TOOLS
    names = [s.name for s in run_tool_specs()]
    assert names.count("search_docs") == 1 and names.count("read_doc") == 1
    assert {s.name for s in doc_tool_specs()} == {"search_docs", "read_doc"}
    eval_names = {s.name for s in tool_specs()}
    assert not eval_names & {"search_docs", "read_doc"}  # 평가 도구 목록에는 없다
    assert not set(names) & {"create_doc", "update_doc", "delete_doc", "share_doc", "open_link"}


def test_prompt_v06_mentions_doc_tools_and_scope_error() -> None:
    prompt = build_system_prompt(NOW, UTC, B)
    assert "search_docs" in prompt and "read_doc" in prompt and "D1" in prompt
    assert "scope_missing" in prompt and "unsupported_file" in prompt
    assert "문서" in context_tool_spec().description


def test_fingerprint_covers_doc_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from orbit.runflow import fingerprint as fp

    base = config_fingerprint("m")
    without_docs = tuple(s for s in run_tool_specs() if s.name not in {"search_docs", "read_doc"})
    monkeypatch.setattr(fp, "run_tool_specs", lambda: without_docs)
    assert config_fingerprint("m") != base


# ---------------------------------------------------------------- 대화 맥락·출처 분류


def _tool_row(seq: int, name: str, payload: dict[str, Any], ws: WorkSet | None = None) -> Any:
    content: dict[str, Any] = {
        "step": 0,
        "tool_call_id": f"c{seq}",
        "name": name,
        "text": json.dumps(payload, ensure_ascii=False),
        "is_error": False,
    }
    if ws is not None:
        content["handles"] = ws.export()
    return RunMessageRecord(seq, "tool", content, NOW)


async def _prior_with_doc_search() -> tuple[ConversationContext, WorkSet]:
    ws = WorkSet()
    tools = _tools(workset=ws)
    outcome = await tools.execute("search_docs", {"text": "회의록"})
    rows = (
        RunMessageRecord(1, "user", {"text": "회의록 찾아줘"}, NOW),
        _tool_row(2, "search_docs", json.loads(outcome.content), ws),
        RunMessageRecord(3, "assistant", {"step": 1, "text": "찾았습니다.", "tool_calls": []}, NOW),
    )
    return ConversationContext((rows,), ws.export()), ws


async def test_conversation_context_targets_include_documents_with_name_and_time() -> None:
    prior, _ = await _prior_with_doc_search()
    assert prior.targets() == [
        {
            "handle": "D1",
            "kind": "document",
            "name": "분기 회의록",
            "modified": "2026-11-10T09:00:00+00:00",
        }
    ]
    # 소유자 주소나 본문은 대상에 담기지 않는다.
    assert "alice@example.com" not in json.dumps(prior.targets())


async def test_document_handle_survives_into_the_next_run_for_read_doc() -> None:
    prior, _ = await _prior_with_doc_search()
    restored = Transcript((), prior).workset()
    assert restored.resolve_document("D1") == "f-doc-1"
    body, _ = await _run(_tools(workset=restored), "read_doc", {"handle": "D1"})
    assert body["name"] == "분기 회의록"
    # 새 문서는 이어서 D2로 등록된다
    assert restored.document_handle("f-sheet-1") == "D2"


async def test_provenance_owners_are_workspace_data_and_body_is_external() -> None:
    ws = WorkSet()
    ws.document_handle("f-doc-1")
    tools = _tools(workset=ws)
    search = await tools.execute("search_docs", {"text": "회의록"})
    read = await tools.execute("read_doc", {"handle": "D1"})
    rows = (
        _tool_row(1, "search_docs", json.loads(search.content)),
        _tool_row(2, "read_doc", json.loads(read.content)),
    )
    emails, texts = Transcript(rows).provenance_inputs()
    assert emails.count("alice@example.com") == 2  # 소유자 주소: workspace_data
    assert any("예산 논의" in t for t in texts)  # 본문: 외부 콘텐츠
    assert "분기 회의록" in texts  # 이름도 외부 텍스트


async def test_doc_results_travel_inside_the_data_envelope() -> None:
    ws = WorkSet()
    outcome = await _tools(workset=ws).execute("search_docs", {"text": "회의록"})
    row = _tool_row(2, "search_docs", json.loads(outcome.content))
    messages = Transcript((row,)).llm_messages("SYS", B)
    content = messages[-1].content or ""
    assert content.startswith(f'<orbit_data source="search_docs" boundary="{B}">')
    assert content.endswith(f'</orbit_data boundary="{B}">')


async def test_read_doc_needs_no_separate_get_file_call() -> None:
    ws = WorkSet()
    ws.document_handle("f-doc-1")
    conns = build_sim_connectors(_world(), ["acct-alice"])
    tools = DocTools(conns.drive["acct-alice"], ws, UTC)
    body, _ = await _run(tools, "read_doc", {"handle": "D1"})
    assert body["name"] == "분기 회의록" and body["kind"] == "document"
    assert [r.tool for r in conns.calls.records] == ["read_file_text"]
