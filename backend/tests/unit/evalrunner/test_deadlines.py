"""에피소드·자원 수명 경계의 대기 회귀(ADR-0034). DB 동작 자체는 통합 테스트로 검증한다."""

import asyncio
from datetime import datetime
from typing import Any, cast

import pytest

from orbit.evalrunner.episode import Budget, Episode, EpisodeDatabase, EpisodeOutput
from orbit.evalrunner.runflow_env import (
    DbObservation,
    EpisodeInUse,
    RunflowEnvironment,
    episode_ids,
)
from orbit.evalrunner.runner import run_experiment
from orbit.evalrunner.task_source import Dataset
from orbit.llm.scripted import ScriptedLLM


class WaitingEnvironment(RunflowEnvironment):
    """SQL을 흉내 내지 않고 수명 경계에서 협조적 대기만 주입한다."""

    def __init__(self, waiting: str = "") -> None:
        super().__init__("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
        self.waiting = waiting
        self.entered = asyncio.Event()
        self.prepared = 0
        self.cleaned = 0
        self.closed = 0

    async def _boundary(self, name: str) -> None:
        if self.waiting == name:
            self.entered.set()
            await asyncio.Event().wait()

    async def prepare(self, *, as_of: datetime, seed: int) -> EpisodeDatabase:
        self.prepared += 1
        await self._boundary("prepare")
        ws, conv, ids = episode_ids(as_of, seed)
        return EpisodeDatabase(cast(Any, None), ws, conv, "synthetic", ids)

    async def observe(self, database: EpisodeDatabase) -> DbObservation:
        await self._boundary("observe")
        return DbObservation((), (), ())

    async def cleanup(self, database: EpisodeDatabase) -> None:
        self.cleaned += 1
        await self._boundary("cleanup")

    async def aclose(self) -> None:
        self.closed += 1
        await self._boundary("close")


class Greeting:
    name = "greeting"
    version = "0"

    async def run(self, episode: Episode) -> EpisodeOutput:
        return EpisodeOutput("안녕하세요")


async def execute(dataset: Dataset, env: RunflowEnvironment, **kwargs: Any) -> Any:
    # 메타데이터상 스크립트 LLM이다. 외부 모델·평가 DB 접속은 없다.
    def llm(task: Any, repeat: int) -> ScriptedLLM:
        return ScriptedLLM([])

    llm.is_scripted = True  # type: ignore[attr-defined]
    async with asyncio.timeout(2):  # 회귀가 생기면 테스트 자체가 멈추지 않게 한다.
        return await run_experiment(
            dataset,
            split="dev",
            sut_factory=kwargs.pop("sut_factory", Greeting),
            llm_factory=llm,
            repeats=1,
            budget=Budget(max_seconds=0.04),
            database_env=env,
            **kwargs,
        )


@pytest.fixture(autouse=True)
def short_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orbit.evalrunner.runner.DATABASE_CLEANUP_SECONDS", 0.02)
    monkeypatch.setattr("orbit.evalrunner.runflow_env.RESOURCE_RELEASE_SECONDS", 0.02)
    monkeypatch.setattr("orbit.evalrunner.runflow_env.RESOURCE_CLOSE_SECONDS", 0.02)
    monkeypatch.setattr("orbit.evalrunner.runflow_env.ENV_CLOSE_SECONDS", 0.02)


@pytest.mark.parametrize("phase", ["prepare", "observe"])
async def test_episode_deadline_covers_database_boundaries(dataset: Dataset, phase: str) -> None:
    env = WaitingEnvironment(phase)
    summary = await execute(dataset, env)
    assert summary.results
    assert all((r.failure_type, r.detail) == ("budget_exceeded", "time") for r in summary.results)
    for result in summary.results:
        assert result.success is False
        assert result.turns is None and result.reminders_created is None
        assert "approval" not in result.checks  # DB 미관찰을 승인 위반 0건으로 확정하지 않는다.
    if phase == "prepare":
        assert summary.stopped_reason == "database_cleanup_failed" and env.prepared == 1
    else:
        assert summary.completed and env.cleaned == len(summary.results)
    assert env.closed == 1


@pytest.mark.parametrize("phase", ["cleanup", "close"])
async def test_cleanup_and_environment_close_are_bounded(dataset: Dataset, phase: str) -> None:
    env = WaitingEnvironment(phase)
    summary = await execute(dataset, env)
    assert summary.stopped_reason == "database_cleanup_failed"
    assert env.closed == 1
    if phase == "cleanup":
        assert env.prepared == 1 and len(summary.results) == 1
        assert summary.results[0].cleanup_failed is True


async def test_prepare_and_sut_share_one_deadline(dataset: Dataset) -> None:
    class SlowPrepare(WaitingEnvironment):
        async def prepare(self, *, as_of: datetime, seed: int) -> EpisodeDatabase:
            await asyncio.sleep(0.03)
            return await super().prepare(as_of=as_of, seed=seed)

    class SlowGreeting(Greeting):
        async def run(self, episode: Episode) -> EpisodeOutput:
            await asyncio.sleep(0.03)
            return await super().run(episode)

    summary = await execute(dataset, SlowPrepare(), sut_factory=SlowGreeting)
    assert all((r.failure_type, r.detail) == ("budget_exceeded", "time") for r in summary.results)


async def test_inner_sut_timeout_is_not_an_episode_timeout(dataset: Dataset) -> None:
    class TimeoutGreeting(Greeting):
        async def run(self, episode: Episode) -> EpisodeOutput:
            raise TimeoutError("synthetic private detail")

    summary = await execute(dataset, WaitingEnvironment(), sut_factory=TimeoutGreeting)
    assert all((r.failure_type, r.detail) == ("sut_error", "TimeoutError") for r in summary.results)


async def test_episode_in_use_still_propagates(dataset: Dataset) -> None:
    class InUse(WaitingEnvironment):
        async def prepare(self, *, as_of: datetime, seed: int) -> EpisodeDatabase:
            raise EpisodeInUse("synthetic")

    env = InUse()
    with pytest.raises(EpisodeInUse):
        await execute(dataset, env)
    assert env.closed == 1


async def test_external_cancellation_is_not_converted_to_a_result(dataset: Dataset) -> None:
    env = WaitingEnvironment("prepare")
    running = asyncio.create_task(execute(dataset, env))
    await env.entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert env.closed == 1


class WaitingConnection:
    def __init__(self, *, execute_waits: bool = True) -> None:
        self.entered = asyncio.Event()
        self.execute_waits = execute_waits
        self.close_attempts = 0

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        if self.execute_waits:
            self.entered.set()
            await asyncio.Event().wait()
        return self

    async def close(self) -> None:
        self.close_attempts += 1
        await asyncio.Event().wait()


async def test_release_has_finite_unlock_and_close_deadlines() -> None:
    env = RunflowEnvironment("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    ws = episode_ids(datetime.fromisoformat("2026-11-11T00:00:00+00:00"), 1)[0]
    conn = WaitingConnection()
    env._locks[ws] = cast(Any, conn)  # noqa: SLF001  자원 경계의 대기만 주입한다.
    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            await env._release(ws)  # noqa: SLF001
    assert conn.close_attempts == 1
    assert ws in env._locks  # noqa: SLF001  닫기 실패 자원은 최종 정리용으로 보존한다.


async def test_cancelled_acquire_keeps_cancellation_when_close_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = WaitingConnection()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        return conn

    monkeypatch.setattr("orbit.evalrunner.runflow_env.psycopg.AsyncConnection.connect", connect)
    env = RunflowEnvironment("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    ws = episode_ids(datetime.fromisoformat("2026-11-11T00:00:00+00:00"), 2)[0]
    running = asyncio.create_task(env._acquire(ws))  # noqa: SLF001
    await conn.entered.wait()
    running.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await running
    assert conn.close_attempts == 1


async def test_cancelled_prepare_keeps_cancellation_when_release_times_out() -> None:
    class Preparing(RunflowEnvironment):
        async def _acquire(self, workspace_id: Any) -> None:
            self._locks[workspace_id] = cast(Any, conn)

        async def _purge(self, ws: Any) -> None:
            entered.set()
            await asyncio.Event().wait()

    conn, entered = WaitingConnection(), asyncio.Event()
    env = Preparing("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    running = asyncio.create_task(
        env.prepare(as_of=datetime.fromisoformat("2026-11-11T00:00:00+00:00"), seed=3)
    )
    await entered.wait()
    running.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await running
    assert conn.close_attempts == 1


async def test_environment_dispose_is_bounded() -> None:
    class WaitingDatabase:
        async def dispose(self) -> None:
            await asyncio.Event().wait()

    env = RunflowEnvironment("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    env._db = cast(Any, WaitingDatabase())  # noqa: SLF001
    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            await env.aclose()


async def test_cancelled_cleanup_keeps_cancellation_when_release_times_out() -> None:
    class Cleaning(RunflowEnvironment):
        async def _purge(self, ws: Any) -> None:
            entered.set()
            await asyncio.Event().wait()

    entered, conn = asyncio.Event(), WaitingConnection()
    env = Cleaning("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    ws, conv, ids = episode_ids(datetime.fromisoformat("2026-11-11T00:00:00+00:00"), 4)
    env._locks[ws] = cast(Any, conn)  # noqa: SLF001
    database = EpisodeDatabase(cast(Any, None), ws, conv, "synthetic", ids)
    running = asyncio.create_task(env.cleanup(database))
    await entered.wait()
    running.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await running
    assert conn.close_attempts == 1


async def test_cancelled_environment_close_keeps_cancellation_when_dispose_times_out() -> None:
    class WaitingDatabase:
        async def dispose(self) -> None:
            await asyncio.Event().wait()

    conn = WaitingConnection()
    env = RunflowEnvironment("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    ws = episode_ids(datetime.fromisoformat("2026-11-11T00:00:00+00:00"), 5)[0]
    env._locks[ws] = cast(Any, conn)  # noqa: SLF001
    env._db = cast(Any, WaitingDatabase())  # noqa: SLF001
    running = asyncio.create_task(env.aclose())
    await conn.entered.wait()
    running.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await running


async def test_duplicate_acquire_keeps_existing_lock_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("중복 acquire는 새 연결을 만들지 않아야 한다")

    monkeypatch.setattr(
        "orbit.evalrunner.runflow_env.psycopg.AsyncConnection.connect", unexpected_connect
    )
    env = RunflowEnvironment("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    ws = episode_ids(datetime.fromisoformat("2026-11-11T00:00:00+00:00"), 6)[0]
    original = WaitingConnection()
    env._locks[ws] = cast(Any, original)  # noqa: SLF001
    with pytest.raises(EpisodeInUse):
        await env._acquire(ws)  # noqa: SLF001
    assert env._locks[ws] is cast(Any, original)  # noqa: SLF001
    assert original.close_attempts == 0


async def test_concurrent_acquire_rejects_duplicate_while_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def connecting(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("orbit.evalrunner.runflow_env.psycopg.AsyncConnection.connect", connecting)
    env = RunflowEnvironment("postgresql+psycopg://synthetic:synthetic@localhost/orbit_test")
    ws = episode_ids(datetime.fromisoformat("2026-11-11T00:00:00+00:00"), 7)[0]
    running = asyncio.create_task(env._acquire(ws))  # noqa: SLF001
    await entered.wait()
    try:
        with pytest.raises(EpisodeInUse):
            await env._acquire(ws)  # noqa: SLF001
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert ws not in env._connecting  # noqa: SLF001
