"""세계 모델 v1(TOML) 로드와 검증. 사용자·인물·프로젝트·반복 일정·노이즈 발신자·목표 수량.

v0 `orbit.evaldata.worldmodel`과 따로 둔다(v0 동작을 바꾸지 않기 위해). 표준 라이브러리
`tomllib`만 쓴다.
"""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError, model_validator

from orbit.evaldata.v1.skeleton import SCENARIO_TYPES, SCENARIO_VARIANTS

FAMILIES = ("openai", "anthropic", "google")
ALLOWED_DOMAINS = ("example.com", "example.org", "example.net")


class WorldModelError(ValueError):
    """세계 모델 파일이 올바르지 않다."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Meta(_Model):
    world_name: str
    world_version: str


class Window(_Model):
    snapshot: AwareDatetime  # 메일함·캘린더 스냅샷 시각. 모든 메일·일정 갱신은 이 시각 이전이다
    mail_weeks: int  # 메일: 스냅샷 이전 몇 주
    event_weeks_before: int
    event_weeks_after: int
    asof_start: date  # 과제 as_of 날짜 범위(사용자 현지 평일)
    asof_end: date


class Generation(_Model):
    families: list[Literal["openai", "anthropic", "google"]]


class CalendarSpec(_Model):
    id: str
    name: str
    primary: bool = False


class User(_Model):
    key: str
    account_id: str
    name: str
    email: str
    role: str
    company: str
    time_zone: str
    purpose: Literal["e2e", "search"]
    calendars: list[CalendarSpec]


class Person(_Model):
    key: str
    name: str
    email: str
    company: str
    role: str
    relation: Literal["colleague", "customer", "vendor"]
    works_with: list[str]


class Project(_Model):
    key: str
    name: str
    user: str
    members: list[str]
    topics: list[str]


class Recurring(_Model):
    user: str
    title: str
    weekday: int  # 0=월
    start: str  # 사용자 현지 "HH:MM"
    duration_min: int
    every_weeks: int
    attendee_count: int
    location: str


class NoiseSender(_Model):
    name: str
    email: str
    kind: Literal["newsletter", "notice", "spam"]


class LengthMix(_Model):
    short: float
    medium: float
    long: float


class Targets(_Model):
    mails_per_e2e_user: int
    mails_tolerance: int
    docs_per_e2e_user: tuple[int, int]
    search_docs: int
    search_sizes: list[int]
    background_events_per_user: int
    background_all_day_per_user: int
    length_mix: LengthMix
    scenarios: dict[str, int]
    variants: dict[str, int] = {}


class WorldModel(_Model):
    meta: Meta
    window: Window
    generation: Generation
    targets: Targets
    users: list[User]
    people: list[Person]
    projects: list[Project]
    recurring: list[Recurring]
    noise_senders: list[NoiseSender]

    @model_validator(mode="after")
    def _check(self) -> WorldModel:
        self._check_users()
        self._check_people()
        self._check_targets()
        return self

    def _check_users(self) -> None:
        keys = [u.key for u in self.users]
        accts = [u.account_id for u in self.users]
        if len(set(keys)) != len(keys) or len(set(accts)) != len(accts):
            raise ValueError("users의 key·account_id는 중복될 수 없다")
        for u in self.users:
            try:
                ZoneInfo(u.time_zone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"사용자 {u.key}의 시간대를 모른다: {u.time_zone}") from exc
            if not u.calendars or sum(c.primary for c in u.calendars) != 1:
                raise ValueError(f"사용자 {u.key}의 캘린더는 기본 캘린더 하나를 포함해야 한다")
            if u.purpose == "e2e" and not 2 <= len(u.calendars) <= 3:
                raise ValueError(f"종단 사용자 {u.key}의 캘린더는 2~3개여야 한다")
        e2e, search = self.e2e_users(), self.search_users()
        if not e2e:
            raise ValueError("종단 사용자가 한 명 이상 있어야 한다")
        if len(search) != len(self.targets.search_sizes):
            raise ValueError("검색 사용자 수와 search_sizes 길이가 같아야 한다")
        if self.targets.search_sizes != sorted(self.targets.search_sizes):
            raise ValueError("search_sizes는 오름차순이어야 한다")
        if len({(u.name, u.role, u.company) for u in search}) > 1:
            raise ValueError(
                "검색 사용자는 같은 인물 설정(이름·역할·회사)을 공유해야 한다(본문 공유)"
            )
        if not self.generation.families or len(set(self.generation.families)) != len(
            self.generation.families
        ):
            raise ValueError("generation.families는 중복 없는 비어 있지 않은 목록이어야 한다")

    def _check_people(self) -> None:
        user_keys = {u.key for u in self.users}
        person_keys = [p.key for p in self.people]
        if len(set(person_keys)) != len(person_keys):
            raise ValueError("people의 key는 중복될 수 없다")
        emails = [u.email for u in self.users] + [p.email for p in self.people]
        emails += [n.email for n in self.noise_senders]
        if len(set(emails)) != len(emails):
            raise ValueError("이메일은 중복될 수 없다 (동명이인도 주소는 다르다)")
        for e in emails:
            if not _allowed_address(e):
                raise ValueError(f"허용하지 않는 주소 도메인이다: {e}")
        for p in self.people:
            if not p.works_with or not set(p.works_with) <= user_keys:
                raise ValueError(f"인물 {p.key}의 works_with가 올바르지 않다")
        pset = set(person_keys)
        for proj in self.projects:
            if proj.user not in user_keys or not set(proj.members) <= pset:
                raise ValueError(f"프로젝트 {proj.key}의 user·members 참조가 올바르지 않다")
        for r in self.recurring:
            if r.user not in user_keys or not 0 <= r.weekday <= 4 or r.every_weeks < 1:
                raise ValueError("recurring의 user·weekday·every_weeks가 올바르지 않다")
        if len(self.homonym_pairs()) < 2:
            raise ValueError("동명이인이 두 쌍 이상 있어야 한다")
        for kind in ("newsletter", "notice", "spam"):
            if not any(n.kind == kind for n in self.noise_senders):
                raise ValueError(f"노이즈 발신자에 {kind}가 없다")

    def _check_targets(self) -> None:
        t = self.targets
        mix = t.length_mix
        if abs(mix.short + mix.medium + mix.long - 1.0) > 1e-9:
            raise ValueError("length_mix의 합은 1이어야 한다")
        unknown = set(t.scenarios) - set(SCENARIO_TYPES)
        if unknown:
            raise ValueError(f"알 수 없는 시나리오 유형: {sorted(unknown)}")
        for key, n in t.variants.items():
            base, _, variant = key.partition(".")
            if variant not in SCENARIO_VARIANTS.get(base, ()):
                raise ValueError(f"알 수 없는 갈래: {key}")
            if n < 0:
                raise ValueError(f"갈래 수가 음수다: {key}")
        for base in SCENARIO_VARIANTS:
            keys = [k for k in t.variants if k.startswith(base + ".")]
            if keys and sum(t.variants[k] for k in keys) != t.scenarios.get(base, 0):
                raise ValueError(f"{base}의 갈래 수 합이 유형 수와 다르다")
        if t.docs_per_e2e_user[0] > t.docs_per_e2e_user[1]:
            raise ValueError("docs_per_e2e_user는 [최소, 최대]여야 한다")

    # ---- 조회

    def e2e_users(self) -> list[User]:
        return [u for u in self.users if u.purpose == "e2e"]

    def search_users(self) -> list[User]:
        return [u for u in self.users if u.purpose == "search"]

    def person(self, key: str) -> Person:
        for p in self.people:
            if p.key == key:
                return p
        raise KeyError(key)

    def user(self, key: str) -> User:
        for u in self.users:
            if u.key == key:
                return u
        raise KeyError(key)

    def contacts_of(self, user_key: str) -> list[Person]:
        return [p for p in self.people if user_key in p.works_with]

    def homonym_pairs(self) -> list[tuple[Person, Person]]:
        by_name: dict[str, list[Person]] = {}
        for p in self.people:
            by_name.setdefault(p.name, []).append(p)
        return [(v[0], v[1]) for v in by_name.values() if len(v) == 2]

    def variant_counts(self, base: str) -> dict[str, int]:
        """유형의 갈래별 수. 갈래 표가 없으면 비어 있다."""
        prefix = base + "."
        return {
            k[len(prefix) :]: n for k, n in self.targets.variants.items() if k.startswith(prefix)
        }


def _allowed_address(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return (
        domain in ALLOWED_DOMAINS
        or any(domain.endswith("." + d) for d in ALLOWED_DOMAINS)
        or domain.endswith(".test")
    )


def parse_world_model(data: dict[str, Any]) -> WorldModel:
    try:
        return WorldModel.model_validate(data)
    except ValidationError as exc:
        raise WorldModelError(f"세계 모델 검증 실패: {exc}") from exc


def load_world_model(path: str | Path) -> WorldModel:
    try:
        raw = Path(path).read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise WorldModelError(f"세계 모델을 읽을 수 없다: {exc}") from exc
    return parse_world_model(data)
