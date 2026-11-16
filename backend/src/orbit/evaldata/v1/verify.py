"""생성 결과 검증. 하나라도 실패하면 `SkeletonError`로 생성을 실패시킨다.

검사 항목: 참조 무결성, 계약(일정 시각·종일 규칙), 템플릿 본문의 필수·금지 사실, 시나리오 간 근거
비공유, absent 정말 없음, ambiguous 해석 차이, 정답 재료 재계산(day_agenda·free_slot·project_week·
person_next), 주소 도메인·전화번호 형식, 길이 비율, 계열 균형, 수량 목표, 검색 메일함 부분집합.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from orbit.connectors.contracts import CalendarEvent, check_event_times
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
    DocVersionTruth,
    Fact,
    FreeSlotTruth,
    MailFactTruth,
    MismatchTruth,
    PersonNextTruth,
    ProjectWeekTruth,
    RescheduleTruth,
    ScenarioRecord,
    SearchTopicTruth,
    Skeleton,
    TzEventTruth,
)
from orbit.evaldata.v1.stats import refs_in
from orbit.evaldata.v1.worldmodel import ALLOWED_DOMAINS, WorldModel

_PHONE = re.compile(r"(?<![\d,])(?:01[016789]|0\d{1,2})[-. ]\d{3,4}[-. ]\d{4}(?!\d)")
_RRN = re.compile(r"(?<!\d)\d{6}-?[1-4]\d{6}(?!\d)")
FAMILY_TOLERANCE = 0.05
LENGTH_TOLERANCE = 0.03


class SkeletonError(ValueError):
    """생성 결과가 검증을 통과하지 못했다. 전체 실패 목록은 `errors`에 있다."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


def _domain_ok(email: str) -> bool:
    d = email.rsplit("@", 1)[-1].lower()
    return d.endswith(".test") or any(d == x or d.endswith("." + x) for x in ALLOWED_DOMAINS)


def verify_skeleton(wm: WorldModel, sk: Skeleton) -> None:
    errors: list[str] = []
    _refs_and_contract(wm, sk, errors)
    _templates(sk, errors)
    _scenarios(wm, sk, errors)
    _pii(sk, errors)
    _ratios(wm, sk, errors)
    _counts(wm, sk, errors)
    _search(wm, sk, errors)
    if errors:
        shown = "\n  ".join(errors[:30])
        extra = f"\n  ... 외 {len(errors) - 30}건" if len(errors) > 30 else ""
        raise SkeletonError(f"뼈대 검증 실패 {len(errors)}건:\n  {shown}{extra}", errors)


# ---------------------------------------------------------------- 참조·계약


