"""승인 화면에 보여 줄 대상 요약(ADR-0012 §4 보강).

승인 요청을 만드는 시점에 커넥터에서 대상의 제목·현재 시각을 읽어 `tool_call.provenance`에 함께
저장한다. 제공자 ID는 담지 않는다. 조회에 실패해도 승인 요청은 막지 않고 `target`만 비운다.
제목은 외부 콘텐츠일 수 있으므로 화면은 텍스트로만 보여 준다.
"""

from datetime import UTC, datetime
from typing import Any

from orbit.actions.models import CancelEvent, SendMail, UpdateEventTime, WriteAction
from orbit.connectors.contracts import CalendarConnector, ConnectorError, MailConnector

MAX_TARGET_TEXT = 300


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def build_target(
    action: WriteAction, *, calendar: CalendarConnector, mail: MailConnector
) -> dict[str, Any] | None:
    try:
        match action:
            case UpdateEventTime() | CancelEvent():
                event = await calendar.get_event(action.calendar_id, action.event_id)
                return {
                    "kind": "event",
                    "title": event.title[:MAX_TARGET_TEXT],
                    "start": _utc(event.start),
                    "end": _utc(event.end),
                }
            case SendMail() if action.reply_to_thread_id is not None:
                thread = await mail.get_thread(action.reply_to_thread_id)
                if not thread.messages:
                    return None
                return {"kind": "thread", "subject": thread.messages[0].subject[:MAX_TARGET_TEXT]}
            case _:
                return None
    except ConnectorError:
        return None


def public_target(stored: object) -> dict[str, Any] | None:
    """저장된 값에서 화면에 내보낼 키만 고른다. 형태가 다르면 None."""
    if not isinstance(stored, dict):
        return None
    if stored.get("kind") == "event":
        keys = ("title", "start", "end")
        if all(isinstance(stored.get(k), str) for k in keys):
            return {"kind": "event", **{k: stored[k] for k in keys}}
    if stored.get("kind") == "thread" and isinstance(stored.get("subject"), str):
        return {"kind": "thread", "subject": stored["subject"]}
    return None
