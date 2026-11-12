"""10단계 문서 도구 통합 (ADR-0020): D 핸들, 봉투, 잘림, 권한 알림, 대화 범위. 합성 데이터."""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from integration.runflow_support import ACCOUNT, Harness, StubResolver
from integration.test_runflow_api import _checkpoint_dump, _job_args
from orbit.connectors.sim.world import SimFile
from orbit.runflow.deps import ScopeMissingDrive

Make = Callable[..., Awaitable[Harness]]

DOC_NAME = "분기 회의록"
DOC_BODY_MARK = "SYNTH-DOC-BODY-5d1e"
DOC_ADDR = "ext-doc@example.net"
OWNER = "doc-owner@example.com"
DOC_TEXT = f"예산 논의 {DOC_BODY_MARK}. 담당자 연락처는 {DOC_ADDR} 입니다."

SEARCH = {"tool_calls": [{"name": "search_docs", "arguments": {"text": "예산"}}]}
READ = {"tool_calls": [{"name": "read_doc", "arguments": {"handle": "D1"}}]}


def _add_files(h: Harness, **overrides: Any) -> None:
    from datetime import UTC, datetime

    from orbit.connectors.contracts import Address

    files = h.world.account(ACCOUNT).files
    files["doc-1"] = SimFile(
        id="doc-1",
        name=DOC_NAME,
        mime_type="application/vnd.google-apps.document",
        modified_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
        owners=(Address(email=OWNER, name="소유자"),),
        text=DOC_TEXT,
    )
    for file_id, update in overrides.items():
        files[file_id] = files[file_id].model_copy(update=update)


async def _tool_rows(h: Harness, run_id: uuid.UUID) -> list[dict[str, Any]]:
    from orbit.records.runs import list_messages

    async with h.db.workspace_session(h.ws) as s:
        rows = await list_messages(s, workspace_id=h.ws, run_id=run_id)
    return [r.content for r in rows if r.role == "tool"]


