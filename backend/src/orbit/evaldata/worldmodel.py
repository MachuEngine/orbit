"""세계 모델(TOML) 로드와 검증. 가상 사용자·인물·프로젝트·반복 일정·노이즈 발신자·목표 수량.

ADR-0009는 YAML이라 했지만 새 의존성을 피하려고 표준 라이브러리 `tomllib`를 쓴다(메인 결정).
"""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError, model_validator


class WorldModelError(ValueError):
    """세계 모델 파일이 올바르지 않다."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Meta(_Model):
    dataset_name: str
    dataset_version: str
    dev_ratio: float
    test_ratio: float


class Window(_Model):
    mail_start: date
    snapshot: AwareDatetime  # 메일함 스냅샷 시각. 모든 메일·일정 갱신은 이 시각 이전이다
    event_start: date
    event_end: date
    asof_start: date  # 과제 as_of_time이 놓이는 날짜 범위
    asof_end: date


class User(_Model):
    key: str
    account_id: str
    name: str
    email: str
    role: str
    calendar_id: str
    calendar_name: str


class Person(_Model):
    key: str
    name: str
    email: str
    company: str
    role: str
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
    start: str  # "HH:MM" KST
    duration_min: int
    every_weeks: int
    attendees: list[str]
    location: str


class NoiseSender(_Model):
    name: str
    email: str
    kind: Literal["newsletter", "spam", "notice"]


class Targets(_Model):
    day_schedule: int
    free_slot: int
    reschedule: int
    cancelled_exclusion: int
    project_week: int
    person_next: int
    newsletters_per_user: int
    spam_per_user: int
    notices_per_user: int
    deadlines: int
    followups: int


class WorldModel(_Model):
    meta: Meta
    window: Window
    users: list[User]
    people: list[Person]
    projects: list[Project]
    recurring: list[Recurring]
    noise_senders: list[NoiseSender]
    targets: Targets

    @model_validator(mode="after")
    def _check_refs(self) -> WorldModel:
        user_keys = [u.key for u in self.users]
        person_keys = [p.key for p in self.people]
        if len(set(user_keys)) != len(user_keys) or len(set(person_keys)) != len(person_keys):
            raise ValueError("users·people의 key는 중복될 수 없다")
        emails = [u.email for u in self.users] + [p.email for p in self.people]
        if len(set(emails)) != len(emails):
            raise ValueError("이메일은 중복될 수 없다 (동명이인도 주소는 다르다)")
        for p in self.people:
            if not set(p.works_with) <= set(user_keys):
                raise ValueError(f"인물 {p.key}의 works_with에 없는 사용자가 있다")
        pset = set(person_keys)
        for proj in self.projects:
            if proj.user not in user_keys:
                raise ValueError(f"프로젝트 {proj.key}의 user가 없다")
            if not set(proj.members) <= pset:
                raise ValueError(f"프로젝트 {proj.key}의 members에 없는 인물이 있다")
            for m in proj.members:
                if proj.user not in self.person(m).works_with:
                    raise ValueError(
                        f"프로젝트 {proj.key}의 멤버 {m}은 {proj.user}와 일하지 않는다"
                    )
        for r in self.recurring:
            if r.user not in user_keys or not set(r.attendees) <= pset:
                raise ValueError("recurring의 user·attendees 참조가 올바르지 않다")
            if not 0 <= r.weekday <= 4 or r.every_weeks < 1:
                raise ValueError("recurring의 weekday는 0~4, every_weeks는 1 이상이어야 한다")
        if abs(self.meta.dev_ratio + self.meta.test_ratio - 1.0) > 1e-9:
            raise ValueError("dev_ratio + test_ratio는 1이어야 한다")
        if not any(self.is_homonym(p) for p in self.people):
            raise ValueError("동명이인이 한 쌍 이상 있어야 한다")
        return self

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

    def is_homonym(self, person: Person) -> bool:
        return sum(1 for q in self.people if q.name == person.name) > 1


def parse_world_model(data: dict[str, Any]) -> WorldModel:
    try:
        return WorldModel.model_validate(data)
    except ValidationError as exc:
        raise WorldModelError(f"세계 모델 검증 실패: {exc}") from exc


def load_world_model(path: str | Path) -> WorldModel:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorldModelError(f"세계 모델을 읽을 수 없다: {exc}") from exc
    return parse_world_model(data)
