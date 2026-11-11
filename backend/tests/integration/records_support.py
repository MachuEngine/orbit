"""기록 계층 통합 테스트 공통 도우미. 데이터는 모두 합성 값이다."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from integration.support import WorkspaceFactory
from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.approvals import create_tool_call_with_approval, decide_approval
from orbit.records.executions import SqlExecutionStore
from orbit.records.runs import append_event, append_message, create_run

LEASE_SECONDS = 300.0
RECORD_TABLES = ("run", "run_event", "run_message", "tool_call", "approval", "execution")


def new_clock() -> FixedClock:
    return FixedClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))


@dataclass(frozen=True)
class Seeded:
    workspace_id: uuid.UUID
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    tool_call_id: uuid.UUID
    approval_id: uuid.UUID
    execution_id: uuid.UUID
    owner: uuid.UUID


async def conversation_of(db: AsyncDatabase, workspace_id: uuid.UUID) -> uuid.UUID:
    async with db.workspace_session(workspace_id) as s:
        conv: uuid.UUID = (await s.execute(text("SELECT id FROM conversation"))).scalar_one()
        return conv


async def new_run(
    db: AsyncDatabase,
    workspaces: WorkspaceFactory,
    clock: FixedClock,
    ids: Uuid7Generator,
) -> tuple[uuid.UUID, uuid.UUID]:
    """대화 1개가 있는 새 워크스페이스에 queued run을 만들고 (workspace_id, run_id)를 돌려준다."""
    ws = workspaces.create(conversations=1)
    conv = await conversation_of(db, ws)
    async with db.workspace_session(ws) as s:
        run = await create_run(
            s,
            workspace_id=ws,
            conversation_id=conv,
            pattern="p0",
            config_fingerprint="fp-test",
            clock=clock,
            ids=ids,
        )
    return ws, run.id


async def seed_all_tables(
    db: AsyncDatabase,
    workspaces: WorkspaceFactory,
    clock: FixedClock,
    ids: Uuid7Generator,
    lease_seconds: float = LEASE_SECONDS,
) -> Seeded:
    """새 워크스페이스에 6개 테이블 모두 행이 있는 run 트리를 만든다(승인됨, 실행 기록 pending)."""
    ws = workspaces.create(conversations=1)
    conv = await conversation_of(db, ws)
    tool_call_id = ids.next()
    async with db.workspace_session(ws) as s:
        run = await create_run(
            s,
            workspace_id=ws,
            conversation_id=conv,
            pattern="p0",
            config_fingerprint="fp-test",
            clock=clock,
            ids=ids,
        )
        await append_event(
            s,
            workspace_id=ws,
            run_id=run.id,
            type="run_started",
            payload={"note": "synthetic"},
            clock=clock,
        )
        await append_message(
            s,
            workspace_id=ws,
            run_id=run.id,
            role="user",
            content={"text": "synthetic message"},
            clock=clock,
        )
        _, approval, _ = await create_tool_call_with_approval(
            s,
            workspace_id=ws,
            run_id=run.id,
            tool_call_id=tool_call_id,
            tool_name="create_event",
            args={"title": "synthetic"},
            args_hash="h-1",
            provenance={"attendees": {}},
            clock=clock,
            ids=ids,
        )
        assert await decide_approval(
            s, workspace_id=ws, approval_id=approval.id, approve=True, clock=clock
        )
    owner = ids.next()
    store = SqlExecutionStore(db, ws, clock=clock, ids=ids)
    record, created = await store.begin(approval.id, owner, lease_seconds)
    assert created
    return Seeded(ws, conv, run.id, tool_call_id, approval.id, record.id, owner)