def _refs_and_contract(wm: WorldModel, sk: Skeleton, errors: list[str]) -> None:
    snap = sk.snapshot_time
    mail_start = snap - timedelta(weeks=wm.window.mail_weeks)
    used_bodies: Counter[str] = Counter()
    for acct, a in sk.accounts.items():
        tz = ZoneInfo(a.time_zone)
        base = snap.astimezone(tz).date()
        lo_day = base - timedelta(weeks=wm.window.event_weeks_before)
        hi_day = base + timedelta(weeks=wm.window.event_weeks_after)
        cal_ids = {c.id for c in a.calendars}
        ev_ids = [e.id for e in a.events]
        if len(set(ev_ids)) != len(ev_ids):
            errors.append(f"{acct}: 일정 ID 중복")
        for e in a.events:
            if e.calendar_id not in cal_ids:
                errors.append(f"{acct}: 일정 {e.id}의 캘린더가 없다")
            if not e.start < e.end:
                errors.append(f"{acct}: 일정 {e.id}의 시작이 종료보다 앞서지 않는다")
            if e.all_day:  # 계약(ADR-0027): 그 캘린더 시간대의 00:00인 순간
                zone = next(
                    (c.time_zone for c in a.calendars if c.id == e.calendar_id), a.time_zone
                )
                try:
                    check_event_times(e.start, e.end, True, zone)
                except ValueError as exc:
                    errors.append(f"{acct}: 종일 일정 {e.id}가 계약을 어긴다 ({exc})")
            if e.updated_at > snap:
                errors.append(f"{acct}: 일정 {e.id}의 갱신이 스냅샷 이후다")
            day = e.start.astimezone(tz).date()
            if not lo_day <= day <= hi_day:
                errors.append(f"{acct}: 일정 {e.id}가 일정 기간 밖이다({day})")
        msg_ids = {m.id for m in a.messages}
        if len(msg_ids) != len(a.messages):
            errors.append(f"{acct}: 메시지 ID 중복")
        by_thread: dict[str, list[str]] = {t.thread_id: t.message_ids for t in a.threads}
        for m in a.messages:
            if m.body_id not in sk.bodies:
                errors.append(f"{acct}: 메시지 {m.id}의 본문 명세가 없다")
            used_bodies[m.body_id] += 1
            if m.id not in by_thread.get(m.thread_id, []):
                errors.append(f"{acct}: 메시지 {m.id}가 스레드 {m.thread_id}에 없다")
            if not mail_start <= m.sent_at < snap:
                errors.append(f"{acct}: 메시지 {m.id}가 메일 기간 밖이다")
        for th in a.threads:
            if not th.message_ids or any(i not in msg_ids for i in th.message_ids):
                errors.append(f"{acct}: 스레드 {th.thread_id}의 메시지가 올바르지 않다")
        names = [f.name for f in a.files]
        if len(set(names)) != len(names):
            errors.append(f"{acct}: 문서 이름 중복")
        for f in a.files:
            if f.body_id not in sk.bodies:
                errors.append(f"{acct}: 파일 {f.id}의 본문 명세가 없다")
            used_bodies[f.body_id] += 1
            if f.modified_at > snap:
                errors.append(f"{acct}: 파일 {f.id}의 수정이 스냅샷 이후다")
    for bid in sk.bodies:
        if used_bodies[bid] == 0:
            errors.append(f"본문 명세 {bid}를 쓰는 아티팩트가 없다")


def _exists(sk: Skeleton, acct: str, ref: ArtifactRef) -> bool:
    a = sk.accounts.get(acct)
    if a is None:
        return False
    if ref.kind == "event":
        return any(e.id == ref.id and e.calendar_id == ref.calendar_id for e in a.events)
    if ref.kind == "message":
        return any(m.id == ref.id and (ref.body_id in (None, m.body_id)) for m in a.messages)
    return any(f.id == ref.id and (ref.body_id in (None, f.body_id)) for f in a.files)


# ---------------------------------------------------------------- 템플릿


def _templates(sk: Skeleton, errors: list[str]) -> None:
    for bid, spec in sk.bodies.items():
        for f in spec.required_facts:
            if not fact_in(spec.template_text, f):
                errors.append(f"{bid}: 템플릿에 필수 사실 {f.fact_id}가 없다")
        for f in spec.forbidden_facts:
            if fact_in(spec.template_text, f):
                errors.append(f"{bid}: 템플릿에 금지 사실 {f.fact_id}가 있다")
        for r in spec.required_facts:
            for x in spec.forbidden_facts:
                if conflicts(r, x):
                    errors.append(f"{bid}: 필수 {r.fact_id}와 금지 {x.fact_id}의 표기가 겹친다")


# ---------------------------------------------------------------- 시나리오


