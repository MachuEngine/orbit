"""일정 알림 작업 등록 (ADR-0005, ADR-0006, ADR-0021 §4).

작업 인자는 `{"workspace_id", "connection_id"}`(둘 다 필수, UUID 문자열)뿐이다. ID만이라 일정 제목·
알림 문구를 실을 수 없다. 같은 연결의 대기 중인 동기화는 `queueing_lock`으로 하나만 둔다.
알림 작업은 색인·run과 큐를 나눈다(`WorkerPool`이 큐마다 워커를 따로 돌린다).
"""

import logging
import uuid

import procrastinate
from procrastinate import exceptions as procrastinate_exceptions

from orbit.core.clock import Clock
from orbit.db.session import AsyncDatabase
from orbit.mailindex.jobs import validate_sync_args
from orbit.notify import store
from orbit.notify.limits import EVENT_REMINDER_RETENTION, REMINDER_RETENTION

logger = logging.getLogger(__name__)

NOTIFY_QUEUE = "notify"
SYNC_EVENT_REMINDERS = "sync_event_reminders"
SCHEDULE_EVENT_REMINDERS = "schedule_event_reminders"

__all__ = [
    "NOTIFY_QUEUE",
    "SCHEDULE_EVENT_REMINDERS",
    "SYNC_EVENT_REMINDERS",
    "defer_event_sync",
    "enqueue_event_syncs",
    "purge_expired_notifications",
    "validate_sync_args",
]


async def defer_event_sync(
    app: procrastinate.App, *, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> int | None:
    """일정 알림 동기화 작업을 등록한다. 이미 대기 중이면 None."""
    args = validate_sync_args(
        {"workspace_id": str(workspace_id), "connection_id": str(connection_id)}
    )
    try:
        return await app.configure_task(
            SYNC_EVENT_REMINDERS,
            queue=NOTIFY_QUEUE,
            queueing_lock=f"{SYNC_EVENT_REMINDERS}:{connection_id}",
        ).defer_async(**args)
    except procrastinate_exceptions.AlreadyEnqueued:
        return None


async def purge_expired_notifications(db: AsyncDatabase, clock: Clock) -> int:
    """보존 기간이 지난 알림을 워크스페이스별 트랜잭션으로 지우고 지운 개수를 돌려준다(ADR-0021 §7).

    연결이 없거나 일정 알림이 꺼진 워크스페이스도 대상이다. 선별 함수는 ID만 돌려준다.
    """
    now = clock.now()
    event_before = now - EVENT_REMINDER_RETENTION
    reminder_before = now - REMINDER_RETENTION
    total = 0
    for workspace_id in await db.unscoped_notification_expired_workspaces(
        event_before, reminder_before
    ):
        async with db.workspace_session(workspace_id) as s:
            total += await store.purge_expired(
                s, workspace_id, event_before=event_before, reminder_before=reminder_before
            )
    return total


async def enqueue_event_syncs(db: AsyncDatabase, app: procrastinate.App) -> int:
    """일정 알림을 켠 워크스페이스의 활성 연결마다 동기화를 등록하고 등록 수를 돌려준다."""
    count = 0
    for workspace_id, connection_id in await db.unscoped_event_reminder_due_connections():
        if await defer_event_sync(app, workspace_id=workspace_id, connection_id=connection_id):
            count += 1
    return count
