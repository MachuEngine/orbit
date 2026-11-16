"""시나리오 기록(`truth`)에서 질문·정답을 계산하는 빌더 (ADR-0026 §4).

정답은 코드가 세계(`TaskWorld`)에서 다시 계산한다.
truth의 참조가 세계에 없거나 재계산 결과가 truth와
다르면 `TaskGenError`로 생성을 실패시킨다. 정답이 비었거나 질문이 모호해지는 시나리오는 버릴
사유(문자열)를 돌려준다(호출자가 다른 시나리오로 대체한다).

한 시나리오가 여러 형태(`form`)의 과제가 될 수 있다. 예: person_next는 단일 턴(`single`)과 다중 턴
(`mt`), deadline은 리마인더 요청(`request`)과 마감 질문(`ask`)이다.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from orbit.evaldata.schema import (
    AbstainAnswer,
    AnswerFact,
    ClarifyAnswer,
    DocumentAnswer,
    DocumentRef,
    DraftSentChange,
    EventAnswer,
    EventCancelledChange,
    EventCreatedChange,
    EventFieldChanges,
    EventRef,
    EventSetAnswer,
    EventTimeAnswer,
    EventUpdatedChange,
    ExpectedAnswer,
    ExpectedChange,
    Gold,
    MessageRef,
    MessageSetAnswer,
    ReadingAnswer,
    ReminderCreatedChange,
    TimeSlotAnswer,
)
from orbit.evaldata.v1 import task_templates as tt
from orbit.evaldata.v1.facts import conflicts, fact_in, norm
from orbit.evaldata.v1.skeleton import (
    AbsentTruth,
    AllDayTruth,
    AmbiguousTruth,
    ArtifactRef,
    CancellationTruth,
    CrossMidnightTruth,
    DayAgendaTruth,
    DeadlineTruth,
    DocFactTruth,
    DocVersionTruth,
    EventChangeTargetTruth,
    Fact,
    FreeSlotTruth,
    MailFactTruth,
    MeetingRequestTruth,
    MismatchTruth,
    PersonNextTruth,
    ProjectWeekTruth,
    ReplyNeededTruth,
    RescheduleTruth,
    ScenarioRecord,
    TzEventTruth,
)
from orbit.evaldata.v1.task_world import AccountView, TaskWorld

# 과제 유형(ADR-0026 §4): 시나리오 유형 -> 유형 이름
KIND_OF_SCENARIO: dict[str, str] = {
    "day_agenda": "calendar",
    "free_slot": "calendar",
    "reschedule": "calendar",
    "cancellation": "calendar",
    "project_week": "calendar",
    "person_next": "calendar",
    "cross_midnight": "calendar",
    "tz_event": "calendar",
    "all_day": "calendar",
    "mail_fact": "mail",
    "reply_needed": "write",
    "meeting_request": "write",
    "event_change_target": "write",
    "doc_fact": "document",
    "doc_version": "document",
    "deadline": "reminder",
    "mismatch": "mismatch",
    "absent": "unanswerable",
    "ambiguous": "ambiguous",
}
MAX_FACT_EVENTS = 10


class TaskGenError(RuntimeError):
    """세계에서 다시 계산한 값이 시나리오 기록과 다르다. 생성을 실패시킨다."""


@dataclass
class Draft:
    """과제 한 건의 설계도. `tasks.py`가 분할·ID를 붙여 `Task`로 바꾼다."""

    scenario: ScenarioRecord
    kind: str  # calendar, mail, write, multi_turn, document, reminder, mismatch, ...
    form: str  # single, mt, request, ask ...
    turns: list[str]
    gold: Gold
    refs: tuple[str, ...]  # ref_event:·ref_message:·ref_file: 태그(근거와 정답 계산에 쓴 아티팩트)
    keep: tuple[str, ...] = ()  # 표현 변형이 지켜야 할 고유 표현
    difficulty: dict[str, int | float | str] = field(default_factory=dict)
    approval_policy: list[str] | None = None
    fingerprint_extra: Any = None  # 정답 지문에 더할 값(거절 과제가 원래 바꾸려던 내용 등)
    fault_tool: str | None = None  # 이 과제에 장애를 걸 커넥터 도구 이름
    note: str = ""  # 생성 중 알게 된 한계(매니페스트 집계용 코드)
    variant: str | None = None  # 시나리오 유형 안의 갈래(absent·ambiguous·event_change_target)

    @property
    def paraphrasable(self) -> bool:
        return (
            self.form in ("single", "request", "ask")
            and len(self.turns) == 1
            and self.kind != "ambiguous"
        )

    @property
    def writes(self) -> bool:
        return bool(self.gold.expected_changes)


@dataclass(frozen=True)
class Ctx:
    tw: TaskWorld
    seed: int

    def rng(self, sc: ScenarioRecord, form: str) -> random.Random:
        return random.Random(f"{self.seed}:{sc.scenario_id}:{form}")  # noqa: S311  재현성용

    def acct(self, sc: ScenarioRecord) -> AccountView:
        return self.tw.account(sc.account_id)


Result = Draft | str  # str은 버릴 사유


# ---------------------------------------------------------------- 공용 도구


def _tag(ref: ArtifactRef) -> str:
    prefix = {"event": "ref_event", "message": "ref_message", "file": "ref_file"}[ref.kind]
    return f"{prefix}:{ref.id}"


def ref_tags(refs: Iterable[ArtifactRef], extra_events: Iterable[Any] = ()) -> tuple[str, ...]:
    tags = {_tag(r) for r in refs}
    tags |= {f"ref_event:{e.id}" for e in extra_events}
    return tuple(sorted(tags))


def _ev_ref(e: Any) -> EventRef:
    return EventRef(calendar_id=e.calendar_id, event_id=e.id)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _fact(fact_id: str, any_of: list[str], description: str) -> AnswerFact:
    return AnswerFact(fact_id=fact_id, any_of=_dedupe(any_of), description=description)


def _event_facts(events: list[Any], acct: AccountView) -> list[AnswerFact]:
    tz = acct.tz
    facts: list[AnswerFact] = []
    for i, e in enumerate(events[:MAX_FACT_EVENTS], start=1):
        facts.append(_fact(f"title{i}", [e.title], "일정 제목"))
        if not e.all_day:
            facts.append(_fact(f"time{i}", tt.local_time_forms(e.start, tz), "일정 시작 시각"))
    return facts


def _need(cond: bool, message: str) -> None:
    if not cond:
        raise TaskGenError(message)


def _check_event_ids(
    sc: ScenarioRecord, label: str, got: Iterable[Any], want: Iterable[ArtifactRef]
) -> None:
    got_ids = sorted(e.id for e in got)
    want_ids = sorted(r.id for r in want)
    _need(got_ids == want_ids, f"{sc.scenario_id}: {label}이 세계에서 다시 계산한 값과 다르다")


def _gold(
    expected: ExpectedAnswer | None = None,
    *,
    facts: list[AnswerFact] | None = None,
    changes: list[ExpectedChange] | None = None,
    must_not: list[str] | None = None,
    facts_required: bool = False,
) -> Gold:
    return Gold(
        answer_facts=facts or [],
        expected_answer=expected,
        must_not_contain=must_not or [],
        expected_changes=[] if changes is None else changes,
        answer_facts_required=facts_required,
    )


def _asof_day(sc: ScenarioRecord, acct: AccountView) -> Any:
    return sc.as_of.astimezone(acct.tz).date()


def _when(rng: random.Random, sc: ScenarioRecord, acct: AccountView, target: Any) -> str:
    """상대 표현을 쓰면 되돌려 계산해 날짜가 맞는지 확인한다."""
    phrase = tt.pick_date_phrase(rng, target, _asof_day(sc, acct), relative=rng.random() < 0.6)
    _need(
        tt.resolve_date_phrase(phrase, _asof_day(sc, acct)) == target,
        f"{sc.scenario_id}: 날짜 표현 {phrase!r}이 날짜로 되돌려지지 않는다",
    )
    return phrase


def _forms_conflict(forms: list[str], others: list[str]) -> bool:
    """`forms` 중 하나가 `others` 중 하나에 (정규화한) 부분 문자열로 들어가거나 그 반대다."""
    return any(norm(a) in norm(b) or norm(b) in norm(a) for a in forms for b in others)


def trimmed_previous(previous: Fact, latest: Fact) -> tuple[list[str], int]:
    """`must_not_contain`에 쓸 이전 값 표기. 최신 값 표기와 부분 문자열로 겹치는 표기는 뺀다.

    (표기 목록, 뺀 표기 수)를 돌려준다.
    """
    keep: list[str] = []
    dropped = 0
    for form in previous.surface_forms:
        probe = Fact(fact_id="p", kind=previous.kind, value=previous.value, surface_forms=[form])
        if conflicts(probe, latest):
            dropped += 1
        else:
            keep.append(form)
    return keep, dropped


# ---------------------------------------------------------------- 캘린더


def build_day_agenda(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, DayAgendaTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    got = acct.events_on_day(t.day)
    _check_event_ids(sc, "day_agenda 일정", got, t.events)
    cancelled = acct.events_on_day(t.day, cancelled=True)
    _need(
        sorted(e.id for e in cancelled) == sorted(r.id for r in t.cancelled),
        f"{sc.scenario_id}: day_agenda 취소 일정이 세계에서 다시 계산한 값과 다르다",
    )
    if not got:
        return "정답 일정이 비었다"
    rng = ctx.rng(sc, form)
    when = _when(rng, sc, acct, t.day)
    refs = ref_tags([*t.events, *t.cancelled], got)
    diff: dict[str, int | float | str] = {
        "scope_events": len(got),
        "cancelled_in_scope": len(cancelled),
    }
    if form == "mt":
        return _mt_day_agenda(sc, acct, rng, when, got, refs, diff)
    expected = EventSetAnswer(events=[_ev_ref(e) for e in got])
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.DAY_AGENDA).format(when=when)],
        _gold(expected, facts=_event_facts(got, acct)),
        refs,
        keep=(when,),
        difficulty=diff,
        fault_tool="list_events",
    )


def _mt_day_agenda(
    sc: ScenarioRecord,
    acct: AccountView,
    rng: random.Random,
    when: str,
    got: list[Any],
    refs: tuple[str, ...],
    diff: dict[str, int | float | str],
) -> Result:
    day = tt.resolve_date_phrase(when, _asof_day(sc, acct))
    if day is None:
        raise TaskGenError(f"{sc.scenario_id}: 날짜 표현을 해석할 수 없다")
    noon = acct.local_day_bounds(day)[0] + timedelta(hours=12)
    timed = [e for e in got if not e.all_day]
    if any(e.start < noon < e.end for e in timed):
        return "정오에 걸친 일정이 있어 오후 기준이 모호하다"
    afternoon = [e for e in timed if e.start >= noon]
    if not afternoon or len(afternoon) == len(got):
        return "오후 일정만 고르면 전체와 같거나 비어 있다"
    expected = EventSetAnswer(events=[_ev_ref(e) for e in afternoon])
    return Draft(
        sc,
        "multi_turn",
        "mt",
        [tt.pick(rng, tt.DAY_AGENDA).format(when=when), tt.pick(rng, tt.MT_DAY_FOLLOWUP)],
        _gold(expected, facts=_event_facts(afternoon, acct)),
        refs,
        keep=(when,),
        difficulty={**diff, "turns": 2, "afternoon_events": len(afternoon)},
    )


def build_free_slot(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, FreeSlotTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    busy = acct.events_on_day(t.day)
    _check_event_ids(sc, "free_slot 일정", busy, t.busy)
    if any(e.all_day for e in busy):
        return "종일 일정이 있어 빈 시간 기준이 모호하다"
    need = timedelta(minutes=t.min_minutes)
    cursor, free = t.work.start, []
    for e in busy:
        if e.start - cursor >= need:
            free.append((cursor, e.start))
        cursor = max(cursor, e.end)
    if t.work.end - cursor >= need:
        free.append((cursor, t.work.end))
    _need(
        free == [(f.start, f.end) for f in t.free_slots] and bool(free),
        f"{sc.scenario_id}: free_slot의 빈 시간이 세계에서 다시 계산한 값과 다르다",
    )
    rng = ctx.rng(sc, form)
    when = _when(rng, sc, acct, t.day)
    work = f"{tt.korean_clock(t.work.start, acct.tz)}~{tt.korean_clock(t.work.end, acct.tz)}"
    dur = tt.korean_duration(t.min_minutes)
    past = t.day < _asof_day(sc, acct)
    templates = tt.FREE_SLOT_PAST if past else tt.FREE_SLOT_FUTURE
    start = free[0][0]
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, templates).format(when=when, work=work, dur=dur)],
        _gold(
            TimeSlotAnswer(start=start),
            facts=[_fact("start", tt.local_time_forms(start, acct.tz), "가장 이른 빈 시간 시작")],
        ),
        ref_tags(t.busy, busy),
        keep=(when, dur),
        difficulty={"scope_events": len(busy), "free_slots": len(free)},
        fault_tool="list_events",
    )


def build_reschedule(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, RescheduleTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    ev = acct.event_of(t.event)
    _need(
        ev.status == "confirmed" and ev.start == t.new.start and ev.end == t.new.end,
        f"{sc.scenario_id}: reschedule 일정의 시각이 truth와 다르다",
    )
    acct.message_of(t.change_message)
    old_day = t.old.start.astimezone(acct.tz).date()
    if acct.same_title_on_day(ev.title, old_day):
        return "원래 날짜에 같은 제목의 일정이 있어 질문이 모호하다"
    rng = ctx.rng(sc, form)
    old = tt.explicit_date(old_day)
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.RESCHEDULE).format(old=old, title=ev.title)],
        _gold(
            EventTimeAnswer(event=_ev_ref(ev), start=ev.start),
            facts=[_fact("start", tt.local_time_forms(ev.start, acct.tz), "변경된 시작 시각")],
        ),
        ref_tags([t.event, t.change_message]),
        keep=(old, ev.title),
        fault_tool="list_events",
    )


def build_cancellation(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, CancellationTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    ev = acct.event_of(t.event)
    _need(ev.status == "cancelled", f"{sc.scenario_id}: 취소 일정이 세계에서 취소 상태가 아니다")
    acct.message_of(t.cancel_message)
    day = t.original.start.astimezone(acct.tz).date()
    got = acct.events_on_day(day)
    _need(ev.id not in {e.id for e in got}, f"{sc.scenario_id}: 취소 일정이 목록에 들어 있다")
    if not got:
        return "취소를 빼면 그날 일정이 없다"
    rng = ctx.rng(sc, form)
    when = _when(rng, sc, acct, day)
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.CANCELLATION).format(when=when)],
        _gold(EventSetAnswer(events=[_ev_ref(e) for e in got]), facts=_event_facts(got, acct)),
        ref_tags([t.event, t.cancel_message], got),
        keep=(when,),
        difficulty={"scope_events": len(got), "cancelled_in_scope": 1},
        fault_tool="list_events",
    )


def build_project_week(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, ProjectWeekTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    end = t.week_start + timedelta(days=7)
    got = [
        e
        for e in acct.events
        if norm(t.project) in norm(e.title)
        and t.week_start <= e.start.astimezone(acct.tz).date() < end
        and e.status == "confirmed"
    ]
    got.sort(key=lambda e: (e.start, e.id))
    _check_event_ids(sc, "project_week 일정", got, t.events)
    if not got:
        return "정답 일정이 비었다"
    rng = ctx.rng(sc, form)
    week = tt.week_phrase(t.week_start, _asof_day(sc, acct))
    _need(
        tt.resolve_week_phrase(week, _asof_day(sc, acct)) == t.week_start,
        f"{sc.scenario_id}: 주 표현 {week!r}이 날짜로 되돌려지지 않는다",
    )
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.PROJECT_WEEK).format(week=week, project=t.project)],
        _gold(EventSetAnswer(events=[_ev_ref(e) for e in got]), facts=_event_facts(got, acct)),
        ref_tags([*t.events, *t.cancelled], got),
        keep=(week, t.project),
        difficulty={"scope_events": len(got), "cancelled_in_scope": len(t.cancelled)},
        fault_tool="list_events",
    )


def build_person_next(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, PersonNextTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    upcoming = acct.upcoming_with(t.person_email, t.as_of)
    _check_event_ids(sc, "person_next 일정", upcoming, t.upcoming)
    nxt = acct.event_of(t.next_event)
    _need(upcoming[0].id == nxt.id, f"{sc.scenario_id}: person_next 다음 일정이 truth와 다르다")
    if any(
        e.status == "confirmed"
        and e.start <= t.as_of < e.end
        and any(a.email.casefold() == t.person_email.casefold() for a in e.attendees)
        for e in acct.events
    ):
        return "as_of에 그 사람과의 미팅이 진행 중이다"
    if len(upcoming) > 1 and upcoming[0].start == upcoming[1].start:
        return "다음 미팅이 동시에 둘 이상이다"
    refs = ref_tags(t.upcoming)
    rng = ctx.rng(sc, form)
    if form == "mt":
        return _mt_person_next(sc, acct, rng, t, nxt, refs)
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.PERSON_NEXT).format(name=t.person_name)],
        _gold(
            EventAnswer(event=_ev_ref(nxt)),
            facts=[
                _fact("date", tt.local_date_forms(nxt.start.astimezone(acct.tz).date()), "날짜"),
                _fact("time", tt.local_time_forms(nxt.start, acct.tz), "시작 시각"),
            ],
        ),
        refs,
        keep=(t.person_name,),
        difficulty={"upcoming_meetings": len(upcoming)},
        fault_tool="list_events",
    )


def _mt_person_next(
    sc: ScenarioRecord,
    acct: AccountView,
    rng: random.Random,
    t: PersonNextTruth,
    nxt: Any,
    refs: tuple[str, ...],
) -> Result:
    shift = timedelta(minutes=30)
    turns = [tt.pick(rng, tt.MT_PERSON_FIRST).format(name=t.person_name)]
    if rng.random() < 0.5:
        turns.append(tt.pick(rng, tt.MT_PERSON_MIDDLE))
    turns.append(tt.pick(rng, tt.MT_PERSON_MOVE))
    change = EventUpdatedChange(
        calendar_id=nxt.calendar_id,
        event_id=nxt.id,
        fields=EventFieldChanges(start=nxt.start + shift, end=nxt.end + shift),
    )
    return Draft(
        sc,
        "multi_turn",
        "mt",
        turns,
        _gold(None, changes=[change]),
        refs,
        keep=(t.person_name,),
        difficulty={"turns": len(turns), "upcoming_meetings": len(t.upcoming)},
        fault_tool="update_event",
        variant="person_next",
    )


def build_cross_midnight(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, CrossMidnightTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    ev = acct.event_of(t.event)
    tz = acct.tz
    crossing = [
        e
        for e in acct.events
        if e.status == "confirmed"
        and not e.all_day
        and e.start.astimezone(tz).date() == t.start_day
        and (e.end.astimezone(tz) - timedelta(microseconds=1)).date() > t.start_day
    ]
    _need(
        [e.id for e in crossing] == [ev.id],
        f"{sc.scenario_id}: 자정을 넘기는 일정이 세계에서 다시 계산한 값과 다르다",
    )
    rng = ctx.rng(sc, form)
    day = tt.explicit_date(t.start_day)
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.CROSS_MIDNIGHT).format(date=day)],
        _gold(
            EventAnswer(event=_ev_ref(ev)),
            facts=[_fact("title", [ev.title], "일정 제목")],
        ),
        ref_tags([t.event]),
        keep=(day,),
        fault_tool="list_events",
    )


def build_tz_event(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, TzEventTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    ev = acct.event_of(t.event)
    _need(
        ev.start == t.span.start and ev.status == "confirmed",
        f"{sc.scenario_id}: tz_event 일정의 시각이 truth와 다르다",
    )
    day = ev.start.astimezone(acct.tz).date()
    if len(acct.same_title_on_day(ev.title, day)) != 1:
        return "같은 날 같은 제목의 일정이 둘 이상이다"
    rng = ctx.rng(sc, form)
    date_text = tt.explicit_date(day)
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.TZ_EVENT).format(date=date_text, title=ev.title)],
        _gold(
            EventTimeAnswer(event=_ev_ref(ev), start=ev.start),
            facts=[_fact("start", tt.local_time_forms(ev.start, acct.tz), "시작 시각")],
        ),
        ref_tags([t.event]),
        keep=(date_text, ev.title),
        difficulty={"utc_offset_hours": t.user_utc_offset_hours},
        fault_tool="list_events",
        variant=t.variant,
    )


def build_all_day(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, AllDayTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    ev = acct.event_of(t.event)
    _need(
        ev.all_day
        and ev.status == "confirmed"
        and ev.start == t.span.start
        and ev.end == t.span.end
        and ev.start.astimezone(acct.tz).date() == t.intended_date,
        f"{sc.scenario_id}: 종일 일정이 truth와 다르다",
    )
    got = acct.events_on_day(t.intended_date)
    _need(ev.id in {e.id for e in got}, f"{sc.scenario_id}: 종일 일정이 그날 목록에 없다")
    rng = ctx.rng(sc, form)
    when = _when(rng, sc, acct, t.intended_date)
    return Draft(
        sc,
        "calendar",
        form,
        [tt.pick(rng, tt.ALL_DAY).format(when=when)],
        _gold(EventSetAnswer(events=[_ev_ref(e) for e in got]), facts=_event_facts(got, acct)),
        ref_tags([t.event], got),
        keep=(when,),
        difficulty={"scope_events": len(got), "all_day_events": 1},
        fault_tool="list_events",
    )


# ---------------------------------------------------------------- 메일


def _message_text(acct: AccountView, ref: ArtifactRef) -> str:
    return str(acct.message_of(ref).get("body_text", ""))


def build_mail_fact(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, MailFactTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    latest = acct.message_of(t.latest_message)
    acct.message_of(t.previous_message)
    if not fact_in(_message_text(acct, t.latest_message), t.latest):
        return "최신 메일 본문에 최신 값이 없다"
    must_not, trimmed = trimmed_previous(t.previous, t.latest)
    rng = ctx.rng(sc, form)
    refs = ref_tags([t.latest_message, t.previous_message])
    diff: dict[str, int | float | str] = {
        "fact_kind": t.fact_kind,
        "same_thread": int(t.same_thread),
    }
    note = "must_not_trimmed" if trimmed else ""
    first = tt.pick(rng, tt.MAIL_FACT[t.fact_kind]).format(project=t.project, item=t.item)
    gold_facts = [_fact("latest", t.latest.surface_forms, "최신 값")]
    if form == "mt":
        if t.fact_kind == "attachment":
            return "첨부 파일 이름은 답장에 쓰기 어색해 다중 턴에서 뺀다"
        sender = str(latest["from"]["email"])
        turns = [first]
        if rng.random() < 0.5:
            turns.append(tt.pick(rng, tt.MT_MAIL_MIDDLE))
        turns.append(tt.pick(rng, tt.MT_MAIL_REPLY[t.fact_kind]))
        change = DraftSentChange(
            to_include=[sender],
            reply_to_message_id=t.latest_message.id,
            body_facts=gold_facts,
            body_must_not_contain=must_not,
        )
        return Draft(
            sc,
            "multi_turn",
            "mt",
            turns,
            _gold(None, changes=[change]),
            refs,
            keep=(t.project, t.item),
            difficulty={**diff, "turns": len(turns)},
            fault_tool="send_draft",
            note=note,
            variant="mail_fact",
        )
    return Draft(
        sc,
        "mail",
        form,
        [first],
        _gold(
            MessageSetAnswer(messages=[MessageRef(message_id=t.latest_message.id)]),
            facts=gold_facts,
            must_not=must_not,
            facts_required=True,
        ),
        refs,
        keep=(t.project, t.item),
        difficulty=diff,
        fault_tool="search_messages",
        note=note,
        variant=t.fact_kind,
    )


def build_mismatch(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, MismatchTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    ev = acct.event_of(t.event)
    _need(
        ev.status == "confirmed" and ev.start == t.calendar_span.start,
        f"{sc.scenario_id}: mismatch 캘린더 일정의 시각이 truth와 다르다",
    )
    acct.message_of(t.message)
    if "time" not in t.fields:
        return "시각이 다르지 않아 시각 질문으로 불일치를 잴 수 없다"
    tz = acct.tz
    day = ev.start.astimezone(tz).date()
    if [e.id for e in acct.same_title_on_day(ev.title, day)] != [ev.id]:
        return "같은 날 같은 제목의 일정이 둘 이상이다"
    cal_forms = tt.local_time_forms(ev.start, tz)
    mail_forms = [
        f for f in tt.local_time_forms(t.mail_start, tz) if not _forms_conflict([f], cal_forms)
    ]
    mail_day = t.mail_start.astimezone(tz).date()
    if mail_day != day:
        mail_forms += [
            f
            for f in tt.local_date_forms(mail_day)
            if not _forms_conflict([f], tt.local_date_forms(day))
        ]
    if not mail_forms:
        return "메일 값의 표기가 캘린더 값과 구분되지 않는다"
    rng = ctx.rng(sc, form)
    date_text = tt.explicit_date(day)
    return Draft(
        sc,
        "mismatch",
        form,
        [tt.pick(rng, tt.MISMATCH).format(date=date_text, title=ev.title)],
        _gold(
            EventTimeAnswer(event=_ev_ref(ev), start=ev.start),
            facts=[_fact("start", cal_forms, "캘린더 시작 시각")],
            must_not=mail_forms,
        ),
        ref_tags([t.event, t.message]),
        keep=(date_text, ev.title),
        difficulty={"mismatch_fields": "+".join(t.fields)},
        fault_tool="list_events",
    )


# ---------------------------------------------------------------- 쓰기


def build_meeting_request(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, MeetingRequestTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    message = acct.message_of(t.message)
    _need(
        str(message["from"]["email"]).casefold() == t.requester_email.casefold(),
        f"{sc.scenario_id}: 요청 메일의 발신자가 truth와 다르다",
    )
    if t.superseded_message is not None:
        acct.message_of(t.superseded_message)
    if t.proposed.start <= sc.as_of:
        return "제안 시각이 as_of 이전이다"
    rng = ctx.rng(sc, form)
    proposal = "가장 최근에 바꿔 제안한 시간으로" if t.revised else "제안한 시간으로"
    change = EventCreatedChange(
        calendar_id=acct.primary_calendar_id,
        start=t.proposed.start,
        end=t.proposed.end,
        title_any_of=[t.title],
        attendees_include=list(t.attendee_emails),
        location_any_of=[t.location] if t.location else None,
    )
    refs = [t.message] + ([t.superseded_message] if t.superseded_message else [])
    return Draft(
        sc,
        "write",
        form,
        [
            tt.pick(rng, tt.MEETING_REQUEST).format(
                requester=t.requester_name, title=t.title, proposal=proposal
            )
        ],
        _gold(None, changes=[change]),
        ref_tags(refs),
        keep=(t.requester_name, t.title),
        difficulty={"revised": int(t.revised), "attendees": len(t.attendee_emails)},
        fault_tool="create_event",
        variant="meeting_request",
    )


def build_event_change(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, EventChangeTargetTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    ev = acct.event_of(t.event)
    request = acct.message_of(t.request_message)
    _need(
        ev.status == "confirmed" and ev.start == t.current.start and ev.end == t.current.end,
        f"{sc.scenario_id}: 변경 대상 일정의 시각이 truth와 다르다",
    )
    day = ev.start.astimezone(acct.tz).date()
    if [e.id for e in acct.same_title_on_day(ev.title, day)] != [ev.id]:
        return "같은 날 같은 제목의 일정이 둘 이상이다"
    if ev.start <= sc.as_of:
        return "변경 대상 일정이 as_of 이전이다"
    requester = str(request["from"].get("name") or "")
    if not requester:
        return "요청자 이름이 없다"
    rng = ctx.rng(sc, form)
    date_text = tt.explicit_date(day)
    change: ExpectedChange
    if t.action == "move":
        _need(t.new is not None, f"{sc.scenario_id}: 이동 요청에 새 시각이 없다")
        if t.new is None:
            raise TaskGenError(f"{sc.scenario_id}: 이동할 새 시각이 없다")
        change = EventUpdatedChange(
            calendar_id=ev.calendar_id,
            event_id=ev.id,
            fields=EventFieldChanges(start=t.new.start, end=t.new.end),
        )
        templates = tt.EVENT_MOVE
        tool = "update_event"
    else:
        change = EventCancelledChange(calendar_id=ev.calendar_id, event_id=ev.id)
        templates = tt.EVENT_CANCEL
        tool = "cancel_event"
    return Draft(
        sc,
        "write",
        form,
        [tt.pick(rng, templates).format(requester=requester, date=date_text, title=ev.title)],
        _gold(None, changes=[change]),
        ref_tags([t.event, t.request_message]),
        keep=(requester, date_text, ev.title),
        difficulty={"action": t.action},
        fault_tool=tool,
        variant=t.action,
    )


def build_reply_needed(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, ReplyNeededTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    message = acct.message_of(t.awaiting_message)
    _need(
        str(message["from"]["email"]).casefold() == t.sender_email.casefold(),
        f"{sc.scenario_id}: 답장 대기 메일의 발신자가 truth와 다르다",
    )
    subject = t.subject.removeprefix("Re: ").strip()
    rng = ctx.rng(sc, form)
    change = DraftSentChange(to_include=[t.sender_email], reply_to_message_id=t.awaiting_message.id)
    return Draft(
        sc,
        "write",
        form,
        [tt.pick(rng, tt.REPLY_NEEDED).format(sender=t.sender_name, subject=subject)],
        _gold(None, changes=[change]),
        ref_tags([t.awaiting_message]),
        keep=(t.sender_name, subject),
        fault_tool="send_draft",
        variant="reply_needed",
    )


# ---------------------------------------------------------------- 문서


def _file_text(acct: AccountView, ref: ArtifactRef) -> str:
    return str(acct.file_of(ref).get("text", ""))


def build_doc_fact(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, DocFactTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    file = acct.file_of(t.file)
    if file.get("trashed"):
        return "휴지통의 문서다"
    same_name = [f for f in acct.files.values() if f["name"] == file["name"]]
    if len(same_name) != 1:
        return "같은 이름의 문서가 둘 이상이다"
    if not fact_in(_file_text(acct, t.file), t.fact):
        return "문서 본문에 정답 값이 없다"
    key = "date" if t.fact.kind in ("deadline", "date", "datetime", "time") else t.fact.kind
    if key not in tt.DOC_FACT:
        return "문서 질문 템플릿이 없는 사실 종류다"
    rng = ctx.rng(sc, form)
    title = str(file["name"])
    return Draft(
        sc,
        "document",
        form,
        [tt.pick(rng, tt.DOC_FACT[key]).format(title=title)],
        _gold(
            DocumentAnswer(document=DocumentRef(file_id=t.file.id)),
            facts=[_fact("value", t.fact.surface_forms, "문서의 정답 값")],
            facts_required=True,
        ),
        ref_tags([t.file]),
        keep=(title,),
        difficulty={"doc_kind": t.doc_kind},
        variant="doc_fact",
    )


def build_doc_version(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, DocVersionTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    latest, previous = acct.file_of(t.latest_file), acct.file_of(t.previous_file)
    if datetime.fromisoformat(str(latest["modified_at"])) <= datetime.fromisoformat(
        str(previous["modified_at"])
    ):
        return "최신 판의 수정 시각이 이전 판보다 늦지 않다"
    if not fact_in(_file_text(acct, t.latest_file), t.latest):
        return "최신 문서 본문에 최신 값이 없다"
    must_not, trimmed = trimmed_previous(t.previous, t.latest)
    rng = ctx.rng(sc, form)
    return Draft(
        sc,
        "document",
        form,
        [tt.pick(rng, tt.DOC_VERSION).format(base=t.base_title)],
        _gold(
            DocumentAnswer(document=DocumentRef(file_id=t.latest_file.id)),
            facts=[_fact("latest", t.latest.surface_forms, "최신 판의 값")],
            must_not=must_not,
            facts_required=True,
        ),
        ref_tags([t.latest_file, t.previous_file]),
        keep=(t.base_title,),
        note="must_not_trimmed" if trimmed else "",
        variant="doc_version",
    )


# ---------------------------------------------------------------- 리마인더


def _reminder_options(
    t: DeadlineTruth, tz: Any, as_of: datetime
) -> dict[str, tuple[datetime, dict[str, Any]]]:
    """as_of 이후인 리마인더 시각 후보. {방식: (시각, 템플릿 인자)}."""
    deadline = t.deadline_at.astimezone(tz)
    day = deadline.date()
    out: dict[str, tuple[datetime, dict[str, Any]]] = {}
    before1 = datetime.combine(day - timedelta(days=1), time(9, 0), tzinfo=tz)
    before2 = datetime.combine(day - timedelta(days=2), time(10, 0), tzinfo=tz)
    hours = 3
    candidates = {
        "day_before": (before1, {}),
        "days_before": (before2, {"days": 2}),
        "hours_before": (deadline - timedelta(hours=hours), {"hours": hours}),
    }
    for name, (due, args) in candidates.items():
        if due > as_of + timedelta(minutes=30):
            out[name] = (due, args)
    return out


def build_deadline(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, DeadlineTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    text = (
        _message_text(acct, t.source) if t.source.kind == "message" else _file_text(acct, t.source)
    )
    if not fact_in(text, t.fact):
        return "출처 본문에 마감 표기가 없다"
    if t.deadline_at <= sc.as_of:
        return "마감이 as_of 이전이다"
    tz = acct.tz
    rng = ctx.rng(sc, form)
    refs = ref_tags([t.source])
    keep = (t.project, t.item)
    if form == "ask":
        deadline = t.deadline_at.astimezone(tz)
        facts = [
            _fact("date", tt.local_date_forms(deadline.date()), "마감 날짜"),
            _fact("time", tt.local_time_forms(deadline, tz), "마감 시각"),
        ]
        return Draft(
            sc,
            "reminder",
            form,
            [tt.pick(rng, tt.DEADLINE_ASK).format(project=t.project, item=t.item)],
            _gold(None, facts=facts, changes=[]),
            refs,
            keep=keep,
            difficulty={"days_until": t.days_until},
            fault_tool="search_messages" if t.source.kind == "message" else None,
            variant="ask",
        )
    options = _reminder_options(t, tz, sc.as_of)
    if not options:
        return "마감 전 리마인더 시각이 모두 as_of 이전이다"
    name = rng.choice(sorted(options))
    due, args = options[name]
    change = ReminderCreatedChange(due_at=due, tolerance_minutes=0, text_any_of=[t.item])
    question = tt.pick(rng, tt.DEADLINE_REMINDER[name]).format(
        project=t.project, item=t.item, **args
    )
    return Draft(
        sc,
        "reminder",
        form,
        [question],
        _gold(None, changes=[change]),
        refs,
        keep=(*keep, *(str(v) for v in args.values())),
        difficulty={"days_until": t.days_until, "reminder_rule": name},
        variant="request",
    )


# ---------------------------------------------------------------- 답할 수 없음·모호함


def _exists_in_world(acct: AccountView, t: AbsentTruth) -> str | None:
    """없어야 할 대상이 세계에 있으면 그 설명을 돌려준다(생성 실패 사유)."""
    q = norm(t.query)
    if t.variant == "person_no_meeting":
        email = (t.person_email or "").casefold()
        if any(
            any(a.email.casefold() == email for a in e.attendees)
            or (e.organizer is not None and e.organizer.email.casefold() == email)
            for e in acct.events
        ):
            return "없어야 할 인물의 일정이 있다"
    elif t.variant == "no_document":
        if any(q in norm(str(f["name"])) for f in acct.files.values()):
            return "없어야 할 문서가 있다"
    elif t.variant == "project_no_meeting":
        if any(q in norm(e.title) for e in acct.events):
            return "미팅이 없어야 할 프로젝트에 일정이 있다"
    else:  # no_amount: 그 프로젝트를 다루는 메일·문서 어디에도 금액 표기가 없어야 한다
        for m in acct.messages.values():
            body = f"{m.get('subject', '')} {m.get('body_text', '')}"
            if q in norm(body) and _has_amount(body):
                return "없어야 할 금액이 메일에 있다"
        for f in acct.files.values():
            body = f"{f.get('name', '')} {f.get('text', '')}"
            if q in norm(body) and _has_amount(body):
                return "없어야 할 금액이 문서에 있다"
    return None


def _has_amount(text: str) -> bool:
    return re.search(r"\d[\d,]*\s*(?:만\s*)?원", text) is not None


def build_absent(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, AbsentTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    problem = _exists_in_world(acct, t)
    _need(problem is None, f"{sc.scenario_id}: {problem}")
    rng = ctx.rng(sc, form)
    return Draft(
        sc,
        "unanswerable",
        form,
        [tt.pick(rng, tt.ABSENT[t.variant]).format(query=t.query)],
        _gold(AbstainAnswer()),
        ref_tags(t.related_exist),
        keep=(t.query,),
        difficulty={"related_exist": len(t.related_exist)},
        fingerprint_extra={"absent": t.variant, "query": t.query},  # 정답(abstain)이 모두 같다
        variant=t.variant,
    )


def _reading(
    acct: AccountView, answer_kind: str, refs: list[ArtifactRef], sc: ScenarioRecord
) -> ReadingAnswer:
    if answer_kind == "event":
        _need(len(refs) == 1, f"{sc.scenario_id}: event 해석의 정답이 하나가 아니다")
        return EventAnswer(event=_ev_ref(acct.event_of(refs[0])))
    if answer_kind == "event_set":
        return EventSetAnswer(events=[_ev_ref(acct.event_of(r)) for r in refs])
    _need(len(refs) == 1, f"{sc.scenario_id}: document 해석의 정답이 하나가 아니다")
    acct.file_of(refs[0])
    return DocumentAnswer(document=DocumentRef(file_id=refs[0].id))


def build_ambiguous(ctx: Ctx, sc: ScenarioRecord, form: str) -> Result:
    t = sc.truth
    if not isinstance(t, AmbiguousTruth):
        raise TaskGenError(f"{sc.scenario_id}: 시나리오 truth 유형이 다르다")
    acct = ctx.acct(sc)
    readings = [_reading(acct, i.answer_kind, list(i.gold), sc) for i in t.interpretations]
    people = [p for p in ctx.tw.skeleton.people.values() if p.name == t.query_subject]
    if t.variant == "person_meetings" and len(people) == 1:
        upcoming = acct.upcoming_with(people[0].email, t.as_of)
        _need(
            [e.id for e in upcoming] == [r.id for r in t.interpretations[1].gold]
            and upcoming[0].id == t.interpretations[0].gold[0].id,
            f"{sc.scenario_id}: person_meetings 해석이 세계에서 다시 계산한 값과 다르다",
        )
    elif t.variant == "homonym_person":
        _need(len(people) == 2, f"{sc.scenario_id}: 동명이인이 두 명이 아니다")
        firsts = {
            acct.upcoming_with(p.email, t.as_of)[0].id
            for p in people
            if acct.upcoming_with(p.email, t.as_of)
        }
        _need(
            firsts == {i.gold[0].id for i in t.interpretations},
            f"{sc.scenario_id}: homonym_person의 다음 미팅이 세계에서 다시 계산한 값과 다르다",
        )
    elif t.variant == "same_project_notes":
        notes = [
            f
            for f in acct.files.values()
            if norm(t.query_subject) in norm(str(f["name"])) and "회의록" in str(f["name"])
        ]
        _need(len(notes) == 2, f"{sc.scenario_id}: 회의록 문서가 두 개가 아니다")
    rng = ctx.rng(sc, form)
    refs = [r for i in t.interpretations for r in i.gold]
    return Draft(
        sc,
        "ambiguous",
        form,
        [tt.pick(rng, tt.AMBIGUOUS[t.variant]).format(name=t.query_subject)],
        _gold(ClarifyAnswer(readings=readings)),
        ref_tags(refs),
        keep=(t.query_subject,),
        difficulty={"readings": len(readings)},
        variant=t.variant,
    )


# ---------------------------------------------------------------- 등록

Builder = Callable[[Ctx, ScenarioRecord, str], Result]

BUILDERS: dict[str, Builder] = {
    "day_agenda": build_day_agenda,
    "free_slot": build_free_slot,
    "reschedule": build_reschedule,
    "cancellation": build_cancellation,
    "project_week": build_project_week,
    "person_next": build_person_next,
    "cross_midnight": build_cross_midnight,
    "tz_event": build_tz_event,
    "all_day": build_all_day,
    "mail_fact": build_mail_fact,
    "reply_needed": build_reply_needed,
    "meeting_request": build_meeting_request,
    "event_change_target": build_event_change,
    "doc_fact": build_doc_fact,
    "doc_version": build_doc_version,
    "deadline": build_deadline,
    "mismatch": build_mismatch,
    "absent": build_absent,
    "ambiguous": build_ambiguous,
}
# 시나리오 유형별로 만들 수 있는 형태
FORMS: dict[str, tuple[str, ...]] = {
    "day_agenda": ("single", "mt"),
    "person_next": ("single", "mt"),
    "mail_fact": ("single", "mt"),
    "deadline": ("request", "ask"),
}


def forms_of(scenario_type: str) -> tuple[str, ...]:
    return FORMS.get(scenario_type, ("single",))
