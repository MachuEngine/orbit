"""Procrastinate 워커: 작업 정의와 실행 진입점 (ADR-0005 조건 1~3, ADR-0006 결정 2).

- 작업 인자는 `run_id`, `workspace_id`(UUID 문자열)뿐이다.
- `execute_run`(처음 시작)과 `resume_run`(승인 결정 뒤 재개)은 멱등 처리기 `advance_run`을 쓴다.
  run이 없거나 종료 상태이거나 승인이 아직 `pending`이면 아무것도 하지 않고 정상 종료한다.
- 처리기가 예외로 끝나면 재시도한다(작업 재실행은 처음부터이므로 멱등해야 한다). 외부 쓰기는
  `execution` 선기록과 lease(ADR-0003 조건 1)가 중복을 막는다.
- 메일 색인(ADR-0019): `deps.mail_index`가 있으면 `sync_mail_index` 작업(인자 `workspace_id`,
  `connection_id`뿐)과 마지막 동기화가 오래된 연결을 골라 등록하는 주기 작업을 함께 등록한다.
- 멈춘 작업 복구 주기 작업(ADR-0005 조건 2): heartbeat가 끊긴 `doing` 작업을 `todo`로 되돌린다.
  복구 작업은 워커가 쓰는 연결과 따로 만든 앱으로 실행한다(열려 있는 앱을 다시 열지 않기 위해서다).
"""

import asyncio
import logging
import math
import uuid
from datetime import timedelta
from typing import Any

import procrastinate
from procrastinate.retry import BaseRetryStrategy, RetryDecision

from orbit.mailindex.jobs import (
    MAIL_INDEX_QUEUE,
    MAIL_INDEX_TICK,
    SYNC_MAIL_INDEX,
    TICK_CRON,
    enqueue_due_syncs,
    validate_sync_args,
)
from orbit.mailindex.sync import sync_connection
from orbit.records.errors import InvalidJobArgs
from orbit.records.jobs import make_procrastinate_app, recover_stalled_jobs, validate_job_args
from orbit.runflow.deps import NoConnection, RunDeps
from orbit.runflow.graph import advance_run, expire_if_due
from orbit.runflow.jobs import EXECUTE_RUN, EXPIRE_APPROVALS, RECOVER_STALLED, RESUME_RUN
from orbit.runflow.nodes import ExecutionNotSettled

logger = logging.getLogger(__name__)

__all__ = [
    "EXECUTE_RUN",
    "EXPIRE_APPROVALS",
    "MAIL_INDEX_QUEUE",
    "MAIL_INDEX_TICK",
    "RECOVER_STALLED",
    "RESUME_RUN",
    "RUN_QUEUE",
    "SYNC_MAIL_INDEX",
    "WorkerPool",
    "build_worker_app",
]

RUN_QUEUE = "default"  # run 실행·재개, 승인 만료, 멈춘 작업 복구
STALLED_AFTER_SECONDS = 60.0  # 워커 heartbeat 간격(10초)과 stalled_worker_timeout(30초)보다 길게
RETRY_MAX_ATTEMPTS = 5
RETRY_WAIT_SECONDS = 30
UNSETTLED_MAX_ATTEMPTS = 20  # 실행 기록이 끝나길 기다리는 재시도의 별도 상한


class RunRetryStrategy(BaseRetryStrategy):
    """재시도 시점을 오류 종류로 정한다.

    - `ExecutionNotSettled`(lease 미획득, 대조 미완): `lease_seconds + 1`초 뒤에 다시 한다.
      남은 lease는 `lease_seconds`를 넘지 않으므로 다음 실행은 만료된 lease를 넘겨받아
      대조까지 간다. 이 경우의 상한은 일반 재시도와 따로 `UNSETTLED_MAX_ATTEMPTS`다.
    - 그 밖의 예외: 고정 대기 `wait`초 뒤, 총 `max_attempts`번째까지.
    """

    def __init__(self, *, wait: int, lease_seconds: float, max_attempts: int) -> None:
        self._wait = wait
        self._unsettled_wait = math.ceil(lease_seconds) + 1
        self._max_attempts = max_attempts

    def get_retry_decision(
        self, *, exception: BaseException, job: procrastinate.jobs.Job
    ) -> RetryDecision | None:
        if isinstance(exception, ExecutionNotSettled):
            if job.attempts + 1 >= UNSETTLED_MAX_ATTEMPTS:
                return None
            return RetryDecision(retry_in={"seconds": self._unsettled_wait})
        if job.attempts + 1 >= self._max_attempts:
            return None
        return RetryDecision(retry_in={"seconds": self._wait})


