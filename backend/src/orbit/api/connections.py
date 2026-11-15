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
- 데스크톱(ADR-0024 §5, 연결 CSRF 방지: 브라우저는 동의만 받고 저장은 앱이 세션으로 완료한다)
  - `POST /connections/google/desktop-ticket`(CSRF, `{intent, connection_id?, code_challenge, port,
    app_state}`): 앱이 만든 PKCE challenge·loopback 포트·앱 `state`를 묶은 60초 1회용 티켓과
    `{url}`(서버 출처의 `desktop-begin`)을 준다.
  - `GET /connections/google/desktop-begin?ticket=…`: 티켓을 소비하지 않고 "계속" 버튼 안내만
    보인다.
  - `POST /connections/google/desktop-begin`(폼, 서명 값+쿠키 확인): 티켓을 소비하고 서명 쿠키에
    워크스페이스·사용자·앱 정보를 묶어 Google로 303한다.
  - 콜백이 데스크톱 시작이면 토큰을 교환·저장하지 않는다. 인가 코드를 봉투 암호화해 5분 대기 행에
    두고
    `http://127.0.0.1:<port>/callback?code=…&state=…`로 302한다(실패는 `error=<종류>`).
  - `POST /connections/google/desktop-complete`(세션·CSRF, `{code, code_verifier}`): 세션의
    워크스페이스·사용자의 대기 행을 PKCE로 확인해 사용 처리하고, 인가 코드를 교환해 웹과 같은 저장
    규칙으로
    저장한다. 코드·verifier·세션 불일치는 모두 400이고, 저장 규칙 위반은 200 `{result: "error",
    error}`다.
