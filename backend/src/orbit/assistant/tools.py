"""읽기 전용 도구 정의와 실행 (ADR-0011).

- 도구 결과의 일정·메일은 핸들과 요약 필드로만 모델에 보인다. 제공자 ID는 결과에 넣지 않는다.
- 메일 본문·일정 설명은 `<external_content>` 표지로 감싸고 길이 상한을 둔다 (D6-1).
- 인자 검증 실패와 커넥터 오류는 `{"error": "<종류>"}`로 돌려준다. 원문 메시지는 넣지 않는다.
- 쓰기 도구는 정의하지 않는다.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from orbit.assistant.workset import UnknownHandleError, WorkSet
from orbit.connectors.contracts import (
    Address,
    Attendee,
    CalendarConnector,
    CalendarEvent,
    ConnectorError,
    InvalidQuery,
    MailConnector,
    NotFound,
    PermissionRevoked,
    RateLimited,
    TransientError,
)
from orbit.llm.types import ToolSpec

LIST_EVENTS = "list_events"
GET_EVENT = "get_event"
SEARCH_MAIL = "search_mail"
READ_MAIL = "read_mail"
SUBMIT_ANSWER = "submit_answer"

EXTERNAL_OPEN = "<external_content>"
EXTERNAL_CLOSE = "</external_content>"
MAX_EXTERNAL_CHARS = 4000
MAX_FIELD_CHARS = 200  # 제목·이름·장소 같은 짧은 외부 문자열 필드의 상한
_MARKER_START = re.compile(r"<(\s*/?\s*external_content)", re.IGNORECASE)
MAX_SEARCH_RESULTS = 20
DEFAULT_SEARCH_RESULTS = 10

ANSWER_KINDS = ("event_set", "event", "event_time", "time_slot")

_SEARCH_SYNTAX = (
    "검색 문법은 부분집합만 지원한다: from:, to:, subject:, after:·before:(YYYY/MM/DD), "
    "has:attachment, in:sent·in:inbox·in:drafts, 일반 키워드, 큰따옴표 구문. "
    "조건은 모두 AND다. OR, -(제외), label: 같은 다른 연산자는 오류가 난다."
)


def tool_specs() -> tuple[ToolSpec, ...]:
    """모델에 노출하는 도구. 전부 읽기 전용이고 답 제출 도구 하나가 있다."""
    return (
        ToolSpec(
            LIST_EVENTS,
            "계정의 모든 캘린더에서 기간 안의 일정을 시작 시각 순으로 조회한다. 취소된 일정은 "
            "포함하지 않는다. 시각은 오프셋이 있는 ISO 8601이다. 종일 일정은 날짜(YYYY-MM-DD)로 "
            "표시하며 end는 배타적이다.",
            {
                "type": "object",
                "properties": {
                    "time_min": {"type": "string", "description": "기간 시작(포함), 오프셋 필수"},
                    "time_max": {"type": "string", "description": "기간 끝(미포함), 오프셋 필수"},
                    "query": {"type": "string", "description": "제목 등에서 찾을 키워드(선택)"},
                },
                "required": ["time_min", "time_max"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            GET_EVENT,
            "일정 하나의 상세를 조회한다. 설명은 외부 콘텐츠 표지 안에 담겨 온다.",
            {
                "type": "object",
                "properties": {"handle": {"type": "string", "description": "일정 핸들(예: E1)"}},
                "required": ["handle"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            SEARCH_MAIL,
            f"메일을 검색해 요약 목록을 돌려준다. {_SEARCH_SYNTAX}",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색어"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_RESULTS,
                        "description": f"기본 {DEFAULT_SEARCH_RESULTS}",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            READ_MAIL,
            "메일 한 통을 읽는다. 본문은 외부 콘텐츠 표지 안에 담겨 온다.",
            {
                "type": "object",
                "properties": {"handle": {"type": "string", "description": "메일 핸들(예: M1)"}},
                "required": ["handle"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            SUBMIT_ANSWER,
            "답이 정해지면 구조화된 답을 제출한다. kind에 따라 필요한 필드가 다르다: "
            "event_set은 events, event는 event, event_time은 event와 start, "
            "time_slot은 start. start는 오프셋이 있는 ISO 8601이다.",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(ANSWER_KINDS)},
                    "events": {"type": "array", "items": {"type": "string"}},
                    "event": {"type": "string"},
                    "start": {"type": "string"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        ),
    )


class ToolInputError(ValueError):
    """모델이 낸 도구 인자가 맞지 않는다. `kind`만 모델에 돌려준다."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class ToolOutcome:
    content: str  # 모델에 돌려줄 JSON 문자열
    structured: dict[str, Any] | None = None  # 유효한 `submit_answer`일 때만
    is_error: bool = False


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def wrap_external(text: str) -> tuple[str, bool]:
    """본문을 데이터 표지로 감싼다. 상한을 넘으면 자르고 True를 함께 돌려준다."""
    truncated = len(text) > MAX_EXTERNAL_CHARS
    body = text[:MAX_EXTERNAL_CHARS]
    # 본문 안의 표지 문자열(대소문자·공백 변형 포함)로 표지를 일찍 닫지 못하게 한다.
    body = _MARKER_START.sub(r"&lt;\1", body)
    return f"{EXTERNAL_OPEN}{body}{EXTERNAL_CLOSE}", truncated


