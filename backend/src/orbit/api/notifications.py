"""알림 API (ADR-0021 §6). 모든 경로는 `workspace_session` 안에서 동작하고 다른 워크스페이스의
알림은 404다. 상태를 바꾸는 요청은 CSRF를 확인한다(`current_session`).

- `GET /api/notifications`: 도착한 알림(`scheduled`이고 `due_at <= 지금`), 최근순 최대 50개,
  읽음 여부와 읽지 않은 전체 수(`unread_count`).
- `GET /api/notifications/upcoming`: 예정된 알림(`due_at > 지금`), 시각순.
- `POST /api/notifications/{id}/read`, `POST /api/notifications/read-all`: 읽음 처리(멱등).
- `DELETE /api/notifications/{id}`: 예정된 알림만 취소한다. 이미 도착했거나 취소된 알림은 409다.
- `GET·PUT /api/notification-settings`: 일정 알림 사용 여부와 알림 시점(5·10·15·30·60분 전).
알림 제목은 외부 문자열일 수 있다. 화면이 텍스트로만 그린다.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, StrictBool

from orbit.api.auth import current_session
from orbit.auth import SessionInfo
from orbit.core.clock import Clock
from orbit.db.session import AsyncDatabase
from orbit.notify import service
from orbit.notify.store import NotificationRow


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_reminders_enabled: StrictBool
    lead_minutes: Literal[5, 10, 15, 30, 60]


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "not_found")


def _item(row: NotificationRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.title,
        "due_at": row.due_at.isoformat(),
        "event_start": None if row.event_start is None else row.event_start.isoformat(),
        "read": row.read_at is not None,
        "created_at": row.created_at.isoformat(),
    }


def build_notifications_router() -> APIRouter:
    router = APIRouter()
    Session = Annotated[SessionInfo, Depends(current_session)]  # noqa: N806

    def _db(request: Request) -> AsyncDatabase:
        db: AsyncDatabase = request.app.state.db
        return db

    def _clock(request: Request) -> Clock:
        clock: Clock = request.app.state.clock
        return clock

    @router.get("/notifications")
    async def arrived(request: Request, session: Session) -> dict[str, Any]:
        found = await service.list_arrived(_db(request), _clock(request), session.workspace_id)
        return {
            "notifications": [_item(r) for r in found.notifications],
            "unread_count": found.unread_count,
        }

    @router.get("/notifications/upcoming")
    async def upcoming(request: Request, session: Session) -> dict[str, Any]:
        rows = await service.list_upcoming(_db(request), _clock(request), session.workspace_id)
        return {"notifications": [_item(r) for r in rows]}

    @router.post("/notifications/read-all")
    async def read_all(request: Request, session: Session) -> dict[str, Any]:
        count = await service.mark_all_read(_db(request), _clock(request), session.workspace_id)
        return {"updated": count}

    @router.post("/notifications/{notification_id}/read")
    async def read_one(
        notification_id: uuid.UUID, request: Request, session: Session
    ) -> dict[str, Any]:
        found = await service.mark_read(
            _db(request), _clock(request), session.workspace_id, notification_id
        )
        if not found:
            raise _not_found()
        return {"id": str(notification_id)}

    @router.delete("/notifications/{notification_id}")
    async def cancel(
        notification_id: uuid.UUID, request: Request, session: Session
    ) -> dict[str, Any]:
        result = await service.cancel(
            _db(request), _clock(request), session.workspace_id, notification_id
        )
        if result == "not_found":
            raise _not_found()
        if result == "not_cancellable":
            raise HTTPException(status.HTTP_409_CONFLICT, "not_cancellable")
        return {"id": str(notification_id)}

    @router.get("/notification-settings")
    async def get_settings(request: Request, session: Session) -> dict[str, Any]:
        found = await service.get_settings(_db(request), _clock(request), session.workspace_id)
        return {
            "event_reminders_enabled": found.event_reminders_enabled,
            "lead_minutes": found.lead_minutes,
        }

    @router.put("/notification-settings")
    async def put_settings(
        body: SettingsBody, request: Request, session: Session
    ) -> dict[str, Any]:
        try:
            saved = await service.save_settings(
                _db(request),
                _clock(request),
                session.workspace_id,
                enabled=body.event_reminders_enabled,
                lead_minutes=body.lead_minutes,
            )
        except service.InvalidSetting:
            raise HTTPException(422, "invalid_setting") from None
        return {
            "event_reminders_enabled": saved.event_reminders_enabled,
            "lead_minutes": saved.lead_minutes,
        }

    return router
