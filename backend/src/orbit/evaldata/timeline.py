"""사건 타임라인 생성 (시드 고정). 시나리오가 일정과 메일을 만들고 과제 명세를 돌려준다.

- 일정·메일의 날짜, ID, 발신자 같은 구조 필드는 모두 코드가 채운다. 본문만 템플릿 문장이다.
- 모든 일정은 평일 09:00~18:00(KST), 30분 단위다 (`timeutil` 한계 참고).
- 모든 메일과 일정 갱신은 스냅샷 시각(`window.snapshot`) 이전이다.
- 시나리오는 4종이다: agenda(하루 일정), reschedule(메일로 시간 변경), cancel(취소와 비슷한 제목),
  projperson(프로젝트 주간 회의와 인물). 시나리오 하나가 만든 과제는 같은 split에 들어간다.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from orbit.evaldata import texts
from orbit.evaldata.records import Contact, EventRec, MailRec, TaskSpec
from orbit.evaldata.timeutil import at_local, half_of, kor_when, local_date, week_start
from orbit.evaldata.worldmodel import Person, Project, User, WorldModel

SCENARIO_FOR = {
    "day_schedule": "agenda",
    "free_slot": "agenda",
    "reschedule": "reschedule",
    "cancelled_exclusion": "cancel",
    "project_week": "projperson",
    "person_next": "projperson",
}


class Builder:
    def __init__(self, wm: WorldModel, rng: random.Random) -> None:
        self.wm = wm
        self.rng = rng
        self.events: list[EventRec] = []
        self.mails: list[MailRec] = []
        self._ev_n = 0
        self._ver_n = 0
        self._mail_n = {u.key: 0 for u in wm.users}
        self._thr_n = {u.key: 0 for u in wm.users}
        self._scn_n: dict[str, int] = {}
        self._user_rr: dict[str, int] = {}
        self.pp_overrides: list[tuple[Project, Person]] = []

    # ------------------------------------------------------------ 기본 도구

    def contact(self, key: str) -> Contact:
        for u in self.wm.users:
            if u.key == key:
                return (u.name, u.email)
        p = self.wm.person(key)
        return (p.name, p.email)

    def next_scenario_id(self, kind: str) -> str:
        self._scn_n[kind] = self._scn_n.get(kind, 0) + 1
        return f"scn-{kind}-{self._scn_n[kind]:03d}"

    def next_user(self, kind: str) -> User:
        n = self._user_rr.get(kind, 0)
        self._user_rr[kind] = n + 1
        return self.wm.users[n % len(self.wm.users)]

    def new_thread(self, user: str) -> str:
        self._thr_n[user] += 1
        return f"thr-{user}-{self._thr_n[user]:04d}"

    def new_version(self) -> str:
        self._ver_n += 1
        return f"v{self._ver_n:05d}"

    def user_events(self, user: str) -> list[EventRec]:
        return [e for e in self.events if e.user == user]

    def rand_time(self, lo: datetime, hi: datetime) -> datetime:
        """[lo, hi] 안의 업무 시간대 시각."""
        if hi <= lo:
            return lo
        days = (local_date(hi) - local_date(lo)).days
        day = local_date(lo) + timedelta(days=self.rng.randint(0, days))
        if day.weekday() >= 5:  # 주말이면 직전 금요일로 당긴다
            day -= timedelta(days=day.weekday() - 4)
        cand = at_local(day, self.rng.randint(9, 18), self.rng.choice([0, 10, 20, 30, 40, 50]))
        return min(max(cand, lo), hi)

    def sorted_times(self, n: int, lo: datetime, hi: datetime) -> list[datetime]:
        times = sorted(self.rand_time(lo, hi) for _ in range(n))
        for i in range(1, len(times)):
            if times[i] <= times[i - 1]:
                times[i] = times[i - 1] + timedelta(minutes=30)
        return times

    def mail_lo(self) -> datetime:
        return at_local(self.wm.window.mail_start, 9)

    def mail_hi(self) -> datetime:
        return self.wm.window.snapshot - timedelta(hours=1)

    # ------------------------------------------------------------ 일정 배치

    def free_start(
        self, user: str, day: date, dur_min: int, lo_min: int = 9 * 60, hi_min: int = 18 * 60
    ) -> datetime | None:
        """그 날 겹치지 않는 시작 시각을 30분 단위로 무작위 선택한다. 없으면 None."""
        busy = [(e.start, e.end) for e in self.user_events(user) if local_date(e.start) == day]
        cands: list[datetime] = []
        for minute in range(lo_min, hi_min - dur_min + 1, 30):
            start = at_local(day, minute // 60, minute % 60)
            end = start + timedelta(minutes=dur_min)
            if all(end <= b0 or start >= b1 for b0, b1 in busy):
                cands.append(start)
        return self.rng.choice(cands) if cands else None

    def weekday_between(self, lo: date, hi: date) -> date | None:
        days = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
        days = [d for d in days if d.weekday() < 5]
        return self.rng.choice(days) if days else None

    def add_event(
        self,
        user: str,
        title: str,
        start: datetime,
        dur_min: int,
        attendees: list[str],
        organizer: str,
        scenario_id: str,
        *,
        project: str | None = None,
        location: str | None = None,
    ) -> EventRec:
        self._ev_n += 1
        ev = EventRec(
            id=f"ev{self._ev_n:06d}",
            user=user,
            title=title,
            start=start,
            end=start + timedelta(minutes=dur_min),
            attendees=attendees,
            organizer=organizer,
            location=location or texts.pick(self.rng, texts.LOCATIONS),
            description=f"{title} 논의",
            version=self.new_version(),
            updated_at=self.mail_lo(),
            scenario_id=scenario_id,
            project=project,
        )
        self.events.append(ev)
        return ev

    def touch(self, ev: EventRec, at: datetime) -> None:
        ev.version = self.new_version()
        ev.updated_at = at

    def add_mail(
        self,
        user: str,
        thread: str,
        sender: Contact,
        to: list[Contact],
        subject: str,
        body: str,
        sent_at: datetime,
        labels: list[str],
        kind: str,
        scenario_id: str,
        event: EventRec | None = None,
    ) -> MailRec:
        self._mail_n[user] += 1
        mail = MailRec(
            id=f"msg-{user}-{self._mail_n[user]:04d}",
            thread=thread,
            user=user,
            sender=sender,
            to=to,
            subject=subject,
            body=body,
            sent_at=sent_at,
            labels=labels,
            kind=kind,
            scenario_id=scenario_id,
            event_id=event.id if event else None,
        )
        self.mails.append(mail)
        if event is not None:
            event.msg_ids.append(mail.id)
        return mail

    def invite_mail(self, ev: EventRec, sent_at: datetime, *, thread: str | None = None) -> MailRec:
        """일정 초대 메일. 주최자가 인물이면 받은 편지함, 사용자면 보낸 편지함 메일이다."""
        me = self.contact(ev.user)
        when = kor_when(ev.start)
        if ev.organizer == ev.user:
            other = self.contact(ev.attendees[0])
            subject, body = texts.invite(self.rng, other[0], me[0], ev.title, when, ev.location)
            sender, to, labels = me, [other], ["SENT"]
        else:
            org = self.contact(ev.organizer)
            subject, body = texts.invite(self.rng, me[0], org[0], ev.title, when, ev.location)
            sender, to, labels = org, [me], ["INBOX"]
        return self.add_mail(
            ev.user,
            thread or self.new_thread(ev.user),
            sender,
            to,
            subject,
            body,
            sent_at,
            labels,
            "invite",
            ev.scenario_id,
            ev,
        )

    # ------------------------------------------------------------ 제목·날짜 선택

    def project_title(self, user: str, project: Project) -> str:
        """프로젝트·주제·회의 종류로 사용자 일정 안에서 유일한 제목 기반을 만든다."""
        used = [e.title for e in self.user_events(user)]
        combos = [(t, k) for t in project.topics for k in texts.MEETING_KINDS]
        self.rng.shuffle(combos)
        for topic, kind in combos:
            base = f"{project.name} {topic} {kind}"
            if not any(u.startswith(base) for u in used):
                return base
        raise RuntimeError(f"{project.key}의 제목 조합이 소진됐다")

    def generic_title(self, user: str, day: date) -> str:
        used = {e.title for e in self.user_events(user) if local_date(e.start) == day}
        options = [t for t in texts.GENERIC_TITLES if t not in used]
        return texts.pick(self.rng, options)

    def pick_asof(self) -> datetime:
        w = self.wm.window
        days = [
            w.asof_start + timedelta(days=i) for i in range((w.asof_end - w.asof_start).days + 1)
        ]
        days = [d for d in days if d.weekday() < 5]
        day = self.rng.choice(days)
        hour, minute = self.rng.choice([(8, 30), (9, 0), (9, 30)])
        return at_local(day, hour, minute)

    def pick_target(self, asof: datetime) -> tuple[date, bool] | None:
        """as_of 이후의 평일 목표 날짜. (날짜, 명시적 날짜로 말할지)."""
        a = local_date(asof)
        options: list[tuple[date, bool]] = []
        if a.weekday() < 4:
            options.append((a + timedelta(days=1), False))  # 내일
        next_mon = week_start(a) + timedelta(days=7)
        options.append((next_mon + timedelta(days=self.rng.randrange(5)), False))  # 다음 주 N요일
        if a.weekday() <= 2:
            later = self.rng.randint(2, 4 - a.weekday())
            options.append((a + timedelta(days=later), False))  # 이번 주 N요일
        explicit = self.weekday_between(a + timedelta(days=1), self.wm.window.event_end)
        if explicit is not None:
            options.append((explicit, True))
        target, is_explicit = self.rng.choice(options)
        if target > self.wm.window.event_end:
            return None
        return target, is_explicit

    def pick_attendees(self, user: str, count: int, pool: list[str] | None = None) -> list[str]:
        keys = pool or [p.key for p in self.wm.people if user in p.works_with]
        return self.rng.sample(keys, min(count, len(keys)))

    def invite_window(self, start: datetime) -> tuple[datetime, datetime]:
        lo = max(self.mail_lo(), start - timedelta(days=12))
        hi = min(self.mail_hi(), start - timedelta(hours=20))
        return lo, hi

    # ------------------------------------------------------------ 반복 일정

    def add_recurring(self) -> None:
        w = self.wm.window
        for rec in self.wm.recurring:
            sid = f"scn-recurring-{rec.user}"
            first = w.event_start + timedelta(days=(rec.weekday - w.event_start.weekday()) % 7)
            hh, mm = (int(x) for x in rec.start.split(":"))
            events: list[EventRec] = []
            day = first
            while day <= w.event_end:
                events.append(
                    self.add_event(
                        rec.user,
                        rec.title,
                        at_local(day, hh, mm),
                        rec.duration_min,
                        list(rec.attendees),
                        rec.user,
                        sid,
                        location=rec.location,
                    )
                )
                day += timedelta(weeks=rec.every_weeks)
            me = self.contact(rec.user)
            first_att = self.contact(rec.attendees[0])
            rule = f"격주 {'월화수목금'[rec.weekday]}요일 {hh:02d}:{mm:02d}"
            body = texts.series_notice(self.rng, first_att[0], me[0], rec.title, rule)
            sent = at_local(w.mail_start, 8, 30)
            mail = self.add_mail(
                rec.user,
                self.new_thread(rec.user),
                me,
                [first_att],
                f"[반복 일정] {rec.title}",
                body,
                sent,
                ["SENT"],
                "series",
                sid,
            )
            for ev in events:
                ev.msg_ids.append(mail.id)


# ---------------------------------------------------------------- 시나리오


def scn_agenda(b: Builder, user: User) -> list[TaskSpec]:
    """하루에 일정 2개. 오전·오후 조회와 가장 이른 빈 시간 과제를 만든다."""
    asof = b.pick_asof()
    picked = b.pick_target(asof)
    if picked is None:
        return []
    target, explicit = picked
    sid = b.next_scenario_id("agenda")
    first_start = b.free_start(user.key, target, 60, 9 * 60, 11 * 60 + 30)
    if first_start is None:
        return []
    first = _agenda_event(b, user, target, first_start, 60, sid)
    dur = b.rng.choice([30, 60, 90])
    second_start = b.free_start(user.key, target, dur)
    if second_start is None:
        b.events.remove(first)
        return []
    second = _agenda_event(b, user, target, second_start, dur, sid)
    for ev in (first, second):
        lo, hi = b.invite_window(ev.start)
        b.touch(ev, b.invite_mail(ev, b.rand_time(lo, hi)).sent_at)
    halves = sorted({_half(ev.start) for ev in (first, second)})
    half = b.rng.choice([None, *halves])
    params = {"target": target.isoformat(), "explicit": explicit, "half": half}
    return [
        TaskSpec("day_schedule", sid, user.key, asof, dict(params)),
        TaskSpec(
            "free_slot", sid, user.key, asof, {"target": params["target"], "explicit": explicit}
        ),
    ]


def _half(start: datetime) -> str:
    return half_of(start)


def _agenda_event(
    b: Builder, user: User, day: date, start: datetime, dur: int, sid: str
) -> EventRec:
    attendees = b.pick_attendees(user.key, b.rng.choice([1, 2]))
    organizer = b.rng.choice([attendees[0], user.key])
    return b.add_event(
        user.key, b.generic_title(user.key, day), start, dur, attendees, organizer, sid
    )


def scn_reschedule(b: Builder, user: User) -> list[TaskSpec]:
    """메일 요청으로 일정 시간이 1~2번 바뀐다. 캘린더에는 최신 시간이 반영돼 있다."""
    w = b.wm.window
    asof = b.pick_asof()
    a = local_date(asof)
    floor = w.asof_start + timedelta(days=1)
    final_day = b.weekday_between(a + timedelta(days=1), min(a + timedelta(days=9), w.event_end))
    if final_day is None:
        return []
    changes = b.rng.choice([1, 1, 2])
    dur = b.rng.choice([30, 60, 90])
    final = b.free_start(user.key, final_day, dur)
    if final is None:
        return []
    projects = [p for p in b.wm.projects if p.user == user.key]
    project = b.rng.choice(projects)
    person = b.wm.person(b.rng.choice(project.members))
    sid = b.next_scenario_id("reschedule")
    # 과거 시각: 최종 시각과 서로 다른 시각을 날짜 범위 안에서 고른다.
    times: list[datetime] = []
    for _ in range(changes):
        for _try in range(20):
            day = b.weekday_between(
                max(floor, final_day - timedelta(days=3)), final_day + timedelta(days=3)
            )
            if day is None or day > w.event_end:
                continue
            cand = at_local(day, b.rng.randint(9, 16), b.rng.choice([0, 30]))
            if cand != final and cand not in times:
                times.append(cand)
                break
        else:
            return []
    path = [*times, final]  # path[0]은 처음 초대 시각, path[-1]은 최종
    title = b.project_title(user.key, project)
    ev = b.add_event(
        user.key, title, path[0], dur, [person.key], person.key, sid, project=project.key
    )
    ev.attendees = [
        person.key,
        *b.pick_attendees(user.key, 1, [m for m in project.members if m != person.key]),
    ]
    lo = max(b.mail_lo(), min(path) - timedelta(days=12))
    stamps = b.sorted_times(changes + 1, lo, b.mail_hi() - timedelta(hours=4))
    me, other = b.contact(user.key), b.contact(person.key)
    thread = b.new_thread(user.key)
    invite = b.invite_mail(ev, stamps[0], thread=thread)
    last = invite.sent_at
    for i in range(changes):
        body = texts.change_request(
            b.rng, me[0], other[0], title, kor_when(path[i]), kor_when(path[i + 1])
        )
        req = b.add_mail(
            user.key,
            thread,
            other,
            [me],
            f"Re: {invite.subject}",
            body,
            stamps[i + 1],
            ["INBOX"],
            "change",
            sid,
            ev,
        )
        reply_at = min(req.sent_at + timedelta(hours=b.rng.randint(1, 3)), b.mail_hi())
        reply = b.add_mail(
            user.key,
            thread,
            me,
            [other],
            f"Re: {invite.subject}",
            texts.confirm_change(b.rng, other[0], me[0], title, kor_when(path[i + 1])),
            reply_at,
            ["SENT"],
            "confirm",
            sid,
            ev,
        )
        last = reply.sent_at
        b.touch(ev, last)
    ev.start, ev.end = final, final + timedelta(minutes=dur)
    ev.changes = changes
    return [TaskSpec("reschedule", sid, user.key, asof, {"event_id": ev.id})]


def scn_cancel(b: Builder, user: User) -> list[TaskSpec]:
    """취소된 일정(1차)과 비슷한 제목의 유효한 일정(2차)이 같은 날, 같은 오전·오후에 있다."""
    asof = b.pick_asof()
    picked = b.pick_target(asof)
    if picked is None:
        return []
    target, explicit = picked
    half = b.rng.choice(["오전", "오후"])
    lo_min, hi_min = (9 * 60, 12 * 60) if half == "오전" else (13 * 60, 18 * 60)
    project = b.rng.choice([p for p in b.wm.projects if p.user == user.key])
    person = b.wm.person(b.rng.choice(project.members))
    sid = b.next_scenario_id("cancel")
    base = b.project_title(user.key, project)
    s1 = b.free_start(user.key, target, 60, lo_min, hi_min)
    if s1 is None:
        return []
    cancelled = b.add_event(
        user.key, f"{base} 1차", s1, 60, [person.key], person.key, sid, project=project.key
    )
    s2 = b.free_start(user.key, target, 60, lo_min, hi_min)
    if s2 is None:
        b.events.remove(cancelled)
        return []
    kept = b.add_event(
        user.key, f"{base} 2차", s2, 60, [person.key], person.key, sid, project=project.key
    )
    for ev in (cancelled, kept):
        lo, hi = b.invite_window(ev.start)
        b.touch(ev, b.invite_mail(ev, b.rand_time(lo, hi)).sent_at)
    inv_at = max(m.sent_at for m in b.mails if m.id in cancelled.msg_ids)
    me, other = b.contact(user.key), b.contact(person.key)
    subject, body = texts.cancel_notice(
        b.rng,
        me[0],
        other[0],
        cancelled.title,
        kor_when(cancelled.start),
        texts.pick(b.rng, texts.REASONS),
    )
    cancel_at = b.rand_time(inv_at + timedelta(hours=1), b.mail_hi())
    b.add_mail(
        user.key,
        b.new_thread(user.key),
        other,
        [me],
        subject,
        body,
        cancel_at,
        ["INBOX"],
        "cancel",
        sid,
        cancelled,
    )
    cancelled.status = "cancelled"
    b.touch(cancelled, cancel_at)
    scope = b.rng.choice([None, half])
    return [
        TaskSpec(
            "cancelled_exclusion",
            sid,
            user.key,
            asof,
            {"target": target.isoformat(), "explicit": explicit, "half": scope},
        )
    ]


def scn_projperson(b: Builder, user: User) -> list[TaskSpec]:
    """프로젝트 회의 2개가 같은 주에 있고 같은 인물이 참석한다."""
    w = b.wm.window
    overridden = bool(b.pp_overrides)
    if overridden:
        project, person = b.pp_overrides[0]
        user = b.wm.user(project.user)
    else:
        project = b.rng.choice([p for p in b.wm.projects if p.user == user.key])
        person = b.wm.person(b.rng.choice(project.members))
    asof = b.pick_asof()
    a = local_date(asof)
    this_week = a.weekday() <= 2 and b.rng.random() < 0.4
    if this_week:
        pool = [a + timedelta(days=i) for i in range(1, 5 - a.weekday())]
    else:
        monday = week_start(a) + timedelta(days=7)
        pool = [monday + timedelta(days=i) for i in range(5)]
    pool = [d for d in pool if d <= w.event_end]
    if len(pool) < 2:
        return []
    days = sorted(b.rng.sample(pool, 2))
    sid = b.next_scenario_id("projperson")
    created: list[EventRec] = []
    for day in days:
        dur = b.rng.choice([30, 60, 90])
        start = b.free_start(user.key, day, dur)
        if start is None:
            for ev in created:
                b.events.remove(ev)
            return []
        extra = b.pick_attendees(user.key, 1, [m for m in project.members if m != person.key])
        created.append(
            b.add_event(
                user.key,
                b.project_title(user.key, project),
                start,
                dur,
                [person.key, *extra],
                b.rng.choice([person.key, user.key]),
                sid,
                project=project.key,
            )
        )
    if overridden:
        b.pp_overrides.pop(0)
    for ev in created:
        lo, hi = b.invite_window(ev.start)
        b.touch(ev, b.invite_mail(ev, b.rand_time(lo, hi)).sent_at)
    week = "this" if this_week else "next"
    return [
        TaskSpec("project_week", sid, user.key, asof, {"project": project.key, "week": week}),
        TaskSpec("person_next", sid, user.key, asof, {"person": person.key}),
    ]


SCENARIO_FUNCS = {
    "agenda": scn_agenda,
    "reschedule": scn_reschedule,
    "cancel": scn_cancel,
    "projperson": scn_projperson,
}


def run_scenarios(b: Builder, scenario: str, count: int) -> list[TaskSpec]:
    """시나리오를 `count`개 만든다. 배치에 실패하면 새로 시도한다."""
    specs: list[TaskSpec] = []
    made = 0
    attempts = 0
    while made < count:
        attempts += 1
        if attempts > count * 50 + 50:
            raise RuntimeError(f"시나리오 {scenario}를 만들지 못했다")
        user = b.next_user(scenario)
        got = SCENARIO_FUNCS[scenario](b, user)
        if got:
            specs.extend(got)
            made += 1
    return specs


# ---------------------------------------------------------------- 노이즈


def add_noise(b: Builder) -> None:
    """뉴스레터·광고·알림, 마감 공지, 비슷한 제목의 후속 메일을 추가한다."""
    wm, rng, t = b.wm, b.rng, b.wm.targets
    senders = {
        k: [s for s in wm.noise_senders if s.kind == k] for k in ("newsletter", "spam", "notice")
    }
    plan = (
        ("newsletter", t.newsletters_per_user, ["INBOX", "CATEGORY_PROMOTIONS"]),
        ("spam", t.spam_per_user, ["SPAM"]),
        ("notice", t.notices_per_user, ["INBOX"]),
    )
    for user in wm.users:
        me = (user.name, user.email)
        for kind, per_user, labels in plan:
            for _ in range(per_user):
                s = rng.choice(senders[kind])
                if kind == "newsletter":
                    subject, body = texts.newsletter(rng, rng.randint(20, 120))
                elif kind == "spam":
                    subject, body = texts.spam(rng)
                else:
                    subject, body = texts.notice(rng)
                b.add_mail(
                    user.key,
                    b.new_thread(user.key),
                    (s.name, s.email),
                    [me],
                    subject,
                    body,
                    b.rand_time(b.mail_lo(), b.mail_hi()),
                    list(labels),
                    kind,
                    f"noise-{kind}",
                )
    # 마감 공지: 프로젝트 담당 사용자에게 멤버가 보낸다. 일정이 아니다.
    for _ in range(t.deadlines):
        project = rng.choice(wm.projects)
        sender = b.contact(rng.choice(project.members))
        due = b.weekday_between(wm.window.asof_start, wm.window.event_end)
        if due is None:
            raise RuntimeError("마감 날짜를 고를 수 없다")
        subject, body = texts.deadline(
            rng, project.name, texts.pick(rng, texts.DEADLINE_ITEMS), f"{due.month}월 {due.day}일"
        )
        b.add_mail(
            project.user,
            b.new_thread(project.user),
            sender,
            [b.contact(project.user)],
            subject,
            body,
            b.rand_time(b.mail_lo(), b.mail_hi()),
            ["INBOX"],
            "deadline",
            "noise-deadline",
        )
    # 비슷한 제목의 후속 메일: 해당 일정 스레드에 시각 없이 이어진다.
    candidates = [e for e in b.events if e.msg_ids and e.status == "confirmed" and e.project]
    rng.shuffle(candidates)
    made = 0
    for ev in candidates:
        if made >= t.followups:
            break
        first = next(m for m in b.mails if m.id == ev.msg_ids[0])
        thread_last = max(m.sent_at for m in b.mails if m.thread == first.thread)
        lo = thread_last + timedelta(hours=1)
        if lo >= b.mail_hi():
            continue
        persons = [k for k in ev.attendees if k != ev.user]
        sender = b.contact(rng.choice(persons))
        me = b.contact(ev.user)
        subject, body = texts.followup(rng, me[0], sender[0], ev.title)
        b.add_mail(
            ev.user,
            first.thread,
            sender,
            [me],
            subject,
            body,
            b.rand_time(lo, b.mail_hi()),
            ["INBOX"],
            "followup",
            ev.scenario_id,
            ev,
        )
        made += 1
