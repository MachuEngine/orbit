"""로그인 사용자 조회·생성 (ADR-0016 §1, 마이그레이션 0005).

`app_user`는 워크스페이스에 속하지 않아 `orbit_app`이 테이블을 직접 읽지 못한다.
`login_upsert_user`·`auth_session_profile` 함수(SECURITY DEFINER)로만 접근한다.
ID와 시각은 주입한 `Clock`·`Uuid7Generator`로 정해 넘긴다(이미 있는 사용자면 쓰이지 않는다).
"""

import uuid
from dataclasses import dataclass

from orbit.auth.sessions import hash_token
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase


@dataclass(frozen=True)
class LoginResult:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    created: bool


@dataclass(frozen=True)
class SessionProfile:
    """세션에 묶인 본인 정보. 다른 사용자의 정보는 이 경로로 읽을 수 없다."""

    user_id: uuid.UUID
    email: str
    display_name: str


async def login_upsert_user(
    db: AsyncDatabase,
    *,
    google_sub: str,
    email: str,
    display_name: str,
    clock: Clock,
    ids: Uuid7Generator,
) -> LoginResult:
    row = await db.unscoped_login_upsert_user(
        google_sub=google_sub,
        email=email,
        display_name=display_name,
        user_id=ids.next(),
        workspace_id=ids.next(),
        now=clock.now(),
    )
    user_id, workspace_id, created = row
    if workspace_id is None:
        raise RuntimeError("사용자의 개인 워크스페이스를 찾을 수 없다")
    return LoginResult(uuid.UUID(str(user_id)), uuid.UUID(str(workspace_id)), bool(created))


async def session_profile(db: AsyncDatabase, token: str) -> SessionProfile | None:
    if not token or len(token) > 256:
        return None
    row = await db.unscoped_auth_session_profile(hash_token(token))
    if row is None:
        return None
    user_id, email, display_name = row
    return SessionProfile(uuid.UUID(str(user_id)), str(email), str(display_name))
