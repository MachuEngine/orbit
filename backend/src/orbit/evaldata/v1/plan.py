"""시나리오 계획: 어떤 유형을 어느 사용자에게 몇 개 만들지, 인물·프로젝트를 어떻게 배정할지."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from orbit.evaldata.v1.core import Builder, Claims, WorldBuildError
from orbit.evaldata.v1.worldmodel import WorldModel

# 만드는 순서. 날짜를 예약하는 유형을 먼저 만들어 다른 유형이 그 날을 피하게 한다.
BUILD_ORDER = (
    "day_agenda",
    "free_slot",
    "cross_midnight",
    "all_day",
    "project_week",
    "person_next",
    "ambiguous",
    "absent",
    "tz_event",
    "reschedule",
    "cancellation",
    "mismatch",
    "meeting_request",
    "event_change_target",
    "mail_fact",
    "reply_needed",
    "doc_fact",
    "doc_version",
    "deadline",
    "search_topic",
)


@dataclass(frozen=True)
class PlanItem:
    type: str
    variant: str | None
    user_key: str


def plan_scenarios(wm: WorldModel) -> list[PlanItem]:
    e2e = wm.e2e_users()
    search = wm.search_users()
    tzs = Counter(u.time_zone for u in e2e)
    majority = max(tzs, key=lambda k: (tzs[k], -list(tzs).index(k)))
    items: list[PlanItem] = []
    rot = 0
    for type_ in BUILD_ORDER:
        total = wm.targets.scenarios.get(type_, 0)
        if total <= 0:
            continue
        if type_ == "search_topic":
            if not search:
                raise WorldBuildError("search_topic에는 검색 사용자가 필요하다")
            items += [PlanItem(type_, None, search[-1].key) for _ in range(total)]
            continue
        if type_ == "tz_event":
            odd = [u for u in e2e if u.time_zone != majority]
            same = [u for u in e2e if u.time_zone == majority]
            n_local = total // 2 if odd else 0
            for i in range(n_local):
                items.append(PlanItem(type_, "local", odd[i % len(odd)].key))
            for i in range(total - n_local):
                items.append(PlanItem(type_, "foreign_attendee", same[i % len(same)].key))
            continue
        by_name = wm.variant_counts(type_)
        variants: dict[str | None, int] = (
            {k: v for k, v in by_name.items()} if by_name else {None: total}
        )
        for variant, n in variants.items():
            for i in range(n):
                items.append(PlanItem(type_, variant, e2e[(rot + i) % len(e2e)].key))
            rot += n
    return items


def allocate_claims(b: Builder, items: list[PlanItem]) -> None:
    """사용자별로 인물 시나리오가 쓸 인물과 프로젝트를 정한다(결정적)."""
    wm = b.wm
    need: dict[str, Counter[str]] = {u.key: Counter() for u in wm.e2e_users()}
    for it in items:
        if it.user_key in need:
            need[it.user_key][it.type + (f".{it.variant}" if it.variant else "")] += 1
    pair_names = {p.name for pair in wm.homonym_pairs() for p in pair}
    for u in wm.e2e_users():
        c = Claims()
        n = need[u.key]
        contacts = wm.contacts_of(u.key)
        singles = sorted(p.key for p in contacts if p.name not in pair_names)
        b.rng(f"claims:{u.key}").shuffle(singles)
        n_next, n_meet, n_abs = (
            n["person_next"],
            n["ambiguous.person_meetings"],
            n["absent.person_no_meeting"],
        )
        if n_next + n_meet + n_abs > len(singles) - 1:
            raise WorldBuildError(
                f"{u.key}: 인물 시나리오({n_next + n_meet + n_abs})에 비해 "
                f"연락처({len(singles)})가 모자란다"
            )
        c.next_people = singles[:n_next]
        c.meeting_people = singles[n_next : n_next + n_meet]
        c.absent_people = singles[n_next + n_meet : n_next + n_meet + n_abs]
        c.free_people = singles[n_next + n_meet + n_abs :]
        pairs = [
            (a.key, z.key)
            for a, z in wm.homonym_pairs()
            if u.key in a.works_with and u.key in z.works_with
        ]
        pairs.sort()
        if n["ambiguous.homonym_person"] > len(pairs):
            raise WorldBuildError(f"{u.key}: 동명이인 시나리오 수가 동명이인 쌍보다 많다")
        c.homonym_pairs = pairs
        projects = [p for p in wm.projects if p.user == u.key]
        quiet = n["absent.no_amount"] + n["absent.project_no_meeting"] > 0
        if quiet:
            if len(projects) < 2:
                raise WorldBuildError(f"{u.key}: absent 시나리오에는 프로젝트가 2개 이상 필요하다")
            c.quiet_project = projects[0].name
            projects = projects[1:]
        c.projects = projects
        need_proj = max(n["project_week"], n["ambiguous.same_project_notes"])
        if need_proj > len(projects):
            raise WorldBuildError(f"{u.key}: 프로젝트가 모자란다({need_proj}>{len(projects)})")
        b.claims[u.key] = c
    for u in wm.search_users():
        b.claims[u.key] = Claims()
