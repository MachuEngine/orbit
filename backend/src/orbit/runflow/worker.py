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
import math
import uuid
from datetime import timedelta

import procrastinate
from procrastinate.retry import BaseRetryStrategy, RetryDecision

from orbit.records.errors import InvalidJobArgs
from orbit.records.jobs import make_procrastinate_app, recover_stalled_jobs, validate_job_args
from orbit.runflow.deps import RunDeps
from orbit.runflow.graph import advance_run, expire_if_due
from orbit.runflow.jobs import EXECUTE_RUN, EXPIRE_APPROVALS, RECOVER_STALLED, RESUME_RUN
from orbit.runflow.nodes import ExecutionNotSettled

logger = logging.getLogger(__name__)

__all__ = ["EXECUTE_RUN", "EXPIRE_APPROVALS", "RECOVER_STALLED", "RESUME_RUN", "build_worker_app"]

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

    return app


def main() -> None:
    """개발용 워커 진입점: `python -m orbit.runflow.worker`.

    LLM은 `ORBIT_LLM_*`·`ORBIT_OPENROUTER_API_KEY` 설정으로, 커넥터는 개발용 Sim 스냅샷
    (`ORBIT_SIM_SNAPSHOT_PATH`)으로 만든다. 6단계에서 Google Adapter 해석기로 바꾼다.
    """
    from zoneinfo import ZoneInfo

    from pydantic import ValidationError
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
    try:
        llm_settings = get_llm_settings()
    except ValidationError:
        raise SystemExit(
            "LLM 설정이 없어 워커를 시작하지 않는다. 필요한 환경 변수: ORBIT_LLM_MODEL_ID, "
            "ORBIT_LLM_PROVIDER_ORDER, ORBIT_OPENROUTER_API_KEY (backend/dev.env 참고)"
        ) from None
    url = settings.database_url.get_secret_value()
    clock = SystemClock()
    deps = RunDeps(
        db=AsyncDatabase.from_url(url),
        clock=clock,
        ids=Uuid7Generator(clock),
        llm=OpenRouterLLM(llm_settings),
        resolver=SimConnectorResolver(settings.sim_snapshot_path, settings.sim_account_id, clock),
        tz=ZoneInfo(settings.user_timezone),
        checkpoint_conninfo=make_url(url).set(drivername="postgresql").render_as_string(False),
        approval_ttl=timedelta(seconds=settings.approval_ttl_seconds),
    )
    build_worker_app(url, deps).run_worker(queues=["default"])


if __name__ == "__main__":
    main()
