"""CalendarConnector 계약 테스트 (ADR-0008 3절). 계약 메서드만 쓰고 Sim 내부 상태는 보지 않는다."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from orbit.connectors.contracts import (
    AlreadyExists,
    CalendarEvent,
    EventDraft,
    EventPatch,
    NotFound,
    PreconditionFailed,
)
from orbit.connectors.sim.world import SimWorld

from .conftest import CalendarFactory

# ---- 시드 값. 6단계 Google 테스트 계정에도 같은 시드가 필요한 값이다.
# 팩토리 시그니처가 (world, account_id)뿐이라 ID를 픽스처에서 받지 못한다. 어댑터 쪽에서
# 캘린더 ID를 Google이 정하면 이 상수를 픽스처로 바꿔야 한다.
ALICE = "acct-alice"  # 테스트 계정 A
OTHER_ACCOUNT_CAL = "cal-bob-main"  # A가 접근할 수 없는 다른 계정의 캘린더 (계정 경계 테스트)
OTHER_ACCOUNT_EV = "evbob00001"  # 위 캘린더의 일정
OTHER_ACCOUNT_VERSION = "v-b1"  # 위 일정의 version (Google에서는 etag 등 실제 값)
MAIN = "cal-alice-main"  # A의 캘린더 1: EV1(11-12 01:00~02:00 UTC), EV2(03:00~04:00 UTC)
TEAM = "cal-alice-team"  # A의 캘린더 2: 일정 1개(EV1·EV2 없음)
EV1 = "evalice001"  # MAIN, 제목 "주간 회의", 장소 "회의실 A", 설명 "합성 일정"
EV2 = "evalice002"  # MAIN, 제목 "점심"
EV1_TITLE_WORD = "주간"  # EV1 제목의 완전한 단어이며 다른 일정 제목에는 없다
MISSING_EV = "evmissing01"  # 어디에도 없는 일정 ID(형식은 유효)
ABSENT_WORD = "없는검색어"  # 어떤 일정에도 없는 단어
WIDE_MIN = datetime(2026, 11, 1, tzinfo=UTC)
WIDE_MAX = datetime(2026, 12, 1, tzinfo=UTC)
NEW_ID = "evnaa00001"
NAIVE_MIN = WIDE_MIN.replace(tzinfo=None)
NAIVE_MAX = WIDE_MAX.replace(tzinfo=None)


def _draft(title: str = "신규 일정", hour: int = 8, day: int = 20) -> EventDraft:
    return EventDraft(
        title=title,
        start=datetime(2026, 11, day, hour, tzinfo=UTC),
        end=datetime(2026, 11, day, hour + 1, tzinfo=UTC),
        description="합성 설명",
        location="합성 장소",
    )


async def test_list_calendars(calendar_factory: CalendarFactory, minimal_world: SimWorld) -> None:
    cals = await calendar_factory(minimal_world, ALICE).list_calendars()
    assert {c.id for c in cals} == {MAIN, TEAM}


async def test_list_events_sorted_by_start(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    events = await conn.list_events(MAIN, WIDE_MIN, WIDE_MAX)
    assert [e.id for e in events] == [EV1, EV2]
    await conn.create_event(MAIN, NEW_ID, _draft(hour=0, day=11))
    events = await conn.list_events(MAIN, WIDE_MIN, WIDE_MAX)
    assert [e.id for e in events] == [NEW_ID, EV1, EV2]


async def test_list_events_period_filters(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    # evalice001은 11-12 01:00~02:00, evalice002는 03:00~04:00
    lo, hi = datetime(2026, 11, 12, 0, tzinfo=UTC), datetime(2026, 11, 12, 2, 30, tzinfo=UTC)
    assert [e.id for e in await conn.list_events(MAIN, lo, hi)] == [EV1]
    lo, hi = datetime(2026, 11, 14, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC)
    assert await conn.list_events(MAIN, lo, hi) == []


async def test_list_events_handles_timezone_offsets(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    kst = timezone(timedelta(hours=9))
    conn = calendar_factory(minimal_world, ALICE)
    # 11-12 10:00~11:30 KST = 01:00~02:30 UTC -> evalice001만 겹친다
    events = await conn.list_events(
        MAIN, datetime(2026, 11, 12, 10, tzinfo=kst), datetime(2026, 11, 12, 11, 30, tzinfo=kst)
    )
    assert [e.id for e in events] == [EV1]


async def test_list_events_excludes_cancelled_by_default(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    ev = await conn.get_event(MAIN, EV2)
    await conn.cancel_event(MAIN, EV2, if_match=ev.version)
    default = await conn.list_events(MAIN, WIDE_MIN, WIDE_MAX)
    assert [e.id for e in default] == [EV1]
    full = await conn.list_events(MAIN, WIDE_MIN, WIDE_MAX, include_cancelled=True)
    assert [e.id for e in full] == [EV1, EV2]
    assert full[1].status == "cancelled"


async def test_list_events_query_finds_whole_title_word(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    # 계약은 단어 단위 검색만 보장한다(Google `q`는 전문 검색이다).
    conn = calendar_factory(minimal_world, ALICE)
    assert [
        e.id for e in await conn.list_events(MAIN, WIDE_MIN, WIDE_MAX, query=EV1_TITLE_WORD)
    ] == [EV1]
    await conn.create_event(MAIN, NEW_ID, _draft(title="Quarterly Review"))
    found = await conn.list_events(MAIN, WIDE_MIN, WIDE_MAX, query="quarterly")
    assert [e.id for e in found] == [NEW_ID]


async def test_list_events_query_without_match_returns_nothing(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    assert await conn.list_events(MAIN, WIDE_MIN, WIDE_MAX, query=ABSENT_WORD) == []


async def test_get_event(calendar_factory: CalendarFactory, minimal_world: SimWorld) -> None:
    ev = await calendar_factory(minimal_world, ALICE).get_event(MAIN, EV1)
    assert isinstance(ev, CalendarEvent)
    assert (ev.id, ev.calendar_id, ev.title) == (EV1, MAIN, "주간 회의")
    assert ev.start.tzinfo is not None


async def test_get_event_not_found(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    with pytest.raises(NotFound):
        await conn.get_event(MAIN, MISSING_EV)
    with pytest.raises(NotFound):  # 일정은 있지만 다른 캘린더에 속한다
        await conn.get_event(TEAM, EV1)


async def test_create_event(calendar_factory: CalendarFactory, minimal_world: SimWorld) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    draft = _draft()
    created = await conn.create_event(MAIN, NEW_ID, draft)
    assert (created.id, created.calendar_id) == (NEW_ID, MAIN)
    assert (created.title, created.start, created.end) == (draft.title, draft.start, draft.end)
    assert created.status == "confirmed"
    assert created.version
    assert await conn.get_event(MAIN, NEW_ID) == created


async def test_create_event_already_exists(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    with pytest.raises(AlreadyExists):
        await conn.create_event(MAIN, EV1, _draft())
    await conn.create_event(MAIN, NEW_ID, _draft())
    with pytest.raises(AlreadyExists):
        await conn.create_event(MAIN, NEW_ID, _draft(title="다른 제목"))
    assert (await conn.get_event(MAIN, NEW_ID)).title == "신규 일정"


async def test_create_event_cancelled_id_still_exists(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    ev = await conn.get_event(MAIN, EV1)
    await conn.cancel_event(MAIN, EV1, if_match=ev.version)
    with pytest.raises(AlreadyExists):
        await conn.create_event(MAIN, EV1, _draft())


@pytest.mark.parametrize("bad_id", ["abc", "EVNEW00001", "ev-new-0001", "evnew0000w", "a" * 1025])
async def test_create_event_rejects_invalid_id(
    calendar_factory: CalendarFactory, minimal_world: SimWorld, bad_id: str
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    with pytest.raises(ValueError, match="일정 ID"):
        await conn.create_event(MAIN, bad_id, _draft())


async def test_update_event(calendar_factory: CalendarFactory, minimal_world: SimWorld) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    before = await conn.get_event(MAIN, EV1)
    updated = await conn.update_event(
        MAIN,
        EV1,
        EventPatch(title="변경된 회의", location="회의실 C"),
        if_match=before.version,
    )
    assert (updated.title, updated.location) == ("변경된 회의", "회의실 C")
    assert (updated.description, updated.start, updated.end) == (
        before.description,
        before.start,
        before.end,
    )
    assert updated.version != before.version
    assert await conn.get_event(MAIN, EV1) == updated


async def test_update_event_reschedules(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    before = await conn.get_event(MAIN, EV1)
    start, end = datetime(2026, 11, 15, 1, tzinfo=UTC), datetime(2026, 11, 15, 2, tzinfo=UTC)
    updated = await conn.update_event(
        MAIN, EV1, EventPatch(start=start, end=end), if_match=before.version
    )
    assert (updated.start, updated.end) == (start, end)


async def test_update_event_precondition_failed(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    before = await conn.get_event(MAIN, EV1)
    with pytest.raises(PreconditionFailed):
        await conn.update_event(MAIN, EV1, EventPatch(title="x"), if_match="stale")
    assert await conn.get_event(MAIN, EV1) == before
    # 성공한 수정 뒤에는 이전 버전이 낡은 값이 된다
    await conn.update_event(MAIN, EV1, EventPatch(title="첫 수정"), if_match=before.version)
    with pytest.raises(PreconditionFailed):
        await conn.update_event(MAIN, EV1, EventPatch(title="두 번째"), if_match=before.version)
    assert (await conn.get_event(MAIN, EV1)).title == "첫 수정"


async def test_update_event_not_found(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    with pytest.raises(NotFound):
        await conn.update_event(MAIN, MISSING_EV, EventPatch(title="x"), if_match="any")


async def test_cancel_event(calendar_factory: CalendarFactory, minimal_world: SimWorld) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    before = await conn.get_event(MAIN, EV1)
    cancelled = await conn.cancel_event(MAIN, EV1, if_match=before.version)
    assert cancelled.status == "cancelled"
    assert cancelled.version != before.version
    got = await conn.get_event(MAIN, EV1)  # 삭제가 아니라 상태 변경이다
    assert got.status == "cancelled"


async def test_cancel_event_precondition_failed_and_not_found(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    with pytest.raises(PreconditionFailed):
        await conn.cancel_event(MAIN, EV1, if_match="stale")
    assert (await conn.get_event(MAIN, EV1)).status == "confirmed"
    with pytest.raises(NotFound):
        await conn.cancel_event(MAIN, MISSING_EV, if_match="any")


async def test_other_account_calendar_is_not_found(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    bob_cal, bob_ev = OTHER_ACCOUNT_CAL, OTHER_ACCOUNT_EV
    with pytest.raises(NotFound):
        await conn.list_events(bob_cal, WIDE_MIN, WIDE_MAX)
    with pytest.raises(NotFound):
        await conn.get_event(bob_cal, bob_ev)
    with pytest.raises(NotFound):
        await conn.create_event(bob_cal, NEW_ID, _draft())
    with pytest.raises(NotFound):
        await conn.update_event(
            bob_cal, bob_ev, EventPatch(title="x"), if_match=OTHER_ACCOUNT_VERSION
        )
    with pytest.raises(NotFound):
        await conn.cancel_event(bob_cal, bob_ev, if_match=OTHER_ACCOUNT_VERSION)
    assert {c.id for c in await conn.list_calendars()} == {MAIN, TEAM}


# ---- 계약 해석 규칙 (종일 일정, 병합 검증, 기간 인자)


def _all_day(day: int, days: int = 1) -> EventDraft:
    return EventDraft(
        title="종일 일정",
        start=datetime(2026, 11, day, tzinfo=UTC),
        end=datetime(2026, 11, day, tzinfo=UTC) + timedelta(days=days),
        all_day=True,
    )


async def test_create_all_day_event_uses_exclusive_end(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    created = await conn.create_event(MAIN, NEW_ID, _all_day(21))
    assert created.all_day
    assert created.end - created.start == timedelta(days=1)


async def test_create_all_day_event_rejects_non_midnight_or_empty(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    with pytest.raises(ValueError):
        await conn.create_event(
            MAIN,
            NEW_ID,
            EventDraft(
                title="x",
                start=datetime(2026, 11, 21, 1, tzinfo=UTC),
                end=datetime(2026, 11, 22, tzinfo=UTC),
                all_day=True,
            ),
        )
    with pytest.raises(ValueError):  # 배타적 end이므로 start == end는 빈 일정이다
        await conn.create_event(
            MAIN,
            NEW_ID,
            EventDraft(
                title="x",
                start=datetime(2026, 11, 21, tzinfo=UTC),
                end=datetime(2026, 11, 21, tzinfo=UTC),
                all_day=True,
            ),
        )
    with pytest.raises(NotFound):  # 실패한 생성은 아무것도 남기지 않는다
        await conn.get_event(MAIN, NEW_ID)


async def test_update_to_all_day_requires_midnight(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    before = await conn.get_event(MAIN, EV1)  # 01:00~02:00 UTC
    with pytest.raises(ValueError):
        await conn.update_event(MAIN, EV1, EventPatch(all_day=True), if_match=before.version)
    assert await conn.get_event(MAIN, EV1) == before


async def test_update_rejects_invalid_merged_range_and_keeps_version(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    before = await conn.get_event(MAIN, EV1)  # 01:00~02:00 UTC
    # 시작만 끝 이후로 옮기면 병합 결과의 start > end
    with pytest.raises(ValueError):
        await conn.update_event(
            MAIN,
            EV1,
            EventPatch(start=datetime(2026, 11, 12, 5, tzinfo=UTC)),
            if_match=before.version,
        )
    # start == end도 거절한다
    with pytest.raises(ValueError):
        await conn.update_event(
            MAIN,
            EV1,
            EventPatch(end=before.start),
            if_match=before.version,
        )
    after = await conn.get_event(MAIN, EV1)
    assert after == before
    assert after.version == before.version


async def test_list_events_rejects_naive_and_inverted_period(
    calendar_factory: CalendarFactory, minimal_world: SimWorld
) -> None:
    conn = calendar_factory(minimal_world, ALICE)
    with pytest.raises(ValueError):
        await conn.list_events(MAIN, NAIVE_MIN, WIDE_MAX)
    with pytest.raises(ValueError):
        await conn.list_events(MAIN, WIDE_MIN, NAIVE_MAX)
    with pytest.raises(ValueError):
        await conn.list_events(MAIN, WIDE_MAX, WIDE_MIN)
    with pytest.raises(ValueError):
        await conn.list_events(MAIN, WIDE_MIN, WIDE_MIN)
