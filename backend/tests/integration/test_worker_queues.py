"""색인 작업은 별도 큐에서 돌아 run 작업을 막지 않는다 (ADR-0019 §6). 데이터는 모두 합성이다."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from integration.mailindex_support import ACCOUNT, create_connection
from integration.runflow_support import Harness, StubResolver
from orbit.mailindex import service
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import FakeEmbedder
from orbit.mailindex.jobs import MAIL_INDEX_QUEUE, SYNC_MAIL_INDEX, enqueue_due_syncs
from orbit.runflow.worker import RUN_QUEUE, WorkerPool, build_worker_app

Make = Callable[..., Awaitable[Harness]]


class _GatedMail:
    """첫 `get_message`에서 멈췄다가 풀어 주면 이어가는 느린 가짜 동기화."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def get_message(self, message_id: str) -> Any:
        self.started.set()
        await self.gate.wait()
        return await self._inner.get_message(message_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _queues(h: Harness) -> dict[str, str]:
    async with h.db.workspace_session(h.ws) as s:
        rows = await s.execute(
            text(
                "SELECT task_name, queue_name FROM procrastinate_jobs"
                " WHERE args->>'workspace_id' = :ws"
            ),
            {"ws": str(h.ws)},
        )
        return {r[0]: r[1] for r in rows}


async def test_slow_sync_does_not_block_run_jobs_and_jobs_use_separate_queues(
    make_harness: Make, owner_engine: Engine
) -> None:
    h = await make_harness([{"text": "안녕하세요."}])
    cid = create_connection(owner_engine, h.ws, h.deps.ids)
    slow = _GatedMail(h.conns.mail[ACCOUNT])
    h.deps.resolver = StubResolver(h.calendar, slow)
    mi = MailIndexDeps(db=h.db, clock=h.clock, ids=h.deps.ids, embedder=FakeEmbedder())
    h.deps.mail_index = mi
    app = build_worker_app(h.app_url, h.deps)
    await service.add_scope(mi, h.ws, cid, label_id="INBOX", label_name="INBOX", period_days=365)
    run_id = await h.start("안녕")

    queues = await _queues(h)
    assert queues[SYNC_MAIL_INDEX] == MAIL_INDEX_QUEUE == "mail_index"
    assert queues["execute_run"] == RUN_QUEUE == "default"

    pool = WorkerPool(app, install_signal_handlers=False, listen_notify=False)
    async with app.open_async():
        running = asyncio.create_task(pool.run())
        try:
            await asyncio.wait_for(slow.started.wait(), 10)  # 동기화가 시작해 멈춰 있다
            for _ in range(100):  # 동기화가 도는 동안에도 run이 끝난다
                if await h.status(run_id) == "succeeded":
                    break
                await asyncio.sleep(0.1)
            assert await h.status(run_id) == "succeeded"
            assert (await service.get_status(mi, h.ws, cid)).status == "pending"
            slow.gate.set()
            for _ in range(100):
                if (await service.get_status(mi, h.ws, cid)).status == "ok":
                    break
                await asyncio.sleep(0.1)
            assert (await service.get_status(mi, h.ws, cid)).status == "ok"
        finally:
            slow.gate.set()
            pool.stop()
            await asyncio.wait_for(running, 15)


async def test_periodic_selection_registers_on_the_index_queue(
    make_harness: Make, owner_engine: Engine
) -> None:
    h = await make_harness([])
    cid = create_connection(owner_engine, h.ws, h.deps.ids)
    mi = MailIndexDeps(db=h.db, clock=h.clock, ids=h.deps.ids, embedder=FakeEmbedder())
    h.deps.mail_index = mi
    app = build_worker_app(h.app_url, h.deps)
    await service.add_scope(mi, h.ws, cid, label_id="INBOX", label_name="INBOX", period_days=30)
    async with h.db.workspace_session(
        h.ws
    ) as s:  # 범위 저장이 등록한 작업을 치우고 주기 등록만 본다
        await s.execute(
            text("DELETE FROM procrastinate_jobs WHERE args->>'workspace_id' = :ws"),
            {"ws": str(h.ws)},
        )
    h.clock.advance(timedelta(hours=1))
    async with app.open_async():
        assert await enqueue_due_syncs(mi, app) >= 1
    assert (await _queues(h))[SYNC_MAIL_INDEX] == MAIL_INDEX_QUEUE


async def test_pool_stops_other_workers_and_reraises_when_one_worker_fails(
    make_harness: Make, monkeypatch: Any
) -> None:
    from procrastinate.worker import Worker  # noqa: PLC0415

    h = await make_harness([])
    app = build_worker_app(h.app_url, h.deps)
    original = Worker.run
    stopped: list[str] = []

    async def run(self: Worker) -> None:
        if self.worker_name == "worker-mail_index":
            raise RuntimeError("합성 워커 오류")
        try:
            await original(self)  # type: ignore[no-untyped-call]
        finally:
            stopped.append(str(self.worker_name))

    monkeypatch.setattr(Worker, "run", run)
    pool = WorkerPool(app, install_signal_handlers=False, listen_notify=False)
    async with app.open_async():
        with pytest.raises(RuntimeError, match="합성 워커 오류"):
            await asyncio.wait_for(pool.run(), 20)
    assert stopped == ["worker-default"]  # 다른 워커는 정상 종료까지 마쳤다