def _scenarios(wm: WorldModel, sk: Skeleton, errors: list[str]) -> None:
    seen: dict[tuple[str, str, str], str] = {}
    users = {u.account_id: u for u in wm.users}
    snap = sk.snapshot_time
    for s in sk.scenarios:
        u = users.get(s.account_id)
        if u is None or s.account_id not in sk.accounts:
            errors.append(f"{s.scenario_id}: 계정이 없다")
            continue
        tz = ZoneInfo(u.time_zone)
        for ref in [*s.evidence, *s.distractors, *refs_in(s.truth)]:
            if not _exists(sk, s.account_id, ref):
                errors.append(f"{s.scenario_id}: 참조 {ref.kind}:{ref.id}가 없다")
        for ref in s.evidence:
            key = (s.account_id, ref.kind, ref.id)
            if key in seen:
                errors.append(f"{s.scenario_id}: 근거 {ref.id}를 {seen[key]}와 공유한다")
            seen[key] = s.scenario_id
        if s.as_of < snap or s.as_of.astimezone(tz).weekday() >= 5:
            errors.append(f"{s.scenario_id}: as_of가 스냅샷 이후 평일이 아니다")
        local = s.as_of.astimezone(tz).date()
        if not wm.window.asof_start <= local <= wm.window.asof_end:
            errors.append(f"{s.scenario_id}: as_of가 날짜 범위 밖이다")
        _same_family(sk, s, errors)
        t = s.truth
        if isinstance(t, DayAgendaTruth):
            _check_day_agenda(sk, s, t, tz, errors)
        elif isinstance(t, FreeSlotTruth):
            _check_free_slot(sk, s, t, errors)
        elif isinstance(t, ProjectWeekTruth):
            _check_project_week(sk, s, t, tz, errors)
        elif isinstance(t, PersonNextTruth):
            _check_person_next(sk, s, t, errors)
        elif isinstance(t, AbsentTruth):
            _check_absent(sk, s, t, errors)
        elif isinstance(t, AmbiguousTruth):
            _check_ambiguous(sk, s, t, errors)
        else:
            _check_truth_misc(sk, s, t, tz, errors)
    _check_question_keys(sk, errors)


def _same_family(sk: Skeleton, s: ScenarioRecord, errors: list[str]) -> None:
    for ref in [*s.evidence, *s.distractors]:
        if (
            ref.body_id is not None
            and sk.bodies[ref.body_id].generator_family != s.generator_family
        ):
            errors.append(
                f"{s.scenario_id}: 근거·방해 본문 {ref.body_id}의 계열이 시나리오와 다르다"
            )


def _day_events(
    sk: Skeleton, acct: str, tz: ZoneInfo, day: date, *, cancelled: bool
) -> list[CalendarEvent]:
    out = []
    for e in sk.accounts[acct].events:
        s, en = e.start.astimezone(tz), e.end.astimezone(tz)
        last = (en - timedelta(microseconds=1)).date() if en > s else s.date()
        if s.date() <= day <= last and (e.status == "cancelled") == cancelled:
            out.append(e)
    return sorted(out, key=lambda e: (e.start, e.id))


def _check_day_agenda(
    sk: Skeleton, s: ScenarioRecord, t: DayAgendaTruth, tz: ZoneInfo, errors: list[str]
) -> None:
    got = [e.id for e in _day_events(sk, s.account_id, tz, t.day, cancelled=False)]
    if got != [r.id for r in t.events]:
        errors.append(f"{s.scenario_id}: day_agenda 일정이 세계와 다르다")
    got_c = [e.id for e in _day_events(sk, s.account_id, tz, t.day, cancelled=True)]
    if got_c != sorted(r.id for r in t.cancelled) and got_c != [r.id for r in t.cancelled]:
        errors.append(f"{s.scenario_id}: day_agenda 취소 일정이 세계와 다르다")
    if len({r.calendar_id for r in t.events}) < 2 and len(sk.accounts[s.account_id].calendars) > 1:
        errors.append(f"{s.scenario_id}: day_agenda가 여러 캘린더에 걸치지 않는다")


def _check_free_slot(sk: Skeleton, s: ScenarioRecord, t: FreeSlotTruth, errors: list[str]) -> None:
    tz = ZoneInfo(t.time_zone)
    evs = _day_events(sk, s.account_id, tz, t.day, cancelled=False)
    if [e.id for e in evs] != [r.id for r in t.busy]:
        errors.append(f"{s.scenario_id}: free_slot 일정이 세계와 다르다")
    cur = t.work.start
    free = []
    for e in evs:
        if e.start - cur >= timedelta(minutes=t.min_minutes):
            free.append((cur, e.start))
        cur = max(cur, e.end)
    if t.work.end - cur >= timedelta(minutes=t.min_minutes):
        free.append((cur, t.work.end))
    if free != [(f.start, f.end) for f in t.free_slots] or not free:
        errors.append(f"{s.scenario_id}: free_slot의 빈 시간이 재계산과 다르다")


