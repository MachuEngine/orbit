"""구조 생성기의 상태와 기본 동작(일정·메일·문서 추가, 날짜 예약, 시나리오 기록).

시나리오 함수(`scenarios.py`)와 배경 채우기(`background.py`)가 이 `Builder`를 쓴다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from orbit.connectors.contracts import (
    DOCS_MIME,
    Address,
    AttachmentMeta,
    Attendee,
    Calendar,
    CalendarEvent,
)
from orbit.evaldata.v1 import templates as T
from orbit.evaldata.v1.skeleton import (
    ArtifactRef,
    BodyContext,
    ContextPerson,
    Fact,
    SkFile,
    SkMessage,
    _Truth,
)
from orbit.evaldata.v1.worldmodel import Person, Project, User, WorldModel


class WorldBuildError(RuntimeError):
    """생성 중 조건을 만족하지 못했다(세계 모델 수량 부족 등)."""


@dataclass
class MsgSpec:
    """스레드 안 메일 한 통의 입력. `sender`·`to`·`cc`는 "user"·인물 key·노이즈 발신자 주소다."""

    sender: str
    to: list[str]
    subject: str
    sent_at: datetime
    lines: list[str]
    intent: str
    cc: list[str] = field(default_factory=list)
    required: list[Fact] = field(default_factory=list)
    forbidden: list[Fact] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    project: str | None = None
    topic: str | None = None
    decoy_for: str | None = None


@dataclass
class ThreadOut:
    thread_id: str
    messages: list[ArtifactRef]


@dataclass
class BodyDraft:
    body_id: str
    artifact: str
    genre: str
    context: BodyContext
    required: list[Fact]
    forbidden: list[Fact]
    template: str
    scenario_id: str | None


@dataclass
class Scn:
    """만들고 있는 시나리오 하나."""

    scenario_id: str
    type: str
    account_id: str
    user_key: str
    variant: str | None
    rng: random.Random
    as_of: datetime
    evidence: list[ArtifactRef] = field(default_factory=list)
    distractors: list[ArtifactRef] = field(default_factory=list)
    truth: _Truth | None = None


@dataclass
class Claims:
    """사용자별 인물·프로젝트 배정. 같은 인물을 두 인물 시나리오가 쓰지 않게 한다."""

    next_people: list[str] = field(default_factory=list)
    meeting_people: list[str] = field(default_factory=list)
    absent_people: list[str] = field(default_factory=list)
    homonym_pairs: list[tuple[str, str]] = field(default_factory=list)
    free_people: list[str] = field(default_factory=list)  # 미래 일정 참석자로 써도 되는 인물
    quiet_project: str | None = None  # 금액·미팅이 없어야 하는 프로젝트(absent 전용)
    projects: list[Project] = field(default_factory=list)  # quiet을 뺀 프로젝트


class Acct:
    """계정 하나의 쌓이는 산출물."""

    def __init__(self, user: User, index: int) -> None:
        self.user = user
        self.index = index
        self.tz = ZoneInfo(user.time_zone)
        self.calendars = [
            Calendar(id=c.id, name=c.name, time_zone=user.time_zone, primary=c.primary)
            for c in user.calendars
        ]
        self.events: list[CalendarEvent] = []
        self.messages: list[SkMessage] = []
        self.thread_msgs: dict[str, list[str]] = {}
        self.files: list[SkFile] = []
        self.reserved_days: set[date] = set()
        self.titles: set[str] = set()
        self.n_event = 0
        self.n_msg = 0
        self.n_thread = 0
        self.n_file = 0

    @property
    def addr(self) -> Address:
        return Address(email=self.user.email, name=self.user.name)


class Builder:
    def __init__(self, wm: WorldModel, seed: int) -> None:
        self.wm = wm
        self.seed = seed
        self.snapshot = wm.window.snapshot.astimezone(UTC)
        self.acc: dict[str, Acct] = {u.account_id: Acct(u, i) for i, u in enumerate(wm.users)}
        self.bodies: dict[str, BodyDraft] = {}
        self.n_body_mail = 0
        self.n_body_doc = 0
        self.scenarios: list[Scn] = []
        self.type_counter: dict[str, int] = {}
        self.cur: Scn | None = None
        self.claims: dict[str, Claims] = {}
        self.noise_by_email = {n.email: n for n in wm.noise_senders}
        self.search_accts = [u.account_id for u in wm.search_users()]  # 크기 오름차순
        self.mail_group_of_body: dict[str, int] = {}  # 검색 메일함 body_id -> 그룹(0..)
        self.decoys: dict[str, list[str]] = {}  # 검색 시나리오 ID -> 무관 본문 body_id
        self.search_msg_by_body: dict[str, dict[str, str]] = {a: {} for a in self.search_accts}
        self.search_file_by_body: dict[str, dict[str, str]] = {a: {} for a in self.search_accts}
        self.used: dict[str, set[str]] = {}  # 한 번만 써야 하는 선택(프로젝트·제목 등)
        self.claim_idx: dict[str, int] = {}

    # ---------------------------------------------------------------- 난수·시간

    def rng(self, label: str) -> random.Random:
        return random.Random(f"{self.seed}:{label}")  # noqa: S311  재현성용 의사난수

    def local(self, acct: str, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.acc[acct].tz)

    def snap_day(self, acct: str) -> date:
        return self.snapshot.astimezone(self.acc[acct].tz).date()

    def weekdays(self, acct: str, lo: int, hi: int) -> list[date]:
        """스냅샷 현지 날짜 기준 +lo~+hi일 사이의 평일."""
        base = self.snap_day(acct)
        days = [base + timedelta(days=i) for i in range(lo, hi + 1)]
        return [d for d in days if d.weekday() < 5]

    def pick_as_of(self, rng: random.Random, acct: str) -> datetime:
        """사용자 현지 평일 09~17시. 스냅샷 이후, asof 날짜 범위 안."""
        w = self.wm.window
        days = []
        d = w.asof_start
        while d <= w.asof_end:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        for _ in range(200):
            day = days[rng.randrange(len(days))]
            cand = self.local(acct, day, 9 + rng.randrange(9), 30 * rng.randrange(2))
            if cand > self.snapshot + timedelta(minutes=30):
                return cand
        raise WorldBuildError("as_of를 고를 수 없다: asof 날짜 범위를 확인한다")

    def event_days(self, acct: str, start: datetime, end: datetime) -> list[date]:
        """일정이 걸치는 현지 날짜 목록. 종료 시각 자체는 포함하지 않는다."""
        tz = self.acc[acct].tz
        s = start.astimezone(tz)
        e = end.astimezone(tz)
        last = (e - timedelta(microseconds=1)).date() if e > s else s.date()
        return [s.date() + timedelta(days=i) for i in range((last - s.date()).days + 1)]

    @staticmethod
    def all_day_footprint(day: date) -> tuple[datetime, datetime]:
        """종일 일정이 배치 규칙에서 차지하는 구간. UTC 날짜 경계로 본다.

        종일 일정의 실제 시각은 캘린더 시간대 00:00이다(ADR-0027). 다만 v1 본문을 다시 쓰지 않으려고
        배치 규칙(빈 날 고르기, 예약)은 UTC 경계 해석일 때와 같게 둔다. 그래서 시간대에 따라 현지
        앞날 또는 뒷날도 비워 둔다. 이 구간은 일정 시각이 아니라 배치 전용이다.
        """
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return start, start + timedelta(days=1)

    def footprint(self, acct: str, event: CalendarEvent) -> tuple[datetime, datetime]:
        if not event.all_day:
            return event.start, event.end
        return self.all_day_footprint(event.start.astimezone(self.acc[acct].tz).date())

    def events_on(self, acct: str, day: date) -> list[CalendarEvent]:
        return [
            e
            for e in self.acc[acct].events
            if day in self.event_days(acct, *self.footprint(acct, e))
        ]

    def place_ok(self, acct: str, start: datetime, end: datetime) -> bool:
        reserved = self.acc[acct].reserved_days
        return not any(d in reserved for d in self.event_days(acct, start, end))

    def reserve(self, acct: str, days: list[date]) -> None:
        self.acc[acct].reserved_days.update(days)

    def pick_day(
        self,
        rng: random.Random,
        acct: str,
        lo: int,
        hi: int,
        *,
        empty: bool = False,
        span: int = 1,
    ) -> date:
        """예약되지 않은 평일. `empty`면 일정이 하나도 없는 날(연속 `span`일)."""
        cands = self.weekdays(acct, lo, hi)
        rng.shuffle(cands)
        for d in cands:
            days = [d + timedelta(days=i) for i in range(span)]
            if any(x in self.acc[acct].reserved_days for x in days):
                continue
            if empty and any(self.events_on(acct, x) for x in days):
                continue
            return d
        raise WorldBuildError(f"{acct}: 조건에 맞는 날짜가 없다(범위 {lo}~{hi}일, 빈 날 {empty})")

    # ---------------------------------------------------------------- 연락처

    def person(self, key: str) -> Person:
        return self.wm.person(key)

    def addr(self, acct: str, key: str) -> Address:
        if key == "user":
            return self.acc[acct].addr
        if key in self.noise_by_email:
            n = self.noise_by_email[key]
            return Address(email=n.email, name=n.name)
        p = self.person(key)
        return Address(email=p.email, name=p.name)

    def name_of(self, acct: str, key: str) -> str:
        return self.addr(acct, key).name or ""

    def ctx_person(self, acct: str, key: str) -> ContextPerson:
        if key == "user":
            u = self.acc[acct].user
            return ContextPerson(name=u.name, relation="사용자 본인", company=u.company)
        if key in self.noise_by_email:
            n = self.noise_by_email[key]
            return ContextPerson(name=n.name, relation=f"자동 발송({n.kind})", company="")
        p = self.person(key)
        rel = {"colleague": "동료", "customer": "고객사 담당자", "vendor": "협력사 담당자"}[
            p.relation
        ]
        return ContextPerson(name=p.name, relation=rel, company=p.company)

    # ---------------------------------------------------------------- 일정

    def add_event(
        self,
        acct: str,
        *,
        title: str,
        start: datetime,
        end: datetime,
        calendar: int | None = None,
        attendees: list[str] | None = None,
        location: str = "",
        status: str = "confirmed",
        all_day: bool = False,
        organizer: str = "user",
        updated_at: datetime | None = None,
        description: str = "",
        claim: bool = False,
        rng: random.Random | None = None,
    ) -> ArtifactRef:
        """일정을 추가한다. `claim`이 아니면 예약된 날짜에는 놓을 수 없다(호출 버그로 보고 실패)."""
        a = self.acc[acct]
        start_u, end_u = start.astimezone(UTC), end.astimezone(UTC)
        if not start_u < end_u:
            raise WorldBuildError("일정의 시작이 종료보다 앞서야 한다")
        place = (
            self.all_day_footprint(start.astimezone(a.tz).date()) if all_day else (start_u, end_u)
        )
        if not claim and not self.place_ok(acct, *place):
            raise WorldBuildError(f"{acct}: 예약된 날짜에 일정을 놓으려 했다 ({title})")
        r = rng or self.rng(f"event:{acct}:{a.n_event}")
        cal = a.calendars[(calendar if calendar is not None else 0) % len(a.calendars)]
        a.n_event += 1
        if updated_at is None:
            updated_at = self.snapshot - timedelta(hours=1 + r.randrange(24 * 20))
        if updated_at > self.snapshot:
            raise WorldBuildError("일정 갱신 시각이 스냅샷 이후다")
        people = [
            Attendee(
                email=self.addr(acct, k).email,
                name=self.addr(acct, k).name,
                response_status="accepted",
            )
            for k in (attendees or [])
        ]
        me = Attendee(email=a.user.email, name=a.user.name, response_status="accepted")
        org = self.addr(acct, organizer)
        ev = CalendarEvent(
            id=f"ev{a.index:02d}{a.n_event:05d}",
            calendar_id=cal.id,
            title=title,
            description=description,
            location=location,
            start=start_u,
            end=end_u,
            all_day=all_day,
            attendees=(*people, me) if people else (),
            organizer=Attendee(email=org.email, name=org.name, response_status="accepted"),
            status="cancelled" if status == "cancelled" else "confirmed",
            version=f"v{a.n_event:08x}",
            updated_at=updated_at.astimezone(UTC),
        )
        a.events.append(ev)
        return ArtifactRef(kind="event", id=ev.id, calendar_id=cal.id)

    # ---------------------------------------------------------------- 본문 명세

    def _new_body(
        self,
        artifact: str,
        genre: str,
        context: BodyContext,
        required: list[Fact],
        forbidden: list[Fact],
        template: str,
    ) -> str:
        if artifact == "mail":
            self.n_body_mail += 1
            body_id = f"bm-{self.n_body_mail:06d}"
        else:
            self.n_body_doc += 1
            body_id = f"bd-{self.n_body_doc:05d}"
        sid = self.cur.scenario_id if self.cur is not None else None
        self.bodies[body_id] = BodyDraft(
            body_id, artifact, genre, context, required, forbidden, template, sid
        )
        return body_id

    def _genre_of(self, spec: MsgSpec) -> str:
        if spec.sender in self.noise_by_email:
            return self.noise_by_email[spec.sender].kind
        other = spec.sender if spec.sender != "user" else (spec.to[0] if spec.to else "user")
        if other in self.noise_by_email:
            return self.noise_by_email[other].kind
        return self.person(other).relation

    def add_thread(
        self,
        accts: list[str],
        specs: list[MsgSpec],
        *,
        rng: random.Random,
    ) -> dict[str, ThreadOut]:
        """스레드를 `accts`의 각 계정에 넣는다. 본문 명세는 한 번만 만들어 공유한다.

        `specs`는 오래된 순이어야 한다. 반환은 계정별 스레드 ID와 메시지 참조(body_id 포함)다.
        """
        if not specs:
            raise WorldBuildError("빈 스레드")
        for prev, nxt in zip(specs, specs[1:], strict=False):
            if not prev.sent_at < nxt.sent_at:
                raise WorldBuildError("스레드 안 메일은 시각이 증가해야 한다")
        if specs[-1].sent_at >= self.snapshot:
            raise WorldBuildError("메일이 스냅샷 이후에 보내졌다")
        first = accts[0]
        body_ids: list[str] = []
        for i, sp in enumerate(specs):
            genre = self._genre_of(sp)
            recipients = [self.ctx_person(first, k) for k in sp.to]
            ctx = BodyContext(
                subject=sp.subject,
                sender=self.ctx_person(first, sp.sender),
                recipients=recipients,
                thread_position=i + 1,
                thread_size=len(specs),
                prior_body_ids=list(body_ids),
                project=sp.project,
                topic=sp.topic,
                intent=sp.intent,
                sent_at=sp.sent_at.astimezone(UTC),
                decoy_for=sp.decoy_for,
            )
            if sp.sender in self.noise_by_email:
                text = " ".join(sp.lines)
            else:
                to_name = recipients[0].name if recipients else ""
                text = T.compose(rng, to_name, self.name_of(first, sp.sender), sp.lines)
            bid = self._new_body("mail", genre, ctx, sp.required, sp.forbidden, text)
            body_ids.append(bid)
        out: dict[str, ThreadOut] = {}
        for acct in accts:
            a = self.acc[acct]
            a.n_thread += 1
            thread_id = f"thr-{a.index:02d}-{a.n_thread:05d}"
            refs: list[ArtifactRef] = []
            ids: list[str] = []
            for sp, bid in zip(specs, body_ids, strict=True):
                a.n_msg += 1
                mid = f"msg-{a.index:02d}-{a.n_msg:05d}"
                mine = sp.sender == "user"
                labels = ["SENT"] if mine else ["INBOX"]
                if not mine and sp.sender in self.noise_by_email:
                    kind = self.noise_by_email[sp.sender].kind
                    if kind in ("newsletter", "spam"):
                        labels.append("CATEGORY_PROMOTIONS")
                atts = tuple(
                    AttachmentMeta(
                        attachment_id=f"att-{a.index:02d}-{a.n_msg:05d}-{j}",
                        filename=name,
                        mime_type="application/pdf",
                        size_bytes=20_000 + 1_000 * ((len(name) * 7 + j) % 90),
                    )
                    for j, name in enumerate(sp.attachments)
                )
                a.messages.append(
                    SkMessage(
                        id=mid,
                        thread_id=thread_id,
                        from_=self.addr(acct, sp.sender),
                        to=tuple(self.addr(acct, k) for k in sp.to),
                        cc=tuple(self.addr(acct, k) for k in sp.cc),
                        subject=sp.subject,
                        body_id=bid,
                        sent_at=sp.sent_at.astimezone(UTC),
                        labels=tuple(labels),
                        attachments=atts,
                    )
                )
                ids.append(mid)
                refs.append(ArtifactRef(kind="message", id=mid, body_id=bid))
                if acct in self.search_msg_by_body:
                    self.search_msg_by_body[acct][bid] = mid
            a.thread_msgs[thread_id] = ids
            out[acct] = ThreadOut(thread_id, refs)
        return out

    def thread(self, acct: str, specs: list[MsgSpec], rng: random.Random) -> ThreadOut:
        return self.add_thread([acct], specs, rng=rng)[acct]

    def add_doc(
        self,
        accts: list[str],
        *,
        title: str,
        genre: str,
        modified_at: datetime,
        owner: str,
        lines: list[str],
        intent: str,
        required: list[Fact] | None = None,
        forbidden: list[Fact] | None = None,
        project: str | None = None,
        topic: str | None = None,
        doc_date: date | None = None,
    ) -> dict[str, ArtifactRef]:
        """문서를 `accts`의 각 계정에 넣는다. 이름은 계정 안에서 유일해야 한다."""
        if modified_at > self.snapshot:
            raise WorldBuildError("문서 수정 시각이 스냅샷 이후다")
        for acct in accts:
            if title in self.acc[acct].titles:
                raise WorldBuildError(f"{acct}: 문서 이름 중복 {title}")
        first = accts[0]
        ctx = BodyContext(
            sender=self.ctx_person(first, owner),
            project=project,
            topic=topic,
            intent=intent,
            document_title=title,
            document_date=doc_date or modified_at.astimezone(self.acc[first].tz).date(),
            sent_at=modified_at.astimezone(UTC),
        )
        bid = self._new_body(
            "document", genre, ctx, required or [], forbidden or [], T.doc_text(title, lines)
        )
        out: dict[str, ArtifactRef] = {}
        for acct in accts:
            a = self.acc[acct]
            a.titles.add(title)
            a.n_file += 1
            fid = f"doc-{a.index:02d}-{a.n_file:04d}"
            a.files.append(
                SkFile(
                    id=fid,
                    name=title,
                    mime_type=DOCS_MIME,
                    modified_at=modified_at.astimezone(UTC),
                    owners=(self.addr(acct, owner),),
                    body_id=bid,
                )
            )
            if acct in self.search_file_by_body:
                self.search_file_by_body[acct][bid] = fid
            out[acct] = ArtifactRef(kind="file", id=fid, body_id=bid)
        return out

    def doc(self, acct: str, **kw: object) -> ArtifactRef:
        return self.add_doc([acct], **kw)[acct]  # type: ignore[arg-type]

    # ---------------------------------------------------------------- 시나리오

    def begin(
        self, type_: str, acct: str, variant: str | None = None, *, as_of: datetime | None = None
    ) -> Scn:
        n = self.type_counter.get(type_, 0) + 1
        self.type_counter[type_] = n
        sid = f"sc-{type_.replace('_', '-')}-{n:04d}"
        rng = self.rng(f"scenario:{sid}")
        s = Scn(
            scenario_id=sid,
            type=type_,
            account_id=acct,
            user_key=self.acc[acct].user.key,
            variant=variant,
            rng=rng,
            as_of=as_of or self.pick_as_of(rng, acct),
        )
        self.cur = s
        return s

    def end(self, s: Scn, truth: _Truth) -> None:
        s.truth = truth
        self.scenarios.append(s)
        self.cur = None

    def when_before_snapshot(self, rng: random.Random, lo_days: float, hi_days: float) -> datetime:
        """스냅샷 `lo_days`~`hi_days`일 전의 시각(UTC), 분 단위로 맞춘다."""
        minutes = int(rng.uniform(lo_days, hi_days) * 24 * 60)
        return self.snapshot - timedelta(minutes=minutes)