def build_worker_app(
    database_url: str,
    deps: RunDeps,
    *,
    retry_wait_seconds: int = RETRY_WAIT_SECONDS,
) -> procrastinate.App:
    """작업 처리기를 등록한 Procrastinate 앱을 만든다."""
    app = make_procrastinate_app(database_url)
    retry = RunRetryStrategy(
        wait=retry_wait_seconds,
        lease_seconds=deps.lease_seconds,
        max_attempts=RETRY_MAX_ATTEMPTS,
    )
    deps.jobs = app  # 승인 요청 때 만료 확인 작업을 같은 트랜잭션에 예약한다

    async def _handle(job_name: str, **kwargs: str) -> None:
        try:
            args = validate_job_args(kwargs)
        except InvalidJobArgs:
            logger.error("작업 인자가 올바르지 않다: %s", job_name)
            return  # 재시도해도 같은 결과다
        outcome = await advance_run(
            deps, uuid.UUID(args["workspace_id"]), uuid.UUID(args["run_id"])
        )
        logger.info("%s: run %s -> %s", job_name, args["run_id"], outcome)

    @app.task(name=EXECUTE_RUN, retry=retry)
    async def execute_run(run_id: str, workspace_id: str) -> None:
        await _handle(EXECUTE_RUN, run_id=run_id, workspace_id=workspace_id)

    @app.task(name=RESUME_RUN, retry=retry)
    async def resume_run(run_id: str, workspace_id: str) -> None:
        await _handle(RESUME_RUN, run_id=run_id, workspace_id=workspace_id)

    @app.task(name=EXPIRE_APPROVALS, retry=retry)
    async def expire_approvals(run_id: str, workspace_id: str) -> None:
        # 만료 시각에 도착한다. 만료된 pending 승인만 expired로 바꾸고 resume_run을 등록한다.
        # 그 밖의 경우(이미 결정됨, 진행 중, 종료)에는 그래프를 구동하지 않는다.
        try:
            args = validate_job_args({"run_id": run_id, "workspace_id": workspace_id})
        except InvalidJobArgs:
            return
        changed = await expire_if_due(
            deps, uuid.UUID(args["workspace_id"]), uuid.UUID(args["run_id"])
        )
        logger.info("%s: run %s -> %s", EXPIRE_APPROVALS, args["run_id"], changed)

    @app.periodic(cron="* * * * *")
    @app.task(name=RECOVER_STALLED, queueing_lock=RECOVER_STALLED)
    async def recover_stalled(timestamp: int) -> None:
        recovered = await recover_stalled_jobs(
            make_procrastinate_app(database_url), seconds_since_heartbeat=STALLED_AFTER_SECONDS
        )
        if recovered:
            logger.warning("멈춘 작업 %d개를 되돌렸다", len(recovered))

    _register_mail_index_tasks(app, deps)
    return app


