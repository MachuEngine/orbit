"""평가 데이터 형식. 평가 러너와 공유하는 계약이며 이 모듈이 기준이다 (ADR-0009 2절).

- `Gold.expected_answer`(ADR-0010 1절)가 있으면 성공 판정은 구조화된 답과의 비교로 한다. 일정은
  `{calendar_id, event_id}`로 가리키고 시각은 오프셋이 있는 ISO 8601 순간으로 비교한다.
- `AnswerFact.any_of`는 보조 지표다(`expected_answer`가 없는 과제에서만 판정에 쓴다). 응답에
  (공백을 무시하고) 하나라도 포함되면 충족이다.
- `Gold.must_not_contain`의 표현이 응답에 있으면 안 된다. v0 데이터는 비워 둔다.
- `expected_changes`: `None`은 상태 변화를 검사하지 않고, `[]`는 상태 변화가 없어야 한다. 비어 있지
  않으면 타입 있는 기대 변화 목록이다(`event_created` 등, ADR-0026 4절). 실제 변화와 일대일로
  맞아야 하고 남는 실제 변화가 있으면 안 된다(`orbit.evalrunner.state_changes`).
- `answer_facts_required`: True이고 `expected_answer`가 있으면 `answer_facts`도 모두 충족해야 한다.
- `required_tool_calls`: 궤적에 최소 한 번 있어야 하는 호출(`args_subset`은 인자의 부분집합).
- `forbidden_tools`: 하나라도 호출하면 실패한다.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

DEFAULT_TIME_ZONE = "Asia/Seoul"  # `Task.time_zone`이 없을 때 쓴다(v0 호환)


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
    """데이터셋 매니페스트. `smoke_task_ids`·`details`는 v1 선택 필드이며 없으면 파일에 쓰지 않는다.

    `created_with`: `templates`는 템플릿 문장만 쓴 v0, `structure+llm`은 구조는 코드가 만들고 본문은
    LLM이 쓴 세계 v1(ADR-0026 §2)이다.
    `smoke_task_ids`: 스모크 부분집합(dev 과제, ADR-0026 §5).
    `details`: 생성 요약(수량·분할·비용 등).
    """

    name: str
    version: str
    generator_git_sha: str
    seed: int
    created_with: Literal["templates", "structure+llm"]
    split_ratio: SplitRatio
    counts: Counts
    notes: str = ""
    smoke_task_ids: list[str] | None = None
    details: dict[str, Any] | None = None


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


class MessageRef(_Model):
    message_id: str


class MessageSetAnswer(_Model):
    """메일 집합. 순서 무관 집합이 같아야 한다 (ADR-0026 4절)."""

    kind: Literal["message_set"] = "message_set"
    messages: list[MessageRef]


class DocumentRef(_Model):
    file_id: str


class DocumentAnswer(_Model):
    kind: Literal["document"] = "document"
    document: DocumentRef


class AbstainAnswer(_Model):
    """도구로 확인했지만 답할 정보가 없음을 알린다."""

    kind: Literal["abstain"] = "abstain"


ReadingAnswer = Annotated[
    EventSetAnswer
    | EventAnswer
    | EventTimeAnswer
    | TimeSlotAnswer
    | MessageSetAnswer
    | DocumentAnswer,
    Field(discriminator="kind"),
]


def answer_signature(answer: Any) -> tuple[str, Any]:
    """정답의 비교용 값. 종류와 순서 없는 값으로 만든 해시 가능한 값의 쌍이다.

    정답 검증기(`ClarifyAnswer`)와 채점(`orbit.evalrunner.structured`)이 같은 규칙을 쓴다.
    """
    if isinstance(answer, EventSetAnswer):
        return answer.kind, frozenset((e.calendar_id, e.event_id) for e in answer.events)
    if isinstance(answer, EventAnswer):
        return answer.kind, (answer.event.calendar_id, answer.event.event_id)
    if isinstance(answer, EventTimeAnswer):
        return answer.kind, ((answer.event.calendar_id, answer.event.event_id), answer.start)
    if isinstance(answer, TimeSlotAnswer):
        return answer.kind, answer.start
    if isinstance(answer, MessageSetAnswer):
        return answer.kind, frozenset(m.message_id for m in answer.messages)
    if isinstance(answer, DocumentAnswer):
        return answer.kind, answer.document.file_id
    return answer.kind, None


class ClarifyAnswer(_Model):
    """모호한 질문 문항의 정답: 되묻는 것이 성공이다 (ADR-0026 4절).

    `readings`는 해석별 정답이고 서로 달라야 한다(같으면 모호성이 결과에 영향이 없다).
    """

    kind: Literal["clarify"] = "clarify"
    readings: list[ReadingAnswer] = Field(min_length=2)

    @model_validator(mode="after")
    def _readings_differ(self) -> ClarifyAnswer:
        signatures = [answer_signature(r) for r in self.readings]
        if len(set(signatures)) != len(signatures):
            raise ValueError("clarify readings must be distinct answers")
        return self


ExpectedAnswer = Annotated[
    EventSetAnswer
    | EventAnswer
    | EventTimeAnswer
    | TimeSlotAnswer
    | MessageSetAnswer
    | DocumentAnswer
    | AbstainAnswer
    | ClarifyAnswer,
    Field(discriminator="kind"),
]


class EventCreatedChange(_Model):
    """일정 하나가 만들어져야 한다. 시각은 순간으로 비교한다."""

    kind: Literal["event_created"] = "event_created"
    calendar_id: str
    start: AwareDatetime
    end: AwareDatetime
    all_day: bool = False
    title_any_of: list[str] = Field(min_length=1)
    attendees_include: list[str] = Field(default_factory=list)
    location_any_of: list[str] | None = None


class EventFieldChanges(_Model):
    """`event_updated`에서 바뀌어야 하는 필드. 하나 이상 지정한다."""

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    location: str | None = None  # 바뀐 위치에 이 문자열이 들어 있어야 한다
    title_any_of: list[str] | None = Field(default=None, min_length=1)
    attendees_include: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _not_empty(self) -> EventFieldChanges:
        if all(getattr(self, f) is None for f in type(self).model_fields):
            raise ValueError("fields must name at least one changed field")
        return self


class EventUpdatedChange(_Model):
    kind: Literal["event_updated"] = "event_updated"
    calendar_id: str
    event_id: str
    fields: EventFieldChanges


class EventCancelledChange(_Model):
    kind: Literal["event_cancelled"] = "event_cancelled"
    calendar_id: str
    event_id: str


class DraftChange(_Model):
    """메일 초안 기대. `draft_created`(발송 안 함)와 `draft_sent`(발송)가 같은 필드를 쓴다."""

    to_include: list[str] = Field(min_length=1)
    reply_to_message_id: str | None = None
    subject_any_of: list[str] | None = Field(default=None, min_length=1)
    body_facts: list[AnswerFact] = Field(default_factory=list)
    body_must_not_contain: list[str] = Field(default_factory=list)


class DraftCreatedChange(DraftChange):
    kind: Literal["draft_created"] = "draft_created"


class DraftSentChange(DraftChange):
    kind: Literal["draft_sent"] = "draft_sent"


class ReminderCreatedChange(_Model):
    kind: Literal["reminder_created"] = "reminder_created"
    due_at: AwareDatetime
    tolerance_minutes: int = Field(default=0, ge=0)
    text_any_of: list[str] | None = Field(default=None, min_length=1)


ExpectedChange = Annotated[
    EventCreatedChange
    | EventUpdatedChange
    | EventCancelledChange
    | DraftCreatedChange
    | DraftSentChange
    | ReminderCreatedChange,
    Field(discriminator="kind"),
]


class Gold(_Model):
    answer_facts: list[AnswerFact] = Field(default_factory=list)
    expected_answer: ExpectedAnswer | None = None
    must_not_contain: list[str] = Field(default_factory=list)
    expected_changes: list[ExpectedChange] | None = None
    required_tool_calls: list[RequiredCall] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    answer_facts_required: bool = False


class Task(_Model):
    task_id: str
    scenario_id: str
    cluster_id: str  # 근거를 공유하는 과제 묶음. 같은 묶음은 같은 split이다
    split: Literal["dev", "test"]
    as_of_time: AwareDatetime
    account_id: str
    turns: list[Turn] = Field(min_length=1)
    gold: Gold
    difficulty: dict[str, int | float | str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    # 시뮬레이션 사용자의 승인 결정(ADR-0023 §3). n번째 쓰기 승인 요청에 대한 결정이고 목록 밖은
    # 승인이다. 없으면(v0) 모두 승인한다. 정답이 아니라 사용자 행동 입력이라 평가 대상에게 보인다.
    approval_policy: list[Literal["approve", "reject"]] | None = None
    # 사용자 시간대(IANA 이름). 없으면 `DEFAULT_TIME_ZONE`. 평가 대상의 시각 해석 기준이다.
    time_zone: str | None = None
    # 이 과제에만 거는 장애 주입(`FaultPlan.from_dict` 형식, ADR-0026 7절). 평가 대상에게 안 보인다.
    fault_plan: dict[str, Any] | None = None


ReviewDecision = Literal["approve", "fix", "exclude"]


class ReviewEntry(_Model):
    task_id: str
    decision: ReviewDecision
    note: str = ""
