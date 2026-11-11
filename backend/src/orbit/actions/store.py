"""`ExecutionStore` Protocol과 메모리 구현 (ADR-0012 3절). SQL 구현은 `orbit.records`(4A)가 만든다.

각 메서드는 호출마다 커밋한다. `record_ref`는 `external_ref`를 통째로 바꾼다(병합 아님).
"""

import asyncio
import copy
import dataclasses
import uuid
from typing import Any, Protocol

from orbit.core.ids import Uuid7Generator, uuid7

PENDING = "pending"
SUCCEEDED = "succeeded"
FAILED = "failed"
UNKNOWN = "unknown"
NEEDS_USER = "needs_user"
TERMINAL = frozenset({SUCCEEDED, FAILED, NEEDS_USER})


@dataclasses.dataclass(frozen=True)
class ExecutionRecord:
    id: uuid.UUID
    approval_id: uuid.UUID
    status: str
    external_ref: dict[str, Any]
    error_kind: str | None


class ExecutionStore(Protocol):
    async def begin(self, approval_id: uuid.UUID) -> tuple[ExecutionRecord, bool]:
        """`approval_id`당 한 행을 `pending`으로 만든다. 이미 있으면 `(기존 행, False)`."""
        ...

    async def record_ref(self, execution_id: uuid.UUID, external_ref: dict[str, Any]) -> None: ...

    async def finish(
        self,
        execution_id: uuid.UUID,
        status: str,
        external_ref: dict[str, Any],
        error_kind: str | None,
    ) -> None: ...

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None: ...


class InMemoryExecutionStore:
    """테스트용. 동시 `begin`에도 `approval_id`당 행은 하나다."""

    def __init__(self, id_generator: Uuid7Generator | None = None) -> None:
        self._ids = id_generator
        self._by_approval: dict[uuid.UUID, ExecutionRecord] = {}
        self._approval_of: dict[uuid.UUID, uuid.UUID] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _copy(record: ExecutionRecord) -> ExecutionRecord:
        return dataclasses.replace(record, external_ref=copy.deepcopy(record.external_ref))

    async def begin(self, approval_id: uuid.UUID) -> tuple[ExecutionRecord, bool]:
        async with self._lock:
            existing = self._by_approval.get(approval_id)
            if existing is not None:
                return self._copy(existing), False
            record = ExecutionRecord(
                id=uuid7(self._ids),
                approval_id=approval_id,
                status=PENDING,
                external_ref={},
                error_kind=None,
            )
            self._by_approval[approval_id] = record
            self._approval_of[record.id] = approval_id
            return self._copy(record), True

    def _update(self, execution_id: uuid.UUID, **changes: Any) -> None:
        approval_id = self._approval_of[execution_id]
        self._by_approval[approval_id] = dataclasses.replace(
            self._by_approval[approval_id], **changes
        )

    async def record_ref(self, execution_id: uuid.UUID, external_ref: dict[str, Any]) -> None:
        async with self._lock:
            self._update(execution_id, external_ref=copy.deepcopy(external_ref))

    async def finish(
        self,
        execution_id: uuid.UUID,
        status: str,
        external_ref: dict[str, Any],
        error_kind: str | None,
    ) -> None:
        async with self._lock:
            self._update(
                execution_id,
                status=status,
                external_ref=copy.deepcopy(external_ref),
                error_kind=error_kind,
            )

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None:
        async with self._lock:
            record = self._by_approval.get(approval_id)
            return None if record is None else self._copy(record)
