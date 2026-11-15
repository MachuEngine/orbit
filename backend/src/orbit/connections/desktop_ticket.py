"""데스크톱 계정 연결 티켓 (ADR-0024 §5, 마이그레이션 0012).

연결 흐름은 `state`를 브라우저 쿠키에 두는데, 데스크톱 앱 창은 Google 동의 화면을 열 수 없다. 앱
창이
세션으로 티켓을 받고, 시스템 브라우저가 `GET /api/connections/google/desktop-begin?ticket=…`로
흐름을
시작한다.

- 티켓: `secrets.token_urlsafe(32)`, 유효 60초, 1회용,
  워크스페이스·사용자·의도(`connect`/`docs_scope`)·
  대상 연결에 묶이고, 앱이 만든 PKCE `code_challenge`·loopback 포트·앱 `state`도 묶는다. DB에는
  SHA-256 해시만 둔다. 원문은 로그·예외에 넣지 않는다.
- 사용 처리는 `desktop_connect_ticket_redeem` 함수의 조건부 `UPDATE`라 동시에 두 번 와도 한 쪽만
  성공한다.
  없음·만료·재사용은 구분하지 않는다(`None`).
- 대기 행(`desktop_connect_pending`): 데스크톱 콜백은 토큰을 교환하지 않고 Google 인가 코드와 이
  흐름의
  PKCE 검증기·nonce를 봉투 암호화해 5분짜리 대기 행에 둔다. 앱이 자기 세션으로 완료 코드와 PKCE
  `code_verifier`를 제출해야만(`claim_pending`) 꺼내 쓴다. 세션의 워크스페이스·사용자가 다르거나
  만료·재사용·verifier 불일치면 `None`이다(RLS 안에서 한 `UPDATE ... RETURNING`).
"""

import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from sqlalchemy import text

from orbit.auth.google import DesktopLoginRequest, LoginFlow
from orbit.auth.sessions import hash_token
from orbit.connections.oauth import DesktopBinding
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.crypto.envelope import EnvelopeCipher, EnvelopeError, SealedSecret
from orbit.db.session import AsyncDatabase

TICKET_TTL_SECONDS = 60
PENDING_TTL_SECONDS = 300
TICKET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
BEGIN_PATH = "/api/connections/google/desktop-begin"

Intent = Literal["connect", "docs_scope"]


@dataclass(frozen=True)
class RedeemedTicket:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    intent: Intent
    connection_id: uuid.UUID | None
    request: DesktopLoginRequest


async def issue_ticket(
    db: AsyncDatabase,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    intent: Intent,
    connection_id: uuid.UUID | None,
    request: DesktopLoginRequest,
    clock: Clock,
    ids: Uuid7Generator,
) -> str:
    """티켓을 만들어 해시만 저장하고 원문을 돌려준다. 앱의 challenge·포트·앱 `state`를 묶는다."""
    ticket = secrets.token_urlsafe(32)
    now = clock.now()
    async with db.workspace_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO desktop_connect_ticket (id, workspace_id, user_id, ticket_hash,"
                " intent, connection_id, code_challenge, port, app_state, created_at, expires_at)"
                " VALUES (:id, :ws, :uid, :h, :intent, :cid, :dc, :dp, :da, :now, :exp)"
            ),
            {
                "id": ids.next(),
                "ws": workspace_id,
                "uid": user_id,
                "h": hash_token(ticket),
                "intent": intent,
                "cid": connection_id,
                "dc": request.code_challenge,
                "dp": request.port,
                "da": request.app_state,
                "now": now,
                "exp": now + timedelta(seconds=TICKET_TTL_SECONDS),
            },
        )
    return ticket


async def redeem_ticket(db: AsyncDatabase, ticket: str, *, clock: Clock) -> RedeemedTicket | None:
    """티켓을 사용 처리하고 묶인 값을 돌려준다. 형식 오류·없음·만료·재사용은 None이다."""
    if not TICKET_PATTERN.fullmatch(ticket):
        return None
    row = await db.unscoped_desktop_connect_ticket_redeem(hash_token(ticket), clock.now())
    if row is None:
        return None
    user_id, workspace_id, intent, connection_id, challenge, port, app_state = row
    if intent not in ("connect", "docs_scope"):
        return None
    return RedeemedTicket(
        uuid.UUID(str(user_id)),
        uuid.UUID(str(workspace_id)),
        intent,
        None if connection_id is None else uuid.UUID(str(connection_id)),
        DesktopLoginRequest(str(challenge), int(port), str(app_state)),
    )


