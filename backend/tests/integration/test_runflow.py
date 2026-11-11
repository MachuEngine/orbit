"""쓰기 Run 통합 테스트 (ADR-0003 조건 1~4, ADR-0005, ADR-0012 §4).

실제 DB·체크포인터·작업 큐 위에서 승인 -> 실행 흐름을 확인한다. LLM과 커넥터만 합성 대체물이다.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from integration.runflow_support import DAY, Harness
from orbit.runflow.deps import NoConnection
from orbit.runflow.worker import RECOVER_STALLED, RESUME_RUN

Make = Callable[..., Awaitable[Harness]]

MOVE = {
    "handle": "E2",
    "start": "2026-11-12T14:00:00+09:00",
    "end": "2026-11-12T15:00:00+09:00",
}


def move_script(final: str = "점심을 14시로 옮겼습니다.") -> list[dict[str, Any]]:
    return [
        {"text": "일정을 찾아본다.", "tool_calls": [{"name": "list_events", "arguments": DAY}]},
        {"text": None, "tool_calls": [{"name": "update_event_time", "arguments": MOVE}]},
        {"text": final},
    ]


async def _until_waiting(
    h: Harness, message: str = "점심을 2시로 옮겨줘"
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = await h.start(message)
    await h.work()
    assert await h.status(run_id) == "waiting_approval"
    return run_id, await h.approval_id(run_id)


# ---------------------------------------------------------------- 승인 없는 쓰기 0


async def test_no_external_write_before_approval(make_harness: Make) -> None:
    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)

    assert h.world_diff().is_empty
    assert h.write_calls() == []
    assert (await h.run_row(run_id))["pending_approval_id"] == str(approval_id)
    assert await h.count("SELECT count(*) FROM execution") == 0

    # 승인 전에 재개 작업이 와도 아무것도 하지 않는다(승인이 pending).
    await _defer_resume(h, run_id)
    await h.work()
    assert await h.status(run_id) == "waiting_approval"
    assert h.write_calls() == []
    assert len(h.llm.requests) == 2  # 승인 대기 중에 LLM을 다시 부르지 않았다


async def _defer_resume(h: Harness, run_id: uuid.UUID) -> None:
    from orbit.records.jobs import defer_job

    async with h.db.workspace_session(h.ws) as s:
        await defer_job(
            s,
            h.worker_app,
            task_name=RESUME_RUN,
            args={"run_id": str(run_id), "workspace_id": str(h.ws)},
        )


async def test_rejected_approval_never_writes(make_harness: Make) -> None:
    h = await make_harness(move_script("알겠습니다. 바꾸지 않았습니다."))
    run_id, approval_id = await _until_waiting(h)

    response = await h.decide(approval_id, "reject")
    assert response.status_code == 200
    assert response.json() == {"status": "rejected"}
    await h.work()

    assert await h.status(run_id) == "succeeded"
    assert h.world_diff().is_empty
    assert h.write_calls() == []
    assert await h.count("SELECT count(*) FROM execution") == 0
    types = await h.event_types(run_id)
    assert "execution_result" in types
    result = next(e for e in await h.events(run_id) if e.type == "execution_result")
    assert result.payload["status"] == "rejected"
    # 모델은 거절 결과를 도구 결과로 받았다.
    tool_texts = [m.content for m in h.llm.requests[-1].messages if m.role == "tool"]
    assert any('"rejected"' in (t or "") for t in tool_texts)


async def test_expired_approval_never_writes(make_harness: Make) -> None:
    h = await make_harness(move_script("만료되어 바꾸지 않았습니다."))
    run_id, approval_id = await _until_waiting(h)

    h.clock.advance(timedelta(minutes=16))
    response = await h.decide(approval_id, "approve")
    assert response.status_code == 409  # 만료된 승인은 결정할 수 없다
    await h.work()  # 만료를 반영해 run을 이어서 끝낸다

    assert await h.status(run_id) == "succeeded"
    assert h.world_diff().is_empty
    assert h.write_calls() == []
    assert await h.count("SELECT count(*) FROM execution") == 0
    assert await h.count("SELECT count(*) FROM approval WHERE status = 'expired'") == 1


async def test_expiry_without_any_decision_is_applied_by_resume_job(make_harness: Make) -> None:
    h = await make_harness(move_script("만료되었습니다."))
    run_id, _ = await _until_waiting(h)

    h.clock.advance(timedelta(minutes=16))
    await _defer_resume(h, run_id)
    await h.work()

    assert await h.status(run_id) == "succeeded"
    assert h.write_calls() == []
    assert await h.count("SELECT count(*) FROM approval WHERE status = 'expired'") == 1


# ---------------------------------------------------------------- 승인 -> 정확히 1번 쓰기


async def test_approve_executes_once_and_finishes(make_harness: Make) -> None:
    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)

    response = await h.decide(approval_id, "approve")
    assert response.status_code == 200
    assert response.json() == {"status": "approved"}
    await h.work()

    assert await h.status(run_id) == "succeeded"
    updated = h.world_diff().events_updated
    assert len(updated) == 1
    assert updated[0].event_id == "evalice002"
    assert h.write_calls() == ["update_event"]
    assert await h.count("SELECT count(*) FROM execution WHERE status = 'succeeded'") == 1

    events = await h.events(run_id)
    assert [e.type for e in events] == [
        "status",  # running
        "assistant_text",
        "approval_requested",
        "status",  # waiting_approval
        "status",  # running (재개)
        "execution_result",
        "assistant_text",
        "status",  # succeeded
    ]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    statuses = [e.payload["status"] for e in events if e.type == "status"]
    assert statuses == ["running", "waiting_approval", "running", "succeeded"]
    requested = next(e for e in events if e.type == "approval_requested")
    assert requested.payload["approval_id"] == str(approval_id)
    assert requested.payload["tool_name"] == "update_event_time"
    assert requested.payload["summary"]["handle"] == "E2"
    assert next(e for e in events if e.type == "execution_result").payload["status"] == "succeeded"
    # 승인 인자가 실행된 일정 시각과 같다.
    change = updated[0].changes["start"].after
    assert "2026-11-12T05:00:00" in str(change)  # 14:00 KST = 05:00 UTC


async def test_decision_on_decided_approval_conflicts(make_harness: Make) -> None:
    h = await make_harness(move_script())
    _, approval_id = await _until_waiting(h)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    assert (await h.decide(approval_id, "approve")).status_code == 409
    assert (await h.decide(approval_id, "reject")).status_code == 409
    assert len(await h.jobs(RESUME_RUN)) == 1


# ---------------------------------------------------------------- S4: 쓰기 직후 처리기 중단


async def test_s4_crash_after_external_write_does_not_duplicate(make_harness: Make) -> None:
    script = [
        {
            "text": None,
            "tool_calls": [
                {
                    "name": "create_event",
                    "arguments": {
                        "title": "합성 회의",
                        "start": "2026-11-14T10:00:00+09:00",
                        "end": "2026-11-14T11:00:00+09:00",
                    },
                }
            ],
        },
        {"text": "일정을 만들었습니다."},
    ]
    h = await make_harness(script)
    run_id, approval_id = await _until_waiting(h, "토요일 10시에 합성 회의 잡아줘")

    h.calendar.crash_after_create = True  # create_event 적용 직후 예외, execution은 finish 전
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()

    # 외부 쓰기는 이미 한 번 적용됐고 run은 끝나지 않았다. 작업은 재시도 대기다.
    assert len(h.world_diff().events_created) == 1
    assert await h.status(run_id) == "running"
    assert await h.count("SELECT count(*) FROM execution WHERE status = 'pending'") == 1
    (_, _, job_status, attempts) = (await h.jobs(RESUME_RUN))[0]
    assert (job_status, attempts) == ("todo", 1)

    # 실행자가 죽어 lease가 만료됐고, 작업이 재시도된다(처음부터 다시 실행).
    await h.expire_leases()
    await h.make_jobs_due()
    await h.work()

    assert await h.status(run_id) == "succeeded"
    assert len(h.world_diff().events_created) == 1  # 정확히 1번
    assert h.write_calls() == ["create_event"]  # 재호출 없음(대조로 확인)
    assert await h.count("SELECT count(*) FROM execution WHERE status = 'succeeded'") == 1


async def test_result_unknown_is_reconciled_without_second_write(make_harness: Make) -> None:
    plan = {"rules": [{"tool": "create_event", "nth_call": 1, "error": "result_unknown"}]}
    script = [
        {
            "text": None,
            "tool_calls": [
                {
                    "name": "create_event",
                    "arguments": {
                        "title": "합성 회의",
                        "start": "2026-11-14T10:00:00+09:00",
                        "end": "2026-11-14T11:00:00+09:00",
                    },
                }
            ],
        },
        {"text": "일정을 만들었습니다."},
    ]
    h = await make_harness(script, plan=plan)
    run_id, approval_id = await _until_waiting(h)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()

    assert await h.status(run_id) == "succeeded"
    assert len(h.world_diff().events_created) == 1
    assert h.write_calls() == ["create_event"]
    assert await h.count("SELECT count(*) FROM execution WHERE status = 'succeeded'") == 1


# ---------------------------------------------------------------- S7: 동시 승인


async def test_s7_concurrent_decisions_execute_once(make_harness: Make) -> None:
    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)

    responses = await asyncio.gather(
        h.decide(approval_id, "approve"),
        h.decide(approval_id, "approve"),
        h.decide(approval_id, "approve"),
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409]
    assert len(await h.jobs(RESUME_RUN)) == 1
    await h.work()

    assert await h.status(run_id) == "succeeded"
    assert h.write_calls() == ["update_event"]
    assert len(h.world_diff().events_updated) == 1


async def test_concurrent_approve_and_reject_has_single_winner(make_harness: Make) -> None:
    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)
    responses = await asyncio.gather(
        h.decide(approval_id, "approve"), h.decide(approval_id, "reject")
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(await h.jobs(RESUME_RUN)) == 1
    await h.work()
    assert await h.status(run_id) == "succeeded"
    assert len(h.write_calls()) <= 1


# ---------------------------------------------------------------- 장애 후 agent 재실행: step 멱등


async def test_agent_rerun_does_not_call_llm_again(make_harness: Make) -> None:
    h = await make_harness(move_script())
    h.calendar.fail_list_once = True  # step 0: LLM 응답을 저장한 뒤 읽기 도구에서 중단
    run_id = await h.start("점심을 2시로 옮겨줘")
    await h.work()

    assert len(h.llm.requests) == 1
    assert await h.status(run_id) == "running"
    assert (await h.jobs("execute_run"))[0][2:] == ("todo", 1)  # 재시도 대기

    await h.make_jobs_due()
    await h.work()

    # 재실행된 agent 노드는 저장된 응답을 썼으므로 step 0의 LLM 호출은 한 번뿐이다.
    assert len(h.llm.requests) == 2
    assert await h.status(run_id) == "waiting_approval"
    assert await h.count("SELECT count(*) FROM run_message WHERE role = 'assistant'") == 2
    assert await h.count("SELECT count(*) FROM tool_call") == 1
    assert await h.count("SELECT count(*) FROM approval") == 1


async def test_job_handler_is_idempotent_for_finished_and_missing_runs(
    make_harness: Make,
) -> None:
    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()
    assert await h.status(run_id) == "succeeded"
    requests_before = len(h.llm.requests)

    await _defer_resume(h, run_id)  # 종료 상태 run에 재개 작업이 또 온다
    await _defer_resume(h, uuid.uuid4())  # 존재하지 않는 run
    await h.work()

    assert len(h.llm.requests) == requests_before
    assert h.write_calls() == ["update_event"]
    assert all(job[2] == "succeeded" for job in await h.jobs())


# ---------------------------------------------------------------- 인자 변조 방지


async def test_app_role_cannot_change_approved_tool_call_args(make_harness: Make) -> None:
    h = await make_harness(move_script())
    run_id, _ = await _until_waiting(h)
    with pytest.raises(DBAPIError):
        async with h.db.workspace_session(h.ws) as s:
            await s.execute(
                text("UPDATE tool_call SET args = CAST('{}' AS jsonb) WHERE run_id = :r"),
                {"r": run_id},
            )


async def test_args_changed_after_approval_are_not_executed(
    make_harness: Make, owner_url: str
) -> None:
    """저장된 인자가 승인된 해시와 달라지면(소유자 권한으로 직접 바꾼 경우) 실행하지 않는다."""
    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)
    engine = create_engine(owner_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(h.ws)}
            )
            changed = conn.execute(
                text(
                    "UPDATE tool_call SET args = jsonb_set(args, '{event_id}', '\"evalice001\"')"
                    " WHERE run_id = :r"
                ),
                {"r": run_id},
            ).rowcount
            assert changed == 1
    finally:
        engine.dispose()

    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()

    assert await h.status(run_id) == "failed"
    assert (await h.run_row(run_id))["error_kind"] == "hash_mismatch"
    assert h.world_diff().is_empty
    assert h.write_calls() == []
    assert await h.count("SELECT count(*) FROM execution") == 0
    types = await h.event_types(run_id)
    assert "error" in types and types[-1] == "status"


# ---------------------------------------------------------------- 멈춘 작업 복구 주기 작업


async def test_recover_stalled_task_returns_dead_workers_job_to_queue(make_harness: Make) -> None:
    h = await make_harness(move_script())
    run_id = await h.start("점심을 2시로 옮겨줘")
    (job_id, _, _, _) = (await h.jobs("execute_run"))[0]

    # 워커가 작업을 가져간 뒤 죽은 상황: 작업은 doing, 워커 heartbeat는 1시간 전이다.
    async with h.db.workspace_session(h.ws) as s:
        worker_id = (
            await s.execute(text("SELECT procrastinate_register_worker_v1()"))
        ).scalar_one()
        fetched = (
            await s.execute(
                text("SELECT id FROM procrastinate_fetch_job_v2(ARRAY['default'], :w)"),
                {"w": worker_id},
            )
        ).scalar_one()
        assert fetched == job_id
        await s.execute(
            text(
                "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '1 hour'"
                " WHERE id = :w"
            ),
            {"w": worker_id},
        )
    assert (await h.jobs("execute_run"))[0][2] == "doing"

    task = h.worker_app.tasks[RECOVER_STALLED]
    await task.func(timestamp=0)  # 주기 작업 본문
    assert (await h.jobs("execute_run"))[0][2] == "todo"

    await h.work()  # 되돌린 작업이 처음부터 다시 실행된다
    assert await h.status(run_id) == "waiting_approval"
    async with h.db.workspace_session(h.ws) as s:
        await s.execute(text("DELETE FROM procrastinate_workers WHERE id = :w"), {"w": worker_id})


# ---------------------------------------------------------------- interrupt 도달 전 종료


async def test_resume_after_checkpoint_stopped_before_await_approval(make_harness: Make) -> None:
    """agent 체크포인트까지 저장하고 interrupt에 닿기 전에 끊긴 run도 승인 뒤 끝까지 간다."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from orbit.runflow.graph import build_graph

    h = await make_harness(move_script())
    run_id = await h.start("점심을 2시로 옮겨줘")
    config = {"configurable": {"thread_id": str(run_id)}}
    initial = {
        "run_id": str(run_id),
        "workspace_id": str(h.ws),
        "step": 0,
        "tool_call_id": "",
        "next_node": "agent",
    }
    async with AsyncPostgresSaver.from_conn_string(h.deps.checkpoint_conninfo) as saver:
        graph = build_graph(h.deps, saver)
        await graph.ainvoke(initial, config, interrupt_before=["await_approval"])
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("await_approval",)
        assert not any(task.interrupts for task in snapshot.tasks)  # 아직 interrupt에 닿지 않았다
    assert await h.status(run_id) == "waiting_approval"
    approval_id = await h.approval_id(run_id)

    assert (await h.decide(approval_id, "approve")).status_code == 200  # 끊긴 사이에 결정이 났다
    await h.work()

    assert await h.status(run_id) == "succeeded"
    assert h.write_calls() == ["update_event"]
    assert len(h.world_diff().events_updated) == 1


