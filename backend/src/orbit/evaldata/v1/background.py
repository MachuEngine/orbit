"""배경 채우기: 반복 일정·잡다한 일정, 배경 메일 스레드(업무·뉴스레터·공지·스팸), 배경 문서.

배경 아티팩트는 정답 근거가 아니다. 시나리오가 예약한 날짜와 인물 배정을 지킨다
(예약된 날에는 일정을 놓지 않고, 일정이 없어야 하는 인물은 참석자로 쓰지 않는다).
배경 본문 명세에는 필수 사실이 없다.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

from orbit.evaldata.v1 import templates as T
from orbit.evaldata.v1.core import Builder, MsgSpec, WorldBuildError
from orbit.evaldata.v1.worldmodel import User


def _free(b: Builder, user: User) -> list[str]:
    return sorted(b.claims[user.key].free_people)


def _any(b: Builder, user: User) -> list[str]:
    absent = set(b.claims[user.key].absent_people)
    return sorted(p.key for p in b.wm.contacts_of(user.key) if p.key not in absent)


# ---------------------------------------------------------------- 일정


def add_recurring(b: Builder, user: User) -> None:
    acct = user.account_id
    rng = b.rng(f"recurring:{user.key}")
    base = b.snap_day(acct)
    first = base - timedelta(weeks=b.wm.window.event_weeks_before)
    last = base + timedelta(weeks=b.wm.window.event_weeks_after) - timedelta(days=1)
    free = _free(b, user)
    for r in (x for x in b.wm.recurring if x.user == user.key):
        who = sorted(rng.sample(free, min(r.attendee_count, len(free))))
        hh, mm = (int(x) for x in r.start.split(":"))
        day = first + timedelta(days=(r.weekday - first.weekday()) % 7)
        while day <= last:
            st = b.local(acct, day, hh, mm)
            en = st + timedelta(minutes=r.duration_min)
            if b.place_ok(acct, st, en):
                b.add_event(
                    acct,
                    title=r.title,
                    start=st,
                    end=en,
                    calendar=0,
                    attendees=who,
                    location=r.location,
                    rng=rng,
                )
            day += timedelta(weeks=r.every_weeks)


def add_background_events(b: Builder, user: User) -> None:
    acct = user.account_id
    rng = b.rng(f"bgevents:{user.key}")
    n_cal = len(b.acc[acct].calendars)
    free, anyp = _free(b, user), _any(b, user)
    made = 0
    guard = 0
    while made < b.wm.targets.background_events_per_user:
        guard += 1
        if guard > 5000:
            raise WorldBuildError(f"{user.key}: 배경 일정을 놓을 자리가 없다")
        lo = -7 * b.wm.window.event_weeks_before
        hi = 7 * b.wm.window.event_weeks_after - 1
        days = b.weekdays(acct, lo, hi)
        day = days[rng.randrange(len(days))]
        st = b.local(acct, day, 9 + rng.randrange(9), 30 * rng.randrange(2))
        en = st + timedelta(minutes=rng.choice([30, 60, 60, 90]))
        if not b.place_ok(acct, st, en):
            continue
        pool = free if st >= b.snapshot else anyp
        k = rng.choice([0, 0, 1, 1, 2])
        who = sorted(rng.sample(pool, min(k, len(pool))))
        b.add_event(
            acct,
            title=rng.choice(T.GENERIC_TITLES),
            start=st,
            end=en,
            calendar=rng.randrange(n_cal),
            attendees=who,
            location=rng.choice(T.LOCATIONS) if rng.random() < 0.6 else "",
            status="cancelled" if rng.random() < 0.08 else "confirmed",
            rng=rng,
        )
        made += 1
    made = 0
    guard = 0
    while made < b.wm.targets.background_all_day_per_user:
        guard += 1
        if guard > 2000:
            raise WorldBuildError(f"{user.key}: 배경 종일 일정을 놓을 자리가 없다")
        days = b.weekdays(acct, -40, 25)
        day = days[rng.randrange(len(days))]
        # 캘린더 시간대 00:00(ADR-0027). 이 세계의 캘린더 시간대는 사용자 시간대와 같다.
        start = b.local(acct, day, 0)
        end = b.local(acct, day + timedelta(days=1), 0)
        if not b.place_ok(acct, *b.all_day_footprint(day)):
            continue
        b.add_event(
            acct,
            title=rng.choice(T.ALL_DAY_TITLES),
            start=start,
            end=end,
            all_day=True,
            rng=rng,
        )
        made += 1


# ---------------------------------------------------------------- 메일


def _business_hours(b: Builder, acct: str, moment: datetime, rng: random.Random) -> datetime:
    """시각을 80% 확률로 현지 08~20시로 옮긴다(날짜는 유지)."""
    tz = b.acc[acct].tz
    loc = moment.astimezone(tz)
    if rng.random() < 0.8 and not 8 <= loc.hour < 20:
        loc = loc.replace(hour=8 + rng.randrange(12), minute=rng.randrange(60))
    return loc.astimezone(UTC)


def fill_mail(
    b: Builder,
    accts: list[str],
    need: int,
    *,
    rng: random.Random,
    group: int | None = None,
) -> int:
    """`accts` 모두에 같은 배경 스레드를 넣어 메일을
    정확히 `need`통 채운다. 만든 통수를 돌려준다."""
    first = accts[0]
    user = b.acc[first].user
    contacts = _any(b, user)
    owners = {u.key for u in b.wm.search_users()} if user.purpose == "search" else {user.key}
    quiet = b.claims[user.key].quiet_project if user.key in b.claims else None
    projects = [p for p in b.wm.projects if p.user in owners and p.name != quiet]
    senders = {
        k: [n for n in b.wm.noise_senders if n.kind == k] for k in ("newsletter", "notice", "spam")
    }
    margin = timedelta(hours=14)  # 근무 시간 보정(±12시간)이 기간 밖으로 나가지 않게 둔다
    window_start = b.snapshot - timedelta(weeks=b.wm.window.mail_weeks) + margin
    remaining = need
    made = 0
    while remaining > 0:
        roll = rng.random()
        if roll < 0.72:
            kind = "business"
        elif roll < 0.84:
            kind = "newsletter"
        elif roll < 0.94:
            kind = "notice"
        else:
            kind = "spam"
        if kind == "business":
            size = min(
                remaining,
                rng.choices([1, 2, 3, 4, 5, 6], weights=[50, 25, 12, 7, 4, 2])[0],
            )
            partner = contacts[rng.randrange(len(contacts))]
            proj = (
                projects[rng.randrange(len(projects))] if projects and rng.random() < 0.7 else None
            )
            topic = (
                proj.topics[rng.randrange(len(proj.topics))]
                if proj is not None
                else rng.choice(["업무 일정 공유", "자료 요청", "진행 확인"])
            )
            subj = f"[{proj.name}] {topic}" if proj is not None else topic
            gaps = [timedelta(minutes=20 + rng.randrange(36 * 60)) for _ in range(size - 1)]
            span_ = sum(gaps, timedelta())
            latest_start = b.snapshot - span_ - margin
            total = (latest_start - window_start).total_seconds()
            t = window_start + timedelta(seconds=rng.random() * total)
            t = _business_hours(b, first, t, rng)
            starts_user = rng.random() < 0.4
            specs: list[MsgSpec] = []
            for i in range(size):
                if i > 0:
                    t = t + gaps[i - 1]
                from_user = starts_user == (i % 2 == 0)
                sender, to = ("user", [partner]) if from_user else (partner, ["user"])
                specs.append(
                    MsgSpec(
                        sender=sender,
                        to=to,
                        subject=subj if i == 0 else f"Re: {subj}",
                        sent_at=t,
                        lines=T.business_lines(rng, topic, 1 + rng.randrange(2)),
                        intent=f"{topic}에 관한 일상적인 업무 메일",
                        project=proj.name if proj is not None else None,
                        topic=topic,
                    )
                )
            # 시각 보정: 순서를 지키고 스냅샷 이전으로 맞춘다
            for i in range(1, size):
                if not specs[i - 1].sent_at < specs[i].sent_at:
                    specs[i].sent_at = specs[i - 1].sent_at + timedelta(minutes=5)
        else:
            size = 1
            pool = senders[kind]
            ns = pool[rng.randrange(len(pool))]
            t = window_start + timedelta(
                seconds=rng.random()
                * (b.snapshot - timedelta(hours=1) - window_start).total_seconds()
            )
            subj = {
                "newsletter": f"[{ns.name}] 이번 주 소식",
                "notice": f"[공지] {ns.name} 안내",
                "spam": f"[광고] {ns.name}",
            }[kind]
            specs = [
                MsgSpec(
                    sender=ns.email,
                    to=["user"],
                    subject=subj,
                    sent_at=t,
                    lines=T.compose_auto(rng, kind, ns.name),
                    intent={
                        "newsletter": "정기 뉴스레터(여러 소식을 소개한다)",
                        "notice": "자동 발송되는 공지·알림",
                        "spam": "원치 않는 광고성 메일",
                    }[kind],
                )
            ]
        out = b.add_thread(accts, specs, rng=rng)
        if group is not None:
            for ref in out[first].messages:
                b.mail_group_of_body[ref.body_id or ""] = group
        remaining -= size
        made += size
    return made


# ---------------------------------------------------------------- 문서

_NOTE_TOPICS = ["정기 운영 점검", "분기 일정 조율", "고객 요구사항 정리", "인력 배치"]


def fill_docs(b: Builder, accts: list[str], need: int, *, rng: random.Random) -> None:
    first = accts[0]
    user = b.acc[first].user
    contacts = _any(b, user)
    for _ in range(need):
        genre = rng.choice(["meeting_notes", "report", "memo", "memo", "plan"])
        for _attempt in range(200):
            d = b.weekdays(first, -50, -1)
            day = d[rng.randrange(len(d))]
            if genre == "meeting_notes":
                title = f"{rng.choice(T.MEETING_KINDS)} 회의록 ({day.month}월 {day.day}일)"
            elif genre == "report":
                title = f"{rng.choice(T.DOC_REPORT_TITLES)} ({day.month}월)"
            elif genre == "memo":
                title = f"{rng.choice(T.DOC_MEMO_TITLES)} ({day.month}월 {day.day}일)"
            else:
                title = f"팀 운영 계획서 ({day.month}월 {day.day}일 기준)"
            if all(title not in b.acc[a].titles for a in accts):
                break
        else:
            raise WorldBuildError("배경 문서 이름을 정하지 못했다")
        owner = "user" if rng.random() < 0.4 else contacts[rng.randrange(len(contacts))]
        topic = rng.choice(_NOTE_TOPICS)
        b.add_doc(
            accts,
            title=title,
            genre=genre,
            modified_at=b.local(first, day, 9 + rng.randrange(9)).astimezone(UTC),
            owner=owner,
            lines=T.business_lines(rng, topic, 3),
            intent=f"{topic}에 관한 일반 문서({genre})",
            topic=topic,
            doc_date=date(day.year, day.month, day.day),
        )
