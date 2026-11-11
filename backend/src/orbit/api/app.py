"""FastAPI 앱: 헬스체크와 Run·승인 API. 모든 경로는 `/api` 아래에 있다.

로그인(6단계) 전에는 `ORBIT_ENV=dev`에서만 시작한다(`orbit.api.auth`). 시계·ID 생성기는 주입할 수
있고(테스트), 기본은 시스템 시계다.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response, status

from orbit.api.auth import build_auth_router, ensure_dev_environment
from orbit.api.runs import router as runs_router
from orbit.core.clock import Clock, SystemClock
from orbit.core.config import Settings, get_settings
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.jobs import make_procrastinate_app

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: Uuid7Generator | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else get_settings()
    ensure_dev_environment(resolved)  # 개발 환경이 아니면 시작하지 않는다
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
    api.include_router(build_auth_router(resolved))
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
