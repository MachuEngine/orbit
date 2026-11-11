"""`args_hash` (ADR-0012 2절): 같은 의미의 다른 표현은 같은 해시, 다른 의미는 다른 해시.

정규화: 동작 종류 포함, 키 정렬, 시각 UTC, 문자열 NFC, 이메일 소문자, 수신자·참석자 목록은
중복 제거 후 정렬(`to`와 `cc`는 따로), `description`·`location`의 `None`은 빈 문자열과 같다.
정규 JSON(공백 없음, 키 정렬, 비ASCII 유지)의 SHA-256 16진 문자열을 돌려준다.
"""

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from typing import Any

from orbit.actions.models import CancelEvent, CreateEvent, SendMail, UpdateEventTime, WriteAction


def _s(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _t(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_email(value: str) -> str:
    return _s(value).lower()


def _emails(values: tuple[str, ...]) -> list[str]:
    return sorted({normalize_email(v) for v in values})


def canonical_args(action: WriteAction) -> dict[str, Any]:
    match action:
        case CreateEvent():
            return {
                "kind": action.kind,
                "calendar_id": _s(action.calendar_id),
                "title": _s(action.title),
                "start": _t(action.start),
                "end": _t(action.end),
                "attendees": _emails(action.attendees),
                "description": _s(action.description or ""),
                "location": _s(action.location or ""),
            }
        case UpdateEventTime():
            return {
                "kind": action.kind,
                "calendar_id": _s(action.calendar_id),
                "event_id": _s(action.event_id),
                "start": _t(action.start),
                "end": _t(action.end),
            }
        case CancelEvent():
            return {
                "kind": action.kind,
                "calendar_id": _s(action.calendar_id),
                "event_id": _s(action.event_id),
            }
        case SendMail():
            return {
                "kind": action.kind,
                "to": _emails(action.to),
                "cc": _emails(action.cc),
                "subject": _s(action.subject),
                "body": _s(action.body),
                "reply_to_thread_id": (
                    None if action.reply_to_thread_id is None else _s(action.reply_to_thread_id)
                ),
            }


def args_hash(action: WriteAction) -> str:
    text = json.dumps(
        canonical_args(action), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
