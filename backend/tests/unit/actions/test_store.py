import asyncio
import uuid

from orbit.actions.store import PENDING, SUCCEEDED, InMemoryExecutionStore


async def test_begin_creates_once() -> None:
    store = InMemoryExecutionStore()
    approval_id = uuid.uuid4()
    first, created1 = await store.begin(approval_id)
    second, created2 = await store.begin(approval_id)
    assert (created1, created2) == (True, False)
    assert first.id == second.id and first.status == PENDING


async def test_concurrent_begin_makes_one_row() -> None:
    store = InMemoryExecutionStore()
    approval_id = uuid.uuid4()
    results = await asyncio.gather(*(store.begin(approval_id) for _ in range(10)))
    assert sum(1 for _, created in results if created) == 1
    assert len({r.id for r, _ in results}) == 1


async def test_ref_and_finish_roundtrip_and_isolation() -> None:
    store = InMemoryExecutionStore()
    approval_id = uuid.uuid4()
    record, _ = await store.begin(approval_id)
    ref = {"a": 1}
    await store.record_ref(record.id, ref)
    ref["a"] = 2  # 호출자가 나중에 바꿔도 저장된 값은 그대로다
    got = await store.get(approval_id)
    assert got is not None and got.external_ref == {"a": 1}
    await store.finish(record.id, SUCCEEDED, {"b": 1}, None)
    done = await store.get(approval_id)
    assert done is not None and done.status == SUCCEEDED and done.external_ref == {"b": 1}
    assert await store.get(uuid.uuid4()) is None
