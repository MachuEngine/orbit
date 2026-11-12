"""통합 테스트 픽스처."""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine

from integration.mailindex_support import EXTRAS, IndexEnv, cleanup_users, env_iter
from integration.runflow_support import Harness, build_harness
from integration.support import WorkspaceFactory, alembic_config, require_test_database
from orbit.db.session import AsyncDatabase, SyncDatabase


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database(app_url: str, owner_url: str) -> None:
    """통합 테스트 시작 전에 테스트 DB를 head로 맞춘다(개발 DB는 건드리지 않는다)."""
    require_test_database(app_url)
    require_test_database(owner_url)
    command.upgrade(alembic_config(owner_url), "head")


@pytest.fixture
def sync_db(app_url: str) -> Iterator[SyncDatabase]:
    db = SyncDatabase.from_url(app_url)
    yield db
    db.dispose()


@pytest.fixture
async def async_db(app_url: str) -> AsyncIterator[AsyncDatabase]:
    db = AsyncDatabase.from_url(app_url)
    yield db
    await db.dispose()


@pytest.fixture
def raw_engine(app_url: str) -> Iterator[Engine]:
    """세션 헬퍼를 거치지 않는 엔진. 워크스페이스 설정이 없는 상태를 재현하는 데만 쓴다."""
    engine = create_engine(app_url, pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture
def workspaces(sync_db: SyncDatabase) -> Iterator[WorkspaceFactory]:
    factory = WorkspaceFactory(sync_db)
    yield factory
    factory.cleanup()


@pytest.fixture
async def make_harness(
    app_url: str, async_db: AsyncDatabase, workspaces: WorkspaceFactory
) -> AsyncIterator[Callable[..., Awaitable[Harness]]]:
    """쓰기 Run 통합 테스트용 환경을 만든다. `script`는 ScriptedLLM의 응답 목록이다."""
    stack = AsyncExitStack()

    async def make(script: list[dict[str, Any]], **kwargs: Any) -> Harness:
        manager = asynccontextmanager(build_harness)(
            app_url, async_db, workspaces, script, **kwargs
        )
        return await stack.enter_async_context(manager)

    yield make
    await stack.aclose()


@pytest.fixture
def owner_engine(owner_url: str) -> Iterator[Engine]:
    """소유자 역할 엔진. orbit_app이 만들 수 없는 합성 사용자·연결을 준비하는 데 쓴다."""
    engine = create_engine(owner_url, pool_size=1, max_overflow=0)
    yield engine
    cleanup_users(engine)
    engine.dispose()


@pytest.fixture
async def ix(
    async_db: AsyncDatabase, workspaces: WorkspaceFactory, owner_engine: Engine
) -> AsyncIterator[IndexEnv]:
    """활성 연결과 Sim 메일(합성 견적·점심 메일 포함)이 있는 색인 테스트 환경."""
    async for env in env_iter(async_db, workspaces, owner_engine, extra=EXTRAS):
        yield env
