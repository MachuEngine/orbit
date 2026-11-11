"""Procrastinate 작업 등록과 멈춘 작업 복구 (ADR-0005 조건 1·2·4, ADR-0006 결정 2).

- 작업 등록은 도메인 변경과 같은 SQLAlchemy 트랜잭션의 psycopg 연결로 한다(`connection=`).
  별도 연결로 등록하지 않는다. 롤백하면 도메인 행과 작업이 함께 사라진다.
- `queueing_lock`을 쓰면 SAVEPOINT 안에서 등록하고 `AlreadyEnqueued`를 처리한다. SAVEPOINT는
  `begin_nested()` 안에서 `session.connection()`을 먼저 불러야 실제로 발행된다.
- 작업 인자는 `{"run_id", "workspace_id"}`(둘 다 필수, UUID 문자열)뿐이다. 워커가 RLS 아래에서
  run을 읽으려면 워크스페이스를 알아야 한다. ID만이라 본문·주소·이름을 실을 수 없다(ADR-0006).
- 작업 큐 쪽 시각(heartbeat 등)은 Procrastinate가 DB `now()`로 기록한다. 도메인 시각이 아니다.
"""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import procrastinate
from procrastinate import exceptions as procrastinate_exceptions
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.records.errors import InvalidJobArgs

_ALLOWED_ARG_KEYS = frozenset({"run_id", "workspace_id"})


def make_procrastinate_app(database_url: str) -> procrastinate.App:
    """SQLAlchemy URL(`postgresql+psycopg://...`)로 Procrastinate 앱을 만든다.

    작업 처리기 등록(`import_paths`)과 워커 구성은 4C가 한다. 이 앱은 이름만으로 작업을 등록한다.
    """
    conninfo = make_url(database_url).set(drivername="postgresql").render_as_string(False)
    return procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=conninfo))


def validate_job_args(args: Mapping[str, object]) -> dict[str, str]:
    """작업 인자가 `run_id`와 `workspace_id`(둘 다 UUID 문자열) 두 개뿐인지 검사한다.

    어긋나면 `InvalidJobArgs`. 예외 메시지에는 키 이름만 담고 값은 담지 않는다.
    """
    extra = set(args) - _ALLOWED_ARG_KEYS
    if extra:
        raise InvalidJobArgs(f"허용되지 않은 작업 인자 키: {sorted(extra)}")
    result: dict[str, str] = {}
    for key in sorted(_ALLOWED_ARG_KEYS):
        value = args.get(key)
        if not isinstance(value, str):
            raise InvalidJobArgs(f"작업 인자 {key}는 UUID 문자열이어야 한다")
        try:
            uuid.UUID(value)
        except ValueError:
            raise InvalidJobArgs(f"작업 인자 {key}는 UUID 문자열이어야 한다") from None
        result[key] = value
    return result


async def defer_job(
    session: AsyncSession,
    app: procrastinate.App,
    *,
    task_name: str,
    args: Mapping[str, object],
    queue: str = "default",
    queueing_lock: str | None = None,
) -> int | None:
    """세션의 트랜잭션 안에서 작업을 등록하고 job id를 돌려준다.

    `queueing_lock`이 이미 대기 중이면 None을 돌려준다(바깥 트랜잭션은 유지된다).
    커밋·롤백은 호출자(세션) 몫이다.
    """
    job_args = validate_job_args(args)
    if queueing_lock is None:
        return await _defer(session, app, task_name, job_args, queue, None)
    try:
        async with session.begin_nested():
            await session.connection()  # SAVEPOINT를 지금 발행시킨다(ADR-0005 조건 4)
            return await _defer(session, app, task_name, job_args, queue, queueing_lock)
    except procrastinate_exceptions.AlreadyEnqueued:
        return None


async def _defer(
    session: AsyncSession,
    app: procrastinate.App,
    task_name: str,
    job_args: dict[str, str],
    queue: str,
    queueing_lock: str | None,
) -> int:
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    options: dict[str, Any] = {"connection": raw.driver_connection, "queue": queue}
    if queueing_lock is not None:
        options["queueing_lock"] = queueing_lock
    return await app.configure_task(task_name, **options).defer_async(**job_args)


async def recover_stalled_jobs(
    app: procrastinate.App,
    *,
    seconds_since_heartbeat: float,
    queue: str | None = None,
    retry_at: datetime | None = None,
) -> list[int]:
    """heartbeat가 끊긴 워커의 `doing` 작업을 `todo`로 되돌리고 job id 목록을 돌려준다.

    Procrastinate는 멈춘 작업을 자동 복구하지 않는다(ADR-0005 조건 2). 되돌린 작업은 처음부터 다시
    실행되므로 처리기는 멱등해야 한다(외부 쓰기는 execution 선기록). 주기 작업 등록은 4C가 한다.
    `retry_at`을 주지 않으면(None) 즉시 재시도한다(Procrastinate가 DB 시각으로 처리).
    """
    async with app.open_async():
        jobs = list(
            await app.job_manager.get_stalled_jobs(
                queue=queue, seconds_since_heartbeat=seconds_since_heartbeat
            )
        )
        for job in jobs:
            await app.job_manager.retry_job(job, retry_at=retry_at)
    return [job.id for job in jobs if job.id is not None]
