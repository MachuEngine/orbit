"""`ExecutionStore` Protocol과 메모리 구현 (ADR-0012 3절). SQL 구현은 `orbit.records`(4A)가 만든다.

각 메서드는 호출마다 커밋한다. `record_ref`는 `external_ref`를 통째로 바꾼다(병합 아님).
실행 소유권(lease): 행은 `lease_owner`가 `lease_expires_at`까지 소유한다. 소유자가 아닌 실행자의
`record_ref`·`finish`는 반영되지 않고 `False`를 돌려준다.
"""

import copy
import dataclasses
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

from orbit.core.clock import Clock
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
    lease_owner: uuid.UUID | None = None
    lease_expires_at: datetime | None = None


class ExecutionStore(Protocol):
    async def begin(
        self, approval_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float
    ) -> tuple[ExecutionRecord, bool]:
        """`approval_id`당 한 행을 `pending`으로 만들고 `owner`에게 소유권을 준다.

        이미 있으면 `(기존 행, False)`이며 소유권은 바뀌지 않는다.
        """
        ...

    async def claim(self, execution_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float) -> bool:
        """`pending`·`unknown` 행의 소유권을 얻는다. 비어 있거나 만료됐거나 내 것일 때만 성공."""
        ...

    async def record_ref(
        self, execution_id: uuid.UUID, owner: uuid.UUID, external_ref: dict[str, Any]
    ) -> bool: ...

    async def finish(
        self,
        execution_id: uuid.UUID,
        owner: uuid.UUID,
        status: str,
        external_ref: dict[str, Any],
        error_kind: str | None,
    ) -> bool:
        """소유자일 때만 종료 기록을 반영한다. `unknown`으로 끝내면 소유권을 놓는다."""
        ...

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None: ...


class InMemoryExecutionStore:
    """테스트용. 모든 연산이 `await` 없이 원자적으로 일어나므로 소유권 판정이 경쟁하지 않는다."""

    def __init__(self, clock: Clock, id_generator: Uuid7Generator | None = None) -> None:
        self._clock = clock
        self._ids = id_generator
        self._by_approval: dict[uuid.UUID, ExecutionRecord] = {}
        self._approval_of: dict[uuid.UUID, uuid.UUID] = {}
        self._mutex = threading.Lock()

    @staticmethod
    def _copy(record: ExecutionRecord) -> ExecutionRecord:
        return dataclasses.replace(record, external_ref=copy.deepcopy(record.external_ref))

    def _current(self, execution_id: uuid.UUID) -> ExecutionRecord:
        return self._by_approval[self._approval_of[execution_id]]

    def _update(self, execution_id: uuid.UUID, **changes: Any) -> None:
        approval_id = self._approval_of[execution_id]
        self._by_approval[approval_id] = dataclasses.replace(
            self._by_approval[approval_id], **changes
        )

    def _owns(self, record: ExecutionRecord, owner: uuid.UUID) -> bool:
        return record.lease_owner == owner and record.status not in TERMINAL

    async def begin(
        self, approval_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float
    ) -> tuple[ExecutionRecord, bool]:
        with self._mutex:
            existing = self._by_approval.get(approval_id)
            if existing is not None:
                return self._copy(existing), False
            record = ExecutionRecord(
                id=uuid7(self._ids),
                approval_id=approval_id,
                status=PENDING,
                external_ref={},
                error_kind=None,
                lease_owner=owner,
                lease_expires_at=self._clock.now() + timedelta(seconds=lease_seconds),
            )
            self._by_approval[approval_id] = record
            self._approval_of[record.id] = approval_id
            return self._copy(record), True

    async def claim(self, execution_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float) -> bool:
        with self._mutex:
            record = self._current(execution_id)
            if record.status in TERMINAL:
                return False
            free = (
                record.lease_owner is None
                or record.lease_owner == owner
                or record.lease_expires_at is None
                or record.lease_expires_at <= self._clock.now()
            )
            if not free:
                return False
            self._update(
                execution_id,
                lease_owner=owner,
                lease_expires_at=self._clock.now() + timedelta(seconds=lease_seconds),
            )
            return True

    async def record_ref(
        self, execution_id: uuid.UUID, owner: uuid.UUID, external_ref: dict[str, Any]
    ) -> bool:
        with self._mutex:
            if not self._owns(self._current(execution_id), owner):
                return False
            self._update(execution_id, external_ref=copy.deepcopy(external_ref))
            return True

    async def finish(
        self,
        execution_id: uuid.UUID,
        owner: uuid.UUID,
        status: str,
        external_ref: dict[str, Any],
        error_kind: str | None,
    ) -> bool:
        with self._mutex:
            if not self._owns(self._current(execution_id), owner):
                return False
            changes: dict[str, Any] = {
                "status": status,
                "external_ref": copy.deepcopy(external_ref),
                "error_kind": error_kind,
            }
            if status == UNKNOWN:
                changes.update(lease_owner=None, lease_expires_at=None)
            self._update(execution_id, **changes)
            return True

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None:
        with self._mutex:
            record = self._by_approval.get(approval_id)
            return None if record is None else self._copy(record)