# ---------------------------------------------------------------- lease 만료를 실제로 기다린다


async def test_unsettled_execution_is_retried_after_real_lease_expiry(make_harness: Make) -> None:
    script = [
        {
            "text": None,
            "tool_calls": [
                {
                    "name": "create_event",
                    "arguments": {
                        "title": "합성 회의",
                        "start": "2026-11-14T10:00:00+09:00",
                        "end": "2026-11-14T11:00:00+09:00",
                    },
                }
            ],
        },
        {"text": "일정을 만들었습니다."},
    ]
    h = await make_harness(script, lease_seconds=3.0)
    run_id, approval_id = await _until_waiting(h, "토요일 10시에 합성 회의 잡아줘")
    h.calendar.crash_after_create = True
    assert (await h.decide(approval_id, "approve")).status_code == 200
    await h.work()  # 외부 쓰기 직후 중단. lease는 아직 남아 있다.
    assert len(h.world_diff().events_created) == 1

    # 일반 재시도 대기(30초)가 지난 것으로 보고 다시 실행한다. lease가 살아 있어 대조하지 못한다.
    await h.make_jobs_due()
    await h.work()
    assert await h.status(run_id) == "running"
    assert await h.count("SELECT count(*) FROM execution WHERE status = 'pending'") == 1
    async with h.db.workspace_session(h.ws) as s:
        later = (
            await s.execute(
                text(
                    "SELECT count(*) FROM procrastinate_jobs WHERE task_name = 'resume_run'"
                    " AND status = 'todo' AND scheduled_at > now() + interval '2 seconds'"
                    " AND args->>'workspace_id' = :ws"
                ),
                {"ws": str(h.ws)},
            )
        ).scalar_one()
    assert later == 1  # lease 길이(3초) 이후로 재예약됐다

    await asyncio.sleep(3.5)  # lease가 실제로 만료되길 기다린다(expire_leases를 부르지 않는다)
    await h.make_jobs_due()  # 재예약 시각(lease + 1초)이 지났다
    await h.work()

    assert await h.status(run_id) == "succeeded"
    assert len(h.world_diff().events_created) == 1
    assert h.write_calls() == ["create_event"]  # 대조로 끝났고 재호출은 없다


