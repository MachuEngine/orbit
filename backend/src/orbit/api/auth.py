"""쿠키 세션 인증 의존성과 개발용 로그인·로그아웃·세션 조회 (ADR-0013 §2).

- 세션 쿠키 `orbit_session`: HttpOnly, SameSite=Lax, Path=/. `ORBIT_ENV=dev`가 아니면 Secure.
- CSRF 쿠키 `orbit_csrf`: 스크립트가 읽을 수 있다. 상태를 바꾸는 요청은 `X-CSRF-Token` 헤더 ==
  `orbit_csrf` 쿠키 == 세션에 묶인 값(해시 비교)이어야 한다.
  실패는 403이고 본문에는 원인 종류만 둔다.
- 개발용 로그인(`POST /api/dev/login`)은 `ORBIT_ENV=dev`일 때만 라우트를 등록한다.
  로그인 구현(6단계) 전에는 dev가 아니면 앱이 시작되지 않는다(`ensure_dev_environment`).
- 세션 유효성은 요청을 받을 때 한 번 확인한다. 이미 열린 SSE 연결은 만료로 끊지 않는다.
"""

import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from orbit.auth import (
    SessionInfo,
    csrf_matches,
    issue_session,
    lookup_session,
    revoke_session,
)
from orbit.core.clock import Clock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.runflow.repo import workspace_exists

SESSION_COOKIE = "orbit_session"
CSRF_COOKIE = "orbit_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def ensure_dev_environment(settings: Settings) -> None:
    if settings.env != "dev":
        raise RuntimeError(
            "로그인 구현 전에는 ORBIT_ENV=dev에서만 API를 시작할 수 있다 "
            "(개발용 로그인은 개발 환경 전용이다)"
        )


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthenticated")


def _csrf_failure(kind: str) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, kind)


async def optional_session(request: Request) -> SessionInfo | None:
    """쿠키의 세션이 유효하면 돌려준다. CSRF는 확인하지 않는다."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    db: AsyncDatabase = request.app.state.db
    clock: Clock = request.app.state.clock
    return await lookup_session(db, token, clock=clock)


async def current_session(request: Request) -> SessionInfo:
    """유효한 세션이 없으면 401. 상태를 바꾸는 메서드는 CSRF도 확인한다(실패 403)."""
    session = await optional_session(request)
    if session is None:
        raise _unauthorized()
    if request.method not in SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER)
        cookie = request.cookies.get(CSRF_COOKIE)
        if not header or not cookie:
            raise _csrf_failure("csrf_missing")
        if not hmac.compare_digest(header.encode(), cookie.encode()):
            raise _csrf_failure("csrf_mismatch")
        if not csrf_matches(header, session.csrf_token_hash):
            raise _csrf_failure("csrf_mismatch")
    return session


async def current_workspace(
    session: Annotated[SessionInfo, Depends(current_session)],
) -> uuid.UUID:
    return session.workspace_id


def _set_cookies(response: Response, settings: Settings, token: str, csrf: str) -> None:
    secure = settings.env != "dev"
    max_age = int(settings.session_ttl_seconds)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def _delete_cookies(response: Response, settings: Settings) -> None:
    secure = settings.env != "dev"
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, httponly=False, samesite="lax")


def build_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    if settings.env == "dev":

        @router.post("/dev/login")
        async def dev_login(request: Request, response: Response) -> dict[str, Any]:
            """설정된 개발 워크스페이스의 세션을 발급한다. 워크스페이스를 만들지 않는다."""
            db: AsyncDatabase = request.app.state.db
            workspace_id = settings.dev_workspace_id
            if workspace_id is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "개발 워크스페이스가 설정되지 않았다"
                )
            async with db.workspace_session(workspace_id) as s:
                found = await workspace_exists(s, workspace_id)
            if not found:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "개발 워크스페이스가 없다")
            clock: Clock = request.app.state.clock
            ids: Uuid7Generator = request.app.state.ids
            issued = await issue_session(
                db,
                workspace_id=workspace_id,
                clock=clock,
                ids=ids,
                ttl_seconds=settings.session_ttl_seconds,
            )
            _set_cookies(response, settings, issued.token, issued.csrf_token)
            return {
                "workspace_id": str(workspace_id),
                "expires_at": issued.expires_at.isoformat(),
            }

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        session: Annotated[SessionInfo, Depends(current_session)],
    ) -> Response:
        db: AsyncDatabase = request.app.state.db
        clock: Clock = request.app.state.clock
        await revoke_session(
            db, workspace_id=session.workspace_id, session_id=session.session_id, clock=clock
        )
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _delete_cookies(response, settings)
        return response

    @router.get("/session")
    async def read_session(
        session: Annotated[SessionInfo | None, Depends(optional_session)],
    ) -> dict[str, Any]:
        if session is None:
            return {"authenticated": False, "workspace_id": None, "expires_at": None}
        return {
            "authenticated": True,
            "workspace_id": str(session.workspace_id),
            "expires_at": session.expires_at.isoformat(),
        }

    return router
