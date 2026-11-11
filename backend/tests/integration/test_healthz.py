import httpx
from pydantic import SecretStr

from orbit.api.app import create_app
from orbit.core.config import Settings


async def _get_healthz(url: str) -> httpx.Response:
    app = create_app(Settings(database_url=SecretStr(url), env="dev"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/healthz")


async def test_healthz_ok_with_db(app_url: str) -> None:
    response = await _get_healthz(app_url)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_healthz_503_when_db_unreachable() -> None:
    # 합성 URL: 닫힌 포트. 실제 DB가 없어도 실행된다.
    response = await _get_healthz("postgresql+psycopg://x:y@127.0.0.1:1/none?connect_timeout=2")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
