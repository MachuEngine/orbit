"""사건 타임라인 생성 (시드 고정). 시나리오가 일정과 메일을 만들고 과제 명세를 돌려준다.

- 일정·메일의 날짜, ID, 발신자 같은 구조 필드는 모두 코드가 채운다. 본문만 템플릿 문장이다.
- 모든 일정은 평일 09:00~18:00(KST), 30분 단위다 (`timeutil` 한계 참고).
- 모든 메일과 일정 갱신은 스냅샷 시각(`window.snapshot`) 이전이다.
- 시나리오는 4종이다: agenda(하루 일정), reschedule(메일로 시간 변경), cancel(취소와 비슷한 제목),
  projperson(프로젝트 주간 회의와 인물). 시나리오 하나가 만든 과제는 같은 split에 들어간다.

분할 계획 (ADR-0010 2절): dev와 test 과제가 정답 근거(일정·메일)를 공유하지 않도록 시나리오를
만들기 전에 근거가 놓일 자리를 나눈다.
- 날짜: (사용자, 날짜)마다 소유 split이 정해져 있고 그 날의 일정은 소유 split의 시나리오만 만든다.
  하루 일정·빈 시간·취소 제외 과제의 근거가 그 날 일정 전체이기 때문이다.
- 프로젝트 주: (프로젝트, 주)마다 소유 split이 있다. 프로젝트 주간 과제의 근거가 그 주의
  프로젝트 일정 전체이기 때문이다.
- 인물: 세계 모델의 `split`이 정한 인물은 그 split 시나리오의 일정에만 참석한다. 인물의 다음
  회의 과제의 근거가 그 인물의 가장 이른 다가오는 일정이기 때문이다.
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
    def __init__(self, wm: WorldModel, rng: random.Random, seed: int = 0) -> None:
        self.wm = wm
        self.resched_made: dict[str, int] = {}
        self.rng = rng
        self.events: list[EventRec] = []
        self.mails: list[MailRec] = []
        self._ev_n = 0
        self._ver_n = 0
        self._mail_n = {u.key: 0 for u in wm.users}
        self._thr_n = {u.key: 0 for u in wm.users}
        self._scn_n: dict[str, int] = {}
        self._user_rr: dict[str, int] = {}
        self._person_by_email = {p.email: p for p in wm.people}
        self.pp_overrides: list[tuple[Project, Person]] = []
        # 분할 계획에 쓰는 평일 목록 (as_of 다음 날부터 일정 종료일까지)
        w = wm.window
        first = w.asof_start + timedelta(days=1)
        self.days = [
            first + timedelta(days=i)
            for i in range((w.event_end - first).days + 1)
            if (first + timedelta(days=i)).weekday() < 5
        ]
        self._day_index = {d: i for i, d in enumerate(self.days)}
        self._day_owner = self._plan_days(seed)
        # 아직 과제가 쓰지 않은 칸을 우선 고르기 위한 기록 (정답 지문 중복을 줄인다)
        self.used_days: set[tuple[str, date]] = set()
        self.used_cells: set[tuple[str, int]] = set()
        self.used_persons: set[str] = set()

    # ------------------------------------------------------------ 분할 계획

    def day_owner(self, user: str, day: date) -> str | None:
        """(사용자, 날짜)의 소유 split. 계획 범위 밖의 날은 None이다."""
        return self._day_owner.get((user, day))

    def _plan_days(self, seed: int) -> dict[tuple[str, date], str]:
        """시드로 (사용자, 날짜)의 소유 split을 정한다. 주마다 test가 약 40%가 되게 섞는다.

        주 안에 두 split이 모두 있어야 주 단위 칸(프로젝트 주)의 시나리오를 둘 다 만들 수 있다.
        """
        dev_ratio = self.wm.meta.dev_ratio
        owner: dict[tuple[str, date], str] = {}
        for user in self.wm.users:
            rng = random.Random(f"{seed}:days:{user.key}")  # noqa: S311  재현성용 의사난수
            weeks: dict[date, list[date]] = {}
            for d in self.days:
                weeks.setdefault(week_start(d), []).append(d)
            for week_days in weeks.values():
                shuffled = list(week_days)
                rng.shuffle(shuffled)
                n_test = max(1, round(len(shuffled) * (1 - dev_ratio)))
                for i, d in enumerate(shuffled):
                    owner[(user.key, d)] = "test" if i < n_test else "dev"
        return owner

    def week_index(self, day: date) -> int:
        return (week_start(day) - week_start(self.wm.window.asof_start)).days // 7

    def cell_owner(self, project: Project, day: date) -> str:
        """(프로젝트, 주)의 소유 split."""
        p = [x.key for x in self.wm.projects].index(project.key)
        return "test" if (p + self.week_index(day)) % 5 in (1, 3) else "dev"

    def owned(self, user: str, day: date, split: str, project: Project | None = None) -> bool:
        if self.day_owner(user, day) != split:
            return False
        return project is None or self.cell_owner(project, day) == split

    def split_people(self, user: str, split: str, pool: list[str] | None = None) -> list[str]:
        keys = pool if pool is not None else [p.key for p in self.wm.people]
        return [
            k
            for k in keys
            if self.wm.person(k).split == split and user in self.wm.person(k).works_with
        ]

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
        """[lo, hi] 안의 업무 시간대(평일) 시각."""
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
        person = self._person_by_email.get(sender[1])
        if person is not None:  # 인물이 보낸 메일은 서명에 소속을 적는다 (동명이인 구분 근거)
            body = f"{body}\n\n-- \n{person.name} / {person.company}"
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

    def pick_target(
        self, asof: datetime, user: str, split: str, project: Project | None = None
    ) -> tuple[date, bool] | None:
        """as_of 이후 평일 중 split이 소유한 날. (날짜, 명시적 날짜로 말할지)."""
        a = local_date(asof)
        next_mon = week_start(a) + timedelta(days=7)
        groups: list[tuple[list[date], bool]] = []
        if a.weekday() < 4:
            groups.append(([a + timedelta(days=1)], False))  # 내일
        groups.append(([next_mon + timedelta(days=i) for i in range(5)], False))  # 다음 주 N요일
        if a.weekday() <= 2:
            groups.append(
                ([a + timedelta(days=i) for i in range(2, 5 - a.weekday())], False)
            )  # 이번 주 N요일
        groups.append(([d for d in self.days if d > a], True))  # 'M월 D일'
        usable: list[tuple[list[date], bool]] = []
        for candidates, explicit in groups:
            mine = [d for d in candidates if self.owned(user, d, split, project)]
            mine = [d for d in mine if (user, d) not in self.used_days] or mine
            if mine:
                usable.append((mine, explicit))
        if not usable:
            return None
        days, explicit = self.rng.choice(usable)
        day = self.rng.choice(days)
        self.used_days.add((user, day))
        return day, explicit

    def pick_attendees(self, user: str, count: int, split: str, pool: list[str]) -> list[str]:
        keys = self.split_people(user, split, pool)
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
            day = first
            while day <= w.event_end:
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
                day += timedelta(weeks=rec.every_weeks)
            # 안내 메일은 반복 일정 전체에 대한 것이라 특정 일정의 근거로 연결하지 않는다.
            me = self.contact(rec.user)
            first_att = self.contact(rec.attendees[0])
            rule = f"격주 {'월화수목금'[rec.weekday]}요일 {hh:02d}:{mm:02d}"
            body = texts.series_notice(self.rng, first_att[0], me[0], rec.title, rule)
            self.add_mail(
                rec.user,
                self.new_thread(rec.user),
                me,
                [first_att],
                f"[반복 일정] {rec.title}",
                body,
                at_local(w.mail_start, 8, 30),
                ["SENT"],
                "series",
                sid,
            )


# ---------------------------------------------------------------- 시나리오


def scn_agenda(b: Builder, user: User, split: str) -> list[TaskSpec]:
    """하루에 일정 2개. 오전·오후 조회와 가장 이른 빈 시간 과제를 만든다."""
    asof = b.pick_asof()
    picked = b.pick_target(asof, user.key, split)
    if picked is None:
        return []
    target, explicit = picked
    sid = b.next_scenario_id("agenda")
    first_start = b.free_start(user.key, target, 60, 9 * 60, 11 * 60 + 30)
    if first_start is None:
        return []
    first = _agenda_event(b, user, target, first_start, 60, sid, split)
    dur = b.rng.choice([30, 60, 90])
    second_start = b.free_start(user.key, target, dur)
    if second_start is None:
        b.events.remove(first)
        return []
    second = _agenda_event(b, user, target, second_start, dur, sid, split)
    for ev in (first, second):
        lo, hi = b.invite_window(ev.start)
        b.touch(ev, b.invite_mail(ev, b.rand_time(lo, hi)).sent_at)
    halves = sorted({half_of(ev.start) for ev in (first, second)})
    half = b.rng.choice([None, *halves])
    params = {"target": target.isoformat(), "explicit": explicit, "half": half}
    return [
        TaskSpec("day_schedule", sid, user.key, asof, dict(params)),
        TaskSpec(
            "free_slot", sid, user.key, asof, {"target": params["target"], "explicit": explicit}
        ),
    ]


def _agenda_event(
    b: Builder, user: User, day: date, start: datetime, dur: int, sid: str, split: str
) -> EventRec:
    pool = [p.key for p in b.wm.people]
    attendees = b.pick_attendees(user.key, b.rng.choice([1, 2]), split, pool)
    organizer = b.rng.choice([attendees[0], user.key])
    return b.add_event(
        user.key, b.generic_title(user.key, day), start, dur, attendees, organizer, sid
    )


def scn_reschedule(b: Builder, user: User, split: str) -> list[TaskSpec]:
    """메일 요청으로 일정 시간이 1~2번 바뀐다. 캘린더에는 최신 시간이 반영돼 있다."""
    w = b.wm.window
    asof = b.pick_asof()
    a = local_date(asof)
    floor = w.asof_start + timedelta(days=1)
    project = b.rng.choice([p for p in b.wm.projects if p.user == user.key])
    members = b.split_people(user.key, split, project.members)
    if not members:
        return []
    final_days = [
        d
        for d in b.days
        if a + timedelta(days=1) <= d <= min(a + timedelta(days=9), w.event_end)
        and b.owned(user.key, d, split, project)
    ]
    if not final_days:
        return []
    final_day = b.rng.choice(final_days)
    # 변경 2회 과제가 양쪽 split에 모두 생기도록 split마다 두 번째 시나리오부터 3개에 1개꼴로 둔다.
    made = b.resched_made.get(split, 0)
    changes = 2 if made % 3 == 1 else 1
    dur = b.rng.choice([30, 60, 90])
    final = b.free_start(user.key, final_day, dur)
    if final is None:
        return []
    person = b.wm.person(b.rng.choice(members))
    sid = b.next_scenario_id("reschedule")
    # 과거 시각: 최종 시각과 다른 시각을 날짜 범위 안에서 고른다. 메일에만 있고 캘린더에는 없다.
    times: list[datetime] = []
    for _ in range(changes):
        for _try in range(20):
            lo_day, hi_day = (
                max(floor, final_day - timedelta(days=3)),
                final_day + timedelta(days=3),
            )
            days = [
                lo_day + timedelta(days=i)
                for i in range((hi_day - lo_day).days + 1)
                if (lo_day + timedelta(days=i)).weekday() < 5
            ]
            if not days:
                continue
            cand = at_local(b.rng.choice(days), b.rng.randint(9, 16), b.rng.choice([0, 30]))
            if cand != final and cand not in times:
                times.append(cand)
                break
        else:
            return []
    path = [*times, final]  # path[0]은 처음 초대 시각, path[-1]은 최종
    title = b.project_title(user.key, project)
    extra = b.pick_attendees(user.key, 1, split, [m for m in members if m != person.key])
    ev = b.add_event(
        user.key, title, path[0], dur, [person.key, *extra], person.key, sid, project=project.key
    )
    lo = max(b.mail_lo(), min(path) - timedelta(days=12))
    stamps = b.sorted_times(changes + 1, lo, b.mail_hi() - timedelta(hours=4))
    me, other = b.contact(user.key), b.contact(person.key)
    thread = b.new_thread(user.key)
    invite = b.invite_mail(ev, stamps[0], thread=thread)
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
        # 확인 메일은 다음 변경 요청보다 먼저여야 시간순 마지막 메일이 최종 일정이 된다.
        reply_at = req.sent_at + timedelta(hours=b.rng.randint(1, 3))
        reply_at = min(reply_at, b.mail_hi())
        if i + 2 < len(stamps):
            reply_at = min(reply_at, stamps[i + 2] - timedelta(minutes=10))
        reply_at = max(reply_at, req.sent_at + timedelta(minutes=5))
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
        b.touch(ev, reply.sent_at)
    ev.start, ev.end = final, final + timedelta(minutes=dur)
    ev.changes = changes
    b.resched_made[split] = made + 1
    return [TaskSpec("reschedule", sid, user.key, asof, {"event_id": ev.id})]


def scn_cancel(b: Builder, user: User, split: str) -> list[TaskSpec]:
    """취소된 일정(1차)과 비슷한 제목의 유효한 일정(2차)이 같은 날, 같은 오전·오후에 있다."""
    asof = b.pick_asof()
    project = b.rng.choice([p for p in b.wm.projects if p.user == user.key])
    members = b.split_people(user.key, split, project.members)
    picked = b.pick_target(asof, user.key, split, project)
    if picked is None or not members:
        return []
    target, explicit = picked
    half = b.rng.choice(["오전", "오후"])
    lo_min, hi_min = (9 * 60, 12 * 60) if half == "오전" else (13 * 60, 18 * 60)
    person = b.wm.person(b.rng.choice(members))
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


def scn_projperson(b: Builder, user: User, split: str) -> list[TaskSpec]:
    """프로젝트 회의 2개가 같은 주에 있고 같은 인물이 참석한다.

    (프로젝트, 주) 칸은 아직 쓰지 않은 것을 우선한다. 같은 칸의 프로젝트 주간 과제는 정답이 같아
    중복으로 버려지기 때문이다. 주는 as_of 기준 이번 주(0)·다음 주(1)·다다음 주(2)다.
    """
    w = b.wm.window
    override = next((o for o in b.pp_overrides if o[1].split == split), None)
    if override is not None:
        user = b.wm.user(override[0].user)
        projects = [override[0]]
    else:
        projects = [p for p in b.wm.projects if p.user == user.key]
    asof = b.pick_asof()
    a = local_date(asof)
    cells: list[tuple[Project, int, list[date]]] = []
    for project in projects:
        if not b.split_people(user.key, split, project.members):
            continue
        for week in range(3):
            pool = [
                d
                for d in b.days
                if b.week_index(d) == week
                and d > a
                and d <= w.event_end
                and b.owned(user.key, d, split, project)
            ]
            if len(pool) >= 2:
                cells.append((project, week, pool))
    if not cells:
        return []
    fresh = [c for c in cells if (c[0].key, c[1]) not in b.used_cells]
    project, week, pool = b.rng.choice(fresh or cells)
    members = b.split_people(user.key, split, project.members)
    if override is not None:
        person = override[1]
    else:
        unused = [m for m in members if m not in b.used_persons]
        person = b.wm.person(b.rng.choice(unused or members))
    days = sorted(b.rng.sample(pool, 2))
    sid = b.next_scenario_id("projperson")
    homonym = b.wm.is_homonym(person)
    created: list[EventRec] = []
    for day in days:
        dur = b.rng.choice([30, 60, 90])
        start = b.free_start(user.key, day, dur)
        if start is None:
            for ev in created:
                b.events.remove(ev)
            return []
        extra = b.pick_attendees(user.key, 1, split, [m for m in members if m != person.key])
        organizer = person.key if homonym else b.rng.choice([person.key, user.key])
        created.append(
            b.add_event(
                user.key,
                b.project_title(user.key, project),
                start,
                dur,
                [person.key, *extra],
                organizer,
                sid,
                project=project.key,
            )
        )
    if override is not None:
        b.pp_overrides.remove(override)
    b.used_cells.add((project.key, week))
    b.used_persons.add(person.key)
    for ev in created:
        lo, hi = b.invite_window(ev.start)
        b.touch(ev, b.invite_mail(ev, b.rand_time(lo, hi)).sent_at)
    week_name = ("this", "next", "after_next")[week]
    return [
        TaskSpec("project_week", sid, user.key, asof, {"project": project.key, "week": week_name}),
        TaskSpec("person_next", sid, user.key, asof, {"person": person.key}),
    ]


SCENARIO_FUNCS = {
    "agenda": scn_agenda,
    "reschedule": scn_reschedule,
    "cancel": scn_cancel,
    "projperson": scn_projperson,
}


def run_scenarios(b: Builder, scenario: str, count: int, split: str) -> list[TaskSpec]:
    """`split` 소유의 시나리오를 `count`개 만든다. 배치에 실패하면 새로 시도한다."""
    specs: list[TaskSpec] = []
    made = 0
    attempts = 0
    while made < count:
        attempts += 1
        if attempts > count * 100 + 100:
            raise RuntimeError(f"시나리오 {scenario}({split})를 만들지 못했다")
        user = b.next_user(f"{scenario}-{split}")
        got = SCENARIO_FUNCS[scenario](b, user, split)
        if got:
            for spec in got:
                spec.split = split
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
        due = rng.choice([d for d in b.days if d >= wm.window.asof_start])
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
