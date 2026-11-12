"""알림 설정·목록·읽음·취소 (ADR-0021 §3·§6). API는 이 함수만 부른다.

"도착"은 저장하지 않는다. `status = 'scheduled' AND due_at <= now`인 알림이 도착한 알림이고, 시계는
호출자가 준 `Clock`이다(`datetime.now()`를 직접 부르지 않는다).
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from orbit.core.clock import Clock
from orbit.db.session import AsyncDatabase
from orbit.notify import store
from orbit.notify.limits import ARRIVED_LIMIT, LEAD_MINUTES_CHOICES, UPCOMING_LIMIT
from orbit.notify.store import NotificationRow, Setting


class InvalidSetting(ValueError):
    """알림 시점이 허용 값(5·10·15·30·60분)이 아니다."""


@dataclass(frozen=True)
class Arrived:
    notifications: tuple[NotificationRow, ...]
    unread_count: int


async def get_settings(db: AsyncDatabase, clock: Clock, ws: uuid.UUID) -> Setting:
    async with db.workspace_session(ws) as s:
        return await store.get_setting(s, ws)


async def save_settings(
    db: AsyncDatabase, clock: Clock, ws: uuid.UUID, *, enabled: bool, lead_minutes: int
) -> Setting:
    """설정을 저장하고 같은 트랜잭션에서 도착 전 일정 알림을 맞춘다.

    - 끄면 도착 전 일정 알림을 지운다(다시 켜면 다음 동기화가 새로 만든다). 도착한 알림은 남는다.
    - 켜져 있으면 도착 전 일정 알림의 `due_at`을 새 알림 시점으로 다시 계산한다.
    """
    if lead_minutes not in LEAD_MINUTES_CHOICES:
        raise InvalidSetting("lead_minutes")
    now = clock.now()
    async with db.workspace_session(ws) as s:
        await store.upsert_setting(s, ws, enabled=enabled, lead_minutes=lead_minutes, now=now)
        if enabled:
            await store.recompute_pending_due(s, ws, lead_minutes, now)
        else:
            await store.delete_pending_event_reminders(s, ws, now)
    return Setting(enabled, lead_minutes)


async def list_arrived(db: AsyncDatabase, clock: Clock, ws: uuid.UUID) -> Arrived:
    now = clock.now()
    async with db.workspace_session(ws) as s:
        rows = await store.list_arrived(s, ws, now, ARRIVED_LIMIT)
        unread = await store.count_unread(s, ws, now)
    return Arrived(tuple(rows), unread)


async def list_upcoming(db: AsyncDatabase, clock: Clock, ws: uuid.UUID) -> list[NotificationRow]:
    async with db.workspace_session(ws) as s:
        return await store.list_upcoming(s, ws, clock.now(), UPCOMING_LIMIT)


async def mark_read(db: AsyncDatabase, clock: Clock, ws: uuid.UUID, nid: uuid.UUID) -> bool:
    async with db.workspace_session(ws) as s:
        return await store.mark_read(s, ws, nid, clock.now())


async def mark_all_read(db: AsyncDatabase, clock: Clock, ws: uuid.UUID) -> int:
    async with db.workspace_session(ws) as s:
        return await store.mark_all_read(s, ws, clock.now())


CancelResult = Literal["cancelled", "not_found", "not_cancellable"]


async def cancel(db: AsyncDatabase, clock: Clock, ws: uuid.UUID, nid: uuid.UUID) -> CancelResult:
    async with db.workspace_session(ws) as s:
        result = await store.cancel_upcoming(s, ws, nid, clock.now())
    return result  # type: ignore[return-value]
