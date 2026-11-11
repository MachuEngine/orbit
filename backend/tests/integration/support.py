"""통합 테스트 공통 픽스처. 데이터는 모두 합성 값이다."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

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
