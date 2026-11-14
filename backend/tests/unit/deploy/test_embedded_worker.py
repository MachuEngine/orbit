"""내장 워커: 정상 종료 순서와 실패 시 프로세스 종료 (ADR-0022 §2). DB 없이 가짜 러너로 확인한다."""

import asyncio
import socket
import subprocess
import sys
import textwrap
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from orbit.runflow import embedded
from orbit.runflow.embedded import EmbeddedWorker


class FakeRunner:
    def __init__(self, events: list[str], *, fail: Exception | None = None, ends: bool = False):
        self.events = events
        self.fail = fail
        self.ends = ends  # True면 멈추라고 하지 않았는데 스스로 끝난다
        self.stopped = asyncio.Event()

    async def run(self) -> None:
        self.events.append("run")
        await asyncio.sleep(0)
        if self.fail is not None:
            raise self.fail
        if self.ends:
            return
        await self.stopped.wait()
        self.events.append("run-finished")

    def stop(self) -> None:
        self.events.append("stop")
        self.stopped.set()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_worker_exception_triggers_failure_with_kind_only() -> None:
    events: list[str] = []
    failures: list[str] = []
    runner = FakeRunner(events, fail=ConnectionError("synthetic-secret-detail"))
    worker = EmbeddedWorker(runner, on_failure=failures.append)
    worker.launch()
    await _settle()
    assert failures == ["ConnectionError"]
    await worker.aclose()


async def test_unexpected_return_is_a_failure() -> None:
    failures: list[str] = []
    worker = EmbeddedWorker(FakeRunner([], ends=True), on_failure=failures.append)
    worker.launch()
    await _settle()
    assert failures == ["ExitedUnexpectedly"]
    await worker.aclose()


async def test_graceful_close_stops_worker_first_and_is_not_a_failure() -> None:
    events: list[str] = []
    failures: list[str] = []
    stack = AsyncExitStack()
    stack.push_async_callback(_record(events, "app-closed"))
    worker = EmbeddedWorker(FakeRunner(events), on_failure=failures.append, stack=stack)
    worker.launch()
    await _settle()
    await worker.aclose()
    assert events == ["run", "stop", "run-finished", "app-closed"]  # 워커를 먼저 멈춘 뒤 닫는다
    assert failures == []


async def test_close_cancels_worker_that_does_not_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedded, "STOP_TIMEOUT_SECONDS", 0.05)

    class Stuck:
        async def run(self) -> None:
            await asyncio.sleep(3600)

        def stop(self) -> None:
            pass

    failures: list[str] = []
    worker = EmbeddedWorker(Stuck(), on_failure=failures.append)
    worker.launch()
    await _settle()
    await worker.aclose()  # 제한 시간 뒤 취소하고 돌아온다
    assert failures == []


def _record(events: list[str], name: str) -> Callable[[], Awaitable[None]]:
    async def callback() -> None:
        events.append(name)

    return callback


SCRIPT = textwrap.dedent(
    """
    import asyncio, sys
    from contextlib import asynccontextmanager

    import uvicorn
    from fastapi import FastAPI

    from orbit.runflow.embedded import EmbeddedWorker


    class Failing:
        async def run(self) -> None:
            await asyncio.sleep(0.5)
            raise RuntimeError("synthetic-secret-detail")

        def stop(self) -> None:
            pass


    @asynccontextmanager
    async def lifespan(app):
        worker = EmbeddedWorker(Failing())
        worker.launch()
        try:
            yield
        finally:
            await worker.aclose()


    uvicorn.run(FastAPI(lifespan=lifespan), host="127.0.0.1", port=int(sys.argv[1]))
    """
)


def test_process_exits_with_failure_when_worker_dies_under_uvicorn(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    script = tmp_path / "serve_failing_worker.py"
    script.write_text(SCRIPT, encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(script), str(port)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0  # 신호 종료(음수) 또는 강제 종료(1). 0이면 워커 없이 끝난 것이다
    assert "kind=RuntimeError" in result.stderr
    assert "synthetic-secret-detail" not in result.stderr  # 오류 종류만 로그에 남는다
