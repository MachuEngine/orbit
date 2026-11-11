import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from orbit.actions.store import FAILED, PENDING, SUCCEEDED, UNKNOWN, InMemoryExecutionStore
from orbit.core.clock import FixedClock

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
A, B = uuid.UUID(int=1), uuid.UUID(int=2)


def make() -> tuple[InMemoryExecutionStore, FixedClock]:
    clock = FixedClock(NOW)
    return InMemoryExecutionStore(clock), clock


async def test_begin_creates_once_and_gives_lease() -> None:
    store, _ = make()
    approval_id = uuid.uuid4()
    first, created1 = await store.begin(approval_id, A, NOW + timedelta(minutes=5))
    second, created2 = await store.begin(approval_id, B, NOW + timedelta(minutes=5))
    assert (created1, created2) == (True, False)
    assert first.id == second.id and first.status == PENDING
    assert second.lease_owner == A and second.lease_expires_at == NOW + timedelta(minutes=5)


async def test_concurrent_begin_makes_one_row() -> None:
    store, _ = make()
    approval_id = uuid.uuid4()
    results = await asyncio.gather(
        *(store.begin(approval_id, uuid.uuid4(), NOW + timedelta(minutes=5)) for _ in range(10))
    )
    assert sum(1 for _, created in results if created) == 1
    assert len({r.id for r, _ in results}) == 1


async def test_claim_only_when_free_or_expired() -> None:
    store, clock = make()
    approval_id = uuid.uuid4()
    record, _ = await store.begin(approval_id, A, NOW + timedelta(minutes=5))
    assert not await store.claim(record.id, B, NOW + timedelta(minutes=10))
    assert await store.claim(record.id, A, NOW + timedelta(minutes=10))  # 내 것은 연장된다
    clock.advance(timedelta(minutes=10))
    assert await store.claim(record.id, B, NOW + timedelta(minutes=20))
    got = await store.get(approval_id)
    assert got is not None and got.lease_owner == B


async def test_non_owner_cannot_record_or_finish() -> None:
    store, _ = make()
    approval_id = uuid.uuid4()
    record, _ = await store.begin(approval_id, A, NOW + timedelta(minutes=5))
    assert not await store.record_ref(record.id, B, {"x": 1})
    assert not await store.finish(record.id, B, FAILED, {}, "X")
    got = await store.get(approval_id)
    assert got is not None and got.status == PENDING and got.external_ref == {}


async def test_ref_and_finish_roundtrip_and_isolation() -> None:
    store, _ = make()
    approval_id = uuid.uuid4()
    record, _ = await store.begin(approval_id, A, NOW + timedelta(minutes=5))
    ref = {"a": 1}
    assert await store.record_ref(record.id, A, ref)
    ref["a"] = 2  # 호출자가 나중에 바꿔도 저장된 값은 그대로다
    got = await store.get(approval_id)
    assert got is not None and got.external_ref == {"a": 1}
    assert await store.finish(record.id, A, SUCCEEDED, {"b": 1}, None)
    done = await store.get(approval_id)
    assert done is not None and done.status == SUCCEEDED and done.external_ref == {"b": 1}
    assert not await store.claim(record.id, B, NOW + timedelta(minutes=5))  # 종료된 행
    assert not await store.finish(record.id, A, FAILED, {}, None)
    assert await store.get(uuid.uuid4()) is None


async def test_unknown_releases_lease() -> None:
    store, _ = make()
    approval_id = uuid.uuid4()
    record, _ = await store.begin(approval_id, A, NOW + timedelta(minutes=5))
    assert await store.finish(record.id, A, UNKNOWN, {}, "TransientError")
    assert await store.claim(record.id, B, NOW + timedelta(minutes=5))
