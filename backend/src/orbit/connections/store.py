"""`connection` 테이블 접근 (마이그레이션 0006). 모든 함수는 워크스페이스 세션을 받고
`workspace_id`로 거른다. 토큰 원문은 다루지 않는다(봉투 암호문만 읽고 쓴다)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.crypto.envelope import SealedSecret

PROVIDER = "google"


@dataclass(frozen=True)
class ConnectionRow:
    id: uuid.UUID
    provider: str
    status: str
    account_email: str
    scopes: tuple[str, ...]
    created_at: datetime


_COLUMNS = "id, provider, status, account_email, scopes, created_at"


def _row(r: object) -> ConnectionRow:
    return ConnectionRow(
        id=r.id,  # type: ignore[attr-defined]
        provider=r.provider,  # type: ignore[attr-defined]
        status=r.status,  # type: ignore[attr-defined]
        account_email=r.account_email,  # type: ignore[attr-defined]
        scopes=tuple(r.scopes),  # type: ignore[attr-defined]
        created_at=r.created_at,  # type: ignore[attr-defined]
    )


async def list_open_connections(session: AsyncSession, ws: uuid.UUID) -> list[ConnectionRow]:
    rows = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM connection WHERE workspace_id = :ws"  # noqa: S608
            " AND status <> 'revoked'"
            " ORDER BY created_at, id"
        ),
        {"ws": ws},
    )
    return [_row(r) for r in rows]


async def get_connection(
    session: AsyncSession, ws: uuid.UUID, connection_id: uuid.UUID
) -> ConnectionRow | None:
    r = (
        await session.execute(
            text(
                f"SELECT {_COLUMNS} FROM connection WHERE id = :id AND workspace_id = :ws"  # noqa: S608
            ),
            {"id": connection_id, "ws": ws},
        )
    ).one_or_none()
    return None if r is None else _row(r)


async def find_open(
    session: AsyncSession, ws: uuid.UUID, *, for_update: bool = False
) -> ConnectionRow | None:
    """폐기되지 않은 Google 연결(워크스페이스당 최대 1개)."""
    lock = " FOR UPDATE" if for_update else ""
    r = (
        await session.execute(
            text(
                f"SELECT {_COLUMNS} FROM connection WHERE workspace_id = :ws"  # noqa: S608
                f" AND provider = :p AND status <> 'revoked'{lock}"
            ),
            {"ws": ws, "p": PROVIDER},
        )
    ).one_or_none()
    return None if r is None else _row(r)


async def find_active_id(session: AsyncSession, ws: uuid.UUID) -> uuid.UUID | None:
    value = (
        await session.execute(
            text(
                "SELECT id FROM connection WHERE workspace_id = :ws AND provider = :p"
                " AND status = 'active'"
            ),
            {"ws": ws, "p": PROVIDER},
        )
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


async def has_active_with_sub(session: AsyncSession, ws: uuid.UUID, sub: str) -> bool:
    """같은 워크스페이스에 같은 Google 계정(`sub`)의 활성 연결이 있는지."""
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM connection WHERE workspace_id = :ws AND provider = :p"
                " AND status = 'active' AND account_sub = :sub LIMIT 1"
            ),
            {"ws": ws, "p": PROVIDER, "sub": sub},
        )
    ).first()
    return found is not None


async def load_sealed(
    session: AsyncSession, ws: uuid.UUID, connection_id: uuid.UUID
) -> tuple[str, SealedSecret | None] | None:
    """(상태, 봉투 암호문). 연결이 없으면 None, 토큰이 없으면 암호문이 None."""
    r = (
        await session.execute(
            text(
                "SELECT status, token_ciphertext, token_nonce, wrapped_dek, kek_id"
                " FROM connection WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": connection_id, "ws": ws},
        )
    ).one_or_none()
    if r is None:
        return None
    if r.token_ciphertext is None:
        return str(r.status), None
    return str(r.status), SealedSecret(
        ciphertext=bytes(r.token_ciphertext),
        nonce=bytes(r.token_nonce),
        wrapped_dek=bytes(r.wrapped_dek),
        kek_id=str(r.kek_id),
    )


async def insert_connection(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    ws: uuid.UUID,
    user_id: uuid.UUID,
    account_email: str,
    account_sub: str,
    scopes: list[str],
    status: str,
    sealed: SealedSecret | None,
    now: datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO connection (id, workspace_id, user_id, provider, account_email,"
            " account_sub, scopes, status, token_ciphertext, token_nonce, wrapped_dek, kek_id,"
            " created_at, updated_at) VALUES (:id, :ws, :uid, :p, :email, :sub, :scopes, :status,"
            " :ct, :nonce, :dek, :kek, :now, :now)"
        ),
        {
            "id": connection_id,
            "ws": ws,
            "uid": user_id,
            "p": PROVIDER,
            "email": account_email,
            "sub": account_sub,
            "scopes": scopes,
            "status": status,
            "ct": None if sealed is None else sealed.ciphertext,
            "nonce": None if sealed is None else sealed.nonce,
            "dek": None if sealed is None else sealed.wrapped_dek,
            "kek": None if sealed is None else sealed.kek_id,
            "now": now,
        },
    )


async def mark_dead(
    session: AsyncSession,
    ws: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    status: str,
    now: datetime,
) -> None:
    """`revoked` 또는 `error`로 바꾸고 토큰 암호문을 지운다."""
    if status not in ("revoked", "error"):
        raise ValueError(status)
    await session.execute(
        text(
            "UPDATE connection SET status = :status, token_ciphertext = NULL, token_nonce = NULL,"
            " wrapped_dek = NULL, kek_id = NULL, updated_at = :now,"
            " revoked_at = CASE WHEN :status = 'revoked' THEN :now ELSE revoked_at END"
            " WHERE id = :id AND workspace_id = :ws AND status <> 'revoked'"
        ),
        {"status": status, "now": now, "id": connection_id, "ws": ws},
    )
