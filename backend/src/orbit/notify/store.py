"""알림 테이블 접근 (마이그레이션 0010). 모든 함수는 워크스페이스 세션을 받고 `workspace_id`로
거른다. 제목은 외부 문자열일 수 있으므로 로그에 남기지 않는다."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.notify.limits import (
    DEFAULT_EVENT_REMINDERS_ENABLED,
    DEFAULT_LEAD_MINUTES,
)

KIND_EVENT = "event_reminder"
KIND_REMINDER = "reminder"


@dataclass(frozen=True)
class Setting:
    event_reminders_enabled: bool
    lead_minutes: int


@dataclass(frozen=True)
class NotificationRow:
    id: uuid.UUID
    kind: str
    title: str
    due_at: datetime
    event_start: datetime | None
    read_at: datetime | None
    created_at: datetime


_COLUMNS = "id, kind, title, due_at, event_start, read_at, created_at"


def _row(r: object) -> NotificationRow:
    return NotificationRow(
        id=r.id,  # type: ignore[attr-defined]
        kind=r.kind,  # type: ignore[attr-defined]
        title=r.title,  # type: ignore[attr-defined]
        due_at=r.due_at,  # type: ignore[attr-defined]
        event_start=r.event_start,  # type: ignore[attr-defined]
        read_at=r.read_at,  # type: ignore[attr-defined]
        created_at=r.created_at,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------- 설정


async def get_setting(session: AsyncSession, ws: uuid.UUID) -> Setting:
    """설정 행이 없으면 기본값(켬, 10분 전)이다."""
    row = (
        await session.execute(
            text(
                "SELECT event_reminders_enabled, lead_minutes FROM notification_setting"
                " WHERE workspace_id = :ws"
            ),
            {"ws": ws},
        )
    ).first()
    if row is None:
        return Setting(DEFAULT_EVENT_REMINDERS_ENABLED, DEFAULT_LEAD_MINUTES)
    return Setting(bool(row[0]), int(row[1]))


async def upsert_setting(
    session: AsyncSession, ws: uuid.UUID, *, enabled: bool, lead_minutes: int, now: datetime
) -> None:
    await session.execute(
        text(
            "INSERT INTO notification_setting"
            " (workspace_id, event_reminders_enabled, lead_minutes, updated_at)"
            " VALUES (:ws, :enabled, :lead, :now)"
            " ON CONFLICT (workspace_id) DO UPDATE SET"
            " event_reminders_enabled = EXCLUDED.event_reminders_enabled,"
            " lead_minutes = EXCLUDED.lead_minutes, updated_at = EXCLUDED.updated_at"
        ),
        {"ws": ws, "enabled": enabled, "lead": lead_minutes, "now": now},
    )


async def delete_pending_event_reminders(
    session: AsyncSession, ws: uuid.UUID, now: datetime
) -> int:
    """아직 도착하지 않은 일정 알림을 지운다(일정 알림을 끌 때). 도착한 알림은 남긴다."""
    result = await session.execute(
        text(
            "DELETE FROM notification WHERE workspace_id = :ws AND kind = 'event_reminder'"
            " AND status = 'scheduled' AND due_at > :now"
        ),
        {"ws": ws, "now": now},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def recompute_pending_due(
    session: AsyncSession, ws: uuid.UUID, lead_minutes: int, now: datetime
) -> int:
    """아직 도착하지 않은 일정 알림의 `due_at`을 새 알림 시점으로 다시 계산한다.

    시작 `lead_minutes` 전이 이미 지났으면 지금으로 맞춘다(바로 도착한다).
    """
    result = await session.execute(
        text(
            "UPDATE notification SET due_at = GREATEST("
            " event_start - make_interval(mins => :lead), CAST(:now AS timestamptz))"
            " WHERE workspace_id = :ws AND kind = 'event_reminder' AND status = 'scheduled'"
            " AND due_at > :now"
        ),
        {"ws": ws, "lead": lead_minutes, "now": now},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 목록·읽음·취소


async def list_arrived(
    session: AsyncSession, ws: uuid.UUID, now: datetime, limit: int
) -> list[NotificationRow]:
    rows = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM notification WHERE workspace_id = :ws"  # noqa: S608
            " AND status = 'scheduled' AND due_at <= :now ORDER BY due_at DESC, id DESC LIMIT :n"
        ),
        {"ws": ws, "now": now, "n": limit},
    )
    return [_row(r) for r in rows]


async def count_unread(session: AsyncSession, ws: uuid.UUID, now: datetime) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification WHERE workspace_id = :ws"
                    " AND status = 'scheduled' AND due_at <= :now AND read_at IS NULL"
                ),
                {"ws": ws, "now": now},
            )
        ).scalar_one()
    )


async def list_upcoming(
    session: AsyncSession, ws: uuid.UUID, now: datetime, limit: int
) -> list[NotificationRow]:
    rows = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM notification WHERE workspace_id = :ws"  # noqa: S608
            " AND status = 'scheduled' AND due_at > :now ORDER BY due_at, id LIMIT :n"
        ),
        {"ws": ws, "now": now, "n": limit},
    )
    return [_row(r) for r in rows]


async def mark_read(
    session: AsyncSession, ws: uuid.UUID, notification_id: uuid.UUID, now: datetime
) -> bool:
    """도착한 알림을 읽음으로 바꾼다(이미 읽었으면 그대로 둔다). 도착한 알림이 없으면 False."""
    found = (
        await session.execute(
            text(
                "UPDATE notification SET read_at = COALESCE(read_at, :now)"
                " WHERE id = :id AND workspace_id = :ws AND status = 'scheduled'"
                " AND due_at <= :now RETURNING id"
            ),
            {"id": notification_id, "ws": ws, "now": now},
        )
    ).first()
    return found is not None


async def mark_all_read(session: AsyncSession, ws: uuid.UUID, now: datetime) -> int:
    result = await session.execute(
        text(
            "UPDATE notification SET read_at = :now WHERE workspace_id = :ws"
            " AND status = 'scheduled' AND due_at <= :now AND read_at IS NULL"
        ),
        {"ws": ws, "now": now},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def cancel_upcoming(
    session: AsyncSession, ws: uuid.UUID, notification_id: uuid.UUID, now: datetime
) -> str:
    """예정된 알림을 취소한다. `cancelled`, `not_found`(없거나 다른 워크스페이스),
    `not_cancellable`(이미 도착했거나 취소됐다) 중 하나를 돌려준다."""
    changed = (
        await session.execute(
            text(
                "UPDATE notification SET status = 'cancelled' WHERE id = :id"
                " AND workspace_id = :ws AND status = 'scheduled' AND due_at > :now RETURNING id"
            ),
            {"id": notification_id, "ws": ws, "now": now},
        )
    ).first()
    if changed is not None:
        return "cancelled"
    exists = (
        await session.execute(
            text("SELECT 1 FROM notification WHERE id = :id AND workspace_id = :ws"),
            {"id": notification_id, "ws": ws},
        )
    ).first()
    return "not_cancellable" if exists is not None else "not_found"


# ---------------------------------------------------------------- 대화 리마인더


async def count_pending_reminders(session: AsyncSession, ws: uuid.UUID, now: datetime) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification WHERE workspace_id = :ws"
                    " AND kind = 'reminder' AND status = 'scheduled' AND due_at > :now"
                ),
                {"ws": ws, "now": now},
            )
        ).scalar_one()
    )


async def reminder_exists(session: AsyncSession, ws: uuid.UUID, notification_id: uuid.UUID) -> bool:
    found = (
        await session.execute(
            text("SELECT 1 FROM notification WHERE id = :id AND workspace_id = :ws"),
            {"id": notification_id, "ws": ws},
        )
    ).first()
    return found is not None


async def insert_reminder(
    session: AsyncSession,
    *,
    ws: uuid.UUID,
    notification_id: uuid.UUID,
    title: str,
    due_at: datetime,
    run_id: uuid.UUID,
    now: datetime,
) -> bool:
    """대화 리마인더를 저장한다. 같은 ID가 이미 있으면(노드 재실행) 저장하지 않고 False."""
    found = (
        await session.execute(
            text(
                "INSERT INTO notification (id, workspace_id, kind, title, due_at, status,"
                " created_at, run_id) VALUES (:id, :ws, 'reminder', :title, :due, 'scheduled',"
                " :now, :run) ON CONFLICT (id) DO NOTHING RETURNING id"
            ),
            {
                "id": notification_id,
                "ws": ws,
                "title": title,
                "due": due_at,
                "now": now,
                "run": run_id,
            },
        )
    ).first()
    return found is not None


# ---------------------------------------------------------------- 일정 알림 동기화


async def connection_account(
    session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID
) -> tuple[str, str] | None:
    """(상태, 계정 이메일). 연결이 없으면 None."""
    row = (
        await session.execute(
            text(
                "SELECT status, account_email FROM connection"
                " WHERE id = :cid AND workspace_id = :ws"
            ),
            {"cid": cid, "ws": ws},
        )
    ).first()
    return None if row is None else (str(row[0]), str(row[1]))


async def upsert_event_reminder(
    session: AsyncSession,
    *,
    ws: uuid.UUID,
    cid: uuid.UUID,
    calendar_id: str,
    event_id: str,
    notification_id: uuid.UUID,
    title: str,
    event_start: datetime,
    due_at: datetime,
    now: datetime,
) -> str:
    """일정 알림을 만들거나 고친다. `created`, `updated`, `skipped`(취소된 알림) 중 하나.

    - 아직 도착하지 않은 알림(`due_at > now`)은 시작 시각이 바뀌면 `due_at`도 새로 정한다.
    - 이미 도착한 알림은 제목·일정 시작 시각만 고치고 `due_at`은 그대로 둔다(다시 울리지 않는다).
    - 취소된 알림(사용자가 취소했거나 일정이 취소됨)은 되살리지 않는다.
    """
    row = (
        await session.execute(
            text(
                "INSERT INTO notification (id, workspace_id, kind, title, due_at, status,"
                " created_at, connection_id, calendar_id, event_id, event_start)"
                " VALUES (:id, :ws, 'event_reminder', :title, :due, 'scheduled', :now, :cid,"
                " :cal, :event, :start)"
                " ON CONFLICT (workspace_id, connection_id, calendar_id, event_id) DO UPDATE SET"
                " title = EXCLUDED.title, event_start = EXCLUDED.event_start,"
                " due_at = CASE WHEN notification.due_at > :now THEN EXCLUDED.due_at"
                " ELSE notification.due_at END"
                " WHERE notification.status = 'scheduled'"
                " RETURNING (xmax = 0) AS inserted"
            ),
            {
                "id": notification_id,
                "ws": ws,
                "title": title,
                "due": due_at,
                "now": now,
                "cid": cid,
                "cal": calendar_id,
                "event": event_id,
                "start": event_start,
            },
        )
    ).first()
    if row is None:
        return "skipped"
    return "created" if row[0] else "updated"


async def cancel_pending_events(
    session: AsyncSession,
    ws: uuid.UUID,
    cid: uuid.UUID,
    calendar_id: str,
    event_ids: Sequence[str],
    now: datetime,
) -> int:
    """일정이 취소·거절·종일로 바뀌었으면 아직 도착하지 않은 알림을 `cancelled`로 바꾼다."""
    if not event_ids:
        return 0
    result = await session.execute(
        text(
            "UPDATE notification SET status = 'cancelled' WHERE workspace_id = :ws"
            " AND connection_id = :cid AND calendar_id = :cal AND kind = 'event_reminder'"
            " AND status = 'scheduled' AND due_at > :now AND event_id = ANY(CAST(:ids AS text[]))"
        ),
        {"ws": ws, "cid": cid, "cal": calendar_id, "now": now, "ids": list(event_ids)},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def delete_absent_pending(
    session: AsyncSession,
    ws: uuid.UUID,
    cid: uuid.UUID,
    calendar_id: str,
    present_event_ids: Sequence[str],
    now: datetime,
) -> int:
    """조회 창에 더 이상 없는 일정(창 밖으로 옮겨짐)의 도착 전 알림을 지운다.

    지우므로 일정이 다시 창 안에 들어오면 새로 만들어진다.
    """
    result = await session.execute(
        text(
            "DELETE FROM notification WHERE workspace_id = :ws AND connection_id = :cid"
            " AND calendar_id = :cal AND kind = 'event_reminder' AND status = 'scheduled'"
            " AND due_at > :now AND NOT (event_id = ANY(CAST(:ids AS text[])))"
        ),
        {"ws": ws, "cid": cid, "cal": calendar_id, "now": now, "ids": list(present_event_ids)},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def purge_expired(
    session: AsyncSession, ws: uuid.UUID, *, event_before: datetime, reminder_before: datetime
) -> int:
    """보존 기간이 지난 알림을 지운다(ADR-0021 §7). 일정 알림은 일정 시작이 `event_before`보다
    앞선 것, 대화 리마인더는 `due_at`이 `reminder_before`보다 앞선 것(상태 무관)이다."""
    result = await session.execute(
        text(
            "DELETE FROM notification WHERE workspace_id = :ws AND"
            " ((kind = 'event_reminder' AND event_start < :eb)"
            " OR (kind = 'reminder' AND due_at < :rb))"
        ),
        {"ws": ws, "eb": event_before, "rb": reminder_before},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def purge_connection_reminders(session: AsyncSession, ws: uuid.UUID, cid: uuid.UUID) -> int:
    """연결의 일정 알림을 모두 지운다(ADR-0021 §7). 호출자의 트랜잭션 안에서 쓴다.

    대화 리마인더는 연결과 무관하므로 남는다.
    """
    result = await session.execute(
        text(
            "DELETE FROM notification WHERE workspace_id = :ws AND connection_id = :cid"
            " AND kind = 'event_reminder'"
        ),
        {"ws": ws, "cid": cid},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
