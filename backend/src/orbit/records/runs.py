"""run·run_event·run_message 저장소 함수 (ADR-0012 §1).

모든 함수는 `AsyncDatabase.workspace_session`이 연 세션을 받는다(워크스페이스 설정은 호출자 책임).
쿼리에도 `workspace_id` 조건을 넣는다(RLS는 두 번째 방어선). 시각은 주입한 `Clock`에서 얻는다.

seq 부여: run 행을 `FOR NO KEY UPDATE`로 잠근 뒤 `max(seq)+1`을 넣는다. 같은 run의 추가가
직렬화되어 seq가 빠짐없이 커밋 순서대로 이어진다(`after_seq` 재생이 중간 값을 건너뛰지 않는다).
잠금은 FK 검사의 `FOR KEY SHARE`와 충돌하지 않으므로 같은 run에 tool_call을 넣는 쪽은 막히지 않는다.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.records.errors import InvalidTransition, RecordNotFound

RUN_STATUSES = frozenset(
    {"queued", "running", "waiting_approval", "succeeded", "failed", "cancelled"}
)
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})

# 이동할 상태 -> 이동할 수 있는 이전 상태. 종료 상태에서는 나가지 않는다.
_ALLOWED_FROM: dict[str, frozenset[str]] = {
    "running": frozenset({"queued", "waiting_approval"}),
    "waiting_approval": frozenset({"running"}),
    "succeeded": frozenset({"running"}),
    "failed": frozenset({"queued", "running", "waiting_approval"}),
    "cancelled": frozenset({"queued", "running", "waiting_approval"}),
}


@dataclass(frozen=True)
class RunRecord:
    id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    pattern: str
    config_fingerprint: str
    error_kind: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RunEventRecord:
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class RunMessageRecord:
    seq: int
    role: str
    content: dict[str, Any]
    created_at: datetime


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _run(row: Any) -> RunRecord:
    return RunRecord(
        id=row.id,
        conversation_id=row.conversation_id,
        status=row.status,
        pattern=row.pattern,
        config_fingerprint=row.config_fingerprint,
        error_kind=row.error_kind,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def create_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    pattern: str,
    config_fingerprint: str,
    clock: Clock,
    ids: Uuid7Generator,
) -> RunRecord:
    """`queued` 상태의 run을 만든다."""
    now = clock.now()
    row = (
        await session.execute(
            text(
                "INSERT INTO run (id, workspace_id, conversation_id, status, pattern,"
                " config_fingerprint, created_at, updated_at)"
                " VALUES (:id, :ws, :conv, 'queued', :pattern, :fp, :now, :now)"
                " RETURNING id, conversation_id, status, pattern, config_fingerprint,"
                " error_kind, created_at, updated_at"
            ),
            {
                "id": ids.next(),
                "ws": workspace_id,
                "conv": conversation_id,
                "pattern": pattern,
                "fp": config_fingerprint,
                "now": now,
            },
        )
    ).one()
    return _run(row)


async def get_run(
    session: AsyncSession, *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> RunRecord | None:
    row = (
        await session.execute(
            text(
                "SELECT id, conversation_id, status, pattern, config_fingerprint, error_kind,"
                " created_at, updated_at FROM run WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": run_id, "ws": workspace_id},
        )
    ).one_or_none()
    return None if row is None else _run(row)


async def transition_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    to_status: str,
    clock: Clock,
    error_kind: str | None = None,
) -> RunRecord:
    """허용된 전이만 수행한다. 조건부 갱신이라 동시에 불러도 전이 규칙이 깨지지 않는다.

    `failed`로 갈 때만 `error_kind`가 필요하고, 다른 상태에서는 줄 수 없다.
    """
    allowed = _ALLOWED_FROM.get(to_status)
    if allowed is None:
        raise InvalidTransition(f"run으로 이동할 수 없는 상태: {to_status}")
    if (to_status == "failed") != (error_kind is not None):
        raise ValueError("error_kind는 failed 전이에만, 그리고 반드시 함께 준다")
    row = (
        await session.execute(
            text(
                "UPDATE run SET status = :to, error_kind = :ek, updated_at = :now"
                " WHERE id = :id AND workspace_id = :ws"
                " AND status = ANY(CAST(:src AS text[]))"
                " RETURNING id, conversation_id, status, pattern, config_fingerprint,"
                " error_kind, created_at, updated_at"
            ),
            {
                "to": to_status,
                "ek": error_kind,
                "now": clock.now(),
                "id": run_id,
                "ws": workspace_id,
                "src": sorted(allowed),
            },
        )
    ).one_or_none()
    if row is not None:
        return _run(row)
    current = await get_run(session, workspace_id=workspace_id, run_id=run_id)
    if current is None:
        raise RecordNotFound(f"run을 찾을 수 없다: {run_id}")
    raise InvalidTransition(f"run {run_id}: {current.status} -> {to_status} 전이는 허용되지 않는다")


async def _lock_run(session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    locked = (
        await session.execute(
            text("SELECT 1 FROM run WHERE id = :id AND workspace_id = :ws FOR NO KEY UPDATE"),
            {"id": run_id, "ws": workspace_id},
        )
    ).first()
    if locked is None:
        raise RecordNotFound(f"run을 찾을 수 없다: {run_id}")


async def append_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    type: str,  # noqa: A002 - 열 이름과 같게 둔다
    payload: Mapping[str, Any],
    clock: Clock,
) -> RunEventRecord:
    """run_event를 추가하고 부여된 seq를 돌려준다."""
    await _lock_run(session, workspace_id, run_id)
    row = (
        await session.execute(
            text(
                "INSERT INTO run_event (workspace_id, run_id, seq, type, payload, created_at)"
                " SELECT :ws, :run, COALESCE(max(seq), 0) + 1, :type, CAST(:payload AS jsonb), :now"
                " FROM run_event WHERE run_id = :run AND workspace_id = :ws"
                " RETURNING seq, type, payload, created_at"
            ),
            {
                "ws": workspace_id,
                "run": run_id,
                "type": type,
                "payload": _json(payload),
                "now": clock.now(),
            },
        )
    ).one()
    return RunEventRecord(row.seq, row.type, row.payload, row.created_at)


async def list_events(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    after_seq: int = 0,
    limit: int | None = None,
) -> list[RunEventRecord]:
    """`seq > after_seq`인 이벤트를 seq 순서로 돌려준다."""
    rows = await session.execute(
        text(
            "SELECT seq, type, payload, created_at FROM run_event"
            " WHERE run_id = :run AND workspace_id = :ws AND seq > :after"
            " ORDER BY seq LIMIT :limit"
        ),
        {"run": run_id, "ws": workspace_id, "after": after_seq, "limit": limit},
    )
    return [RunEventRecord(r.seq, r.type, r.payload, r.created_at) for r in rows]


async def append_message(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    role: str,
    content: Mapping[str, Any],
    clock: Clock,
) -> RunMessageRecord:
    """run_message를 추가한다. Agent 대화 원문의 유일한 저장소다(그래프 상태에 넣지 않는다)."""
    if role not in MESSAGE_ROLES:
        raise ValueError(f"알 수 없는 role: {role}")
    await _lock_run(session, workspace_id, run_id)
    row = (
        await session.execute(
            text(
                "INSERT INTO run_message (workspace_id, run_id, seq, role, content, created_at)"
                " SELECT :ws, :run, COALESCE(max(seq), 0) + 1, :role, CAST(:content AS jsonb), :now"
                " FROM run_message WHERE run_id = :run AND workspace_id = :ws"
                " RETURNING seq, role, content, created_at"
            ),
            {
                "ws": workspace_id,
                "run": run_id,
                "role": role,
                "content": _json(content),
                "now": clock.now(),
            },
        )
    ).one()
    return RunMessageRecord(row.seq, row.role, row.content, row.created_at)


async def list_messages(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    after_seq: int = 0,
) -> list[RunMessageRecord]:
    rows = await session.execute(
        text(
            "SELECT seq, role, content, created_at FROM run_message"
            " WHERE run_id = :run AND workspace_id = :ws AND seq > :after ORDER BY seq"
        ),
        {"run": run_id, "ws": workspace_id, "after": after_seq},
    )
    return [RunMessageRecord(r.seq, r.role, r.content, r.created_at) for r in rows]
