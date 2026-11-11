"""세션 발급·조회·폐기."""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text

from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase


@dataclass(frozen=True)
class IssuedSession:
    """발급 직후에만 존재하는 토큰 원문. 저장하거나 기록하지 않는다."""

    session_id: uuid.UUID
    workspace_id: uuid.UUID
    token: str
    csrf_token: str
    expires_at: datetime

    def __repr__(self) -> str:  # 토큰이 로그·예외 메시지에 새지 않게 한다
        return f"IssuedSession(session_id={self.session_id}, workspace_id={self.workspace_id})"


@dataclass(frozen=True)
class SessionInfo:
    session_id: uuid.UUID
    workspace_id: uuid.UUID
    csrf_token_hash: str
    expires_at: datetime


def hash_token(token: str) -> str:
    """토큰은 엔트로피가 충분한 무작위 값이라 솔트 없는 SHA-256 hex로 저장한다."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_matches(candidate: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(candidate), stored_hash)


async def issue_session(
    db: AsyncDatabase,
    *,
    workspace_id: uuid.UUID,
    clock: Clock,
    ids: Uuid7Generator,
    ttl_seconds: float,
) -> IssuedSession:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = clock.now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    session_id = ids.next()
    async with db.workspace_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO auth_session (id, workspace_id, token_hash, csrf_token_hash,"
                " created_at, expires_at) VALUES (:id, :ws, :th, :ch, :now, :exp)"
            ),
            {
                "id": session_id,
                "ws": workspace_id,
                "th": hash_token(token),
                "ch": hash_token(csrf_token),
                "now": now,
                "exp": expires_at,
            },
        )
    return IssuedSession(session_id, workspace_id, token, csrf_token, expires_at)


async def lookup_session(db: AsyncDatabase, token: str, *, clock: Clock) -> SessionInfo | None:
    """유효한 세션만 돌려준다. 없거나 폐기됐거나 만료됐으면 None."""
    if not token or len(token) > 256:
        return None
    row = await db.unscoped_auth_session_lookup(hash_token(token))
    if row is None:
        return None
    session_id, workspace_id, csrf_hash, expires_at, revoked_at = row
    if revoked_at is not None or clock.now() >= expires_at:
        return None
    return SessionInfo(session_id, workspace_id, csrf_hash, expires_at)


async def revoke_session(
    db: AsyncDatabase, *, workspace_id: uuid.UUID, session_id: uuid.UUID, clock: Clock
) -> None:
    async with db.workspace_session(workspace_id) as s:
        await s.execute(
            text(
                "UPDATE auth_session SET revoked_at = :now"
                " WHERE id = :id AND workspace_id = :ws AND revoked_at IS NULL"
            ),
            {"now": clock.now(), "id": session_id, "ws": workspace_id},
        )
