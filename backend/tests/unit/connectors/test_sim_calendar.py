"""SimCalendarConnector 고유 동작: 장애 주입, 호출 기록, 상태 비교, 결정성."""

from datetime import UTC, datetime, timedelta

import pytest

from orbit.connectors.contracts import (
    AlreadyExists,
    EventDraft,
    EventPatch,
    NotFound,
    PreconditionFailed,
    RateLimited,
    ResultUnknown,
)
from orbit.connectors.sim.calendar import SimCalendarConnector
from orbit.connectors.sim.faults import FaultPlan
from orbit.connectors.sim.runtime import SimRuntime
from orbit.connectors.sim.world import SimWorld, diff
from orbit.core.clock import FixedClock

MAIN = "cal-alice-main"
NEW_ID = "evnaa00001"
LO, HI = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)


def _draft() -> EventDraft:
    return EventDraft(
        title="신규 일정",
        start=datetime(2026, 11, 20, 8, tzinfo=UTC),
        end=datetime(2026, 11, 20, 9, tzinfo=UTC),
    )


def _conn(world: SimWorld, *rules: dict[str, object]) -> SimCalendarConnector:
    plan = FaultPlan.from_dict({"rules": list(rules)})
    return SimCalendarConnector(world, "acct-alice", SimRuntime(plan))


async def test_result_unknown_applies_write(world: SimWorld) -> None:
    conn = _conn(world, {"tool": "create_event", "nth_call": 1, "error": "result_unknown"})
    with pytest.raises(ResultUnknown):
        await conn.create_event(MAIN, NEW_ID, _draft())
    assert (await conn.get_event(MAIN, NEW_ID)).title == "신규 일정"
    with pytest.raises(AlreadyExists):
        await conn.create_event(MAIN, NEW_ID, _draft())


async def test_result_unknown_not_applied_skips_write(world: SimWorld) -> None:
    conn = _conn(
        world, {"tool": "create_event", "nth_call": 1, "error": "result_unknown_not_applied"}
    )
    with pytest.raises(ResultUnknown):
        await conn.create_event(MAIN, NEW_ID, _draft())
    with pytest.raises(NotFound):
        await conn.get_event(MAIN, NEW_ID)
    await conn.create_event(MAIN, NEW_ID, _draft())  # 두 번째 호출은 정상 적용


async def test_rate_limited(world: SimWorld) -> None:
    conn = _conn(
        world, {"tool": "list_events", "nth_call": 1, "error": "rate_limited", "retry_after_s": 3}
    )
    with pytest.raises(RateLimited) as info:
        await conn.list_events(MAIN, LO, HI)
    assert info.value.retry_after_s == 3
    assert len(await conn.list_events(MAIN, LO, HI)) == 2


async def test_call_log_records_order_and_errors(world: SimWorld) -> None:
    conn = _conn(world)
    await conn.list_calendars()
    await conn.create_event(MAIN, NEW_ID, _draft())
    with pytest.raises(AlreadyExists):
        await conn.create_event(MAIN, NEW_ID, _draft())
    with pytest.raises(NotFound):
        await conn.get_event(MAIN, "evmissing01")
    records = conn.runtime.calls.records
    assert [(r.seq, r.tool, r.outcome, r.error_type) for r in records] == [
        (1, "list_calendars", "ok", None),
        (2, "create_event", "ok", None),
        (3, "create_event", "error", "AlreadyExists"),
        (4, "get_event", "error", "NotFound"),
    ]


async def test_diff_detects_create_update_cancel(world: SimWorld) -> None:
    before = world.copy()
    conn = _conn(world)
    await conn.create_event(MAIN, NEW_ID, _draft())
    e1 = await conn.get_event(MAIN, "evalice001")
    await conn.update_event(MAIN, "evalice001", EventPatch(title="변경"), if_match=e1.version)
    e2 = await conn.get_event(MAIN, "evalice002")
    await conn.cancel_event(MAIN, "evalice002", if_match=e2.version)

    d = diff(before, world)
    assert [c.event["id"] for c in d.events_created] == [NEW_ID]
    assert [(u.event_id, u.changes["title"].after) for u in d.events_updated] == [
        ("evalice001", "변경")
    ]
    assert [c.event_id for c in d.events_cancelled] == ["evalice002"]


async def test_same_seed_and_calls_give_same_versions(world: SimWorld) -> None:
    async def run(w: SimWorld) -> list[str]:
        conn = SimCalendarConnector(w, "acct-alice")
        created = await conn.create_event(MAIN, NEW_ID, _draft())
        updated = await conn.update_event(
            MAIN, NEW_ID, EventPatch(title="변경"), if_match=created.version
        )
        cancelled = await conn.cancel_event(MAIN, NEW_ID, if_match=updated.version)
        return [created.version, updated.version, cancelled.version]

    first = await run(world.fork(42))
    second = await run(world.fork(42))
    other = await run(world.fork(43))
    assert first == second
    assert len(set(first)) == 3
    assert first != other