def _check_project_week(
    sk: Skeleton, s: ScenarioRecord, t: ProjectWeekTruth, tz: ZoneInfo, errors: list[str]
) -> None:
    end = t.week_start + timedelta(days=7)
    got = [
        e
        for e in sk.accounts[s.account_id].events
        if norm(t.project) in norm(e.title)
        and t.week_start <= e.start.astimezone(tz).date() < end
        and e.status == "confirmed"
    ]
    if sorted(e.id for e in got) != sorted(r.id for r in t.events):
        errors.append(f"{s.scenario_id}: project_week 일정이 세계와 다르다")


def _upcoming(sk: Skeleton, acct: str, email: str, as_of: datetime) -> list[str]:
    evs = [
        e
        for e in sk.accounts[acct].events
        if e.status == "confirmed"
        and e.start >= as_of
        and any(x.email == email for x in e.attendees)
    ]
    return [e.id for e in sorted(evs, key=lambda e: (e.start, e.id))]


def _check_person_next(
    sk: Skeleton, s: ScenarioRecord, t: PersonNextTruth, errors: list[str]
) -> None:
    got = _upcoming(sk, s.account_id, t.person_email, t.as_of)
    if got != [r.id for r in t.upcoming] or len(got) < 2 or got[0] != t.next_event.id:
        errors.append(f"{s.scenario_id}: person_next 일정이 세계와 다르다")


def _check_absent(sk: Skeleton, s: ScenarioRecord, t: AbsentTruth, errors: list[str]) -> None:
    a = sk.accounts[s.account_id]
    q = norm(t.query)
    if t.variant == "person_no_meeting":
        hit = [
            e.id
            for e in a.events
            if any(x.email == t.person_email for x in e.attendees)
            or (e.organizer is not None and e.organizer.email == t.person_email)
        ]
        if hit:
            errors.append(f"{s.scenario_id}: 없어야 할 인물의 일정이 있다 {hit[:3]}")
    elif t.variant == "no_document":
        if any(q in norm(f.name) for f in a.files):
            errors.append(f"{s.scenario_id}: 없어야 할 문서가 있다")
        if any(
            sp.context.document_title and q in norm(sp.context.document_title)
            for sp in sk.bodies.values()
            if sp.artifact == "document"
            and any(f.body_id and sk.bodies[f.body_id] is sp for f in a.files)
        ):
            errors.append(f"{s.scenario_id}: 없어야 할 문서 제목이 본문 명세에 있다")
    else:
        if t.variant == "project_no_meeting" and any(q in norm(e.title) for e in a.events):
            errors.append(f"{s.scenario_id}: 미팅이 없어야 할 프로젝트에 일정이 있다")
        _check_quiet_bodies(sk, s, t, errors)


_COMMITMENT_KINDS = ("amount", "date", "time", "datetime", "deadline")


def _check_quiet_bodies(sk: Skeleton, s: ScenarioRecord, t: AbsentTruth, errors: list[str]) -> None:
    """조용한 프로젝트를 말하는 본문 명세: 금액·일정 필수 사실이 없고 약속 금지 사실이 있다."""
    a = sk.accounts[s.account_id]
    q = norm(t.query)
    for bid in sorted({m.body_id for m in a.messages} | {f.body_id for f in a.files}):
        sp = sk.bodies[bid]
        c = sp.context
        text = " ".join(
            x or "" for x in (c.subject, c.topic, c.intent, c.project, c.document_title)
        )
        if q not in norm(text + " " + sp.template_text):
            continue
        if any(f.kind in _COMMITMENT_KINDS for f in sp.required_facts):
            errors.append(
                f"{s.scenario_id}: 조용한 프로젝트 본문 {bid}에 금액·일정 필수 사실이 있다"
            )
        kinds = {f.kind for f in sp.forbidden_facts if f.value == "*"}
        if not {"amount", "decision"} <= kinds:
            errors.append(f"{s.scenario_id}: 조용한 프로젝트 본문 {bid}에 약속 금지 사실이 없다")


