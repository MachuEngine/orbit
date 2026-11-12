"""색인 동기화 작업 등록 (ADR-0005, ADR-0006, ADR-0019 §6).

작업 인자는 `{"workspace_id", "connection_id"}`(둘 다 필수, UUID 문자열)뿐이다
(`orbit.records.jobs.validate_job_args`와 같은 규칙). ID만이라 제목·본문·주소·질의문을 실을 수
없다. 같은 연결의 대기 중인 동기화는 `queueing_lock`으로 하나만 둔다.
"""

import logging
import uuid
from collections.abc import Mapping

import procrastinate
from procrastinate import exceptions as procrastinate_exceptions
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.mailindex.deps import MailIndexDeps
from orbit.records.errors import InvalidJobArgs

logger = logging.getLogger(__name__)

# 색인 작업은 긴 작업(첫 동기화는 수 분)이라 run 작업과 큐를 나눈다. 워커 프로세스 하나가 큐마다
# 워커를 따로 돌려(`runflow.worker.WorkerPool`) 서로 막지 않는다.
MAIL_INDEX_QUEUE = "mail_index"
SYNC_MAIL_INDEX = "sync_mail_index"
MAIL_INDEX_TICK = "mail_index_tick"
TICK_CRON = "*/5 * * * *"  # 대상 선별 주기. 실제 동기화 간격은 `sync_interval`이 정한다
_ALLOWED_ARG_KEYS = frozenset({"workspace_id", "connection_id"})


def validate_sync_args(args: Mapping[str, object]) -> dict[str, str]:
    """작업 인자가 `workspace_id`와 `connection_id`(둘 다 UUID 문자열) 두 개뿐인지 검사한다.

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


def _lock(connection_id: uuid.UUID) -> str:
    return f"{SYNC_MAIL_INDEX}:{connection_id}"


async def defer_sync(
    session: AsyncSession,
    app: procrastinate.App,
    *,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> int | None:
    """세션의 트랜잭션 안에서 동기화 작업을 등록한다. 이미 대기 중이면 None."""
    args = validate_sync_args(
        {"workspace_id": str(workspace_id), "connection_id": str(connection_id)}
    )
    try:
        async with session.begin_nested():
            await session.connection()  # SAVEPOINT를 지금 발행시킨다(ADR-0005 조건 4)
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            return await app.configure_task(
                SYNC_MAIL_INDEX,
                connection=raw.driver_connection,
                queue=MAIL_INDEX_QUEUE,
                queueing_lock=_lock(connection_id),
            ).defer_async(**args)
    except procrastinate_exceptions.AlreadyEnqueued:
        return None


async def defer_sync_standalone(
    app: procrastinate.App, *, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> int | None:
    """도메인 변경 없이 동기화 작업을 등록한다(주기 작업이 쓴다). 이미 대기 중이면 None."""
    args = validate_sync_args(
        {"workspace_id": str(workspace_id), "connection_id": str(connection_id)}
    )
    try:
        return await app.configure_task(
            SYNC_MAIL_INDEX, queue=MAIL_INDEX_QUEUE, queueing_lock=_lock(connection_id)
        ).defer_async(**args)
    except procrastinate_exceptions.AlreadyEnqueued:
        return None


async def enqueue_due_syncs(mi: MailIndexDeps, app: procrastinate.App) -> int:
    """마지막 동기화가 `sync_interval`보다 오래된 연결의 동기화를 등록하고 등록 수를 돌려준다."""
    stale_before = mi.clock.now() - mi.sync_interval
    due = await mi.db.unscoped_mail_index_due_connections(
        stale_before, mi.clock.now() - mi.error_backoff
    )
    count = 0
    for workspace_id, connection_id in due:
        if await defer_sync_standalone(app, workspace_id=workspace_id, connection_id=connection_id):
            count += 1
    return count


__all__ = [
    "MAIL_INDEX_QUEUE",
    "MAIL_INDEX_TICK",
    "SYNC_MAIL_INDEX",
    "TICK_CRON",
    "defer_sync",
    "defer_sync_standalone",
    "enqueue_due_syncs",
    "validate_sync_args",
]
