"""문서·부재·모호성·검색 시나리오: doc_fact, doc_version, absent, ambiguous, search_topic."""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from orbit.evaldata.v1 import templates as T
from orbit.evaldata.v1.core import Builder, MsgSpec, Scn, WorldBuildError
from orbit.evaldata.v1.facts import amount_fact, date_fact, text_fact
from orbit.evaldata.v1.scen_calendar import person_events
from orbit.evaldata.v1.scen_common import (
    any_project,
    claims_of,
    compatible,
    mail_person,
    next_claim,
    no_promise_facts,
    pool_any,
    retry,
    span,
    take_project,
    take_question_key,
)
from orbit.evaldata.v1.skeleton import (
    AbsentTruth,
    AmbiguousTruth,
    ArtifactRef,
    DocFactTruth,
    DocVersionTruth,
    Fact,
    Interpretation,
    SearchTopicTruth,
)

DOC_KINDS = ("meeting_notes", "quote", "plan")
NOTE_TOPICS = [
    "정기 운영 점검",
    "분기 일정 조율",
    "고객 요구사항 정리",
    "인력 배치",
    "릴리스 준비 현황",
]
PLAN_TOPICS = ["출시", "정산", "교육", "이전", "점검"]


def _idx(b: Builder, type_: str) -> int:
    return b.type_counter[type_] - 1


def _past_day(b: Builder, rng: random.Random, acct: str, lo: int = -40, hi: int = -2) -> date:
    days = b.weekdays(acct, lo, hi)
    return days[rng.randrange(len(days))]


def _unique_title(
    b: Builder, acct: str, make: Callable[[random.Random], str], rng: random.Random
) -> str:
    return retry(rng, make, lambda t: t not in b.acc[acct].titles)


def _notes_title(b: Builder, rng: random.Random, acct: str) -> tuple[str, date]:
    def make(r: random.Random) -> tuple[str, date]:
        d = _past_day(b, r, acct)
        return f"{r.choice(T.MEETING_KINDS)} 회의록 ({d.month}월 {d.day}일)", d

    return retry(rng, make, lambda v: v[0] not in b.acc[acct].titles)


def _decisions(rng: random.Random) -> tuple[Fact, Fact]:
    (v1, f1), (v2, f2) = rng.sample(T.DECISIONS, 2)
    return text_fact("decision", "decision", v1, f1), text_fact(
        "other_decision", "decision", v2, f2
    )


def _notes_doc(
    b: Builder, s: Scn, who: str, title: str, d: date, fact: Fact, other: Fact, project: str | None
) -> ArtifactRef:
    rng = s.rng
    topic = rng.choice(NOTE_TOPICS)
    return b.doc(
        s.account_id,
        title=title,
        genre="meeting_notes",
        modified_at=b.local(s.account_id, d, 18).astimezone(UTC),
        owner=who,
        lines=[
            f"{topic} 안건을 논의했습니다.",
            f"결정 사항: {fact.surface_forms[0]}.",
            "다음 회의 전까지 담당자별로 진행 상황을 정리합니다.",
        ],
        intent=f"회의록: {topic}에 대해 논의하고 결정 사항을 남긴다",
        required=[fact],
        forbidden=[other],
        project=project,
        topic=topic,
        doc_date=d,
    )


def _quote_person(b: Builder, s: Scn) -> str:
    """견적서를 보낼 인물. 같은 회사의 견적서 질문이 한 사용자에게 둘 생기지 않게 한다."""
    pool = pool_any(b, s)
    s.rng.shuffle(pool)
    for key in pool:
        if take_question_key(b, s, f"quote|{b.person(key).company}"):
            return key
    raise WorldBuildError(f"{s.user_key}: 견적서를 보낼 회사가 모자란다")


