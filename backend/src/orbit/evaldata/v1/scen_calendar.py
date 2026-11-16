"""일정 시나리오: day_agenda, free_slot, reschedule, cancellation, project_week, person_next,
cross_midnight, tz_event, all_day."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from orbit.evaldata.v1 import templates as T
from orbit.evaldata.v1.core import Builder, MsgSpec, Scn, WorldBuildError
from orbit.evaldata.v1.facts import datetime_fact
from orbit.evaldata.v1.scen_common import (
    MAX_FUTURE_DAYS,
    attendees_for,
    claims_of,
    compatible,
    day_slots,
    future_weekday,
    next_claim,
    organizer_for,
    retry,
    span,
    take_project,
)
from orbit.evaldata.v1.skeleton import (
    AllDayTruth,
    ArtifactRef,
    CancellationTruth,
    CrossMidnightTruth,
    DayAgendaTruth,
    FreeSlotTruth,
    PersonNextTruth,
    ProjectWeekTruth,
    RescheduleTruth,
    TimeSpan,
    TzEventTruth,
)


def _tz(b: Builder, s: Scn) -> str:
    return b.acc[s.account_id].user.time_zone


def day_agenda(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    day = b.pick_day(rng, acct, -20, 15, empty=True)
    n = 3 + rng.randrange(2)
    cancelled_idx = rng.randrange(n + 1) if rng.random() < 0.5 else -1
    total = n + (1 if cancelled_idx >= 0 else 0)
    slots = day_slots(rng, b, acct, day, total)
    titles = rng.sample(T.GENERIC_TITLES, total)
    cal_n = len(b.acc[acct].calendars)
    shown: list[ArtifactRef] = []
    cancelled: list[ArtifactRef] = []
    for i, (st, en) in enumerate(slots):
        status = "cancelled" if i == cancelled_idx else "confirmed"
        ref = b.add_event(
            acct,
            title=titles[i],
            start=st,
            end=en,
            calendar=i % cal_n,
            attendees=attendees_for(b, s, st, rng.randrange(3)),
            location=rng.choice(T.LOCATIONS),
            status=status,
            claim=True,
            rng=rng,
        )
        (cancelled if status == "cancelled" else shown).append(ref)
    b.reserve(acct, [day])
    s.evidence += shown
    s.distractors += cancelled
    b.end(s, DayAgendaTruth(day=day, time_zone=_tz(b, s), events=shown, cancelled=cancelled))


def free_slot(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    day = b.pick_day(rng, acct, -10, 15, empty=True)
    work_s, work_e = b.local(acct, day, 9), b.local(acct, day, 18)
    min_minutes = 60

    def make(r: random.Random) -> list[tuple[datetime, datetime]]:
        return day_slots(r, b, acct, day, 3 + r.randrange(2))

    def free_of(slots: list[tuple[datetime, datetime]]) -> list[TimeSpan]:
        out: list[TimeSpan] = []
        cur = work_s
        for st, en in slots:
            if st - cur >= timedelta(minutes=min_minutes):
                out.append(span(cur, st))
            cur = max(cur, en)
        if work_e - cur >= timedelta(minutes=min_minutes):
            out.append(span(cur, work_e))
        return out

    slots = retry(rng, make, lambda v: bool(free_of(v)))
    free = free_of(slots)
    titles = rng.sample(T.GENERIC_TITLES, len(slots) + 1)
    cal_n = len(b.acc[acct].calendars)
    busy: list[ArtifactRef] = []
    for i, (st, en) in enumerate(slots):
        busy.append(
            b.add_event(
                acct,
                title=titles[i],
                start=st,
                end=en,
                calendar=i % cal_n,
                attendees=attendees_for(b, s, st, rng.randrange(2)),
                location=rng.choice(T.LOCATIONS),
                claim=True,
                rng=rng,
            )
        )
    if rng.random() < 0.5:  # 빈 시간 안에 취소된 일정: 정답에 영향이 없다
        f = free[0]
        st = f.start
        s.distractors.append(
            b.add_event(
                acct,
                title=titles[-1],
                start=st,
                end=st + timedelta(minutes=30),
                status="cancelled",
                claim=True,
                rng=rng,
            )
        )
    b.reserve(acct, [day])
    s.evidence += busy
    b.end(
        s,
        FreeSlotTruth(
            day=day,
            time_zone=_tz(b, s),
            work=span(work_s, work_e),
            min_minutes=min_minutes,
            busy=busy,
            free_slots=free,
        ),
    )


def _times_pair(b: Builder, s: Scn, new_lo: int, new_hi: int) -> tuple[datetime, datetime]:
    """(옛 시작, 새 시작). 같은 날짜 표기·시각 표기가 서로 겹치지 않는다."""
    rng, acct = s.rng, s.account_id

    def make(r: random.Random) -> tuple[datetime, datetime]:
        new_day = future_weekday(b, s, new_lo, new_hi)
        new = b.local(acct, new_day, 10 + r.randrange(6), 30 * r.randrange(2))
        old_day = new_day - timedelta(days=1 + r.randrange(5))
        old = b.local(acct, old_day, 9 + r.randrange(8), 30 * r.randrange(2))
        return old, new

    def ok(v: tuple[datetime, datetime]) -> bool:
        old, new = v
        return old.weekday() < 5 and compatible(
            [datetime_fact("o", old)], [datetime_fact("n", new)]
        )

    return retry(rng, make, ok)


def reschedule(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    old, new = _times_pair(b, s, 3, MAX_FUTURE_DAYS)
    org = organizer_for(b, s, new)
    title = rng.choice(T.MEETING_KINDS)
    loc = rng.choice(T.LOCATIONS)
    dur = timedelta(minutes=rng.choice([30, 60, 60, 90]))
    t_invite = b.when_before_snapshot(rng, 15, 30)
    # 변경 메일은 안내 메일 뒤, 스냅샷 전, 옛 일정 시각보다 앞에 온다
    # (옛 시각이 이미 지났으면 변경 안내가 아니다)
    latest = min(b.snapshot - timedelta(hours=1), old.astimezone(UTC) - timedelta(hours=2))
    earliest = t_invite + timedelta(days=1)
    t_change = earliest + timedelta(
        minutes=int(rng.random() * (latest - earliest).total_seconds() / 60)
    )
    fo, fn = datetime_fact("old", old), datetime_fact("new", new)
    ev_ref = b.add_event(
        acct,
        title=title,
        start=new,
        end=new + dur,
        attendees=[org],
        location=loc,
        organizer=org,
        updated_at=t_change + timedelta(minutes=20),
        rng=rng,
    )
    subj = f"{title} 일정 안내"
    out = b.thread(
        acct,
        [
            MsgSpec(
                sender=org,
                to=["user"],
                subject=subj,
                sent_at=t_invite,
                lines=[
                    f"{title} 회의를 {fo.surface_forms[0]}에 진행하려고 합니다.",
                    f"장소는 {loc}입니다.",
                ],
                intent=f"{title} 회의의 원래 일정(시각)을 안내하는 메일",
                required=[fo],
                forbidden=[fn],
            ),
            MsgSpec(
                sender=org,
                to=["user"],
                subject=f"Re: {subj} (시간 변경)",
                sent_at=t_change,
                lines=[
                    f"사정이 생겨 {title} 회의 시간을 바꿨습니다.",
                    f"기존 {fo.surface_forms[0]}에서 {fn.surface_forms[0]}로 옮깁니다.",
                    "캘린더도 새 시간으로 수정해 두었습니다.",
                ],
                intent=f"{title} 회의 시간이 바뀌었음을 알리는 답장(새 시각을 분명히 밝힌다)",
                required=[fn],
            ),
        ],
        rng,
    )
    s.evidence += [ev_ref, out.messages[1]]
    s.distractors.append(out.messages[0])
    b.end(
        s,
        RescheduleTruth(
            event=ev_ref,
            title=title,
            time_zone=_tz(b, s),
            old=span(old, old + dur),
            new=span(new, new + dur),
            change_message=out.messages[1],
        ),
    )


def cancellation(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    day = future_weekday(b, s, 2, 20)
    start = b.local(acct, day, 10 + rng.randrange(6), 30 * rng.randrange(2))
    dur = timedelta(minutes=rng.choice([30, 60, 90]))
    title = rng.choice(T.MEETING_KINDS)
    org = organizer_for(b, s, start)
    t_mail = b.when_before_snapshot(rng, 0.5, 8)
    f = datetime_fact("orig", start)
    reason = rng.choice(T.REASONS)
    ref = b.add_event(
        acct,
        title=title,
        start=start,
        end=start + dur,
        attendees=[org],
        organizer=org,
        status="cancelled",
        updated_at=t_mail + timedelta(minutes=10),
        rng=rng,
    )
    out = b.thread(
        acct,
        [
            MsgSpec(
                sender=org,
                to=["user"],
                subject=f"[취소] {title}",
                sent_at=t_mail,
                lines=[f"{f.surface_forms[0]}에 예정이던 {title} 회의는 {reason} 취소합니다."],
                intent=f"{title} 회의가 취소되었음을 알리는 메일",
                required=[f],
            )
        ],
        rng,
    )
    # 같은 제목의 다음 주 일정은 취소되지 않았다
    nxt = start + timedelta(days=7)
    if (nxt.date() - b.snap_day(acct)).days <= MAX_FUTURE_DAYS and b.place_ok(acct, nxt, nxt + dur):
        s.distractors.append(
            b.add_event(
                acct,
                title=title,
                start=nxt,
                end=nxt + dur,
                attendees=[org],
                organizer=org,
                rng=rng,
            )
        )
    s.evidence += [ref, out.messages[0]]
    b.end(
        s,
        CancellationTruth(
            event=ref,
            title=title,
            time_zone=_tz(b, s),
            original=span(start, start + dur),
            cancel_message=out.messages[0],
        ),
    )


def project_week(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    project = take_project(b, s, "project_week")
    members = [m for p in b.wm.projects if p.name == project for m in p.members]
    base = b.snap_day(acct)
    this_monday = base - timedelta(days=base.weekday())
    reserved = b.acc[acct].reserved_days
    n = 3 + rng.randrange(2)
    week = this_monday
    free_days: list[int] = []
    for _ in range(40):
        week = this_monday + timedelta(weeks=rng.randrange(-4, 3))
        free_days = [d for d in range(5) if week + timedelta(days=d) not in reserved]
        if len(free_days) >= n + 1:
            break
    else:
        raise WorldBuildError(f"{acct}: 프로젝트 주간 일정을 놓을 주가 없다")
    days = sorted(rng.sample(free_days, n))
    cancel_day = rng.choice([d for d in free_days if d not in days])
    kinds = rng.sample(T.MEETING_KINDS, n + 1)
    claims = claims_of(b, s)
    shown: list[ArtifactRef] = []
    for i, d in enumerate(days):
        day = week + timedelta(days=d)
        st = b.local(acct, day, 9 + rng.randrange(8), 30 * rng.randrange(2))
        if st >= b.snapshot:
            pool = [m for m in members if m in claims.free_people]
        else:
            pool = [m for m in members if m not in claims.absent_people]
        who = rng.sample(pool, min(2, len(pool)))
        shown.append(
            b.add_event(
                acct,
                title=f"{project} {kinds[i]}",
                start=st,
                end=st + timedelta(minutes=rng.choice([30, 60])),
                calendar=i,
                attendees=sorted(who),
                location=rng.choice(T.LOCATIONS),
                rng=rng,
            )
        )
    # 방해: 같은 프로젝트의 취소된 일정(그 주), 같은 프로젝트의 다른 주 일정
    cst = b.local(acct, week + timedelta(days=cancel_day), 17)
    cancelled = b.add_event(
        acct,
        title=f"{project} {kinds[n]}",
        start=cst,
        end=cst + timedelta(minutes=30),
        status="cancelled",
        rng=rng,
    )
    other_days = [
        week - timedelta(weeks=w) + timedelta(days=d)
        for w in (1, 2, 3)
        for d in range(5)
        if week - timedelta(weeks=w) + timedelta(days=d) not in reserved
    ]
    if not other_days:
        raise WorldBuildError(f"{acct}: 프로젝트 다른 주 일정을 놓을 날이 없다")
    ost = b.local(acct, other_days[0], 11)
    other = b.add_event(
        acct, title=f"{project} 주간 점검", start=ost, end=ost + timedelta(minutes=60), rng=rng
    )
    s.evidence += shown
    s.distractors += [cancelled, other]
    b.end(
        s,
        ProjectWeekTruth(
            project=project,
            week_start=week,
            time_zone=_tz(b, s),
            events=shown,
            cancelled=[cancelled],
        ),
    )


def person_events(
    b: Builder, s: Scn, person_key: str, *, n_upcoming: int
) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
    """인물과의 앞으로의 일정 `n_upcoming`개(확정)와 방해(과거·취소)를 만든다."""
    rng, acct = s.rng, s.account_id
    p = b.person(person_key)
    kinds = rng.sample(T.MEETING_KINDS, n_upcoming + 2)
    as_of_day = s.as_of.astimezone(b.acc[acct].tz).date()
    upcoming: list[ArtifactRef] = []
    cur = as_of_day
    first_day = None
    for i in range(n_upcoming):
        for _ in range(60):
            cur = cur + timedelta(days=1 + rng.randrange(1, 4) if i else 1 + rng.randrange(3))
            while cur.weekday() >= 5:
                cur += timedelta(days=1)
            if (cur - b.snap_day(acct)).days > MAX_FUTURE_DAYS:
                raise WorldBuildError("인물 일정이 4주 범위를 넘었다")
            st = b.local(acct, cur, 10 + rng.randrange(6), 30 * rng.randrange(2))
            if b.place_ok(acct, st, st + timedelta(hours=1)):
                break
        first_day = first_day or cur
        upcoming.append(
            b.add_event(
                acct,
                title=f"{kinds[i]} - {p.name}",
                start=st,
                end=st + timedelta(minutes=rng.choice([30, 60])),
                calendar=i,
                attendees=[person_key],
                location=rng.choice(T.LOCATIONS),
                rng=rng,
            )
        )
    if first_day is None:
        raise WorldBuildError("인물 일정이 없다")
    dist: list[ArtifactRef] = []
    # 첫 일정 날 이른 시각에 취소된 일정(다음 미팅이 아니다)
    cst = b.local(acct, first_day, 9)
    if b.place_ok(acct, cst, cst + timedelta(minutes=30)):
        dist.append(
            b.add_event(
                acct,
                title=f"{kinds[-2]} - {p.name}",
                start=cst,
                end=cst + timedelta(minutes=30),
                attendees=[person_key],
                status="cancelled",
                rng=rng,
            )
        )
    # 과거 일정
    pday = b.pick_day(rng, acct, -25, -3)
    pst = b.local(acct, pday, 10 + rng.randrange(6))
    dist.append(
        b.add_event(
            acct,
            title=f"{kinds[-1]} - {p.name}",
            start=pst,
            end=pst + timedelta(hours=1),
            attendees=[person_key],
            rng=rng,
        )
    )
    return upcoming, dist


def person_next(b: Builder, s: Scn) -> None:
    pkey = next_claim(b, s, "next_people")
    p = b.person(pkey)
    upcoming, dist = person_events(b, s, pkey, n_upcoming=2 + s.rng.randrange(2))
    s.evidence += upcoming
    s.distractors += dist
    b.end(
        s,
        PersonNextTruth(
            person_name=p.name,
            person_email=p.email,
            as_of=s.as_of,
            time_zone=_tz(b, s),
            next_event=upcoming[0],
            upcoming=upcoming,
        ),
    )


def cross_midnight(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    day = b.pick_day(rng, acct, -15, 20, empty=True, span=2)
    st = b.local(acct, day, 22, 30 * rng.randrange(2))
    nxt = day + timedelta(days=1)
    en = b.local(acct, nxt, 1 + rng.randrange(2), 30 * rng.randrange(2))
    if en <= st + timedelta(minutes=30):
        en = b.local(acct, nxt, 1)
    ref = b.add_event(
        acct,
        title=rng.choice(T.NIGHT_TITLES),
        start=st,
        end=en,
        attendees=attendees_for(b, s, st, 1),
        location="화상 회의",
        claim=True,
        rng=rng,
    )
    b.reserve(acct, [day, nxt])
    s.evidence.append(ref)
    b.end(
        s,
        CrossMidnightTruth(
            event=ref, time_zone=_tz(b, s), span=span(st, en), start_day=day, end_day=nxt
        ),
    )


def tz_event(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    user_tz = _tz(b, s)
    if s.variant == "local":
        # 2026-11-01 일광 절약 시간 종료 전후의 현지 일정(UTC 시각이 같은 현지 시각에서도 달라진다)
        other = "Asia/Seoul"
        target = date(b.snap_day(acct).year, 11, 1)
        n_local = b.used.setdefault("tz_local_count", set())
        before = len(n_local) % 2 == 0  # 전환일 전(여름 시간)과 후(표준시)를 번갈아 쓴다
        n_local.add(s.scenario_id)
        cands = [
            d
            for d in b.weekdays(acct, -60, 27)
            if abs((d - target).days) <= 14
            and (d < target) == before
            and b.place_ok(acct, b.local(acct, d, 8), b.local(acct, d, 11))
        ]
        if not cands:
            raise WorldBuildError(f"{acct}: 일광 절약 시간 전환일 근처에 놓을 날짜가 없다")
        day = cands[rng.randrange(len(cands))]
        title = f"본사 {rng.choice(T.MEETING_KINDS)}"
        desc_prefix = "서울 본사 시각 기준 "
    else:
        other = "America/New_York"
        day = future_weekday(b, s, 2, 25) if rng.random() < 0.5 else b.pick_day(rng, acct, -20, -2)
        title = f"뉴욕 지사 {rng.choice(T.MEETING_KINDS)}"
        desc_prefix = "뉴욕 현지 시각 "
    hour = (8 + rng.randrange(3)) if s.variant == "local" else (21 + rng.randrange(2))
    st = b.local(acct, day, hour, 30 * rng.randrange(2))
    en = st + timedelta(hours=1)
    other_start = st.astimezone(ZoneInfo(other))
    ref = b.add_event(
        acct,
        title=title,
        start=st,
        end=en,
        attendees=attendees_for(b, s, st, 1),
        location="화상 회의",
        description=f"{desc_prefix}{other_start:%m월 %d일 %H:%M}",
        rng=rng,
    )
    s.evidence.append(ref)
    b.end(
        s,
        TzEventTruth(
            variant="local" if s.variant == "local" else "foreign_attendee",
            event=ref,
            user_time_zone=user_tz,
            other_time_zone=other,
            span=span(st, en),
            start_in_other_zone=other_start,
            user_utc_offset_hours=(st.utcoffset() or timedelta(0)).total_seconds() / 3600,
        ),
    )


def all_day(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    day = b.pick_day(rng, acct, -15, 20, empty=True, span=3)
    # 계약 해석(ADR-0027): 종일 일정은 캘린더 시간대 00:00~다음 날 00:00이다.
    start = b.local(acct, day, 0)
    end = b.local(acct, day + timedelta(days=1), 0)
    # 배치 규칙은 UTC 경계 해석일 때와 같게 둔다(`all_day_footprint`). 앞뒤 날도 예약한다.
    days = b.event_days(acct, *b.all_day_footprint(day))
    ref = b.add_event(
        acct,
        title=rng.choice(T.ALL_DAY_TITLES),
        start=start,
        end=end,
        all_day=True,
        claim=True,
        rng=rng,
    )
    b.reserve(acct, [days[0] - timedelta(days=1), *days, days[-1] + timedelta(days=1)])
    s.evidence.append(ref)
    b.end(
        s,
        AllDayTruth(event=ref, intended_date=day, span=span(start, end), time_zone=_tz(b, s)),
    )
