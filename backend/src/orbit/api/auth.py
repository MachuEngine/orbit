"""임시 인증 (6단계 로그인 전까지, ADR-0012 §4).

`ORBIT_ENV=dev`일 때만 `X-Orbit-Workspace` 헤더의 워크스페이스 ID를 그대로 받는다.
개발 환경이 아니면 앱이 시작되지 않는다(`ensure_dev_environment`).
로그인이 생기면 이 모듈을 세션 기반 의존성으로 바꾼다.
이 헤더는 사용자 신원을 증명하지 않으므로 외부에 노출된 서버에서 켜면 안 된다.
"""

import uuid
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from orbit.core.config import Settings

WORKSPACE_HEADER = "X-Orbit-Workspace"


def ensure_dev_environment(settings: Settings) -> None:
    if settings.env != "dev":
        raise RuntimeError(
            "로그인 구현 전에는 ORBIT_ENV=dev에서만 API를 시작할 수 있다 "
            "(임시 인증은 개발 환경 전용이다)"
        )


def current_workspace(
    request: Request,
    x_orbit_workspace: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """요청의 워크스페이스 ID. 헤더가 없거나 UUID가 아니면 401."""
    settings: Settings = request.app.state.settings
    ensure_dev_environment(settings)
    if x_orbit_workspace is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"{WORKSPACE_HEADER} 헤더가 필요하다")
    try:
        return uuid.UUID(x_orbit_workspace)
    except ValueError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, f"{WORKSPACE_HEADER} 헤더가 UUID가 아니다"
        ) from None
