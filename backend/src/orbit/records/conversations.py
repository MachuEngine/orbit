"""대화 목록·이어서 보기 조회 (ADR-0017 §4). 워크스페이스 세션을 받고 `workspace_id`로 거른다."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TITLE_CHARS = 40
MAX_TURNS = 100


@dataclass(frozen=True)
class ConversationSummary:
    id: uuid.UUID
    title: str | None
    created_at: datetime
    last_activity_at: datetime
    last_run_status: str | None


@dataclass(frozen=True)
class ConversationTurn:
    run_id: uuid.UUID
    status: str
    user_text: str
    assistant_text: str | None
    pending_approval_id: uuid.UUID | None
    created_at: datetime


def make_title(first_text: str | None) -> str | None:
    """첫 사용자 메시지의 첫 줄을 40자까지. 넘으면 `…`를 붙인다. 비어 있으면 None."""
    if first_text is None:
        return None
    lines = first_text.strip().splitlines()
    line = lines[0].strip() if lines else ""
    if not line:
        return None
    return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS] + "…"


async def list_conversations(
    session: AsyncSession, workspace_id: uuid.UUID, *, limit: int
) -> list[ConversationSummary]:
    """최근 활동 순(마지막 run의 updated_at, run이 없으면 대화 생성 시각)."""
    rows = await session.execute(
        text(
            "SELECT c.id, c.created_at, lr.status AS last_status,"
            " COALESCE(lr.updated_at, c.created_at) AS last_activity,"
            " (SELECT m.content->>'text' FROM run r JOIN run_message m"
            "   ON m.run_id = r.id AND m.workspace_id = r.workspace_id AND m.role = 'user'"
            "  WHERE r.conversation_id = c.id AND r.workspace_id = c.workspace_id"
            "  ORDER BY r.created_at, r.id, m.seq LIMIT 1) AS first_text"
            " FROM conversation c LEFT JOIN LATERAL ("
            "   SELECT status, updated_at FROM run"
            "   WHERE conversation_id = c.id AND workspace_id = c.workspace_id"
            "   ORDER BY created_at DESC, id DESC LIMIT 1) lr ON true"
            " WHERE c.workspace_id = :ws"
            " ORDER BY last_activity DESC, c.id DESC LIMIT :limit"
        ),
        {"ws": workspace_id, "limit": limit},
    )
    return [
        ConversationSummary(
            id=r.id,
            title=make_title(r.first_text),
            created_at=r.created_at,
            last_activity_at=r.last_activity,
            last_run_status=r.last_status,
        )
        for r in rows
    ]


async def conversation_exists(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM conversation WHERE id = :id AND workspace_id = :ws"),
            {"id": conversation_id, "ws": workspace_id},
        )
    ).first()
    return row is not None


async def list_turns(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[ConversationTurn]:
    """최근 100턴을 오래된 순으로 돌려준다. 비서 텍스트는 step 순으로 `\\n\\n`으로 잇는다."""
    runs = (
        await session.execute(
            text(
                "SELECT id, status, created_at FROM run"
                " WHERE conversation_id = :conv AND workspace_id = :ws"
                " ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            {"conv": conversation_id, "ws": workspace_id, "limit": MAX_TURNS},
        )
    ).all()
    run_ids = [r.id for r in runs]
    if not run_ids:
        return []
    users: dict[uuid.UUID, list[str]] = {}
    replies: dict[uuid.UUID, list[str]] = {}
    messages = await session.execute(
        text(
            "SELECT run_id, role, content->>'text' AS body FROM run_message"
            " WHERE workspace_id = :ws AND run_id = ANY(:ids) AND role IN ('user', 'assistant')"
            " ORDER BY run_id, seq"
        ),
        {"ws": workspace_id, "ids": run_ids},
    )
    for m in messages:
        if m.body is None or (m.role == "assistant" and not m.body.strip()):
            continue
        (users if m.role == "user" else replies).setdefault(m.run_id, []).append(m.body)
    pending = {
        r.run_id: r.id
        for r in await session.execute(
            text(
                "SELECT DISTINCT ON (run_id) run_id, id, status FROM approval"
                " WHERE workspace_id = :ws AND run_id = ANY(:ids)"
                " ORDER BY run_id, created_at DESC, id DESC"
            ),
            {"ws": workspace_id, "ids": run_ids},
        )
        if r.status == "pending"
    }
    return [
        ConversationTurn(
            run_id=r.id,
            status=r.status,
            user_text="\n".join(users.get(r.id, [])),
            assistant_text="\n\n".join(replies[r.id]) if r.id in replies else None,
            pending_approval_id=pending.get(r.id),
            created_at=r.created_at,
        )
        for r in reversed(runs)
    ]
