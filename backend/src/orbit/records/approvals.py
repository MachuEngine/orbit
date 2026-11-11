"""tool_call·approval 저장소 함수 (ADR-0012 §1, ADR-0003 조건 2).

- `create_tool_call_with_approval`은 멱등하다. 같은 `tool_call_id`로 다시 불러도 같은 행을 돌려준다.
  그래프 노드가 interrupt 앞에서 재실행되어도 승인 요청이 둘 생기지 않는다.
- 승인 결정은 `status = 'pending' AND expires_at > :now` 조건부 갱신 하나뿐이다. 성공(True)한 쪽만
  run을 재개한다. 시각은 DB `now()`가 아니라 주입한 `Clock` 값이다.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.records.errors import IdempotencyConflict, RecordNotFound

DEFAULT_APPROVAL_TTL = timedelta(minutes=15)  # D5-5 후보값


@dataclass(frozen=True)
class ToolCallRecord:
    id: uuid.UUID
    run_id: uuid.UUID
    tool_name: str
    args: dict[str, Any]
    args_hash: str
    provenance: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class ApprovalRecord:
    id: uuid.UUID
    run_id: uuid.UUID
    tool_call_id: uuid.UUID
    args_hash: str
    status: str
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


def _approval(row: Any) -> ApprovalRecord:
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


async def create_tool_call_with_approval(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    tool_call_id: uuid.UUID,
    tool_name: str,
    args: Mapping[str, Any],
    args_hash: str,
    provenance: Mapping[str, Any],
    clock: Clock,
    ids: Uuid7Generator,
    ttl: timedelta = DEFAULT_APPROVAL_TTL,
) -> tuple[ToolCallRecord, ApprovalRecord, bool]:
    """tool_call과 `pending` approval을 멱등하게 만든다. 반환값의 bool은 새로 만들었는지 여부다.

    `tool_call_id`가 멱등 키다(호출자가 run과 단계에서 결정적으로 만든다). 같은 키인데 run, 도구,
    인자 해시가 다르면 `IdempotencyConflict`를 낸다(승인된 인자와 다른 인자가 끼어드는 것을 막는다).
    """
    now = clock.now()
    params = {"id": tool_call_id, "ws": workspace_id}
    await session.execute(
        text(
            "INSERT INTO tool_call (id, workspace_id, run_id, tool_name, args, args_hash,"
            " provenance, created_at)"
            " VALUES (:id, :ws, :run, :tool, CAST(:args AS jsonb), :hash,"
            " CAST(:prov AS jsonb), :now)"
            " ON CONFLICT (id) DO NOTHING RETURNING id"
        ),
        {
            **params,
            "run": run_id,
            "tool": tool_name,
            "args": json.dumps(args, ensure_ascii=False, sort_keys=True),
            "hash": args_hash,
            "prov": json.dumps(provenance, ensure_ascii=False, sort_keys=True),
            "now": now,
        },
    )
    call_row = (
        await session.execute(
            text(
                "SELECT id, run_id, tool_name, args, args_hash, provenance, created_at"
                " FROM tool_call WHERE id = :id AND workspace_id = :ws"
            ),
            params,
        )
    ).one_or_none()
    if call_row is None:
        # 다른 워크스페이스의 행과 키가 겹친 경우도 여기로 온다. 존재 여부를 알리지 않는다.
        raise RecordNotFound(f"tool_call을 만들거나 찾을 수 없다: {tool_call_id}")
    if (call_row.run_id, call_row.tool_name, call_row.args_hash) != (run_id, tool_name, args_hash):
        raise IdempotencyConflict(f"tool_call {tool_call_id}: 같은 키에 다른 내용이 들어왔다")
    tool_call = ToolCallRecord(
        id=call_row.id,
        run_id=call_row.run_id,
        tool_name=call_row.tool_name,
        args=call_row.args,
        args_hash=call_row.args_hash,
        provenance=call_row.provenance,
        created_at=call_row.created_at,
    )

    approval_inserted = (
        await session.execute(
            text(
                "INSERT INTO approval (id, workspace_id, run_id, tool_call_id, args_hash, status,"
                " expires_at, created_at)"
                " VALUES (:id, :ws, :run, :call, :hash, 'pending', :expires, :now)"
                " ON CONFLICT (tool_call_id) DO NOTHING RETURNING id"
            ),
            {
                "id": ids.next(),
                "ws": workspace_id,
                "run": run_id,
                "call": tool_call_id,
                "hash": args_hash,
                "expires": now + ttl,
                "now": now,
            },
        )
    ).first()
    approval_row = (
        await session.execute(
            text(
                "SELECT id, run_id, tool_call_id, args_hash, status, expires_at, decided_at,"
                " created_at FROM approval WHERE tool_call_id = :call AND workspace_id = :ws"
            ),
            {"call": tool_call_id, "ws": workspace_id},
        )
    ).one()
    return tool_call, _approval(approval_row), approval_inserted is not None


async def get_approval(
    session: AsyncSession, *, workspace_id: uuid.UUID, approval_id: uuid.UUID
) -> ApprovalRecord | None:
    row = (
        await session.execute(
            text(
                "SELECT id, run_id, tool_call_id, args_hash, status, expires_at, decided_at,"
                " created_at FROM approval WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": approval_id, "ws": workspace_id},
        )
    ).one_or_none()
    return None if row is None else _approval(row)


async def decide_approval(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    approval_id: uuid.UUID,
    approve: bool,
    clock: Clock,
) -> bool:
    """`pending`이고 만료 전인 승인만 결정한다. 성공하면 True, 이미 결정됐거나 만료면 False."""
    updated = (
        await session.execute(
            text(
                "UPDATE approval SET status = :status, decided_at = :now"
                " WHERE id = :id AND workspace_id = :ws"
                " AND status = 'pending' AND expires_at > :now RETURNING id"
            ),
            {
                "status": "approved" if approve else "rejected",
                "now": clock.now(),
                "id": approval_id,
                "ws": workspace_id,
            },
        )
    ).first()
    return updated is not None


async def expire_approval(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    approval_id: uuid.UUID,
    clock: Clock,
) -> bool:
    """만료 시각이 지난 `pending` 승인을 `expired`로 바꾼다. 바꿨으면 True."""
    updated = (
        await session.execute(
            text(
                "UPDATE approval SET status = 'expired', decided_at = :now"
                " WHERE id = :id AND workspace_id = :ws"
                " AND status = 'pending' AND expires_at <= :now RETURNING id"
            ),
            {"now": clock.now(), "id": approval_id, "ws": workspace_id},
        )
    ).first()
    return updated is not None


async def expire_due_approvals(
    session: AsyncSession, *, workspace_id: uuid.UUID, clock: Clock
) -> list[uuid.UUID]:
    """워크스페이스의 만료된 `pending` 승인을 모두 `expired`로 바꾸고 ID를 돌려준다."""
    rows = await session.execute(
        text(
            "UPDATE approval SET status = 'expired', decided_at = :now"
            " WHERE workspace_id = :ws AND status = 'pending' AND expires_at <= :now"
            " RETURNING id"
        ),
        {"now": clock.now(), "ws": workspace_id},
    )
    return [r.id for r in rows]
