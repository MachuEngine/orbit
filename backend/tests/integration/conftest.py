"""통합 테스트 픽스처."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine

from integration.support import WorkspaceFactory
from orbit.db.session import SyncDatabase


@pytest.fixture
def sync_db(app_url: str) -> Iterator[SyncDatabase]:
    db = SyncDatabase.from_url(app_url)
    yield db
    db.dispose()


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
