"""`SqlExecutionStore`(4A)를 실행기(4B)의 `ExecutionStore` Protocol에 맞추는 어댑터.

두 구현은 같은 메서드를 갖지만 반환하는 `ExecutionRecord` 타입이 다르다. SQL 쪽은 새 행의
`external_ref`가 NULL(None)이고, 실행기는 항상 dict를 기대한다(`dict(record.external_ref)`).
어느 쪽 계약도 바꾸지 않고 여기서 None을 빈 dict로 바꿔 넘긴다.
"""

import uuid
from typing import Any

from orbit.actions.store import ExecutionRecord
from orbit.records import executions as sql


def _convert(record: sql.ExecutionRecord) -> ExecutionRecord:
    return ExecutionRecord(
        id=record.id,
        approval_id=record.approval_id,
        status=record.status,
        external_ref=dict(record.external_ref or {}),
        error_kind=record.error_kind,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
    )


class ActionsStoreAdapter:
    def __init__(self, inner: sql.SqlExecutionStore) -> None:
        self._inner = inner

    async def begin(
        self, approval_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float
    ) -> tuple[ExecutionRecord, bool]:
        record, created = await self._inner.begin(approval_id, owner, lease_seconds)
        return _convert(record), created

    async def claim(self, execution_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float) -> bool:
        return await self._inner.claim(execution_id, owner, lease_seconds)

    async def record_ref(
        self, execution_id: uuid.UUID, owner: uuid.UUID, external_ref: dict[str, Any]
    ) -> bool:
        return await self._inner.record_ref(execution_id, owner, external_ref)

    async def finish(
        self,
        execution_id: uuid.UUID,
        owner: uuid.UUID,
        status: str,
        external_ref: dict[str, Any],
        error_kind: str | None,
    ) -> bool:
        return await self._inner.finish(execution_id, owner, status, external_ref, error_kind)

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None:
        record = await self._inner.get(approval_id)
        return None if record is None else _convert(record)
