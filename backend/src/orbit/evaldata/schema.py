"""평가 데이터 형식. 평가 러너와 공유하는 계약이며 이 모듈이 기준이다 (ADR-0009 2절).

- `Gold.expected_answer`(ADR-0010 1절)가 있으면 성공 판정은 구조화된 답과의 비교로 한다. 일정은
  `{calendar_id, event_id}`로 가리키고 시각은 오프셋이 있는 ISO 8601 순간으로 비교한다.
- `AnswerFact.any_of`는 보조 지표다(`expected_answer`가 없는 과제에서만 판정에 쓴다). 응답에
  (공백을 무시하고) 하나라도 포함되면 충족이다.
- `Gold.must_not_contain`의 표현이 응답에 있으면 안 된다. v0 데이터는 비워 둔다.
- `expected_changes`: `None`은 상태 변화를 검사하지 않고, `[]`는 상태 변화가 없어야 한다.
- `required_tool_calls`: 궤적에 최소 한 번 있어야 하는 호출(`args_subset`은 인자의 부분집합).
- `forbidden_tools`: 하나라도 호출하면 실패한다.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

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


class EventRef(_Model):
    calendar_id: str
    event_id: str


class EventSetAnswer(_Model):
    """순서 무관 집합이 같아야 한다."""

    kind: Literal["event_set"] = "event_set"
    events: list[EventRef]


class EventAnswer(_Model):
    kind: Literal["event"] = "event"
    event: EventRef


class EventTimeAnswer(_Model):
    """같은 일정이고 시작 시각이 같은 순간이어야 한다."""

    kind: Literal["event_time"] = "event_time"
    event: EventRef
    start: AwareDatetime


class TimeSlotAnswer(_Model):
    kind: Literal["time_slot"] = "time_slot"
    start: AwareDatetime


ExpectedAnswer = Annotated[
    EventSetAnswer | EventAnswer | EventTimeAnswer | TimeSlotAnswer,
    Field(discriminator="kind"),
]


class Gold(_Model):
    answer_facts: list[AnswerFact] = Field(default_factory=list)
    expected_answer: ExpectedAnswer | None = None
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
