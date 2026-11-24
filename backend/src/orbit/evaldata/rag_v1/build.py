"""rag-v1 구조 생성기: 계정·사람·프로젝트·메일 사슬·집계 대상·방해 메일을 코드로 정한다.

LLM과 네트워크를 쓰지 않고 같은 시드·git SHA면 같은 skeleton을 만든다. 산출물은 기존
`orbit.evaldata.v1.skeleton.Skeleton`이라 `orbit.evaldata.v1.realize`로 본문을 쓸 수 있다.

계정마다 프로젝트 10개(묶음, cluster)를 만든다. 프로젝트마다 다음을 서로 다른 메일에 둔다.
- 담당 안내(내부 리드 -> 사용자): 협력사 담당자 이름과 회사. 다중 홉의 1홉.
- 견적 메일(협력사 담당자 -> 사용자, 긴 본문): 금액. 프로젝트 이름을 쓰지 않아 2홉이 필요하다.
- 협력사 담당자와 직접 주고받은 메일 3~5통: 전역 집계(건수) 대상.
- 마감 메일 3통(항목 4개 중 하나는 일부러 없다): 전역 집계(모든 마감일)와 답할 수 없음 대상.
- 킥오프 장소, 결정 사항, (일부) 보증금 메일, 사실 없는 진행 메일 6통.
계정마다 400통이 되도록 뉴스레터·공지·스팸·무관한 동료 메일로 채운다.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from orbit.connectors.contracts import Address, Calendar
from orbit.evaldata.rag_v1.model import Cluster, Deadline, Plan, Triple
from orbit.evaldata.rag_v1.questions import DEPOSIT_ABSENT, blueprints_for, deadline_absent
from orbit.evaldata.v1.build import skeleton_json
from orbit.evaldata.v1.facts import amount_fact, conflicts, date_fact, text_fact
from orbit.evaldata.v1.skeleton import (
    SCHEMA_VERSION,
    TARGET_CHARS,
    BodyContext,
    BodySpec,
    ContextPerson,
    Fact,
    SkAccount,
    Skeleton,
    SkMessage,
    SkPerson,
    SkThread,
)

FAMILIES = ("openai", "anthropic", "google")
N_ACCOUNTS = 4
N_PROJECTS = 10  # 계정당 묶음 수
MAILS_PER_ACCOUNT = 400
DEV_PER_ACCOUNT = 4  # 계정당 dev 묶음 -> 묶음 16개 = 유형별 dev 16문항
SNAPSHOT = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
LAST_MAIL_DAY = date(2026, 11, 9)
ITEMS = ("시안 제출", "중간 보고", "최종 납품", "검수 완료")

SURNAMES = "김이박최정강조윤장임한오서신권황안송류홍"
GIVENS = [
    "민수",
    "서연",
    "지훈",
    "하은",
    "도윤",
    "수빈",
    "예준",
    "지우",
    "현우",
    "서윤",
    "준서",
    "지민",
    "건우",
    "유나",
    "시우",
    "채원",
    "민재",
    "다은",
    "재원",
    "소영",
]
# 일반 명사와 겹치지 않는 합성 코드명(어간 10 x 어미 4 = 40개). 본문 검사 오탐을 피한다.
PROJECTS = [
    stem + tail
    for stem in ("벨라", "노르", "카젤", "리모", "타니", "젠빌", "쿠렌", "미솔", "바르", "도넬")
    for tail in ("딘", "스", "온", "릭")
]
VENDOR_PREFIX = [
    "아르온",
    "루미",
    "벼리",
    "에버솔",
    "담솔",
    "카펠",
    "노바르",
    "젠타",
    "비올",
    "마레온",
]
VENDOR_SUFFIX = ["정밀", "소프트", "물산", "기획"]
INTERNAL = ("마루빛랩", "은결랩", "청람랩", "솔바람랩")
COLLEAGUE_ROLES = ("기획", "디자이너", "개발", "운영", "영업", "재무")
DECISIONS = (
    "격주 점검 회의로 전환",
    "외주 디자인 범위 축소",
    "시안은 두 가지로 압축",
    "출시 일정을 한 주 연기",
    "검수 인원을 두 명으로 확대",
    "보고 형식을 한 장 요약으로 통일",
    "예산 집행을 단계별로 분할",
    "외부 공유 자료는 PDF로만 배포",
    "자료 보관 위치를 공용 폴더로 통일",
    "시범 운영 기간을 2주로 설정",
)
PLACES = (
    "본관 3층 회의실",
    "별관 대회의실",
    "2층 세미나실",
    "지하 1층 협업 공간",
    "5층 소회의실 B",
    "7층 라운지 회의석",
    "본관 1층 로비 미팅룸",
    "별관 2층 교육실",
    "4층 프로젝트룸",
    "6층 대회의실 A",
)
NEWSLETTER = (
    "분기 업계 동향 요약",
    "주간 생산성 팁",
    "신기술 소식 모음",
    "물류 시장 소식",
    "디자인 트렌드 노트",
)
NOTICE = (
    "사내 시스템 점검 안내",
    "주차장 이용 안내",
    "휴가 신청 마감 공지",
    "보안 교육 이수 안내",
    "복지 포인트 안내",
)
SPAM = ("특별 할인 안내", "무료 체험 이벤트", "회원 등급 상향 혜택", "한정 수량 특가")
CHATTER = (
    "진행 상황 공유",
    "자료 확인 요청",
    "일반 문의",
    "검토 의견 전달",
    "협업 요청 사항 정리",
    "감사 인사",
)
OFFICE = (
    "주간 업무 공유",
    "사무실 비품 요청",
    "교육 자료 전달",
    "온보딩 문서 확인",
    "장비 신청 문의",
    "복지 제도 문의",
)

LATE = (
    " 필수 사실은 본문 후반부(마지막 두 문단 안)에서 처음 언급하고, 앞부분은 배경과 경과 설명으로"
    " 길게 채운다. 본문은 900자 이상으로 쓴다."
)
# 결정 메일에 결정이 여럿 들어가면 정답이 하나라는 전제가 깨진다(사람 검수에서 5통 발견).
MULTI_DECISION = (
    "둘째",
    "두 번째 결정",
    "셋째",
    "몇 가지 중요한 결정",
    "몇 가지 결정",
    "결정 사항은 아래",
    "결정된 사항은 아래",
    "첫째",
    "결정된 사항들",
)
# 결정 문구 없이 결정이 있었다고만 말하는 안내 문장. 필수 사실이 우연히 맞는 것을 막는다.
MULTI_DECISION_PATTERNS = (
    r"몇\s*가지[^.\n]{0,20}(결정|사항)",
    r"결정[^.\n]{0,30}회의록",
    r"회의록[^.\n]{0,30}(결정|정리되었|정리된)",
)
# 마감일 메일이 아닌 클러스터 메일에 날짜 기한이 있으면 "마감일 전부" 정답이 불완전해진다.
# 날짜 바로 뒤(요일 괄호만 허용)의 "까지", 또는 같은 문장의 날짜+마감·기한. "오후 2시까지"는 통과.
_MD = r"(?:\d{1,2}월\s*\d{1,2}일|(?<!\d)\d{1,2}/\d{1,2}(?!\d))"
# 월 없는 "24일"도 날짜로 본다. "2026/11/24"의 일부(11/24)는 날짜로 보지 않는다.
_DATE = (
    r"(?:\d{1,2}월\s*\d{1,2}일|(?<![\d월])(?<!월\s)\d{1,2}일|(?<![\d/])\d{1,2}/\d{1,2}(?![\d/]))"
)
_WEEKDAY = r"(?:(?:\([월화수목금토일](?:요일)?\)|[월화수목금토일]요일)\s*)?"
# "부터"가 끼면 걸리지 않는다
_TIME = r"(?:(?:(?:오전|오후)\s*)?\d{1,2}(?::\d{2}|시(?:\s*\d{1,2}분)?)\s*)?"
# 요일만 있는 "금요일까지"는 정답이 날짜라 여기서 거르지 않는다.
# 날짜 기간("11월 20일부터 11월 24일까지")의 끝 날짜도 마감이라 걸러낸다. 누락이 오탐보다 나쁘다.
NO_DEADLINE_PATTERNS = (
    r"(?<!\d)" + _DATE + r"\s*" + _WEEKDAY + _TIME + r"(?:(?:전\s*)?(?:까지|안에)|이내|내에|전에)",
    _MD + r"[^.!?\n]*(?:마감|기한)",
    r"(?:마감|기한)[^.!?\n]*" + _MD,
)
NO_NEW = "날짜·시각·금액·장소·결정 사항은 새로 말하지 않는다."

CONFIG: dict[str, Any] = {
    "n_accounts": N_ACCOUNTS,
    "n_projects": N_PROJECTS,
    "mails_per_account": MAILS_PER_ACCOUNT,
    "dev_per_account": DEV_PER_ACCOUNT,
    "items": ITEMS,
    "pools": [
        SURNAMES,
        GIVENS,
        PROJECTS,
        VENDOR_PREFIX,
        VENDOR_SUFFIX,
        INTERNAL,
        DECISIONS,
        PLACES,
    ],
    "late": LATE,
}
CONFIG_SHA256 = hashlib.sha256(
    json.dumps(CONFIG, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()


def _rng(seed: int, *parts: object) -> random.Random:
    return random.Random(":".join([str(seed), *map(str, parts)]))  # noqa: S311  재현성용 난수


@dataclass(frozen=True)
class Who:
    key: str
    name: str
    email: str
    company: str
    role: str
    relation: str  # self | colleague | vendor | auto

    def ctx(self) -> ContextPerson:
        label = {
            "self": "사용자 본인",
            "colleague": "동료",
            "vendor": "협력사 담당자",
            "auto": "자동 발송",
        }[self.relation]
        return ContextPerson(name=self.name, relation=label, company=self.company)

    def addr(self) -> Address:
        return Address(email=self.email, name=self.name)


@dataclass
class Draft:
    key: str
    thread_key: str
    sender: Who
    to: list[Who]
    subject: str
    sent_at: datetime
    genre: str
    length: str
    family: str
    intent: str
    template: str
    required: list[Fact] = field(default_factory=list)
    forbidden: list[Fact] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    project: str | None = None
    topic: str | None = None
    labels: tuple[str, ...] = ("INBOX",)
    prior: list[str] = field(default_factory=list)
    id: str = ""
    body_id: str = ""
    thread_id: str = ""


def _deadline_days(rng: random.Random) -> list[date]:
    """마감일 3개. 어느 날짜의 표기도 다른 날짜 표기의 부분 문자열이 아니게 다시 뽑는다."""
    while True:
        d0 = date(2026, 11, 16) + timedelta(days=rng.randint(0, 10))
        d1 = d0 + timedelta(days=rng.randint(4, 9))
        days = [d0, d1, d1 + timedelta(days=rng.randint(4, 9))]
        facts = [date_fact(f"d{i}", d) for i, d in enumerate(days)]
        if not any(conflicts(a, b) for i, a in enumerate(facts) for b in facts[i + 1 :]):
            return days


def _at(day: date, rng: random.Random) -> datetime:
    return datetime(day.year, day.month, day.day, rng.randint(0, 8), rng.randint(0, 59), tzinfo=UTC)


class _Pool:
    """중복 없이 하나씩 꺼내는 결정적 풀."""

    def __init__(self, items: list[str], rng: random.Random) -> None:
        self._items = list(items)
        rng.shuffle(self._items)

    def take(self) -> str:
        return self._items.pop()


@dataclass
class _AccountBuild:
    acct: str
    user: Who
    drafts: list[Draft]
    people: list[Who]
    clusters_raw: list[dict[str, Any]]


def _build_account(
    seed: int, a: int, names: _Pool, projects: _Pool, vendors: _Pool
) -> _AccountBuild:
    acct = f"acct-r{a}"
    rng = _rng(seed, "acct", a)
    people: list[Who] = []

    def person(key: str, company: str, role: str, relation: str, domain: str) -> Who:
        who = Who(key, names.take(), f"{key}@{domain}", company, role, relation)
        people.append(who)
        return who

    internal = INTERNAL[a - 1]
    user = person(f"user-r{a}", internal, "담당 매니저", "self", "example.org")
    colleagues = [
        person(f"col-r{a}-{n}", internal, r, "colleague", f"in{a}.example.com")
        for n, r in enumerate(COLLEAGUE_ROLES)
    ]
    autos = {
        kind: [
            Who(
                f"{kind}-r{a}-{n}",
                f"{label} {n + 1}",
                f"{kind}{n}@{kind}{a}.example.com",
                "",
                "발송 서비스",
                "auto",
            )
            for n in range(count)
        ]
        for kind, label, count in (
            ("news", "소식 편집부", 3),
            ("notice", "공지 알림", 2),
            ("spam", "혜택 센터", 2),
        )
    }
    for group in autos.values():
        people.extend(group)

    drafts: list[Draft] = []
    raws: list[dict[str, Any]] = []
    places = _Pool(list(PLACES), rng)
    decisions = _Pool(list(DECISIONS), rng)
    used_amounts: set[int] = set()
    for j in range(N_PROJECTS):
        mail_start = len(drafts)
        g = (a - 1) * N_PROJECTS + j
        rp = _rng(seed, "proj", a, j)
        project, company = projects.take(), vendors.take()
        contact = person(f"ven-r{a}-{j}", company, "협력사 담당", "vendor", f"v{a}{j}.example.com")
        lead, other = colleagues[j % 6], colleagues[(j + 1) % 6]
        fam = FAMILIES[g % 3]
        start = date(2026, 8, 3) + timedelta(days=rp.randint(0, 60))
        place, decision = places.take(), decisions.take()
        won = rp.randrange(3_000_000, 95_000_000, 10_000)
        while won in used_amounts:
            won += 10_000
        used_amounts.add(won)
        missing = ITEMS[j % 4]
        items = [x for x in ITEMS if x != missing]
        days = _deadline_days(rp)
        # 질문이 묻지만 메일에 없어야 하는 값: realize 검사가 이 표현을 막는다.
        absent = [text_fact("absent_item", "item", missing, deadline_absent(missing))]
        if j % 4 != 3:  # 보증금 메일이 없는 프로젝트
            absent.append(text_fact("absent_deposit", "item", "보증금", list(DEPOSIT_ABSENT)))
        key = f"{a}-{j}"

        def add(
            suffix: str,
            day: date,
            sender: Who,
            to: list[Who],
            subject: str,
            genre: str,
            length: str,
            intent: str,
            template: str,
            *,
            required: list[Fact] | None = None,
            forbidden: list[Fact] | None = None,
            thread: str | None = None,
            labels: tuple[str, ...] = ("INBOX",),
            prior: list[str] | None = None,
            topic: str | None = None,
            project_name: str | None = None,
            _key: str = key,
            _fam: str = fam,
            _rp: random.Random = rp,
            _absent: list[Fact] = absent,
        ) -> Draft:
            d = Draft(
                key=f"{_key}-{suffix}",
                thread_key=thread or f"{_key}-{suffix}",
                sender=sender,
                to=to,
                subject=subject,
                sent_at=_at(min(day, LAST_MAIL_DAY), _rp),
                genre=genre,
                length=length,
                family=_fam,
                intent=intent,
                template=template,
                required=required or [],
                forbidden=[*(forbidden or []), *_absent],
                project=project_name,
                topic=topic,
                labels=labels,
                prior=prior or [],
            )
            drafts.append(d)
            return d

        assign = add(
            "assign",
            start,
            lead,
            [user],
            f"[{project}] 협력사 담당자 안내",
            "colleague",
            "medium",
            f"{project} 프로젝트의 협력사 담당자를 안내한다. 협력사 이름과 담당자 이름을 쓴다.",
            f"{project} 프로젝트 협력사 담당자는 {company}의 {contact.name}님입니다. "
            "앞으로 협의는 이 담당자와 진행해 주세요.",
            required=[
                text_fact("contact", "person", contact.name, [contact.name]),
                text_fact("vendor", "item", company, [company]),
            ],
            topic="협력사 담당자 안내",
            project_name=project,
        )
        kick = add(
            "kick",
            start + timedelta(days=2),
            lead,
            [user],
            f"[{project}] 킥오프 회의 안내",
            "colleague",
            "long",
            "킥오프 회의 장소를 안내한다." + LATE,
            f"{project} 킥오프 회의는 {place}에서 진행합니다.",
            required=[text_fact("place", "place", place, [place])],
            topic="킥오프 회의",
            project_name=project,
        )
        dec = add(
            "decision",
            start + timedelta(days=7),
            other,
            [user],
            f"[{project}] 결정 사항 공유",
            "colleague",
            "long",
            "회의에서 내린 결정 사항을 공유한다." + LATE,
            f"{project} 회의 결정 사항: {decision}.",
            required=[text_fact("decision", "decision", decision, [decision])],
            forbidden=[
                text_fact(
                    "multi_decision",
                    "decision",
                    "여러 결정",
                    [*MULTI_DECISION, f"{decision} 예정", *(x for x in DECISIONS if x != decision)],
                )
            ],
            topic="결정 사항 공유",
            project_name=project,
        )
        # 협력사 담당자와 직접 주고받은 사슬. 프로젝트 이름은 일부러 쓰지 않는다.
        n_thread = 3 + j % 3
        ban = [text_fact("project", "item", project, [project])]
        thread_drafts: list[Draft] = []
        for i in range(n_thread):
            from_vendor = i % 2 == 0
            first = i == 0
            d = add(
                f"t{i}",
                start + timedelta(days=4 + 2 * i),
                contact if from_vendor else user,
                [user] if from_vendor else [contact],
                "견적 송부 안내" if first else "Re: 견적 송부 안내",
                "vendor",
                "long" if first else ("medium" if from_vendor else "short"),
                (
                    "견적 금액을 안내한다. 프로젝트 이름은 쓰지 않고 '요청하신 건'이라고만"
                    " 부른다." + LATE
                    if first
                    else f"견적 내용을 확인한 뒤 진행 방식을 협의한다. {NO_NEW}"
                ),
                (
                    f"요청하신 건의 견적 금액은 {amount_fact('q', won).surface_forms[0]}입니다."
                    if first
                    else "견적 내용을 확인했습니다. 진행 방식은 다시 협의하겠습니다."
                ),
                required=[amount_fact("quote", won)] if first else [],
                forbidden=ban,
                thread=f"{key}-thr",
                labels=("INBOX",) if from_vendor else ("SENT",),
                prior=[x.key for x in thread_drafts],
                topic="견적 안내",
            )
            thread_drafts.append(d)
        deadline_drafts: list[Draft] = []
        for i, item in enumerate(items):
            fact = date_fact(f"dl{i}", days[i], kind="deadline")
            length = "long" if i != 1 else "medium"
            deadline_drafts.append(
                add(
                    f"dl{i}",
                    start + timedelta(days=10 + 3 * i),
                    lead,
                    [user],
                    f"[{project}] {item} 마감 안내",
                    "colleague",
                    length,
                    f"{item}의 마감일을 안내한다." + (LATE if length == "long" else ""),
                    f"{project} {item} 마감일은 {fact.surface_forms[0]}입니다.",
                    required=[text_fact(f"it{i}", "item", item, [item]), fact],
                    topic=f"{item} 마감",
                    project_name=project,
                )
            )
        deposit: Draft | None = None
        deposit_won: int | None = None
        if j % 4 == 3:
            deposit_won = rp.randrange(1_000_000, 9_000_000, 100_000)
            deposit = add(
                "deposit",
                start + timedelta(days=9),
                lead,
                [user],
                f"[{project}] 계약 보증금 안내",
                "colleague",
                "medium",
                "계약 보증금 금액을 안내한다.",
                f"{project} 계약 보증금은 {amount_fact('d', deposit_won).surface_forms[0]}입니다.",
                required=[amount_fact("deposit", deposit_won)],
                topic="계약 보증금",
                project_name=project,
            )
        for i in range(6):
            span = (LAST_MAIL_DAY - start).days
            mine = i % 3 == 2
            subject = CHATTER[i]
            add(
                f"ch{i}",
                start + timedelta(days=rp.randint(1, span)),
                user if mine else (lead if i % 2 == 0 else other),
                [lead] if mine else [user],
                f"[{project}] {subject}",
                "colleague",
                "short" if i % 2 else "medium",
                f"{project} 프로젝트의 {subject}을 나눈다. {NO_NEW}",
                f"{project} 프로젝트 {subject}입니다. 확인 부탁드립니다.",
                labels=("SENT",) if mine else ("INBOX",),
                topic=subject,
                project_name=project,
            )
        dl_keys = {d.key for d in deadline_drafts}
        for d in drafts[mail_start:]:
            if d.key not in dl_keys:
                d.patterns = list(NO_DEADLINE_PATTERNS)
        dec.patterns += MULTI_DECISION_PATTERNS
        raws.append(
            {
                "index": j,
                "family": fam,
                "project": project,
                "vendor": company,
                "contact": contact,
                "lead": lead.name,
                "place": place,
                "decision": decision,
                "won": won,
                "assign": assign,
                "kick": kick,
                "dec": dec,
                "thread": thread_drafts,
                "deadlines": list(zip(items, days, deadline_drafts, strict=True)),
                "missing": missing,
                "deposit": deposit,
                "deposit_won": deposit_won,
                "mails": drafts[mail_start:],
            }
        )

    # 배경 메일: 뉴스레터·공지·스팸·무관한 동료 메일로 400통을 채운다.
    remaining = MAILS_PER_ACCOUNT - len(drafts)
    mix = (("news", 0.4), ("notice", 0.3), ("spam", 0.15), ("office", 0.15))
    counts = [int(remaining * w) for _, w in mix]
    counts[-1] += remaining - sum(counts)
    rb = _rng(seed, "bg", a)
    n = 0
    for (kind, _), count in zip(mix, counts, strict=True):
        for i in range(count):
            n += 1
            day = date(2026, 6, 1) + timedelta(
                days=rb.randint(0, (LAST_MAIL_DAY - date(2026, 6, 1)).days)
            )
            fam = FAMILIES[n % 3]
            if kind == "office":
                sender, topic = colleagues[rb.randrange(6)], OFFICE[i % len(OFFICE)]
                genre, length = "colleague", "medium"
            else:
                sender = autos[kind][i % len(autos[kind])]
                topic = {"news": NEWSLETTER, "notice": NOTICE, "spam": SPAM}[kind][
                    i % len({"news": NEWSLETTER, "notice": NOTICE, "spam": SPAM}[kind])
                ]
                genre = {"news": "newsletter", "notice": "notice", "spam": "spam"}[kind]
                length = "medium" if kind == "news" else "short"
            drafts.append(
                Draft(
                    key=f"{a}-bg-{n}",
                    thread_key=f"{a}-bg-{n}",
                    sender=sender,
                    to=[user],
                    subject=f"{topic} ({i + 1}호)" if kind == "news" else topic,
                    sent_at=_at(day, rb),
                    genre=genre,
                    length=length,
                    family=fam,
                    intent=f"{topic}을 다룬다. 가상의 회사 이름이나 일반 명사만 쓰고 {NO_NEW}",
                    template=f"{topic}을 안내드립니다. 자세한 내용은 담당자에게 문의해 주세요.",
                    topic=topic,
                )
            )
    return _AccountBuild(acct, user, drafts, people, raws)


def _finalize_account(acc: _AccountBuild) -> tuple[SkAccount, dict[str, BodySpec]]:
    drafts = sorted(acc.drafts, key=lambda d: (d.sent_at, d.key))
    a = acc.acct.rsplit("-r", 1)[1]
    by_key = {d.key: d for d in drafts}
    thread_ids: dict[str, str] = {}
    for n, d in enumerate(drafts, start=1):
        d.id, d.body_id = f"msg-r{a}-{n:05d}", f"bm-r{a}-{n:05d}"
        if d.thread_key not in thread_ids:
            thread_ids[d.thread_key] = f"thr-r{a}-{len(thread_ids) + 1:05d}"
        d.thread_id = thread_ids[d.thread_key]
    size: dict[str, int] = {}
    for d in drafts:
        size[d.thread_key] = size.get(d.thread_key, 0) + 1
    position: dict[str, int] = {}
    bodies: dict[str, BodySpec] = {}
    messages: list[SkMessage] = []
    for d in drafts:
        position[d.thread_key] = position.get(d.thread_key, 0) + 1
        ctx = BodyContext(
            subject=d.subject,
            sender=d.sender.ctx(),
            recipients=[w.ctx() for w in d.to],
            thread_position=position[d.thread_key],
            thread_size=size[d.thread_key],
            prior_body_ids=[by_key[k].body_id for k in d.prior],
            project=d.project,
            topic=d.topic,
            intent=d.intent,
            sent_at=d.sent_at,
        )
        bodies[d.body_id] = BodySpec(
            artifact="mail",
            genre=d.genre,
            length_class=d.length,
            target_chars=TARGET_CHARS[d.length],
            context=ctx,
            required_facts=d.required,
            forbidden_facts=d.forbidden,
            forbidden_patterns=d.patterns,
            template_text=d.template,
            generator_family=d.family,
        )
        messages.append(
            SkMessage(
                id=d.id,
                thread_id=d.thread_id,
                from_=d.sender.addr(),
                to=tuple(w.addr() for w in d.to),
                subject=d.subject,
                body_id=d.body_id,
                sent_at=d.sent_at,
                labels=d.labels,
            )
        )
    threads: dict[str, list[str]] = {}
    for d in drafts:
        threads.setdefault(d.thread_id, []).append(d.id)
    account = SkAccount(
        email=acc.user.email,
        name=acc.user.name,
        time_zone="Asia/Seoul",
        purpose="e2e",
        calendars=[Calendar(id=f"cal-r{a}-1", name="업무", time_zone="Asia/Seoul", primary=True)],
        events=[],
        threads=[SkThread(thread_id=t, message_ids=ids) for t, ids in threads.items()],
        messages=messages,
        files=[],
    )
    return account, bodies


def _triples(acct: str, cl: Cluster) -> list[Triple]:
    def t(s: str, st: str, p: str, o: str, ot: str, src: list[str]) -> Triple:
        return Triple(
            account_id=acct,
            subject=s,
            subject_type=st,
            predicate=p,
            object=o,
            object_type=ot,
            sources=src,
        )

    out = [
        t(cl.project, "project", "vendor_contact", cl.contact, "person", [cl.assign_id]),
        t(cl.contact, "person", "works_at", cl.vendor, "company", [cl.assign_id]),
        t(cl.contact, "person", "quoted_amount", f"{cl.quote_won:,}원", "amount", [cl.quote_id]),
        t(cl.project, "project", "kickoff_place", cl.place, "place", [cl.kick_id]),
        t(cl.project, "project", "decision", cl.decision, "decision", [cl.decision_id]),
    ]
    out += [
        t(cl.project, "project", f"deadline:{d.item}", d.day, "date", [d.message_id])
        for d in cl.deadlines
    ]
    if cl.deposit_id and cl.deposit_won:
        out.append(
            t(
                cl.project,
                "project",
                "deposit_amount",
                f"{cl.deposit_won:,}원",
                "amount",
                [cl.deposit_id],
            )
        )
    return out


def _split_key(seed: int, a: int) -> Callable[[int], str]:
    def key(j: int) -> str:
        return hashlib.sha256(f"{seed}:split:{a}:{j}".encode()).hexdigest()

    return key


def build(seed: int, git_sha: str) -> tuple[Skeleton, Plan]:
    """구조 생성. 같은 (seed, git_sha)면 같은 결과다."""
    top = _rng(seed, "top")
    names = _Pool([s + g for s in SURNAMES for g in GIVENS], top)
    projects = _Pool(list(PROJECTS), top)
    vendors = _Pool([p + s for p in VENDOR_PREFIX for s in VENDOR_SUFFIX], top)
    accounts: dict[str, SkAccount] = {}
    bodies: dict[str, BodySpec] = {}
    people: dict[str, SkPerson] = {}
    clusters: list[Cluster] = []
    triples: list[Triple] = []
    for a in range(1, N_ACCOUNTS + 1):
        acc = _build_account(seed, a, names, projects, vendors)
        sk_acct, acct_bodies = _finalize_account(acc)
        accounts[acc.acct] = sk_acct
        bodies.update(acct_bodies)
        for w in acc.people:
            people[w.key] = SkPerson(
                key=w.key,
                name=w.name,
                email=w.email,
                company=w.company,
                role=w.role,
                relation=w.relation,
                works_with=[acc.acct],
            )
        ranked = sorted(range(N_PROJECTS), key=_split_key(seed, a))
        dev = set(ranked[:DEV_PER_ACCOUNT])
        for raw in acc.clusters_raw:
            j = raw["index"]
            contact: Who = raw["contact"]
            clusters.append(
                Cluster(
                    cluster_id=f"c-rag-r{a}-{j:02d}",
                    account_id=acc.acct,
                    index=j,
                    family=raw["family"],
                    split="dev" if j in dev else "test",
                    project=raw["project"],
                    vendor=raw["vendor"],
                    contact=contact.name,
                    contact_email=contact.email,
                    lead=raw["lead"],
                    place=raw["place"],
                    decision=raw["decision"],
                    quote_won=raw["won"],
                    assign_id=raw["assign"].id,
                    kick_id=raw["kick"].id,
                    decision_id=raw["dec"].id,
                    quote_id=raw["thread"][0].id,
                    thread_ids=[d.id for d in raw["thread"]],
                    deadlines=[
                        Deadline(
                            item=item,
                            day=day.isoformat(),
                            message_id=d.id,
                            forms=date_fact("x", day, kind="deadline").surface_forms,
                        )
                        for item, day, d in raw["deadlines"]
                    ],
                    missing_item=raw["missing"],
                    deposit_id=raw["deposit"].id if raw["deposit"] else None,
                    deposit_won=raw["deposit_won"],
                    mail_ids=[d.id for d in raw["mails"]],
                )
            )
    for cl in clusters:
        triples.extend(_triples(cl.account_id, cl))
    blueprints = [bp for cl in clusters for bp in blueprints_for(cl)]
    sk = Skeleton(
        schema_version=SCHEMA_VERSION,
        seed=seed,
        world_model_sha256=CONFIG_SHA256,
        generator_git_sha=git_sha,
        snapshot_time=SNAPSHOT,
        accounts=accounts,
        people=people,
        bodies=bodies,
        scenarios=[],
        search_mailboxes={},
        notes=[
            "rag-v1: RAG 아키텍처 비교용 코퍼스(ADR-0036). 구조·정답은 코드가 계산했다.",
            "scenarios·search_mailboxes는 비어 있다. 질문과 정답은 rag-plan.json에 있다.",
            "bodies[*].template_text는 본문 실현 실패 때 쓰는 결정적 대체 본문이다.",
        ],
    )
    plan = Plan(
        seed=seed,
        generator_git_sha=git_sha,
        config_sha256=CONFIG_SHA256,
        skeleton_sha256=hashlib.sha256(skeleton_json(sk).encode("utf-8")).hexdigest(),
        user_emails={k: v.email for k, v in accounts.items()},
        clusters=clusters,
        blueprints=blueprints,
        triples=triples,
    )
    return sk, plan