async def test_search_then_read_doc_by_handle(make_harness: Make) -> None:
    h = await make_harness([SEARCH, READ, {"text": "회의록 요약입니다."}])
    _add_files(h)
    run_id = await h.start("예산 얘기한 문서 읽어줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"

    search, read = await _tool_rows(h, run_id)
    found = json.loads(search["text"])["files"]
    assert [(f["handle"], f["name"]) for f in found] == [("D1", DOC_NAME)]
    assert "doc-1" not in search["text"]  # 제공자 ID는 모델에 가지 않는다
    assert search["handles"]["documents"] == {"D1": "doc-1"}  # 서버 저장소(RLS)에만 있다
    body = json.loads(read["text"])
    assert DOC_BODY_MARK in body["body"] and body["body_truncated"] is False
    assert read["name"] == "read_doc" and read["is_error"] is False

    # 모델 요청에서 문서 결과는 데이터 봉투 안에 있다(ADR-0018)
    third = h.llm.requests[2]
    tool_msgs = [m for m in third.messages if m.role == "tool"]
    assert [m.content.split("\n", 1)[0].split('"')[1] for m in tool_msgs if m.content] == [
        "search_docs",
        "read_doc",
    ]
    assert DOC_BODY_MARK in (tool_msgs[1].content or "")
    assert (tool_msgs[1].content or "").rstrip().endswith('">')
    system = next(m.content for m in third.messages if m.role == "system")
    assert DOC_BODY_MARK not in (system or "") and DOC_NAME not in (system or "")
    assert h.write_calls() == []  # 문서 도구는 외부 쓰기를 만들지 않는다
    assert [r.tool for r in h.conns.calls.records] == ["search_files", "read_file_text"]


async def test_long_body_is_truncated_and_marked(make_harness: Make) -> None:
    h = await make_harness([SEARCH, READ, {"text": "앞부분만 읽었습니다."}])
    _add_files(h, **{"doc-1": {"text": "예산 " + "가" * 9000}})
    run_id = await h.start("문서 읽어줘")
    await h.work()
    _, read = await _tool_rows(h, run_id)
    body = json.loads(read["text"])
    assert len(body["body"]) == 4000 and body["body_truncated"] is True


async def test_unknown_or_foreign_handles_are_rejected_without_connector_calls(
    make_harness: Make,
) -> None:
    script = [
        {"tool_calls": [{"name": "read_doc", "arguments": {"handle": "D1"}}]},
        {"tool_calls": [{"name": "read_doc", "arguments": {"handle": "doc-1"}}]},
        {"text": "읽지 못했습니다."},
    ]
    h = await make_harness(script)
    _add_files(h)
    run_id = await h.start("문서 읽어줘")
    await h.work()
    results = [json.loads(r["text"]) for r in await _tool_rows(h, run_id)]
    assert results == [{"error": "unknown_handle"}, {"error": "unknown_handle"}]
    assert [r.tool for r in h.conns.calls.records] == []
    assert "connector_notice" not in await h.event_types(run_id)


async def test_scope_missing_returns_error_and_one_drive_notice(make_harness: Make) -> None:
    plan = {"rules": [{"tool": "search_files", "error": "scope_missing"}]}
    script = [
        {"tool_calls": [SEARCH["tool_calls"][0], SEARCH["tool_calls"][0]]},
        {"text": "문서 권한이 필요합니다."},
    ]
    h = await make_harness(script, plan=plan)
    _add_files(h)
    run_id = await h.start("문서 찾아줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    results = [json.loads(r["text"]) for r in await _tool_rows(h, run_id)]
    assert results == [{"error": "scope_missing"}, {"error": "scope_missing"}]
    notices = [e.payload for e in await h.events(run_id) if e.type == "connector_notice"]
    assert notices == [  # (step, service, kind)당 하나
        {"step": 0, "service": "drive", "kind": "scope_missing", "reason": "scope_missing"}
    ]


async def test_connection_without_docs_scope_gives_the_same_notice(make_harness: Make) -> None:
    h = await make_harness([SEARCH, {"text": "권한이 없습니다."}])
    h.deps.resolver = StubResolver(
        h.conns.calendar[ACCOUNT], h.conns.mail[ACCOUNT], ScopeMissingDrive()
    )
    run_id = await h.start("문서 찾아줘")
    await h.work()
    notices = [e.payload for e in await h.events(run_id) if e.type == "connector_notice"]
    assert [(n["service"], n["kind"]) for n in notices] == [("drive", "scope_missing")]
    assert h.conns.calls.records == []  # 권한이 없으면 Drive 호출 자체가 없다


async def test_unsupported_file_is_a_tool_error_without_notice(make_harness: Make) -> None:
    h = await make_harness([SEARCH, READ, {"text": "읽을 수 없는 형식입니다."}])
    _add_files(h, **{"doc-1": {"mime_type": "application/pdf"}})
    run_id = await h.start("문서 읽어줘")
    await h.work()
    _, read = await _tool_rows(h, run_id)
    assert json.loads(read["text"]) == {"error": "unsupported_file"} and read["is_error"]
    assert "connector_notice" not in await h.event_types(run_id)


async def _next(h: Harness, message: str, conv: str) -> uuid.UUID:
    response = await h.client.post(
        "/api/runs", json={"message": message, "conversation_id": conv}, headers=h.headers()
    )
    assert response.status_code == 201, response.text
    run_id = uuid.UUID(response.json()["run_id"])
    h.run_ids.append(run_id)
    return run_id


async def test_document_handle_and_context_survive_into_the_next_run(make_harness: Make) -> None:
    h = await make_harness([SEARCH, {"text": "찾았습니다."}, READ, {"text": "읽었습니다."}])
    _add_files(h)
    first = await h.start("예산 문서 찾아줘")
    await h.work()
    conv = (await h.run_row(first))["conversation_id"]
    run2 = await _next(h, "그 문서 읽어줘", conv)
    await h.work()
    assert await h.status(run2) == "succeeded"

    context = next(
        m
        for m in h.llm.requests[2].messages
        if m.role == "tool" and "conversation_context" in (m.content or "")
    )
    assert f'"name": "{DOC_NAME}"' in (context.content or "")
    assert '"handle": "D1"' in (context.content or "") and '"kind": "document"' in (
        context.content or ""
    )
    assert "2026-11-10T" in (context.content or "")  # 수정 시각
    for secret in (DOC_BODY_MARK, OWNER, "doc-1"):
        assert secret not in (context.content or "")  # 대상에는 이름·시각만 있다
    [read] = await _tool_rows(h, run2)
    assert DOC_BODY_MARK in json.loads(read["text"])["body"]  # 이전 run의 D1로 읽었다
    assert read["handles"]["documents"] == {"D1": "doc-1"}


async def test_doc_body_address_is_external_content_and_owner_is_workspace_data(
    make_harness: Make,
) -> None:
    send = {"to": [DOC_ADDR, OWNER, "nobody@example.invalid"], "subject": "합성", "body": "합성"}
    script = [
        SEARCH,
        READ,
        {"tool_calls": [{"name": "send_mail", "arguments": send}]},
        {"text": "x"},
    ]
    h = await make_harness(script)
    _add_files(h)
    run_id = await h.start("문서를 읽고 담당자에게 메일 써줘")
    await h.work()
    approval_id = await h.approval_id(run_id)
    body = (await h.client.get(f"/api/approvals/{approval_id}")).json()
    assert body["provenance"] == {
        DOC_ADDR: "external_content",
        OWNER: "workspace_data",
        "nobody@example.invalid": "model",
    }
    assert h.write_calls() == []  # 승인 전에는 아무것도 보내지 않는다


async def test_no_doc_content_in_checkpoints_or_job_args(make_harness: Make) -> None:
    h = await make_harness([SEARCH, READ, {"text": "끝."}])
    _add_files(h)
    run_id = await h.start("예산 문서 읽어줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert await h.count(
        "SELECT count(*) FROM run_message WHERE content::text LIKE :p", p=f"%{DOC_BODY_MARK}%"
    )
    rows, dump = await _checkpoint_dump(h)
    assert rows > 0
    for marker in (DOC_BODY_MARK, DOC_NAME, DOC_ADDR, OWNER, "doc-1"):
        assert marker.encode() not in dump, marker
    jobs = [row[0] for row in await _job_args(h)]
    assert jobs and all(set(a) == {"run_id", "workspace_id"} for a in jobs)
    assert not any(DOC_NAME in json.dumps(a, ensure_ascii=False) for a in jobs)
