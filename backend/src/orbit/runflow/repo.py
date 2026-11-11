"""`orbit.records`에 없는 읽기·잠금 쿼리. 워크스페이스 세션을 받고 `workspace_id`로 거른다.

`orbit.records`의 계약은 바꾸지 않는다. 같은 테이블을 읽기만 하거나 run 행을 잠그는 용도다.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.records.approvals import ApprovalRecord, ToolCallRecord

DEADLOCK_SQLSTATE = "40P01"


async def lock_run(session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
    """run 행을 `FOR NO KEY UPDATE`로 잠그고 상태를 돌려준다. 없으면 None.

    잠금 순서는 항상 run -> approval이다(ADR-0012 §4). approval을 건드린 뒤 run을 잠그지 않는다.
    """
    row = (
        await session.execute(
            text("SELECT status FROM run WHERE id = :id AND workspace_id = :ws FOR NO KEY UPDATE"),
            {"id": run_id, "ws": workspace_id},
        )
    ).one_or_none()
    return None if row is None else str(row.status)


async def get_tool_call(
    session: AsyncSession, workspace_id: uuid.UUID, tool_call_id: uuid.UUID
) -> ToolCallRecord | None:
    row = (
        await session.execute(
            text(
                "SELECT id, run_id, tool_name, args, args_hash, provenance, created_at"
                " FROM tool_call WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": tool_call_id, "ws": workspace_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return ToolCallRecord(
        id=row.id,
        run_id=row.run_id,
        tool_name=row.tool_name,
        args=row.args,
        args_hash=row.args_hash,
        provenance=row.provenance,
        created_at=row.created_at,
    )


async def get_approval_by_tool_call(
    session: AsyncSession, workspace_id: uuid.UUID, tool_call_id: uuid.UUID
) -> ApprovalRecord | None:
    row = (
        await session.execute(
            text(
                "SELECT id, run_id, tool_call_id, args_hash, status, expires_at, decided_at,"
                " created_at FROM approval WHERE tool_call_id = :call AND workspace_id = :ws"
            ),
            {"call": tool_call_id, "ws": workspace_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return ApprovalRecord(
        id=row.id,
        run_id=row.run_id,
        tool_call_id=row.tool_call_id,
        args_hash=row.args_hash,
        status=row.status,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


async def latest_approval_for_run(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> ApprovalRecord | None:
    """run의 가장 최근 승인(v0는 run당 한 번에 하나만 대기한다)."""
    row = (
        await session.execute(
            text(
                "SELECT id, run_id, tool_call_id, args_hash, status, expires_at, decided_at,"
                " created_at FROM approval WHERE run_id = :run AND workspace_id = :ws"
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"run": run_id, "ws": workspace_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return ApprovalRecord(
        id=row.id,
        run_id=row.run_id,
        tool_call_id=row.tool_call_id,
        args_hash=row.args_hash,
        status=row.status,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


async def workspace_exists(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    row = (
        await session.execute(text("SELECT 1 FROM workspace WHERE id = :id"), {"id": workspace_id})
    ).first()
    return row is not None


def is_deadlock(exc: BaseException) -> bool:
    """교착 오류(40P01)인가."""
    if not isinstance(exc, DBAPIError):
        return False
    return getattr(exc.orig, "sqlstate", None) == DEADLOCK_SQLSTATE