# ---------------------------------------------------------------- 승인 만료 확인 작업


async def test_expiry_job_is_scheduled_with_approval_and_expires_it(make_harness: Make) -> None:
    h = await make_harness(move_script("만료되었습니다."))
    run_id, _ = await _until_waiting(h)

    async with h.db.workspace_session(h.ws) as s:
        row = (
            await s.execute(
                text(
                    "SELECT j.scheduled_at = a.expires_at, j.args FROM procrastinate_jobs j,"
                    " approval a WHERE j.task_name = 'expire_approvals' AND a.run_id = :r"
                    " AND j.args->>'run_id' = :rs"
                ),
                {"r": run_id, "rs": str(run_id)},
            )
        ).one()
    assert row[0] is True  # 승인 만료 시각에 예약됐다
    assert row[1] == {"run_id": str(run_id), "workspace_id": str(h.ws)}

    # 테스트 시계가 실제 시각보다 과거라 예약 작업이 이미 한 번 실행됐다(만료 전이라 변화 없음).
    # 만료 시각이 된 상황을 만들려고 시계를 옮기고 확인 작업을 다시 등록한다.
    h.clock.advance(timedelta(minutes=16))
    from orbit.runflow.jobs import EXPIRE_APPROVALS, defer_at

    async with h.db.workspace_session(h.ws) as s:
        await defer_at(
            s,
            h.worker_app,
            task_name=EXPIRE_APPROVALS,
            workspace_id=h.ws,
            run_id=run_id,
            at=h.clock.now(),
        )
    await h.work()  # 아무도 결정하지 않았다. 확인 작업이 만료를 반영해 run을 끝낸다.

    assert await h.status(run_id) == "succeeded"
    assert await h.count("SELECT count(*) FROM approval WHERE status = 'expired'") == 1
    assert h.write_calls() == []
    assert h.world_diff().is_empty


