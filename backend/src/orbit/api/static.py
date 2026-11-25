"""빌드한 프론트(SPA)를 `/api` 밖의 경로에 서빙한다 (ADR-0022 §2).

- 디렉터리 안에 실제로 있는 파일이면 그 파일을 돌려준다.
- 그 밖의 경로는 SPA 진입용으로 `index.html`을 돌려준다.
- `/api`와 `/api/...`는 `index.html`로 넘기지 않고 JSON 404다. 앞 슬래시(`//api`)와 대소문자(`/API`)
  변형도 같다. 퍼센트 인코딩은 서버가 라우팅 전에 한 번 풀어 준다.
- `/api` 경로는 catch-all이 아예 매칭하지 않는다. 그래서 `/api`의 라우트 응답(존재하는 경로에 틀린
  메서드는 405, 없는 경로는 JSON 404)이 정적 서빙 유무와 상관없이 같다.
- `/api` 밖에서 GET·HEAD 외 메서드(OPTIONS·TRACE 포함)는 모두 JSON 404다.
- 디렉터리 밖으로 나가는 경로(`..`, 심볼릭 링크 포함)는 파일로 보지 않는다.
- 캐시: 해시가 붙은 `assets/*`는 1년·immutable, 그 밖(`index.html` 포함)은 `no-cache`.
  없는 `assets/*` 파일은 `index.html`이 아니라 404다(HTML을 JS·CSS로 받는 혼란을 막는다).
"""

import posixpath
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import Request
from starlette.routing import Match, Route
from starlette.types import Scope

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"


_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]


def _first_segment(path: str) -> str:
    """`.`·`..`·중복 슬래시를 정리한 뒤의 첫 세그먼트(`/x/../api/x` -> `api`)."""
    return posixpath.normpath("/" + path.lstrip("/")).lstrip("/").split("/", 1)[0]


def _is_api_path(path: str) -> bool:
    """첫 세그먼트가 api이면 API 경로다. 경로 정리·앞 슬래시 중복·대소문자 변형도 같게 본다."""
    return _first_segment(path).casefold() == "api"


class _SpaRoute(Route):
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if _is_api_path(scope["path"]):
            return Match.NONE, {}
        return super().matches(scope)


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

    async def spa(request: Request) -> Response:
        if request.method not in ("GET", "HEAD"):
            return _not_found()
        full_path = request.path_params["full_path"]
        found = _resolve_file(root, full_path)
        if found is not None:
            # 요청 문자열이 아니라 실제로 찾은 파일 위치로 판단한다(`assets/../index.html` 대비)
            in_assets = found.relative_to(root).parts[0] == "assets"
            cache = ASSET_CACHE_CONTROL if in_assets else NO_CACHE
            return FileResponse(found, headers={"Cache-Control": cache})
        if _first_segment(full_path) == "assets" and full_path.rstrip("/") != "assets":
            return _not_found()
        return FileResponse(index, headers={"Cache-Control": NO_CACHE})

    app.router.routes.append(_SpaRoute("/{full_path:path}", spa, methods=_METHODS))