def _check_ambiguous(sk: Skeleton, s: ScenarioRecord, t: AmbiguousTruth, errors: list[str]) -> None:
    g = [{(r.kind, r.id) for r in i.gold} for i in t.interpretations]
    if g[0] == g[1] or not g[0] or not g[1]:
        errors.append(f"{s.scenario_id}: 두 해석의 정답이 같거나 비었다")
    a = sk.accounts[s.account_id]
    if t.variant == "same_project_notes":
        n = sum(1 for f in a.files if norm(t.query_subject) in norm(f.name) and "회의록" in f.name)
        if n != 2:
            errors.append(
                f"{s.scenario_id}: '{t.query_subject} 회의록' 문서가 {n}개다(2개여야 한다)"
            )
    elif t.variant == "person_meetings":
        email = next(
            (p.email for p in sk.people.values() if p.name == t.query_subject),
            None,
        )
        if email is None or _upcoming(sk, s.account_id, email, t.as_of) != [
            r.id for r in t.interpretations[1].gold
        ]:
            errors.append(f"{s.scenario_id}: person_meetings 일정이 세계와 다르다")
    else:
        same = [p for p in sk.people.values() if p.name == t.query_subject]
        if len(same) != 2:
            errors.append(f"{s.scenario_id}: 동명이인이 두 명이 아니다")
        else:
            nexts = [_upcoming(sk, s.account_id, p.email, t.as_of) for p in same]
            firsts = {x[0] for x in nexts if x}
            gold = {i.gold[0].id for i in t.interpretations}
            if firsts != gold:
                errors.append(f"{s.scenario_id}: homonym_person의 다음 미팅이 세계와 다르다")


# ---------------------------------------------------------------- 재계산하지 않는 유형의 truth


def _event_of(sk: Skeleton, acct: str, ref: ArtifactRef) -> CalendarEvent | None:
    for e in sk.accounts[acct].events:
        if e.id == ref.id and e.calendar_id == ref.calendar_id:
            return e
    return None


def _required(sk: Skeleton, ref: ArtifactRef) -> list[Fact]:
    return sk.bodies[ref.body_id].required_facts if ref.body_id in sk.bodies else []


def _has(facts: list[Fact], fact: Fact) -> bool:
    return any(f.kind == fact.kind and f.value == fact.value for f in facts)


def _sent_at(sk: Skeleton, acct: str, ref: ArtifactRef) -> datetime | None:
    for m in sk.accounts[acct].messages:
        if m.id == ref.id:
            return m.sent_at
    for f in sk.accounts[acct].files:
        if f.id == ref.id:
            return f.modified_at
    return None


def _same_event_time(e: CalendarEvent | None, start: datetime, end: datetime) -> bool:
    return e is not None and e.start == start and e.end == end


