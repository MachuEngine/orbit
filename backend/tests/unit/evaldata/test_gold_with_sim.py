"""정답을 Sim 커넥터로 다시 확인한다.

생성기의 계산 코드와 따로, `list_events`·`get_event` 결과에서 이상적인 응답을 만들어
Gold의 정답 표현과 금지 표현을 검사한다. 날짜·시각 계산은 생성기 함수를 쓰지 않고 여기서 다시 한다.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

from orbit.connectors.contracts import CalendarEvent
from orbit.connectors.sim.calendar import SimCalendarConnector
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.dataset import Dataset, load_dataset
from orbit.evaldata.schema import Task
from orbit.evaldata.timeutil import KST


def _norm(text: str) -> str:
    return "".join(text.split())


def _hit(task: Task, answer: str) -> list[str]:
    """응답에서 충족하지 못한 fact_id 목록."""
    text = _norm(answer)
    return [
        f.fact_id for f in task.gold.answer_facts if not any(_norm(x) in text for x in f.any_of)
    ]


def _tag(task: Task, prefix: str) -> list[str]:
    return [t.split(":", 1)[1] for t in task.tags if t.startswith(prefix + ":")]


def _local(dt: datetime) -> datetime:
    return dt.astimezone(KST)


def _hhmm(dt: datetime) -> str:
    return f"{_local(dt):%H:%M}"


def _md(dt: datetime) -> str:
    local = _local(dt)
    return f"{local.month}월 {local.day}일"


class _Env:
    def __init__(self, ds: Dataset, path: Path) -> None:
        self.world = SimWorld.load(path / "world.json", FixedClock(ds.tasks[0].as_of_time))
        self.ds = ds

    def calendar(self, task: Task) -> tuple[SimCalendarConnector, str]:
        cal_id = self.ds.world_dict["accounts"][task.account_id]["calendars"][0]["id"]
        return SimCalendarConnector(self.world, task.account_id), cal_id

    async def events(
        self, task: Task, lo: datetime, hi: datetime, *, cancelled: bool = False
    ) -> list[CalendarEvent]:
        cal, cal_id = self.calendar(task)
        found = await cal.list_events(cal_id, lo, hi, include_cancelled=cancelled)
        return sorted(found, key=lambda e: (e.start, e.id))


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    lo = datetime(day.year, day.month, day.day, tzinfo=KST)
    return lo, lo + timedelta(days=1)


async def test_every_gold_matches_sim(dataset_dir: Path) -> None:
    ds = load_dataset(dataset_dir)
    env = _Env(ds, dataset_dir)
    checked: set[str] = set()
    for task in ds.tasks:
        kind = _tag(task, "type")[0]
        checked.add(kind)
        if kind in ("day_schedule", "cancelled_exclusion"):
            await _check_day(env, task, kind)
        elif kind == "free_slot":
            await _check_free_slot(env, task)
        elif kind == "reschedule":
            await _check_reschedule(env, task)
        elif kind == "person_next":
            await _check_person_next(env, task)
        else:
            await _check_project_week(env, task)
    assert len(checked) == 6


async def _check_day(env: _Env, task: Task, kind: str) -> None:
    day = date.fromisoformat(_tag(task, "date")[0])
    half = _tag(task, "half")[0]
    lo, hi = _day_bounds(day)

    def in_scope(e: CalendarEvent) -> bool:
        h = _local(e.start).hour
        return half == "all" or (h < 12) == (half == "오전")

    live = [e for e in await env.events(task, lo, hi) if in_scope(e)]
    everything = [e for e in await env.events(task, lo, hi, cancelled=True) if in_scope(e)]
    cancelled = [e for e in everything if e.status == "cancelled"]
    assert live
    ideal = "\n".join(f"{e.title} {_hhmm(e.start)}" for e in live)
    assert _hit(task, ideal) == []
    assert len(task.gold.answer_facts) == 2 * len(live)  # 빠뜨리거나 더한 일정이 없다
    assert sorted(task.gold.must_not_contain) == sorted(e.title for e in cancelled)
    assert not any(t in ideal for t in task.gold.must_not_contain)
    if kind == "cancelled_exclusion":
        assert cancelled
    # 잘못된 시각만 말하면 시각 fact가 빠진다
    wrong = "\n".join(e.title for e in live)
    assert _hit(task, wrong)


async def _check_free_slot(env: _Env, task: Task) -> None:
    day = date.fromisoformat(_tag(task, "date")[0])
    lo, hi = _day_bounds(day)
    live = await env.events(task, lo, hi)
    cursor = datetime(day.year, day.month, day.day, 9, tzinfo=KST)
    end = datetime(day.year, day.month, day.day, 18, tzinfo=KST)
    slot = None
    for e in live:
        if e.start - cursor >= timedelta(hours=1):
            slot = cursor
            break
        cursor = max(cursor, e.end)
    else:
        slot = cursor if end - cursor >= timedelta(hours=1) else None
    assert slot is not None
    assert _hit(task, _hhmm(slot)) == []
    # 구간 전체가 비어 있다
    busy = await env.events(task, slot, slot + timedelta(hours=1))
    assert busy == []


async def _check_reschedule(env: _Env, task: Task) -> None:
    (event_id,) = _tag(task, "ref_event")
    cal, cal_id = env.calendar(task)
    ev = await cal.get_event(cal_id, event_id)
    assert ev.status == "confirmed"
    assert ev.start > task.as_of_time
    assert int(task.difficulty["schedule_changes"]) >= 1
    assert ev.title in task.turns[0].content
    assert _hit(task, f"{_md(ev.start)} {_hhmm(ev.start)}") == []
    # 메일에는 변경 요청이 있다(캘린더가 최신 상태)
    mails = [
        m
        for m in env.ds.world_dict["accounts"][task.account_id]["messages"]
        if m["id"] in set(_tag(task, "ref_message"))
    ]
    assert any(ev.title in m["body_text"] for m in mails)


async def _check_person_next(env: _Env, task: Task) -> None:
    (email,) = _tag(task, "attendee")
    (event_id,) = _tag(task, "ref_event")
    asof = task.as_of_time
    found = [
        e
        for e in await env.events(task, asof, asof + timedelta(days=60))
        if any(a.email == email for a in e.attendees)
    ]
    assert found
    assert found[0].id == event_id
    assert _hit(task, f"{_md(found[0].start)} {_hhmm(found[0].start)}") == []


async def _check_project_week(env: _Env, task: Task) -> None:
    (project,) = _tag(task, "project")
    start = date.fromisoformat(_tag(task, "week_start")[0])
    lo = datetime(start.year, start.month, start.day, tzinfo=KST)
    live = [e for e in await env.events(task, lo, lo + timedelta(days=7)) if project in e.title]
    assert live
    ideal = "\n".join(f"{e.title} {_md(e.start)} {_hhmm(e.start)}" for e in live)
    assert _hit(task, ideal) == []
    assert len(task.gold.answer_facts) == 3 * len(live)