async def test_uses_injected_clock(world: SimWorld, fixed_clock: FixedClock) -> None:
    conn = _conn(world)
    created = await conn.create_event(MAIN, NEW_ID, _draft())
    assert created.updated_at == fixed_clock.now()
    fixed_clock.advance(timedelta(hours=1))
    updated = await conn.update_event(
        MAIN, NEW_ID, EventPatch(title="변경"), if_match=created.version
    )
    assert updated.updated_at == fixed_clock.now()


async def test_list_events_includes_event_overlapping_period_start(world: SimWorld) -> None:
    conn = _conn(world)
    # evalice001은 11-12 01:00~02:00 UTC. 기간이 01:30에 시작해도 겹치므로 포함된다.
    events = await conn.list_events(
        MAIN, datetime(2026, 11, 12, 1, 30, tzinfo=UTC), datetime(2026, 11, 12, 2, 30, tzinfo=UTC)
    )
    assert [e.id for e in events] == ["evalice001"]
    # 끝 시각과 time_min이 같으면 겹치지 않는다
    assert (
        await conn.list_events(
            MAIN, datetime(2026, 11, 12, 2, tzinfo=UTC), datetime(2026, 11, 12, 2, 30, tzinfo=UTC)
        )
        == []
    )


async def test_recancel_is_idempotent_with_matching_version(world: SimWorld) -> None:
    conn = _conn(world)
    ev = await conn.get_event(MAIN, "evalice001")
    first = await conn.cancel_event(MAIN, "evalice001", if_match=ev.version)
    again = await conn.cancel_event(MAIN, "evalice001", if_match=first.version)
    assert again == first
    with pytest.raises(PreconditionFailed):
        await conn.cancel_event(MAIN, "evalice001", if_match=ev.version)


async def test_naive_datetime_rejected(world: SimWorld) -> None:
    conn = _conn(world)
    with pytest.raises(ValueError, match="시간대"):
        await conn.list_events(MAIN, LO.replace(tzinfo=None), HI)


def test_unknown_account_fails_fast(world: SimWorld) -> None:
    with pytest.raises(KeyError):
        SimCalendarConnector(world, "acct-nobody")


async def test_query_is_case_insensitive_substring_over_title_description_location(
    world: SimWorld,
) -> None:
    conn = _conn(world)
    await conn.create_event(
        MAIN,
        NEW_ID,
        EventDraft(
            title="Quarterly Review",
            start=datetime(2026, 11, 20, 8, tzinfo=UTC),
            end=datetime(2026, 11, 20, 9, tzinfo=UTC),
        ),
    )

    async def ids(q: str) -> list[str]:
        return [e.id for e in await conn.list_events(MAIN, LO, HI, query=q)]

    assert await ids("QUARTER") == [NEW_ID]  # 부분 문자열, 대소문자 무시
    assert await ids("quarterly review") == [NEW_ID]  # 여러 단어는 구절 그대로 일치
    assert await ids("회의실 a") == ["evalice001"]  # 장소
    assert await ids("합성 일정") == ["evalice001"]  # 설명
    assert await ids("없는검색어") == []


async def test_update_cancelled_event_is_allowed_and_stays_cancelled(world: SimWorld) -> None:
    conn = _conn(world)
    ev = await conn.get_event(MAIN, "evalice001")
    cancelled = await conn.cancel_event(MAIN, "evalice001", if_match=ev.version)
    updated = await conn.update_event(
        MAIN, "evalice001", EventPatch(title="취소 후 수정"), if_match=cancelled.version
    )
    assert updated.status == "cancelled"
    assert updated.title == "취소 후 수정"
    assert updated.version != cancelled.version


async def test_update_result_unknown_then_reconcile_by_stale_if_match(world: SimWorld) -> None:
    conn = _conn(world, {"tool": "update_event", "nth_call": 1, "error": "result_unknown"})
    before = await conn.get_event(MAIN, "evalice001")
    patch = EventPatch(title="대조 대상")
    with pytest.raises(ResultUnknown):
        await conn.update_event(MAIN, "evalice001", patch, if_match=before.version)
    # 재호출하지 않고 현재 상태를 읽어 대조한다: 적용됐으므로 version이 바뀌어 있다.
    current = await conn.get_event(MAIN, "evalice001")
    assert current.version != before.version
    assert current.title == "대조 대상"
    # 같은 if_match로 다시 보내면 낡은 버전이라 거절된다(중복 적용 방지).
    with pytest.raises(PreconditionFailed):
        await conn.update_event(MAIN, "evalice001", patch, if_match=before.version)
    assert (await conn.get_event(MAIN, "evalice001")) == current
