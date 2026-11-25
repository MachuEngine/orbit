"""내장 워커: 정상 종료 순서와 실패 시 프로세스 종료 (ADR-0022 §2). DB 없이 가짜 러너로 확인한다."""

import asyncio
import signal
import socket
import subprocess
import sys
import textwrap
import time
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
    import asyncio, signal, sys
    from contextlib import asynccontextmanager

    import uvicorn
    from fastapi import FastAPI
    from starlette.responses import StreamingResponse

    from orbit.runflow import embedded
    from orbit.runflow.embedded import EmbeddedWorker

    port, mode, graceful, force_after = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
    embedded.FAILURE_EXIT_GRACE_SECONDS = float(force_after)


    class Worker:
        def __init__(self):
            self.stopped = asyncio.Event()

        async def run(self) -> None:
            if mode.startswith("fail"):
                await asyncio.sleep(0.5)
                raise RuntimeError("synthetic-secret-detail")
            await self.stopped.wait()

        def stop(self) -> None:
            print("worker-stopped", flush=True)
            self.stopped.set()


    @asynccontextmanager
    async def lifespan(app):
        worker = EmbeddedWorker(Worker())
        worker.launch()
        try:
            yield
        finally:
            await worker.aclose()


    app = FastAPI(lifespan=lifespan)


    @app.get("/sse")
    async def sse():
        async def forever():
            while True:
                yield b": ping\\n\\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(forever(), media_type="text/event-stream")


    if mode == "fail-pid1":  # PID 1은 SIG_DFL 신호가 무시된다: uvicorn의 신호 재발생을 없앤다
        signal.raise_signal = lambda sig: None
    uvicorn.run(
        app, host="127.0.0.1", port=port, timeout_graceful_shutdown=int(graceful), log_level="info"
    )
    """
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(tmp_path: Path, mode: str, graceful: int, force_after: float) -> subprocess.Popen[str]:
    script = tmp_path / "serve.py"
    script.write_text(SCRIPT, encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", str(script), str(port), mode, str(graceful), str(force_after)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc.port = port  # type: ignore[attr-defined]
    return proc


def _open_sse(proc: subprocess.Popen[str]) -> socket.socket:
    port = proc.port  # type: ignore[attr-defined]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=1)
        except OSError:
            time.sleep(0.1)
            continue
        conn.sendall(b"GET /sse HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"200" in conn.recv(256)
        return conn
    raise AssertionError("서버가 시작되지 않았다")


def test_worker_failure_ends_process_nonzero_via_sigterm(tmp_path: Path) -> None:
    proc = _serve(tmp_path, "fail", graceful=5, force_after=30)
    out, _ = proc.communicate(timeout=60)
    assert (
        proc.returncode == -signal.SIGTERM
    )  # uvicorn이 정상 종료한 뒤 신호 종료로 끝난다(강제 아님)
    assert "kind=RuntimeError" in out
    assert "synthetic-secret-detail" not in out  # 오류 종류만 로그에 남는다


def test_worker_failure_exits_nonzero_even_if_sigterm_is_ignored(tmp_path: Path) -> None:
    proc = _serve(tmp_path, "fail-pid1", graceful=5, force_after=30)
    proc.communicate(timeout=60)
    assert proc.returncode == embedded.FAILURE_EXIT_CODE


async def test_aclose_closes_stack_even_if_cancelled() -> None:
    events: list[str] = []
    stack = AsyncExitStack()
    stack.push_async_callback(_record(events, "app-closed"))
    worker = EmbeddedWorker(FakeRunner(events), stack=stack)
    worker.launch()
    await _settle()
    assert worker._task is not None
    worker._task.cancel()
    await asyncio.gather(worker._task, return_exceptions=True)
    closer = asyncio.create_task(worker.aclose())
    await _settle()
    closer.cancel()  # 대기 중 CancelledError가 나도 정리는 한다
    await asyncio.gather(closer, return_exceptions=True)
    assert "app-closed" in events


def test_worker_failure_with_open_sse_is_force_exited_nonzero(tmp_path: Path) -> None:
    # 종료 지연(열린 SSE)이 상한보다 길면 강제 종료(1)로 끝난다
    proc = _serve(tmp_path, "fail", graceful=60, force_after=2)
    conn = _open_sse(proc)
    try:
        proc.communicate(timeout=60)
    finally:
        conn.close()
    assert proc.returncode == embedded.FAILURE_EXIT_CODE


def test_sigterm_with_open_sse_stops_worker_within_graceful_limit(tmp_path: Path) -> None:
    proc = _serve(tmp_path, "idle", graceful=1, force_after=30)
    conn = _open_sse(proc)
    try:
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        elapsed = time.monotonic() - started
    finally:
        conn.close()
    assert "worker-stopped" in out  # SSE가 열려 있어도 lifespan 종료에 도달해 워커를 멈춘다
    assert elapsed < 10
    assert proc.returncode in (0, -signal.SIGTERM)


def test_shutdown_limits_do_not_contradict() -> None:
    # 강제 상한이 uvicorn 상한 + 워커 종료 상한보다 길어야 정상 종료가 끝나기 전에 끊지 않는다
    assert (
        embedded.FAILURE_EXIT_GRACE_SECONDS
        > embedded.UVICORN_GRACEFUL_SHUTDOWN_SECONDS + embedded.STOP_TIMEOUT_SECONDS
    )
    dockerfile = Path(__file__).resolve().parents[4] / "Dockerfile"
    expected = f"--timeout-graceful-shutdown {embedded.UVICORN_GRACEFUL_SHUTDOWN_SECONDS:g}"
    assert expected in dockerfile.read_text(encoding="utf-8")