- 연결은 사용자 세션이 필요하다(개발용 세션은 사용자가 없어 start가 403 `user_required`).
"""

import hmac
import logging
import re
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, model_validator

from orbit.api.auth import SESSION_COOKIE, current_session, optional_session
from orbit.api.desktop_pages import begin_page, failed_page
from orbit.auth import SessionInfo
from orbit.auth.desktop import loopback_url, parse_desktop_request
from orbit.auth.google import FLOW_TTL_SECONDS, DesktopLoginRequest, LoginError, pkce_challenge
from orbit.auth.users import session_profile
from orbit.connections import store
from orbit.connections.desktop_ticket import (
    BEGIN_PATH,
    TICKET_PATTERN,
    claim_pending,
    issue_ticket,
    redeem_ticket,
    store_pending,
)
from orbit.connections.oauth import (
    BEGIN_COOKIE,
    BEGIN_FORM_TTL_SECONDS,
    CONNECT_COOKIE,
    CONNECT_PATH,
    ConnectError,
    ConnectFlow,
    DesktopBinding,
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

CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
VERIFIER_PATTERN = re.compile(r"[A-Za-z0-9._~-]{43,128}")


def _failure(settings: Settings, kind: str) -> Response:
    logger.info("Google 계정 연결 실패: kind=%s", kind)
    response = RedirectResponse(f"/?connection_error={kind}", status_code=status.HTTP_302_FOUND)
    _clear_cookie(response, settings)
    return response


def _desktop_failure(settings: Settings, desktop: DesktopLoginRequest, kind: str) -> Response:
    """데스크톱 시작의 실패는 앱의 loopback 수신기로 알린다(브라우저에는 앱 화면이 없다)."""
    logger.info("데스크톱 Google 계정 연결 실패: kind=%s", kind)
    response = RedirectResponse(
        loopback_url(desktop, error=kind), status_code=status.HTTP_302_FOUND
    )
    response.headers["Cache-Control"] = "no-store"
    _clear_cookie(response, settings)
    return response


class DesktopCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    code_verifier: str


class DesktopTicketRequest(BaseModel):
    """`intent`가 `docs_scope`일 때만 `connection_id`가 필요하다."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["connect", "docs_scope"]
    connection_id: uuid.UUID | None = None
    code_challenge: str
    port: int
    app_state: str

    @model_validator(mode="after")
    def _check(self) -> "DesktopTicketRequest":
        if (self.intent == "docs_scope") != (self.connection_id is not None):
            raise ValueError("connection_id는 docs_scope에서만, 그리고 반드시 필요하다")
        if parse_desktop_request(self.code_challenge, str(self.port), self.app_state) is None:
            raise ValueError("code_challenge·port·app_state 형식이 올바르지 않다")
        return self

    def app_request(self) -> DesktopLoginRequest:
        parsed = parse_desktop_request(self.code_challenge, str(self.port), self.app_state)
        assert parsed is not None  # noqa: S101 (검증기가 보장)
        return parsed


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

    async def _docs_target(
        request: Request, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> store.ConnectionRow:
        """문서 권한을 더할 수 있는 연결인지 확인한다(없음 404, 비활성·이미 허용 409)."""
        db: AsyncDatabase = request.app.state.db
        async with db.workspace_session(workspace_id) as s:
            row = await store.get_connection(s, workspace_id, connection_id)
        if row is None or row.status == "revoked":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
        if row.status != "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "connection_inactive")
        if has_docs_scope(row.scopes):
            raise HTTPException(status.HTTP_409_CONFLICT, "docs_already_granted")
        return row

    def _set_connect_cookie(response: Response, cookie: str) -> None:
        response.set_cookie(
            CONNECT_COOKIE,
            cookie,
            max_age=FLOW_TTL_SECONDS,
            path=CONNECT_PATH,
            secure=settings.env != "dev",
            httponly=True,
            samesite="lax",
        )

    def _peek_flow(request: Request) -> ConnectFlow | None:
        """연결 쿠키가 유효하면 흐름을 돌려준다. 완료 방식(웹·데스크톱)을 알아내는 데만 쓴다."""
        if oauth is None:
            return None
        try:
            return oauth.read_connect_flow(request.cookies.get(CONNECT_COOKIE))
        except LoginError:
            return None

    @router.post("/connections/google/desktop-ticket")
    async def desktop_ticket(
        body: DesktopTicketRequest,
        request: Request,
        session: Annotated[SessionInfo, Depends(current_session)],
    ) -> Response:
        """데스크톱 앱이 시스템 브라우저로 연결을 시작할 일회용 티켓과 시작 주소를 준다."""
        redirect = settings.connect_redirect_uri()
        if oauth is None or cipher is None or not redirect:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "connections_unavailable")
        db: AsyncDatabase = request.app.state.db
        profile = await session_profile(
            db, request.cookies.get(SESSION_COOKIE, ""), clock=request.app.state.clock
        )
        if profile is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "user_required")
        if body.intent == "connect":
            async with db.workspace_session(session.workspace_id) as s:
                existing = await store.find_open(s, session.workspace_id)
            if existing is not None and existing.status == "active":
                raise HTTPException(status.HTTP_409_CONFLICT, "already_connected")
        elif body.connection_id is not None:
            await _docs_target(request, session.workspace_id, body.connection_id)
        ticket = await issue_ticket(
            db,
            workspace_id=session.workspace_id,
            user_id=profile.user_id,
            intent=body.intent,
            connection_id=body.connection_id,
            request=body.app_request(),
            clock=request.app.state.clock,
            ids=request.app.state.ids,
        )
        # 시작 주소는 콜백(redirect URI)과 같은 출처여야 `state` 쿠키가 콜백까지 간다.
        parts = urlsplit(redirect)
        url = f"{parts.scheme}://{parts.netloc}{BEGIN_PATH}?{urlencode({'ticket': ticket})}"
        return JSONResponse({"url": url}, headers={"Cache-Control": "no-store"})

    @router.get("/connections/google/desktop-begin")
    async def desktop_begin_page(request: Request, ticket: str | None = None) -> Response:
        """안내 화면만 보인다. 티켓을 소비하지 않아 링크 미리보기·프리페치가 티켓을 소진하지
        못한다."""
        redirect = settings.connect_redirect_uri()
        if oauth is None or cipher is None or not redirect:
            return failed_page()
        if ticket is None or not TICKET_PATTERN.fullmatch(ticket):
            return failed_page()
        token, nonce = oauth.begin_token(ticket)
        response = begin_page(action=BEGIN_PATH, ticket=ticket, token=token)
        response.set_cookie(
            BEGIN_COOKIE,
            nonce,
            max_age=BEGIN_FORM_TTL_SECONDS,
            path=CONNECT_PATH,
            secure=settings.env != "dev",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.post("/connections/google/desktop-begin")
    async def desktop_begin(request: Request) -> Response:
        """ "계속"을 눌렀을 때. 폼의 서명 값과 쿠키를 확인한 뒤 티켓을 소비하고 Google로 보낸다."""
        if oauth is None or cipher is None:
            return failed_page()
        try:
            form = parse_qs((await request.body()).decode("ascii"), max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            return failed_page()
        ticket = (form.get("ticket") or [""])[0]
        token = (form.get("token") or [""])[0]
        if not oauth.check_begin_token(ticket, token, request.cookies.get(BEGIN_COOKIE)):
            return failed_page()
        db: AsyncDatabase = request.app.state.db
        redeemed = await redeem_ticket(db, ticket, clock=request.app.state.clock)
        if redeemed is None:
            return failed_page()
        binding = DesktopBinding(redeemed.workspace_id, redeemed.user_id, redeemed.request)
        try:
            if redeemed.intent == "connect":
                async with db.workspace_session(redeemed.workspace_id) as s:
                    existing = await store.find_open(s, redeemed.workspace_id)
                if existing is not None and existing.status == "active":
                    return failed_page()
                url, cookie = oauth.begin_connect(binding)
            else:
                if redeemed.connection_id is None:
                    return failed_page()
                row = await _docs_target(request, redeemed.workspace_id, redeemed.connection_id)
                url, cookie = oauth.begin_scope_add(
                    redeemed.connection_id, login_hint=row.account_email, desktop=binding
                )
        except HTTPException:
            return failed_page()
        response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        _set_connect_cookie(response, cookie)
        response.delete_cookie(
            BEGIN_COOKIE,
            path=CONNECT_PATH,
            secure=settings.env != "dev",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.post("/connections/google/desktop-complete")
    async def desktop_complete(
        body: DesktopCompleteRequest,
        request: Request,
        session: Annotated[SessionInfo, Depends(current_session)],
    ) -> Response:
        """앱이 자기 세션으로 완료 코드를 제출해 연결을 저장한다. 코드·세션 불일치는 구분 없는
        400이다."""
        invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_grant")
        if oauth is None or cipher is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "connections_unavailable")
        if not CODE_PATTERN.fullmatch(body.code) or not VERIFIER_PATTERN.fullmatch(
            body.code_verifier
        ):
            raise invalid
        db: AsyncDatabase = request.app.state.db
        clock: Clock = request.app.state.clock
        profile = await session_profile(db, request.cookies.get(SESSION_COOKIE, ""), clock=clock)
        if profile is None:
            raise invalid
        try:
            pending = await claim_pending(
                db,
                cipher,
                workspace_id=session.workspace_id,
                user_id=profile.user_id,
                code=body.code,
                code_challenge=pkce_challenge(body.code_verifier),
                clock=clock,
            )
        except Exception as exc:
            logger.error("데스크톱 연결 완료 처리 실패: %s", type(exc).__name__)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from None
        if pending is None:
            raise invalid
        try:
            grant = await oauth.complete_connect(
                flow=pending.flow, state=None, code=pending.auth_code, verify_state=False
            )
            if pending.connection_id is not None:
                await save_scope_grant(
                    db,
                    oauth,
                    cipher,
                    workspace_id=session.workspace_id,
                    connection_id=pending.connection_id,
                    grant=grant,
                    clock=clock,
                )
                result = "docs_added"
            else:
                await save_grant(
                    db,
                    oauth,
                    cipher,
                    workspace_id=session.workspace_id,
                    user_id=profile.user_id,
                    grant=grant,
                    clock=clock,
                    ids=request.app.state.ids,
                )
                result = "connected"
        except ConnectError as exc:
            logger.info("데스크톱 Google 계정 연결 실패: kind=%s", exc.kind)
            return JSONResponse(
                {"result": "error", "error": exc.kind}, headers={"Cache-Control": "no-store"}
            )
        except Exception as exc:
            logger.error("데스크톱 Google 계정 연결 처리 실패: %s", type(exc).__name__)
            return JSONResponse(
                {"result": "error", "error": "unavailable"}, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse({"result": result}, headers={"Cache-Control": "no-store"})

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
        row = await _docs_target(request, session.workspace_id, connection_id)
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

    async def _desktop_callback(
        request: Request,
        connect: ConnectFlow,
        desktop: DesktopBinding,
        state: str | None,
        code: str | None,
    ) -> Response:
        """데스크톱 시작의 콜백: 토큰을 교환·저장하지 않고 인가 코드를 대기 행에 둔다."""
        assert cipher is not None  # noqa: S101 (호출부가 확인)
        app = desktop.request
        flow = connect.flow
        if not state or not hmac.compare_digest(state.encode(), flow.state.encode()):
            return _desktop_failure(settings, app, "state")
        if not code or len(code) > 2048:
            return _desktop_failure(settings, app, "exchange")
        try:
            completion = await store_pending(
                request.app.state.db,
                cipher,
                binding=desktop,
                intent="docs_scope" if connect.connection_id is not None else "connect",
                connection_id=connect.connection_id,
                flow=flow,
                auth_code=code,
                clock=request.app.state.clock,
                ids=request.app.state.ids,
            )
        except Exception as exc:
            logger.error("데스크톱 연결 대기 행 저장 실패: %s", type(exc).__name__)
            return _desktop_failure(settings, app, "unavailable")
        response = RedirectResponse(
            loopback_url(app, code=completion), status_code=status.HTTP_302_FOUND
        )
        response.headers["Cache-Control"] = "no-store"
        _clear_cookie(response, settings)
        return response

    @router.get("/connections/google/callback")
    async def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        # 서명 쿠키가 데스크톱 시작이면 세션이 아니라 앱이 완료한다(ADR-0024 §5).
        # 웹 시작은 아래 순서 그대로다.
        peek = _peek_flow(request)
        desktop = peek.desktop if peek is not None else None
        if desktop is not None and peek is not None:
            if oauth is None or cipher is None:
                return _desktop_failure(settings, desktop.request, "unavailable")
            if error:
                return _desktop_failure(settings, desktop.request, "denied")
            return await _desktop_callback(request, peek, desktop, state, code)
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
