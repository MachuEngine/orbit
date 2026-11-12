"""질문과 정답(Gold) 계산. 정답은 최종 캘린더(`EventRec` 목록)에서 코드로 계산한다 (ADR-0009, 0010).

일정 조회 유형(읽기 전용) 6종과 구조화된 정답(`expected_answer`):
- day_schedule: 특정 날짜(와 오전/오후)의 일정 -> event_set
- cancelled_exclusion: 위와 같지만 범위 안에 취소된 일정이 있다 -> event_set (취소 일정은 빠진다)
- project_week: 이번/다음 주 특정 프로젝트 회의 전부 -> event_set
- person_next: 특정 인물과의 다음 회의 -> event
- reschedule: 메일로 시간이 바뀐 회의의 현재(캘린더 최신) 시각 -> event_time
- free_slot: 업무 시간(09~18시 KST) 안에서 가장 이른 1시간 빈 시간 -> time_slot

`answer_facts`는 보조 지표다. 반복 일정 안내 메일은 배경 아티팩트로 근거 태그에서 제외한다
(특정 일정의 근거가 아니며, 연결하면 dev와 test가 한 묶음으로 뭉친다).
정답 계산이 불가능하거나 모호하면 버릴 사유(문자열)를 돌려주고,
호출자가 그 명세를 버린다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from orbit.connectors.contracts import CALENDAR_TOOLS, MAIL_SYNC_METHODS, MAIL_TOOLS
from orbit.evaldata import texts
from orbit.evaldata.records import NOISE_KINDS, EventRec, MailRec, TaskSpec
from orbit.evaldata.schema import (
    AnswerFact,
    EventAnswer,
    EventRef,
    EventSetAnswer,
    EventTimeAnswer,
    ExpectedAnswer,
    Gold,
    TimeSlotAnswer,
)
from orbit.evaldata.timeutil import (
    KST,
    at_local,
    date_forms,
    half_of,
    hhmm,
    local_date,
    time_forms,
    week_start,
    weekday_ko,
)
from orbit.evaldata.worldmodel import WorldModel

READ_TOOLS = frozenset(
    {
        "list_calendars",
        "list_events",
        "get_event",
        "search_messages",
        "get_message",
        "get_thread",
        "get_draft",
    }
)
FORBIDDEN_TOOLS = sorted((CALENDAR_TOOLS | MAIL_TOOLS) - READ_TOOLS - MAIL_SYNC_METHODS)
WORK_START_HOUR = 9
WORK_END_HOUR = 18

# 버리는 사유
EMPTY_SCOPE = "정답 범위가 비어 있음"
NO_CANCELLED = "범위에 취소 일정이 없음"
NOON_BOUNDARY = "오전/오후 경계에 걸친 일정"
IN_PROGRESS = "as_of에 진행 중이거나 막 시작하는 일정"
AMBIGUOUS_TITLE = "일정 제목이 모호함"
AMBIGUOUS_NEXT = "다음 회의가 동시에 둘 이상"
PAST_IN_WEEK = "이번 주 지난 회의가 섞임"
NOT_FUTURE = "일정이 as_of 이전"


@dataclass
class TaskParts:
    question: str
    gold: Gold
    tags: list[str]
    difficulty: dict[str, int | float | str]


# ---------------------------------------------------------------- 계산 함수 (테스트도 참고)


def day_scope(
    events: list[EventRec], user: str, day: date, half: str | None
) -> tuple[list[EventRec], list[EventRec]]:
    """(유효 일정, 취소된 일정). 오전/오후는 시작 시각(KST) 기준이다."""
    scoped = [
        e
        for e in events
        if e.user == user
        and local_date(e.start) == day
        and (half is None or half_of(e.start) == half)
    ]

    def key(e: EventRec) -> tuple[datetime, str]:
        return (e.start, e.id)

    return (
        sorted((e for e in scoped if e.status == "confirmed"), key=key),
        sorted((e for e in scoped if e.status == "cancelled"), key=key),
    )


def straddles_noon(events: list[EventRec], user: str, day: date) -> bool:
    """그 날 12:00을 걸치는 일정(상태 무관)이 있는가."""
    noon = at_local(day, 12)
    return any(
        e.user == user and local_date(e.start) == day and e.start < noon < e.end for e in events
    )


def earliest_free(events: list[EventRec], user: str, day: date) -> datetime | None:
    """업무 시간 안에서 유효 일정에 겹치지 않는 가장 이른 1시간 구간의 시작."""
    cursor = at_local(day, WORK_START_HOUR)
    limit = at_local(day, WORK_END_HOUR)
    one_hour = timedelta(hours=1)
    busy = sorted(
        (
            e
            for e in events
            if e.user == user and e.status == "confirmed" and local_date(e.start) == day
        ),
        key=lambda e: (e.start, e.id),
    )
    for e in busy:
        if e.start - cursor >= one_hour:
            return cursor
        cursor = max(cursor, e.end)
    return cursor if limit - cursor >= one_hour else None


def when_phrase(asof_day: date, target: date, explicit: bool) -> str:
    """as_of 기준 상대 표현. 가능하지 않거나 `explicit`이면 'M월 D일'."""
    absolute = f"{target.month}월 {target.day}일"
    if explicit:
        return absolute
    if target == asof_day + timedelta(days=1):
        return "내일"
    this_week = week_start(asof_day)
    if week_start(target) == this_week:
        return f"이번 주 {weekday_ko(target)}요일"
    if week_start(target) == this_week + timedelta(days=7):
        return f"다음 주 {weekday_ko(target)}요일"
    return absolute


# ---------------------------------------------------------------- Gold 조립


def _facts(raw: list[tuple[list[str], str]]) -> list[AnswerFact]:
    return [
        AnswerFact(fact_id=f"f{i}", any_of=any_of, description=desc)
        for i, (any_of, desc) in enumerate(raw, start=1)
    ]


def _time_fact(ev: EventRec) -> tuple[list[str], str]:
    return time_forms(ev.start), f"{ev.title} 시작 {hhmm(ev.start)}"


def _date_fact(ev: EventRec) -> tuple[list[str], str]:
    return date_forms(local_date(ev.start)), f"{ev.title} 날짜 {local_date(ev.start).isoformat()}"


def _ref(wm: WorldModel, ev: EventRec) -> EventRef:
    return EventRef(calendar_id=wm.user(ev.user).calendar_id, event_id=ev.id)


def _event_set(wm: WorldModel, events: list[EventRec]) -> EventSetAnswer:
    return EventSetAnswer(events=[_ref(wm, e) for e in events])


def _gold(facts: list[tuple[list[str], str]], expected: ExpectedAnswer) -> Gold:
    return Gold(
        answer_facts=_facts(facts),
        expected_answer=expected,
        must_not_contain=[],
        expected_changes=[],
        required_tool_calls=[],
        forbidden_tools=list(FORBIDDEN_TOOLS),
    )


def _difficulty(
    refs: list[EventRec],
    mails: list[MailRec],
    user: str,
    scope: int,
    cancelled: int,
    extra: dict[str, int | float | str],
) -> dict[str, int | float | str]:
    ids = {mid for e in refs for mid in e.msg_ids}
    mine = [m for m in mails if m.user == user]
    noise = sum(1 for m in mine if m.kind in NOISE_KINDS)
    out: dict[str, int | float | str] = {
        "related_mails": len(ids),
        "schedule_changes": sum(e.changes for e in refs),
        "scope_events": scope,
        "cancelled_in_scope": cancelled,
        "noise_ratio": round(noise / len(mine), 2) if mine else 0.0,
    }
    out.update(extra)
    return out


def ref_tags(refs: list[EventRec]) -> list[str]:
    """정답 근거(일정·메일)를 모두 tags로 남긴다 (ADR-0010 2절)."""
    tags = [f"ref_event:{e.id}" for e in refs]
    seen: list[str] = []
    for e in refs:
        for mid in e.msg_ids:
            if mid not in seen:
                seen.append(mid)
    return tags + [f"ref_message:{m}" for m in seen]


def build_parts(
    spec: TaskSpec,
    events: list[EventRec],
    mails: list[MailRec],
    wm: WorldModel,
    rng: random.Random,
) -> TaskParts | str:
    """질문·정답 조각을 만든다. 버려야 하면 사유 문자열을 돌려준다."""
    return _BUILDERS[spec.kind](spec, events, mails, wm, rng)


def _day_like(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | str:
    target = date.fromisoformat(spec.params["target"])
    half: str | None = spec.params["half"]
    conf, canc = day_scope(events, spec.user, target, half)
    if not conf:
        return EMPTY_SCOPE
    if spec.kind == "cancelled_exclusion" and not canc:
        return NO_CANCELLED
    if half is not None and straddles_noon(events, spec.user, target):
        return NOON_BOUNDARY
    facts: list[tuple[list[str], str]] = []
    for e in conf:
        facts.append(([e.title], f"{local_date(e.start).isoformat()} 일정 제목 {e.title}"))
        facts.append(_time_fact(e))
    scope = when_phrase(local_date(spec.as_of), target, spec.params["explicit"])
    if half:
        scope = f"{scope} {half}"
    templates = (
        texts.CANCEL_QUESTIONS if spec.kind == "cancelled_exclusion" else texts.DAY_QUESTIONS
    )
    question = texts.pick(rng, templates).format(scope=scope)
    refs = [*conf, *canc]
    tags = [
        f"type:{spec.kind}",
        f"date:{target.isoformat()}",
        f"half:{half or 'all'}",
        *ref_tags(refs),
    ]
    return TaskParts(
        question,
        _gold(facts, _event_set(wm, conf)),
        tags,
        _difficulty(refs, mails, spec.user, len(conf), len(canc), {"relative_date": scope}),
    )


def _free_slot(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | str:
    target = date.fromisoformat(spec.params["target"])
    conf, canc = day_scope(events, spec.user, target, None)
    slot = earliest_free(events, spec.user, target)
    if not conf or slot is None:
        return EMPTY_SCOPE
    scope = when_phrase(local_date(spec.as_of), target, spec.params["explicit"])
    question = texts.pick(rng, texts.FREE_QUESTIONS).format(scope=scope)
    facts = [(time_forms(slot), f"가장 이른 1시간 빈 시간 시작 {hhmm(slot)}")]
    refs = [*conf, *canc]
    tags = [f"type:{spec.kind}", f"date:{target.isoformat()}", *ref_tags(refs)]
    return TaskParts(
        question,
        _gold(facts, TimeSlotAnswer(start=slot.astimezone(KST))),
        tags,
        _difficulty(refs, mails, spec.user, len(conf), len(canc), {"relative_date": scope}),
    )


def _reschedule(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | str:
    ev = next((e for e in events if e.id == spec.params["event_id"]), None)
    if ev is None or ev.status != "confirmed" or ev.start <= spec.as_of:
        return NOT_FUTURE
    if sum(1 for e in events if e.user == ev.user and ev.title in e.title) != 1:
        return AMBIGUOUS_TITLE
    facts = [_date_fact(ev), _time_fact(ev)]
    question = texts.pick(rng, texts.RESCHEDULE_QUESTIONS).format(title=ev.title)
    tags = [f"type:{spec.kind}", *ref_tags([ev])]
    expected = EventTimeAnswer(event=_ref(wm, ev), start=ev.start.astimezone(KST))
    return TaskParts(
        question, _gold(facts, expected), tags, _difficulty([ev], mails, spec.user, 1, 0, {})
    )


def _person_next(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | str:
    person = wm.person(spec.params["person"])
    mine = [
        e
        for e in events
        if e.user == spec.user and e.status == "confirmed" and person.key in e.attendees
    ]
    if any(e.start <= spec.as_of < e.end for e in mine):
        return IN_PROGRESS
    upcoming = sorted((e for e in mine if e.start > spec.as_of), key=lambda e: (e.start, e.id))
    if not upcoming:
        return EMPTY_SCOPE
    if len(upcoming) > 1 and upcoming[0].start == upcoming[1].start:
        return AMBIGUOUS_NEXT
    nxt = upcoming[0]
    homonym = wm.is_homonym(person)
    who = f"{person.company}의 {person.name}" if homonym else person.name
    question = texts.pick(rng, texts.PERSON_QUESTIONS).format(who=who)
    facts = [_date_fact(nxt), _time_fact(nxt)]
    tags = [f"type:{spec.kind}", f"attendee:{person.email}", *ref_tags([nxt])]
    return TaskParts(
        question,
        _gold(facts, EventAnswer(event=_ref(wm, nxt))),
        tags,
        _difficulty([nxt], mails, spec.user, len(upcoming), 0, {"homonym": int(homonym)}),
    )


def _project_week(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | str:
    project = next(p for p in wm.projects if p.key == spec.params["project"])
    this_week = spec.params["week"] == "this"
    offset = {"this": 0, "next": 7, "after_next": 14}[spec.params["week"]]
    ws = week_start(local_date(spec.as_of)) + timedelta(days=offset)
    we = ws + timedelta(days=7)
    scoped = [
        e
        for e in events
        if e.user == spec.user and project.name in e.title and ws <= local_date(e.start) < we
    ]
    conf = sorted((e for e in scoped if e.status == "confirmed"), key=lambda e: (e.start, e.id))
    canc = [e for e in scoped if e.status == "cancelled"]
    if not conf:
        return EMPTY_SCOPE
    if this_week and any(e.start < spec.as_of for e in scoped):
        return PAST_IN_WEEK  # 이미 지난 회의가 섞이면 '이번 주 회의'가 모호하다
    facts: list[tuple[list[str], str]] = []
    for e in conf:
        facts += [([e.title], f"프로젝트 회의 제목 {e.title}"), _date_fact(e), _time_fact(e)]
    week = {"this": "이번 주", "next": "다음 주", "after_next": "다다음 주"}[spec.params["week"]]
    question = texts.pick(rng, texts.PROJECT_QUESTIONS).format(week=week, project=project.name)
    refs = [*conf, *canc]
    tags = [
        f"type:{spec.kind}",
        f"project:{project.name}",
        f"week_start:{ws.isoformat()}",
        *ref_tags(refs),
    ]
    return TaskParts(
        question,
        _gold(facts, _event_set(wm, conf)),
        tags,
        _difficulty(refs, mails, spec.user, len(conf), len(canc), {"relative_date": week}),
    )


_BUILDERS = {
    "day_schedule": _day_like,
    "cancelled_exclusion": _day_like,
    "free_slot": _free_slot,
    "reschedule": _reschedule,
    "person_next": _person_next,
    "project_week": _project_week,
}