class WorkerPool:
    """큐마다 워커를 따로 돌리는 워커 프로세스 하나.

    색인 동기화는 첫 동기화에 수 분이 걸린다. 같은 큐·같은 워커(동시성 1)에 두면 그동안 run 작업과
    멈춘 작업 복구가 밀리므로, run 큐와 색인 큐에 각각 동시성 1인 워커를 동시에 돌린다.
    종료 신호는 이 풀이 받아 모든 워커를 정상 종료시킨다(워커별 신호 처리기는 서로 덮어쓴다).
    """

    def __init__(
        self,
        app: procrastinate.App,
        *,
        queues: tuple[str, ...] = (RUN_QUEUE, MAIL_INDEX_QUEUE),
        wait: bool = True,
        install_signal_handlers: bool = True,
        **options: Any,
    ) -> None:
        from procrastinate.worker import Worker

        self._install_signals = install_signal_handlers
        self._workers = [
            Worker(
                app=app,
                queues=[queue],
                concurrency=1,
                name=f"worker-{queue}",
                wait=wait,
                install_signal_handlers=False,
                **options,
            )
            for queue in queues
        ]

    def stop(self) -> None:
        for worker in self._workers:
            worker.stop()  # type: ignore[no-untyped-call]

    async def _run_one(self, worker: Any) -> BaseException | None:
        try:
            await worker.run()
        except Exception as exc:
            logger.error("워커가 예외로 끝났다: %s %s", worker.worker_name, type(exc).__name__)
            self.stop()
            return exc
        return None

    async def run(self) -> None:
        """앱이 열려 있어야 한다. 모든 워커가 끝나면 돌아온다."""
        import signal

        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        if self._install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.stop)
                installed.append(sig)
        try:
            # 한 워커가 예외로 끝나면 나머지를 정상 종료시키고, 모두 끝난 뒤 첫 예외를 다시 던진다.
            results = await asyncio.gather(
                *(self._run_one(worker) for worker in self._workers),
            )
            errors = [r for r in results if r is not None]
            if errors:
                raise errors[0]
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)


class SyncBusy(RuntimeError):
    """같은 연결의 다른 동기화가 진행 중이다. 작업을 재시도한다."""


def _register_mail_index_tasks(app: procrastinate.App, deps: RunDeps) -> None:
    """색인 동기화 작업과 주기 작업을 등록한다(`deps.mail_index`가 있을 때만)."""
    mail_index = deps.mail_index
    if mail_index is None:
        return
    mail_index.jobs = app  # 범위 저장·지금 동기화가 같은 앱으로 작업을 등록한다

    @app.task(
        name=SYNC_MAIL_INDEX,
        queue=MAIL_INDEX_QUEUE,
        retry=procrastinate.RetryStrategy(max_attempts=3, wait=60),
    )
    async def sync_mail_index(workspace_id: str, connection_id: str) -> None:
        # 연결이 없거나 해제됐으면 아무것도 하지 않고 끝난다. 오류 종류는 sync_state에 남는다.
        try:
            args = validate_sync_args(
                {"workspace_id": workspace_id, "connection_id": connection_id}
            )
        except InvalidJobArgs:
            logger.error("작업 인자가 올바르지 않다: %s", SYNC_MAIL_INDEX)
            return
        ws, cid = uuid.UUID(args["workspace_id"]), uuid.UUID(args["connection_id"])
        try:
            _, mail = await deps.resolver.connectors_for(ws)
        except NoConnection:
            logger.info("%s: connection %s -> no_connection", SYNC_MAIL_INDEX, cid)
            return
        result = await sync_connection(mail_index, mail, ws, cid)
        logger.info("%s: connection %s -> %s", SYNC_MAIL_INDEX, cid, result.outcome)
        if result.outcome == "busy":
            # 다른 동기화가 돌고 있다. 그 동기화가 놓쳤을 수 있는 새 범위를 위해 잠시 뒤 다시 한다.
            raise SyncBusy(f"connection {cid}")

    @app.periodic(cron=TICK_CRON)
    @app.task(name=MAIL_INDEX_TICK, queue=MAIL_INDEX_QUEUE, queueing_lock=MAIL_INDEX_TICK)
    async def mail_index_tick(timestamp: int) -> None:
        registered = await enqueue_due_syncs(mail_index, app)
        if registered:
            logger.info("%s: 동기화 %d건을 등록했다", MAIL_INDEX_TICK, registered)