@dataclass(frozen=True)
class PendingConnect:
    """대기 행에서 꺼낸 값. 인가 코드·검증기는 `repr`에 나오지 않는다."""

    intent: Intent
    connection_id: uuid.UUID | None
    flow: LoginFlow
    auth_code: str = field(repr=False)


async def store_pending(
    db: AsyncDatabase,
    cipher: EnvelopeCipher,
    *,
    binding: DesktopBinding,
    intent: Intent,
    connection_id: uuid.UUID | None,
    flow: LoginFlow,
    auth_code: str,
    clock: Clock,
    ids: Uuid7Generator,
) -> str:
    """Google 인가 코드를 봉투 암호화해 대기 행에 두고 앱에 줄 완료 코드를 돌려준다(해시만 저장)."""
    completion = secrets.token_urlsafe(32)
    row_id = ids.next()
    now = clock.now()
    workspace_id = binding.workspace_id
    payload = json.dumps(
        {"c": auth_code, "v": flow.code_verifier, "n": flow.nonce}, separators=(",", ":")
    )
    sealed = cipher.seal(payload, connection_id=row_id, workspace_id=workspace_id)
    async with db.workspace_session(workspace_id) as s:
        await s.execute(
            text(
                "INSERT INTO desktop_connect_pending (id, workspace_id, user_id, code_hash,"
                " code_challenge, intent, connection_id, ciphertext, nonce, wrapped_dek, kek_id,"
                " created_at, expires_at)"
                " VALUES (:id, :ws, :uid, :h, :c, :intent, :cid, :ct, :n, :w, :k, :now, :exp)"
            ),
            {
                "id": row_id,
                "ws": workspace_id,
                "uid": binding.user_id,
                "h": hash_token(completion),
                "c": binding.request.code_challenge,
                "intent": intent,
                "cid": connection_id,
                "ct": sealed.ciphertext,
                "n": sealed.nonce,
                "w": sealed.wrapped_dek,
                "k": sealed.kek_id,
                "now": now,
                "exp": now + timedelta(seconds=PENDING_TTL_SECONDS),
            },
        )
    return completion


async def claim_pending(
    db: AsyncDatabase,
    cipher: EnvelopeCipher,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    code: str,
    code_challenge: str,
    clock: Clock,
) -> PendingConnect | None:
    """세션의 워크스페이스·사용자의 대기 행을 사용 처리하고 복호화해 돌려준다. 실패는 모두
    None이다."""
    now = clock.now()
    async with db.workspace_session(workspace_id) as s:
        row = (
            await s.execute(
                text(
                    "UPDATE desktop_connect_pending SET used_at = :now"
                    " WHERE workspace_id = :ws AND user_id = :uid AND code_hash = :h"
                    " AND code_challenge = :c AND used_at IS NULL AND expires_at > :now"
                    " RETURNING id, intent, connection_id, ciphertext, nonce, wrapped_dek, kek_id"
                ),
                {
                    "now": now,
                    "ws": workspace_id,
                    "uid": user_id,
                    "h": hash_token(code),
                    "c": code_challenge,
                },
            )
        ).first()
    if row is None or row.intent not in ("connect", "docs_scope"):
        return None
    try:
        opened = cipher.open(
            SealedSecret(
                bytes(row.ciphertext), bytes(row.nonce), bytes(row.wrapped_dek), row.kek_id
            ),
            connection_id=row.id,
            workspace_id=workspace_id,
        )
        data = json.loads(opened.get_secret_value())
        auth_code, verifier, nonce = data["c"], data["v"], data["n"]
    except (EnvelopeError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(v, str) for v in (auth_code, verifier, nonce)):
        return None
    return PendingConnect(
        intent=row.intent,
        connection_id=row.connection_id,
        flow=LoginFlow(state="", nonce=nonce, code_verifier=verifier),
        auth_code=auth_code,
    )
