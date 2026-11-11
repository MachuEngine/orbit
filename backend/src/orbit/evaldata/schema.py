"""평가 데이터 형식. 평가 러너와 공유하는 계약이며 이 모듈이 기준이다 (ADR-0009 2절).

- `AnswerFact.any_of` 중 하나가 (공백을 무시하고) 응답에 포함돼야 한다.
- `Gold.must_not_contain`의 표현이 응답에 있으면 안 된다.
- `expected_changes`: `None`은 상태 변화를 검사하지 않고, `[]`는 상태 변화가 없어야 한다.
- `required_tool_calls`: 궤적에 최소 한 번 있어야 하는 호출(`args_subset`은 인자의 부분집합).
- `forbidden_tools`: 하나라도 호출하면 실패한다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SplitRatio(_Model):
    dev: float
    test: float


class Counts(_Model):
    users: int
    messages: int
    events: int
    tasks: int


class Manifest(_Model):
    name: str
    version: str
    generator_git_sha: str
    seed: int
    created_with: Literal["templates"]
    split_ratio: SplitRatio
    counts: Counts
    notes: str = ""


class Turn(_Model):
    role: Literal["user"]
    content: str


class AnswerFact(_Model):
    fact_id: str
    any_of: list[str] = Field(min_length=1)
    description: str


class RequiredCall(_Model):
    tool: str
    args_subset: dict[str, Any] = Field(default_factory=dict)


class Gold(_Model):
    answer_facts: list[AnswerFact] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    expected_changes: list[dict[str, Any]] | None = None
    required_tool_calls: list[RequiredCall] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)


class Task(_Model):
    task_id: str
    scenario_id: str
    split: Literal["dev", "test"]
    as_of_time: AwareDatetime
    account_id: str
    turns: list[Turn] = Field(min_length=1)
    gold: Gold
    difficulty: dict[str, int | float | str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


ReviewDecision = Literal["approve", "fix", "exclude"]


class ReviewEntry(_Model):
    task_id: str
    decision: ReviewDecision
    note: str = ""
