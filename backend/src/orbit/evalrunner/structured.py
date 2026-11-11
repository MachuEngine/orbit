"""구조화된 답 채점 (ADR-0010 §1). 순수 함수만 둔다.

평가 대상이 낸 `EpisodeOutput.structured`를 `gold.expected_answer`와 비교한다.
- 형식 오류(`answer_format`): 답이 없음, dict가 아님, kind 불일치, 필드 누락·추가 키,
  오프셋 없는 시각, 문자열이 아닌 ID, `event_set`의 중복 항목.
- 값 불일치(`wrong_answer`): 형식은 맞지만 일정 집합·일정·시각이 다르다.
시각은 오프셋이 있는 ISO 8601만 받고 순간(instant)으로 비교한다(시간대만 다른 같은 순간은 일치).

`answer_ids`는 결과 행에 남기는 요약이다. 평가 대상이 낸 일정 ID와 시각만 담고(원문 문장 없음),
형식이 맞지 않거나 너무 긴 값은 `"<invalid>"`로 바꾼다.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from orbit.evaldata.schema import (
    EventAnswer,
    EventSetAnswer,
    EventTimeAnswer,
    ExpectedAnswer,
    TimeSlotAnswer,
)

Verdict = Literal["ok", "answer_format", "wrong_answer"]

_EVENT_KEYS = {"calendar_id", "event_id"}
_KINDS = ("event_set", "event", "event_time", "time_slot")
_ID = re.compile(r"[A-Za-z0-9_\-:.]{1,64}")
INVALID = "<invalid>"


def _event(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != _EVENT_KEYS:
        return None
    cal, ev = value["calendar_id"], value["event_id"]
    if not isinstance(cal, str) or not isinstance(ev, str) or not cal or not ev:
        return None
    return cal, ev


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def check_structured(expected: ExpectedAnswer, structured: Mapping[str, Any] | None) -> Verdict:
    if not isinstance(structured, Mapping):
        return "answer_format"
    keys = {
        "event_set": {"kind", "events"},
        "event": {"kind", "event"},
        "event_time": {"kind", "event", "start"},
        "time_slot": {"kind", "start"},
    }[expected.kind]
    if structured.get("kind") != expected.kind or set(structured) != keys:
        return "answer_format"

    if isinstance(expected, EventSetAnswer):
        raw = structured["events"]
        if not isinstance(raw, list | tuple):
            return "answer_format"
        parsed = [_event(e) for e in raw]
        if any(e is None for e in parsed):
            return "answer_format"
        got = {e for e in parsed if e is not None}
        if len(got) != len(parsed):  # 중복 항목
            return "answer_format"
        want = {(e.calendar_id, e.event_id) for e in expected.events}
        return "ok" if got == want else "wrong_answer"

    event_ok = True
    if isinstance(expected, EventAnswer | EventTimeAnswer):
        got_event = _event(structured["event"])
        if got_event is None:
            return "answer_format"
        event_ok = got_event == (expected.event.calendar_id, expected.event.event_id)
    start_ok = True
    if isinstance(expected, EventTimeAnswer | TimeSlotAnswer):
        got_start = _instant(structured["start"])
        if got_start is None:
            return "answer_format"
        start_ok = got_start == expected.start
    return "ok" if event_ok and start_ok else "wrong_answer"


def _safe_id(value: Any) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else INVALID


def answer_ids(structured: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """결과 행에 남길 요약: kind, 일정 ID(캘린더·일정), 시작 시각(UTC ISO). 없으면 None."""
    if not isinstance(structured, Mapping):
        return None
    out: dict[str, Any] = {}
    kind = structured.get("kind")
    out["kind"] = kind if kind in _KINDS else INVALID

    def ref(value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            return {"calendar_id": INVALID, "event_id": INVALID}
        return {
            "calendar_id": _safe_id(value.get("calendar_id")),
            "event_id": _safe_id(value.get("event_id")),
        }

    if "events" in structured:
        raw = structured["events"]
        out["events"] = [ref(e) for e in raw[:200]] if isinstance(raw, list | tuple) else INVALID
    if "event" in structured:
        out["event"] = ref(structured["event"])
    if "start" in structured:
        moment = _instant(structured["start"])
        out["start"] = moment.astimezone(UTC).isoformat() if moment is not None else INVALID
    return out
