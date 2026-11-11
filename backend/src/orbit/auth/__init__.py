"""쿠키 세션 (ADR-0013 §2).

- 토큰은 `secrets.token_urlsafe(32)`로 만들고 DB에는 SHA-256 해시만 저장한다.
- 세션은 워크스페이스에 묶이고 만료 시각을 가진다. 시각은 주입된 `Clock`에서만 얻는다.
- 조회는 `auth_session_lookup` 함수(마이그레이션 0004) 하나로만 한다. 발급·폐기는
  워크스페이스 세션을 거친다(RLS).
- 토큰 원문은 로그·예외에 넣지 않는다.
- Google 로그인 흐름은 `orbit.auth.google`, 사용자 조회·생성은 `orbit.auth.users`다(ADR-0016).
"""

from orbit.auth.sessions import (
    IssuedSession,
    SessionInfo,
    csrf_matches,
    hash_token,
    issue_session,
    lookup_session,
    revoke_session,
)

__all__ = [
    "IssuedSession",
    "SessionInfo",
    "csrf_matches",
    "hash_token",
    "issue_session",
    "lookup_session",
    "revoke_session",
]
