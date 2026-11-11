"""승인 요청 요약. 이벤트에는 원문 대신 핸들·시각·개수만 담는다(ADR-0012 §4, ADR-0006 취지)."""

from collections.abc import Mapping
from typing import Any


def approval_summary(
    tool_name: str, args: Mapping[str, Any], handles: Mapping[str, str]
) -> dict[str, Any]:
    """`tool_call.args`(정규화된 인자)에서 이벤트에 실을 최소 요약을 만든다."""
    match tool_name:
        case "create_event":
            return {
                "start": args.get("start"),
                "end": args.get("end"),
                "attendee_count": len(args.get("attendees", [])),
            }
        case "update_event_time":
            return {
                "handle": handles.get("event"),
                "start": args.get("start"),
                "end": args.get("end"),
            }
        case "cancel_event":
            return {"handle": handles.get("event")}
        case "send_mail":
            return {
                "to_count": len(args.get("to", [])),
                "cc_count": len(args.get("cc", [])),
                "is_reply": args.get("reply_to_thread_id") is not None,
            }
        case _:
            return {}