def _check_truth_misc(
    sk: Skeleton, s: ScenarioRecord, t: object, tz: ZoneInfo, errors: list[str]
) -> None:
    sid, acct = s.scenario_id, s.account_id
    if isinstance(t, RescheduleTruth):
        e = _event_of(sk, acct, t.event)
        if not _same_event_time(e, t.new.start, t.new.end) or (e and e.status != "confirmed"):
            errors.append(f"{sid}: reschedule의 변경 후 시각이 일정과 다르다")
        if t.old.start == t.new.start:
            errors.append(f"{sid}: reschedule의 옛 시각과 새 시각이 같다")
        sent = _sent_at(sk, acct, t.change_message)
        if sent is None or not sent < t.old.start:
            errors.append(f"{sid}: reschedule의 변경 메일이 옛 일정 시각보다 늦다")
        if not any(
            f.kind == "datetime" and datetime.fromisoformat(str(f.value)) == t.new.start
            for f in _required(sk, t.change_message)
        ):
            errors.append(f"{sid}: reschedule의 변경 메일에 새 시각 필수 사실이 없다")
    elif isinstance(t, CancellationTruth):
        e = _event_of(sk, acct, t.event)
        if (
            e is None
            or e.status != "cancelled"
            or not _same_event_time(e, t.original.start, t.original.end)
        ):
            errors.append(f"{sid}: cancellation 일정이 취소 상태가 아니거나 시각이 다르다")
    elif isinstance(t, MismatchTruth):
        e = _event_of(sk, acct, t.event)
        if not _same_event_time(e, t.calendar_span.start, t.calendar_span.end) or (
            e and (e.location != t.calendar_location or e.status != "confirmed")
        ):
            errors.append(f"{sid}: mismatch의 캘린더 값이 일정과 다르다")
        want = (["time"] if t.mail_start != t.calendar_span.start else []) + (
            ["location"] if t.mail_location != t.calendar_location else []
        )
        if not want or want != t.fields:
            errors.append(f"{sid}: mismatch의 불일치 필드가 값과 다르다")
    elif isinstance(t, CrossMidnightTruth):
        e = _event_of(sk, acct, t.event)
        if not _same_event_time(e, t.span.start, t.span.end):
            errors.append(f"{sid}: cross_midnight 시각이 일정과 다르다")
        last = (t.span.end.astimezone(tz) - timedelta(microseconds=1)).date()
        if (
            t.start_day != t.span.start.astimezone(tz).date()
            or t.end_day != last
            or t.start_day == t.end_day
        ):
            errors.append(f"{sid}: cross_midnight의 날짜가 자정을 넘지 않거나 시각과 다르다")
    elif isinstance(t, AllDayTruth):
        e = _event_of(sk, acct, t.event)
        if e is None or not e.all_day or not _same_event_time(e, t.span.start, t.span.end):
            errors.append(f"{sid}: all_day가 종일 일정과 다르다")
        cal = next((c for c in sk.accounts[acct].calendars if e and c.id == e.calendar_id), None)
        zone = ZoneInfo(cal.time_zone) if cal else tz  # 이벤트가 속한 캘린더의 시간대
        if t.intended_date != t.span.start.astimezone(zone).date() or t.span.end.astimezone(
            zone
        ).date() != t.intended_date + timedelta(days=1):
            errors.append(f"{sid}: all_day의 날짜가 span과 다르다")
    elif isinstance(t, TzEventTruth):
        e = _event_of(sk, acct, t.event)
        if not _same_event_time(e, t.span.start, t.span.end):
            errors.append(f"{sid}: tz_event 시각이 일정과 다르다")
        if t.start_in_other_zone != t.span.start.astimezone(ZoneInfo(t.other_time_zone)) or (
            t.start_in_other_zone.utcoffset()
            != t.span.start.astimezone(ZoneInfo(t.other_time_zone)).utcoffset()
        ):
            errors.append(f"{sid}: tz_event의 상대 시간대 시각이 재계산과 다르다")
        offset = t.span.start.astimezone(ZoneInfo(t.user_time_zone)).utcoffset()
        if offset is None or offset.total_seconds() / 3600 != t.user_utc_offset_hours:
            errors.append(f"{sid}: tz_event의 UTC 오프셋이 재계산과 다르다")
    elif isinstance(t, MailFactTruth):
        _check_latest_previous(
            sk, s, t.latest, t.previous, t.latest_message, t.previous_message, errors
        )
        same = _thread_of(sk, acct, t.latest_message) == _thread_of(sk, acct, t.previous_message)
        if same != t.same_thread:
            errors.append(f"{sid}: mail_fact의 same_thread가 실제와 다르다")
    elif isinstance(t, DocVersionTruth):
        _check_latest_previous(sk, s, t.latest, t.previous, t.latest_file, t.previous_file, errors)
    elif isinstance(t, DeadlineTruth):
        if not _has(_required(sk, t.source), t.fact):
            errors.append(f"{sid}: deadline 근거에 마감 사실이 없다")
        if datetime.fromisoformat(str(t.fact.value)) != t.deadline_at:
            errors.append(f"{sid}: deadline_at이 마감 사실과 다르다")
        hours = (t.deadline_at - t.as_of).total_seconds() / 3600
        if abs(hours - t.hours_until) > 0.01 or hours <= 0:
            errors.append(f"{sid}: deadline의 hours_until이 재계산과 다르다")
        if (
            t.deadline_at.astimezone(tz).date() - t.as_of.astimezone(tz).date()
        ).days != t.days_until:
            errors.append(f"{sid}: deadline의 days_until이 재계산과 다르다")


