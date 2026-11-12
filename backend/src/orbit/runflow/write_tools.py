"""쓰기 도구 정의와 인자 해석 (ADR-0012 §4).

모델은 대상 일정·메일을 핸들(E1, M3)로 가리키고 수신자·참석자는 이메일로 적는다. 이 모듈이 핸들을
제공자 ID로 바꿔 `orbit.actions`의 동작 모델을 만든다. 도구 호출은 실행이 아니라 승인 요청이다.
오류는 `ToolInputError(kind)`로 알리고, 모델에는 `kind`만 돌려준다(원문·주소를 넣지 않는다).
"""

from typing import Any

from pydantic import ValidationError

from orbit.actions.models import CancelEvent, CreateEvent, SendMail, UpdateEventTime, WriteAction
from orbit.assistant.tools import ToolInputError, tool_specs
from orbit.assistant.workset import UnknownHandleError, WorkSet
from orbit.connectors.contracts import CalendarConnector, ConnectorError, MailConnector
from orbit.llm.types import ToolSpec
from orbit.runflow.context import CONVERSATION_CONTEXT, context_tool_spec

CREATE_EVENT = "create_event"
UPDATE_EVENT_TIME = "update_event_time"
CANCEL_EVENT = "cancel_event"
SEND_MAIL = "send_mail"
WRITE_TOOLS = frozenset({CREATE_EVENT, UPDATE_EVENT_TIME, CANCEL_EVENT, SEND_MAIL})
READ_TOOLS = frozenset(
    {"list_events", "get_event", "search_mail", "read_mail", CONVERSATION_CONTEXT}
)

MAX_TITLE = 200
MAX_SUBJECT = 300
MAX_BODY = 10_000
MAX_TEXT = 4_000
MAX_RECIPIENTS = 50

_EMAILS = {"type": "array", "items": {"type": "string"}, "description": "이메일 주소 목록"}
_TIME = {"type": "string", "description": "오프셋이 있는 ISO 8601"}


def write_tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            CREATE_EVENT,
            "일정을 만든다. 실행 전에 사용자 승인이 필요하다. 기본 캘린더에 만든다.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": _TIME,
                    "end": _TIME,
                    "attendees": _EMAILS,
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title", "start", "end"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            UPDATE_EVENT_TIME,
            "일정의 시작·종료 시각만 바꾼다. 실행 전에 사용자 승인이 필요하다.",
            {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "일정 핸들(예: E1)"},
                    "start": _TIME,
                    "end": _TIME,
                },
                "required": ["handle", "start", "end"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            CANCEL_EVENT,
            "일정을 취소한다. 실행 전에 사용자 승인이 필요하다.",
            {
                "type": "object",
                "properties": {"handle": {"type": "string", "description": "일정 핸들(예: E1)"}},
                "required": ["handle"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            SEND_MAIL,
            "메일을 보낸다. 실행 전에 사용자 승인이 필요하다. reply_to_handle을 주면 그 메일이 "
            "속한 스레드에 답장한다.",
            {
                "type": "object",
                "properties": {
                    "to": _EMAILS,
                    "cc": _EMAILS,
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "reply_to_handle": {"type": "string", "description": "메일 핸들(예: M1)"},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        ),
    )


def run_tool_specs() -> tuple[ToolSpec, ...]:
    """모델에 주는 도구: 읽기 4개(답 제출 제외), 대화 맥락 조회, 쓰기 4개."""
    reads = tuple(spec for spec in tool_specs() if spec.name in READ_TOOLS)
    return reads + (context_tool_spec(),) + write_tool_specs()


def _check_keys(args: dict[str, Any], required: set[str], optional: set[str]) -> None:
    if not required <= set(args) or set(args) - required - optional:
        raise ToolInputError("invalid_arguments")


def _text(value: object, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not value.strip() and not allow_empty):
        raise ToolInputError("invalid_arguments")
    return value


def _emails(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or len(value) > MAX_RECIPIENTS:
        raise ToolInputError("invalid_arguments")
    if not all(isinstance(v, str) for v in value):
        raise ToolInputError("invalid_arguments")
    return tuple(value)


async def build_action(
    name: str,
    args: object,
    *,
    workset: WorkSet,
    calendar: CalendarConnector,
    mail: MailConnector,
) -> WriteAction:
    """쓰기 도구 호출을 제공자 ID로 해석된 동작으로 바꾼다. 실패하면 `ToolInputError`."""
    if not isinstance(args, dict):
        raise ToolInputError("invalid_arguments")
    try:
        return await _build(name, args, workset, calendar, mail)
    except ValidationError:
        raise ToolInputError("invalid_arguments") from None
    except UnknownHandleError:
        raise ToolInputError("unknown_handle") from None
    except ConnectorError:
        raise ToolInputError("lookup_failed") from None


async def _build(
    name: str,
    args: dict[str, Any],
    workset: WorkSet,
    calendar: CalendarConnector,
    mail: MailConnector,
) -> WriteAction:
    if name == CREATE_EVENT:
        _check_keys(args, {"title", "start", "end"}, {"attendees", "description", "location"})
        calendars = await calendar.list_calendars()
        if not calendars:
            raise ToolInputError("lookup_failed")
        target = next((c for c in calendars if c.primary), calendars[0])
        return CreateEvent(
            calendar_id=target.id,
            title=_text(args["title"], MAX_TITLE),
            start=args["start"],
            end=args["end"],
            attendees=_emails(args.get("attendees", ())),
            description=_text(args.get("description", ""), MAX_TEXT, allow_empty=True),
            location=_text(args.get("location", ""), MAX_TITLE, allow_empty=True),
        )
    if name == UPDATE_EVENT_TIME:
        _check_keys(args, {"handle", "start", "end"}, set())
        calendar_id, event_id = workset.resolve_event(args["handle"])
        return UpdateEventTime(
            calendar_id=calendar_id, event_id=event_id, start=args["start"], end=args["end"]
        )
    if name == CANCEL_EVENT:
        _check_keys(args, {"handle"}, set())
        calendar_id, event_id = workset.resolve_event(args["handle"])
        return CancelEvent(calendar_id=calendar_id, event_id=event_id)
    if name == SEND_MAIL:
        _check_keys(args, {"to", "subject", "body"}, {"cc", "reply_to_handle"})
        thread_id: str | None = None
        if "reply_to_handle" in args:
            message = await mail.get_message(workset.resolve_message(args["reply_to_handle"]))
            thread_id = message.thread_id
        return SendMail(
            to=_emails(args["to"]),
            cc=_emails(args.get("cc", ())),
            subject=_text(args["subject"], MAX_SUBJECT),
            body=_text(args["body"], MAX_BODY),
            reply_to_thread_id=thread_id,
        )
    raise ToolInputError("unknown_tool")


def recipient_emails(action: WriteAction) -> list[str]:
    """출처를 분류할 이메일 목록(수신자·참석자)."""
    match action:
        case CreateEvent():
            return list(action.attendees)
        case SendMail():
            return [*action.to, *action.cc]
        case _:
            return []
