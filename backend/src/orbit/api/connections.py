"""계정 연결 API (ADR-0016 §2·§3). 모든 경로는 `/api` 아래다.

- `GET /connections`: 현재 워크스페이스의 연결 목록(상태·계정 이메일·범위·생성 시각). 토큰 관련 값은
  어느 응답에도 없다. `mode`(커넥터 모드)와 `connect_available`(연결 흐름 설정 완료 여부)도 준다.
- `POST /connections/google/start`(CSRF): 인가 URL을 JSON으로 돌려준다. 302로 응답하면 화면의
  `fetch`가 교차 출처 리디렉션을 따라가지 못한다. 화면이 받은 주소로 직접 이동한다. 서명 임시 쿠키
  (`orbit_connect`, HttpOnly, SameSite=Lax, 10분, Path는 연결 경로)를 함께 심는다.
- `POST /connections/{id}/scopes/docs/start`(CSRF): 문서(Drive) 읽기 권한 추가(선택 권한, ADR-0020).
  같은 서명 쿠키에 목적과 연결 ID를 담고 인가 URL을 돌려준다. 활성 연결이 아니면 404/409다.
  콜백이 같은 활성 연결이고 같은 계정(`sub`)일 때만 그 행의 범위·토큰을 갱신하며 결과는
  `/?connection=docs_added` 또는 `/?connection_error=<종류>`다.
- `GET /connections/google/callback`: state 대조, 코드 교환, 범위·refresh_token 확인, 봉투 암호화
  저장. 결과는 `/?connection=connected` 또는 `/?connection_error=<종류>`로 302한다.
- `DELETE /connections/{id}`(CSRF): Google 철회를 시도하고 결과와 무관하게 로컬은 `revoked`다.
  응답의 `provider_revoke`가 `revoked`/`failed`/`none`이다. 다른 워크스페이스의 연결은 404다.
- 연결은 사용자 세션이 필요하다(개발용 세션은 사용자가 없어 start가 403 `user_required`).
"""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from orbit.api.auth import SESSION_COOKIE, current_session, optional_session
from orbit.auth import SessionInfo
from orbit.auth.google import FLOW_TTL_SECONDS, LoginError
from orbit.auth.users import session_profile
from orbit.connections import store
from orbit.connections.oauth import (
    CONNECT_COOKIE,
    CONNECT_PATH,
    ConnectError,
    GoogleConnectOAuth,
    has_docs_scope,
)
from orbit.connections.service import disconnect, save_grant, save_scope_grant
from orbit.core.clock import Clock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.crypto.envelope import EnvelopeCipher
from orbit.db.session import AsyncDatabase

logger = logging.getLogger(__name__)


def _failure(settings: Settings, kind: str) -> Response:
    logger.info("Google 계정 연결 실패: kind=%s", kind)
    response = RedirectResponse(f"/?connection_error={kind}", status_code=status.HTTP_302_FOUND)
    _clear_cookie(response, settings)
    return response


def _clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        CONNECT_COOKIE,
        path=CONNECT_PATH,
        secure=settings.env != "dev",
        httponly=True,
        samesite="lax",
    )