def _thread_of(sk: Skeleton, acct: str, ref: ArtifactRef) -> str | None:
    for m in sk.accounts[acct].messages:
        if m.id == ref.id:
            return m.thread_id
    return None


def _check_latest_previous(
    sk: Skeleton,
    s: ScenarioRecord,
    latest: Fact,
    previous: Fact,
    latest_ref: ArtifactRef,
    previous_ref: ArtifactRef,
    errors: list[str],
) -> None:
    sid, acct = s.scenario_id, s.account_id
    if latest.value == previous.value:
        errors.append(f"{sid}: 최신 값과 이전 값이 같다")
    if not _has(_required(sk, latest_ref), latest):
        errors.append(f"{sid}: 최신 값이 최신 아티팩트의 필수 사실에 없다")
    if not _has(_required(sk, previous_ref), previous):
        errors.append(f"{sid}: 이전 값이 이전 아티팩트의 필수 사실에 없다")
    a, b = _sent_at(sk, acct, latest_ref), _sent_at(sk, acct, previous_ref)
    if a is None or b is None or not a > b:
        errors.append(f"{sid}: 최신 아티팩트가 이전 아티팩트보다 늦지 않다")


def _check_question_keys(sk: Skeleton, errors: list[str]) -> None:
    """사용자별 질문 키 유일성. 같은 키가 두 시나리오에 나오면 한 질문에 정답이 둘이다."""
    seen: dict[tuple[str, str], str] = {}
    for s in sk.scenarios:
        key = getattr(s.truth, "question_key", None)
        if key is None:
            continue
        k = (s.account_id, key)
        if k in seen:
            errors.append(f"{s.scenario_id}: 질문 키 '{key}'가 {seen[k]}와 겹친다")
        seen[k] = s.scenario_id


# ---------------------------------------------------------------- 개인정보 형식


def _texts(sk: Skeleton) -> Iterator[tuple[str, str]]:
    for bid, sp in sk.bodies.items():
        yield f"body {bid}", sp.template_text
        if sp.context.subject:
            yield f"body {bid} subject", sp.context.subject
    for acct, a in sk.accounts.items():
        for e in a.events:
            yield f"{acct} event {e.id}", f"{e.title} {e.description} {e.location}"
        for m in a.messages:
            yield (
                f"{acct} message {m.id}",
                m.subject + " " + " ".join(x.filename for x in m.attachments),
            )
        for f in a.files:
            yield f"{acct} file {f.id}", f.name


def _pii(sk: Skeleton, errors: list[str]) -> None:
    emails: list[str] = [p.email for p in sk.people.values()]
    for a in sk.accounts.values():
        emails.append(a.email)
        for m in a.messages:
            emails += [m.from_.email, *(x.email for x in m.to), *(x.email for x in m.cc)]
        for e in a.events:
            emails += [x.email for x in e.attendees]
            if e.organizer:
                emails.append(e.organizer.email)
        for f in a.files:
            emails += [x.email for x in f.owners]
    for em in sorted(set(emails)):
        if not _domain_ok(em):
            errors.append(f"허용하지 않는 주소 도메인: {em}")
    for where, text in _texts(sk):
        if _PHONE.search(text) or _RRN.search(text):
            errors.append(f"{where}: 전화번호·주민번호 형식이 있다")


# ---------------------------------------------------------------- 비율·수량


