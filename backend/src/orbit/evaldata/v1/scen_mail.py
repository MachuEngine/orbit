"""메일 중심 시나리오: mail_fact, reply_needed, meeting_request, event_change_target, mismatch,
deadline."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC as UTC_
from datetime import date, datetime, timedelta

from orbit.evaldata.v1 import templates as T
from orbit.evaldata.v1.core import Builder, MsgSpec, Scn, WorldBuildError
from orbit.evaldata.v1.facts import (
    amount_fact,
    date_fact,
    datetime_fact,
    text_fact,
)
from orbit.evaldata.v1.scen_common import (
    MAX_FUTURE_DAYS,
    any_project,
    future_weekday,
    mail_person,
    organizer_for,
    pick_project_item,
    question_key,
    retry,
    span,
    take_question_key,
)
from orbit.evaldata.v1.scen_common import compatible as _compatible
from orbit.evaldata.v1.skeleton import (
    ArtifactRef,
    DeadlineTruth,
    EventChangeTargetTruth,
    Fact,
    MailFactTruth,
    MeetingRequestTruth,
    MismatchTruth,
    ReplyNeededTruth,
)

FACT_KINDS = ("deadline", "amount", "place", "decision", "attachment")
REQUESTS = [
    ("견적서 재발송", ["견적서 재발송", "견적서를 다시 보내"]),
    ("회의 자료 공유", ["회의 자료 공유", "회의 자료를 공유"]),
    ("참석 가능 여부 회신", ["참석 가능 여부", "참석 가능하신지"]),
    ("계약서 초안 검토", ["계약서 초안 검토", "계약서 초안을 검토"]),
    ("일정 확정 회신", ["일정 확정", "일정을 확정"]),
    ("요구사항 목록 전달", ["요구사항 목록 전달", "요구사항 목록을 전달"]),
]


def _idx(b: Builder, type_: str) -> int:
    return b.type_counter[type_] - 1


@dataclass
class _Pair:
    prev: Fact
    new: Fact
    prev_lines: list[str]
    new_lines: list[str]
    prev_att: list[str] = field(default_factory=list)
    new_att: list[str] = field(default_factory=list)
    hint: str = ""


def _weekday_after(day: date, n: int) -> date:
    d = day + timedelta(days=n)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _fact_pair(b: Builder, s: Scn, kind: str, project: str, item: str) -> _Pair:
    rng, acct = s.rng, s.account_id
    sub = f"{project} {item}"
    if kind == "deadline":

        def make(r: random.Random) -> tuple[Fact, Fact]:
            d1 = _weekday_after(b.snap_day(acct), r.randrange(3, 14))
            d2 = _weekday_after(d1, r.randrange(2, 8))
            return date_fact("previous", d1, "deadline"), date_fact("latest", d2, "deadline")

        p, n = retry(rng, make, lambda v: _compatible([v[0]], [v[1]]))
        return _Pair(
            p,
            n,
            [f"{sub} 제출 마감은 {p.surface_forms[0]}입니다."],
            [f"일정이 바뀌어 {sub} 제출 마감을 {n.surface_forms[0]}로 정정합니다."],
            hint=f"{sub} 제출 마감일",
        )
    if kind == "amount":

        def make_a(r: random.Random) -> tuple[Fact, Fact]:
            a1 = r.randrange(30, 95) * 100_000
            a2 = a1 + r.choice([-1, 1]) * r.randrange(1, 6) * 100_000
            return amount_fact("previous", a1), amount_fact("latest", a2)

        p, n = retry(rng, make_a, lambda v: _compatible([v[0]], [v[1]]))
        return _Pair(
            p,
            n,
            [f"{sub} 견적 금액은 {p.surface_forms[0]}입니다."],
            [f"견적을 정정합니다. {sub} 최종 금액은 {n.surface_forms[0]}입니다."],
            hint=f"{sub} 견적 금액",
        )
    if kind == "place":
        l1, l2 = rng.sample(T.LOCATIONS, 2)
        p = text_fact("previous", "place", l1, [l1])
        n = text_fact("latest", "place", l2, [l2])
        return _Pair(
            p,
            n,
            [f"{sub} 논의는 {l1}에서 진행합니다."],
            [f"장소가 바뀌었습니다. {sub} 논의는 {l2}에서 진행합니다."],
            hint=f"{sub} 논의 장소",
        )
    if kind == "decision":
        (v1, f1), (v2, f2) = rng.sample(T.DECISIONS, 2)
        p = text_fact("previous", "decision", v1, f1)
        n = text_fact("latest", "decision", v2, f2)
        return _Pair(
            p,
            n,
            [f"{sub} 건은 {f1[0]}으로 정리했습니다."],
            [f"논의 끝에 결정이 바뀌었습니다. {sub} 건은 {f2[0]}으로 정했습니다."],
            hint=f"{sub} 결정 사항",
        )
    n1, n2 = f"{project}_{item}_v1.pdf", f"{project}_{item}_v2.pdf"
    p = text_fact("previous", "item", n1, [n1, n1.removesuffix(".pdf")])
    n = text_fact("latest", "item", n2, [n2, n2.removesuffix(".pdf")])
    return _Pair(
        p,
        n,
        [f"{sub} 관련 자료를 {n1} 파일로 첨부합니다."],
        [f"수정본을 {n2} 파일로 다시 첨부합니다."],
        prev_att=[n1],
        new_att=[n2],
        hint=f"{sub} 첨부 파일 이름",
    )


def mail_fact(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    kind = FACT_KINDS[_idx(b, "mail_fact") % len(FACT_KINDS)]
    qkind = "deadline" if kind == "deadline" else kind
    project, item = pick_project_item(b, s, qkind)
    who = mail_person(b, s)
    pair = _fact_pair(b, s, kind, project, item)
    t1 = b.when_before_snapshot(rng, 20, 50)
    t2 = min(
        t1 + timedelta(days=rng.randrange(2, 12), minutes=rng.randrange(600)),
        b.snapshot - timedelta(days=1),
    )
    subj = f"[{project}] {item} 안내"
    same = rng.random() < 0.5
    prev_spec = MsgSpec(
        sender=who,
        to=["user"],
        subject=subj,
        sent_at=t1,
        lines=pair.prev_lines,
        intent=f"{project} {item}에 관한 사실을 알리는 메일",
        required=[pair.prev],
        forbidden=[pair.new],
        attachments=pair.prev_att,
        project=project,
        topic=item,
    )
    new_spec = MsgSpec(
        sender=who,
        to=["user"],
        subject=f"Re: {subj}" if same else f"[정정] {subj}",
        sent_at=t2,
        lines=pair.new_lines,
        intent=f"{project} {item}에 관한 앞선 안내를 정정하는 메일(바뀐 값을 분명히 밝힌다)",
        required=[pair.new],
        forbidden=[pair.prev],
        attachments=pair.new_att,
        project=project,
        topic=item,
    )
    if same:
        mid: list[MsgSpec] = []
        if rng.random() < 0.5 and t1 + timedelta(hours=2) < t2:
            mid = [
                MsgSpec(
                    sender="user",
                    to=[who],
                    subject=f"Re: {subj}",
                    sent_at=t1 + timedelta(hours=1 + rng.randrange(20)),
                    lines=T.business_lines(rng, item, 1),
                    intent="앞선 안내를 확인했다고 짧게 답하는 메일",
                    project=project,
                    topic=item,
                )
            ]
            if not (mid[0].sent_at < t2):
                mid = []
        out = b.thread(acct, [prev_spec, *mid, new_spec], rng)
        prev_ref, new_ref = out.messages[0], out.messages[-1]
    else:
        o1 = b.thread(acct, [prev_spec], rng)
        o2 = b.thread(acct, [new_spec], rng)
        prev_ref, new_ref = o1.messages[0], o2.messages[0]
    s.evidence.append(new_ref)
    s.distractors.append(prev_ref)
    b.end(
        s,
        MailFactTruth(
            fact_kind=kind,
            project=project,
            item=item,
            question_key=question_key(project, item, qkind),
            subject_hint=pair.hint,
            latest=pair.new,
            latest_message=new_ref,
            previous=pair.prev,
            previous_message=prev_ref,
            same_thread=same,
        ),
    )


def reply_needed(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    who = mail_person(b, s)
    p = b.person(who)
    project = any_project(b, s)
    value, forms = rng.choice(REQUESTS)
    q = text_fact("question", "item", value, forms)
    subj = f"[{project}] {rng.choice(T.ITEMS)} 관련 문의"
    t_last = b.when_before_snapshot(rng, 0.3, 6)
    specs: list[MsgSpec] = []
    k = rng.randrange(0, 3)  # 앞선 왕복 수
    t = t_last - timedelta(hours=2 * (2 * k + 1) * (1 + rng.randrange(20)))
    for i in range(k):
        specs.append(
            MsgSpec(
                sender=who,
                to=["user"],
                subject=subj if i == 0 else f"Re: {subj}",
                sent_at=t,
                lines=T.business_lines(rng, project, 2),
                intent=f"{project} 진행 상황을 묻거나 공유하는 메일",
                project=project,
            )
        )
        specs.append(
            MsgSpec(
                sender="user",
                to=[who],
                subject=f"Re: {subj}",
                sent_at=t + timedelta(hours=1 + rng.randrange(8)),
                lines=T.business_lines(rng, project, 1),
                intent="앞선 메일에 답하는 메일",
                project=project,
            )
        )
        t = specs[-1].sent_at + timedelta(hours=2 + rng.randrange(30))
    specs.append(
        MsgSpec(
            sender=who,
            to=["user"],
            subject=subj if k == 0 else f"Re: {subj}",
            sent_at=t_last,
            lines=[
                f"{project} 건으로 요청 드립니다.",
                f"요청 사항: {forms[0]}. 가능한 시점을 알려 주세요.",
            ],
            intent="사용자에게 구체적인 요청을 하고 답장을 기다리는 메일(질문으로 끝난다)",
            required=[q],
            project=project,
        )
    )
    # 시각 순서 보정(앞선 메일이 마지막 메일보다 늦으면 간격을 다시 잡는다)
    for i in range(len(specs) - 1):
        if not specs[i].sent_at < specs[i + 1].sent_at:
            specs[i].sent_at = specs[i + 1].sent_at - timedelta(hours=1)
    for i in range(len(specs) - 2, -1, -1):
        if not specs[i].sent_at < specs[i + 1].sent_at:
            specs[i].sent_at = specs[i + 1].sent_at - timedelta(minutes=30)
    out = b.thread(acct, specs, rng)
    # 방해: 사용자가 이미 답한 스레드
    who2 = mail_person(b, s)
    t2 = b.when_before_snapshot(rng, 8, 20)
    subj2 = f"[{project}] {rng.choice(T.ITEMS)} 확인 요청"
    done = b.thread(
        acct,
        [
            MsgSpec(
                sender=who2,
                to=["user"],
                subject=subj2,
                sent_at=t2,
                lines=[f"{project} 건 확인 부탁드립니다."],
                intent="확인을 요청하는 메일",
                project=project,
            ),
            MsgSpec(
                sender="user",
                to=[who2],
                subject=f"Re: {subj2}",
                sent_at=t2 + timedelta(hours=3),
                lines=["확인했습니다. 진행해 주세요."],
                intent="요청에 답하는 메일(스레드가 이미 처리되었다)",
                project=project,
            ),
        ],
        rng,
    )
    s.evidence.append(out.messages[-1])
    s.distractors.append(done.messages[-1])
    b.end(
        s,
        ReplyNeededTruth(
            thread_id=out.thread_id,
            awaiting_message=out.messages[-1],
            sender_name=p.name,
            sender_email=p.email,
            subject=subj if k == 0 else f"Re: {subj}",
            asked_at=t_last,
            question=q,
        ),
    )


def meeting_request(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    who = mail_person(b, s)
    p = b.person(who)
    title = f"{rng.choice(T.MEETING_KINDS)} 미팅"
    revised = _idx(b, "meeting_request") % 2 == 1
    others = [k for k in [mail_person(b, s)] if k != who][:1]

    def make(r: random.Random) -> tuple[datetime, datetime]:
        d1 = future_weekday(b, s, 2, MAX_FUTURE_DAYS)
        d2 = future_weekday(b, s, 2, MAX_FUTURE_DAYS)
        return (
            b.local(acct, d1, 10 + r.randrange(6), 30 * r.randrange(2)),
            b.local(acct, d2, 10 + r.randrange(6), 30 * r.randrange(2)),
        )

    a_start, b_start = retry(
        rng,
        make,
        lambda v: _compatible([datetime_fact("a", v[0])], [datetime_fact("b", v[1])]),
    )
    loc_a, loc_b = rng.sample(T.LOCATIONS, 2)
    final_start, final_loc = (b_start, loc_b) if revised else (a_start, loc_a)
    fa, fb = datetime_fact("a", a_start), datetime_fact("b", b_start)
    pa, pb = text_fact("pa", "place", loc_a, [loc_a]), text_fact("pb", "place", loc_b, [loc_b])
    t1 = b.when_before_snapshot(rng, 3, 9)
    subj = f"{title} 제안"
    specs = [
        MsgSpec(
            sender=who,
            to=["user"],
            cc=others,
            subject=subj,
            sent_at=t1,
            lines=[
                f"{title}을 {fa.surface_forms[0]}에 {loc_a}에서 하면 좋겠습니다.",
                "가능하신지 알려 주세요.",
            ],
            intent="미팅 시각과 장소를 제안하는 메일",
            required=[fa, pa],
            forbidden=[fb, pb] if revised else [],
        )
    ]
    if revised:
        specs.append(
            MsgSpec(
                sender=who,
                to=["user"],
                cc=others,
                subject=f"Re: {subj} (수정 제안)",
                sent_at=t1 + timedelta(hours=4 + rng.randrange(30)),
                lines=[
                    "앞선 제안을 수정합니다.",
                    f"{title}을 {fb.surface_forms[0]}에 {loc_b}에서 진행하면 좋겠습니다.",
                ],
                intent="앞선 미팅 제안을 새 시각·장소로 바꾸는 메일(바뀐 값을 분명히 밝힌다)",
                required=[fb, pb],
            )
        )
    if specs[-1].sent_at >= b.snapshot:
        specs[-1].sent_at = b.snapshot - timedelta(hours=2)
    out = b.thread(acct, specs, rng)
    conflicts: list[ArtifactRef] = []
    if _idx(b, "meeting_request") % 3 == 0:
        cs = final_start + timedelta(minutes=30)
        if b.place_ok(acct, cs, cs + timedelta(hours=1)):
            conflicts.append(
                b.add_event(
                    acct,
                    title=rng.choice(T.GENERIC_TITLES),
                    start=cs,
                    end=cs + timedelta(hours=1),
                    rng=rng,
                )
            )
    s.evidence.append(out.messages[-1])
    s.distractors += out.messages[:-1] + conflicts
    emails = sorted({p.email, *(b.person(o).email for o in others)})
    b.end(
        s,
        MeetingRequestTruth(
            message=out.messages[-1],
            thread_id=out.thread_id,
            requester_name=p.name,
            requester_email=p.email,
            title=title,
            proposed=span(final_start, final_start + timedelta(hours=1)),
            time_zone=b.acc[acct].user.time_zone,
            location=final_loc,
            attendee_emails=emails,
            revised=revised,
            superseded_message=out.messages[0] if revised else None,
            conflicting_events=conflicts,
        ),
    )


def event_change_target(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    action = "move" if _idx(b, "event_change_target") % 2 == 0 else "cancel"
    day = future_weekday(b, s, 3, MAX_FUTURE_DAYS - 7)
    start = b.local(acct, day, 10 + rng.randrange(6), 30 * rng.randrange(2))
    dur = timedelta(minutes=rng.choice([30, 60, 90]))
    who = organizer_for(b, s, start)
    p = b.person(who)
    title = f"{rng.choice(T.MEETING_KINDS)} ({p.company})"
    ref = b.add_event(
        acct,
        title=title,
        start=start,
        end=start + dur,
        attendees=[who],
        organizer="user",
        location=rng.choice(T.LOCATIONS),
        rng=rng,
    )
    t_req = b.when_before_snapshot(rng, 0.3, 7)
    new: tuple[datetime, datetime] | None = None
    if action == "move":

        def make(r: random.Random) -> datetime:
            d = future_weekday(b, s, 3, MAX_FUTURE_DAYS)
            return b.local(acct, d, 10 + r.randrange(6), 30 * r.randrange(2))

        ns = retry(
            rng,
            make,
            lambda v: _compatible([datetime_fact("o", start)], [datetime_fact("n", v)]),
        )
        fn = datetime_fact("new_time", ns)
        new = (ns, ns + dur)
        spec = MsgSpec(
            sender=who,
            to=["user"],
            subject=f"{title} 일정 변경 요청",
            sent_at=t_req,
            lines=[f"{title} 일정을 {fn.surface_forms[0]}로 옮길 수 있을까요?"],
            intent="이미 잡힌 미팅을 새 시각으로 옮겨 달라고 요청하는 메일",
            required=[fn],
        )
    else:
        fd = text_fact("cannot", "decision", "참석 불가", ["참석이 어렵", "참석하기 어렵"])
        spec = MsgSpec(
            sender=who,
            to=["user"],
            subject=f"{title} 취소 요청",
            sent_at=t_req,
            lines=[f"{title}에 참석이 어렵게 되어 일정을 취소해 주셨으면 합니다."],
            intent="사정이 생겨 잡힌 미팅을 취소해 달라고 요청하는 메일",
            required=[fd],
        )
    out = b.thread(acct, [spec], rng)
    s.evidence += [ref, out.messages[0]]
    b.end(
        s,
        EventChangeTargetTruth(
            action=action,
            event=ref,
            title=title,
            time_zone=b.acc[acct].user.time_zone,
            current=span(start, start + dur),
            new=span(*new) if new else None,
            request_message=out.messages[0],
            attendee_emails=[p.email],
        ),
    )


def mismatch(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    fields_choice = rng.choice([["time"], ["location"], ["time", "location"]])
    day = future_weekday(b, s, 3, MAX_FUTURE_DAYS)
    cal_start = b.local(acct, day, 10 + rng.randrange(6), 30 * rng.randrange(2))
    dur = timedelta(minutes=rng.choice([30, 60]))
    cal_loc, mail_loc_alt = rng.sample(T.LOCATIONS, 2)
    mail_loc = mail_loc_alt if "location" in fields_choice else cal_loc

    def make(r: random.Random) -> datetime:
        if "time" not in fields_choice:
            return cal_start
        d = future_weekday(b, s, 3, MAX_FUTURE_DAYS)
        return b.local(acct, d, 10 + r.randrange(6), 30 * r.randrange(2))

    mail_start = retry(
        rng,
        make,
        lambda v: (
            ("time" not in fields_choice)
            or _compatible([datetime_fact("c", cal_start)], [datetime_fact("m", v)])
        ),
    )
    org = organizer_for(b, s, cal_start)
    title = rng.choice(T.MEETING_KINDS)
    t_mail = b.when_before_snapshot(rng, 10, 25)
    t_upd = min(t_mail + timedelta(days=rng.randrange(2, 8)), b.snapshot - timedelta(days=1))
    fm = datetime_fact("mail_time", mail_start)
    fc = datetime_fact("cal_time", cal_start)
    pm = text_fact("mail_place", "place", mail_loc, [mail_loc])
    pc = text_fact("cal_place", "place", cal_loc, [cal_loc])
    forbidden: list[Fact] = []
    if "time" in fields_choice:
        forbidden.append(fc)
    if "location" in fields_choice:
        forbidden.append(pc)
    ref = b.add_event(
        acct,
        title=title,
        start=cal_start,
        end=cal_start + dur,
        attendees=[org],
        organizer=org,
        location=cal_loc,
        updated_at=t_upd,
        rng=rng,
    )
    out = b.thread(
        acct,
        [
            MsgSpec(
                sender=org,
                to=["user"],
                subject=f"{title} 일정 안내",
                sent_at=t_mail,
                lines=[
                    f"{title} 회의를 {fm.surface_forms[0]}에 진행합니다.",
                    f"장소는 {mail_loc}입니다.",
                ],
                intent=f"{title} 회의의 시각과 장소를 안내하는 메일(이후 캘린더가 바뀌었다)",
                required=[fm, pm],
                forbidden=forbidden,
            )
        ],
        rng,
    )
    s.evidence.append(ref)
    s.distractors.append(out.messages[0])
    b.end(
        s,
        MismatchTruth(
            event=ref,
            message=out.messages[0],
            title=title,
            time_zone=b.acc[acct].user.time_zone,
            fields=fields_choice,
            calendar_span=span(cal_start, cal_start + dur),
            mail_start=mail_start,
            calendar_location=cal_loc,
            mail_location=mail_loc,
        ),
    )


def deadline(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    project, item = pick_project_item(b, s, "deadline")
    tz = b.acc[acct].tz

    def make(r: random.Random) -> datetime:
        base = s.as_of.astimezone(tz) + timedelta(hours=r.randrange(5, 110))
        while base.weekday() >= 5:
            base += timedelta(days=1)
        return base.replace(hour=10 + r.randrange(9), minute=30 * r.randrange(2), second=0)

    due = retry(
        rng, make, lambda v: v.astimezone(UTC_) - s.as_of.astimezone(UTC_) >= timedelta(hours=3)
    )
    f = datetime_fact("deadline", due, "deadline")
    others = [x for x in T.ITEMS if x != item]
    rng.shuffle(others)
    other_item = next(
        (x for x in others if take_question_key(b, s, question_key(project, x, "deadline"))), None
    )
    if other_item is None:
        raise WorldBuildError(f"{s.user_key}: 다른 마감 품목이 없다")
    od = due + timedelta(days=rng.randrange(2, 6))
    while od.weekday() >= 5:
        od += timedelta(days=1)
    fo = datetime_fact("other", od, "deadline")
    if not _compatible([f], [fo]):
        od += timedelta(days=1)
        fo = datetime_fact("other", od, "deadline")
    who = mail_person(b, s)
    t_mail = b.when_before_snapshot(rng, 1, 10)
    use_file = _idx(b, "deadline") % 2 == 1
    if use_file:
        title = f"{project} {rng.choice(['출시', '정산', '교육'])} 일정 계획서"
        n = 0
        while title in b.acc[acct].titles:
            n += 1
            title = f"{title} {n}"
        src = b.doc(
            acct,
            title=title,
            genre="plan",
            modified_at=t_mail,
            owner=who,
            lines=[f"{item} 제출 마감: {f.surface_forms[0]}."],
            intent=f"{project} 계획서에 {item} 제출 마감을 적는다",
            required=[f],
            project=project,
            topic=item,
        )
    else:
        out = b.thread(
            acct,
            [
                MsgSpec(
                    sender=who,
                    to=["user"],
                    subject=f"[마감 안내] {project} {item} 제출",
                    sent_at=t_mail,
                    lines=[f"{project} {item} 제출 마감은 {f.surface_forms[0]}입니다."],
                    intent=f"{project} {item}의 제출 마감 시각을 알리는 메일",
                    required=[f],
                    forbidden=[fo],
                    project=project,
                    topic=item,
                )
            ],
            rng,
        )
        src = out.messages[0]
    od_out = b.thread(
        acct,
        [
            MsgSpec(
                sender=who,
                to=["user"],
                subject=f"[마감 안내] {project} {other_item} 제출",
                sent_at=t_mail - timedelta(hours=1 + rng.randrange(40)),
                lines=[f"{project} {other_item} 제출 마감은 {fo.surface_forms[0]}입니다."],
                intent=f"{project}의 다른 항목({other_item}) 마감을 알리는 메일",
                required=[fo],
                forbidden=[f],
                project=project,
                topic=other_item,
            )
        ],
        rng,
    )
    s.evidence.append(src)
    s.distractors.append(od_out.messages[0])
    hours = (due.astimezone(UTC_) - s.as_of.astimezone(UTC_)).total_seconds() / 3600
    b.end(
        s,
        DeadlineTruth(
            source=src,
            project=project,
            item=item,
            question_key=question_key(project, item, "deadline"),
            deadline_at=due,
            time_zone=b.acc[acct].user.time_zone,
            as_of=s.as_of,
            hours_until=round(hours, 2),
            days_until=(due.date() - s.as_of.astimezone(tz).date()).days,
            fact=f,
        ),
    )
