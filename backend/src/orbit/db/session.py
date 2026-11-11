"""워크스페이스 범위 DB 세션 (ADR-0004).

사용자 데이터에 접근하는 유일한 공개 경로다. 트랜잭션을 시작한 직후
`set_config('app.workspace_id', :id, true)`(트랜잭션 범위)를 실행하고, 정상 종료하면 커밋,
예외가 나면 롤백한다. 엔진은 `pool_reset_on_return` 기본값(rollback)을 그대로 쓴다.

규칙
- 엔진·연결은 `SyncDatabase`/`AsyncDatabase` 안에 숨긴다. 앱 코드는 아래 메서드만 쓴다.
- 세션 범위 `SET`, `set_config(..., false)`를 쓰지 않는다.
- 블록 안에서 `session.commit()`·`session.rollback()`을 직접 부르지 않는다. 부르면 그 시점에
  트랜잭션이 끝나고(commit은 그때까지의 변경을 커밋한다), 이어지는 `session.execute(...)`는
  `InvalidRequestError`(closed transaction inside context manager)로 실패한다. 설정 없는 새
  트랜잭션이 열리지 않으므로 데이터가 보이지 않는다(SQLAlchemy 2.1.8에서 확인, 통합 테스트로 고정).
  나눠서 커밋해야 하면 `workspace_session`을 새로 연다.
- 워크스페이스 없이 DB에 닿는 경로는 `ping()`과 `unscoped_auth_session_lookup()`뿐이다.
  새 경로가 필요하면 이름에 `unscoped`를 넣는다.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session

_SET_WORKSPACE = text("SELECT set_config('app.workspace_id', :workspace_id, true)")


class SyncDatabase:
    """동기 SQLAlchemy 접근. 엔진은 외부에 노출하지 않는다."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, url: str, *, pool_size: int = 5, max_overflow: int = 10) -> "SyncDatabase":
        # pool_reset_on_return은 기본값(rollback)을 유지한다.
        return cls(
            create_engine(url, pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True)
        )

    @contextmanager
    def workspace_session(self, workspace_id: uuid.UUID) -> Iterator[Session]:
        """워크스페이스 범위 트랜잭션. 정상 종료는 커밋, 예외는 롤백한다."""
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            session.execute(_SET_WORKSPACE, {"workspace_id": str(workspace_id)})
            yield session

    def ping(self) -> None:
        """워크스페이스 없이 DB 연결만 확인한다(헬스체크용). 사용자 데이터를 읽지 않는다."""
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def dispose(self) -> None:
        self._engine.dispose()


class AsyncDatabase:
    """비동기 SQLAlchemy 접근. 엔진은 외부에 노출하지 않는다."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, url: str, *, pool_size: int = 5, max_overflow: int = 10) -> "AsyncDatabase":
        return cls(
            create_async_engine(
                url, pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True
            )
        )

    @asynccontextmanager
    async def workspace_session(self, workspace_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """워크스페이스 범위 트랜잭션. 정상 종료는 커밋, 예외는 롤백한다."""
        async with (
            AsyncSession(self._engine, expire_on_commit=False) as session,
            session.begin(),
        ):
            await session.execute(_SET_WORKSPACE, {"workspace_id": str(workspace_id)})
            yield session

    async def ping(self) -> None:
        """워크스페이스 없이 DB 연결만 확인한다(헬스체크용). 사용자 데이터를 읽지 않는다."""
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def unscoped_auth_session_lookup(self, token_hash: str) -> tuple[Any, ...] | None:
        """토큰 해시로 세션을 찾는다. 워크스페이스를 모르는 상태에서 쓰는 유일한 조회 경로다.

        `auth_session_lookup` 함수(SECURITY DEFINER, 마이그레이션 0004)만 호출한다. 테이블을 직접
        읽지 않는다. 반환 열: session_id, workspace_id, csrf_token_hash, expires_at, revoked_at.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT * FROM auth_session_lookup(:h)"), {"h": token_hash})
            ).first()
            await conn.rollback()
        return None if row is None else tuple(row)

    async def dispose(self) -> None:
        await self._engine.dispose()