def _short(text: str | None) -> str | None:
    """짧은 외부 문자열 필드에 길이 상한을 건다."""
    return None if text is None else text[:MAX_FIELD_CHARS]


def _parse_aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise ToolInputError("invalid_time")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ToolInputError("invalid_time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolInputError("invalid_time")
    return parsed


def _person(p: Address | Attendee | None) -> dict[str, str | None] | None:
    if p is None:
        return None
    return {"name": _short(p.name), "email": _short(p.email)}


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, ToolInputError):
        return exc.kind
    if isinstance(exc, UnknownHandleError):
        return "unknown_handle"
    if isinstance(exc, InvalidQuery):
        return "invalid_query"
    if isinstance(exc, NotFound):
        return "not_found"
    if isinstance(exc, RateLimited):
        return "rate_limited"
    if isinstance(exc, PermissionRevoked):
        return "permission_revoked"
    if isinstance(exc, TransientError):
        return "transient_error"
    if isinstance(exc, ConnectorError):
        return "connector_error"
    return "invalid_arguments"


class ReadOnlyTools:
    """한 대화의 도구 실행기. 작업 집합(`WorkSet`)을 대화 동안 유지한다."""

    def __init__(
        self,
        *,
        calendar: CalendarConnector,
        mail: MailConnector,
        tz: tzinfo,
        workset: WorkSet | None = None,
    ) -> None:
        self._calendar = calendar
        self._mail = mail
        self._tz = tz
        self.workset = workset if workset is not None else WorkSet()

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """도구를 실행한다. 인자·커넥터 오류는 `{"error": 종류}` 결과로 돌려준다."""
        try:
            if name == SUBMIT_ANSWER:
                structured = self._submit(arguments)
                return ToolOutcome(_dump({"status": "submitted"}), structured=structured)
            handler = {
                LIST_EVENTS: self._list_events,
                GET_EVENT: self._get_event,
                SEARCH_MAIL: self._search_mail,
                READ_MAIL: self._read_mail,
            }.get(name)
            if handler is None:
                raise ToolInputError("unknown_tool")
            if not isinstance(arguments, dict):
                raise ToolInputError("invalid_arguments")
            return ToolOutcome(_dump(await handler(arguments)))
        except (ToolInputError, UnknownHandleError, ConnectorError) as exc:
            return ToolOutcome(_dump({"error": _error_kind(exc)}), is_error=True)

    # ------------------------------------------------------------ 캘린더

    def _local(self, moment: datetime) -> str:
        return moment.astimezone(self._tz).isoformat()

    def _event_summary(self, event: CalendarEvent) -> dict[str, Any]:
        if event.all_day:  # 종일 일정은 UTC 날짜 경계다(계약). 시간대 변환을 하지 않는다.
            start, end = event.start.date().isoformat(), event.end.date().isoformat()
        else:
            start, end = self._local(event.start), self._local(event.end)
        return {
            "handle": self.workset.event_handle(event.calendar_id, event.id),
            "title": _short(event.title),
            "start": start,
            "end": end,
            "all_day": event.all_day,
            "location": _short(event.location),
            "attendees": [_person(a) for a in event.attendees],
            "organizer": _person(event.organizer),
            "status": event.status,
        }

    async def _list_events(self, args: dict[str, Any]) -> dict[str, Any]:
        if not {"time_min", "time_max"} <= set(args) or set(args) - {
            "time_min",
            "time_max",
            "query",
        }:
            raise ToolInputError("invalid_arguments")
        time_min, time_max = _parse_aware(args["time_min"]), _parse_aware(args["time_max"])
        if time_min >= time_max:
            raise ToolInputError("invalid_time_range")
        query = args.get("query")
        if query is not None and not isinstance(query, str):
            raise ToolInputError("invalid_arguments")
        events: list[CalendarEvent] = []
        for calendar in await self._calendar.list_calendars():
            events += await self._calendar.list_events(
                calendar.id, time_min, time_max, query=query or None
            )
        events.sort(key=lambda e: (e.start, e.end, e.title, e.calendar_id, e.id))
        return {"events": [self._event_summary(e) for e in events]}

    async def _get_event(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"handle"}:
            raise ToolInputError("invalid_arguments")
        calendar_id, event_id = self.workset.resolve_event(args["handle"])
        event = await self._calendar.get_event(calendar_id, event_id)
        summary = self._event_summary(event)
        description, truncated = wrap_external(event.description)
        summary["description"] = description
        summary["description_truncated"] = truncated
        return summary

    # ------------------------------------------------------------ 메일

    async def _search_mail(self, args: dict[str, Any]) -> dict[str, Any]:
        if "query" not in args or set(args) - {"query", "max_results"}:
            raise ToolInputError("invalid_arguments")
        query = args["query"]
        limit = args.get("max_results", DEFAULT_SEARCH_RESULTS)
        if not isinstance(query, str) or not query.strip():
            raise ToolInputError("invalid_arguments")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ToolInputError("invalid_arguments")
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ToolInputError("invalid_arguments")
        page = await self._mail.search_messages(query, max_results=limit)
        return {
            "messages": [
                {
                    "handle": self.workset.message_handle(m.id),
                    "subject": _short(m.subject),
                    "from": _person(m.from_),
                    "date": self._local(m.sent_at),
                    "has_attachment": m.has_attachment,
                }
                for m in page.messages
            ],
            "has_more": page.next_page_token is not None,
        }

    async def _read_mail(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"handle"}:
            raise ToolInputError("invalid_arguments")
        message_id = self.workset.resolve_message(args["handle"])
        message = await self._mail.get_message(message_id)
        body, truncated = wrap_external(message.body_text)
        return {
            "handle": self.workset.message_handle(message.id),
            "subject": _short(message.subject),
            "from": _person(message.from_),
            "to": [_person(a) for a in message.to],
            "cc": [_person(a) for a in message.cc],
            "date": self._local(message.sent_at),
            "has_attachment": message.has_attachment,
            "body": body,
            "body_truncated": truncated,
        }

    # ------------------------------------------------------------ 답 제출

    def _event_ref(self, handle: object) -> dict[str, str]:
        calendar_id, event_id = self.workset.resolve_event(handle)
        return {"calendar_id": calendar_id, "event_id": event_id}

    def _submit(self, args: dict[str, Any]) -> dict[str, Any]:
        """핸들 기반 답을 ADR-0010 형식(제공자 ID 기반)으로 바꾼다. 필요 없는 필드는 버린다."""
        if not isinstance(args, dict):
            raise ToolInputError("invalid_arguments")
        kind = args.get("kind")
        if kind not in ANSWER_KINDS:
            raise ToolInputError("invalid_kind")
        if kind == "event_set":
            raw = args.get("events")
            if not isinstance(raw, list | tuple):
                raise ToolInputError("invalid_arguments")
            refs: list[dict[str, str]] = []
            for handle in raw:
                ref = self._event_ref(handle)
                if ref not in refs:  # 같은 핸들 중복은 한 번만 담는다
                    refs.append(ref)
            return {"kind": kind, "events": refs}
        answer: dict[str, Any] = {"kind": kind}
        if kind in ("event", "event_time"):
            answer["event"] = self._event_ref(args.get("event"))
        if kind in ("event_time", "time_slot"):
            answer["start"] = _parse_aware(args.get("start")).isoformat()
        return answer
