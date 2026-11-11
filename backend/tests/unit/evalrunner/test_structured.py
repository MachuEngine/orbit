import json
from datetime import UTC, datetime
from typing import Any

import pytest

from orbit.evaldata.schema import (
    EventAnswer,
    EventRef,
    EventSetAnswer,
    EventTimeAnswer,
    TimeSlotAnswer,
)
from orbit.evalrunner.structured import answer_ids, check_structured

E1 = {"calendar_id": "c1", "event_id": "e1"}
E2 = {"calendar_id": "c1", "event_id": "e2"}
START = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)

SET = EventSetAnswer(
    events=[EventRef(calendar_id="c1", event_id="e1"), EventRef(calendar_id="c1", event_id="e2")]
)
EVENT = EventAnswer(event=EventRef(calendar_id="c1", event_id="e1"))
EVENT_TIME = EventTimeAnswer(event=EventRef(calendar_id="c1", event_id="e1"), start=START)
SLOT = TimeSlotAnswer(start=START)


def test_event_set_matches_regardless_of_order() -> None:
    assert check_structured(SET, {"kind": "event_set", "events": [E2, E1]}) == "ok"
    assert check_structured(SET, {"kind": "event_set", "events": [E1, E2]}) == "ok"


def test_event_set_value_mismatch_is_wrong_answer() -> None:
    other = {"calendar_id": "c1", "event_id": "e9"}
    assert check_structured(SET, {"kind": "event_set", "events": [E1]}) == "wrong_answer"
    assert check_structured(SET, {"kind": "event_set", "events": [E1, E2, other]}) == "wrong_answer"
    assert check_structured(SET, {"kind": "event_set", "events": [E1, other]}) == "wrong_answer"
    # 같은 event_id라도 캘린더가 다르면 다른 일정이다
    moved = {"calendar_id": "c2", "event_id": "e2"}
    assert check_structured(SET, {"kind": "event_set", "events": [E1, moved]}) == "wrong_answer"


def test_empty_event_set_expectation() -> None:
    empty = EventSetAnswer(events=[])
    assert check_structured(empty, {"kind": "event_set", "events": []}) == "ok"
    assert check_structured(empty, {"kind": "event_set", "events": [E1]}) == "wrong_answer"


def test_event_kind() -> None:
    assert check_structured(EVENT, {"kind": "event", "event": E1}) == "ok"
    assert check_structured(EVENT, {"kind": "event", "event": E2}) == "wrong_answer"


def test_event_time_requires_same_event_and_same_instant() -> None:
    good = {"kind": "event_time", "event": E1, "start": "2026-11-12T01:00:00+00:00"}
    assert check_structured(EVENT_TIME, good) == "ok"
    # 시간대만 다른 같은 순간
    kst = {**good, "start": "2026-11-12T10:00:00+09:00"}
    assert check_structured(EVENT_TIME, kst) == "ok"
    assert check_structured(EVENT_TIME, {**good, "start": "2026-11-12T01:30:00+00:00"}) == (
        "wrong_answer"
    )
    assert check_structured(EVENT_TIME, {**good, "event": E2}) == "wrong_answer"


def test_time_slot() -> None:
    assert (
        check_structured(SLOT, {"kind": "time_slot", "start": "2026-11-12T10:00:00+09:00"}) == "ok"
    )
    assert check_structured(SLOT, {"kind": "time_slot", "start": "2026-11-12T10:00:00+00:00"}) == (
        "wrong_answer"
    )
    assert check_structured(SLOT, {"kind": "time_slot", "start": "2026-11-12T01:00:00Z"}) == "ok"


@pytest.mark.parametrize(
    "structured",
    [
        None,
        "문자열",
        [],
        {},
        {"kind": "event", "events": [E1]},  # kind 불일치
        {"kind": "event_set"},  # 필드 누락
        {"kind": "event_set", "events": [E1, E2], "note": "x"},  # 추가 키
        {"kind": "event_set", "events": "e1"},
        {"kind": "event_set", "events": [E1, E1, E2]},  # 중복 항목
        {"kind": "event_set", "events": [{"calendar_id": "c1"}]},
        {"kind": "event_set", "events": [{**E1, "extra": 1}]},
        {"kind": "event_set", "events": [{"calendar_id": 1, "event_id": "e1"}]},
        {"kind": "event_set", "events": ["e1"]},
    ],
)
def test_event_set_format_errors(structured: Any) -> None:
    assert check_structured(SET, structured) == "answer_format"


@pytest.mark.parametrize(
    "structured",
    [
        {"kind": "event_time", "event": E1},  # start 누락
        {"kind": "event_time", "start": "2026-11-12T01:00:00+00:00"},  # event 누락
        {"kind": "event_time", "event": E1, "start": "2026-11-12T01:00:00"},  # 오프셋 없음
        {"kind": "event_time", "event": E1, "start": "내일 10시"},
        {"kind": "event_time", "event": E1, "start": 1_700_000_000},
        {"kind": "event_time", "event": "e1", "start": "2026-11-12T01:00:00+00:00"},
        {"kind": "event_time", "event": E1, "start": "2026-11-12T01:00:00+00:00", "x": 1},
        {"kind": "time_slot", "start": "2026-11-12T01:00:00+00:00"},  # kind 불일치
    ],
)
def test_event_time_format_errors(structured: Any) -> None:
    assert check_structured(EVENT_TIME, structured) == "answer_format"


def test_time_slot_naive_time_is_format_error() -> None:
    assert check_structured(SLOT, {"kind": "time_slot", "start": "2026-11-12T10:00:00"}) == (
        "answer_format"
    )
    assert check_structured(SLOT, {"kind": "time_slot"}) == "answer_format"
    assert check_structured(SLOT, {"kind": "time_slot", "start": "x", "event": E1}) == (
        "answer_format"
    )


def test_answer_ids_keeps_only_ids_and_utc_instants() -> None:
    got = answer_ids(
        {
            "kind": "event_time",
            "event": E1,
            "start": "2026-11-12T10:00:00+09:00",
            "note": "free text",
        }
    )
    assert got == {"kind": "event_time", "event": E1, "start": "2026-11-12T01:00:00+00:00"}
    assert answer_ids({"kind": "event_set", "events": [E2, E1]}) == {
        "kind": "event_set",
        "events": [E2, E1],
    }
    assert answer_ids(None) is None and answer_ids("x") is None  # type: ignore[arg-type]


def test_answer_ids_never_copies_free_text() -> None:
    sentence = "10시에 회의가 있습니다. 비밀 문장"
    got = answer_ids(
        {
            "kind": sentence,
            "event": {"calendar_id": sentence, "event_id": "x" * 500},
            "start": sentence,
            "events": sentence,
        }
    )
    assert got is not None and "비밀" not in json.dumps(got, ensure_ascii=False)
    assert got["kind"] == "<invalid>" and got["start"] == "<invalid>"
    assert got["event"] == {"calendar_id": "<invalid>", "event_id": "<invalid>"}
