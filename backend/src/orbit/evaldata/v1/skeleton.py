"""뼈대 산출물(`skeleton.json`) 스키마. (c) 본문 실현, (d) 종단 과제 생성기, (e) 검색 세트 생성기가
공유하는 계약이다. 필드를 바꾸면 `SCHEMA_VERSION`을 올린다.

구성
- `accounts[account_id]`: 계정(사용자) 하나의 캘린더·일정·메일·문서. 메일·문서에는 본문 대신
  `body_id`가 있다. 본문은 `bodies[body_id]`(BodySpec)를 보고 (c)가 쓴다.
- `people`: 모든 인물(연락처). `notes`: 생성 기록과 알려진 한계.
- `scenarios`: 시나리오 기록. (d)·(e)는 이 기록의 `truth`와 근거 참조에서 정답을 계산한다.
- `search_mailboxes`: 검색 사용자 세 명의 메일함 크기와 body_id별 메시지·파일 ID 대응.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from orbit.connectors.contracts import Address, AttachmentMeta, Calendar, CalendarEvent

SCHEMA_VERSION = "orbit-world-v1-skeleton/1"

ScenarioType = Literal[
    "day_agenda",
    "free_slot",
    "reschedule",
    "cancellation",
    "project_week",
    "person_next",
    "cross_midnight",
    "tz_event",
    "all_day",
    "mail_fact",
    "reply_needed",
    "meeting_request",
    "event_change_target",
    "doc_fact",
    "doc_version",
    "deadline",
    "mismatch",
    "absent",
    "ambiguous",
    "search_topic",
]
SCENARIO_TYPES: tuple[str, ...] = (
    "day_agenda",
    "free_slot",
    "reschedule",
    "cancellation",
    "project_week",
    "person_next",
    "cross_midnight",
    "tz_event",
    "all_day",
    "mail_fact",
    "reply_needed",
    "meeting_request",
    "event_change_target",
    "doc_fact",
    "doc_version",
    "deadline",
    "mismatch",
    "absent",
    "ambiguous",
    "search_topic",
)
# 갈래가 있는 유형. 세계 모델의 targets.variants 키는 "<유형>.<갈래>"다.
SCENARIO_VARIANTS: dict[str, tuple[str, ...]] = {
    "absent": ("person_no_meeting", "no_document", "no_amount", "project_no_meeting"),
    "ambiguous": ("person_meetings", "same_project_notes", "homonym_person"),
}

Family = Literal["openai", "anthropic", "google"]
LengthClass = Literal["short", "medium", "long"]
MailGenre = Literal["colleague", "customer", "vendor", "notice", "newsletter", "spam"]
DocGenre = Literal["meeting_notes", "quote", "plan", "report", "memo"]
MAIL_GENRES: tuple[str, ...] = ("colleague", "customer", "vendor", "notice", "newsletter", "spam")
DOC_GENRES: tuple[str, ...] = ("meeting_notes", "quote", "plan", "report", "memo")
# 길이 등급별 본문 글자 수 범위 [최소, 최대]
TARGET_CHARS: dict[str, tuple[int, int]] = {
    "short": (60, 200),
    "medium": (200, 700),
    "long": (700, 2000),
}
FactKind = Literal[
    "date",
    "time",
    "datetime",
    "person",
    "place",
    "amount",
    "decision",
    "deadline",
    "document_title",
    "item",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- 사실과 본문 명세


class Fact(_Model):
    """본문에 반드시 들어가야 하거나(required) 들어가면 안 되는(forbidden) 사실 하나.

    `value`는 정규값이다(시각은 ISO 8601, 금액은 원 단위 정수 문자열이 아닌 정수).
    `surface_forms` 중 하나가 본문에 들어 있으면(공백·대소문자 무시, NFC) 충족이다.
    금지 사실은 표기 중 어느 하나라도 들어 있으면 위반이다.
    """

    fact_id: str
    kind: FactKind
    value: str | int
    surface_forms: list[str] = Field(min_length=1)


class ContextPerson(_Model):
    """본문 작성자에게 알려 주는 사람 정보. 주소는 담지 않는다."""

    name: str
    relation: str  # 예: "사용자 본인", "동료", "고객사 담당자", "협력사 담당자", "자동 발송"
    company: str = ""


class BodyContext(_Model):
    """본문을 쓸 때 참고할 맥락.

    - `subject`: 메일 제목(문서는 None).
    - `sender`·`recipients`: 메일의 보낸 사람과 받는 사람.
    - `thread_position`·`thread_size`: 스레드 안 순번(1부터)과 길이. `prior_body_ids`는 앞선 메일의
      body_id(오래된 순). 본문 실현은 앞선 본문을 먼저 쓴 뒤 뒤 본문을 쓴다.
    - `project`·`topic`: 관련 프로젝트 이름과 주제.
    - `intent`: 이 본문이 하는 일(한국어 한 문장). 사실 목록과 함께 본문의 방향을 정한다.
    - `document_title`·`document_date`: 문서 제목과 작성일(메일은 None).
    - `sent_at`: 메일 보낸 시각(UTC). 문서는 수정 시각.
    - `decoy_for`: 검색 세트에서 같은 주제어를 쓰지만 답과 무관한 본문일 때 대상 시나리오 ID.
    """

    subject: str | None = None
    sender: ContextPerson | None = None
    recipients: list[ContextPerson] = []
    thread_position: int = 1
    thread_size: int = 1
    prior_body_ids: list[str] = []
    project: str | None = None
    topic: str | None = None
    intent: str = ""
    document_title: str | None = None
    document_date: date | None = None
    sent_at: AwareDatetime | None = None
    decoy_for: str | None = None


class BodySpec(_Model):
    """본문 명세. (c)가 이 명세를 받아 본문을 쓰고 검증한다.

    - `artifact`: "mail"(메일 본문) 또는 "document"(문서 본문).
    - `genre`: 메일(colleague, customer, vendor, notice, newsletter, spam) 또는 문서(meeting_notes,
      quote, plan, report, memo).
    - `length_class`·`target_chars`: 길이 등급과 글자 수 범위 [최소, 최대].
    - `required_facts`·`forbidden_facts`: 본문에 반드시 넣을 사실과 넣으면 안 되는 사실.
    - `template_text`: 결정적 대체 본문. 필수 사실을 모두 담고 금지 사실은 담지 않는다.
      본문 실현이 상한을 넘게 실패하면 이 텍스트를 쓴다.
    - `generator_family`: 이 본문을 쓸 모델 계열(ADR-0026 §2.5).
    - `forbidden_patterns`: 본문에 있으면 실패인 정규식 목록(realize 검사 `forbidden_pattern`).
      기본은 빈 목록이며, 비어 있으면 직렬화에서 빠져 기존 skeleton·bodies 해시가 바뀌지 않는다.
    """

    artifact: Literal["mail", "document"]
    genre: str
    length_class: LengthClass
    target_chars: tuple[int, int]
    context: BodyContext
    required_facts: list[Fact] = []
    forbidden_facts: list[Fact] = []
    template_text: str
    generator_family: Family
    forbidden_patterns: list[str] = []

    @model_serializer(mode="wrap")
    def _omit_empty_patterns(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data: dict[str, object] = handler(self)
        if not data.get("forbidden_patterns"):
            data.pop("forbidden_patterns", None)
        return data

    @model_validator(mode="after")
    def _check(self) -> BodySpec:
        for pattern in self.forbidden_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"forbidden_patterns가 올바른 정규식이 아니다: {pattern}") from exc
        allowed = MAIL_GENRES if self.artifact == "mail" else DOC_GENRES
        if self.genre not in allowed:
            raise ValueError(f"{self.artifact}의 장르가 올바르지 않다: {self.genre}")
        if self.artifact == "document" and self.length_class == "short":
            raise ValueError("문서는 medium·long만 쓴다")
        if self.target_chars != TARGET_CHARS[self.length_class]:
            raise ValueError("target_chars가 길이 등급과 맞지 않다")
        return self


# ---------------------------------------------------------------- 계정 산출물


class SkMessage(_Model):
    """메일 한 통. `MailMessage`에서 `body_text`·`snippet`을 `body_id`로 바꾼 것이다."""

    id: str
    thread_id: str
    from_: Address = Field(alias="from")
    to: tuple[Address, ...] = ()
    cc: tuple[Address, ...] = ()
    subject: str
    body_id: str
    sent_at: AwareDatetime
    labels: tuple[str, ...] = ()
    attachments: tuple[AttachmentMeta, ...] = ()


class SkFile(_Model):
    """문서 파일 하나. `SimFile`에서 `text`를 `body_id`로 바꾼 것이다."""

    id: str
    name: str
    mime_type: str
    modified_at: AwareDatetime
    owners: tuple[Address, ...] = ()
    body_id: str
    trashed: bool = False


class SkThread(_Model):
    thread_id: str
    message_ids: list[str]


class SkAccount(_Model):
    """계정 하나. `purpose`가 "e2e"면 종단 과제 사용자, "search"면 검색 세트 사용자다."""

    email: str
    name: str
    time_zone: str
    purpose: Literal["e2e", "search"]
    calendars: list[Calendar]
    events: list[CalendarEvent]
    threads: list[SkThread]
    messages: list[SkMessage]
    files: list[SkFile]


class SkPerson(_Model):
    key: str
    name: str
    email: str
    company: str
    role: str
    relation: str
    works_with: list[str]


class SearchMailbox(_Model):
    """검색 사용자의 메일함. 작은 메일함은 큰 메일함의 부분집합이다.

    `message_by_body`·`file_by_body`: body_id -> 이 계정의 메시지·파일 ID. 같은 body_id는 세 계정이
    공유한다(본문을 한 번만 생성한다). 시나리오 근거 참조는 가장 큰 메일함 계정의 ID를 쓴다.
    """

    size: int
    subset_of: str | None
    message_by_body: dict[str, str]
    file_by_body: dict[str, str]


# ---------------------------------------------------------------- 시나리오 기록


class ArtifactRef(_Model):
    """근거·방해 아티팩트 참조. 일정은 `calendar_id`가 함께 필요하다."""

    kind: Literal["event", "message", "file"]
    id: str
    calendar_id: str | None = None
    body_id: str | None = None


class TimeSpan(_Model):
    start: AwareDatetime
    end: AwareDatetime


class _Truth(_Model):
    """유형별 정답 재료의 공통 부모. 시각은 모두 시간대가 있다."""


class DayAgendaTruth(_Truth):
    """하루 일정. `events`는 그날(현지 날짜) 겹치는 확정 일정(시작 순)이고
    `cancelled`는 같은 날 취소된 일정이다."""

    type: Literal["day_agenda"] = "day_agenda"
    day: date
    time_zone: str
    events: list[ArtifactRef]
    cancelled: list[ArtifactRef]


class FreeSlotTruth(_Truth):
    """빈 시간. 근무 시간 `work`에서 `busy`를 뺀 `free_slots`(길이 `min_minutes` 이상, 시작 순)."""

    type: Literal["free_slot"] = "free_slot"
    day: date
    time_zone: str
    work: TimeSpan
    min_minutes: int
    busy: list[ArtifactRef]
    free_slots: list[TimeSpan]


class RescheduleTruth(_Truth):
    """일정 변경. 캘린더는 변경 후 시각이고, `change_message`가 변경 안내 메일이다."""

    type: Literal["reschedule"] = "reschedule"
    event: ArtifactRef
    title: str
    time_zone: str
    old: TimeSpan
    new: TimeSpan
    change_message: ArtifactRef


class CancellationTruth(_Truth):
    """취소된 일정(캘린더 status=cancelled)과 취소 안내 메일."""

    type: Literal["cancellation"] = "cancellation"
    event: ArtifactRef
    title: str
    time_zone: str
    original: TimeSpan
    cancel_message: ArtifactRef


class ProjectWeekTruth(_Truth):
    """프로젝트 주간 일정. `week_start`는 현지 월요일이고 `events`는 그 주의 확정 일정이다."""

    type: Literal["project_week"] = "project_week"
    project: str
    week_start: date
    time_zone: str
    events: list[ArtifactRef]
    cancelled: list[ArtifactRef]


class PersonNextTruth(_Truth):
    """인물과의 다음 미팅. `next_event`는 as_of 이후 첫 확정 일정, `upcoming`은 전체(2개 이상)다."""

    type: Literal["person_next"] = "person_next"
    person_name: str
    person_email: str
    as_of: AwareDatetime
    time_zone: str
    next_event: ArtifactRef
    upcoming: list[ArtifactRef]


class CrossMidnightTruth(_Truth):
    """자정을 넘는 일정. 시작일과 종료일(현지)이 다르다."""

    type: Literal["cross_midnight"] = "cross_midnight"
    event: ArtifactRef
    time_zone: str
    span: TimeSpan
    start_day: date
    end_day: date


class TzEventTruth(_Truth):
    """다른 시간대가 얽힌 일정. `variant`가 local이면 사용자 현지 일정(DST 전환 포함),
    foreign_attendee면 다른 시간대 참석자가 있는 일정이다. `span`은 사용자 시간대 기준이다."""

    type: Literal["tz_event"] = "tz_event"
    variant: Literal["local", "foreign_attendee"]
    event: ArtifactRef
    user_time_zone: str
    other_time_zone: str
    span: TimeSpan
    start_in_other_zone: AwareDatetime
    user_utc_offset_hours: float


class AllDayTruth(_Truth):
    """종일 일정(ADR-0027). 계약 해석은 캘린더 시간대 날짜 경계다. `intended_date`는 일정이 가리키는
    현지 날짜이고 `span`은 그날 현지 00:00~다음 날 현지 00:00인 순간이다(일광 절약 시간 전환일은
    24시간이 아닐 수 있다).

    `contract_caveat`는 UTC 날짜 경계 시절(ADR-0026 §3)의 표시다. 옛 스킬레톤을 읽을 수 있게 값만
    받고 새로 만들거나 저장하지 않는다.
    """

    type: Literal["all_day"] = "all_day"
    event: ArtifactRef
    intended_date: date
    span: TimeSpan
    time_zone: str
    contract_caveat: Literal["utc_date_boundary"] | None = Field(default=None, exclude=True)


class MailFactTruth(_Truth):
    """메일 사실. `latest`(최신 값)는 `latest_message`에, `previous`(정정 전 값)는
    `previous_message`에 있다. 같은 스레드일 수도, 다른 스레드일 수도 있다."""

    type: Literal["mail_fact"] = "mail_fact"
    fact_kind: Literal["deadline", "amount", "place", "decision", "attachment"]
    subject_hint: str
    project: str
    item: str
    question_key: (
        str  # "프로젝트|품목|질문 종류". 사용자별로 유일하다(deadline 시나리오와 같은 키 공간)
    )
    latest: Fact
    latest_message: ArtifactRef
    previous: Fact
    previous_message: ArtifactRef
    same_thread: bool


class ReplyNeededTruth(_Truth):
    """답장을 기다리는 스레드. 마지막 메일이 외부 발신자의 질문이다.

    정답은 이 스레드 하나이며, 구조상 답장 대기 상태인 배경 스레드가 많으므로 '답장이 필요한
    스레드 전체'를 정답 집합으로 쓰면 안 된다.
    """

    type: Literal["reply_needed"] = "reply_needed"
    thread_id: str
    awaiting_message: ArtifactRef
    sender_name: str
    sender_email: str
    subject: str
    asked_at: AwareDatetime
    question: Fact


class MeetingRequestTruth(_Truth):
    """메일로 온 미팅 요청. `revised`면 `superseded_message`의 제안 시각이 뒤 메일로 바뀌었다."""

    type: Literal["meeting_request"] = "meeting_request"
    message: ArtifactRef
    thread_id: str
    requester_name: str
    requester_email: str
    title: str
    proposed: TimeSpan
    time_zone: str
    location: str
    attendee_emails: list[str]
    revised: bool
    superseded_message: ArtifactRef | None
    conflicting_events: list[ArtifactRef]


class EventChangeTargetTruth(_Truth):
    """사용자가 옮기거나 취소할 일정. 요청 메일이 근거다. 취소면 `new`는 None이다."""

    type: Literal["event_change_target"] = "event_change_target"
    action: Literal["move", "cancel"]
    event: ArtifactRef
    title: str
    time_zone: str
    current: TimeSpan
    new: TimeSpan | None
    request_message: ArtifactRef
    attendee_emails: list[str]


class DocFactTruth(_Truth):
    """문서 사실(회의록 결정, 견적 금액, 계획서 일정)."""

    type: Literal["doc_fact"] = "doc_fact"
    doc_kind: Literal["meeting_notes", "quote", "plan"]
    file: ArtifactRef
    title: str
    question_key: str  # 사용자별로 유일하다
    fact: Fact


class DocVersionTruth(_Truth):
    """문서 판 구분(견적 v1/v2). `linked_message`는 첨부 이름이 최신 판 제목과 같은
    메일이다(없을 수 있다)."""

    type: Literal["doc_version"] = "doc_version"
    base_title: str
    question_key: str  # 사용자별로 유일하다
    latest_file: ArtifactRef
    latest_title: str
    latest: Fact
    previous_file: ArtifactRef
    previous_title: str
    previous: Fact
    linked_message: ArtifactRef | None
    attachment_name: str | None


class DeadlineTruth(_Truth):
    """리마인더 근거. `source`(메일 또는 문서)에 적힌 마감. as_of와의 차이를 계산해 둔다."""

    type: Literal["deadline"] = "deadline"
    source: ArtifactRef
    project: str
    item: str  # 품목 이름(프로젝트 이름은 `project`)
    question_key: str  # mail_fact(kind=deadline)와 같은 키 공간에서 사용자별로 유일하다
    deadline_at: AwareDatetime
    time_zone: str
    as_of: AwareDatetime
    hours_until: float
    days_until: int  # 현지 날짜 차이(마감일 - as_of 날짜)
    fact: Fact


class MismatchTruth(_Truth):
    """메일과 캘린더가 다른 일정. 캘린더가 최신 상태라 정답은 캘린더 값이다."""

    type: Literal["mismatch"] = "mismatch"
    event: ArtifactRef
    message: ArtifactRef
    title: str
    time_zone: str
    fields: list[Literal["time", "location"]]
    calendar_span: TimeSpan
    mail_start: AwareDatetime
    calendar_location: str
    mail_location: str


class AbsentTruth(_Truth):
    """답할 수 없는 질문. 생성기가 세계 전체를 검사해 `missing`이 정말 없음을 확인했다."""

    type: Literal["absent"] = "absent"
    variant: Literal["person_no_meeting", "no_document", "no_amount", "project_no_meeting"]
    missing: str  # 없는 것의 설명
    query: str  # 인물 이름, 프로젝트 이름, 문서 제목
    person_email: str | None = None
    scope: TimeSpan | None = None  # 일정 부재를 확인한 기간
    related_exist: list[ArtifactRef] = []  # 헷갈리게 하려고 둔 관련 아티팩트(방해)


class Interpretation(_Truth):
    """모호한 질문의 한 해석과 그 해석의 정답 근거."""

    label: str
    answer_kind: Literal["event", "event_set", "document"]
    gold: list[ArtifactRef]


class AmbiguousTruth(_Truth):
    """두 해석의 답이 다른 상황. `interpretations` 두 개의 `gold`가 서로 다르다(생성기가 검사)."""

    type: Literal["ambiguous"] = "ambiguous"
    variant: Literal["person_meetings", "same_project_notes", "homonym_person"]
    query_subject: str  # 모호한 대상(인물 이름, 프로젝트 이름)
    as_of: AwareDatetime
    interpretations: list[Interpretation] = Field(min_length=2, max_length=2)


class SearchTopicTruth(_Truth):
    """검색 주제. `relevance_by_body`: body_id -> 관련도(2 답의 근거, 1 부분 관련, 0 무관).

    같은 body_id가 세 검색 계정에 공유되므로 계정별 ID는 `search_mailboxes`로 찾는다.
    `fact`는 관련도 2 본문이 담은 답 사실이고 `topic_phrase`는 질의를 만들 주제어다.
    """

    type: Literal["search_topic"] = "search_topic"
    topic_phrase: str
    fact: Fact
    relevance_by_body: dict[str, Annotated[int, Field(ge=0, le=2)]]
    artifact_by_body: dict[str, Literal["mail", "document"]]


Truth = Annotated[
    DayAgendaTruth
    | FreeSlotTruth
    | RescheduleTruth
    | CancellationTruth
    | ProjectWeekTruth
    | PersonNextTruth
    | CrossMidnightTruth
    | TzEventTruth
    | AllDayTruth
    | MailFactTruth
    | ReplyNeededTruth
    | MeetingRequestTruth
    | EventChangeTargetTruth
    | DocFactTruth
    | DocVersionTruth
    | DeadlineTruth
    | MismatchTruth
    | AbsentTruth
    | AmbiguousTruth
    | SearchTopicTruth,
    Field(discriminator="type"),
]


class ScenarioRecord(_Model):
    """시나리오 기록 하나.

    - `evidence`: 정답 근거 아티팩트. 시나리오끼리 공유하지 않는다(배경 아티팩트 제외).
    - `distractors`: 헷갈리게 하려고 넣은 아티팩트.
    - `as_of`: 이 시나리오에서 만들 과제의 기준 시각 제안(사용자 현지 평일, 스냅샷 이후 1주 안).
    - `generator_family`: 근거·방해 본문을 쓸 계열. 근거 본문은 모두 이 계열이다.
    """

    scenario_id: str
    type: ScenarioType
    account_id: str
    as_of: AwareDatetime
    generator_family: Family
    evidence: list[ArtifactRef]
    distractors: list[ArtifactRef]
    truth: Truth

    @model_validator(mode="after")
    def _same_type(self) -> ScenarioRecord:
        if self.truth.type != self.type:
            raise ValueError("type과 truth.type이 다르다")
        return self


class Skeleton(_Model):
    schema_version: str = SCHEMA_VERSION
    seed: int
    world_model_sha256: str
    generator_git_sha: str
    snapshot_time: AwareDatetime
    accounts: dict[str, SkAccount]
    people: dict[str, SkPerson]
    bodies: dict[str, BodySpec]
    scenarios: list[ScenarioRecord]
    search_mailboxes: dict[str, SearchMailbox]
    notes: list[str]
