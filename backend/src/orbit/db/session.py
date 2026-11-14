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
- 워크스페이스 없이 DB에 닿는 경로는 `ping()`, `unscoped_auth_session_lookup()`,
  `unscoped_auth_session_profile()`, `unscoped_login_upsert_user()`,
  `unscoped_mail_index_due_connections()`, `unscoped_event_reminder_due_connections()`,
  `unscoped_notification_expired_workspaces()`뿐이다. 시작 점검 두 가지
  (`unscoped_startup_role_report()`, `unscoped_alembic_version()`)는 사용자 데이터가 아니라
  DB 카탈로그와 `alembic_version`만 읽는다(ADR-0022 §3).
  `ping()`과 시작 점검 말고는 모두 `SECURITY DEFINER` 함수 호출만 한다
  (마이그레이션 0004·0005·0008·0010).
  새 경로가 필요하면 이름에 `unscoped`를 넣는다.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session

_ROLE_REPORT = text(
    """
    SELECT r.rolsuper,
           r.rolbypassrls,
           EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid),
           EXISTS (SELECT 1 FROM pg_class c
                    WHERE c.relowner = r.oid AND c.relkind IN ('r', 'p') AND c.relrowsecurity)
      FROM pg_roles r
     WHERE r.rolname = current_user
    """
)


class RoleFlags(NamedTuple):
    """접속 역할의 격리 속성(`orbit.api.startup.RoleReport`의 원본 값)."""

    is_superuser: bool
    bypasses_rls: bool
    is_member_of_other_role: bool
    owns_rls_table: bool


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

    async def unscoped_startup_role_report(self) -> RoleFlags:
        """접속 역할(`current_user`)의 격리 속성을 카탈로그에서 읽는다(시작 점검용, ADR-0022 §3)."""
        async with self._engine.connect() as conn:
            row = (await conn.execute(_ROLE_REPORT)).one()
            await conn.rollback()
        return RoleFlags(bool(row[0]), bool(row[1]), bool(row[2]), bool(row[3]))

    async def unscoped_alembic_version(self) -> str | None:
        """DB의 Alembic 리비전. 테이블이 없거나 읽을 권한이 없거나 행이 하나가 아니면 None이다."""
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(text("SELECT version_num FROM alembic_version"))).all()
                await conn.rollback()
        except DBAPIError:
            return None
        return str(rows[0][0]) if len(rows) == 1 else None

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

    async def unscoped_auth_session_profile(
        self, token_hash: str, now: datetime
    ) -> tuple[Any, ...] | None:
        """토큰 해시의 세션에 묶인 사용자의 (user_id, email, display_name). 본인 정보만 나온다.

        `auth_session_profile` 함수(SECURITY DEFINER, 마이그레이션 0005)만 호출한다.
        """
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM auth_session_profile(:h, :now)"),
                    {"h": token_hash, "now": now},
                )
            ).first()
            await conn.rollback()
        return None if row is None else tuple(row)

    async def unscoped_login_upsert_user(
        self,
        *,
        google_sub: str,
        email: str,
        display_name: str,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        now: datetime,
    ) -> tuple[Any, ...]:
        """로그인한 Google 사용자를 만들거나 갱신한다. 반환: (user_id, workspace_id, created).

        `login_upsert_user` 함수(SECURITY DEFINER, 마이그레이션 0005)만 호출한다. 처음이면 개인
        워크스페이스와 owner 멤버십을 같은 트랜잭션에서 만든다. 인증을 마친 로그인 경로에서만 쓴다.
        """
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM login_upsert_user(:sub, :email, :name, :uid, :ws, :now)"),
                    {
                        "sub": google_sub,
                        "email": email,
                        "name": display_name,
                        "uid": user_id,
                        "ws": workspace_id,
                        "now": now,
                    },
                )
            ).one()
        return tuple(row)

    async def unscoped_mail_index_due_connections(
        self, stale_before: datetime, error_retry_before: datetime
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """색인 범위가 있고 `stale_before`보다 오래 동기화하지 않은 활성 연결의 ID 쌍.

        오류 상태 연결은 영구 오류(`permission_revoked`)면 빼고, 그 밖의 오류는 마지막 시도가
        `error_retry_before`보다 오래됐을 때만 포함한다.

        주기 작업이 워크스페이스를 가로질러 대상을 고르는 유일한 경로다.
        `mail_index_due_connections` 함수(SECURITY DEFINER, 마이그레이션 0008)만 호출하며
        `(workspace_id, connection_id)`만 돌려준다. 제목·본문·주소는 읽지 않는다.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT workspace_id, connection_id FROM mail_index_due_connections(:b, :e)"
                    ),
                    {"b": stale_before, "e": error_retry_before},
                )
            ).all()
            await conn.rollback()
        return [(r[0], r[1]) for r in rows]

    async def unscoped_event_reminder_due_connections(
        self,
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """일정 알림을 켠(설정 행이 없으면 켠 것으로 본다) 워크스페이스의 활성 연결 ID 쌍.

        `event_reminder_due_connections` 함수(SECURITY DEFINER, 마이그레이션 0010)만 호출하며
        `(workspace_id, connection_id)`만 돌려준다. 일정 제목·주소는 읽지 않는다.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT workspace_id, connection_id FROM event_reminder_due_connections()")
                )
            ).all()
            await conn.rollback()
        return [(r[0], r[1]) for r in rows]

    async def unscoped_notification_expired_workspaces(
        self, event_before: datetime, reminder_before: datetime
    ) -> list[uuid.UUID]:
        """보존 기간이 지난 알림이 있는 워크스페이스 ID(마이그레이션 0010의 SECURITY DEFINER 함수).

        일정 시작이 `event_before`보다 앞선 일정 알림이나 `due_at`이 `reminder_before`보다 앞선 대화
        리마인더가 있는 워크스페이스만 돌려준다. 알림 내용은 읽지 않는다.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT workspace_id FROM notification_expired_workspaces(:a, :b)"),
                    {"a": event_before, "b": reminder_before},
                )
            ).all()
            await conn.rollback()
        return [r[0] for r in rows]

    async def dispose(self) -> None:
        await self._engine.dispose()
