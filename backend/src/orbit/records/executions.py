"""SQL `ExecutionStore` (ADR-0012 §3, ADR-0003 조건 1).

Protocol 자체는 4B가 `orbit.actions`에 정의한다. 여기서는 같은 시그니처의 구현만 둔다.
각 메서드는 호출마다 새 워크스페이스 트랜잭션을 열고 커밋한다. 외부 호출 전에 `begin`이 커밋되어
있어야 강제 종료 뒤에도 "호출했을 수 있음"이 남는다.

- `begin`: `approval_id` 유일 제약으로 `pending` 행을 선기록한다. 이미 있으면
  기존 행과 False를 준다. `approved`가 아닌 approval에는 행을 만들지 않는다(DB 쪽 두 번째 방어선).
- `record_ref`, `finish`: 아직 끝나지 않은(`pending`·`unknown`) 행만 바꾼다.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.errors import ApprovalNotApproved, InvalidTransition, RecordNotFound

EXECUTION_STATUSES = frozenset({"pending", "succeeded", "failed", "unknown", "needs_user"})
_OPEN_STATUSES = ["pending", "unknown"]


@dataclass(frozen=True)
class ExecutionRecord:
    id: uuid.UUID
    approval_id: uuid.UUID
    status: str
    external_ref: dict[str, Any] | None
    error_kind: str | None


def _record(row: Any) -> ExecutionRecord:
    return ExecutionRecord(
        id=row.id,
        approval_id=row.approval_id,
        status=row.status,
        external_ref=row.external_ref,
        error_kind=row.error_kind,
    )


class SqlExecutionStore:
    """한 워크스페이스의 실행 기록 저장소. 호출마다 커밋한다."""

    def __init__(
        self, db: AsyncDatabase, workspace_id: uuid.UUID, *, clock: Clock, ids: Uuid7Generator
    ) -> None:
        self._db = db
        self._ws = workspace_id
        self._clock = clock
        self._ids = ids

    async def begin(self, approval_id: uuid.UUID) -> tuple[ExecutionRecord, bool]:
        async with self._db.workspace_session(self._ws) as s:
            now = self._clock.now()
            inserted = (
                await s.execute(
                    text(
                        "INSERT INTO execution (id, workspace_id, approval_id, status, created_at,"
                        " updated_at)"
                        " SELECT :id, workspace_id, id, 'pending', :now, :now FROM approval"
                        " WHERE id = :approval AND workspace_id = :ws AND status = 'approved'"
                        " ON CONFLICT (approval_id) DO NOTHING"
                        " RETURNING id, approval_id, status, external_ref, error_kind"
                    ),
                    {"id": self._ids.next(), "now": now, "approval": approval_id, "ws": self._ws},
                )
            ).one_or_none()
            if inserted is not None:
                return _record(inserted), True
            existing = await self._select(s, approval_id)
            if existing is None:
                # 승인이 없거나 다른 워크스페이스 것이거나 approved가 아니다. 구분해 알리지 않는다.
                raise ApprovalNotApproved(f"approved 상태의 approval이 아니다: {approval_id}")
            return existing, False

    async def record_ref(self, execution_id: uuid.UUID, external_ref: dict[str, Any]) -> None:
        async with self._db.workspace_session(self._ws) as s:
            updated = (
                await s.execute(
                    text(
                        "UPDATE execution SET external_ref = CAST(:ref AS jsonb), updated_at = :now"
                        " WHERE id = :id AND workspace_id = :ws"
                        " AND status = ANY(CAST(:open AS text[])) RETURNING id"
                    ),
                    {
                        "ref": json.dumps(external_ref, ensure_ascii=False, sort_keys=True),
                        "now": self._clock.now(),
                        "id": execution_id,
                        "ws": self._ws,
                        "open": _OPEN_STATUSES,
                    },
                )
            ).first()
            if updated is None:
                await self._explain_failure(s, execution_id, "record_ref")

    async def finish(
        self,
        execution_id: uuid.UUID,
        status: str,
        external_ref: dict[str, Any] | None,
        error_kind: str | None,
    ) -> None:
        """`pending`·`unknown` 행의 최종(또는 대조 대기) 상태를 기록한다.

        `external_ref`가 None이면 기존 값을 유지한다. 이미 같은 상태로 끝난 행에 같은 상태로 다시
        부르면(재진입) 아무것도 하지 않는다.
        """
        if status not in EXECUTION_STATUSES - {"pending"}:
            raise ValueError(f"finish에 쓸 수 없는 상태: {status}")
        async with self._db.workspace_session(self._ws) as s:
            updated = (
                await s.execute(
                    text(
                        "UPDATE execution SET status = :status,"
                        " external_ref = COALESCE(CAST(:ref AS jsonb), external_ref),"
                        " error_kind = :ek, updated_at = :now"
                        " WHERE id = :id AND workspace_id = :ws"
                        " AND status = ANY(CAST(:open AS text[])) RETURNING id"
                    ),
                    {
                        "status": status,
                        "ref": None
                        if external_ref is None
                        else json.dumps(external_ref, ensure_ascii=False, sort_keys=True),
                        "ek": error_kind,
                        "now": self._clock.now(),
                        "id": execution_id,
                        "ws": self._ws,
                        "open": _OPEN_STATUSES,
                    },
                )
            ).first()
            if updated is None:
                await self._explain_failure(s, execution_id, "finish", same_status_ok=status)

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None:
        async with self._db.workspace_session(self._ws) as s:
            return await self._select(s, approval_id)

    async def _select(self, s: Any, approval_id: uuid.UUID) -> ExecutionRecord | None:
        row = (
            await s.execute(
                text(
                    "SELECT id, approval_id, status, external_ref, error_kind FROM execution"
                    " WHERE approval_id = :approval AND workspace_id = :ws"
                ),
                {"approval": approval_id, "ws": self._ws},
            )
        ).one_or_none()
        return None if row is None else _record(row)

    async def _explain_failure(
        self, s: Any, execution_id: uuid.UUID, op: str, same_status_ok: str | None = None
    ) -> None:
        row = (
            await s.execute(
                text("SELECT status FROM execution WHERE id = :id AND workspace_id = :ws"),
                {"id": execution_id, "ws": self._ws},
            )
        ).one_or_none()
        if row is None:
            raise RecordNotFound(f"execution을 찾을 수 없다: {execution_id}")
        if same_status_ok is not None and row.status == same_status_ok:
            return
        raise InvalidTransition(f"execution {execution_id}: {row.status} 상태에서 {op}는 불가")
