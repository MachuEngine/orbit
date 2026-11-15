"""데스크톱 로그인 핸드오프 (ADR-0024 §4, 마이그레이션 0012).

Google 로그인은 앱 창에서 열 수 없어 시스템 브라우저에서 끝낸다. 브라우저에는 세션을 만들지 않고,
일회용 핸드오프 코드를 만들어 `http://127.0.0.1:<port>/callback`으로 넘긴다. 앱이 그 코드와
PKCE `code_verifier`를 `POST /api/auth/desktop/exchange`로 보내면 서버가 검증하고 앱 창에 세션을
준다.

- 코드: `secrets.token_urlsafe(32)`(256비트), 유효 60초, 1회용. DB에는 SHA-256 해시,
  `code_challenge`,
  사용자·워크스페이스 ID, 만료·사용 시각만 둔다. 코드 원문은 로그·예외에 넣지 않는다.
- 교환 검증: 해시 조회, 만료, 미사용, `base64url(sha256(code_verifier)) == code_challenge`. 틀린
  이유를
  구분하지 않는다(`None`).
- 리디렉션 호스트는 `127.0.0.1` 고정이다(`loopback_url`). 사용자 입력은 포트와 앱 쪽 `state`뿐이다.
"""

import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from sqlalchemy import text

from orbit.auth.google import DesktopLoginRequest, pkce_challenge
from orbit.auth.sessions import IssuedSession, hash_token
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase

HANDOFF_TTL_SECONDS = 60
PURGE_AFTER_EXPIRY_SECONDS = 3600
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PATH = "/callback"

_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
_APP_STATE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_CODE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_ERROR_KIND = re.compile(r"[a-z_]{1,32}")


def parse_desktop_request(
    code_challenge: str | None, port: str | None, app_state: str | None
) -> DesktopLoginRequest | None:
    """`/auth/desktop/start` 입력 검증. 하나라도 형식이 다르면 None이다."""
    if code_challenge is None or port is None or app_state is None:
        return None
    if not _CHALLENGE.fullmatch(code_challenge) or not _APP_STATE.fullmatch(app_state):
        return None
    if not (port.isascii() and port.isdigit() and len(port) <= 5):
        return None
    number = int(port)
    if not 1024 <= number <= 65535:
        return None
    return DesktopLoginRequest(code_challenge=code_challenge, port=number, app_state=app_state)


def loopback_url(
    request: DesktopLoginRequest, *, code: str | None = None, error: str | None = None
) -> str:
    """앱의 loopback 수신기 주소. 호스트는 `127.0.0.1` 고정이고, 오류 종류는 소문자·밑줄만 쓴다."""
    params = {"state": request.app_state}
    if code is not None:
        params = {"code": code, **params}
    elif error is not None:
        safe = error if _ERROR_KIND.fullmatch(error) else "unavailable"
        params = {"error": safe, **params}
    return f"http://{LOOPBACK_HOST}:{request.port}{LOOPBACK_PATH}?{urlencode(params)}"


async def issue_handoff_code(
    db: AsyncDatabase,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    code_challenge: str,
    clock: Clock,
    ids: Uuid7Generator,
) -> str:
    """핸드오프 코드를 만들어 해시만 저장하고 원문을 돌려준다. 호출 직후 리디렉션에만 쓴다."""
    code = secrets.token_urlsafe(32)
    now = clock.now()
    async with db.workspace_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO desktop_login_code (id, workspace_id, user_id, code_hash,"
                " code_challenge, created_at, expires_at)"
                " VALUES (:id, :ws, :uid, :h, :c, :now, :exp)"
            ),
            {
                "id": ids.next(),
                "ws": workspace_id,
                "uid": user_id,
                "h": hash_token(code),
                "c": code_challenge,
                "now": now,
                "exp": now + timedelta(seconds=HANDOFF_TTL_SECONDS),
            },
        )
    return code


async def exchange_handoff_code(
    db: AsyncDatabase,
    *,
    code: str,
    code_verifier: str,
    clock: Clock,
    ids: Uuid7Generator,
    session_ttl_seconds: float,
) -> IssuedSession | None:
    """코드와 검증기를 확인하고 세션을 발급한다. 실패 사유는 구분하지 않고 None이다.

    코드 사용 처리와 세션 생성은 한 트랜잭션이다. 이미 쓴 코드를 올바른 검증기로 다시 보내면
    그 코드로 만든 세션을 폐기한다(재사용 시도).
    """
    if not _CODE.fullmatch(code) or not _VERIFIER.fullmatch(code_verifier):
        return None
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = clock.now()
    expires_at = now + timedelta(seconds=session_ttl_seconds)
    session_id = ids.next()
    outcome, workspace_id = await db.unscoped_desktop_login_exchange(
        code_hash=hash_token(code),
        code_challenge=pkce_challenge(code_verifier),
        now=now,
        session_id=session_id,
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf_token),
        expires_at=expires_at,
    )
    if outcome != "ok" or workspace_id is None:
        return None
    return IssuedSession(session_id, workspace_id, token, csrf_token, expires_at)


async def purge_expired_handoffs(db: AsyncDatabase, clock: Clock) -> int:
    """만료 후 1시간이 지난 핸드오프 코드·연결 티켓을 지우고 지운 개수를 돌려준다."""
    return await db.unscoped_desktop_handoff_purge(
        clock.now() - timedelta(seconds=PURGE_AFTER_EXPIRY_SECONDS)
    )
