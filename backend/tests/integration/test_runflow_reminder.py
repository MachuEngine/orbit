"""대화 리마인더 도구 `create_reminder` 통합 (ADR-0021 §5).

승인 없음, 봉투, run_id, 검증을 확인한다. 데이터는 모두 합성 값이다.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from integration.runflow_support import NOW, TZ, Harness
from integration.test_runflow_api import _checkpoint_dump, _job_args
from orbit.notify.limits import MAX_PENDING_REMINDERS
from orbit.runflow.reminder_tool import ReminderTool, reminder_id

Make = Callable[..., Awaitable[Harness]]

MARK = "SYNTH-REMINDER-MARK-4be9"
CALL = {
    "tool_calls": [
        {
            "name": "create_reminder",
            "arguments": {"text": f"보고서 제출 {MARK}", "due_at": "2026-11-11T15:00:00+09:00"},
        }
    ]
}


async def _tool_rows(h: Harness, run_id: Any) -> list[dict[str, Any]]:
    from orbit.records.runs import list_messages

    async with h.db.workspace_session(h.ws) as s:
        rows = await list_messages(s, workspace_id=h.ws, run_id=run_id)
    return [r.content for r in rows if r.role == "tool"]


async def test_reminder_is_created_without_approval_and_shown_in_upcoming(
    make_harness: Make,
) -> None:
    h = await make_harness([CALL, {"text": "알려 드리겠습니다."}])
    run_id = await h.start("오후 3시에 보고서 제출 알려줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert await h.count("SELECT count(*) FROM approval") == 0  # 승인 흐름을 거치지 않는다
    assert await h.count("SELECT count(*) FROM tool_call") == 0
    assert h.write_calls() == []  # 외부 쓰기 없음

    [row] = await _tool_rows(h, run_id)
    assert row["name"] == "create_reminder" and row["is_error"] is False
    assert json.loads(row["text"]) == {"created": True, "due_at": "2026-11-11T15:00:00+09:00"}

    # 모델 요청에서 결과는 데이터 봉투 안에 있다(ADR-0018)
    second = h.llm.requests[1]
    [tool_msg] = [m for m in second.messages if m.role == "tool"]
    assert (tool_msg.content or "").startswith('<orbit_data source="create_reminder"')

    async with h.db.workspace_session(h.ws) as s:
        stored = (
            await s.execute(text("SELECT kind, title, status, run_id, due_at FROM notification"))
        ).all()
    assert len(stored) == 1
    kind, title, status, stored_run, due = stored[0]
    assert (kind, title, status, stored_run) == (
        "reminder",
        f"보고서 제출 {MARK}",
        "scheduled",
        run_id,
    )
    assert due == datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
    upcoming = await h.client.get("/api/notifications/upcoming")
    assert [n["title"] for n in upcoming.json()["notifications"]] == [f"보고서 제출 {MARK}"]


async def test_invalid_calls_return_only_the_error_kind_and_create_nothing(
    make_harness: Make,
) -> None:
    def call(**arguments: Any) -> dict[str, Any]:
        return {"name": "create_reminder", "arguments": arguments}

    bad = [
        call(text=MARK, due_at="2026-11-11T15:00:00"),  # 시간대 없음
        call(text=MARK, due_at="내일 3시"),  # ISO가 아니다
        call(text=MARK, due_at="2026-11-10T15:00:00+09:00"),  # 과거
        call(text=MARK, due_at="2027-11-12T15:00:00+09:00"),  # 1년 초과
        call(text="   ", due_at="2026-11-11T15:00:00+09:00"),  # 빈 문구
        call(text="가" * 201, due_at="2026-11-11T15:00:00+09:00"),  # 200자 초과
        call(text=MARK, due_at="2026-11-11T15:00:00+09:00", extra=1),  # 알 수 없는 인자
        call(text=MARK),  # due_at 없음
    ]
    h = await make_harness([{"tool_calls": bad}, {"text": "만들지 못했습니다."}])
    run_id = await h.start("리마인더 만들어줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    results = await _tool_rows(h, run_id)
    assert [json.loads(r["text"]) for r in results] == [
        {"error": "invalid_time"},
        {"error": "invalid_time"},
        {"error": "time_in_past"},
        {"error": "too_far"},
        {"error": "invalid_arguments"},
        {"error": "invalid_arguments"},
        {"error": "invalid_arguments"},
        {"error": "invalid_arguments"},
    ]
    assert all(r["is_error"] for r in results)
    assert not any(MARK in r["text"] for r in results)  # 입력 원문은 모델에 되돌리지 않는다
    assert await h.count("SELECT count(*) FROM notification") == 0


async def test_boundaries_one_year_and_text_length(make_harness: Make) -> None:
    h = await make_harness([])
    run_id = await h.start("x")
    tool = ReminderTool(h.db, h.clock, TZ)
    edge = (NOW + timedelta(days=365)).astimezone(TZ).isoformat()
    ok = await tool.execute(
        {"text": "가" * 200, "due_at": edge},
        workspace_id=h.ws,
        run_id=run_id,
        step=1,
        llm_call_id="a",
    )
    assert not ok.is_error  # 정확히 1년 뒤와 200자는 허용한다
    over = await tool.execute(
        {"text": "x", "due_at": (NOW + timedelta(days=365, seconds=1)).isoformat()},
        workspace_id=h.ws,
        run_id=run_id,
        step=1,
        llm_call_id="b",
    )
    assert json.loads(over.content) == {"error": "too_far"}
    now = await tool.execute(
        {"text": "x", "due_at": NOW.isoformat()},
        workspace_id=h.ws,
        run_id=run_id,
        step=1,
        llm_call_id="c",
    )
    assert json.loads(now.content) == {"error": "time_in_past"}  # 지금은 과거로 본다


async def test_rerunning_the_same_call_does_not_create_a_second_reminder(
    make_harness: Make,
) -> None:
    h = await make_harness([])
    run_id = await h.start("x")
    tool = ReminderTool(h.db, h.clock, TZ)
    args = {"text": "합성 문구", "due_at": "2026-11-12T09:00:00+09:00"}
    for _ in range(2):  # 노드를 재실행해도 같은 (run, step, 호출 ID)는 한 번만 저장된다
        out = await tool.execute(
            args, workspace_id=h.ws, run_id=run_id, step=2, llm_call_id="call-1"
        )
        assert not out.is_error
    assert await h.count("SELECT count(*) FROM notification") == 1
    await tool.execute(args, workspace_id=h.ws, run_id=run_id, step=2, llm_call_id="call-2")
    assert await h.count("SELECT count(*) FROM notification") == 2  # 호출이 다르면 다른 알림
    assert reminder_id(run_id, 2, "call-1") != reminder_id(run_id, 3, "call-1")


async def test_pending_reminder_limit_blocks_new_ones_but_not_a_replay(make_harness: Make) -> None:
    h = await make_harness([])
    run_id = await h.start("x")
    tool = ReminderTool(h.db, h.clock, TZ)
    args = {"text": "합성", "due_at": "2026-11-12T09:00:00+09:00"}
    first = await tool.execute(args, workspace_id=h.ws, run_id=run_id, step=1, llm_call_id="a")
    assert not first.is_error
    async with h.db.workspace_session(h.ws) as s:
        await s.execute(
            text(
                "INSERT INTO notification (id, workspace_id, kind, title, due_at, status,"
                " created_at) SELECT gen_random_uuid(), :ws, 'reminder', 'x', :due, 'scheduled',"
                " :now FROM generate_series(1, :n)"
            ),
            {
                "ws": h.ws,
                "due": NOW + timedelta(days=1),
                "now": NOW,
                "n": MAX_PENDING_REMINDERS - 1,
            },
        )
    blocked = await tool.execute(args, workspace_id=h.ws, run_id=run_id, step=1, llm_call_id="b")
    assert json.loads(blocked.content) == {"error": "limit_reached"}
    replay = await tool.execute(args, workspace_id=h.ws, run_id=run_id, step=1, llm_call_id="a")
    assert not replay.is_error  # 이미 저장된 호출은 상한과 무관하게 성공으로 본다


async def test_reminder_text_stays_out_of_checkpoints_and_job_args(make_harness: Make) -> None:
    h = await make_harness([CALL, {"text": "끝."}])
    run_id = await h.start("보고서 알려줘")
    await h.work()
    assert await h.status(run_id) == "succeeded"
    rows, dump = await _checkpoint_dump(h)
    assert rows > 0 and MARK.encode() not in dump
    jobs = [row[0] for row in await _job_args(h)]
    assert jobs and all(set(a) == {"run_id", "workspace_id"} for a in jobs)
    assert not any(MARK in json.dumps(a, ensure_ascii=False) for a in jobs)
