"""질문과 정답(Gold) 계산. 정답은 최종 캘린더(`EventRec` 목록)에서 코드로 계산한다 (ADR-0009 2절).

일정 조회 유형(읽기 전용) 6종:
- day_schedule: 특정 날짜(와 오전/오후)의 일정
- cancelled_exclusion: 위와 같지만 범위 안에 취소된 일정이 있어 `must_not_contain`이 생긴다
- free_slot: 업무 시간(09~18시 KST) 안에서 가장 이른 1시간 빈 시간
- reschedule: 메일로 시간이 바뀐 회의의 현재(캘린더 최신) 날짜·시각
- person_next: 특정 인물과의 다음 회의
- project_week: 이번/다음 주 특정 프로젝트 회의 전부

정답 계산이 불가능하거나 모호하면 `None`을 돌려주고 호출자가 그 명세를 버린다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from orbit.connectors.contracts import CALENDAR_TOOLS, MAIL_TOOLS
from orbit.evaldata import texts
from orbit.evaldata.records import NOISE_KINDS, EventRec, MailRec, TaskSpec
from orbit.evaldata.schema import AnswerFact, Gold, RequiredCall
from orbit.evaldata.timeutil import (
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
FORBIDDEN_TOOLS = sorted((CALENDAR_TOOLS | MAIL_TOOLS) - READ_TOOLS)
WORK_START_HOUR = 9
WORK_END_HOUR = 18


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
    key = lambda e: (e.start, e.id)  # noqa: E731
    return (
        sorted((e for e in scoped if e.status == "confirmed"), key=key),
        sorted((e for e in scoped if e.status == "cancelled"), key=key),
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


def _gold(
    facts: list[tuple[list[str], str]], must_not: list[str], tool: str = "list_events"
) -> Gold:
    return Gold(
        answer_facts=_facts(facts),
        must_not_contain=must_not,
        expected_changes=[],
        required_tool_calls=[RequiredCall(tool=tool, args_subset={})],
        forbidden_tools=list(FORBIDDEN_TOOLS),
    )


def _safe_must_not(
    cancelled: list[EventRec], events: list[EventRec], facts: list[tuple[list[str], str]], user: str
) -> list[str] | None:
    """취소된 일정 제목이 유효 일정 제목이나 정답 표현에 포함되면 오탐이 생기므로 명세를 버린다."""
    titles = [e.title for e in cancelled]
    live = [e.title for e in events if e.user == user and e.status == "confirmed"]
    answers = [form for any_of, _ in facts for form in any_of]
    for t in titles:
        if any(t in other for other in live) or any(t in form for form in answers):
            return None
    return titles


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


def _ref_tags(refs: list[EventRec]) -> list[str]:
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
) -> TaskParts | None:
    builder = _BUILDERS[spec.kind]
    return builder(spec, events, mails, wm, rng)


def _day_like(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | None:
    target = date.fromisoformat(spec.params["target"])
    half: str | None = spec.params["half"]
    conf, canc = day_scope(events, spec.user, target, half)
    if not conf:
        return None
    if spec.kind == "cancelled_exclusion" and not canc:
        return None
    facts: list[tuple[list[str], str]] = []
    for e in conf:
        facts.append(([e.title], f"{local_date(e.start).isoformat()} 일정 제목 {e.title}"))
        facts.append(_time_fact(e))
    must_not = _safe_must_not(canc, events, facts, spec.user)
    if must_not is None:
        return None
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
        *_ref_tags(refs),
    ]
    return TaskParts(
        question,
        _gold(facts, must_not),
        tags,
        _difficulty(refs, mails, spec.user, len(conf), len(canc), {"relative_date": scope}),
    )


def _free_slot(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | None:
    target = date.fromisoformat(spec.params["target"])
    conf, canc = day_scope(events, spec.user, target, None)
    slot = earliest_free(events, spec.user, target)
    if not conf or slot is None:
        return None
    scope = when_phrase(local_date(spec.as_of), target, spec.params["explicit"])
    question = texts.pick(rng, texts.FREE_QUESTIONS).format(scope=scope)
    facts = [(time_forms(slot), f"가장 이른 1시간 빈 시간 시작 {hhmm(slot)}")]
    refs = [*conf, *canc]
    tags = [f"type:{spec.kind}", f"date:{target.isoformat()}", *_ref_tags(refs)]
    return TaskParts(
        question,
        _gold(facts, []),
        tags,
        _difficulty(refs, mails, spec.user, len(conf), len(canc), {"relative_date": scope}),
    )


def _reschedule(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | None:
    ev = next((e for e in events if e.id == spec.params["event_id"]), None)
    if ev is None or ev.status != "confirmed" or ev.start <= spec.as_of:
        return None
    if sum(1 for e in events if e.user == ev.user and ev.title in e.title) != 1:
        return None  # 제목이 겹치면 질문이 모호하다
    facts = [_date_fact(ev), _time_fact(ev)]
    question = texts.pick(rng, texts.RESCHEDULE_QUESTIONS).format(title=ev.title)
    tags = [f"type:{spec.kind}", *_ref_tags([ev])]
    return TaskParts(
        question, _gold(facts, []), tags, _difficulty([ev], mails, spec.user, 1, 0, {})
    )


def _person_next(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | None:
    person = wm.person(spec.params["person"])
    upcoming = sorted(
        (
            e
            for e in events
            if e.user == spec.user
            and e.status == "confirmed"
            and person.key in e.attendees
            and e.start >= spec.as_of
        ),
        key=lambda e: (e.start, e.id),
    )
    if not upcoming or (len(upcoming) > 1 and upcoming[0].start == upcoming[1].start):
        return None
    nxt = upcoming[0]
    homonym = wm.is_homonym(person)
    who = f"{person.company}의 {person.name}" if homonym else person.name
    question = texts.pick(rng, texts.PERSON_QUESTIONS).format(who=who)
    facts = [_date_fact(nxt), _time_fact(nxt)]
    tags = [f"type:{spec.kind}", f"attendee:{person.email}", *_ref_tags([nxt])]
    return TaskParts(
        question,
        _gold(facts, []),
        tags,
        _difficulty([nxt], mails, spec.user, len(upcoming), 0, {"homonym": int(homonym)}),
    )


def _project_week(
    spec: TaskSpec, events: list[EventRec], mails: list[MailRec], wm: WorldModel, rng: random.Random
) -> TaskParts | None:
    project = next(p for p in wm.projects if p.key == spec.params["project"])
    this_week = spec.params["week"] == "this"
    ws = week_start(local_date(spec.as_of)) + timedelta(days=0 if this_week else 7)
    we = ws + timedelta(days=7)
    scoped = [
        e
        for e in events
        if e.user == spec.user and project.name in e.title and ws <= local_date(e.start) < we
    ]
    conf = sorted((e for e in scoped if e.status == "confirmed"), key=lambda e: (e.start, e.id))
    canc = [e for e in scoped if e.status == "cancelled"]
    if not conf:
        return None
    if this_week and any(e.start < spec.as_of for e in scoped):
        return None  # 이미 지난 회의가 섞이면 '이번 주 회의'가 모호하다
    facts: list[tuple[list[str], str]] = []
    for e in conf:
        facts += [([e.title], f"프로젝트 회의 제목 {e.title}"), _date_fact(e), _time_fact(e)]
    must_not = _safe_must_not(canc, events, facts, spec.user)
    if must_not is None:
        return None
    week = "이번 주" if this_week else "다음 주"
    question = texts.pick(rng, texts.PROJECT_QUESTIONS).format(week=week, project=project.name)
    refs = [*conf, *canc]
    tags = [
        f"type:{spec.kind}",
        f"project:{project.name}",
        f"week_start:{ws.isoformat()}",
        *_ref_tags(refs),
    ]
    return TaskParts(
        question,
        _gold(facts, must_not),
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
