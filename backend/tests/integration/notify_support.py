"""알림 통합 테스트 공통 도구 (ADR-0021). 데이터는 모두 합성 값이다."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from integration.mailindex_support import ACCOUNT, IndexEnv
from orbit.connectors.contracts import Attendee, CalendarEvent, EventDraft, EventPatch, new_event_id
from orbit.notify.event_sync import EventSyncResult, sync_event_reminders

CALENDAR = "cal-alice-main"  # Sim 월드의 기본(primary) 캘린더
MY_EMAIL = "alice@example.com"  # `create_connection`이 만드는 연결 계정


async def add_event(
    ix: IndexEnv,
    title: str,
    start: datetime,
    *,
    minutes: int = 30,
    all_day: bool = False,
    attendees: tuple[Attendee, ...] = (),
    calendar_id: str = CALENDAR,
) -> CalendarEvent:
    draft = EventDraft(
        title=title,
        start=start,
        end=start + (timedelta(days=1) if all_day else timedelta(minutes=minutes)),
        all_day=all_day,
        attendees=attendees,
    )
    return await ix.conns.calendar[ACCOUNT].create_event(calendar_id, new_event_id(ix.ids), draft)


async def move_event(ix: IndexEnv, event: CalendarEvent, start: datetime) -> CalendarEvent:
    patch = EventPatch(start=start, end=start + (event.end - event.start))
    return await ix.conns.calendar[ACCOUNT].update_event(
        event.calendar_id, event.id, patch, if_match=event.version
    )


async def sync(ix: IndexEnv, calendar: Any = None) -> EventSyncResult:
    return await sync_event_reminders(
        ix.db, ix.clock, ix.ids, calendar or ix.conns.calendar[ACCOUNT], ix.ws, ix.cid
    )


async def rows(ix: IndexEnv, where: str = "true") -> list[dict[str, Any]]:
    """이 워크스페이스의 알림 행(제목 순)."""
    async with ix.db.workspace_session(ix.ws) as s:
        found = await s.execute(
            text(
                "SELECT id, kind, title, due_at, status, read_at, event_id, event_start, run_id"  # noqa: S608
                f" FROM notification WHERE {where} ORDER BY title, id"
            )
        )
        return [dict(r._mapping) for r in found]


async def insert_notification(
    ix: IndexEnv,
    title: str,
    due_at: datetime,
    *,
    ws: uuid.UUID | None = None,
    kind: str = "reminder",
) -> uuid.UUID:
    """대화 리마인더 행을 직접 넣는다(도착 판정·목록 테스트용)."""
    nid = ix.ids.next()
    async with ix.db.workspace_session(ws or ix.ws) as s:
        await s.execute(
            text(
                "INSERT INTO notification (id, workspace_id, kind, title, due_at, status,"
                " created_at) VALUES (:id, :ws, :kind, :title, :due, 'scheduled', :now)"
            ),
            {
                "id": nid,
                "ws": ws or ix.ws,
                "kind": kind,
                "title": title,
                "due": due_at,
                "now": ix.clock.now(),
            },
        )
    return nid