async def test_expiry_job_before_expiry_does_nothing(make_harness: Make) -> None:
    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)
    await h.make_jobs_due()
    await h.work()  # 만료 전에 도착한 확인 작업
    assert await h.status(run_id) == "waiting_approval"
    assert await h.count("SELECT count(*) FROM approval WHERE status = 'pending'") == 1
    assert (await h.decide(approval_id, "approve")).status_code == 200


# ---------------------------------------------------------------- 다른 run의 기록


async def test_tool_call_of_another_run_is_not_executed(make_harness: Make, owner_url: str) -> None:
    from orbit.records.runs import create_run

    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)
    conversation_id = uuid.UUID((await h.run_row(run_id))["conversation_id"])
    async with h.db.workspace_session(h.ws) as s:
        other = await create_run(
            s,
            workspace_id=h.ws,
            conversation_id=conversation_id,
            pattern="p0",
            config_fingerprint="fp",
            clock=h.clock,
            ids=h.deps.ids,
        )
    assert (await h.decide(approval_id, "approve")).status_code == 200
    engine = create_engine(owner_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(h.ws)}
            )
            changed = conn.execute(
                text("UPDATE tool_call SET run_id = :o WHERE run_id = :r"),
                {"o": other.id, "r": run_id},
            ).rowcount
            assert changed == 1
    finally:
        engine.dispose()
    await h.work()

    assert await h.status(run_id) == "failed"
    assert (await h.run_row(run_id))["error_kind"] == "run_mismatch"
    assert h.write_calls() == []
    assert await h.count("SELECT count(*) FROM execution") == 0


