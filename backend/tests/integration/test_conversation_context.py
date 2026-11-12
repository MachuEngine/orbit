"""8단계 대화 맥락 이어가기: 맥락 주입, 핸들 유지, 출처 분류, 직렬화, 대화 API (ADR-0017)."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import text

from integration.runflow_support import DAY, Harness
from integration.support import WorkspaceFactory
from orbit.records.runs import append_message, create_run, transition_run
from orbit.runflow.context import OLDER_REQUESTS_MAX, PRIOR_RUNS_MAX

Make = Callable[..., Awaitable[Harness]]

WIDE = {"time_min": "2026-11-12T00:00:00+09:00", "time_max": "2026-11-14T00:00:00+09:00"}
MOVE_E1 = {
    "handle": "E1",
    "start": "2026-11-12T16:00:00+09:00",
    "end": "2026-11-12T17:00:00+09:00",
}


async def _next(h: Harness, message: str, conversation_id: str | None = None) -> tuple[Any, Any]:
    body: dict[str, Any] = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = await h.client.post("/api/runs", json=body, headers=h.headers())
    assert response.status_code == 201, response.text
    run_id = uuid.UUID(response.json()["run_id"])
    h.run_ids.append(run_id)
    return run_id, response.json()["conversation_id"]


async def _first(h: Harness, message: str) -> tuple[uuid.UUID, str]:
    run_id, conv = await _next(h, message)
    await h.work()
    return run_id, conv


def _texts(request: Any) -> list[tuple[str, str]]:
    return [(m.role, m.content or "") for m in request.messages]


async def test_first_run_request_has_no_context(make_harness: Make) -> None:
    h = await make_harness([{"text": "안녕하세요."}])
    await _first(h, "안녕")
    assert [role for role, _ in _texts(h.llm.requests[0])] == ["system", "user"]


async def test_handles_continue_across_runs_and_write_resolves_old_handle(
    make_harness: Make,
) -> None:
    script = [
        {"text": None, "tool_calls": [{"name": "list_events", "arguments": DAY}]},
        {"text": "오늘 일정은 두 개입니다."},
        {"text": None, "tool_calls": [{"name": "list_events", "arguments": WIDE}]},
        {"text": None, "tool_calls": [{"name": "update_event_time", "arguments": MOVE_E1}]},
        {"text": "옮겼습니다."},
    ]
    h = await make_harness(script)
    run1, conv = await _first(h, "내일 일정 알려줘")
    assert await h.status(run1) == "succeeded"

    run2, _ = await _next(h, "그 회의를 4시로 옮겨줘", conv)
    await h.work()
    assert await h.status(run2) == "waiting_approval"

    # 2번째 run의 첫 요청: 시스템 프롬프트, 맥락 합성 도구 쌍, 앞선 턴, 지금 메시지 순서(ADR-0018)
    first_request = _texts(h.llm.requests[2])
    assert [role for role, _ in first_request] == [
        "system",
        "assistant",
        "tool",
        "user",
        "assistant",
        "user",
    ]
    assert "주간 회의" not in first_request[0][1]  # 외부 문자열은 system에 없다
    assert first_request[2][1].startswith('<orbit_data source="conversation_context"')
    assert '"handle": "E1"' in first_request[2][1]
    assert '"title": "주간 회의"' in first_request[2][1]
    assert first_request[3][1] == "내일 일정 알려줘"
    assert first_request[4][1] == "오늘 일정은 두 개입니다."
    assert all("evalice" not in c for _, c in first_request)  # 제공자 ID는 모델에 보이지 않는다

    # 번호가 이어진다: 이번 run에서 처음 본 팀 회고는 E3, 앞선 E1은 그대로 E1
    async with h.db.workspace_session(h.ws) as s:
        handles = (
            await s.execute(
                text(
                    "SELECT content->'handles' FROM run_message WHERE run_id = :r"
                    " AND role = 'tool' ORDER BY seq LIMIT 1"
                ),
                {"r": run2},
            )
        ).scalar_one()
    assert handles["events"]["E1"] == ["cal-alice-main", "evalice001"]
    assert handles["events"]["E3"] == ["cal-alice-team", "evalice003"]

    approval_id = await h.approval_id(run2)
    approval = (await h.client.get(f"/api/approvals/{approval_id}")).json()
    assert approval["args"]["event_id"] == "evalice001"
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    assert await h.status(run2) == "succeeded"
    assert h.write_calls() == ["update_event"]
    assert not h.world_diff().is_empty


async def test_address_said_in_earlier_turn_is_user_provenance(make_harness: Make) -> None:
    send = {"to": ["bob@example.org"], "subject": "합성 제목", "body": "합성 본문"}
    script = [
        {"text": "알겠습니다. 보낼 내용을 알려주세요."},
        {"text": None, "tool_calls": [{"name": "send_mail", "arguments": send}]},
        {"text": "보냈습니다."},
    ]
    h = await make_harness(script)
    _, conv = await _first(h, "bob@example.org 에게 메일 쓸 거야")
    run2, _ = await _next(h, "방금 말한 주소로 보내줘", conv)
    await h.work()
    approval_id = await h.approval_id(run2)
    body = (await h.client.get(f"/api/approvals/{approval_id}")).json()
    assert body["provenance"] == {"bob@example.org": "user"}


async def test_busy_conversation_is_409_and_replay_comes_first(make_harness: Make) -> None:
    h = await make_harness([{"text": "하나"}, {"text": "둘"}])
    key = str(uuid.uuid4())
    headers = {**h.headers(), "Idempotency-Key": key}
    first = await h.client.post("/api/runs", json={"message": "하나"}, headers=headers)
    conv = first.json()["conversation_id"]  # run은 아직 queued

    busy = await h.client.post(
        "/api/runs", json={"message": "둘", "conversation_id": conv}, headers=h.headers()
    )
    assert busy.status_code == 409 and busy.json()["detail"] == "conversation_busy"
    replay = await h.client.post("/api/runs", json={"message": "하나"}, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert await h.count("SELECT count(*) FROM run") == 1

    await h.work()
    ok = await h.client.post(
        "/api/runs", json={"message": "둘", "conversation_id": conv}, headers=h.headers()
    )
    assert ok.status_code == 201
    h.run_ids.append(uuid.UUID(ok.json()["run_id"]))


async def test_waiting_approval_blocks_new_run(make_harness: Make) -> None:
    script = [
        {"text": None, "tool_calls": [{"name": "list_events", "arguments": DAY}]},
        {"text": None, "tool_calls": [{"name": "update_event_time", "arguments": MOVE_E1}]},
    ]
    h = await make_harness(script)
    run_id, conv = await _first(h, "옮겨줘")
    assert await h.status(run_id) == "waiting_approval"
    busy = await h.client.post(
        "/api/runs", json={"message": "또", "conversation_id": conv}, headers=h.headers()
    )
    assert busy.status_code == 409


async def test_concurrent_posts_to_one_conversation_create_one_run(make_harness: Make) -> None:
    h = await make_harness([{"text": "하나"}])
    _, conv = await _first(h, "하나")
    results = await asyncio.gather(
        *(
            h.client.post(
                "/api/runs",
                json={"message": f"동시 {i}", "conversation_id": conv},
                headers=h.headers(),
            )
            for i in range(4)
        )
    )
    assert sorted(r.status_code for r in results) == [201, 409, 409, 409]
    for r in results:
        if r.status_code == 201:
            h.run_ids.append(uuid.UUID(r.json()["run_id"]))
    assert await h.count("SELECT count(*) FROM run") == 2


async def test_conversation_list_and_detail(
    make_harness: Make, workspaces: WorkspaceFactory
) -> None:
    long_title = "가" * 50 + "\n둘째 줄"
    script = [{"text": "첫 답"}, {"text": "둘째 답"}, {"text": "셋째 답"}]
    h = await make_harness(script)
    run1, conv1 = await _first(h, long_title)
    h.clock.advance(timedelta(minutes=5))
    run2, _ = await _next(h, "후속", conv1)
    await h.work()
    h.clock.advance(timedelta(minutes=5))
    _, conv2 = await _first(h, "다른 대화")

    listing = (await h.client.get("/api/conversations")).json()["conversations"]
    assert [c["id"] for c in listing] == [conv2, conv1]  # 최근 활동 순
    by_id = {c["id"]: c for c in listing}
    assert by_id[conv1]["title"] == "가" * 40 + "…"
    assert by_id[conv2]["title"] == "다른 대화"
    assert by_id[conv1]["last_run_status"] == "succeeded"
    assert set(by_id[conv1]) == {"id", "title", "created_at", "last_activity_at", "last_run_status"}
    assert len((await h.client.get("/api/conversations?limit=1")).json()["conversations"]) == 1
    assert (await h.client.get("/api/conversations?limit=101")).status_code == 422

    detail = (await h.client.get(f"/api/conversations/{conv1}")).json()
    assert detail["id"] == conv1
    assert [(t["run_id"], t["user_text"], t["assistant_text"]) for t in detail["turns"]] == [
        (str(run1), long_title, "첫 답"),
        (str(run2), "후속", "둘째 답"),
    ]
    assert detail["turns"][0]["pending_approval_id"] is None

    other = await h.client_for(workspaces.create())
    assert (await other.get("/api/conversations")).json() == {"conversations": []}
    assert (await other.get(f"/api/conversations/{conv1}")).status_code == 404
    assert (await h.client.get(f"/api/conversations/{uuid.uuid4()}")).status_code == 404


async def test_conversation_detail_shows_pending_approval(make_harness: Make) -> None:
    script = [
        {"text": "찾는다.", "tool_calls": [{"name": "list_events", "arguments": DAY}]},
        {"text": None, "tool_calls": [{"name": "update_event_time", "arguments": MOVE_E1}]},
    ]
    h = await make_harness(script)
    run_id, conv = await _first(h, "옮겨줘")
    approval_id = await h.approval_id(run_id)
    turn = (await h.client.get(f"/api/conversations/{conv}")).json()["turns"][0]
    assert turn["status"] == "waiting_approval"
    assert turn["pending_approval_id"] == str(approval_id)
    assert turn["assistant_text"] == "찾는다."


async def test_unauthenticated_conversation_routes_are_401(make_harness: Make) -> None:
    h = await make_harness([{"text": "x"}])
    async with httpx.AsyncClient(transport=h.transport, base_url="http://test") as anon:
        assert (await anon.get("/api/conversations")).status_code == 401


async def test_read_range_is_limited_but_handles_still_continue(make_harness: Make) -> None:
    old_addr = "old-only@example.org"
    send = {"to": [old_addr], "subject": "합성 제목", "body": "합성 본문"}
    script = [
        {"text": None, "tool_calls": [{"name": "list_events", "arguments": WIDE}]},
        {"text": None, "tool_calls": [{"name": "send_mail", "arguments": send}]},
        {"text": "보냈습니다."},
    ]
    h = await make_harness(script)
    conv = h.api_ids.next()
    handles = {
        "events": {
            "E1": ["cal-alice-main", "evalice001"],
            "E2": ["cal-alice-main", "evalice002"],
        },
        "messages": {},
    }
    total = PRIOR_RUNS_MAX + 2
    async with h.db.workspace_session(h.ws) as s:
        await s.execute(
            text("INSERT INTO conversation (id, workspace_id, created_at) VALUES (:i, :w, :n)"),
            {"i": conv, "w": h.ws, "n": h.clock.now()},
        )
        for i in range(total):
            h.clock.advance(timedelta(seconds=1))
            run = await create_run(
                s,
                workspace_id=h.ws,
                conversation_id=conv,
                pattern="p",
                config_fingerprint="f",
                clock=h.clock,
                ids=h.api_ids,
            )
            user = f"{old_addr} 요청 {i}" if i < 2 else f"요청 {i}"
            await append_message(
                s,
                workspace_id=h.ws,
                run_id=run.id,
                role="user",
                content={"text": user},
                clock=h.clock,
            )
            if i == 0:  # 핸들 기록은 가장 오래된 run에만 있다
                tool = {
                    "step": 0,
                    "tool_call_id": "c",
                    "name": "list_events",
                    "text": "{}",
                    "is_error": False,
                    "handles": handles,
                }
                await append_message(
                    s, workspace_id=h.ws, run_id=run.id, role="tool", content=tool, clock=h.clock
                )
            for to in ("running", "succeeded"):
                await transition_run(
                    s, workspace_id=h.ws, run_id=run.id, to_status=to, clock=h.clock
                )

    run_id, _ = await _next(h, "아까 일정 보여줘", str(conv))
    await h.work()
    assert await h.status(run_id) == "waiting_approval"

    first = _texts(h.llm.requests[0])
    assert all(old_addr not in c for _, c in first)  # 범위 밖 사용자 원문은 입력에 없다
    assert [role for role, _ in first[:3]] == ["system", "assistant", "tool"]
    envelope = first[2][1]
    older = json.loads(envelope.split("\n")[1])["earlier_requests"]
    assert len(older) == OLDER_REQUESTS_MAX
    assert "요청 25" in older and "요청 1" not in older and "요청 2" not in older
    async with h.db.workspace_session(h.ws) as s:
        mapping = (
            await s.execute(
                text(
                    "SELECT content->'handles' FROM run_message WHERE run_id = :r"
                    " AND role = 'tool' ORDER BY seq LIMIT 1"
                ),
                {"r": run_id},
            )
        ).scalar_one()
    assert mapping["events"]["E3"] == ["cal-alice-team", "evalice003"]  # 번호가 이어진다
    approval = (await h.client.get(f"/api/approvals/{await h.approval_id(run_id)}")).json()
    assert approval["provenance"] == {old_addr: "model"}  # 범위 밖이라 user로 잡히지 않는다


async def test_model_can_call_conversation_context_tool(make_harness: Make) -> None:
    script = [
        {"text": None, "tool_calls": [{"name": "list_events", "arguments": DAY}]},
        {"text": "오늘 일정은 두 개입니다."},
        {"text": None, "tool_calls": [{"name": "conversation_context", "arguments": {}}]},
        {"text": "주간 회의가 있었습니다."},
    ]
    h = await make_harness(script)
    _, conv = await _first(h, "내일 일정 알려줘")
    run2, _ = await _next(h, "아까 뭐 봤지?", conv)
    await h.work()
    assert await h.status(run2) == "succeeded"
    last = _texts(h.llm.requests[3])
    tool_results = [c for role, c in last if role == "tool"]
    assert len(tool_results) == 2  # 합성 호출 결과와 모델이 부른 결과
    assert tool_results[0].startswith('<orbit_data source="conversation_context"')
    # 모델이 다시 부르면 같은 데이터를 받는다(경계값은 같은 run이라 같다)
    assert tool_results[1] == tool_results[0]
