"""정답을 Sim 커넥터로 다시 확인한다.

생성기의 계산 코드와 따로, `list_events`·`get_event` 결과에서 구조화된 정답(`expected_answer`)을
다시 계산해 비교한다. 이상적인 응답 텍스트로 보조 지표(`answer_facts`)도 확인한다.
날짜·시각 계산은 생성기 함수를 쓰지 않고 여기서 다시 한다.
"""

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from orbit.connectors.contracts import CalendarEvent
from orbit.connectors.sim.calendar import SimCalendarConnector
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.dataset import Dataset, load_dataset
from orbit.evaldata.schema import (
    EventAnswer,
    EventSetAnswer,
    EventTimeAnswer,
    Task,
    TimeSlotAnswer,
)
from orbit.evaldata.timeutil import KST


def _norm(text: str) -> str:
    return "".join(text.split())


def _match(text: str, cand: str) -> bool:
    """공백 무시 + 숫자 경계 일치. 러너의 규칙과 같고, 통합 때 정리하려고 import하지 않는다."""
    text, cand = _norm(text), _norm(cand)
    start = 0
    while (i := text.find(cand, start)) != -1:
        j = i + len(cand)
        ok = True
        if i > 0 and text[i - 1].isdigit():
            ok = False
        if ok and cand[-1].isdigit() and j < len(text) and text[j].isdigit():
            ok = False
        if ok and cand.endswith("시") and re.match(r"(\d+분|반)", text[j:]):
            ok = False
        if ok:
            return True
        start = i + 1
    return False


def _hit(task: Task, answer: str) -> list[str]:
    """응답에서 충족하지 못한 fact_id 목록."""
    return [
        f.fact_id for f in task.gold.answer_facts if not any(_match(answer, x) for x in f.any_of)
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


def test_match_rules() -> None:
    assert _match("10시에 만나요", "10시")
    assert not _match("10시 30분에 만나요", "10시")
    assert not _match("110시", "10시")
    assert not _match("110:00", "10:00")
    assert not _match("10:005", "10:00")
    assert not _match("10시반", "10시")
    assert _match("오전 10 시", "오전 10시")


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


def _refs_of(env: _Env, task: Task, events: list[CalendarEvent]) -> set[tuple[str, str]]:
    _, cal_id = env.calendar(task)
    return {(cal_id, e.id) for e in events}


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
    answer = task.gold.expected_answer
    assert isinstance(answer, EventSetAnswer)
    assert {(r.calendar_id, r.event_id) for r in answer.events} == _refs_of(env, task, live)
    assert len(answer.events) == len(live)
    if kind == "cancelled_exclusion":
        assert cancelled
        assert not {e.id for e in cancelled} & {r.event_id for r in answer.events}
    assert task.gold.must_not_contain == []
    ideal = "; ".join(f"{e.title} {_hhmm(e.start)}" for e in live)
    assert _hit(task, ideal) == []
    assert len(task.gold.answer_facts) == 2 * len(live)
    assert _hit(task, "\n".join(e.title for e in live))  # 시각이 없으면 fact가 빠진다


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
    answer = task.gold.expected_answer
    assert isinstance(answer, TimeSlotAnswer)
    assert answer.start == slot  # 같은 순간
    assert _hit(task, _hhmm(slot)) == []
    assert await env.events(task, slot, slot + timedelta(hours=1)) == []


async def _check_reschedule(env: _Env, task: Task) -> None:
    (event_id,) = _tag(task, "ref_event")
    cal, cal_id = env.calendar(task)
    ev = await cal.get_event(cal_id, event_id)
    assert ev.status == "confirmed"
    assert ev.start > task.as_of_time
    assert int(task.difficulty["schedule_changes"]) >= 1
    assert ev.title in task.turns[0].content
    answer = task.gold.expected_answer
    assert isinstance(answer, EventTimeAnswer)
    assert (answer.event.calendar_id, answer.event.event_id) == (cal_id, event_id)
    assert answer.start == ev.start
    assert _hit(task, f"{_md(ev.start)} {_hhmm(ev.start)}") == []
    mails = [
        m
        for m in env.ds.world_dict["accounts"][task.account_id]["messages"]
        if m["id"] in set(_tag(task, "ref_message"))
    ]
    assert any(ev.title in m["body_text"] for m in mails)


async def _check_person_next(env: _Env, task: Task) -> None:
    (email,) = _tag(task, "attendee")
    asof = task.as_of_time
    found = [
        e
        for e in await env.events(task, asof, asof + timedelta(days=60))
        if any(a.email == email for a in e.attendees)
    ]
    assert found
    answer = task.gold.expected_answer
    assert isinstance(answer, EventAnswer)
    _, cal_id = env.calendar(task)
    assert (answer.event.calendar_id, answer.event.event_id) == (cal_id, found[0].id)
    assert _hit(task, f"{_md(found[0].start)} {_hhmm(found[0].start)}") == []


async def _check_project_week(env: _Env, task: Task) -> None:
    (project,) = _tag(task, "project")
    start = date.fromisoformat(_tag(task, "week_start")[0])
    lo = datetime(start.year, start.month, start.day, tzinfo=KST)
    live = [e for e in await env.events(task, lo, lo + timedelta(days=7)) if project in e.title]
    assert live
    answer = task.gold.expected_answer
    assert isinstance(answer, EventSetAnswer)
    assert {(r.calendar_id, r.event_id) for r in answer.events} == _refs_of(env, task, live)
    ideal = "; ".join(f"{e.title} {_md(e.start)} {_hhmm(e.start)}" for e in live)
    assert _hit(task, ideal) == []
    assert len(task.gold.answer_facts) == 3 * len(live)
