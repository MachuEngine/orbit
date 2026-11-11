"""SQL `ExecutionStore` (ADR-0012 §3, ADR-0003 조건 1).

Protocol 자체는 4B가 `orbit.actions`에 정의한다. 여기서는 같은 시그니처의 구현만 둔다.
각 메서드는 호출마다 새 워크스페이스 트랜잭션을 열고 커밋한다. 외부 호출 전에 `begin`이 커밋되어
있어야 강제 종료 뒤에도 "호출했을 수 있음"이 남는다.

- `begin`: `approval_id` 유일 제약으로 `pending` 행을 선기록한다. 이미 있으면
  기존 행과 False를 준다. `approved`가 아닌 approval에는 행을 만들지 않는다(DB 쪽 두 번째 방어선).
  새 행이면 호출자(owner)에게 lease(소유권)를 준다. 기존 행이면 소유권을 바꾸지 않는다.
- `claim`: 기존 행에 재진입할 때 lease를 얻는다(조건부 갱신). 얻지 못하면 False.
- `record_ref`, `finish`: lease 소유자이고 lease가 유효하며 상태가 `pending`·`unknown`일 때만
  반영한다. 아니면 예외 없이 False. `finish`는 어떤 상태로 끝나도 lease를 비운다.
- lease 시각은 주입 `Clock`이 아니라 DB `clock_timestamp()`로 부여·비교한다(프로세스 사이 시계
  어긋남과 무관하게 한 시계로 판단). `updated_at` 같은 도메인 시각은 주입 `Clock`을 쓴다.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from orbit.core.clock import Clock
from orbit.core.ids import Uuid7Generator
from orbit.db.session import AsyncDatabase
from orbit.records.errors import ApprovalNotApproved

EXECUTION_STATUSES = frozenset({"pending", "succeeded", "failed", "unknown", "needs_user"})
_OPEN_STATUSES = ["pending", "unknown"]


@dataclass(frozen=True)
class ExecutionRecord:
    id: uuid.UUID
    approval_id: uuid.UUID
    status: str
    external_ref: dict[str, Any] | None
    error_kind: str | None
    lease_owner: uuid.UUID | None = None
    lease_expires_at: datetime | None = None


def _record(row: Any) -> ExecutionRecord:
    return ExecutionRecord(
        id=row.id,
        approval_id=row.approval_id,
        status=row.status,
        external_ref=row.external_ref,
        error_kind=row.error_kind,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
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

    async def begin(
        self, approval_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float
    ) -> tuple[ExecutionRecord, bool]:
        async with self._db.workspace_session(self._ws) as s:
            now = self._clock.now()
            inserted = (
                await s.execute(
                    text(
                        "INSERT INTO execution (id, workspace_id, approval_id, status,"
                        " lease_owner, lease_expires_at, created_at, updated_at)"
                        " SELECT :id, workspace_id, id, 'pending', :owner,"
                        " clock_timestamp() + make_interval(secs => :lease), :now, :now"
                        " FROM approval"
                        " WHERE id = :approval AND workspace_id = :ws AND status = 'approved'"
                        " ON CONFLICT (approval_id) DO NOTHING"
                        " RETURNING id, approval_id, status, external_ref, error_kind,"
                        " lease_owner, lease_expires_at"
                    ),
                    {
                        "id": self._ids.next(),
                        "owner": owner,
                        "lease": lease_seconds,
                        "now": now,
                        "approval": approval_id,
                        "ws": self._ws,
                    },
                )
            ).one_or_none()
            if inserted is not None:
                return _record(inserted), True
            existing = await self._select(s, approval_id)
            if existing is None:
                # 승인이 없거나 다른 워크스페이스 것이거나 approved가 아니다. 구분해 알리지 않는다.
                raise ApprovalNotApproved(f"approved 상태의 approval이 아니다: {approval_id}")
            return existing, False

    async def claim(self, execution_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float) -> bool:
        """lease가 없거나 만료됐거나 이미 내 것이면 lease를 얻는다(`pending`·`unknown`만)."""
        async with self._db.workspace_session(self._ws) as s:
            now = self._clock.now()
            updated = (
                await s.execute(
                    text(
                        "UPDATE execution SET lease_owner = :owner,"
                        " lease_expires_at = clock_timestamp() + make_interval(secs => :lease),"
                        " updated_at = :now"
                        " WHERE id = :id AND workspace_id = :ws"
                        " AND status = ANY(CAST(:open AS text[]))"
                        " AND (lease_owner IS NULL OR lease_expires_at <= clock_timestamp()"
                        " OR lease_owner = :owner) RETURNING id"
                    ),
                    {
                        "owner": owner,
                        "lease": lease_seconds,
                        "now": now,
                        "id": execution_id,
                        "ws": self._ws,
                        "open": _OPEN_STATUSES,
                    },
                )
            ).first()
            return updated is not None

    async def record_ref(
        self, execution_id: uuid.UUID, owner: uuid.UUID, external_ref: dict[str, Any]
    ) -> bool:
        """`external_ref`를 통째로 바꾼다. 유효한 lease 소유자만 반영된다."""
        async with self._db.workspace_session(self._ws) as s:
            now = self._clock.now()
            updated = (
                await s.execute(
                    text(
                        "UPDATE execution SET external_ref = CAST(:ref AS jsonb), updated_at = :now"
                        " WHERE id = :id AND workspace_id = :ws"
                        " AND status = ANY(CAST(:open AS text[]))"
                        " AND lease_owner = :owner AND lease_expires_at > clock_timestamp()"
                        " RETURNING id"
                    ),
                    {
                        "ref": json.dumps(external_ref, ensure_ascii=False, sort_keys=True),
                        "now": now,
                        "id": execution_id,
                        "ws": self._ws,
                        "open": _OPEN_STATUSES,
                        "owner": owner,
                    },
                )
            ).first()
            return updated is not None

    async def finish(
        self,
        execution_id: uuid.UUID,
        owner: uuid.UUID,
        status: str,
        external_ref: dict[str, Any] | None,
        error_kind: str | None,
    ) -> bool:
        """유효한 lease 소유자의 `pending`·`unknown` 행에 상태를 기록한다. 반영되면 True.

        `external_ref`가 None이면 기존 값을 유지한다. `pending`이 아닌 어떤 상태로 끝나도
        lease를 비운다(`unknown`이면 다음 재진입이 바로 `claim`할 수 있다).
        """
        if status not in EXECUTION_STATUSES - {"pending"}:
            raise ValueError(f"finish에 쓸 수 없는 상태: {status}")
        async with self._db.workspace_session(self._ws) as s:
            now = self._clock.now()
            updated = (
                await s.execute(
                    text(
                        "UPDATE execution SET status = :status,"
                        " external_ref = COALESCE(CAST(:ref AS jsonb), external_ref),"
                        " error_kind = :ek, updated_at = :now,"
                        " lease_owner = NULL, lease_expires_at = NULL"
                        " WHERE id = :id AND workspace_id = :ws"
                        " AND status = ANY(CAST(:open AS text[]))"
                        " AND lease_owner = :owner AND lease_expires_at > clock_timestamp()"
                        " RETURNING id"
                    ),
                    {
                        "status": status,
                        "ref": None
                        if external_ref is None
                        else json.dumps(external_ref, ensure_ascii=False, sort_keys=True),
                        "ek": error_kind,
                        "now": now,
                        "id": execution_id,
                        "ws": self._ws,
                        "open": _OPEN_STATUSES,
                        "owner": owner,
                    },
                )
            ).first()
            return updated is not None

    async def get(self, approval_id: uuid.UUID) -> ExecutionRecord | None:
        async with self._db.workspace_session(self._ws) as s:
            return await self._select(s, approval_id)

    async def _select(self, s: Any, approval_id: uuid.UUID) -> ExecutionRecord | None:
        row = (
            await s.execute(
                text(
                    "SELECT id, approval_id, status, external_ref, error_kind, lease_owner,"
                    " lease_expires_at FROM execution"
                    " WHERE approval_id = :approval AND workspace_id = :ws"
                ),
                {"approval": approval_id, "ws": self._ws},
            )
        ).one_or_none()
        return None if row is None else _record(row)
