"""일정 알림 동기화 (ADR-0021 §4).

기본 캘린더(`primary` 표시가 있는 캘린더)에서 지금부터 `SYNC_WINDOW`(2시간) 안에 시작하는 일정을
조회해 알림을 만들거나 고친다.
- 종일 일정, 취소된 일정, 내가(연결 계정 이메일) 거절한 일정은 알림 대상이 아니다. 이미 만든 도착 전
  알림은 `cancelled`로 바꾼다.
- 이미 시작한 일정은 새로 만들지 않는다. 시작이 `lead_minutes` 안으로 다가온 일정은 `due_at`을
  지금으로 맞춰 바로 도착하게 한다.
- 일정 시작 시각이 바뀌면 도착 전 알림의 `due_at`을 다시 정한다. 조회 창 밖으로 옮겨졌으면 도착 전
  알림을 지운다.
- 커넥터 오류는 상태에 남기지 않는다. 오류 종류만 로그에 남기고 끝나며 다음 주기(5분)에 다시
  시도한다(색인과 달리 백오프 상태가 없다). 권한 철회(`permission_revoked`)도 같은 방식으로
  건너뛴다.
- 보존 기간이 지난 알림(일정 시작 7일 뒤, 리마인더 도착 30일 뒤)도 이 워크스페이스 단위로 지운다.
  연결이 없거나 알림을 끈 워크스페이스는 주기 작업이 `purge_expired_notifications`로 지운다.
- 쓰기 트랜잭션은 연결 행을 `FOR SHARE`로 잠근다. 연결 해제가 겹치면 해제가 끝난 뒤 이 쓰기가
  거절돼 해제 뒤에 알림이 남지 않는다.
- 로그에는 개수·ID·오류 종류만 남긴다(일정 제목 금지).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from orbit.connectors.contracts import CalendarConnector, CalendarEvent, ConnectorError
from orbit.connectors.errors import connector_error_kind
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.mailindex.store import lock_active_connection
from orbit.notify import store
from orbit.notify.limits import (
    EVENT_REMINDER_RETENTION,
    MAX_TITLE_CHARS,
    REMINDER_RETENTION,
    SYNC_WINDOW,
    UNTITLED,
)

logger = logging.getLogger(__name__)

Outcome = Literal["synced", "no_connection", "disabled", "no_calendar", "error"]


@dataclass
class EventSyncResult:
    outcome: Outcome
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    removed: int = 0
    error_kind: str | None = None


def _title(event: CalendarEvent) -> str:
    return (event.title or "").strip()[:MAX_TITLE_CHARS] or UNTITLED


def _declined_by_me(event: CalendarEvent, my_email: str) -> bool:
    me = my_email.casefold()
    return any(
        a.email.casefold() == me and a.response_status == "declined" for a in event.attendees
    )


def _not_remindable(event: CalendarEvent, my_email: str) -> bool:
    return event.all_day or event.status == "cancelled" or _declined_by_me(event, my_email)


async def sync_event_reminders(
    db: AsyncDatabase,
    clock: Clock,
    ids: Uuid7Generator,
    calendar: CalendarConnector,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> EventSyncResult:
    ws, cid = workspace_id, connection_id
    async with db.workspace_session(ws) as s:
        account = await store.connection_account(s, ws, cid)
        setting = await store.get_setting(s, ws)
    if account is None or account[0] != "active":
        return EventSyncResult("no_connection")
    if not setting.event_reminders_enabled:
        return EventSyncResult("disabled")
    my_email = account[1]

    now: datetime = clock.now()
    try:
        calendars = await calendar.list_calendars()
        if not calendars:
            return EventSyncResult("no_calendar")
        target = next((c for c in calendars if c.primary), calendars[0])
        events = await calendar.list_events(
            target.id, now, now + SYNC_WINDOW, include_cancelled=True
        )
    except ConnectorError as exc:
        kind = connector_error_kind(exc)
        logger.warning("일정 알림 동기화 실패: connection_id=%s kind=%s", cid, kind)
        return EventSyncResult("error", error_kind=kind)

    result = EventSyncResult("synced")
    async with db.workspace_session(ws) as s:
        if not await lock_active_connection(s, ws, cid):
            return EventSyncResult("no_connection")
        # 조회하는 동안 설정이 바뀌었을 수 있다. 쓰기 트랜잭션 안에서 다시 읽는다.
        current = await store.get_setting(s, ws)
        if not current.event_reminders_enabled:
            return EventSyncResult("disabled")
        lead = timedelta(minutes=current.lead_minutes)
        dropped: list[str] = []
        for event in events:
            if _not_remindable(event, my_email):
                dropped.append(event.id)
                continue
            if event.start <= now:
                continue  # 이미 시작한 일정은 새로 만들지 않는다. 있던 알림은 그대로 둔다
            outcome = await store.upsert_event_reminder(
                s,
                ws=ws,
                cid=cid,
                calendar_id=target.id,
                event_id=event.id,
                notification_id=ids.next(),
                title=_title(event),
                event_start=event.start,
                due_at=max(event.start - lead, now),
                now=now,
            )
            if outcome == "created":
                result.created += 1
            elif outcome == "updated":
                result.updated += 1
        result.cancelled = await store.cancel_pending_events(s, ws, cid, target.id, dropped, now)
        result.removed = await store.delete_absent_pending(
            s, ws, cid, target.id, [e.id for e in events], now
        )
        await store.purge_expired(
            s,
            ws,
            event_before=now - EVENT_REMINDER_RETENTION,
            reminder_before=now - REMINDER_RETENTION,
        )
    logger.info(
        "일정 알림 동기화: connection_id=%s created=%d updated=%d cancelled=%d removed=%d",
        cid,
        result.created,
        result.updated,
        result.cancelled,
        result.removed,
    )
    return result
