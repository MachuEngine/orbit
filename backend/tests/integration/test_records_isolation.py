"""새 테이블 6개의 워크스페이스 격리와 orbit_app 권한 (ADR-0004, ADR-0012 §1)."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from integration.records_support import LEASE, RECORD_TABLES, new_clock, seed_all_tables
from integration.support import WorkspaceFactory
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.approvals import decide_approval, get_approval
from orbit.records.deletion import delete_run
from orbit.records.errors import RecordNotFound
from orbit.records.executions import SqlExecutionStore
from orbit.records.runs import append_event, get_run, list_events, transition_run

# 갱신 가능한 테이블과 열(마이그레이션의 열 단위 GRANT와 같다).
UPDATABLE = {
    "run": "status = 'cancelled'",
    "approval": "status = 'expired'",
    "execution": "status = 'unknown'",
}
# UPDATE 권한이 없는 테이블에서 시도할 갱신문.
NOT_UPDATABLE = {
    "run_event": "type = 'x'",
    "run_message": "role = 'user'",
    "tool_call": "tool_name = 'x'",
}


async def _violation(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    """SAVEPOINT 안에서 실행해 실패 SQLSTATE를 돌려준다(바깥 트랜잭션은 유지)."""
    with pytest.raises(DBAPIError) as info:
        async with session.begin_nested():
            await session.execute(text(sql), params)
    return str(getattr(info.value.orig, "sqlstate", None))


async def test_workspace_b_sees_and_touches_nothing_of_a(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    a = await seed_all_tables(async_db, workspaces, clock, ids)
    b = await seed_all_tables(async_db, workspaces, clock, ids)

    async with async_db.workspace_session(b.workspace_id) as s:
        for table in RECORD_TABLES:
            # 조회: B 세션에서 A의 행은 0건, B 자신의 행만 보인다.
            seen = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": a.workspace_id},
                )
            ).scalar_one()
            assert seen == 0, table
            own = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608
                )
            ).scalar_one()
            assert own == 1, table

        # 갱신: 권한이 있는 테이블은 A의 행이 보이지 않아 0행이 바뀐다.
        for table, assignment in UPDATABLE.items():
            result = await s.execute(
                text(f"UPDATE {table} SET {assignment} WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": a.workspace_id},
            )
            assert result.rowcount == 0, table  # type: ignore[attr-defined]
        # 갱신 권한이 없는 테이블은 권한 오류(42501)다.
        for table, assignment in NOT_UPDATABLE.items():
            state = await _violation(
                s,
                f"UPDATE {table} SET {assignment} WHERE workspace_id = :ws",  # noqa: S608
                {"ws": a.workspace_id},
            )
            assert state == "42501", table

        # 삭제: run만 DELETE 권한이 있고, A의 행은 0행이 지워진다. 나머지는 권한 오류다.
        result = await s.execute(
            text("DELETE FROM run WHERE workspace_id = :ws"), {"ws": a.workspace_id}
        )
        assert result.rowcount == 0  # type: ignore[attr-defined]
        for table in RECORD_TABLES[1:]:
            state = await _violation(
                s,
                f"DELETE FROM {table} WHERE workspace_id = :ws",  # noqa: S608
                {"ws": a.workspace_id},
            )
            assert state == "42501", table

        # 삽입: B 세션에서 A의 workspace_id로 넣으면 RLS(WITH CHECK)가 막는다.
        state = await _violation(
            s,
            "INSERT INTO run_event (workspace_id, run_id, seq, type, payload, created_at)"
            " VALUES (:ws, :run, 99, 't', '{}'::jsonb, now())",
            {"ws": a.workspace_id, "run": a.run_id},
        )
        assert state == "42501"
        # B의 workspace_id로 A의 run을 가리키는 행은 복합 FK(워크스페이스 일치)가 막는다.
        state = await _violation(
            s,
            "INSERT INTO run_event (workspace_id, run_id, seq, type, payload, created_at)"
            " VALUES (:ws, :run, 99, 't', '{}'::jsonb, now())",
            {"ws": b.workspace_id, "run": a.run_id},
        )
        assert state == "23503"

    # A의 데이터는 그대로다.
    async with async_db.workspace_session(a.workspace_id) as s:
        for table in RECORD_TABLES:
            count = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert count == 1, table


async def test_record_functions_do_not_cross_workspaces(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    a = await seed_all_tables(async_db, workspaces, clock, ids)
    b = await seed_all_tables(async_db, workspaces, clock, ids)
    ws_b = b.workspace_id

    async with async_db.workspace_session(ws_b) as s:
        assert await get_run(s, workspace_id=ws_b, run_id=a.run_id) is None
        assert await get_approval(s, workspace_id=ws_b, approval_id=a.approval_id) is None
        assert await list_events(s, workspace_id=ws_b, run_id=a.run_id) == []
        with pytest.raises(RecordNotFound):
            await transition_run(
                s, workspace_id=ws_b, run_id=a.run_id, to_status="running", clock=clock
            )
        with pytest.raises(RecordNotFound):
            await append_event(
                s, workspace_id=ws_b, run_id=a.run_id, type="t", payload={}, clock=clock
            )
        assert not await decide_approval(
            s, workspace_id=ws_b, approval_id=a.approval_id, approve=True, clock=clock
        )
        assert await delete_run(s, workspace_id=ws_b, run_id=a.run_id) is False

    store_b = SqlExecutionStore(async_db, ws_b, clock=clock, ids=ids)
    assert await store_b.get(a.approval_id) is None
    assert not await store_b.claim(a.execution_id, b.owner, clock.now() + LEASE)
    assert not await store_b.finish(a.execution_id, a.owner, "succeeded", None, None)

    # 데이터가 없는 다른 워크스페이스 설정으로 읽으면 6개 테이블 모두 0건이다.
    async with async_db.workspace_session(uuid.uuid4()) as s:
        for table in RECORD_TABLES:
            count = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert count == 0, table

    # A 쪽은 변하지 않았다.
    async with async_db.workspace_session(a.workspace_id) as s:
        run = await get_run(s, workspace_id=a.workspace_id, run_id=a.run_id)
        approval = await get_approval(s, workspace_id=a.workspace_id, approval_id=a.approval_id)
    assert run is not None and run.status == "queued"
    assert approval is not None and approval.status == "approved"
    rec = await SqlExecutionStore(async_db, a.workspace_id, clock=clock, ids=ids).get(a.approval_id)
    assert rec is not None and rec.status == "pending"


async def test_immutable_columns_cannot_be_updated(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    """열 단위 GRANT: 승인된 인자 해시와 만료 시각은 갱신할 수 없다."""
    clock = new_clock()
    ids = Uuid7Generator(clock)
    a = await seed_all_tables(async_db, workspaces, clock, ids)
    async with async_db.workspace_session(a.workspace_id) as s:
        for sql in (
            "UPDATE approval SET args_hash = 'tampered'",
            "UPDATE approval SET expires_at = now() + interval '1 year'",
            "UPDATE execution SET approval_id = id",
            "UPDATE run SET workspace_id = id",
        ):
            assert await _violation(s, sql, {}) == "42501", sql
