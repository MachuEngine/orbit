"""Run·승인 API, SSE, 격리, RLS 밖 저장소 점검 (ADR-0004, ADR-0006, ADR-0012 §4)."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text

from integration.runflow_support import (
    BODY_MARKER,
    DAY,
    EXTERNAL_ONLY_EMAIL,
    USER_MARKER,
    Harness,
)
from integration.support import WorkspaceFactory
from orbit.records.deletion import CHECKPOINT_TABLES, delete_run

Make = Callable[..., Awaitable[Harness]]

MOVE = {
    "handle": "E2",
    "start": "2026-11-12T14:00:00+09:00",
    "end": "2026-11-12T15:00:00+09:00",
}
SCRIPT = [
    {"text": "찾아본다.", "tool_calls": [{"name": "list_events", "arguments": DAY}]},
    {"text": None, "tool_calls": [{"name": "update_event_time", "arguments": MOVE}]},
    {"text": "옮겼습니다."},
]


async def _waiting(h: Harness, message: str = "점심을 2시로 옮겨줘") -> tuple[uuid.UUID, uuid.UUID]:
    run_id = await h.start(message)
    await h.work()
    assert await h.status(run_id) == "waiting_approval"
    return run_id, await h.approval_id(run_id)


# ---------------------------------------------------------------- POST /runs


async def test_post_runs_creates_run_message_and_job_together(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    run_id = await h.start("안녕")

    assert await h.count("SELECT count(*) FROM conversation") == 1
    assert await h.count("SELECT count(*) FROM run WHERE status = 'queued'") == 1
    rows = await h.count("SELECT count(*) FROM run_message WHERE role = 'user'")
    assert rows == 1
    jobs = await h.jobs("execute_run")
    assert [(j[1], j[2]) for j in jobs] == [("execute_run", "todo")]
    async with h.db.workspace_session(h.ws) as s:
        args = (
            await s.execute(
                text("SELECT args FROM procrastinate_jobs WHERE args->>'run_id' = :r"),
                {"r": str(run_id)},
            )
        ).scalar_one()
    assert args == {"run_id": str(run_id), "workspace_id": str(h.ws)}  # ID만


async def test_post_runs_reuses_existing_conversation(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    first = await h.client.post("/api/runs", json={"message": "하나"}, headers=h.headers())
    conv = first.json()["conversation_id"]
    second = await h.client.post(
        "/api/runs",
        json={"message": "둘", "conversation_id": conv},
        headers=h.headers(),
    )
    assert second.status_code == 201
    assert second.json()["conversation_id"] == conv
    assert await h.count("SELECT count(*) FROM conversation") == 1
    missing = await h.client.post(
        "/api/runs",
        json={"message": "셋", "conversation_id": str(uuid.uuid4())},
        headers=h.headers(),
    )
    assert missing.status_code == 404


async def test_post_runs_rejects_empty_message(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    response = await h.client.post("/api/runs", json={"message": ""}, headers=h.headers())
    assert response.status_code == 422


# ---------------------------------------------------------------- 다른 워크스페이스


async def test_other_workspace_gets_404_everywhere(
    make_harness: Make, workspaces: WorkspaceFactory
) -> None:
    h = await make_harness(SCRIPT)
    run_id, approval_id = await _waiting(h)
    other = await h.client_for(workspaces.create())

    for path in (f"/api/runs/{run_id}", f"/api/approvals/{approval_id}"):
        mine = await h.client.get(path)
        assert mine.status_code == 200, path
    # 승인 대기 중인 run의 SSE는 열려 있으므로(종료 상태가 아니다) 다른 워크스페이스 쪽만 확인한다.
    paths = (f"/api/runs/{run_id}", f"/api/runs/{run_id}/events", f"/api/approvals/{approval_id}")
    for path in paths:
        theirs = await other.get(path)
        assert theirs.status_code == 404, path
    for decision in ("approve", "reject"):
        response = await h.decide(approval_id, decision, client=other)
        assert response.status_code == 404
    # 다른 워크스페이스의 시도가 상태를 바꾸지 못했다.
    assert (await h.run_row(run_id))["pending_approval_id"] == str(approval_id)
    assert h.write_calls() == []
    conv = (await h.client.get(f"/api/runs/{run_id}")).json()["conversation_id"]
    stolen = await other.post(
        "/api/runs",
        json={"message": "x", "conversation_id": conv},
        headers=h.headers(other),
    )
    assert stolen.status_code == 404


# ---------------------------------------------------------------- 승인 화면 데이터


async def test_get_approval_returns_args_summary_provenance_and_expiry(
    make_harness: Make,
) -> None:
    h = await make_harness(SCRIPT)
    _, approval_id = await _waiting(h)
    body = (await h.client.get(f"/api/approvals/{approval_id}")).json()

    assert body["status"] == "pending"
    assert body["tool_name"] == "update_event_time"
    assert body["summary"] == {
        "handle": "E2",
        "start": "2026-11-12T05:00:00Z",
        "end": "2026-11-12T06:00:00Z",
    }
    assert body["args"]["event_id"] == "evalice002"
    assert body["provenance"] == {}
    assert body["expires_at"].startswith("2026-11-11T00:15:00")
    # 대상 요약: 일정의 현재 제목·시각. 제공자 ID는 없다.
    assert body["target"] == {
        "kind": "event",
        "title": "점심",
        "start": "2026-11-12T03:00:00Z",
        "end": "2026-11-12T04:00:00Z",
    }
    assert "evalice002" not in json.dumps(body["target"])


async def test_get_approval_target_for_cancel_and_reply_thread(make_harness: Make) -> None:
    cancel = [
        {"text": None, "tool_calls": [{"name": "list_events", "arguments": DAY}]},
        {"text": None, "tool_calls": [{"name": "cancel_event", "arguments": {"handle": "E2"}}]},
        {"text": "취소했습니다."},
    ]
    h = await make_harness(cancel)
    _, approval_id = await _waiting(h, "점심 취소해줘")
    body = (await h.client.get(f"/api/approvals/{approval_id}")).json()
    assert body["target"] == {
        "kind": "event",
        "title": "점심",
        "start": "2026-11-12T03:00:00Z",
        "end": "2026-11-12T04:00:00Z",
    }

    send = {"to": ["bob@example.org"], "subject": "Re", "body": "네.", "reply_to_handle": "M1"}
    reply = [
        {
            "text": None,
            "tool_calls": [{"name": "search_mail", "arguments": {"query": "in:inbox 프로젝트"}}],
        },
        {"text": None, "tool_calls": [{"name": "send_mail", "arguments": send}]},
        {"text": "보냈습니다."},
    ]
    h2 = await make_harness(reply)
    _, approval_id = await _waiting(h2, "답장해줘")
    body = (await h2.client.get(f"/api/approvals/{approval_id}")).json()
    assert body["target"] == {"kind": "thread", "subject": "프로젝트 일정 문의"}


async def test_get_approval_target_is_null_without_existing_target(make_harness: Make) -> None:
    create = {"title": "합성 회의", "start": MOVE["start"], "end": MOVE["end"]}
    h = await make_harness(
        [
            {"text": None, "tool_calls": [{"name": "create_event", "arguments": create}]},
            {"text": "만들었습니다."},
        ]
    )
    _, approval_id = await _waiting(h, "회의 잡아줘")
    body = (await h.client.get(f"/api/approvals/{approval_id}")).json()
    assert body["target"] is None


async def test_send_mail_records_argument_provenance(make_harness: Make) -> None:
    send = {
        "to": [
            "bob@example.org",
            "alice@example.com",
            EXTERNAL_ONLY_EMAIL,
            "nobody@example.invalid",
        ],
        "subject": "Re: 프로젝트 일정 문의",
        "body": "화요일 오전이 좋습니다.",
        "reply_to_handle": "M1",
    }
    script = [
        {
            "text": None,
            "tool_calls": [{"name": "search_mail", "arguments": {"query": "in:inbox 프로젝트"}}],
        },
        {"text": None, "tool_calls": [{"name": "read_mail", "arguments": {"handle": "M1"}}]},
        {"text": None, "tool_calls": [{"name": "send_mail", "arguments": send}]},
        {"text": "보냈습니다."},
    ]
    h = await make_harness(script)
    run_id, approval_id = await _waiting(h, f"{USER_MARKER} bob@example.org 에게 답장 보내줘")

    body = (await h.client.get(f"/api/approvals/{approval_id}")).json()
    assert body["provenance"] == {
        "bob@example.org": "user",
        "alice@example.com": "workspace_data",
        EXTERNAL_ONLY_EMAIL: "external_content",
        "nobody@example.invalid": "model",
    }
    assert body["summary"] == {"to_count": 4, "cc_count": 0, "is_reply": True}
    assert body["args"]["reply_to_thread_id"] == "thr-a-001"
    requested = next(e for e in await h.events(run_id) if e.type == "approval_requested")
    assert "bob@example.org" not in json.dumps(requested.payload)  # 이벤트에는 주소가 없다
    assert h.world_diff().is_empty

    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert len(h.world_diff().drafts_sent) == 1
    assert h.write_calls() == ["create_draft", "send_draft"]


async def test_second_write_call_in_one_response_is_rejected(make_harness: Make) -> None:
    cancel = {"handle": "E1"}
    script = [
        {"text": None, "tool_calls": [{"name": "list_events", "arguments": DAY}]},
        {
            "text": None,
            "tool_calls": [
                {"name": "update_event_time", "arguments": MOVE},
                {"name": "cancel_event", "arguments": cancel},
            ],
        },
        {"text": "끝"},
    ]
    h = await make_harness(script)
    run_id, approval_id = await _waiting(h)
    assert await h.count("SELECT count(*) FROM approval") == 1
    assert await h.count("SELECT count(*) FROM tool_call") == 1
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert len(h.world_diff().events_updated) == 1
    assert h.world_diff().events_cancelled == ()
    results = [m.content for m in h.llm.requests[-1].messages if m.role == "tool"]
    assert any("one_write_at_a_time" in (r or "") for r in results)


async def test_invalid_write_arguments_return_error_result_without_approval(
    make_harness: Make,
) -> None:
    script = [
        {"text": None, "tool_calls": [{"name": "cancel_event", "arguments": {"handle": "E9"}}]},
        {"text": "대상을 찾지 못했습니다."},
    ]
    h = await make_harness(script)
    run_id = await h.start("취소해줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert await h.count("SELECT count(*) FROM approval") == 0
    assert h.write_calls() == []
    results = [m.content for m in h.llm.requests[-1].messages if m.role == "tool"]
    assert any("unknown_handle" in (r or "") for r in results)


# ---------------------------------------------------------------- SSE


def _parse_sse(body: str) -> list[tuple[int, str, dict[str, Any]]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((int(lines["id"]), lines["event"], json.loads(lines["data"])))
    return events


async def test_sse_replays_after_last_event_id_without_gaps(make_harness: Make) -> None:
    h = await make_harness(SCRIPT)
    run_id, approval_id = await _waiting(h)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    stored = await h.events(run_id)
    assert len(stored) >= 6

    full = await h.client.get(f"/api/runs/{run_id}/events")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("text/event-stream")
    parsed = _parse_sse(full.text)
    assert [(seq, kind) for seq, kind, _ in parsed] == [(e.seq, e.type) for e in stored]
    assert [seq for seq, _, _ in parsed] == list(range(1, len(stored) + 1))

    for last in (0, 3, len(stored) - 1):
        resumed = await h.client.get(
            f"/api/runs/{run_id}/events",
            headers={**h.headers(), "Last-Event-ID": str(last)},
        )
        seqs = [seq for seq, _, _ in _parse_sse(resumed.text)]
        assert seqs == list(range(last + 1, len(stored) + 1))
    by_query = await h.client.get(f"/api/runs/{run_id}/events?after=4")
    assert [seq for seq, _, _ in _parse_sse(by_query.text)] == list(range(5, len(stored) + 1))
    done = await h.client.get(
        f"/api/runs/{run_id}/events", headers={**h.headers(), "Last-Event-ID": str(len(stored))}
    )
    assert done.status_code == 200
    assert done.text == ""  # 종료 상태이고 남은 이벤트가 없으면 바로 닫는다
    bad = await h.client.get(
        f"/api/runs/{run_id}/events", headers={**h.headers(), "Last-Event-ID": "abc"}
    )
    assert bad.status_code == 400


async def test_sse_waits_for_new_events_until_run_finishes(make_harness: Make) -> None:
    """승인 대기 중에 연결한 스트림이 폴링으로 새 이벤트를 받고 종료 상태에서 닫힌다."""
    h = await make_harness(SCRIPT)
    run_id, approval_id = await _waiting(h)
    waiting_events = len(await h.events(run_id))

    stream = asyncio.create_task(h.client.get(f"/api/runs/{run_id}/events"))
    await asyncio.sleep(0.6)  # 폴링 몇 번 동안 새 이벤트가 없다
    assert not stream.done()
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    response = await asyncio.wait_for(stream, timeout=10)

    seqs = [seq for seq, _, _ in _parse_sse(response.text)]
    stored = await h.events(run_id)
    assert seqs == list(range(1, len(stored) + 1))
    assert len(stored) > waiting_events  # 연결한 뒤에 생긴 이벤트도 빠짐없이 받았다


# ---------------------------------------------------------------- ADR-0006


async def _checkpoint_dump(h: Harness, thread_id: uuid.UUID | None = None) -> tuple[int, bytes]:
    """체크포인트 테이블 3개의 모든 열(바이트 포함)을 한 덩어리 바이트로 모은다.

    `thread_id`가 없으면 테이블 전체를 본다(표식이 어느 행에도 없어야 하므로 더 엄격하다).
    """
    chunks: list[bytes] = []
    rows_seen = 0
    async with h.db.workspace_session(h.ws) as s:
        for table in CHECKPOINT_TABLES:
            result = await s.execute(
                text(f"SELECT * FROM {table} WHERE CAST(:t AS text) IS NULL OR thread_id = :t"),  # noqa: S608
                {"t": None if thread_id is None else str(thread_id)},
            )
            for row in result:
                rows_seen += 1
                for value in row:
                    if isinstance(value, bytes | memoryview):
                        chunks.append(bytes(value))
                    elif isinstance(value, dict | list):
                        chunks.append(json.dumps(value, ensure_ascii=False).encode())
                    else:
                        chunks.append(str(value).encode())
    return rows_seen, b"\n".join(chunks)


async def test_no_user_content_in_checkpoints_or_job_args_and_delete_cascades(
    make_harness: Make,
) -> None:
    send = {
        "to": ["bob@example.org"],
        "subject": "Re: 프로젝트 일정 문의",
        "body": f"본문 {USER_MARKER}",
        "reply_to_handle": "M1",
    }
    script = [
        {
            "text": None,
            "tool_calls": [{"name": "search_mail", "arguments": {"query": "in:inbox 프로젝트"}}],
        },
        {"text": None, "tool_calls": [{"name": "read_mail", "arguments": {"handle": "M1"}}]},
        {"text": None, "tool_calls": [{"name": "send_mail", "arguments": send}]},
        {"text": "보냈습니다."},
    ]
    h = await make_harness(script)
    run_id, approval_id = await _waiting(h, f"{USER_MARKER} bob@example.org 에게 답장 보내줘")
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    assert await h.status(run_id) == "succeeded"

    # 표식은 RLS 테이블(run_message)에는 있다.
    for marker in (USER_MARKER, BODY_MARKER, "bob@example.org"):
        found = await h.count(
            "SELECT count(*) FROM run_message WHERE content::text LIKE :p", p=f"%{marker}%"
        )
        assert found >= 1, marker

    rows, dump = await _checkpoint_dump(h)
    own_rows, _ = await _checkpoint_dump(h, run_id)
    assert own_rows > 0 and rows >= own_rows  # 실제로 체크포인트가 저장됐다(점검이 비어 있지 않다)
    forbidden = (USER_MARKER, BODY_MARKER, "bob@example.org", EXTERNAL_ONLY_EMAIL, "프로젝트")
    for marker in forbidden:
        assert marker.encode() not in dump, f"체크포인트에 {marker!r}가 있다"
    jobs = [row[0] for row in await _job_args(h)]
    assert jobs  # 작업이 있었다
    for args in jobs:
        assert set(args) == {"run_id", "workspace_id"}
        assert not any(m in json.dumps(args) for m in forbidden)

    # run을 지우면 하위 행과 체크포인트가 같이 사라진다.
    async with h.db.workspace_session(h.ws) as s:
        assert await delete_run(s, workspace_id=h.ws, run_id=run_id) is True
    after_rows, _ = await _checkpoint_dump(h, run_id)
    assert after_rows == 0
    assert await h.count("SELECT count(*) FROM run_message") == 0


async def _job_args(h: Harness) -> list[tuple[dict[str, Any]]]:
    async with h.db.workspace_session(h.ws) as s:
        rows = await s.execute(
            text("SELECT args FROM procrastinate_jobs WHERE args->>'workspace_id' = :ws"),
            {"ws": str(h.ws)},
        )
        return [(r[0],) for r in rows]


# ---------------------------------------------------------------- 그래프 상태 좁은 스키마


async def test_graph_state_only_has_ids_and_control_values(make_harness: Make) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from orbit.runflow.graph import build_graph

    h = await make_harness(SCRIPT)
    run_id, _ = await _waiting(h)
    async with AsyncPostgresSaver.from_conn_string(h.deps.checkpoint_conninfo) as saver:
        graph = build_graph(h.deps, saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(run_id)}})
    assert set(snapshot.values) <= {"run_id", "workspace_id", "step", "tool_call_id", "next_node"}
    assert snapshot.values["step"] == 1
    assert snapshot.next == ("await_approval",)
    interrupts = [i.value for task in snapshot.tasks for i in task.interrupts]
    assert interrupts == [{"tool_call_id": snapshot.values["tool_call_id"]}]


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_resume_after_decision_uses_orbit_state_not_resume_value(
    make_harness: Make, decision: str
) -> None:
    h = await make_harness(SCRIPT)
    run_id, approval_id = await _waiting(h)
    assert (await h.decide(approval_id, decision)).status_code == 200
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert len(h.write_calls()) == (1 if decision == "approve" else 0)
