"""빌드한 프론트(SPA)를 `/api` 밖의 경로에 서빙한다 (ADR-0022 §2).

- 디렉터리 안에 실제로 있는 파일이면 그 파일을 돌려준다.
- 그 밖의 경로는 SPA 진입용으로 `index.html`을 돌려준다.
- `/api`와 `/api/...`는 어떤 메서드든 지금처럼 JSON 404다(`index.html`로 넘기지 않는다).
- 디렉터리 밖으로 나가는 경로(`..`, 심볼릭 링크 포함)는 파일로 보지 않는다.
- 캐시: 해시가 붙은 `assets/*`는 1년·immutable, 그 밖(`index.html` 포함)은 `no-cache`.
  없는 `assets/*` 파일은 `index.html`이 아니라 404다(HTML을 JS·CSS로 받는 혼란을 막는다).
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
_ASSETS_PREFIX = "assets/"  # 없는 자산 요청의 404 판단에만 쓴다
_SERVED_METHODS = ("GET", "HEAD")


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _resolve_file(root: Path, relative: str) -> Path | None:
    """`root` 안의 일반 파일이면 그 경로, 아니면 None."""
    if not relative:
        return None
    try:
        candidate = (root / relative).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    except (OSError, ValueError):  # NUL 문자, 너무 긴 이름 등
        return None
    return None


def install_static(app: FastAPI, static_dir: str) -> None:
    """모든 라우트 등록 뒤에 불러 catch-all을 마지막에 둔다. index.html이 없으면 시작을 거부한다."""
    root = Path(static_dir).resolve()
    index = root / "index.html"
    if not index.is_file():
        raise RuntimeError("ORBIT_STATIC_DIR에 index.html이 없다 (프론트 빌드 결과 경로 확인)")

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def spa(request: Request, full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            return _not_found()
        if request.method not in _SERVED_METHODS:
            return _not_found()
        found = _resolve_file(root, full_path)
        if found is not None:
            # 요청 문자열이 아니라 실제로 찾은 파일 위치로 판단한다(`assets/../index.html` 대비)
            in_assets = found.relative_to(root).parts[0] == "assets"
            cache = ASSET_CACHE_CONTROL if in_assets else NO_CACHE
            return FileResponse(found, headers={"Cache-Control": cache})
        if full_path.startswith(_ASSETS_PREFIX):
            return _not_found()
        return FileResponse(index, headers={"Cache-Control": NO_CACHE})
