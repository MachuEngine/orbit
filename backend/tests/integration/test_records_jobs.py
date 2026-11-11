"""Procrastinate 작업 등록·멈춘 작업 복구·run 삭제 전파 (ADR-0005, ADR-0006, ADR-0012)."""

import uuid
from collections.abc import AsyncIterator, Iterator

import procrastinate
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from integration.records_support import RECORD_TABLES, new_clock, new_run, seed_all_tables
from integration.support import WorkspaceFactory
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.deletion import CHECKPOINT_TABLES, delete_run
from orbit.records.errors import InvalidJobArgs
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


def test_job_args_accept_only_run_id_string() -> None:
    rid = str(uuid.uuid4())
    assert validate_job_args({"run_id": rid}) == {"run_id": rid}
    for bad in (
        {},
        {"run_id": rid, "body": "synthetic body"},
        {"run_id": rid, "to": "someone@example.invalid"},
        {"run_id": uuid.UUID(rid)},  # UUID 객체가 아니라 문자열만
        {"run_id": "not-a-uuid"},
        {"run_id": 123},
        {"other": rid},
    ):
        with pytest.raises(InvalidJobArgs):
            validate_job_args(bad)


def test_job_args_error_message_has_no_values() -> None:
    with pytest.raises(InvalidJobArgs) as info:
        validate_job_args({"run_id": str(uuid.uuid4()), "body": "secret-synthetic-text"})
    assert "secret-synthetic-text" not in str(info.value)
    assert "body" in str(info.value)


async def test_defer_job_rejects_bad_args_without_registering(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    ws = workspaces.create()
    async with async_db.workspace_session(ws) as s:
        with pytest.raises(InvalidJobArgs):
            await defer_job(
                s, app, task_name=TASK, args={"run_id": str(uuid.uuid4()), "x": "y"}, queue=queue
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
        job_id = await defer_job(s, app, task_name=TASK, args={"run_id": str(run_id)}, queue=queue)
    assert isinstance(job_id, int)
    assert await _jobs(async_db, queue) == [(TASK, {"run_id": str(run_id)}, "todo")]
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
            job_id = await defer_job(
                s, app, task_name=TASK, args={"run_id": str(run_id)}, queue=queue
            )
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
            args={"run_id": str(first_run)},
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
            args={"run_id": str(second_run)},
            queue=queue,
            queueing_lock=lock,
        )
        assert second is None  # AlreadyEnqueued는 None으로 돌려주고
        # SAVEPOINT 덕분에 같은 트랜잭션에서 계속 쓸 수 있다.
        assert await get_run(s, workspace_id=ws, run_id=second_run) is not None

    # 도메인 행은 커밋됐고, 작업은 처음 것 하나뿐이다.
    async with async_db.workspace_session(ws) as s:
        assert await get_run(s, workspace_id=ws, run_id=second_run) is not None
    assert [j[1] for j in await _jobs(async_db, queue)] == [{"run_id": str(first_run)}]


# 멈춘 작업 복구 ------------------------------------------------------------------------


async def test_recover_stalled_jobs_returns_doing_job_to_todo(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, app: procrastinate.App, queue: str
) -> None:
    clock = new_clock()
    ws = workspaces.create()
    run_id = str(uuid.uuid4())
    async with async_db.workspace_session(ws) as s:
        job_id = await defer_job(s, app, task_name=TASK, args={"run_id": run_id}, queue=queue)
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
        assert (
            await recover_stalled_jobs(app, seconds_since_heartbeat=7200, clock=clock, queue=queue)
            == []
        )
        assert (await _jobs(async_db, queue))[0][2] == "doing"

        recovered = await recover_stalled_jobs(
            app, seconds_since_heartbeat=1800, clock=clock, queue=queue
        )
        assert recovered == [job_id]
        assert (await _jobs(async_db, queue))[0][2] == "todo"
        # 이미 되돌렸으므로 다시 불러도 대상이 없다.
        assert (
            await recover_stalled_jobs(app, seconds_since_heartbeat=1800, clock=clock, queue=queue)
            == []
        )
    finally:
        async with async_db.workspace_session(uuid.uuid4()) as s:
            await s.execute(
                text("DELETE FROM procrastinate_jobs WHERE queue_name = :q"), {"q": queue}
            )
            await s.execute(
                text("DELETE FROM procrastinate_workers WHERE id = :w"), {"w": worker_id}
            )


# run 삭제 전파 ----------------------------------------------------------------------------


@pytest.fixture
def checkpoint_tables(owner_url: str) -> Iterator[None]:
    """LangGraph 체크포인트 테이블 모양(thread_id 열)을 흉내 낸다. 이미 있으면 건드리지 않는다."""
    engine = create_engine(owner_url)
    created: list[str] = []
    try:
        with engine.begin() as conn:
            for table in CHECKPOINT_TABLES:
                exists = conn.execute(
                    text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{table}"}
                ).scalar_one()
                if exists:
                    continue
                conn.execute(
                    text(f"CREATE TABLE public.{table} (thread_id text NOT NULL, data text)")  # noqa: S608
                )
                conn.execute(text(f"GRANT SELECT, INSERT, DELETE ON public.{table} TO orbit_app"))
                created.append(table)
        yield
    finally:
        with engine.begin() as conn:
            for table in created:
                conn.execute(text(f"DROP TABLE IF EXISTS public.{table}"))
        engine.dispose()


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
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, checkpoint_tables: None
) -> None:
    clock = new_clock()
    ids = Uuid7Generator(clock)
    a = await seed_all_tables(async_db, workspaces, clock, ids)
    keep = await seed_all_tables(async_db, workspaces, clock, ids)
    for run_id in (a.run_id, keep.run_id):
        async with async_db.workspace_session(a.workspace_id) as s:
            for table in CHECKPOINT_TABLES:
                await s.execute(
                    text(f"INSERT INTO public.{table} (thread_id, data) VALUES (:t, 'synthetic')"),  # noqa: S608
                    {"t": str(run_id)},
                )
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


async def test_delete_run_without_checkpoint_tables(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> None:
    """체크포인트 테이블이 없는 DB(LangGraph 설정 전)에서도 run 삭제가 동작한다."""
    clock = new_clock()
    ids = Uuid7Generator(clock)
    ws, run_id = await new_run(async_db, workspaces, clock, ids)
    async with async_db.workspace_session(ws) as s:
        assert await delete_run(s, workspace_id=ws, run_id=run_id) is True
        assert await get_run(s, workspace_id=ws, run_id=run_id) is None
