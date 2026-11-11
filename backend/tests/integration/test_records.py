"""run·이벤트·승인·실행 기록 통합 테스트 (ADR-0012 4A). 실제 PostgreSQL이 필요하다."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from integration.records_support import new_clock, new_run, seed_all_tables
from integration.support import WorkspaceFactory
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.approvals import (
    create_tool_call_with_approval,
    decide_approval,
    expire_approval,
    expire_due_approvals,
    get_approval,
)
from orbit.records.errors import (
    ApprovalNotApproved,
    IdempotencyConflict,
    InvalidTransition,
    RecordNotFound,
)
from orbit.records.executions import SqlExecutionStore
from orbit.records.runs import (
    append_event,
    append_message,
    get_run,
    list_events,
    list_messages,
    transition_run,
)

CONCURRENCY = 8


# run 상태 전이 ----------------------------------------------------------------


async def test_run_transitions_allowed_and_rejected(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)

    async def go(to: str, error_kind: str | None = None) -> str:
        async with async_db.workspace_session(ws) as s:
            rec = await transition_run(
                s,
                workspace_id=ws,
                run_id=run_id,
                to_status=to,
                clock=clock,
                error_kind=error_kind,
            )
            return rec.status

    # queued는 바로 succeeded로 갈 수 없다.
    with pytest.raises(InvalidTransition):
        await go("succeeded")
    assert await go("running") == "running"
    assert await go("waiting_approval") == "waiting_approval"
    with pytest.raises(InvalidTransition):
        await go("succeeded")  # waiting_approval -> succeeded 불가
    assert await go("running") == "running"
    clock.advance(timedelta(seconds=5))
    assert await go("failed", "tool_error") == "failed"

    async with async_db.workspace_session(ws) as s:
        run = await get_run(s, workspace_id=ws, run_id=run_id)
    assert run is not None
    assert (run.status, run.error_kind, run.updated_at) == ("failed", "tool_error", clock.now())

    # 종료 상태에서는 나갈 수 없다.
    for target in ("running", "cancelled", "failed"):
        with pytest.raises(InvalidTransition):
            await go(target, "x" if target == "failed" else None)


async def test_run_transition_argument_and_missing_rules(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    async with async_db.workspace_session(ws) as s:
        with pytest.raises(ValueError):
            await transition_run(s, workspace_id=ws, run_id=run_id, to_status="failed", clock=clock)
        with pytest.raises(ValueError):
            await transition_run(
                s,
                workspace_id=ws,
                run_id=run_id,
                to_status="running",
                clock=clock,
                error_kind="x",
            )
        with pytest.raises(InvalidTransition):
            await transition_run(s, workspace_id=ws, run_id=run_id, to_status="queued", clock=clock)
        with pytest.raises(RecordNotFound):
            await transition_run(
                s, workspace_id=ws, run_id=ids.next(), to_status="running", clock=clock
            )


async def test_concurrent_transitions_only_one_wins(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)

    async def attempt(to: str) -> bool:
        try:
            async with async_db.workspace_session(ws) as s:
                await transition_run(
                    s,
                    workspace_id=ws,
                    run_id=run_id,
                    to_status=to,
                    clock=clock,
                    error_kind="e" if to == "failed" else None,
                )
            return True
        except InvalidTransition:
            return False

    results = await asyncio.gather(attempt("running"), attempt("cancelled"), attempt("failed"))
    # queued -> {running, cancelled, failed}는 모두 허용되지만, 첫 전이가 끝난 뒤에는
    # running -> cancelled/failed도 허용된다. 어떤 순서여도 마지막 상태는 허용된 경로여야 한다.
    async with async_db.workspace_session(ws) as s:
        run = await get_run(s, workspace_id=ws, run_id=run_id)
    assert run is not None
    assert run.status in {"running", "cancelled", "failed"}
    assert any(results)


# run_event / run_message --------------------------------------------------------


async def test_events_seq_monotonic_and_after_seq(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    other_ws, other_run = await new_run(async_db, workspaces, clock, ids)

    async with async_db.workspace_session(ws) as s:
        seqs = [
            (
                await append_event(
                    s,
                    workspace_id=ws,
                    run_id=run_id,
                    type="t",
                    payload={"n": i},
                    clock=clock,
                )
            ).seq
            for i in range(3)
        ]
    async with async_db.workspace_session(other_ws) as s:
        first_of_other = await append_event(
            s, workspace_id=other_ws, run_id=other_run, type="t", payload={}, clock=clock
        )
    assert seqs == [1, 2, 3]
    assert first_of_other.seq == 1  # run마다 1부터

    async with async_db.workspace_session(ws) as s:
        after = await list_events(s, workspace_id=ws, run_id=run_id, after_seq=1)
        assert [(e.seq, e.payload) for e in after] == [(2, {"n": 1}), (3, {"n": 2})]
        assert await list_events(s, workspace_id=ws, run_id=run_id, after_seq=3) == []
        limited = await list_events(s, workspace_id=ws, run_id=run_id, limit=2)
        assert [e.seq for e in limited] == [1, 2]
        with pytest.raises(RecordNotFound):
            await append_event(
                s, workspace_id=ws, run_id=ids.next(), type="t", payload={}, clock=clock
            )


async def test_concurrent_event_and_message_append_have_gapless_seq(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)

    async def add_event(i: int) -> int:
        async with async_db.workspace_session(ws) as s:
            ev = await append_event(
                s, workspace_id=ws, run_id=run_id, type="t", payload={"i": i}, clock=clock
            )
            return ev.seq

    async def add_message(i: int) -> int:
        async with async_db.workspace_session(ws) as s:
            msg = await append_message(
                s,
                workspace_id=ws,
                run_id=run_id,
                role="assistant",
                content={"i": i},
                clock=clock,
            )
            return msg.seq

    event_seqs, message_seqs = await asyncio.gather(
        asyncio.gather(*(add_event(i) for i in range(CONCURRENCY))),
        asyncio.gather(*(add_message(i) for i in range(CONCURRENCY))),
    )
    assert sorted(event_seqs) == list(range(1, CONCURRENCY + 1))
    assert sorted(message_seqs) == list(range(1, CONCURRENCY + 1))

    async with async_db.workspace_session(ws) as s:
        events = await list_events(s, workspace_id=ws, run_id=run_id)
        messages = await list_messages(s, workspace_id=ws, run_id=run_id, after_seq=2)
    assert [e.seq for e in events] == list(range(1, CONCURRENCY + 1))
    assert [m.seq for m in messages] == list(range(3, CONCURRENCY + 1))


async def test_message_role_is_checked(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    async with async_db.workspace_session(ws) as s:
        with pytest.raises(ValueError):
            await append_message(
                s, workspace_id=ws, run_id=run_id, role="admin", content={}, clock=clock
            )


# tool_call / approval ------------------------------------------------------------


async def _create_call(
    db: AsyncDatabase,
    ws: uuid.UUID,
    run_id: uuid.UUID,
    call_id: uuid.UUID,
    ids: Uuid7Generator,
    clock: Any,
    args_hash: str = "h-1",
) -> tuple[uuid.UUID, uuid.UUID, bool]:
    async with db.workspace_session(ws) as s:
        call, approval, created = await create_tool_call_with_approval(
            s,
            workspace_id=ws,
            run_id=run_id,
            tool_call_id=call_id,
            tool_name="create_event",
            args={"title": "synthetic"},
            args_hash=args_hash,
            provenance={"attendees": {}},
            clock=clock,
            ids=ids,
        )
    return call.id, approval.id, created


async def test_tool_call_and_approval_are_idempotent(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    call_id = ids.next()

    first = await _create_call(async_db, ws, run_id, call_id, ids, clock)
    second = await _create_call(async_db, ws, run_id, call_id, ids, clock)
    assert first[2] is True
    assert second == (first[0], first[1], False)

    # 같은 키에 다른 인자 해시는 거부한다.
    with pytest.raises(IdempotencyConflict):
        await _create_call(async_db, ws, run_id, call_id, ids, clock, args_hash="h-2")

    async with async_db.workspace_session(ws) as s:
        approval = await get_approval(s, workspace_id=ws, approval_id=first[1])
        counts = (
            await s.execute(
                text("SELECT (SELECT count(*) FROM tool_call), (SELECT count(*) FROM approval)")
            )
        ).one()
    assert approval is not None
    assert (approval.status, approval.args_hash, approval.expires_at) == (
        "pending",
        "h-1",
        clock.now() + timedelta(minutes=15),
    )
    assert tuple(counts) == (1, 1)


async def test_concurrent_create_same_key_makes_one_approval(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    call_id = ids.next()
    results = await asyncio.gather(
        *(_create_call(async_db, ws, run_id, call_id, ids, clock) for _ in range(CONCURRENCY))
    )
    assert len({(r[0], r[1]) for r in results}) == 1
    assert sum(r[2] for r in results) == 1


async def test_concurrent_decisions_only_one_succeeds(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    _, approval_id, _ = await _create_call(async_db, ws, run_id, ids.next(), ids, clock)

    async def decide(approve: bool) -> bool:
        async with async_db.workspace_session(ws) as s:
            return await decide_approval(
                s, workspace_id=ws, approval_id=approval_id, approve=approve, clock=clock
            )

    results = await asyncio.gather(*(decide(i % 2 == 0) for i in range(CONCURRENCY)))
    assert sum(results) == 1
    async with async_db.workspace_session(ws) as s:
        approval = await get_approval(s, workspace_id=ws, approval_id=approval_id)
    assert approval is not None
    assert approval.status in {"approved", "rejected"}
    assert approval.decided_at == clock.now()
    assert await decide(True) is False  # 이미 결정된 승인


async def test_decision_after_expiry_fails_and_expire_marks_expired(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    _, late_id, _ = await _create_call(async_db, ws, run_id, ids.next(), ids, clock)
    _, ok_id, _ = await _create_call(async_db, ws, run_id, ids.next(), ids, clock)

    async with async_db.workspace_session(ws) as s:
        # 만료 직전(1초 전)에는 결정된다.
        clock.advance(timedelta(minutes=15) - timedelta(seconds=1))
        assert await decide_approval(
            s, workspace_id=ws, approval_id=ok_id, approve=True, clock=clock
        )
        # 만료 시각 정각부터는 결정할 수 없다(expires_at > now).
        clock.advance(timedelta(seconds=1))
        assert not await decide_approval(
            s, workspace_id=ws, approval_id=late_id, approve=True, clock=clock
        )
        # 아직 만료 처리 전이라 pending이다. 만료 처리는 한 번만 성공한다.
        late = await get_approval(s, workspace_id=ws, approval_id=late_id)
        assert late is not None and late.status == "pending"
        assert await expire_approval(s, workspace_id=ws, approval_id=late_id, clock=clock)
        assert not await expire_approval(s, workspace_id=ws, approval_id=late_id, clock=clock)
        late = await get_approval(s, workspace_id=ws, approval_id=late_id)
        assert late is not None and (late.status, late.decided_at) == ("expired", clock.now())
        assert not await decide_approval(
            s, workspace_id=ws, approval_id=late_id, approve=True, clock=clock
        )
        # 이미 승인된 것은 만료 처리 대상이 아니다.
        assert not await expire_approval(s, workspace_id=ws, approval_id=ok_id, clock=clock)
        assert await expire_due_approvals(s, workspace_id=ws, clock=clock) == []


async def test_expire_due_approvals_bulk(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    created = [(await _create_call(async_db, ws, run_id, ids.next(), ids, clock))[1] for _ in "ab"]
    clock.advance(timedelta(minutes=16))
    async with async_db.workspace_session(ws) as s:
        expired = await expire_due_approvals(s, workspace_id=ws, clock=clock)
    assert sorted(expired) == sorted(created)


# ExecutionStore -------------------------------------------------------------------


async def test_execution_store_begin_is_single_row_under_concurrency(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    _, approval_id, _ = await _create_call(async_db, ws, run_id, ids.next(), ids, clock)
    async with async_db.workspace_session(ws) as s:
        assert await decide_approval(
            s, workspace_id=ws, approval_id=approval_id, approve=True, clock=clock
        )
    store = SqlExecutionStore(async_db, ws, clock=clock, ids=ids)

    results = await asyncio.gather(*(store.begin(approval_id) for _ in range(CONCURRENCY)))
    assert sum(created for _, created in results) == 1
    assert len({rec.id for rec, _ in results}) == 1
    assert all(rec.status == "pending" for rec, _ in results)
    async with async_db.workspace_session(ws) as s:
        count = (await s.execute(text("SELECT count(*) FROM execution"))).scalar_one()
    assert count == 1


async def test_execution_store_requires_approved_approval(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    _, approval_id, _ = await _create_call(async_db, ws, run_id, ids.next(), ids, clock)
    store = SqlExecutionStore(async_db, ws, clock=clock, ids=ids)

    with pytest.raises(ApprovalNotApproved):  # pending
        await store.begin(approval_id)
    async with async_db.workspace_session(ws) as s:
        assert await decide_approval(
            s, workspace_id=ws, approval_id=approval_id, approve=False, clock=clock
        )
    with pytest.raises(ApprovalNotApproved):  # rejected
        await store.begin(approval_id)
    with pytest.raises(ApprovalNotApproved):  # 없는 승인
        await store.begin(ids.next())
    assert await store.get(approval_id) is None


async def test_execution_store_lifecycle(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    seeded = await seed_all_tables(async_db, workspaces, clock, ids)
    ws = seeded.workspace_id
    store = SqlExecutionStore(async_db, ws, clock=clock, ids=ids)

    rec = await store.get(seeded.approval_id)
    assert rec is not None and rec.id == seeded.execution_id
    assert (rec.status, rec.external_ref, rec.error_kind) == ("pending", None, None)
    again, created = await store.begin(seeded.approval_id)
    assert (again, created) == (rec, False)

    await store.record_ref(rec.id, {"draft_id": "d-1"})
    got = await store.get(seeded.approval_id)
    assert got is not None and got.external_ref == {"draft_id": "d-1"}

    # 결과 불명 -> 대조 후 성공. external_ref=None이면 기존 값을 유지한다.
    await store.finish(rec.id, "unknown", None, None)
    await store.record_ref(rec.id, {"draft_id": "d-1", "thread_id": "t-1"})
    await store.finish(rec.id, "succeeded", None, None)
    done = await store.get(seeded.approval_id)
    assert done is not None
    assert (done.status, done.external_ref, done.error_kind) == (
        "succeeded",
        {"draft_id": "d-1", "thread_id": "t-1"},
        None,
    )

    # 같은 최종 상태의 재진입은 무해하고, 다른 상태로 바꾸거나 ref를 고치는 것은 거부한다.
    await store.finish(rec.id, "succeeded", None, None)
    with pytest.raises(InvalidTransition):
        await store.finish(rec.id, "failed", None, "NotFound")
    with pytest.raises(InvalidTransition):
        await store.record_ref(rec.id, {"draft_id": "other"})
    with pytest.raises(RecordNotFound):
        await store.finish(ids.next(), "failed", None, "NotFound")
    with pytest.raises(ValueError):
        await store.finish(rec.id, "pending", None, None)
    final = await store.get(seeded.approval_id)
    assert final == done


async def test_execution_failure_kind_is_recorded(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    seeded = await seed_all_tables(async_db, workspaces, clock, ids)
    store = SqlExecutionStore(async_db, seeded.workspace_id, clock=clock, ids=ids)
    await store.finish(seeded.execution_id, "failed", None, "PreconditionFailed")
    rec = await store.get(seeded.approval_id)
    assert rec is not None and (rec.status, rec.error_kind) == ("failed", "PreconditionFailed")
