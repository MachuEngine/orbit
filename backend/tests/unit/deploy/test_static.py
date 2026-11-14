"""정적 프론트 서빙 (ADR-0022 §2). 파일은 모두 합성이다."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from orbit.api.app import create_app
from orbit.api.static import ASSET_CACHE_CONTROL, NO_CACHE, _resolve_file
from orbit.core.config import Settings

DB = SecretStr("postgresql+psycopg://x:y@localhost:1/z")  # lifespan을 열지 않아 연결하지 않는다
OUTSIDE_SECRET = "outside-secret-synthetic"


@pytest.fixture
def static_root(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>spa-index</html>", encoding="utf-8")
    (root / "assets" / "app-abc123.js").write_text("console.log('app')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(OUTSIDE_SECRET, encoding="utf-8")
    return root


@pytest.fixture
async def client(static_root: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(database_url=DB, env="dev", static_dir=str(static_root)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_root_and_unknown_paths_return_index_with_no_cache(
    client: httpx.AsyncClient,
) -> None:
    for path in ("/", "/index.html", "/conversations/abc", "/settings/notifications"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert "spa-index" in response.text
        assert response.headers["cache-control"] == NO_CACHE


async def test_existing_asset_is_served_with_long_cache(client: httpx.AsyncClient) -> None:
    response = await client.get("/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.text == "console.log('app')"
    assert response.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert "immutable" in ASSET_CACHE_CONTROL


async def test_other_files_are_not_cached_long(client: httpx.AsyncClient) -> None:
    response = await client.get("/favicon.svg")
    assert response.text == "<svg/>"
    assert response.headers["cache-control"] == NO_CACHE


async def test_missing_asset_is_404_not_index(client: httpx.AsyncClient) -> None:
    response = await client.get("/assets/missing-0000.js")
    assert response.status_code == 404
    assert "spa-index" not in response.text


async def test_api_paths_keep_json_404_for_any_method(client: httpx.AsyncClient) -> None:
    for method in ("GET", "POST", "DELETE"):
        response = await client.request(method, "/api/no-such-route")
        assert response.status_code == 404, method
        assert response.json() == {"detail": "Not Found"}
    assert (await client.get("/api")).json() == {"detail": "Not Found"}
    # 존재하는 API 경로는 영향이 없다(라우트가 먼저다)
    assert (
        (await client.get("/api/auth/session"))
        .headers["content-type"]
        .startswith("application/json")
    )


async def test_non_get_outside_api_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/conversations/abc")).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2e%2e%2fsecret.txt",
        "/..%2fsecret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/%252e%252e/secret.txt",
        "//etc/passwd",
        "/..%00/secret.txt",
    ],
)
async def test_path_traversal_never_leaks_outside_files(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)
    assert OUTSIDE_SECRET not in response.text
    assert "root:" not in response.text


def test_resolve_file_rejects_escapes_and_symlinks(static_root: Path, tmp_path: Path) -> None:
    root = static_root.resolve()
    assert _resolve_file(root, "index.html") == root / "index.html"
    for relative in ("../secret.txt", "assets/../../secret.txt", "", "assets", "bad\x00name"):
        assert _resolve_file(root, relative) is None, relative
    (static_root / "link.txt").symlink_to(tmp_path / "secret.txt")
    assert _resolve_file(root, "link.txt") is None  # 심볼릭 링크로 밖을 가리켜도 거부한다


async def test_assets_dotdot_to_index_is_not_cached_long(client: httpx.AsyncClient) -> None:
    # 요청 문자열이 assets/로 시작해도 실제 파일이 index.html이면 no-cache다
    response = await client.get("/assets/../index.html")
    if response.status_code == 200:
        assert response.headers["cache-control"] == NO_CACHE


def test_static_dir_without_index_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="index.html"):
        create_app(Settings(database_url=DB, env="dev", static_dir=str(tmp_path)))


def test_blank_static_dir_is_unset() -> None:
    assert Settings(database_url=DB, static_dir="  ").static_dir is None
    app = create_app(Settings(database_url=DB, env="dev"))
    assert "/{full_path:path}" not in {getattr(r, "path", "") for r in app.routes}
