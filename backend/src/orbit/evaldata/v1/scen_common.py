"""시나리오 함수가 함께 쓰는 보조 함수."""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import date, datetime, timedelta

from orbit.evaldata.v1 import templates as T
from orbit.evaldata.v1.core import Builder, Claims, Scn, WorldBuildError
from orbit.evaldata.v1.facts import conflicts
from orbit.evaldata.v1.skeleton import ArtifactRef, Fact, TimeSpan

MAX_FUTURE_DAYS = 27  # 일정은 스냅샷 4주(28일) 후까지


def span(start: datetime, end: datetime) -> TimeSpan:
    return TimeSpan(start=start, end=end)


def claims_of(b: Builder, s: Scn) -> Claims:
    return b.claims[s.user_key]


def contact_keys(b: Builder, s: Scn) -> list[str]:
    return sorted(p.key for p in b.wm.contacts_of(s.user_key))


def pool_any(b: Builder, s: Scn) -> list[str]:
    """과거 일정·메일에 쓸 수 있는 인물: 연락처에서 '일정이 없어야 하는' 인물을 뺀 것."""
    absent = set(claims_of(b, s).absent_people)
    return [k for k in contact_keys(b, s) if k not in absent]


def pool_future(b: Builder, s: Scn) -> list[str]:
    """스냅샷 이후 일정의 참석자로 써도 되는 인물."""
    return sorted(claims_of(b, s).free_people)


def attendees_for(b: Builder, s: Scn, start: datetime, k: int) -> list[str]:
    pool = pool_future(b, s) if start >= b.snapshot else pool_any(b, s)
    return sorted(s.rng.sample(pool, min(k, len(pool))))


def organizer_for(b: Builder, s: Scn, start: datetime) -> str:
    pool = pool_future(b, s) if start >= b.snapshot else pool_any(b, s)
    if not pool:
        raise WorldBuildError(f"{s.user_key}: 일정 주최자로 쓸 인물이 없다")
    return pool[s.rng.randrange(len(pool))]


def mail_person(b: Builder, s: Scn) -> str:
    """메일 발신자로 쓸 인물(메일에는 제한이 없다. 일정이 없어야 하는 인물은 제외)."""
    pool = pool_any(b, s)
    return pool[s.rng.randrange(len(pool))]


def next_claim(b: Builder, s: Scn, which: str) -> str:
    """사용자별로 미리 배정한 인물을 순서대로 하나씩 꺼낸다."""
    keys = getattr(claims_of(b, s), which)
    tag = f"{s.user_key}:{which}"
    i = b.claim_idx.get(tag, 0)
    if i >= len(keys):
        raise WorldBuildError(f"{tag}: 배정된 인물이 모자란다")
    b.claim_idx[tag] = i + 1
    return str(keys[i])


def take_project(b: Builder, s: Scn, tag: str) -> str:
    """같은 사용자·같은 용도에서 한 번만 쓰는 프로젝트 이름."""
    names = [p.name for p in claims_of(b, s).projects]
    used = b.used.setdefault(f"{s.user_key}:{tag}", set())
    for name in names:
        if name not in used:
            used.add(name)
            return name
    raise WorldBuildError(f"{s.user_key}: {tag}에 쓸 프로젝트가 없다")


def any_project(b: Builder, s: Scn) -> str:
    names = [p.name for p in claims_of(b, s).projects]
    return names[s.rng.randrange(len(names))]


def no_promise_facts() -> list[Fact]:
    """금액·일정 약속을 본문에 쓰지 않게 하는 금지 사실(조용한 프로젝트 본문용).

    표기는 대표 표현이다. (c)의 검증이 이 표기를 금지 표기로 취급한다.
    """
    return [
        Fact(
            fact_id="no_amount",
            kind="amount",
            value="*",
            surface_forms=["만원", ",000원", "견적 금액"],
        ),
        Fact(
            fact_id="no_schedule",
            kind="decision",
            value="*",
            surface_forms=["미팅을 잡", "회의를 잡", "회의 일정", "미팅 일정", "마감은"],
        ),
    ]


def _qkeys(b: Builder, s: Scn) -> set[str]:
    return b.used.setdefault(f"{s.user_key}:question_keys", set())


def take_question_key(b: Builder, s: Scn, key: str) -> bool:
    """사용자별 질문 키를 한 번만 쓴다. 이미 썼으면 False, 아니면 등록하고 True."""
    used = _qkeys(b, s)
    if key in used:
        return False
    used.add(key)
    return True


def pick_project_item(
    b: Builder, s: Scn, kind: str, *, project: str | None = None
) -> tuple[str, str]:
    """(프로젝트, 품목) 조합 중 이 사용자에게서 `kind` 질문에 아직 쓰지 않은 것을 골라 등록한다.

    마감일 질문은 deadline 시나리오와 mail_fact(kind=deadline)가 같은 키 공간(`deadline`)을 쓴다.
    """
    projects = [p.name for p in claims_of(b, s).projects if project in (None, p.name)]
    combos = [(p, i) for p in projects for i in T.ITEMS]
    s.rng.shuffle(combos)
    for p, i in combos:
        if take_question_key(b, s, question_key(p, i, kind)):
            return p, i
    raise WorldBuildError(f"{s.user_key}: {kind} 질문에 쓸 (프로젝트, 품목) 조합이 없다")


def question_key(project: str, item: str, kind: str) -> str:
    return f"{project}|{item}|{kind}"


def retry[V](rng: random.Random, make: Callable[[random.Random], V], ok: Callable[[V], bool]) -> V:
    for _ in range(200):
        v = make(rng)
        if ok(v):
            return v
    raise WorldBuildError("조건을 만족하는 값을 찾지 못했다")


def compatible(a: list[Fact], b: list[Fact]) -> bool:
    return not any(conflicts(x, y) for x in a for y in b)


def day_slots(
    rng: random.Random, b: Builder, acct: str, day: date, n: int
) -> list[tuple[datetime, datetime]]:
    """하루 09~18시 안의 겹치지 않는 시간대 n개(시작 순). 못 만들면 줄여서 돌려준다."""
    for _ in range(500):
        out: list[tuple[datetime, datetime]] = []
        cur = 9 * 60 + 30 * rng.randrange(3)
        for _i in range(n):
            start = cur + 30 * rng.randrange(0, 3)
            end = start + rng.choice([30, 60, 60, 90])
            if end > 18 * 60:
                break
            out.append(
                (
                    b.local(acct, day, start // 60, start % 60),
                    b.local(acct, day, end // 60, end % 60),
                )
            )
            cur = end + 30 * rng.randrange(0, 2)
        if len(out) == n:
            return out
    raise WorldBuildError("하루에 시간대를 배치하지 못했다")


def future_weekday(b: Builder, s: Scn, lo: int, hi: int) -> date:
    """스냅샷 +lo~+hi일(최대 +27일) 중 예약되지 않은 평일."""
    return b.pick_day(s.rng, s.account_id, lo, min(hi, MAX_FUTURE_DAYS))


def ev(ref: ArtifactRef) -> ArtifactRef:
    return ref


def shift_local(moment: datetime, days: int) -> datetime:
    return moment + timedelta(days=days)
