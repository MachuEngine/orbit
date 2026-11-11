"""Procrastinate 작업 등록·멈춘 작업 복구·run 삭제 전파 (ADR-0005, ADR-0006, ADR-0012)."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import procrastinate
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from integration.records_support import RECORD_TABLES, new_clock, new_run, seed_all_tables
from integration.support import WorkspaceFactory
from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.approvals import create_tool_call_with_approval, decide_approval
from orbit.records.deletion import CHECKPOINT_TABLES, delete_run
from orbit.records.errors import ApprovalNotApproved, InvalidJobArgs, RunHasActiveExecution
from orbit.records.executions import SqlExecutionStore
from orbit.records.jobs import (
    defer_job,
    make_procrastinate_app,
    recover_stalled_jobs,
    validate_job_args,
)
from orbit.records.runs import create_run, get_run

TASK = "execute_run"


@pytest.fixture
def app(app_url: str) -> procrastinate.App:
    return make_procrastinate_app(app_url)


@pytest.fixture
async def queue(async_db: AsyncDatabase) -> AsyncIterator[str]:
    """테스트마다 고유 큐 이름을 쓰고, 끝나면 그 큐의 작업을 지운다."""
    name = f"test-{uuid.uuid4().hex}"
    yield name
    async with async_db.workspace_session(uuid.uuid4()) as s:
        await s.execute(text("DELETE FROM procrastinate_jobs WHERE queue_name = :q"), {"q": name})


async def _jobs(db: AsyncDatabase, queue: str) -> list[tuple[str, dict[str, str], str]]:
    async with db.workspace_session(uuid.uuid4()) as s:
        rows = await s.execute(
            text(
                "SELECT task_name, args, status::text FROM procrastinate_jobs"
                " WHERE queue_name = :q ORDER BY id"
            ),
            {"q": queue},
        )
        return [(r[0], r[1], r[2]) for r in rows]


def _args(ws: uuid.UUID, run_id: uuid.UUID) -> dict[str, str]:
    return {"run_id": str(run_id), "workspace_id": str(ws)}


async def _create_run(
    s: AsyncSession, ws: uuid.UUID, conv: uuid.UUID, clock: object, ids: Uuid7Generator
) -> uuid.UUID:
    run = await create_run(
        s,
        workspace_id=ws,
        conversation_id=conv,
        pattern="p0",
        config_fingerprint="fp",
        clock=clock,  # type: ignore[arg-type]
        ids=ids,
    )
    return run.id


# 작업 인자 검사 ------------------------------------------------------------------


def test_job_args_accept_only_run_id_and_workspace_id_strings() -> None:
    rid, wid = str(uuid.uuid4()), str(uuid.uuid4())
    good = {"run_id": rid, "workspace_id": wid}
    assert validate_job_args(good) == good
    for bad in (
        {},
        {"run_id": rid},  # workspace_id 필수
        {"workspace_id": wid},  # run_id 필수
        {**good, "body": "synthetic body"},
        {**good, "to": "someone@example.invalid"},
        {"run_id": uuid.UUID(rid), "workspace_id": wid},  # UUID 객체가 아니라 문자열만
        {"run_id": rid, "workspace_id": uuid.UUID(wid)},
        {"run_id": "not-a-uuid", "workspace_id": wid},
        {"run_id": rid, "workspace_id": "not-a-uuid"},
        {"run_id": 123, "workspace_id": wid},
        {"other": rid, "workspace_id": wid},
    ):
        with pytest.raises(InvalidJobArgs):
            validate_job_args(bad)


def test_job_args_error_message_has_no_values() -> None:
    with pytest.raises(InvalidJobArgs) as info:
        validate_job_args(
            {
                "run_id": str(uuid.uuid4()),
                "workspace_id": str(uuid.uuid4()),
                "body": "secret-synthetic-text",
            }
        )
    assert "secret-synthetic-text" not in str(info.value)
    assert "body" in str(info.value)


async def test_defer_job_rejects_bad_args_without_registering(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    ws = workspaces.create()
    async with async_db.workspace_session(ws) as s:
        with pytest.raises(InvalidJobArgs):
            await defer_job(
                s, app, task_name=TASK, args={**_args(ws, uuid.uuid4()), "x": "y"}, queue=queue
            )
    assert await _jobs(async_db, queue) == []


# 같은 트랜잭션 등록 ------------------------------------------------------------------


async def test_defer_in_same_transaction_commits_both(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws = workspaces.create(conversations=1)
    async with async_db.workspace_session(ws) as s:
        conv = (await s.execute(text("SELECT id FROM conversation"))).scalar_one()
        run_id = await _create_run(s, ws, conv, clock, ids)
        job_id = await defer_job(s, app, task_name=TASK, args=_args(ws, run_id), queue=queue)
    assert isinstance(job_id, int)
    assert await _jobs(async_db, queue) == [(TASK, _args(ws, run_id), "todo")]
    async with async_db.workspace_session(ws) as s:
        assert await get_run(s, workspace_id=ws, run_id=run_id) is not None


async def test_defer_rolls_back_together_with_domain_row(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws = workspaces.create(conversations=1)
    run_id: uuid.UUID | None = None
    with pytest.raises(RuntimeError, match="의도한 롤백"):
        async with async_db.workspace_session(ws) as s:
            conv = (await s.execute(text("SELECT id FROM conversation"))).scalar_one()
            run_id = await _create_run(s, ws, conv, clock, ids)
            job_id = await defer_job(s, app, task_name=TASK, args=_args(ws, run_id), queue=queue)
            assert job_id is not None  # 등록 자체는 성공했다가
            raise RuntimeError("의도한 롤백")  # 커밋 전에 실패한다
    assert run_id is not None
    assert await _jobs(async_db, queue) == []
    async with async_db.workspace_session(ws) as s:
        assert await get_run(s, workspace_id=ws, run_id=run_id) is None


async def test_queueing_lock_duplicate_keeps_outer_transaction(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws = workspaces.create(conversations=1)
    lock = f"run:{uuid.uuid4()}"
    async with async_db.workspace_session(ws) as s:
        conv = (await s.execute(text("SELECT id FROM conversation"))).scalar_one()
        first_run = await _create_run(s, ws, conv, clock, ids)
        first = await defer_job(
            s,
            app,
            task_name=TASK,
            args=_args(ws, first_run),
            queue=queue,
            queueing_lock=lock,
        )
    assert first is not None

    async with async_db.workspace_session(ws) as s:
        conv = (await s.execute(text("SELECT id FROM conversation"))).scalar_one()
        second_run = await _create_run(s, ws, conv, clock, ids)
        second = await defer_job(
            s,
            app,
            task_name=TASK,
            args=_args(ws, second_run),
            queue=queue,
            queueing_lock=lock,
        )
        assert second is None  # AlreadyEnqueued는 None으로 돌려주고
        # SAVEPOINT 덕분에 같은 트랜잭션에서 계속 쓸 수 있다.
        assert await get_run(s, workspace_id=ws, run_id=second_run) is not None

    # 도메인 행은 커밋됐고, 작업은 처음 것 하나뿐이다.
    async with async_db.workspace_session(ws) as s:
        assert await get_run(s, workspace_id=ws, run_id=second_run) is not None
    assert [j[1] for j in await _jobs(async_db, queue)] == [_args(ws, first_run)]


# 멈춘 작업 복구 ------------------------------------------------------------------------


async def test_recover_stalled_jobs_returns_doing_job_to_todo(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    ws = workspaces.create()
    run_id = str(uuid.uuid4())
    async with async_db.workspace_session(ws) as s:
        job_id = await defer_job(
            s, app, task_name=TASK, args={"run_id": run_id, "workspace_id": str(ws)}, queue=queue
        )
    assert job_id is not None

    # 워커가 가져간 뒤 죽은 상황: 작업은 doing, 워커 heartbeat는 1시간 전이다.
    async with async_db.workspace_session(ws) as s:
        worker_id = (
            await s.execute(text("SELECT procrastinate_register_worker_v1()"))
        ).scalar_one()
        fetched = (
            await s.execute(
                text("SELECT id FROM procrastinate_fetch_job_v2(ARRAY[CAST(:q AS varchar)], :w)"),
                {"q": queue, "w": worker_id},
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
    assert (await _jobs(async_db, queue))[0][2] == "doing"

    try:
        # heartbeat가 최근이면(기준을 2시간으로) 멈춘 것으로 보지 않는다.
        assert await recover_stalled_jobs(app, seconds_since_heartbeat=7200, queue=queue) == []
        assert (await _jobs(async_db, queue))[0][2] == "doing"

        recovered = await recover_stalled_jobs(app, seconds_since_heartbeat=1800, queue=queue)
        assert recovered == [job_id]
        assert (await _jobs(async_db, queue))[0][2] == "todo"
        # 이미 되돌렸으므로 다시 불러도 대상이 없다.
        assert await recover_stalled_jobs(app, seconds_since_heartbeat=1800, queue=queue) == []
    finally:
        async with async_db.workspace_session(uuid.uuid4()) as s:
            await s.execute(
                text("DELETE FROM procrastinate_jobs WHERE queue_name = :q"), {"q": queue}
            )
            await s.execute(
                text("DELETE FROM procrastinate_workers WHERE id = :w"), {"w": worker_id}
            )


# run 삭제 전파 ----------------------------------------------------------------------------


_CHECKPOINT_INSERTS = {
    "checkpoints": (
        "INSERT INTO public.checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint)"
        " VALUES (:t, '', 'c1', CAST('{}' AS jsonb))"
    ),
    "checkpoint_blobs": (
        "INSERT INTO public.checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type)"
        " VALUES (:t, '', 'ch', '1', 'empty')"
    ),
    "checkpoint_writes": (
        "INSERT INTO public.checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id,"
        " idx, channel, blob) VALUES (:t, '', 'c1', 'task', 0, 'ch', '\\x00')"
    ),
}


async def _checkpoint_rows(db: AsyncDatabase, thread_id: uuid.UUID) -> int:
    async with db.workspace_session(uuid.uuid4()) as s:
        total = 0
        for table in CHECKPOINT_TABLES:
            total += (
                await s.execute(
                    text(f"SELECT count(*) FROM public.{table} WHERE thread_id = :t"),  # noqa: S608
                    {"t": str(thread_id)},
                )
            ).scalar_one()
        return total


async def test_delete_run_removes_children_and_checkpoints(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    """마이그레이션 0003이 만든 실제 LangGraph 체크포인트 테이블 3개에서 thread_id 행을 지운다."""
    clock = new_clock()
    ids = Uuid7Generator(clock)
    a = await seed_all_tables(async_db, workspaces, clock, ids)
    keep = await seed_all_tables(async_db, workspaces, clock, ids)
    for seeded in (a, keep):  # 끝나지 않은 실행이 있으면 삭제가 거부되므로 먼저 끝낸다
        store = SqlExecutionStore(async_db, seeded.workspace_id, clock=clock, ids=ids)
        assert await store.finish(seeded.execution_id, seeded.owner, "succeeded", None, None)
    for run_id in (a.run_id, keep.run_id):
        async with async_db.workspace_session(a.workspace_id) as s:
            for table in CHECKPOINT_TABLES:
                await s.execute(text(_CHECKPOINT_INSERTS[table]), {"t": str(run_id)})
    assert await _checkpoint_rows(async_db, a.run_id) == len(CHECKPOINT_TABLES)

    # 다른 워크스페이스에서 지우려 하면 아무것도 지워지지 않는다(체크포인트 포함).
    async with async_db.workspace_session(keep.workspace_id) as s:
        assert await delete_run(s, workspace_id=keep.workspace_id, run_id=a.run_id) is False
    assert await _checkpoint_rows(async_db, a.run_id) == len(CHECKPOINT_TABLES)

    async with async_db.workspace_session(a.workspace_id) as s:
        assert await delete_run(s, workspace_id=a.workspace_id, run_id=a.run_id) is True
    async with async_db.workspace_session(a.workspace_id) as s:
        for table in RECORD_TABLES:
            count = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert count == 0, table
        assert await delete_run(s, workspace_id=a.workspace_id, run_id=a.run_id) is False
    assert await _checkpoint_rows(async_db, a.run_id) == 0

    # 다른 run의 행과 체크포인트는 남는다.
    async with async_db.workspace_session(keep.workspace_id) as s:
        for table in RECORD_TABLES:
            count = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert count == 1, table
    assert await _checkpoint_rows(async_db, keep.run_id) == len(CHECKPOINT_TABLES)
    # 남겨 둔 run의 체크포인트는 워크스페이스 정리(CASCADE)로 지워지지 않으므로 직접 지운다.
    async with async_db.workspace_session(keep.workspace_id) as s:
        for table in CHECKPOINT_TABLES:
            await s.execute(
                text(f"DELETE FROM public.{table} WHERE thread_id = :t"),  # noqa: S608
                {"t": str(keep.run_id)},
            )


async def test_delete_run_without_checkpoint_tables(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    """체크포인트 행이 없는 run도 오류 없이 삭제된다."""
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    async with async_db.workspace_session(ws) as s:
        assert await delete_run(s, workspace_id=ws, run_id=run_id) is True
        assert await get_run(s, workspace_id=ws, run_id=run_id) is None


async def test_delete_run_refuses_active_execution_and_removes_todo_jobs(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    a = await seed_all_tables(async_db, workspaces, clock, ids)  # execution은 pending
    ws = a.workspace_id
    other_run = str(uuid.uuid4())
    async with async_db.workspace_session(ws) as s:
        await defer_job(s, app, task_name=TASK, args=_args(ws, a.run_id), queue=queue)
        await defer_job(
            s, app, task_name=TASK, args={"run_id": other_run, "workspace_id": str(ws)}, queue=queue
        )

    # pending 실행이 있으면 거부하고 아무것도 지우지 않는다.
    with pytest.raises(RunHasActiveExecution):
        async with async_db.workspace_session(ws) as s:
            await delete_run(s, workspace_id=ws, run_id=a.run_id)
    async with async_db.workspace_session(ws) as s:
        assert await get_run(s, workspace_id=ws, run_id=a.run_id) is not None
    assert len(await _jobs(async_db, queue)) == 2

    # unknown도 거부한다.
    store = SqlExecutionStore(async_db, ws, clock=clock, ids=ids)
    assert await store.finish(a.execution_id, a.owner, "unknown", None, None)
    with pytest.raises(RunHasActiveExecution):
        async with async_db.workspace_session(ws) as s:
            await delete_run(s, workspace_id=ws, run_id=a.run_id)

    # 끝난 실행이면 지워지고, 그 run의 todo 작업만 같은 트랜잭션에서 사라진다.
    assert await store.claim(a.execution_id, a.owner, 60)
    assert await store.finish(a.execution_id, a.owner, "succeeded", None, None)
    async with async_db.workspace_session(ws) as s:
        assert await delete_run(s, workspace_id=ws, run_id=a.run_id) is True
    remaining = await _jobs(async_db, queue)
    assert [j[1]["run_id"] for j in remaining] == [other_run]


async def test_delete_run_keeps_doing_job(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    async with async_db.workspace_session(ws) as s:
        await defer_job(s, app, task_name=TASK, args=_args(ws, run_id), queue=queue)
        worker_id = (
            await s.execute(text("SELECT procrastinate_register_worker_v1()"))
        ).scalar_one()
        await s.execute(
            text("SELECT id FROM procrastinate_fetch_job_v2(ARRAY[CAST(:q AS varchar)], :w)"),
            {"q": queue, "w": worker_id},
        )
    try:
        async with async_db.workspace_session(ws) as s:
            assert await delete_run(s, workspace_id=ws, run_id=run_id) is True
        assert [j[2] for j in await _jobs(async_db, queue)] == ["doing"]
    finally:
        async with async_db.workspace_session(uuid.uuid4()) as s:
            await s.execute(
                text("DELETE FROM procrastinate_jobs WHERE queue_name = :q"), {"q": queue}
            )
            await s.execute(
                text("DELETE FROM procrastinate_workers WHERE id = :w"), {"w": worker_id}
            )


async def _approved_run(
    db: AsyncDatabase, workspaces: WorkspaceFactory, clock: FixedClock, ids: Uuid7Generator
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """실행 기록이 아직 없는 승인 완료 run을 만들고 (workspace_id, run_id, approval_id)를 준다."""
    ws, run_id = await new_run(db, workspaces, clock, ids)
    async with db.workspace_session(ws) as s:
        _, approval, _ = await create_tool_call_with_approval(
            s,
            workspace_id=ws,
            run_id=run_id,
            tool_call_id=ids.next(),
            tool_name="create_event",
            args={"title": "synthetic"},
            args_hash="h-1",
            provenance={},
            clock=clock,
            ids=ids,
        )
        assert await decide_approval(
            s, workspace_id=ws, approval_id=approval.id, approve=True, clock=clock
        )
    return ws, run_id, approval.id


async def _count(db: AsyncDatabase, ws: uuid.UUID, table: str) -> int:
    async with db.workspace_session(ws) as s:
        return int(
            (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
        )


async def test_begin_waits_for_delete_run_and_leaves_no_orphan(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    """삭제 트랜잭션이 run을 잠그고 있는 동안 begin은 기다리고, 삭제가 커밋되면 실패한다."""
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id, approval_id = await _approved_run(async_db, workspaces, clock, ids)
    store = SqlExecutionStore(async_db, ws, clock=clock, ids=ids)

    async with async_db.workspace_session(ws) as s:
        assert await delete_run(s, workspace_id=ws, run_id=run_id) is True  # 아직 커밋 전
        task = asyncio.ensure_future(store.begin(approval_id, ids.next(), 60))
        await asyncio.sleep(0.5)
        assert not task.done()  # run 잠금 때문에 대기 중
    with pytest.raises(ApprovalNotApproved):
        await task
    assert await _count(async_db, ws, "execution") == 0
    assert await _count(async_db, ws, "run") == 0


async def test_delete_run_waits_for_begin_lock_and_is_refused(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    """begin이 run을 잠그고 있는 동안 삭제는 기다리고, 실행 행이 생긴 뒤에는 거부된다."""
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id, approval_id = await _approved_run(async_db, workspaces, clock, ids)

    async with async_db.workspace_session(ws) as holder:
        # begin과 같은 잠금을 쥔 채로(커밋 전) 삭제를 시작한다.
        await holder.execute(text("SELECT 1 FROM run WHERE id = :id FOR KEY SHARE"), {"id": run_id})

        async def delete() -> bool:
            async with async_db.workspace_session(ws) as s:
                return await delete_run(s, workspace_id=ws, run_id=run_id)

        task = asyncio.ensure_future(delete())
        await asyncio.sleep(0.5)
        assert not task.done()
    assert await task is True  # 잠금이 풀리고 실행 행이 없었으므로 삭제된다
    assert await _count(async_db, ws, "run") == 0
    assert approval_id is not None


async def test_concurrent_delete_and_begin_never_orphan_execution(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    for _ in range(6):
        ws, run_id, approval_id = await _approved_run(async_db, workspaces, clock, ids)
        store = SqlExecutionStore(async_db, ws, clock=clock, ids=ids)

        async def delete(ws: uuid.UUID = ws, run_id: uuid.UUID = run_id) -> bool:
            try:
                async with async_db.workspace_session(ws) as s:
                    return await delete_run(s, workspace_id=ws, run_id=run_id)
            except RunHasActiveExecution:
                return False

        async def begin(
            store: SqlExecutionStore = store, approval_id: uuid.UUID = approval_id
        ) -> bool:
            try:
                await store.begin(approval_id, ids.next(), 60)
            except ApprovalNotApproved:
                return False
            return True

        deleted, began = await asyncio.gather(delete(), begin())
        assert deleted != began  # 둘 중 하나만 성공한다
        runs, executions = (
            await _count(async_db, ws, "run"),
            await _count(async_db, ws, "execution"),
        )
        assert (runs, executions) == ((0, 0) if deleted else (1, 1))
