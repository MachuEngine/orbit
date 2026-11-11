"""쿠키 세션 인증, Google 로그인, 개발용 로그인·로그아웃·세션 조회 (ADR-0013 §2, ADR-0016 §1).

- 세션 쿠키 `orbit_session`: HttpOnly, SameSite=Lax, Path=/. `ORBIT_ENV=dev`가 아니면 Secure.
- CSRF 쿠키 `orbit_csrf`: 스크립트가 읽을 수 있다. 상태를 바꾸는 요청은 `X-CSRF-Token` 헤더 ==
  `orbit_csrf` 쿠키 == 세션에 묶인 값(해시 비교)이어야 한다.
  실패는 403이고 본문에는 원인 종류만 둔다.
- 개발용 로그인(`POST /api/dev/login`)은 `ORBIT_ENV=dev`일 때만 라우트를 등록한다.
- Google 로그인: `GET /api/auth/google/start`(302), `GET /api/auth/google/callback`(성공이면 세션
  쿠키를 발급하고 `/`로, 실패면 `/?login_error=<종류>`로 302). 설정이 모두 있을 때만 등록한다.
  dev가 아니면 설정이 모두 있어야 앱이 시작된다(`ensure_startable`). 허용 목록 밖의 이메일은
  사용자를 만들지 않고 거부한다.
- 로그인 흐름 임시 쿠키 `orbit_login`: 서명(HMAC), 10분, HttpOnly, SameSite=Lax, Path는 로그인 경로.
- 세션 유효성은 요청을 받을 때 한 번 확인한다. 이미 열린 SSE 연결은 만료로 끊지 않는다.
"""

import hmac
import logging
import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from orbit.auth import (
    SessionInfo,
    csrf_matches,
    issue_session,
    lookup_session,
    revoke_session,
)
from orbit.auth.google import (
    FLOW_COOKIE,
    FLOW_TTL_SECONDS,
    GoogleOIDC,
    GoogleOIDCConfig,
    LoginError,
    LoginErrorKind,
    email_allowed,
)
from orbit.auth.users import login_upsert_user, session_profile
from orbit.core.clock import Clock
from orbit.core.config import Settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.runflow.repo import workspace_exists

SESSION_COOKIE = "orbit_session"
CSRF_COOKIE = "orbit_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
GOOGLE_LOGIN_PATH = "/api/auth/google"

logger = logging.getLogger(__name__)


def ensure_startable(settings: Settings) -> None:
    """dev이거나 Google 로그인 설정이 모두 있을 때만 시작한다(ADR-0016 §1)."""
    if settings.env == "dev":
        return
    missing = settings.missing_google_login_settings()
    if missing:
        raise RuntimeError(
            "ORBIT_ENV=dev가 아니면 Google 로그인 설정이 모두 있어야 API를 시작할 수 있다. "
            "빠진 설정: " + ", ".join(missing)
        )


def build_google_login(
    settings: Settings,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GoogleOIDC | None:
    """설정이 모두 있으면 로그인 흐름 객체를 만든다. dev에서 설정이 불완전하면 None."""
    if settings.missing_google_login_settings():
        return None
    key = settings.signing_key()
    secret = settings.google_client_secret
    if key is None or secret is None or not settings.google_client_id:
        return None
    redirect_uri = settings.google_redirect_uri
    if not redirect_uri:
        return None
    config = GoogleOIDCConfig(
        client_id=settings.google_client_id,
        client_secret=secret.get_secret_value(),
        redirect_uri=redirect_uri,
        auth_url=settings.google_auth_url,
        token_url=settings.google_token_url,
        jwks_url=settings.google_jwks_url,
        signing_key=key,
    )
    return GoogleOIDC(config, clock=clock, transport=transport)


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


def _set_flow_cookie(response: Response, settings: Settings, value: str) -> None:
    response.set_cookie(
        FLOW_COOKIE,
        value,
        max_age=FLOW_TTL_SECONDS,
        path=GOOGLE_LOGIN_PATH,
        secure=settings.env != "dev",
        httponly=True,
        samesite="lax",
    )


def _clear_flow_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        FLOW_COOKIE,
        path=GOOGLE_LOGIN_PATH,
        secure=settings.env != "dev",
        httponly=True,
        samesite="lax",
    )


def _login_failure(settings: Settings, kind: LoginErrorKind) -> Response:
    logger.info("Google 로그인 실패: kind=%s", kind)
    response = RedirectResponse(f"/?login_error={kind}", status_code=status.HTTP_302_FOUND)
    _clear_flow_cookie(response, settings)
    return response


def build_auth_router(settings: Settings, google: GoogleOIDC | None = None) -> APIRouter:
    router = APIRouter()

    if google is not None:

        @router.get("/auth/google/start")
        async def google_start() -> Response:
            url, cookie = google.begin()
            response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
            _set_flow_cookie(response, settings, cookie)
            return response

        @router.get("/auth/google/callback")
        async def google_callback(
            request: Request,
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
        ) -> Response:
            if error:  # 동의 화면에서 거절하는 등 Google이 오류로 돌려보낸 경우
                return _login_failure(settings, "denied")
            try:
                flow = google.read_flow_cookie(request.cookies.get(FLOW_COOKIE))
                identity = await google.complete(flow=flow, state=state, code=code)
                if not email_allowed(identity.email, settings.login_allowed_emails):
                    raise LoginError("not_allowed")
            except LoginError as exc:
                return _login_failure(settings, exc.kind)
            db: AsyncDatabase = request.app.state.db
            clock: Clock = request.app.state.clock
            ids: Uuid7Generator = request.app.state.ids
            try:
                result = await login_upsert_user(
                    db,
                    google_sub=identity.sub,
                    email=identity.email,
                    display_name=identity.display_name,
                    clock=clock,
                    ids=ids,
                )
                issued = await issue_session(
                    db,
                    workspace_id=result.workspace_id,
                    clock=clock,
                    ids=ids,
                    ttl_seconds=settings.session_ttl_seconds,
                    user_id=result.user_id,
                )
            except Exception as exc:
                # 예외 메시지에 SQL 인자(이메일 등)가 들어갈 수 있어 종류만 남긴다.
                logger.error("Google 로그인 처리 실패: %s", type(exc).__name__)
                return _login_failure(settings, "unavailable")
            logger.info("Google 로그인 성공: user_id=%s created=%s", result.user_id, result.created)
            response = RedirectResponse("/", status_code=status.HTTP_302_FOUND)
            _clear_flow_cookie(response, settings)
            _set_cookies(response, settings, issued.token, issued.csrf_token)
            return response

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
        request: Request,
        session: Annotated[SessionInfo | None, Depends(optional_session)],
    ) -> dict[str, Any]:
        """현재 세션과 로그인 수단. 사용자 정보는 이 세션에 묶인 본인 것만 나온다."""
        methods: dict[str, Any] = {
            "dev_login": settings.env == "dev",
            "google_login": google is not None,
        }
        if session is None:
            return {
                "authenticated": False,
                "workspace_id": None,
                "expires_at": None,
                "display_name": None,
                "email": None,
                **methods,
            }
        profile = None
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            profile = await session_profile(request.app.state.db, token)
        return {
            "authenticated": True,
            "workspace_id": str(session.workspace_id),
            "expires_at": session.expires_at.isoformat(),
            "display_name": profile.display_name if profile else None,
            "email": profile.email if profile else None,
            **methods,
        }

    return router