def _ratios(wm: WorldModel, sk: Skeleton, errors: list[str]) -> None:
    mails = [b for b in sk.bodies.values() if b.artifact == "mail"]
    if mails:
        c: Counter[str] = Counter(b.length_class for b in mails)
        mix = wm.targets.length_mix
        for name, want in (("short", mix.short), ("medium", mix.medium), ("long", mix.long)):
            got = c[name] / len(mails)
            if abs(got - want) > LENGTH_TOLERANCE:
                errors.append(f"메일 길이 비율 {name}: {got:.3f} (목표 {want})")
    fams = list(wm.generation.families)

    def balanced(label: str, counts: Counter[str]) -> None:
        n = sum(counts.values())
        if n == 0:
            return
        slack = max(1.0, FAMILY_TOLERANCE * n)
        for f in fams:
            if abs(counts[f] - n / len(fams)) > slack:
                errors.append(f"계열 균형 {label}: {f} {counts[f]}/{n}")

    balanced("전체", Counter(b.generator_family for b in sk.bodies.values()))
    for key, attr in (("장르", "genre"), ("길이", "length_class"), ("종류", "artifact")):
        groups: dict[str, Counter[str]] = defaultdict(Counter)
        for b in sk.bodies.values():
            groups[str(getattr(b, attr))][b.generator_family] += 1
        for k, c in sorted(groups.items()):
            balanced(f"{key}={k}", c)
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    for s in sk.scenarios:
        by_type[s.type][s.generator_family] += 1
    for k, c in sorted(by_type.items()):
        balanced(f"시나리오 유형={k}", c)


def _counts(wm: WorldModel, sk: Skeleton, errors: list[str]) -> None:
    t = wm.targets
    for u in wm.e2e_users():
        a = sk.accounts[u.account_id]
        if abs(len(a.messages) - t.mails_per_e2e_user) > t.mails_tolerance:
            errors.append(f"{u.key}: 메일 수 {len(a.messages)}가 목표 범위 밖이다")
        if not t.docs_per_e2e_user[0] <= len(a.files) <= t.docs_per_e2e_user[1]:
            errors.append(f"{u.key}: 문서 수 {len(a.files)}가 목표 범위 밖이다")
        if not 2 <= len(a.calendars) <= 3:
            errors.append(f"{u.key}: 캘린더 수가 2~3이 아니다")
    for s_count in (wm.targets.scenarios,):
        got: Counter[str] = Counter(s.type for s in sk.scenarios)
        for k, n in s_count.items():
            if got[k] != n:
                errors.append(f"시나리오 {k}: {got[k]}개(목표 {n})")


# ---------------------------------------------------------------- 검색 메일함


def _search(wm: WorldModel, sk: Skeleton, errors: list[str]) -> None:
    accts = [u.account_id for u in wm.search_users()]
    sizes = wm.targets.search_sizes
    for i, acct in enumerate(accts):
        a = sk.accounts[acct]
        if len(a.messages) != sizes[i]:
            errors.append(f"{acct}: 메일 수 {len(a.messages)}가 {sizes[i]}가 아니다")
        if len(a.files) != wm.targets.search_docs:
            errors.append(f"{acct}: 문서 수 {len(a.files)}가 {wm.targets.search_docs}가 아니다")
        mb = sk.search_mailboxes.get(acct)
        if mb is None or mb.size != len(a.messages):
            errors.append(f"{acct}: search_mailboxes 기록이 올바르지 않다")
        elif {m.body_id for m in a.messages} != set(mb.message_by_body):
            errors.append(f"{acct}: message_by_body가 메시지와 다르다")
    bodies = [{m.body_id for m in sk.accounts[a].messages} for a in accts]
    for i in range(len(accts) - 1):
        if not bodies[i] <= bodies[i + 1]:
            errors.append(f"{accts[i]}가 {accts[i + 1]}의 부분집합이 아니다")
    if not accts:
        return
    small_msgs = bodies[0]
    small_files = {f.body_id for f in sk.accounts[accts[0]].files}
    for s in sk.scenarios:
        t = s.truth
        if not isinstance(t, SearchTopicTruth):
            continue
        for bid, level in t.relevance_by_body.items():
            if bid not in sk.bodies:
                errors.append(f"{s.scenario_id}: 관련도 표의 본문 {bid}가 없다")
            elif level >= 1 and bid not in small_msgs and bid not in small_files:
                errors.append(f"{s.scenario_id}: 관련도 {level}인 {bid}가 가장 작은 메일함에 없다")
