"""연결 완료·해제 절차 (ADR-0016 §2). 트랜잭션 경계와 비밀 처리 순서를 한곳에 둔다."""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from orbit.connections import store
from orbit.connections.oauth import (
    ConnectError,
    ConnectGrant,
    GoogleConnectOAuth,
    RevokeOutcome,
    missing_scopes,
)
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.crypto.envelope import EnvelopeCipher, EnvelopeError
from orbit.db.session import AsyncDatabase

logger = logging.getLogger(__name__)


async def save_grant(
    db: AsyncDatabase,
    oauth: GoogleConnectOAuth,
    cipher: EnvelopeCipher,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    grant: ConnectGrant,
    clock: Clock,
    ids: Uuid7Generator,
) -> uuid.UUID:
    """코드 교환 결과를 저장한다. 범위가 모자라거나 refresh_token이 없으면 `error` 행을 남기고
    `ConnectError`를 던진다. 이미 활성 연결이 있으면 저장하지 않는다."""
    refresh = grant.refresh_token
    problem: ConnectError | None = None
    if missing_scopes(grant.scopes):
        problem = ConnectError("scope_missing")
    elif refresh is None:
        problem = ConnectError("no_refresh_token")

    connection_id = ids.next()
    sealed = None
    if problem is None and refresh is not None:
        sealed = cipher.seal(
            refresh.get_secret_value(), connection_id=connection_id, workspace_id=workspace_id
        )
    now = clock.now()
    try:
        async with db.workspace_session(workspace_id) as s:
            existing = await store.find_open(s, workspace_id, for_update=True)
            if existing is not None:
                if existing.status == "active":
                    raise ConnectError("already_connected")
                # 실패했거나 끝나지 않은 이전 연결은 새 연결로 바꾼다.
                await store.mark_dead(s, workspace_id, existing.id, status="revoked", now=now)
            await store.insert_connection(
                s,
                connection_id=connection_id,
                ws=workspace_id,
                user_id=user_id,
                account_email=grant.email,
                account_sub=grant.sub,
                scopes=list(grant.scopes),
                status="active" if problem is None else "error",
                sealed=sealed,
                now=now,
            )
    except IntegrityError:
        problem = ConnectError("already_connected")
    except ConnectError as exc:
        problem = exc
    if problem is not None:
        # Google은 refresh token 하나를 철회하면 같은 사용자·클라이언트의 권한 전체를 거둔다고
        # 보고 설계한다. 같은 워크스페이스에 같은 계정의 활성 연결이 있으면 새 토큰은 철회하지 않고
        # 버린다(두 탭 경쟁에서 기존 연결을 끊지 않는다). 없을 때만 쓰지 못할 권한을 거둔다.
        if refresh is not None and problem.kind in ("scope_missing", "already_connected"):
            async with db.workspace_session(workspace_id) as s:
                keep = await store.has_active_with_sub(s, workspace_id, grant.sub)
            if not keep:
                await oauth.revoke(refresh.get_secret_value())
        raise problem
    return connection_id


@dataclass(frozen=True)
class DisconnectResult:
    found: bool
    status: str
    provider_revoke: RevokeOutcome


async def disconnect(
    db: AsyncDatabase,
    oauth: GoogleConnectOAuth | None,
    cipher: EnvelopeCipher | None,
    *,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    clock: Clock,
) -> DisconnectResult:
    """Google 철회를 시도한 뒤 결과와 상관없이 로컬을 `revoked`로 바꾸고 토큰을 지운다.

    연결 데이터 삭제 전파(D7-4): 지금은 연결에 딸린 동기화 데이터가 없어 전파할 대상이 없다.
    동기화 테이블이 생기면 이 자리에서 같은 트랜잭션으로 지운다.
    """
    async with db.workspace_session(workspace_id) as s:
        row = await store.get_connection(s, workspace_id, connection_id)
        loaded = await store.load_sealed(s, workspace_id, connection_id) if row else None
    if row is None:
        return DisconnectResult(False, "", "none")
    if row.status == "revoked":
        return DisconnectResult(True, "revoked", "none")

    outcome: RevokeOutcome = "none"
    sealed = loaded[1] if loaded else None
    if sealed is not None:
        if oauth is None or cipher is None:
            outcome = "failed"
        else:
            try:
                token = cipher.open(sealed, connection_id=connection_id, workspace_id=workspace_id)
            except EnvelopeError as exc:
                logger.warning("연결 해제: 토큰 복호화 실패 kind=%s", exc.kind)
                outcome = "failed"
            else:
                outcome = await oauth.revoke(token.get_secret_value())
    if outcome == "failed":
        logger.warning(
            "연결 해제: Google 철회 실패, 로컬만 해제한다 connection_id=%s", connection_id
        )

    async with db.workspace_session(workspace_id) as s:
        await store.mark_dead(s, workspace_id, connection_id, status="revoked", now=clock.now())
    return DisconnectResult(True, "revoked", outcome)