def _quote_title(b: Builder, rng: random.Random, acct: str, company: str) -> tuple[str, date]:
    """제목의 월·일은 문서 날짜(수정 시각의 현지 날짜)와 같다."""

    def make(r: random.Random) -> tuple[str, date]:
        d = _past_day(b, r, acct)
        return f"{company} 견적서 ({d.month}월 {d.day}일)", d

    return retry(rng, make, lambda v: v[0] not in b.acc[acct].titles)


def doc_fact(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    kind = DOC_KINDS[_idx(b, "doc_fact") % 3]
    who = mail_person(b, s)
    if kind == "meeting_notes":
        f1, f2 = _decisions(rng)
        t1, d1 = _notes_title(b, rng, acct)
        ref = _notes_doc(b, s, who, t1, d1, f1, f2, None)
        t2, d2 = _notes_title(b, rng, acct)
        dist = _notes_doc(b, s, who, t2, d2, f2, f1, None)
        fact, title = f1, t1
    elif kind == "quote":
        who = _quote_person(b, s)
        p = b.person(who)
        qkey = f"quote|{p.company}"

        def make(r: random.Random) -> tuple[Fact, Fact]:
            a1 = r.randrange(30, 95) * 100_000
            a2 = a1 + r.choice([-1, 1]) * r.randrange(1, 6) * 100_000
            return amount_fact("amount", a1), amount_fact("other_amount", a2)

        fact, other = retry(rng, make, lambda v: compatible([v[0]], [v[1]]))
        title, d1 = _quote_title(b, rng, acct, p.company)
        ref = b.doc(
            acct,
            title=title,
            genre="quote",
            modified_at=b.local(acct, d1, 15).astimezone(UTC),
            owner=who,
            lines=[
                f"견적 총액은 {fact.surface_forms[0]}입니다.",
                "유효 기간은 발행일로부터 30일입니다.",
            ],
            intent=f"{p.company}가 보낸 견적서: 총액을 밝힌다",
            required=[fact],
            forbidden=[other],
            doc_date=d1,
        )
        who2 = _quote_person(b, s)
        p2 = b.person(who2)
        title2, d2 = _quote_title(b, rng, acct, p2.company)
        dist = b.doc(
            acct,
            title=title2,
            genre="quote",
            modified_at=b.local(acct, d2, 15).astimezone(UTC),
            owner=who2,
            lines=[f"견적 총액은 {other.surface_forms[0]}입니다."],
            intent=f"{p2.company}가 보낸 다른 견적서: 총액을 밝힌다",
            required=[other],
            forbidden=[fact],
            doc_date=d2,
        )
    else:
        project = any_project(b, s)
        title = _unique_title(b, acct, lambda r: f"{project} {r.choice(PLAN_TOPICS)} 계획서", rng)
        topic = title.split()[1]

        def make_d(r: random.Random) -> tuple[Fact, Fact]:
            start = b.snap_day(acct) + timedelta(days=r.randrange(1, 10))
            end = start + timedelta(days=r.randrange(7, 20))
            return date_fact("start", start), date_fact("end", end, "deadline")

        fs, fe = retry(rng, make_d, lambda v: compatible([v[0]], [v[1]]))
        ref = b.doc(
            acct,
            title=title,
            genre="plan",
            modified_at=b.when_before_snapshot(rng, 3, 30),
            owner=who,
            lines=[
                f"착수일은 {fs.surface_forms[0]}입니다.",
                f"완료 목표는 {fe.surface_forms[0]}입니다.",
            ],
            intent=f"{project} {topic} 계획서: 착수일과 완료 목표를 정한다",
            required=[fs, fe],
            project=project,
            topic=topic,
        )
        t2 = _unique_title(b, acct, lambda r: f"{project} {r.choice(PLAN_TOPICS)} 점검 계획서", rng)
        fs2 = date_fact("start", b.snap_day(acct) + timedelta(days=40))
        dist = b.doc(
            acct,
            title=t2,
            genre="plan",
            modified_at=b.when_before_snapshot(rng, 3, 30),
            owner=who,
            lines=[f"착수일은 {fs2.surface_forms[0]}입니다."],
            intent=f"{project}의 다른 계획서: 착수일을 밝힌다",
            required=[fs2],
            project=project,
        )
        fact = fe
    s.evidence.append(ref)
    s.distractors.append(dist)
    qkey = qkey if kind == "quote" else f"{kind}|{title}"
    b.end(s, DocFactTruth(doc_kind=kind, file=ref, title=title, question_key=qkey, fact=fact))


def doc_version(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    project = any_project(b, s)
    who = mail_person(b, s)
    base = retry(
        rng,
        lambda r: f"{project} {r.choice(PLAN_TOPICS)} 견적서",
        lambda t: f"{t} v1" not in b.acc[acct].titles,
    )
    topic = base.split()[1]

    def make(r: random.Random) -> tuple[Fact, Fact]:
        a1 = r.randrange(30, 95) * 100_000
        a2 = a1 + r.choice([-1, 1]) * r.randrange(1, 6) * 100_000
        return amount_fact("previous", a1), amount_fact("latest", a2)

    prev, new = retry(rng, make, lambda v: compatible([v[0]], [v[1]]))
    t1 = b.when_before_snapshot(rng, 30, 50)
    t2 = min(t1 + timedelta(days=rng.randrange(5, 20)), b.snapshot - timedelta(days=1))
    title1, title2 = f"{base} v1", f"{base} v2"
    d1 = b.doc(
        acct,
        title=title1,
        genre="quote",
        modified_at=t1,
        owner=who,
        lines=[f"{project} {topic} 견적 총액은 {prev.surface_forms[0]}입니다."],
        intent=f"{project} {topic} 견적서 첫 판: 총액을 밝힌다",
        required=[prev],
        forbidden=[new],
        project=project,
        topic=topic,
    )
    d2 = b.doc(
        acct,
        title=title2,
        genre="quote",
        modified_at=t2,
        owner=who,
        lines=[f"수정된 견적 총액은 {new.surface_forms[0]}입니다."],
        intent=f"{project} {topic} 견적서 수정판: 바뀐 총액을 밝힌다",
        required=[new],
        forbidden=[prev],
        project=project,
        topic=topic,
    )
    linked: ArtifactRef | None = None
    att: str | None = None
    if rng.random() < 0.5:
        att = f"{title2}.pdf"
        tf = text_fact("title", "document_title", title2, [title2])
        out = b.thread(
            acct,
            [
                MsgSpec(
                    sender=who,
                    to=["user"],
                    subject=f"[{project}] 견적서 수정본 송부",
                    sent_at=min(t2 + timedelta(hours=3), b.snapshot - timedelta(hours=1)),
                    lines=[f"{title2} 파일을 첨부했습니다. 확인 부탁드립니다."],
                    intent="수정된 견적서를 첨부해 보내는 메일(첨부 이름은 문서 제목과 같다)",
                    required=[tf],
                    attachments=[att],
                    project=project,
                    topic=topic,
                )
            ],
            rng,
        )
        linked = out.messages[0]
    s.evidence += [d2] + ([linked] if linked else [])
    s.distractors.append(d1)
    b.end(
        s,
        DocVersionTruth(
            base_title=base,
            question_key=f"version|{base}",
            latest_file=d2,
            latest_title=title2,
            latest=new,
            previous_file=d1,
            previous_title=title1,
            previous=prev,
            linked_message=linked,
            attachment_name=att,
        ),
    )


def _quiet_mail(b: Builder, s: Scn, project: str) -> ArtifactRef:
    rng = s.rng
    who = mail_person(b, s)
    out = b.thread(
        s.account_id,
        [
            MsgSpec(
                sender=who,
                to=["user"],
                subject=f"[{project}] 진행 상황 공유",
                sent_at=b.when_before_snapshot(rng, 5, 40),
                lines=T.business_lines(rng, project, 2),
                intent=f"{project}의 진행 상황을 공유하는 메일(금액·일정 약속은 없다)",
                project=project,
                forbidden=no_promise_facts(),
            )
        ],
        rng,
    )
    return out.messages[0]


def absent(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    horizon = b.local(acct, b.snap_day(acct) + timedelta(days=28), 23)
    scope = span(s.as_of, horizon)
    variant = s.variant
    if variant == "person_no_meeting":
        pkey = next_claim(b, s, "absent_people")
        p = b.person(pkey)
        out = b.thread(
            acct,
            [
                MsgSpec(
                    sender=pkey,
                    to=["user"],
                    subject=f"{p.company} 협업 문의 감사드립니다",
                    sent_at=b.when_before_snapshot(rng, 5, 40),
                    lines=T.business_lines(rng, p.company, 2),
                    intent="지난 협업에 감사하고 안부를 전하는 메일(미팅 약속은 없다)",
                )
            ],
            rng,
        )
        s.distractors.append(out.messages[0])
        truth = AbsentTruth(
            variant="person_no_meeting",
            missing=f"{p.name}님과의 미팅",
            query=p.name,
            person_email=p.email,
            scope=scope,
            related_exist=out.messages,
        )
    elif variant == "no_document":
        used = b.used.setdefault(f"{s.user_key}:absent_doc", set())
        pairs = [x for x in T.ABSENT_TITLES if x[0] not in used]
        if not pairs:
            raise WorldBuildError(f"{s.user_key}: 없는 문서 제목이 모자란다")
        missing, similar = pairs[rng.randrange(len(pairs))]
        used.add(missing)
        who = mail_person(b, s)
        if similar in b.acc[acct].titles:  # 이미 있으면 다시 만들지 않는다(이름 중복 방지)
            related: list[ArtifactRef] = []
        else:
            related = [
                b.doc(
                    acct,
                    title=similar,
                    genre="memo",
                    modified_at=b.when_before_snapshot(rng, 5, 40),
                    owner=who,
                    lines=T.business_lines(rng, similar, 2),
                    intent=f"{similar}: 짧은 메모",
                )
            ]
        s.distractors += related
        truth = AbsentTruth(
            variant="no_document",
            missing=f"'{missing}' 문서",
            query=missing,
            related_exist=related,
        )
    else:
        quiet = claims_of(b, s).quiet_project
        if quiet is None:
            raise WorldBuildError(f"{s.user_key}: 조용한 프로젝트가 없다")
        ref = _quiet_mail(b, s, quiet)
        s.distractors.append(ref)
        if variant == "no_amount":
            truth = AbsentTruth(
                variant="no_amount",
                missing=f"{quiet}의 금액 정보",
                query=quiet,
                related_exist=[ref],
            )
        else:
            truth = AbsentTruth(
                variant="project_no_meeting",
                missing=f"{quiet} 관련 미팅",
                query=quiet,
                scope=scope,
                related_exist=[ref],
            )
    b.end(s, truth)


def _ref_key(r: ArtifactRef) -> tuple[str, str]:
    return (r.kind, r.id)


def ambiguous(b: Builder, s: Scn) -> None:
    rng, acct = s.rng, s.account_id
    variant = s.variant
    if variant == "person_meetings":
        pkey = next_claim(b, s, "meeting_people")
        p = b.person(pkey)
        upcoming, dist = person_events(b, s, pkey, n_upcoming=2 + rng.randrange(2))
        interps = [
            Interpretation(label="다음 미팅 하나", answer_kind="event", gold=upcoming[:1]),
            Interpretation(label="앞으로 잡힌 미팅 전체", answer_kind="event_set", gold=upcoming),
        ]
        s.evidence += upcoming
        s.distractors += dist
        subject = p.name
    elif variant == "same_project_notes":
        project = take_project(b, s, "notes")
        who = mail_person(b, s)
        f1, f2 = _decisions(rng)
        days = sorted(rng.sample(b.weekdays(acct, -40, -2), 2))
        refs: list[ArtifactRef] = []
        titles: list[str] = []
        for i, d in enumerate(days):
            title = f"{project} 회의록 ({d.month}월 {d.day}일)"
            titles.append(title)
            refs.append(
                _notes_doc(b, s, who, title, d, f1 if i == 0 else f2, f2 if i == 0 else f1, project)
            )
        interps = [
            Interpretation(label=titles[0], answer_kind="document", gold=[refs[0]]),
            Interpretation(label=titles[1], answer_kind="document", gold=[refs[1]]),
        ]
        s.evidence += refs
        subject = project
    else:
        idx = b.claim_idx.get(f"{s.user_key}:homonym", 0)
        b.claim_idx[f"{s.user_key}:homonym"] = idx + 1
        pairs = claims_of(b, s).homonym_pairs
        if idx >= len(pairs):
            raise WorldBuildError(f"{s.user_key}: 동명이인 쌍이 모자란다")
        ka, kb = pairs[idx]
        pa, pb = b.person(ka), b.person(kb)
        ua, da = person_events(b, s, ka, n_upcoming=1 + rng.randrange(2))
        ub, db = person_events(b, s, kb, n_upcoming=1 + rng.randrange(2))
        interps = [
            Interpretation(
                label=f"{pa.name}({pa.company})의 다음 미팅", answer_kind="event", gold=ua[:1]
            ),
            Interpretation(
                label=f"{pb.name}({pb.company})의 다음 미팅", answer_kind="event", gold=ub[:1]
            ),
        ]
        s.evidence += ua + ub
        s.distractors += da + db
        subject = pa.name
    if {_ref_key(r) for r in interps[0].gold} == {_ref_key(r) for r in interps[1].gold}:
        raise WorldBuildError(f"{s.scenario_id}: 두 해석의 정답이 같다")
    b.end(
        s,
        AmbiguousTruth(
            variant=variant,
            query_subject=subject,
            as_of=s.as_of,
            interpretations=interps,
        ),
    )


def search_topic(b: Builder, s: Scn) -> None:
    rng = s.rng
    accts = b.search_accts
    big = accts[-1]
    idx = _idx(b, "search_topic")
    form = ("mail_amount", "mail_deadline", "doc")[idx % 3]
    projects = [p.name for p in b.wm.projects if p.user in {b.acc[a].user.key for a in accts}]
    used = b.used.setdefault("search:topics", set())
    options = [(p, n) for p in projects for n in T.SEARCH_NOUNS if f"{p} {n}" not in used]
    if not options:
        raise WorldBuildError("검색 주제어 조합이 모자란다")
    project, noun = options[rng.randrange(len(options))]
    phrase = f"{project} {noun}"
    used.add(phrase)
    contacts = sorted(p.key for p in b.wm.contacts_of(b.acc[big].user.key))
    who = contacts[rng.randrange(len(contacts))]
    t_new = b.when_before_snapshot(rng, 3, 30)
    t_old = t_new - timedelta(days=rng.randrange(5, 25))
    if form == "mail_deadline":

        def make_d(r: random.Random) -> tuple[Fact, Fact]:
            d1 = b.snap_day(big) + timedelta(days=r.randrange(2, 20))
            d2 = d1 + timedelta(days=r.randrange(2, 9))
            return date_fact("previous", d1, "deadline"), date_fact("latest", d2, "deadline")

        old_f, new_f = retry(rng, make_d, lambda v: compatible([v[0]], [v[1]]))
        old_line = f"{phrase} 마감은 {old_f.surface_forms[0]}입니다."
        new_line = f"{phrase} 마감이 {new_f.surface_forms[0]}로 확정되었습니다."
    else:

        def make_a(r: random.Random) -> tuple[Fact, Fact]:
            a1 = r.randrange(30, 95) * 100_000
            a2 = a1 + r.choice([-1, 1]) * r.randrange(1, 6) * 100_000
            return amount_fact("previous", a1), amount_fact("latest", a2)

        old_f, new_f = retry(rng, make_a, lambda v: compatible([v[0]], [v[1]]))
        old_line = f"{phrase} 금액은 {old_f.surface_forms[0]}입니다."
        new_line = f"{phrase} 최종 금액은 {new_f.surface_forms[0]}입니다."
    rel: dict[str, int] = {}
    kinds: dict[str, str] = {}

    def mail(
        sender: str,
        subj: str,
        t: datetime,
        lines: list[str],
        intent: str,
        req: list[Fact],
        forb: list[Fact],
    ) -> ArtifactRef:
        out = b.add_thread(
            accts,
            [
                MsgSpec(
                    sender=sender,
                    to=["user"],
                    subject=subj,
                    sent_at=t,
                    lines=lines,
                    intent=intent,
                    required=req,
                    forbidden=forb,
                    project=project,
                    topic=phrase,
                )
            ],
            rng=rng,
        )
        for a in accts:
            b.mail_group_of_body[out[a].messages[0].body_id or ""] = 0
        return out[big].messages[0]

    if form == "doc":
        title = f"{phrase} 견적서" if new_f.kind == "amount" else f"{phrase} 일정표"
        if title in b.acc[big].titles:
            title = f"{title} ({idx})"
        docs = b.add_doc(
            accts,
            title=title,
            genre="quote" if new_f.kind == "amount" else "plan",
            modified_at=t_new,
            owner=who,
            lines=[new_line],
            intent=f"{phrase}의 최신 사실을 밝히는 문서",
            required=[new_f],
            forbidden=[old_f],
            project=project,
            topic=phrase,
        )
        ev = docs[big]
    else:
        ev = mail(
            who,
            f"[{project}] {noun} 최종 안내",
            t_new,
            [new_line],
            f"{phrase}의 최신 값을 알리는 메일",
            [new_f],
            [old_f],
        )
    old_ref = mail(
        who,
        f"[{project}] {noun} 안내",
        t_old,
        [old_line],
        f"{phrase}의 이전 값을 알리는 메일(이후 바뀌었다)",
        [old_f],
        [new_f],
    )
    if ev.body_id is None or old_ref.body_id is None:
        raise WorldBuildError("본문 ID가 없다")
    rel[ev.body_id] = 2
    kinds[ev.body_id] = "document" if form == "doc" else "mail"
    rel[old_ref.body_id] = 1
    kinds[old_ref.body_id] = "mail"
    # 같은 주제어를 쓰지만 답과 무관한 메일(관련도 0). 메일함 크기 단계마다 하나씩 퍼뜨린다.
    for g in range(len(accts)):
        if g == 0 and idx % 5 >= 3:
            continue
        sender = contacts[rng.randrange(len(contacts))]
        out = b.add_thread(
            accts[g:],
            [
                MsgSpec(
                    sender=sender,
                    to=["user"],
                    subject=f"{noun} 관련 별건 문의",
                    sent_at=b.when_before_snapshot(rng, 2, 54),
                    lines=[
                        f"{project}와 {noun} 이야기가 나온 김에 다른 건으로 연락드립니다.",
                        "이 건은 별도 업무입니다.",
                    ],
                    intent=f"'{phrase}'와 같은 단어를 쓰지만 그 사실과는 무관한 일반 문의 메일",
                    project=project,
                    topic=phrase,
                    decoy_for=s.scenario_id,
                )
            ],
            rng=rng,
        )
        ref = out[big].messages[0]
        if ref.body_id is None:
            raise WorldBuildError("본문 ID가 없다")
        rel[ref.body_id] = 0
        kinds[ref.body_id] = "mail"
        for a in accts[g:]:
            b.mail_group_of_body[out[a].messages[0].body_id or ""] = g
        b.decoys.setdefault(s.scenario_id, []).append(ref.body_id)
    s.evidence.append(ev)
    s.distractors.append(old_ref)
    b.end(
        s,
        SearchTopicTruth(
            topic_phrase=phrase,
            fact=new_f,
            relevance_by_body=rel,
            artifact_by_body=kinds,
        ),
    )
