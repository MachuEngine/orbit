"""그래프·워커가 쓰는 주입 의존성 (ADR-0012 §4).

시계·ID 생성기·LLM·커넥터는 모두 주입한다. 도메인 코드가 `datetime.now()`나 제공자 SDK를 직접
부르지 않는다. 커넥터는 `ConnectorResolver`(워크스페이스 -> (캘린더, 메일))로 받는다. Google 구현은
`GoogleConnectorResolver`다(6B). 연결 조회가 DB를 거치므로 `connectors_for`는 비동기다.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Protocol

import httpx
import procrastinate
from pydantic import SecretStr

from orbit.connections import store as connection_store
from orbit.connections.oauth import DRIVE_SCOPES
from orbit.connectors.contracts import (
    SCOPE_MISSING_REASON,
    CalendarConnector,
    ConnectorError,
    DriveConnector,
    DriveFile,
    FilePage,
    FileQuery,
    FileText,
    MailConnector,
)
from orbit.connectors.google.calendar import GoogleCalendarConnector
from orbit.connectors.google.drive import GoogleDriveConnector
from orbit.connectors.google.gmail import GmailConnector
from orbit.connectors.google.provider import ConnectionTokenProvider
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.crypto.envelope import EnvelopeCipher
from orbit.db.session import AsyncDatabase
from orbit.llm.interface import LLM
from orbit.mailindex.deps import MailIndexDeps

DEFAULT_MAX_STEPS = 12  # 한 run의 LLM 호출 상한
DEFAULT_LEASE_SECONDS = 300.0  # 커넥터 호출 타임아웃 x (1 + 재호출 1회)보다 길게(ADR-0012 §3)


class NoConnection(Exception):
    """워크스페이스에 활성 연결이 없다. 메시지에는 워크스페이스 ID만 담는다."""


class ConnectorResolver(Protocol):
    """워크스페이스의 커넥터를 돌려준다. 활성 연결이 없으면 `NoConnection`."""

    async def connectors_for(
        self, workspace_id: uuid.UUID
    ) -> tuple[CalendarConnector, MailConnector]: ...

    async def drive_for(self, workspace_id: uuid.UUID) -> DriveConnector:
        """문서 커넥터(ADR-0020). 활성 연결이 없으면 `NoConnection`, 문서 권한이 없으면 모든
        메서드가 사유 `scope_missing`인 `ConnectorError`를 던지는 커넥터를 돌려준다."""
        ...


class ScopeMissingDrive:
    """문서 권한이 없는 연결용 커넥터. 외부 호출 없이 모든 메서드가 `scope_missing`을 던진다."""

    @staticmethod
    def _error() -> ConnectorError:
        return ConnectorError("문서 권한이 없다", reason=SCOPE_MISSING_REASON)

    async def search_files(
        self, query: FileQuery, *, max_results: int = 20, page_token: str | None = None
    ) -> FilePage:
        raise self._error()

    async def get_file(self, file_id: str) -> DriveFile:
        raise self._error()

    async def read_file_text(self, file_id: str, *, max_chars: int) -> FileText:
        raise self._error()


class SimConnectorResolver:
    """개발용. 합성 스냅샷을 한 번 읽고, 워크스페이스마다 한 번 포크해 프로세스 안에서 유지한다.

    워크스페이스끼리 월드를 공유하지 않는다(개발용이어도 격리 규칙을 그대로 둔다). 프로세스를 다시
    시작하면 월드는 스냅샷 상태로 돌아간다.
    """

    def __init__(self, snapshot_path: str | Path, account_id: str, clock: Clock) -> None:
        self._base = SimWorld.load(snapshot_path, clock)
        self._account_id = account_id
        self._clock = clock
        self._cache: dict[uuid.UUID, tuple[CalendarConnector, MailConnector]] = {}
        self._drive: dict[uuid.UUID, DriveConnector] = {}

    async def connectors_for(
        self, workspace_id: uuid.UUID
    ) -> tuple[CalendarConnector, MailConnector]:
        found = self._cache.get(workspace_id)
        if found is None:
            world = self._base.fork(self._base.seed, self._clock)
            conns = build_sim_connectors(world, [self._account_id])
            found = (conns.calendar[self._account_id], conns.mail[self._account_id])
            self._cache[workspace_id] = found
            self._drive[workspace_id] = conns.drive[self._account_id]
        return found

    async def drive_for(self, workspace_id: uuid.UUID) -> DriveConnector:
        await self.connectors_for(workspace_id)  # 같은 월드(포크)에서 문서 커넥터를 꺼낸다
        return self._drive[workspace_id]


class GoogleConnectorResolver:
    """워크스페이스의 활성 Google 연결로 Calendar·Gmail 커넥터를 만든다(ADR-0016 §4).

    호출마다 활성 연결을 DB에서 확인한다(해제·오류 상태가 바로 반영된다). 같은 연결이면 커넥터와
    메모리 액세스 토큰 캐시를 재사용하고, 연결이 바뀌면 이전 커넥터를 닫는다. 토큰 복호화는 토큰
    공급자가 갱신할 때만 한다. 운영 `base_url`을 쓴다(`https://*.googleapis.com` 검증 통과).
    """

    def __init__(
        self,
        *,
        db: AsyncDatabase,
        cipher: EnvelopeCipher,
        clock: Clock,
        client_id: str,
        client_secret: SecretStr,
        token_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._db = db
        self._cipher = cipher
        self._clock = clock
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._transport = transport
        self._cache: dict[uuid.UUID, tuple[uuid.UUID, GoogleCalendarConnector, GmailConnector]] = {}
        self._drive: dict[uuid.UUID, tuple[uuid.UUID, GoogleDriveConnector]] = {}
        self._drive_locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def connectors_for(
        self, workspace_id: uuid.UUID
    ) -> tuple[CalendarConnector, MailConnector]:
        async with self._db.workspace_session(workspace_id) as s:
            connection_id = await connection_store.find_active_id(s, workspace_id)
        cached = self._cache.get(workspace_id)
        if connection_id is None:
            if cached is not None:
                del self._cache[workspace_id]
                await _close(cached[1], cached[2])
            raise NoConnection(f"workspace_id={workspace_id}")
        if cached is not None:
            if cached[0] == connection_id:
                return cached[1], cached[2]
            await _close(cached[1], cached[2])
        tokens = ConnectionTokenProvider(
            db=self._db,
            cipher=self._cipher,
            clock=self._clock,
            workspace_id=workspace_id,
            connection_id=connection_id,
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_url=self._token_url,
            transport=self._transport,
        )
        calendar = GoogleCalendarConnector(tokens)
        mail = GmailConnector(tokens)
        self._cache[workspace_id] = (connection_id, calendar, mail)
        return calendar, mail

    async def drive_for(self, workspace_id: uuid.UUID) -> DriveConnector:
        """활성 연결의 문서 커넥터. 연결에 문서 권한이 없으면 `ScopeMissingDrive`다.

        권한은 호출마다 DB의 연결 행에서 확인한다. 문서 커넥터는 권한이 있을 때만 만들어 캐시하고,
        메일·캘린더와 토큰 공급자(액세스 토큰 캐시)를 따로 둔다. 권한을 추가하기 전에 받은 액세스
        토큰에는 문서 범위가 없으므로, 권한이 생긴 뒤에 새 공급자로 토큰을 새로 발급받게 한다.
        """
        lock = self._drive_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:  # 같은 워크스페이스의 생성·교체·닫기를 직렬화한다
            return await self._drive_locked(workspace_id)

    async def _drive_locked(self, workspace_id: uuid.UUID) -> DriveConnector:
        async with self._db.workspace_session(workspace_id) as s:
            row = await connection_store.find_open(s, workspace_id)
        cached = self._drive.get(workspace_id)
        if row is None or row.status != "active":
            if cached is not None:
                del self._drive[workspace_id]
                await cached[1].aclose()
            raise NoConnection(f"workspace_id={workspace_id}")
        if DRIVE_SCOPES[0] not in row.scopes:
            return ScopeMissingDrive()
        if cached is not None:
            if cached[0] == row.id:
                return cached[1]
            del self._drive[workspace_id]
            await cached[1].aclose()
        tokens = ConnectionTokenProvider(
            db=self._db,
            cipher=self._cipher,
            clock=self._clock,
            workspace_id=workspace_id,
            connection_id=row.id,
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_url=self._token_url,
            transport=self._transport,
        )
        drive = GoogleDriveConnector(tokens)
        self._drive[workspace_id] = (row.id, drive)
        return drive

    async def aclose(self) -> None:
        cached, self._cache = list(self._cache.values()), {}
        for _, calendar, mail in cached:
            await _close(calendar, mail)
        drives, self._drive = list(self._drive.values()), {}
        for _, drive in drives:
            await drive.aclose()


async def _close(calendar: GoogleCalendarConnector, mail: GmailConnector) -> None:
    await calendar.aclose()
    await mail.aclose()


@dataclass
class RunDeps:
    db: AsyncDatabase
    clock: Clock
    ids: Uuid7Generator
    llm: LLM
    resolver: ConnectorResolver
    tz: tzinfo
    checkpoint_conninfo: str  # psycopg 접속 문자열(`postgresql://...`). 체크포인터 전용 연결
    approval_ttl: timedelta = field(default_factory=lambda: timedelta(minutes=15))
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    max_steps: int = DEFAULT_MAX_STEPS
    jobs: procrastinate.App | None = None  # 워커 앱. 있으면 승인 만료 확인 작업을 예약한다
    mail_index: MailIndexDeps | None = None  # 색인 검색 도구·동기화 작업(ADR-0019). 없으면 비활성
