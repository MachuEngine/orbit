"""FastAPI 앱: 헬스체크와 Run·승인 API. 모든 경로는 `/api` 아래에 있다.

`ORBIT_ENV=dev`이거나 Google 로그인 설정이 모두 있을 때만 시작한다(`orbit.api.auth`, ADR-0016).
시계·ID 생성기와 Google HTTP 전송은 주입할 수 있고(테스트), 기본은 시스템 시계다.

데모 배포(ADR-0022): `ORBIT_EMBEDDED_WORKER`이면 lifespan이 작업 워커를 같은 이벤트 루프에서 띄우고,
`ORBIT_STATIC_DIR`이면 `/api` 밖의 경로에 빌드한 프론트를 서빙한다. prod는 시작할 때 환경·DB 역할·
마이그레이션 리비전을 점검하고(`orbit.api.startup`), 접근 로그에서 쿼리 문자열을 지운다.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI, Request, Response, status

from orbit.api.auth import build_auth_router, build_google_login, ensure_startable
from orbit.api.connections import build_connections_router
from orbit.api.mail_index import build_mail_index_router
from orbit.api.notifications import build_notifications_router
from orbit.api.runs import router as runs_router
from orbit.api.startup import ensure_database_ready, ensure_deploy_environment
from orbit.api.static import install_static
from orbit.auth.google import GoogleOIDCConfig
from orbit.connections.oauth import GoogleConnectOAuth
from orbit.core.accesslog import install_access_log_filter
from orbit.core.clock import Clock, SystemClock
from orbit.core.config import Settings, get_settings
from orbit.core.ids import Uuid7Generator
from orbit.crypto.envelope import EnvelopeCipher, EnvelopeError, load_kek_from_file
from orbit.db.session import AsyncDatabase
from orbit.mailindex.deps import MailIndexDeps
from orbit.mailindex.embedding import NullEmbedder
from orbit.records.jobs import make_procrastinate_app
from orbit.runflow.deps import ConnectorResolver, GoogleConnectorResolver, SimConnectorResolver
from orbit.runflow.embedded import EmbeddedWorker
from orbit.runflow.worker import WorkerSetupError

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
            cipher = EnvelopeCipher(
                load_kek_from_file(settings.data_key_file, strict_permissions=settings.env != "dev")
            )
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


def _default_resolver(
    settings: Settings, db: AsyncDatabase, clock: Clock, cipher: EnvelopeCipher | None
) -> ConnectorResolver | None:
    """라벨 조회용 커넥터 해석기. 설정이 모자라면 None이고 라벨 API는 503이다."""
    if settings.connector_mode == "google":
        if (
            cipher is None
            or settings.google_client_id is None
            or settings.google_client_secret is None
        ):
            return None
        return GoogleConnectorResolver(
            db=db,
            cipher=cipher,
            clock=clock,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            token_url=settings.google_token_url,
        )
    if settings.sim_snapshot_path is None:
        return None
    return SimConnectorResolver(settings.sim_snapshot_path, settings.sim_account_id, clock)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: Uuid7Generator | None = None,
    google_transport: httpx.AsyncBaseTransport | None = None,
    resolver: ConnectorResolver | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else get_settings()
    install_access_log_filter()
    ensure_startable(resolved)  # dev가 아니고 Google 로그인 설정이 빠졌으면 시작하지 않는다
    ensure_deploy_environment(resolved)
    resolved_clock: Clock = clock if clock is not None else SystemClock()
    resolved_ids = ids if ids is not None else Uuid7Generator(resolved_clock)
    connect_oauth, cipher = build_connections(resolved, resolved_clock, google_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        url = resolved.database_url.get_secret_value()
        db = AsyncDatabase.from_url(url)
        worker: EmbeddedWorker | None = None
        try:
            await ensure_database_ready(db, resolved)  # prod에서만 점검한다
            app.state.db = db
            app.state.jobs = make_procrastinate_app(url)
            # 색인 범위 관리용 의존성. API 프로세스는 임베딩을 하지 않는다(워커가 동기화한다).
            app.state.mail_index = MailIndexDeps(
                db=db,
                clock=resolved_clock,
                ids=resolved_ids,
                embedder=NullEmbedder(),
                jobs=app.state.jobs,
            )
            app.state.resolver = resolver or _default_resolver(resolved, db, resolved_clock, cipher)
            if resolved.embedded_worker:
                try:
                    worker = await EmbeddedWorker.start(
                        resolved, db=db, clock=resolved_clock, ids=resolved_ids
                    )
                except WorkerSetupError as exc:
                    raise RuntimeError(str(exc)) from None
            yield
        finally:
            if worker is not None:  # 워커를 먼저 멈춘다
                await worker.aclose()
            closer = getattr(getattr(app.state, "resolver", None), "aclose", None)
            if closer is not None:
                await closer()
            await db.dispose()

    app = FastAPI(title="Orbit", lifespan=lifespan)
    app.state.settings = resolved
    app.state.clock = resolved_clock
    app.state.ids = resolved_ids
    api = APIRouter(prefix="/api")
    google = build_google_login(resolved, resolved_clock, google_transport)
    api.include_router(build_auth_router(resolved, google))
    api.include_router(build_connections_router(resolved, connect_oauth, cipher))
    api.include_router(build_mail_index_router())
    api.include_router(build_notifications_router())
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
    if resolved.static_dir:
        install_static(app, resolved.static_dir)  # catch-all이므로 라우트 등록 뒤에 둔다
    return app
