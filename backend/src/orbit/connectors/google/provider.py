"""연결 기반 액세스 토큰 공급자 (ADR-0016 §3·§4). `AccessTokenProvider` 구현.

- 복호화한 리프레시 토큰으로 액세스 토큰을 발급해 메모리에만 캐시한다(만료 60초 전에 갱신).
  액세스 토큰은 DB에 저장하지 않는다(v0). 리프레시 토큰은 갱신할 때마다 복호화하고 바로 버린다.
- `invalid_grant`면 연결을 `error`로 바꾸고(토큰 암호문 삭제) `PermissionRevoked`를 던진다.
- 연결이 `active`가 아니거나 암호문이 없거나 복호화할 수 없으면 `PermissionRevoked`다.
- 네트워크 오류·5xx·그 밖의 응답은 `TransientError`다(연결 상태는 바꾸지 않는다).
- 토큰·응답 본문은 로그와 예외 메시지에 넣지 않는다.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

import httpx
from pydantic import SecretStr

from orbit.connections import store
from orbit.connectors.contracts import PermissionRevoked, TransientError
from orbit.core.clock import Clock
from orbit.crypto.envelope import EnvelopeCipher, EnvelopeError
from orbit.db.session import AsyncDatabase

logger = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(seconds=60)
HTTP_TIMEOUT_SECONDS = 10.0


class ConnectionTokenProvider:
    def __init__(
        self,
        *,
        db: AsyncDatabase,
        cipher: EnvelopeCipher,
        clock: Clock,
        workspace_id: uuid.UUID,
        connection_id: uuid.UUID,
        client_id: str,
        client_secret: SecretStr,
        token_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._db = db
        self._cipher = cipher
        self._clock = clock
        self._ws = workspace_id
        self._id = connection_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._transport = transport
        self._cached: SecretStr | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"ConnectionTokenProvider(connection_id={self._id})"

    async def access_token(self) -> str:
        async with self._lock:
            if (
                self._cached is not None
                and self._expires_at is not None
                and self._clock.now() + REFRESH_MARGIN < self._expires_at
            ):
                return self._cached.get_secret_value()
            return await self._refresh_locked()

    async def refresh(self) -> str:
        async with self._lock:
            return await self._refresh_locked()

    async def _refresh_locked(self) -> str:
        self._cached = None
        self._expires_at = None
        refresh_token = await self._load_refresh_token()
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token.get_secret_value(),
            "client_id": self._client_id,
            "client_secret": self._client_secret.get_secret_value(),
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=HTTP_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(self._token_url, data=form)
        except httpx.HTTPError:
            raise TransientError("액세스 토큰 발급 요청에 실패했다") from None
        if response.status_code != 200:
            if await self._is_invalid_grant(response):
                await self._mark_error()
                raise PermissionRevoked("invalid_grant: 연결을 다시 해야 한다")
            raise TransientError(f"액세스 토큰 발급 실패(HTTP {response.status_code})")
        try:
            body = response.json()
            token = body["access_token"]
            expires_in = float(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TransientError("액세스 토큰 응답을 읽을 수 없다") from None
        if not isinstance(token, str) or not token:
            raise TransientError("액세스 토큰 응답을 읽을 수 없다")
        self._cached = SecretStr(token)
        self._expires_at = self._clock.now() + timedelta(seconds=expires_in)
        return token

    @staticmethod
    async def _is_invalid_grant(response: httpx.Response) -> bool:
        if response.status_code not in (400, 401):
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("error") == "invalid_grant"

    async def _load_refresh_token(self) -> SecretStr:
        async with self._db.workspace_session(self._ws) as s:
            loaded = await store.load_sealed(s, self._ws, self._id)
        if loaded is None or loaded[0] != "active" or loaded[1] is None:
            raise PermissionRevoked("연결이 활성 상태가 아니다")
        try:
            return self._cipher.open(loaded[1], connection_id=self._id, workspace_id=self._ws)
        except EnvelopeError as exc:
            logger.error("연결 토큰 복호화 실패 connection_id=%s kind=%s", self._id, exc.kind)
            raise PermissionRevoked("연결 토큰을 읽을 수 없다") from None

    async def _mark_error(self) -> None:
        async with self._db.workspace_session(self._ws) as s:
            await store.mark_dead(s, self._ws, self._id, status="error", now=self._clock.now())
        logger.warning("연결 토큰이 거부돼 연결을 error로 바꿨다 connection_id=%s", self._id)
