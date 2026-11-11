"""Procrastinate 워커: 작업 정의와 실행 진입점 (ADR-0005 조건 1~3, ADR-0006 결정 2).

- 작업 인자는 `run_id`, `workspace_id`(UUID 문자열)뿐이다.
- `execute_run`(처음 시작)과 `resume_run`(승인 결정 뒤 재개)은 멱등 처리기 `advance_run`을 쓴다.
  run이 없거나 종료 상태이거나 승인이 아직 `pending`이면 아무것도 하지 않고 정상 종료한다.
- 처리기가 예외로 끝나면 재시도한다(작업 재실행은 처음부터이므로 멱등해야 한다). 외부 쓰기는
  `execution` 선기록과 lease(ADR-0003 조건 1)가 중복을 막는다.
- 멈춘 작업 복구 주기 작업(ADR-0005 조건 2): heartbeat가 끊긴 `doing` 작업을 `todo`로 되돌린다.
  복구 작업은 워커가 쓰는 연결과 따로 만든 앱으로 실행한다(열려 있는 앱을 다시 열지 않기 위해서다).
"""

import logging
import uuid
from datetime import timedelta

import procrastinate

from orbit.records.errors import InvalidJobArgs
from orbit.records.jobs import make_procrastinate_app, recover_stalled_jobs, validate_job_args
from orbit.runflow.deps import RunDeps
from orbit.runflow.graph import advance_run

logger = logging.getLogger(__name__)

EXECUTE_RUN = "execute_run"
RESUME_RUN = "resume_run"
RECOVER_STALLED = "recover_stalled_jobs"

STALLED_AFTER_SECONDS = 60.0  # 워커 heartbeat 간격(10초)과 stalled_worker_timeout(30초)보다 길게
RETRY_MAX_ATTEMPTS = 5
RETRY_WAIT_SECONDS = 30


def build_worker_app(
    database_url: str,
    deps: RunDeps,
    *,
    retry_wait_seconds: int = RETRY_WAIT_SECONDS,
) -> procrastinate.App:
    """작업 처리기를 등록한 Procrastinate 앱을 만든다."""
    app = make_procrastinate_app(database_url)
    retry = procrastinate.RetryStrategy(max_attempts=RETRY_MAX_ATTEMPTS, wait=retry_wait_seconds)

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

    @app.periodic(cron="* * * * *")
    @app.task(name=RECOVER_STALLED, queueing_lock=RECOVER_STALLED)
    async def recover_stalled(timestamp: int) -> None:
        recovered = await recover_stalled_jobs(
            make_procrastinate_app(database_url), seconds_since_heartbeat=STALLED_AFTER_SECONDS
        )
        if recovered:
            logger.warning("멈춘 작업 %d개를 되돌렸다", len(recovered))

    return app


def main() -> None:
    """개발용 워커 진입점: `python -m orbit.runflow.worker`.

    LLM은 `ORBIT_LLM_*`·`ORBIT_OPENROUTER_API_KEY` 설정으로, 커넥터는 개발용 Sim 스냅샷
    (`ORBIT_SIM_SNAPSHOT_PATH`)으로 만든다. 6단계에서 Google Adapter 해석기로 바꾼다.
    """
    from zoneinfo import ZoneInfo

    from sqlalchemy.engine import make_url

    from orbit.core.clock import SystemClock
    from orbit.core.config import get_llm_settings, get_settings
    from orbit.core.ids import Uuid7Generator
    from orbit.db.session import AsyncDatabase
    from orbit.llm.openrouter import OpenRouterLLM
    from orbit.runflow.deps import SimConnectorResolver

    settings = get_settings()
    if settings.env != "dev" or settings.sim_snapshot_path is None:
        raise SystemExit("개발용 워커는 ORBIT_ENV=dev와 ORBIT_SIM_SNAPSHOT_PATH가 필요하다")
    url = settings.database_url.get_secret_value()
    clock = SystemClock()
    deps = RunDeps(
        db=AsyncDatabase.from_url(url),
        clock=clock,
        ids=Uuid7Generator(clock),
        llm=OpenRouterLLM(get_llm_settings()),
        resolver=SimConnectorResolver(settings.sim_snapshot_path, settings.sim_account_id, clock),
        tz=ZoneInfo(settings.user_timezone),
        checkpoint_conninfo=make_url(url).set(drivername="postgresql").render_as_string(False),
        approval_ttl=timedelta(seconds=settings.approval_ttl_seconds),
    )
    build_worker_app(url, deps).run_worker(queues=["default"])


if __name__ == "__main__":
    main()
