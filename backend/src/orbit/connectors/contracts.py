"""Connector 계약 v0 (ADR-0008): 오류, 값 객체, `CalendarConnector`·`MailConnector` Protocol.

- 계약 인스턴스는 연결 하나(계정 하나)에 묶인다. 워크스페이스·권한 판단은 계약 밖에서 한다.
- 모든 모델은 불변(pydantic v2 frozen)이며 시각은 시간대 정보가 있는 `datetime`만 받는다.
- 메서드는 모두 비동기다.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from orbit.core.ids import Uuid7Generator, uuid7

ORBIT_EXECUTION_HEADER = "X-Orbit-Execution-Id"

# ---------------------------------------------------------------- 오류


class ConnectorError(Exception):
    """계약이 던지는 모든 오류의 기반."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class NotFound(ConnectorError):
    """대상이 없다."""


class AlreadyExists(ConnectorError):
    """클라이언트가 지정한 ID가 이미 있다."""


class PreconditionFailed(ConnectorError):
    """`if_match` 버전이 현재 버전과 다르다."""


class RateLimited(ConnectorError):
    """호출 한도 초과."""

    def __init__(self, retry_after_s: float | None = None, message: str = "") -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class PermissionRevoked(ConnectorError):
    """연결 권한이 철회됐다."""


class InvalidQuery(ConnectorError):
    """지원하지 않는 검색 문법."""


class TransientError(ConnectorError):
    """일시 오류. 쓰기는 적용되지 않았음이 확실하다."""


class ResultUnknown(ConnectorError):
    """쓰기를 보냈지만 결과를 모른다. 재호출하지 말고 대조한다 (ADR-0003)."""


# ---------------------------------------------------------------- 모델 공통


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- 캘린더 모델

ResponseStatus = Literal["needs_action", "accepted", "declined", "tentative"]
EventStatus = Literal["confirmed", "cancelled"]


class Calendar(_Model):
    id: str
    name: str
    time_zone: str = "UTC"
    primary: bool = False


class Attendee(_Model):
    email: str
    name: str | None = None
    response_status: ResponseStatus = "needs_action"


def _check_order(start: datetime | None, end: datetime | None) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError("end는 start보다 앞설 수 없다")


class CalendarEvent(_Model):
    id: str
    calendar_id: str
    title: str
    description: str = ""
    location: str = ""
    start: AwareDatetime
    end: AwareDatetime
    all_day: bool = False
    attendees: tuple[Attendee, ...] = ()
    organizer: Attendee | None = None
    status: EventStatus = "confirmed"
    version: str
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _order(self) -> CalendarEvent:
        _check_order(self.start, self.end)
        return self


class EventDraft(_Model):
    """일정 생성 입력."""

    title: str
    start: AwareDatetime
    end: AwareDatetime
    description: str = ""
    location: str = ""
    all_day: bool = False
    attendees: tuple[Attendee, ...] = ()

    @model_validator(mode="after")
    def _order(self) -> EventDraft:
        _check_order(self.start, self.end)
        return self


class EventPatch(_Model):
    """일정 부분 수정 입력. `None`이 아닌 필드만 바꾼다(`attendees=()`는 참석자를 비운다)."""

    title: str | None = None
    description: str | None = None
    location: str | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    all_day: bool | None = None
    attendees: tuple[Attendee, ...] | None = None

    @model_validator(mode="after")
    def _order(self) -> EventPatch:
        _check_order(self.start, self.end)
        return self

    def changes(self) -> dict[str, object]:
        """지정된(None이 아닌) 필드만 모은다."""
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if getattr(self, name) is not None
        }


# ---------------------------------------------------------------- 메일 모델


class Address(_Model):
    email: str
    name: str | None = None


class AttachmentMeta(_Model):
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)


class MailMessage(_Model):
    id: str
    thread_id: str
    from_: Address = Field(alias="from")
    to: tuple[Address, ...] = ()
    cc: tuple[Address, ...] = ()
    subject: str
    body_text: str
    snippet: str = ""
    sent_at: AwareDatetime
    labels: tuple[str, ...] = ()
    attachments: tuple[AttachmentMeta, ...] = ()
    headers: dict[str, str] = Field(default_factory=dict)

    @property
    def has_attachment(self) -> bool:
        return bool(self.attachments)


class MessageSummary(_Model):
    """검색 결과의 메시지 요약. 본문은 `get_message`로 읽는다."""

    id: str
    thread_id: str
    from_: Address = Field(alias="from")
    subject: str
    snippet: str = ""
    sent_at: AwareDatetime
    labels: tuple[str, ...] = ()
    has_attachment: bool = False


class MailThread(_Model):
    id: str
    messages: tuple[MailMessage, ...] = ()


class DraftSpec(_Model):
    to: tuple[Address, ...]
    cc: tuple[Address, ...] = ()
    subject: str
    body_text: str
    reply_to_message_id: str | None = None
    execution_id: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        """초안에 넣을 추가 헤더. `execution_id`가 있으면 `X-Orbit-Execution-Id`를 넣는다."""
        if self.execution_id is None:
            return {}
        return {ORBIT_EXECUTION_HEADER: self.execution_id}


class Draft(_Model):
    draft_id: str
    message_id: str
    thread_id: str
    spec: DraftSpec | None = None  # 조회·생성 응답에서 내용을 확인할 수 있게 선택으로 둔다


class SearchPage(_Model):
    messages: tuple[MessageSummary, ...] = ()
    next_page_token: str | None = None


# ---------------------------------------------------------------- Protocol


@runtime_checkable
class CalendarConnector(Protocol):
    async def list_calendars(self) -> list[Calendar]: ...

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        query: str | None = None,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]: ...

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent: ...

    async def create_event(
        self, calendar_id: str, event_id: str, draft: EventDraft
    ) -> CalendarEvent: ...

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch, *, if_match: str
    ) -> CalendarEvent: ...

    async def cancel_event(
        self, calendar_id: str, event_id: str, *, if_match: str
    ) -> CalendarEvent: ...


@runtime_checkable
class MailConnector(Protocol):
    async def search_messages(
        self, query: str, *, max_results: int = 20, page_token: str | None = None
    ) -> SearchPage: ...

    async def get_message(self, message_id: str) -> MailMessage: ...

    async def get_thread(self, thread_id: str) -> MailThread: ...

    async def create_draft(self, spec: DraftSpec) -> Draft: ...

    async def get_draft(self, draft_id: str) -> Draft: ...

    async def send_draft(self, draft_id: str) -> MailMessage: ...

    async def delete_draft(self, draft_id: str) -> None: ...


# ---------------------------------------------------------------- 클라이언트 지정 일정 ID

_CLIENT_EVENT_ID = re.compile(r"[a-v0-9]{5,1024}")


def is_valid_client_event_id(value: str) -> bool:
    """Google 규칙: base32hex 소문자 `[a-v0-9]`, 5~1024자."""
    return _CLIENT_EVENT_ID.fullmatch(value) is not None


def validate_client_event_id(value: str) -> str:
    if not is_valid_client_event_id(value):
        raise ValueError("일정 ID는 [a-v0-9] 5~1024자여야 한다")
    return value


def event_id_from_uuid(value: uuid.UUID) -> str:
    """UUID를 하이픈 없는 16진 소문자 표기로 바꾼다(항상 유효한 일정 ID)."""
    return value.hex


def new_event_id(generator: Uuid7Generator | None = None) -> str:
    """UUIDv7(`orbit.core.ids`)로 클라이언트 지정 일정 ID를 만든다."""
    return event_id_from_uuid(uuid7(generator))
