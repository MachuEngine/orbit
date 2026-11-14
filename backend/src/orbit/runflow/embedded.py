"""API 프로세스 안에서 도는 내장 워커 (ADR-0022 §2).

Render 무료 플랜에는 Background Worker가 없어서 `ORBIT_EMBEDDED_WORKER=true`이면 FastAPI lifespan이
`WorkerPool`(큐 default·mail_index·notify)을 같은 이벤트 루프의 백그라운드 태스크로 띄운다.

- 종료 때는 워커를 먼저 정상 종료시킨다(실행 중인 작업이 끝나길 기다리되 시간 상한이 있다).
- 워커 태스크가 예외로 끝나거나 멈추라고 하지 않았는데 끝나면 오류 종류를 로그에 남기고 프로세스를
  실패로 끝낸다. 워커 없이 API만 도는 상태로 남지 않게 하기 위해서다. 기본 동작은 자기 자신에게
  SIGTERM을 보내 uvicorn이 정상 종료하게 하고(종료 코드는 신호 종료라 0이 아니다), 제한 시간이
  지나도 끝나지 않으면 강제로 끝낸다.
"""

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Protocol

from orbit.core.clock import Clock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.runflow.worker import WorkerPool, WorkerSetupError, build_worker_app, build_worker_deps

logger = logging.getLogger(__name__)

STOP_TIMEOUT_SECONDS = (
    20.0  # 종료 때 워커 태스크를 기다리는 시간. Render는 SIGTERM 뒤 30초 안에 끝내길 기대한다
)
FAILURE_EXIT_GRACE_SECONDS = 15.0  # 워커 실패 뒤 정상 종료를 기다리는 시간. 넘기면 강제로 끝낸다
FAILURE_EXIT_CODE = 1


class WorkerRunner(Protocol):
    """`WorkerPool`이 만족하는 최소 형태(테스트에서 가짜로 바꾼다)."""

    async def run(self) -> None: ...

    def stop(self) -> None: ...


def terminate_process(kind: str) -> None:
    """워커 실패 시 기본 동작: SIGTERM로 정상 종료를 시도하고, 끝나지 않으면 강제로 끝낸다."""
    logger.error("내장 워커 실패로 프로세스를 종료한다: kind=%s", kind)
    loop = asyncio.get_running_loop()
    loop.call_later(FAILURE_EXIT_GRACE_SECONDS, os._exit, FAILURE_EXIT_CODE)
    os.kill(os.getpid(), signal.SIGTERM)


class EmbeddedWorker:
    """워커 풀 하나를 백그라운드 태스크로 돌리고, 실패하면 `on_failure(오류 종류)`를 부른다."""

    def __init__(
        self,
        runner: WorkerRunner,
        *,
        on_failure: Callable[[str], None] = terminate_process,
        stack: AsyncExitStack | None = None,
    ) -> None:
        self._runner = runner
        self._on_failure = on_failure
        self._stack = stack if stack is not None else AsyncExitStack()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def start(
        cls,
        settings: Settings,
        *,
        db: AsyncDatabase,
        clock: Clock,
        ids: Uuid7Generator,
        on_failure: Callable[[str], None] = terminate_process,
    ) -> "EmbeddedWorker":
        """워커 의존성을 만들고 작업 앱을 열어 태스크를 띄운다(설정 부족은 `WorkerSetupError`)."""
        deps = build_worker_deps(settings, db=db, clock=clock, ids=ids)
        stack = AsyncExitStack()
        try:
            closer = getattr(deps.resolver, "aclose", None)
            if closer is not None:
                stack.push_async_callback(closer)
            app = build_worker_app(settings.database_url.get_secret_value(), deps)
            await stack.enter_async_context(app.open_async())
            pool = WorkerPool(
                app,
                install_signal_handlers=False,  # 신호는 uvicorn이 받는다
                shutdown_graceful_timeout=STOP_TIMEOUT_SECONDS - 5,
            )
            worker = cls(pool, on_failure=on_failure, stack=stack)
            worker.launch()
        except BaseException:
            await stack.aclose()
            raise
        return worker

    def launch(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="embedded-worker")

    async def _supervise(self) -> None:
        kind: str | None = None
        try:
            await self._runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind = type(exc).__name__
        else:
            if not self._stopping:
                kind = "ExitedUnexpectedly"
        if kind is not None and not self._stopping:
            logger.error("내장 워커가 끝났다: kind=%s", kind)
            self._on_failure(kind)

    async def aclose(self) -> None:
        """워커를 먼저 정상 종료시키고, 작업 앱과 커넥터 해석기를 닫는다."""
        self._stopping = True
        self._runner.stop()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("내장 워커가 제한 시간 안에 끝나지 않아 취소한다")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception as exc:  # 종료 중 오류는 종류만 남기고 정리를 이어간다
                logger.warning("내장 워커 종료 중 오류: kind=%s", type(exc).__name__)
        await self._stack.aclose()


__all__ = ["EmbeddedWorker", "WorkerSetupError", "terminate_process"]
