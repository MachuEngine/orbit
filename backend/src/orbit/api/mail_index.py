"""메일 색인 범위 API (ADR-0019 §1·§6). 모든 경로는 `/api/connections/{id}` 아래다.

- `GET .../labels`: 커넥터가 돌려준 라벨(id, 이름, 종류). 커넥터 오류는 502이고 `detail`에 오류
  종류(`connector_error_kind`)만 둔다.
- `GET .../index`: 범위 목록, 색인 메일 수, 마지막 동기화 시각, 상태, 오류 종류, 동기화 대기 여부.
  제목·본문·주소는 담지 않는다.
- `POST .../index/scopes`(CSRF): `label_id`와 `window_days`(30·90·365)를 받는다. 라벨 이름은
  서버가 커넥터에서 확인해 저장하고 클라이언트 값을 믿지 않는다. 모르는 라벨(`unknown_label`)과
  색인할 수 없는 라벨(`unsupported_label`: 읽음·별표 변형·초안·스팸 등)은 422다.
- `DELETE .../index/scopes/{scope_id}`(CSRF): 범위를 지우고 남은 범위 밖 항목을 같은 트랜잭션에서
  지운다.
- `POST .../index/sync`(CSRF): "지금 동기화". 항상 202이고, 새로 등록했는지는 `queued`로 알린다.
- 다른 워크스페이스의 연결, 없는 연결, 해제된 연결은 404다.
"""

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from orbit.api.auth import current_session
from orbit.auth import SessionInfo
from orbit.connectors.contracts import ConnectorError, MailConnector, MailLabel
from orbit.connectors.errors import connector_error_kind
from orbit.db.session import AsyncDatabase
from orbit.mailindex import service, store
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.labels import is_indexable_label
from orbit.runflow.deps import ConnectorResolver, NoConnection

logger = logging.getLogger(__name__)


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_id: str
    window_days: Literal[30, 90, 365]


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "not_found")


def _deps(request: Request) -> MailIndexDeps:
    deps: MailIndexDeps = request.app.state.mail_index
    return deps


async def _require_active(request: Request, session: SessionInfo, connection_id: uuid.UUID) -> None:
    db: AsyncDatabase = request.app.state.db
    async with db.workspace_session(session.workspace_id) as s:
        found = await store.connection_status(s, session.workspace_id, connection_id)
    if found != "active":
        raise _not_found()


async def _labels(request: Request, session: SessionInfo) -> list[MailLabel]:
    resolver: ConnectorResolver | None = request.app.state.resolver
    if resolver is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "connector_unavailable")
    try:
        _, mail = await resolver.connectors_for(session.workspace_id)
        return await _list_labels(mail)
    except NoConnection:
        raise _not_found() from None
    except ConnectorError as exc:
        kind = connector_error_kind(exc)
        logger.warning("라벨 조회 실패: kind=%s", kind)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, kind) from None


async def _list_labels(mail: MailConnector) -> list[MailLabel]:
    return await mail.list_labels()


def _status_body(result: service.IndexStatus) -> dict[str, Any]:
    return {
        "scopes": [
            {
                "id": str(sc.id),
                "label_id": sc.label_id,
                "label_name": sc.label_name,
                "window_days": sc.period_days,
                "synced": sc.initial_synced_at is not None,
            }
            for sc in result.scopes
        ],
        "indexed_mail_count": result.indexed_mail_count,
        "last_synced_at": None
        if result.last_synced_at is None
        else result.last_synced_at.isoformat(),
        "status": result.status,
        "error_kind": result.error_kind,
        "sync_queued": result.sync_queued,
    }


def build_mail_index_router() -> APIRouter:
    router = APIRouter()
    Session = Annotated[SessionInfo, Depends(current_session)]  # noqa: N806

    @router.get("/connections/{connection_id}/labels")
    async def labels(
        connection_id: uuid.UUID, request: Request, session: Session
    ) -> dict[str, Any]:
        await _require_active(request, session, connection_id)
        found = await _labels(request, session)
        return {"labels": [{"id": x.id, "name": x.name, "kind": x.kind} for x in found]}

    @router.get("/connections/{connection_id}/index")
    async def index_status(
        connection_id: uuid.UUID, request: Request, session: Session
    ) -> dict[str, Any]:
        await _require_active(request, session, connection_id)
        result = await service.get_status(_deps(request), session.workspace_id, connection_id)
        return _status_body(result)

    @router.post("/connections/{connection_id}/index/scopes", status_code=status.HTTP_201_CREATED)
    async def add_scope(
        connection_id: uuid.UUID, body: ScopeBody, request: Request, session: Session
    ) -> dict[str, Any]:
        await _require_active(request, session, connection_id)
        label = next((x for x in await _labels(request, session) if x.id == body.label_id), None)
        if label is None:
            raise HTTPException(422, "unknown_label")
        if not is_indexable_label(label):
            raise HTTPException(422, "unsupported_label")
        try:
            scope = await service.add_scope(
                _deps(request),
                session.workspace_id,
                connection_id,
                label_id=label.id,
                label_name=label.name,
                period_days=body.window_days,
            )
        except service.ConnectionNotActive:
            raise _not_found() from None
        except service.InvalidScope:
            raise HTTPException(422, "invalid_scope") from None
        return {
            "id": str(scope.id),
            "label_id": scope.label_id,
            "label_name": scope.label_name,
            "window_days": scope.period_days,
            "synced": scope.initial_synced_at is not None,
        }

    @router.delete("/connections/{connection_id}/index/scopes/{scope_id}")
    async def delete_scope(
        connection_id: uuid.UUID, scope_id: uuid.UUID, request: Request, session: Session
    ) -> dict[str, Any]:
        await _require_active(request, session, connection_id)
        removed = await service.remove_scope(
            _deps(request), session.workspace_id, connection_id, scope_id
        )
        if not removed:
            raise _not_found()
        return {"id": str(scope_id)}

    @router.post("/connections/{connection_id}/index/sync", status_code=status.HTTP_202_ACCEPTED)
    async def sync_now(
        connection_id: uuid.UUID, request: Request, session: Session
    ) -> dict[str, Any]:
        await _require_active(request, session, connection_id)
        try:
            queued = await service.request_sync(_deps(request), session.workspace_id, connection_id)
        except service.ConnectionNotActive:
            raise _not_found() from None
        return {"queued": queued}

    return router
