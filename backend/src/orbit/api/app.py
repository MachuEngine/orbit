"""FastAPI 앱: 헬스체크와 Run·승인 API. 모든 경로는 `/api` 아래에 있다.

`ORBIT_ENV=dev`이거나 Google 로그인 설정이 모두 있을 때만 시작한다(`orbit.api.auth`, ADR-0016).
시계·ID 생성기와 Google HTTP 전송은 주입할 수 있고(테스트), 기본은 시스템 시계다.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI, Request, Response, status

from orbit.api.auth import build_auth_router, build_google_login, ensure_startable
from orbit.api.connections import build_connections_router
from orbit.api.runs import router as runs_router
from orbit.auth.google import GoogleOIDCConfig
from orbit.connections.oauth import GoogleConnectOAuth
from orbit.core.clock import Clock, SystemClock
from orbit.core.config import Settings, get_settings
from orbit.core.ids import Uuid7Generator
from orbit.crypto.envelope import EnvelopeCipher, EnvelopeError, load_kek_from_file
from orbit.db.session import AsyncDatabase
from orbit.records.jobs import make_procrastinate_app

logger = logging.getLogger(__name__)


def build_connections(
    settings: Settings,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[GoogleConnectOAuth | None, EnvelopeCipher | None]:
    """계정 연결 흐름 객체. KEK 파일이 설정됐는데 읽을 수 없으면 시작을 거부한다(경로만 알린다).

    `ORBIT_CONNECTOR_MODE=google`인데 연결 설정이 빠졌으면 빠진 환경 변수 이름과 함께 거부한다.
    """
    missing = settings.missing_connection_settings()
    if settings.connector_mode == "google" and missing:
        raise RuntimeError(
            "ORBIT_CONNECTOR_MODE=google이면 계정 연결 설정이 모두 있어야 한다. 빠진 설정: "
            + ", ".join(missing)
        )
    cipher: EnvelopeCipher | None = None
    if settings.data_key_file:
        try:
            cipher = EnvelopeCipher(load_kek_from_file(settings.data_key_file))
        except EnvelopeError as exc:
            raise RuntimeError(str(exc)) from None
    key = settings.signing_key()
    secret = settings.google_client_secret
    redirect = settings.connect_redirect_uri()
    if missing or cipher is None or key is None or secret is None or not redirect:
        return None, cipher
    assert settings.google_client_id is not None  # noqa: S101 (missing 검사로 보장)
    config = GoogleOIDCConfig(
        client_id=settings.google_client_id,
        client_secret=secret.get_secret_value(),
        redirect_uri=redirect,
        auth_url=settings.google_auth_url,
        token_url=settings.google_token_url,
        jwks_url=settings.google_jwks_url,
        signing_key=key,
    )
    oauth = GoogleConnectOAuth(
        config, revoke_url=settings.google_revoke_url, clock=clock, transport=transport
    )
    return oauth, cipher


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: Uuid7Generator | None = None,
    google_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else get_settings()
    ensure_startable(resolved)  # dev가 아니고 Google 로그인 설정이 빠졌으면 시작하지 않는다
    resolved_clock: Clock = clock if clock is not None else SystemClock()
    resolved_ids = ids if ids is not None else Uuid7Generator(resolved_clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        url = resolved.database_url.get_secret_value()
        db = AsyncDatabase.from_url(url)
        app.state.db = db
        app.state.jobs = make_procrastinate_app(url)
        try:
            yield
        finally:
            await db.dispose()

    app = FastAPI(title="Orbit", lifespan=lifespan)
    app.state.settings = resolved
    app.state.clock = resolved_clock
    app.state.ids = resolved_ids
    api = APIRouter(prefix="/api")
    google = build_google_login(resolved, resolved_clock, google_transport)
    api.include_router(build_auth_router(resolved, google))
    connect_oauth, cipher = build_connections(resolved, resolved_clock, google_transport)
    api.include_router(build_connections_router(resolved, connect_oauth, cipher))
    api.include_router(runs_router)

    @api.get("/healthz")
    async def healthz(request: Request, response: Response) -> dict[str, str]:
        """워크스페이스 없이 DB 연결만 확인한다."""
        db: AsyncDatabase = request.app.state.db
        try:
            await db.ping()
        except Exception:
            logger.exception("헬스체크: DB 연결 실패")
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable"}
        return {"status": "ok"}

    app.include_router(api)
    return app
