"""FastAPI 앱. 1단계에는 헬스체크만 있다."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status

from orbit.core.config import Settings, get_settings
from orbit.db.session import AsyncDatabase

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = AsyncDatabase.from_url(resolved.database_url.get_secret_value())
        app.state.db = db
        try:
            yield
        finally:
            await db.dispose()

    app = FastAPI(title="Orbit", lifespan=lifespan)

    @app.get("/healthz")
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

    return app
