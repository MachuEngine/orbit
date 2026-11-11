"""통합 테스트 픽스처."""

from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine

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
