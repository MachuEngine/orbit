"""작업 이름과 예약 등록 (ADR-0005 조건 1).

`defer_at`은 `orbit.records.jobs.defer_job`처럼 도메인 변경과 같은 트랜잭션의 psycopg 연결로 작업을
등록하되 실행 시각(`schedule_at`)을 지정한다. 작업 인자는 `run_id`, `workspace_id`뿐이다.
"""

import uuid
from datetime import datetime

import procrastinate
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.records.jobs import validate_job_args

EXECUTE_RUN = "execute_run"
RESUME_RUN = "resume_run"
EXPIRE_APPROVALS = "expire_approvals"
RECOVER_STALLED = "recover_stalled_jobs"


async def defer_at(
    session: AsyncSession,
    app: procrastinate.App,
    *,
    task_name: str,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    at: datetime,
) -> int:
    args = validate_job_args({"run_id": str(run_id), "workspace_id": str(workspace_id)})
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    configured = app.configure_task(
        task_name, connection=raw.driver_connection, schedule_at=at, queue="default"
    )
    return await configured.defer_async(**args)
