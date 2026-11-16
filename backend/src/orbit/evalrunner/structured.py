"""구조화된 답 채점 (ADR-0010 §1, ADR-0026 §4·§7). 순수 함수만 둔다.

평가 대상이 낸 `EpisodeOutput.structured`를 `gold.expected_answer`와 비교한다. 판정(`Verdict`):
- `answer_format`: 답이 없음, dict가 아님, 알 수 없는 kind, 필드 누락·추가 키, 오프셋 없는 시각,
  문자열이 아닌 ID, 집합 답의 중복 항목. 제출을 제출한 kind 기준으로 해석해 형식을 본다.
- `answer_kind`: 제출 kind가 정답과 다르지만 제출이 가리킨 대상 ID 집합(일정은
  (calendar_id, event_id), 메일 message_id, 문서 file_id)이 정답이 가리킨 집합과 같고 비어 있지
  않다. 답 종류만 틀린 경우다.
- `wrong_answer`: 형식은 맞지만 값이 다르다. kind가 다르고 대상 집합도 다르면(비었을 때 포함)
  여기다.
- `ambiguity_ignored`: 정답이 `clarify`인데 되묻지 않고 유효한 답을 냈다. 그 답이 `readings`
  중 몇 번째(0부터)와 일치하는지를 `Grade.matched_reading`에 함께 담는다(없으면 None).
- `unneeded_clarify`: 정답이 `clarify`가 아닌데 `clarify`를 냈다.
- 정답이 `abstain`이면 `abstain` 제출만 ok이고 다른 유효한 답은 `wrong_answer`다.
시각은 오프셋이 있는 ISO 8601만 받고 순간(instant)으로 비교한다(시간대만 다른 같은 순간은 일치).

`answer_ids`는 결과 행에 남기는 요약이다. 평가 대상이 낸 일정·메일·문서 ID와 시각만 담고(원문
문장 없음), 형식이 맞지 않거나 너무 긴 값은 `"<invalid>"`로 바꾼다.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from orbit.evaldata.schema import (
    ClarifyAnswer,
    DocumentAnswer,
    EventAnswer,
    EventSetAnswer,
    EventTimeAnswer,
    ExpectedAnswer,
    MessageSetAnswer,
    answer_signature,
)

Verdict = Literal[
    "ok",
    "answer_format",
    "answer_kind",
    "ambiguity_ignored",
    "unneeded_clarify",
    "wrong_answer",
]

_EVENT_KEYS = {"calendar_id", "event_id"}
_KEYS: dict[str, set[str]] = {
    "event_set": {"kind", "events"},
    "event": {"kind", "event"},
    "event_time": {"kind", "event", "start"},
    "time_slot": {"kind", "start"},
    "message_set": {"kind", "messages"},
    "document": {"kind", "document"},
    "abstain": {"kind"},
    "clarify": {"kind"},
}
_KINDS = tuple(_KEYS)
_ID = re.compile(r"[A-Za-z0-9_\-:.]{1,64}")
INVALID = "<invalid>"

_Target = tuple[str, ...]  # ("event", calendar_id, event_id) | ("message", id) | ("document", id)


@dataclass(frozen=True)
class Grade:
    verdict: Verdict
    matched_reading: int | None = None  # 정답이 clarify이고 `ambiguity_ignored`일 때만 값이 있다


def _event(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != _EVENT_KEYS:
        return None
    cal, ev = value["calendar_id"], value["event_id"]
    if not isinstance(cal, str) or not isinstance(ev, str) or not cal or not ev:
        return None
    return cal, ev


def _id_ref(value: Any, key: str) -> str | None:
    if not isinstance(value, Mapping) or set(value) != {key}:
        return None
    item = value[key]
    return item if isinstance(item, str) and item else None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _parse(structured: Any) -> tuple[tuple[str, Any], frozenset[_Target]] | None:
    """제출을 자기 kind 기준으로 해석해 (비교용 값, 대상 집합)을 돌려준다. 형식 오류면 None."""
    if not isinstance(structured, Mapping):
        return None
    kind = structured.get("kind")
    if not isinstance(kind, str) or kind not in _KEYS or set(structured) != _KEYS[kind]:
        return None

    if kind == "event_set":
        raw = structured["events"]
        if not isinstance(raw, list | tuple):
            return None
        events = [_event(e) for e in raw]
        found = {e for e in events if e is not None}
        if len(found) != len(events) or None in events:  # 형식 오류·중복 항목
            return None
        return (kind, frozenset(found)), frozenset(("event", *e) for e in found)
    if kind == "message_set":
        raw = structured["messages"]
        if not isinstance(raw, list | tuple):
            return None
        ids = [_id_ref(m, "message_id") for m in raw]
        id_set = {m for m in ids if m is not None}
        if len(id_set) != len(ids) or None in ids:
            return None
        return (kind, frozenset(id_set)), frozenset(("message", m) for m in id_set)
    if kind == "document":
        file_id = _id_ref(structured["document"], "file_id")
        if file_id is None:
            return None
        return (kind, file_id), frozenset({("document", file_id)})
    if kind in ("abstain", "clarify"):
        return (kind, None), frozenset()

    event: tuple[str, str] | None = None
    if kind in ("event", "event_time"):
        event = _event(structured["event"])
        if event is None:
            return None
    start: datetime | None = None
    if kind in ("event_time", "time_slot"):
        start = _instant(structured["start"])
        if start is None:
            return None
    targets = frozenset({("event", *event)}) if event is not None else frozenset()
    if kind == "event":
        return (kind, event), targets
    if kind == "event_time":
        return (kind, (event, start)), targets
    return (kind, start), targets


def _expected_targets(expected: ExpectedAnswer) -> frozenset[_Target]:
    if isinstance(expected, EventSetAnswer):
        return frozenset(("event", e.calendar_id, e.event_id) for e in expected.events)
    if isinstance(expected, EventAnswer | EventTimeAnswer):
        return frozenset({("event", expected.event.calendar_id, expected.event.event_id)})
    if isinstance(expected, MessageSetAnswer):
        return frozenset(("message", m.message_id) for m in expected.messages)
    if isinstance(expected, DocumentAnswer):
        return frozenset({("document", expected.document.file_id)})
    return frozenset()


def grade_structured(expected: ExpectedAnswer, structured: Mapping[str, Any] | None) -> Grade:
    parsed = _parse(structured)
    if parsed is None:
        return Grade("answer_format")
    submitted, targets = parsed
    kind = submitted[0]

    if isinstance(expected, ClarifyAnswer):
        if kind == "clarify":
            return Grade("ok")
        matched = next(
            (i for i, r in enumerate(expected.readings) if answer_signature(r) == submitted), None
        )
        return Grade("ambiguity_ignored", matched)
    if kind == "clarify":
        return Grade("unneeded_clarify")

    want = answer_signature(expected)
    if kind == expected.kind:
        return Grade("ok" if submitted == want else "wrong_answer")
    want_targets = _expected_targets(expected)
    if targets and targets == want_targets:
        return Grade("answer_kind")
    return Grade("wrong_answer")


def check_structured(expected: ExpectedAnswer, structured: Mapping[str, Any] | None) -> Verdict:
    return grade_structured(expected, structured).verdict


def _safe_id(value: Any) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else INVALID


def answer_ids(structured: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """결과 행에 남길 요약: kind, 일정·메일·문서 ID, 시작 시각(UTC ISO). 없으면 None."""
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
    if "messages" in structured:
        raw_messages = structured["messages"]
        out["messages"] = (
            [
                {"message_id": _safe_id(m.get("message_id") if isinstance(m, Mapping) else None)}
                for m in raw_messages[:200]
            ]
            if isinstance(raw_messages, list | tuple)
            else INVALID
        )
    if "document" in structured:
        doc = structured["document"]
        out["document"] = {
            "file_id": _safe_id(doc.get("file_id") if isinstance(doc, Mapping) else None)
        }
    if "event" in structured:
        out["event"] = ref(structured["event"])
    if "start" in structured:
        moment = _instant(structured["start"])
        out["start"] = moment.astimezone(UTC).isoformat() if moment is not None else INVALID
    return out