async def test_expiry_job_is_noop_for_decided_or_finished_runs(make_harness: Make) -> None:
    from orbit.runflow.graph import expire_if_due
    from orbit.runflow.jobs import EXPIRE_APPROVALS, defer_at

    h = await make_harness(move_script())
    run_id, approval_id = await _until_waiting(h)
    assert (await h.decide(approval_id, "approve")).status_code == 200
    h.clock.advance(timedelta(minutes=16))  # 승인 뒤 만료 시각이 지났다
    requests = len(h.llm.requests)

    # 이미 승인됐지만 아직 재개 전인 run: 만료로 바꾸지 않고 작업도 더 등록하지 않는다.
    assert await expire_if_due(h.deps, h.ws, run_id) is False
    assert await h.count("SELECT count(*) FROM approval WHERE status = 'approved'") == 1
    assert len(await h.jobs(RESUME_RUN)) == 1
    assert len(h.llm.requests) == requests

    await h.work()
    assert await h.status(run_id) == "succeeded"
    requests = len(h.llm.requests)
    jobs_before = len(await h.jobs())

    # 종료된 run에 만료 작업이 도착해도 그래프를 구동하지 않는다.
    async with h.db.workspace_session(h.ws) as s:
        await defer_at(
            s,
            h.worker_app,
            task_name=EXPIRE_APPROVALS,
            workspace_id=h.ws,
            run_id=run_id,
            at=h.clock.now(),
        )
    await h.work()
    assert len(h.llm.requests) == requests
    assert len(await h.jobs()) == jobs_before + 1  # 작업만 늘었고 resume_run은 없다
    assert len(await h.jobs(RESUME_RUN)) == 1
    assert (
        h.write_calls() == []
    )  # 재개 때 이미 만료 시각이 지나 실행기가 거절했다(만료 작업과 무관)
    assert await h.status(run_id) == "succeeded"


class _NoConnectionResolver:
    async def connectors_for(self, workspace_id: uuid.UUID) -> Any:
        raise NoConnection(f"workspace_id={workspace_id}")


async def test_run_fails_with_no_connection_when_workspace_has_no_active_connection(
    make_harness: Make,
) -> None:
    h = await make_harness(move_script())
    h.deps.resolver = _NoConnectionResolver()
    run_id = await h.start("점심을 2시로 옮겨줘")
    await h.work()
    assert await h.status(run_id) == "failed"
    assert (await h.run_row(run_id))["error_kind"] == "no_connection"
    assert h.write_calls() == []
