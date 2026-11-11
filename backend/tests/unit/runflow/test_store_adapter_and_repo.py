"""실행 저장소 어댑터와 교착 판별 (ADR-0012 §3·§4). DB 없이 실행한다."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import OperationalError

from orbit.actions.store import ExecutionRecord as ActionsRecord
from orbit.records import executions as sql
from orbit.runflow.repo import is_deadlock
from orbit.runflow.store import ActionsStoreAdapter


class _FakeSql:
    def __init__(self, record: sql.ExecutionRecord) -> None:
        self._record = record

    async def begin(
        self, approval_id: uuid.UUID, owner: uuid.UUID, lease_seconds: float
    ) -> tuple[sql.ExecutionRecord, bool]:
        return self._record, True

    async def get(self, approval_id: uuid.UUID) -> sql.ExecutionRecord | None:
        return self._record


def _record(ref: dict[str, Any] | None) -> sql.ExecutionRecord:
    return sql.ExecutionRecord(
        id=uuid.uuid4(),
        approval_id=uuid.uuid4(),
        status="pending",
        external_ref=ref,
        error_kind=None,
        lease_owner=uuid.uuid4(),
        lease_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_adapter_turns_null_external_ref_into_empty_dict() -> None:
    inner: Any = _FakeSql(_record(None))
    adapter = ActionsStoreAdapter(inner)
    record, created = await adapter.begin(uuid.uuid4(), uuid.uuid4(), 1.0)
    assert created is True
    assert isinstance(record, ActionsRecord)
    assert record.external_ref == {}  # 실행기는 dict(record.external_ref)를 부른다
    got = await adapter.get(uuid.uuid4())
    assert got is not None and got.external_ref == {}


async def test_adapter_copies_external_ref() -> None:
    source = {"attempted": True}
    inner: Any = _FakeSql(_record(source))
    got = await ActionsStoreAdapter(inner).get(uuid.uuid4())
    assert got is not None
    got.external_ref["x"] = 1
    assert source == {"attempted": True}


class _Orig(Exception):  # noqa: N818
    def __init__(self, sqlstate: str) -> None:
        super().__init__("합성 오류")
        self.sqlstate = sqlstate


def test_is_deadlock_checks_sqlstate_40p01() -> None:
    deadlock = OperationalError("stmt", {}, _Orig("40P01"))
    other = OperationalError("stmt", {}, _Orig("23505"))
    assert is_deadlock(deadlock) is True
    assert is_deadlock(other) is False
    assert is_deadlock(RuntimeError("x")) is False
