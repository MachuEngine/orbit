"""통합 테스트 공통 픽스처. 데이터는 모두 합성 값이다."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from orbit.core.clock import FixedClock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import SyncDatabase

CLOCK = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
IDS = Uuid7Generator(CLOCK)

INSERT_WORKSPACE = text("INSERT INTO workspace (id, created_at) VALUES (:id, :at)")
INSERT_CONVERSATION = text(
    "INSERT INTO conversation (id, workspace_id, created_at) VALUES (:id, :ws, :at)"
)
INSERT_MESSAGE = text(
    "INSERT INTO message (id, workspace_id, conversation_id, role, content, created_at)"
    " VALUES (:id, :ws, :conv, 'user', :content, :at)"
)


BACKEND = Path(__file__).resolve().parents[2]


def alembic_config(owner_url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["url"] = owner_url
    return cfg


def require_test_database(url: str) -> None:
    """스키마를 내리는 테스트가 개발 DB에 실행되지 않도록 DB 이름을 확인한다."""
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise RuntimeError(f"테스트 DB 이름은 '_test'로 끝나야 한다: {name!r}")


class WorkspaceFactory:
    """합성 워크스페이스를 만들고, 테스트가 끝나면 지운다(대화·메시지는 CASCADE)."""

    def __init__(self, db: SyncDatabase) -> None:
        self._db = db
        self.created: list[uuid.UUID] = []

    def create(self, conversations: int = 0) -> uuid.UUID:
        ws = IDS.next()
        self.created.append(ws)
        with self._db.workspace_session(ws) as s:
            s.execute(INSERT_WORKSPACE, {"id": ws, "at": CLOCK.now()})
            for _ in range(conversations):
                s.execute(INSERT_CONVERSATION, {"id": IDS.next(), "ws": ws, "at": CLOCK.now()})
        return ws

    def cleanup(self) -> None:
        for ws in self.created:
            with self._db.workspace_session(ws) as s:
                s.execute(text("DELETE FROM workspace WHERE id = :id"), {"id": ws})
