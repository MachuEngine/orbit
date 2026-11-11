"""쓰기 동작 4종 (ADR-0012 2절). 인자는 제공자 ID로 해석된 뒤의 값이며, 시각은 aware만 받는다."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class _Action(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _check_emails(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        local, sep, domain = value.partition("@")
        if not sep or not local or not domain:
            raise ValueError("이메일 형식이 아니다")
    return values


class CreateEvent(_Action):
    kind: Literal["create_event"] = "create_event"
    calendar_id: str
    title: str
    start: AwareDatetime
    end: AwareDatetime
    attendees: tuple[str, ...] = ()
    description: str | None = None
    location: str | None = None

    @field_validator("attendees")
    @classmethod
    def _emails(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_emails(v)

    @model_validator(mode="after")
    def _order(self) -> CreateEvent:
        if not self.start < self.end:
            raise ValueError("end는 start보다 뒤여야 한다")
        return self


class UpdateEventTime(_Action):
    """일정의 시작·종료만 바꾼다."""

    kind: Literal["update_event_time"] = "update_event_time"
    calendar_id: str
    event_id: str
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _order(self) -> UpdateEventTime:
        if not self.start < self.end:
            raise ValueError("end는 start보다 뒤여야 한다")
        return self


class CancelEvent(_Action):
    kind: Literal["cancel_event"] = "cancel_event"
    calendar_id: str
    event_id: str


class SendMail(_Action):
    """새 메일 또는 스레드 답장. `reply_to_thread_id`가 있으면 그 스레드에 답장한다."""

    kind: Literal["send_mail"] = "send_mail"
    to: tuple[str, ...] = Field(min_length=1)
    cc: tuple[str, ...] = ()
    subject: str
    body: str
    reply_to_thread_id: str | None = None

    @field_validator("to", "cc")
    @classmethod
    def _emails(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_emails(v)


WriteAction = CreateEvent | UpdateEventTime | CancelEvent | SendMail