QUIET_LOGGERS = ("httpx", "httpcore")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str) -> None:
    """워커 로그 수준과 형식을 정한다. 로그에는 ID와 종류만 남긴다(본문·주소·토큰 금지)."""
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    # 이 로거들은 요청 URL 전체(검색어 `q=` 포함)를 INFO/DEBUG로 남긴다. 수준과 무관하게 고정한다.
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    """개발용 워커 진입점: `python -m orbit.runflow.worker`.

    LLM은 `ORBIT_LLM_*`·`ORBIT_OPENROUTER_API_KEY` 설정으로, 커넥터는 개발용 Sim 스냅샷
    (`ORBIT_SIM_SNAPSHOT_PATH`)으로, `ORBIT_CONNECTOR_MODE=google`이면 워크스페이스의 활성 Google
    연결(`GoogleConnectorResolver`, KEK는 `ORBIT_DATA_KEY_FILE`)로 만든다.
    """
    from zoneinfo import ZoneInfo

    from pydantic import ValidationError
    from sqlalchemy.engine import make_url

    from orbit.core.clock import SystemClock
    from orbit.core.config import get_llm_settings, get_settings
    from orbit.core.ids import Uuid7Generator
    from orbit.crypto.envelope import EnvelopeCipher, EnvelopeError, load_kek_from_file
    from orbit.db.session import AsyncDatabase
    from orbit.llm.factory import build_llm
    from orbit.mailindex.deps import MailIndexDeps
    from orbit.mailindex.embedding import build_embedder
    from orbit.runflow.deps import ConnectorResolver, GoogleConnectorResolver, SimConnectorResolver

    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.env != "dev":
        raise SystemExit("개발용 워커는 ORBIT_ENV=dev가 필요하다")
    if settings.connector_mode == "sim" and settings.sim_snapshot_path is None:
        raise SystemExit("sim 모드 워커는 ORBIT_SIM_SNAPSHOT_PATH가 필요하다")
    try:
        llm_settings = get_llm_settings()
    except ValidationError:
        raise SystemExit(
            "LLM 설정이 없어 워커를 시작하지 않는다. 필요한 환경 변수: ORBIT_LLM_MODEL_ID, "
            "ORBIT_LLM_PROVIDER_ORDER, ORBIT_OPENROUTER_API_KEY. provider가 openai이면 "
            "ORBIT_LLM_PROVIDER=openai와 ORBIT_OPENAI_API_KEY (backend/dev.env 참고)"
        ) from None
    url = settings.database_url.get_secret_value()
    clock = SystemClock()
    db = AsyncDatabase.from_url(url)
    resolver: ConnectorResolver
    if settings.connector_mode == "google":
        missing = settings.missing_connection_settings()
        if missing:
            raise SystemExit("google 모드 워커에 필요한 설정이 없다: " + ", ".join(missing))
        assert settings.data_key_file is not None  # noqa: S101 (missing 검사로 보장)
        assert settings.google_client_id is not None  # noqa: S101
        assert settings.google_client_secret is not None  # noqa: S101
        try:
            cipher = EnvelopeCipher(load_kek_from_file(settings.data_key_file))
        except EnvelopeError as exc:
            raise SystemExit(str(exc)) from None
        resolver = GoogleConnectorResolver(
            db=db,
            cipher=cipher,
            clock=clock,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            token_url=settings.google_token_url,
        )
    else:
        assert settings.sim_snapshot_path is not None  # noqa: S101
        resolver = SimConnectorResolver(settings.sim_snapshot_path, settings.sim_account_id, clock)
    try:
        embedder = build_embedder(settings, llm_settings)
    except ValueError as exc:
        raise SystemExit(f"{exc} (테스트·Sim이면 ORBIT_EMBEDDING_PROVIDER=fake)") from None
    ids = Uuid7Generator(clock)
    deps = RunDeps(
        db=db,
        clock=clock,
        ids=ids,
        llm=build_llm(llm_settings),
        resolver=resolver,
        tz=ZoneInfo(settings.user_timezone),
        checkpoint_conninfo=make_url(url).set(drivername="postgresql").render_as_string(False),
        approval_ttl=timedelta(seconds=settings.approval_ttl_seconds),
        mail_index=MailIndexDeps(
            db=db,
            clock=clock,
            ids=ids,
            embedder=embedder,
            sync_interval=timedelta(seconds=settings.mail_index_sync_interval_seconds),
            query_stale=timedelta(seconds=settings.mail_index_query_stale_seconds),
            query_sync_timeout_s=settings.mail_index_query_sync_timeout_seconds,
        ),
    )
    app = build_worker_app(url, deps)

    async def serve() -> None:
        async with app.open_async():
            await WorkerPool(app).run()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