def build_connections_router(
    settings: Settings,
    oauth: GoogleConnectOAuth | None,
    cipher: EnvelopeCipher | None,
) -> APIRouter:
    router = APIRouter()
    available = oauth is not None and cipher is not None

    @router.get("/connections")
    async def list_connections(
        request: Request, session: Annotated[SessionInfo, Depends(current_session)]
    ) -> dict[str, Any]:
        db: AsyncDatabase = request.app.state.db
        async with db.workspace_session(session.workspace_id) as s:
            rows = await store.list_open_connections(s, session.workspace_id)
        return {
            "mode": settings.connector_mode,
            "connect_available": available,
            "connections": [
                {
                    "id": str(r.id),
                    "provider": r.provider,
                    "status": r.status,
                    "account_email": r.account_email,
                    "scopes": list(r.scopes),
                    "docs_granted": has_docs_scope(r.scopes),
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        }

    @router.post("/connections/google/start")
    async def start(
        request: Request,
        session: Annotated[SessionInfo, Depends(current_session)],
    ) -> Response:
        if oauth is None or cipher is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "connections_unavailable")
        token = request.cookies.get(SESSION_COOKIE, "")
        clock: Clock = request.app.state.clock
        profile = await session_profile(request.app.state.db, token, clock=clock)
        if profile is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "user_required")
        db: AsyncDatabase = request.app.state.db
        async with db.workspace_session(session.workspace_id) as s:
            existing = await store.find_open(s, session.workspace_id)
        if existing is not None and existing.status == "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "already_connected")
        url, cookie = oauth.begin_connect()
        response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            CONNECT_COOKIE,
            cookie,
            max_age=FLOW_TTL_SECONDS,
            path=CONNECT_PATH,
            secure=settings.env != "dev",
            httponly=True,
            samesite="lax",
        )
        return response

    @router.post("/connections/{connection_id}/scopes/docs/start")
    async def start_docs_scope(
        connection_id: uuid.UUID,
        request: Request,
        session: Annotated[SessionInfo, Depends(current_session)],
    ) -> Response:
        if oauth is None or cipher is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "connections_unavailable")
        clock: Clock = request.app.state.clock
        profile = await session_profile(
            request.app.state.db, request.cookies.get(SESSION_COOKIE, ""), clock=clock
        )
        if profile is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "user_required")
        db: AsyncDatabase = request.app.state.db
        async with db.workspace_session(session.workspace_id) as s:
            row = await store.get_connection(s, session.workspace_id, connection_id)
        if row is None or row.status == "revoked":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
        if row.status != "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "connection_inactive")
        if has_docs_scope(row.scopes):
            raise HTTPException(status.HTTP_409_CONFLICT, "docs_already_granted")
        url, cookie = oauth.begin_scope_add(connection_id, login_hint=row.account_email)
        response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            CONNECT_COOKIE,
            cookie,
            max_age=FLOW_TTL_SECONDS,
            path=CONNECT_PATH,
            secure=settings.env != "dev",
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/connections/google/callback")
    async def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        if oauth is None or cipher is None:
            return _failure(settings, "unavailable")
        if error:
            return _failure(settings, "denied")
        session = await optional_session(request)
        if session is None:
            return _failure(settings, "unauthenticated")
        db: AsyncDatabase = request.app.state.db
        clock: Clock = request.app.state.clock
        ids: Uuid7Generator = request.app.state.ids
        try:
            profile = await session_profile(
                db, request.cookies.get(SESSION_COOKIE, ""), clock=clock
            )
            if profile is None:
                raise ConnectError("unauthenticated")
            try:
                connect = oauth.read_connect_flow(request.cookies.get(CONNECT_COOKIE))
            except LoginError:
                raise ConnectError("expired") from None
            grant = await oauth.complete_connect(flow=connect.flow, state=state, code=code)
            if connect.connection_id is not None:
                await save_scope_grant(
                    db,
                    oauth,
                    cipher,
                    workspace_id=session.workspace_id,
                    connection_id=connect.connection_id,
                    grant=grant,
                    clock=clock,
                )
                docs_added = True
            else:
                await save_grant(
                    db,
                    oauth,
                    cipher,
                    workspace_id=session.workspace_id,
                    user_id=profile.user_id,
                    grant=grant,
                    clock=clock,
                    ids=ids,
                )
                docs_added = False
        except ConnectError as exc:
            return _failure(settings, exc.kind)
        except Exception as exc:
            # 예외 메시지에 SQL 인자(이메일 등)가 들어갈 수 있어 종류만 남긴다.
            logger.error("Google 계정 연결 처리 실패: %s", type(exc).__name__)
            return _failure(settings, "unavailable")
        done = "docs_added" if docs_added else "connected"
        response = RedirectResponse(f"/?connection={done}", status_code=status.HTTP_302_FOUND)
        _clear_cookie(response, settings)
        return response

    @router.delete("/connections/{connection_id}")
    async def delete_connection(
        connection_id: uuid.UUID,
        request: Request,
        session: Annotated[SessionInfo, Depends(current_session)],
    ) -> dict[str, Any]:
        result = await disconnect(
            request.app.state.db,
            oauth,
            cipher,
            workspace_id=session.workspace_id,
            connection_id=connection_id,
            clock=request.app.state.clock,
        )
        if not result.found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
        return {
            "id": str(connection_id),
            "status": result.status,
            "provider_revoke": result.provider_revoke,
        }

    return router
